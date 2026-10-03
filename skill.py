"""
Deep Search Skill —— 编排层
- 抓取所有有效 URL，合并全部正文，实现"多多益善"
- web_fetch 直导抓取（L0 httpx → L1 AsyncFetcher → L2 隐身浏览器 三级降级链）

模块化结构（全部平铺在顶层）：
- web_fetch.py        ScraplingManager —— 网页抓取（去重 / 降级链 / 解析 / 清理）。
                      ⚠️ 文件名避开了第三方 `scrapling` 包命名空间冲突。
- filters.py          正文质量过滤（content_score 连续分 / 截断优先级）
- filters_keywords.py 过滤常量表（黑名单 / 乱码 / 模板 / 域名权重）
- filters_learn.py    自适应降权学习（抓取失败 / 慢站 / 低质模板记负样本，30 天衰减）
- helpers.py          URL 归一化 / 日期归一化
- config.py           DeepSearchConfig —— 全部可调参数（抓取量 / 去重 / 同 host 限条 / 超时）
- skill.py            主编排器（本文件）—— execute / 流水线 / search 调用
- search_engine/      搜索核心（不归本 skill 管，由 search(query) 单入口对外）
"""

import asyncio
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple, Set

from .config import ARCHIVE_DIR, DeepSearchConfig
from .web_fetch import ScraplingManager
from . import helpers
from . import filters
from . import filters_learn
from . import ledger


# ---------- 归档落盘位置（每次搜索任务一个时间戳文件夹，轮次续号） ----------
# ⚠️ 同一分钟内重启会落到同一文件夹，_ARCHIVE_SEQ 必须从目录已有文件名续号，
#    否则新会话的 `01_` 会静默覆盖上一个会话的 `01_`。
# ⚠️ 文件夹名冒号必须全角（半角 `:` 是 Windows 非法文件名字符，建目录直接失败）；
#    月/日补零保证按名排序。改归档样式要同步 tests/test_archive.py。
_ARCHIVE_SESSION_DIR: Optional[str] = None
_ARCHIVE_SEQ = 0
_RE_ARCHIVE_SEQ = re.compile(r"^(\d+)_")


def _archive_stamp() -> str:
    """归档文件夹名：`2026年 09月23日 22：09`（年月日 + 时分；月/日补零，冒号全角）。"""
    now = datetime.now()
    return f"{now.year}年 {now:%m}月{now:%d}日 {now:%H}：{now:%M}"


def _archive_filename(query: str) -> str:
    """关键词 → 归档文件名主体（清洗共用 helpers.safe_filename；挑不出名字兜底为 `query`）。"""
    return helpers.safe_filename(query) or "query"


def _archive_resume_seq(session_dir: str) -> int:
    """目录里已有的最大轮次序号（新会话接着往下编，不覆盖同名文件）。"""
    try:
        return max((int(m.group(1)) for fn in os.listdir(session_dir)
                    if (m := _RE_ARCHIVE_SEQ.match(fn))), default=0)
    except OSError:
        return 0


# ---------- 主Skill（编排器） ----------
class DeepSearchSkill:
    def __init__(
        self,
        config: Optional[DeepSearchConfig] = None,
    ):
        self.config = config or DeepSearchConfig()

        # 日志与 server.py 同一落盘文件；级别固定 INFO（全仓无 logger.debug，可调级别是空操作）。
        self.logger = helpers.setup_logger("deep_search")

        # 组合各管理器（依赖注入：共享 config / logger）。
        # deep_search 工具由 server.py 的 @mcp.tool() 定义，此处不维护工具自描述列表。
        self.scrapling = ScraplingManager(self.config, self.logger)

    # ---------- 统一入口 ----------
    async def execute(self, query: str, **kwargs) -> Dict[str, Any]:
        """
        执行深度搜索：Go CLI（metasearch_cli_windows_amd64.exe）多源搜索 → Scrapling 抓取正文 → 合并返回

        :param query: 搜索关键词
        :param kwargs: 唯一认的参数是 `interactions`（交互抓取指令 scroll / wait）；
            其余一律忽略：不生效也不报错。
        :return: { "success": bool, "answer": str, "source": "...", "metadata": dict }
        """
        start_time = time.time()
        result = {"success": False, "answer": "", "source": "", "metadata": {}}

        # 条数与超时都没有 kwargs 旁路（覆盖通道已删），唯一控制点是 config。
        max_urls = self.config.max_urls_to_try
        overall_timeout = self.config.overall_timeout

        # 交互式抓取指令：滚动/等待，经 fetch_all 传递给 Scrapling。
        interactions: List[Dict] = list(kwargs.get("interactions") or [])
        try:
            return await asyncio.wait_for(
                self._run_pipeline(query, max_urls, result, interactions),
                timeout=overall_timeout
            )
        except asyncio.TimeoutError:
            self.logger.error("Overall pipeline timeout")
            result["metadata"]["error"] = "Overall timeout"
            result["answer"] = "Request timed out. Please try again later."
            result["success"] = False
        except Exception as e:
            self.logger.exception(f"Unexpected pipeline error: {e}")
            result["metadata"]["error"] = str(e)
            result["answer"] = "Internal error occurred."
            result["success"] = False
        finally:
            result["metadata"]["elapsed"] = time.time() - start_time
            # 归档无条件落盘（无开关）；只写真抓到正文的轮次，失败/超时的落盘只是垃圾。
            if result.get("answer") and result.get("metadata", {}).get("fetched_count", 0) > 0:
                _th = time.perf_counter()
                archive_path = self._save_archive(query, result)
                self.logger.info(
                    "Pipeline stage archive: %.2fs", time.perf_counter() - _th
                )
                if archive_path:
                    result["metadata"]["archive_path"] = archive_path
            # 账本记账在 _run_pipeline 里（需要交付条目的正文，这里没有）。
            return result

    # ---------- 内部流水线 ----------
    async def _run_pipeline(
        self,
        query: str,
        max_urls: int,
        result: Dict,
        interactions: Optional[List[Dict]] = None,
    ) -> Dict:
        # 阶段计时（纯日志）：perf_counter 单调时钟，用于定位各阶段耗时空窗。
        _t = time.perf_counter()

        # 日期只存在于候选阶段（仅 4 家引擎响应带 date，抓取层不产出），
        # 拿到候选后立即固化 url→日期原文，供最终 sources 清单回填。
        url_dates: Dict[str, str] = {}

        def _lap(stage: str) -> None:
            nonlocal _t
            now = time.perf_counter()
            self.logger.info("Pipeline stage %s: %.2fs", stage, now - _t)
            _t = now

        # 1. 多源搜索（唯一入口路径；引擎级失败在 _call_search_core 内消化，
        #    级联异常由 execute 的兜底 except 捕获）。
        #    ⚠️ 显式传 limit：不让 provider 请求量回落到 max_urls_to_try，
        #    避免两个值将来漂移时重现隐性压量。
        search_data = await self._call_search_core(
            query,
            limit=self.config.text_sources_per_query,
        )

        primary_text = self._extract_primary_text(search_data)
        urls = self._extract_urls(search_data)
        # 用原始 url 字符串做键：抓取层与去重全程不改写 url，最终 items 可直接命中。
        url_dates = {
            r["url"]: r["date"]
            for r in (search_data.get("results") or [])
            if r.get("url") and r.get("date")
        }
        # 候选池深度（纯可观测性）：区分"池子浅"与"真没料"。
        self.logger.info(
            "Search candidates: %d (CLI 聚合去重后、排序前)",
            len(search_data.get("results") or []),
        )
        # 同 host 限条重排：聚合站不许多占高分名额（只重排不丢弃），保独立信源数。
        if self.config.same_host_cap > 0:
            urls = self._interleave_by_host(urls, self.config.same_host_cap)

        # 跨轮去重 L1（见 ledger.py）：已交付过的来源一律剔，抓取窗口自然后滑，不回填。
        urls, skipped_repeats = ledger.filter_candidates(urls)
        if skipped_repeats:
            result["metadata"]["skipped_repeats"] = skipped_repeats
            self.logger.info(
                "Skipped %d source(s) already delivered earlier: %s",
                len(skipped_repeats),
                ", ".join(f"{d['url']} (round {d['round']})" for d in skipped_repeats[:3]),
            )
        result["metadata"]["search_urls"] = urls

        # 低商业价值页沉底（V10.1）：B2B 店铺/采购公告不占抓取名额 —— 稳定
        # 分区只重排不丢弃，池子比 fetch 窗口浅时它们照样被抓（表与分档见
        # filters_keywords.LOW_VALUE_*）。
        urls, demoted_low = filters.partition_low_value(urls)
        if demoted_low:
            result["metadata"]["low_value_demoted"] = len(demoted_low)
            self.logger.info(
                "Demoted %d low-value URL(s) to fetch-window tail: %s",
                len(demoted_low),
                ", ".join(demoted_low[:3]),
            )

        _lap("search")

        # 2/3. 全量抓取（固定条数 + 单档过滤）：抓满 fetch_url_count 条且在途全部
        # 落地才返回 —— 慢通道（L1/L2）才不会被"收满即停"cancel 掉，降级链才有效。
        fetch_count = self.config.fetch_url_count
        self.logger.info(f"Fetching {fetch_count} HTML URLs via Scrapling.")
        try:
            fetched = await self.scrapling.fetch_all(
                urls,
                fetch_count,
                budget=self.config.scrape_budget or None,
                interactions=interactions or None,
                query=query,
            )
        except Exception as e:
            self.logger.warning(f"Scrapling fetch_all failed: {e}")
            fetched = []

        _lap("fetch")

        # 3.4.0 PDF 支链留档：这些条目不在 fetched 里（L0 判魔数即落盘出链）。
        # ⚠️ 清单必须透传 metadata.pdf_sources，否则文件躺在 temp/pdf 无人知晓。
        # V10.3：条目自带 digest（server 端抽取的限额摘要），由 _render_pdf_
        # digest_section 内联进响应——消费纪律不再依赖 SKILL.md 被加载。
        pdf_sources = list(getattr(self.scrapling, "pdf_sources", None) or [])
        if pdf_sources:
            result["metadata"]["pdf_sources"] = pdf_sources
            pdf_new = sum(1 for p in pdf_sources if not p.get("reused"))
            result["metadata"]["pdf_new"] = pdf_new
            result["metadata"]["pdf_reused"] = len(pdf_sources) - pdf_new
            section = self._render_pdf_digest_section(pdf_sources)
            if section:
                result["metadata"]["_pdf_digest_section"] = section
            self.logger.info(
                "PDF dumps this run: %d (new=%d reused=%d) (%s)",
                len(pdf_sources), pdf_new, len(pdf_sources) - pdf_new,
                ", ".join(f"{p.get('title') or p.get('url')}" for p in pdf_sources[:3]),
            )

        # 3.4 抓取结果分流：主链路只消费过闸条目（低分回捞已删，淘汰池 95% 是结构性垃圾）。
        main_fetched: List[Dict] = list(fetched or [])

        # 3.4.5 首页/根域名标题一致性兜底：短路径 URL 的页面 <title> 与候选标题
        # 明显不一致 ⇒ 疑似首页/跳转页，直接移出主链路（压分不够，去重仍可能回填）。
        if self.config.homepage_guard_enabled and main_fetched:
            # 候选标题表来自搜索阶段；try 兜底：候选记录畸形时守卫整体放行。
            try:
                cand_titles = {
                    r.get("url"): r.get("title")
                    for r in (search_data.get("results") or [])
                    if r.get("url")
                }
            except Exception:
                cand_titles = {}
            kept_fetched: List[Dict] = []
            for d in main_fetched:
                try:
                    if filters.homepage_title_mismatch(
                        d.get("url") or "",
                        d.get("page_title") or "",
                        cand_titles.get(d.get("url")) or "",
                        query=query,
                        max_path_depth=self.config.homepage_guard_path_depth,
                        min_overlap=self.config.homepage_guard_min_title_overlap,
                        min_title_len=self.config.homepage_guard_min_title_len,
                    ):
                        self.logger.warning(
                            "Homepage/root-page guard removed: %s | page_title=%r",
                            d.get("url"), (d.get("page_title") or "")[:60],
                        )
                        continue  # 疑似首页/跳转页：不进主链路，也不进回捞池
                    kept_fetched.append(d)
                except Exception as e:
                    self.logger.warning("homepage guard failed for %s: %s", d.get("url"), e)
                    kept_fetched.append(d)
            main_fetched = kept_fetched

        # 3.4.6 query 相关性闸：content_score 判"像不像正文"，本闸判"讲不讲这件事"
        # （最长连续公共片段 < 门槛即移出主链路，判据见 filters.query_overlap）。
        if self.config.relevance_guard_enabled and main_fetched:
            kept_relevant: List[Dict] = []
            for d in main_fetched:
                try:
                    ov = filters.query_overlap(
                        d.get("text") or "", query, d.get("page_title") or "",
                    )
                except Exception as e:
                    # 判定失败一律放行：这只是道减法闸，不该让整条链路因此丢内容
                    self.logger.warning("Relevance guard failed for %s: %s", d.get("url"), e)
                    kept_relevant.append(d)
                    continue
                if ov is not None and ov < self.config.relevance_guard_min_overlap:
                    self.logger.warning(
                        "Off-topic removed (shared %d chars with query): %s | page_title=%r",
                        ov, d.get("url"), (d.get("page_title") or "")[:60],
                    )
                    continue
                kept_relevant.append(d)
            main_fetched = kept_relevant

        _lap("split")

        # 3.5 三维合成排序（V10.4）：content_score 照旧管形态（闸门不变）；
        # 权威度（URL 静态表）与 query 覆盖率（批内 idf）是独立维度，合成
        # rank_score 决定交付顺序。覆盖率必须在此处批量算——它要看到同批
        # 全部正文才能算文档频率。
        if self.config.authority_enabled:
            for d in main_fetched:
                d["authority"] = filters.authority_score(d.get("url") or "")
        if self.config.coverage_enabled:
            covs = filters.query_coverage(
                [d.get("text") or "" for d in main_fetched], query)
            for d, c in zip(main_fetched, covs):
                d["coverage"] = c
        items = filters.rank_items(
            main_fetched,
            self.config.rank_w_form,
            self.config.rank_w_authority,
            self.config.rank_w_coverage,
        )
        _lap("rank")  # 标签是 rank：去重在其后才发生

        # 3.6 自适应降权学习：成功抓到的条目累积观察；同域一轮只采一次。
        # 失败/低质的负样本已在 web_fetch 层记录。
        seen_host: set = set()
        for d in main_fetched:
            host = helpers.host_of(d.get("url") or "")
            if not host or host in seen_host:
                continue
            seen_host.add(host)
            try:
                filters_learn.penalty_delta(
                    d.get("url") or "",
                    fetch_ok=True,
                    score=d.get("score"),
                    template_hit=bool(d.get("template_hit")),
                    garbled_hit=bool(d.get("garbled_hit")),
                    latency=d.get("latency"),
                )
            except Exception as e:
                self.logger.warning("penalty_delta failed for %s: %s", host, e)

        _lap("learn")

        # 跨轮去重 L3+L4（见 ledger.py）：与前面几轮交付过的正文比包含度或
        # simhash 汉明距离，净跨站转载——L1 与 CLI 元数据去重都覆盖不到的一类。
        # 阈值与同批去重同一套。
        cross_before = len(items)
        if cross_before:
            items, cross_dropped = ledger.filter_bodies(
                items,
                self.config.semantic_dedup_max_chars,
                self.config.semantic_dedup_ngram_threshold,
                sh_threshold=(self.config.simhash_hamming_threshold
                              if self.config.simhash_enabled else 0),
            )
            if cross_dropped:
                result["metadata"]["cross_round_dropped"] = cross_dropped
                self.logger.info(
                    "Cross-round dropped: %d/%d (%s)", len(cross_dropped), cross_before,
                    ", ".join(f"{d['url']}≈{d['dup_of']} sim={d['sim']}"
                              for d in cross_dropped[:3]),
                )
            _lap("cross_round")

        # 3.6 内容相似去重 + 取前 N：唯一的去重闸（3-gram 包含度聚类，
        # 同簇留正文最长；旧 TF-IDF/补位机制已删）。
        items, sem_stat = self._dedupe_similar(items, max_urls)
        result["metadata"]["semantic_dedup"] = sem_stat

        # 记账（见 ledger.record）：只记交付出去的条目 —— 被淘汰的可能是本轮排名
        # 靠后而非没价值，记进来就没有回归可能。
        if items or result["metadata"].get("pdf_sources"):
            ledger.record(
                items,
                [p.get("url") for p in (result["metadata"].get("pdf_sources") or [])],
            )

        _lap("dedup")

        if items:
            # items 已按 score 降序，正文顺序即质量顺序。
            combined = "\n\n---\n\n".join(d["text"] for d in items)

            # 未截断全文进元数据：归档必须用它（answer 可能被截断）。
            result["metadata"]["_full_combined"] = combined

            # 输出控制：60000 字符上限（PDF 全文类实测可达 325K，不截会撑爆宿主）。
            orig_len = len(combined)
            cut_urls: Set[str] = set()
            if self.config.max_total_chars > 0 and orig_len > self.config.max_total_chars:
                # truncate_by_score 一次遍历同时交出正文与截断来源集合，两份不会漂移。
                truncated_text, cut_urls = filters.truncate_by_score(
                    items, self.config.max_total_chars
                )
                combined = (
                    truncated_text
                    + f"\n\n---\n[内容已截断：原始 {orig_len} 字符，已按正文质量分降序截取前 "
                    f"{self.config.max_total_chars} 字符。完整全文见本次返回的 HTML 留档"
                    f"路径；也可把 query 收得更细来缩小范围。]"
                )
                result["metadata"]["truncated"] = True
                result["metadata"]["original_length"] = orig_len
                self.logger.info(
                    "Output truncated: %d -> %d chars",
                    orig_len, self.config.max_total_chars,
                )

            # per-source 清单：编号与正文小节一一对应，truncated 逐条透出。
            result["metadata"]["sources"] = self._build_sources_meta(
                items, cut_urls, url_dates,
            )

            result["success"] = True
            result["answer"] = combined
            result["source"] = "scrapling_filtered"
            result["metadata"]["fetched_count"] = len(items)
            _lap("compose")
            return result

        # 4. 兜底：抓取没拿到但有搜索摘要 → 降级返回摘要。
        if primary_text:
            self.logger.info("Falling back to search snippet content (Scrapling returned nothing).")
            result["success"] = True
            result["answer"] = primary_text
            result["source"] = "exa_fallback"
            result["metadata"]["fetched_count"] = 0
            result["metadata"]["_full_combined"] = primary_text
            return result

        # 5. 彻底失败：搜索没内容、抓取没内容，返回空结果由上层标记错误
        result["metadata"]["warning"] = "No content retrieved from any source."
        return result

    # ---------- 内容相似去重 ----------
    def _dedupe_similar(
        self, items: List[Dict], target: int,
    ) -> Tuple[List[Dict], Dict[str, Any]]:
        """内容相似去重 → 取前 target 条（全仓唯一去重闸）。

        三步：正文前缀切字符 3-gram → 包含度（|A∩B|/min(|A|,|B|)）> 阈值者经
        并查集聚类 → 同簇留正文最长，幸存者按 score 降序取前 target 条。
        用包含度不用 Jaccard/覆盖率：要抓的正是"同源转载各加导语"——共享前缀
        全同、尾巴各不同，包含度看共享侧，覆盖率被尾巴稀释。
        返回 `(kept, stat)`；任何异常一律 fail-open：原样取前 target 条。
        """
        t0 = time.perf_counter()
        n_items = len(items)
        stat: Dict[str, Any] = {
            "before": n_items, "after": n_items, "dropped": 0,
            "merged": 0, "simhash_merged": 0, "elapsed": 0.0,
        }

        def _fail_open() -> List[Dict]:
            return items[:target] if target > 0 else list(items)

        if not self.config.semantic_dedup_enabled or n_items <= 1:
            return _fail_open(), stat
        limit = self.config.semantic_dedup_max_chars
        # 正文取样：剔掉出处头「## 来源: <url>」—— 元数据参与相似度会让同域名
        # 无关文章虚高（strip_source_header 全仓共用一份）。
        texts = [filters.strip_source_header(d.get("text") or "")[:limit] for d in items]
        usable = [bool(t) for t in texts]
        if not any(usable):
            return _fail_open(), stat
        try:
            # 相似度矩阵：3-gram 包含度（V10.3 起多窗口取最大——窗口 0 与旧
            # 单窗口完全同口径，严格覆盖；后 3 个窗口净"截掉原文开头的镜像"）。
            # 判据在 ledger —— 跨轮去重（L3/L5）用同一份。纯标准库，数十条目
            # 全对比较仍在百毫秒级。
            texts_full = [filters.strip_source_header(d.get("text") or "") for d in items]
            gs = [ledger.grams_slices(texts_full[i]) if usable[i] else [set()]
                  for i in range(n_items)]
            sim: List[List[float]] = [[0.0] * n_items for _ in range(n_items)]
            for i in range(n_items):
                if not gs[i] or gs[i] == [set()]:
                    continue
                for j in range(i + 1, n_items):
                    if gs[j] != [set()]:
                        sim[i][j] = sim[j][i] = ledger.containment_multi(gs[i], gs[j])
            # simhash 指纹（V10.3）：全段词汇分布，兜"整篇复用（含重排/换字）"
            # ——前缀改写类由多窗口包含度管，重排类 gram 全盲、simhash 才看得见。
            # 与包含度 OR 组合，不单独判（独立文章词汇重叠高，单用易误杀）。
            sh_thr = (self.config.simhash_hamming_threshold
                      if self.config.simhash_enabled else 0)
            shs = ([ledger.simhash64(filters.strip_source_header(d.get("text") or ""))
                    if usable[i] else 0 for i, d in enumerate(items)]
                   if sh_thr else [0] * n_items)
        except Exception as e:
            self.logger.warning("Similar dedup unavailable, fail-open: %s", e)
            stat["error"] = str(e)[:200]
            stat["elapsed"] = round(time.perf_counter() - t0, 2)
            return _fail_open(), stat

        threshold = self.config.semantic_dedup_ngram_threshold
        n = n_items
        # 并查集聚类（不用逐条贪心：链式相似 A~B~C 会漏杀中间的 B）。
        parent = list(range(n))

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            if not usable[i]:
                continue
            for j in range(i + 1, n):
                if not usable[j]:
                    continue
                gram_hit = sim[i][j] > threshold
                sh_hit = bool(sh_thr and shs[i] and shs[j]
                              and ledger.hamming(shs[i], shs[j]) <= sh_thr)
                if not (gram_hit or sh_hit):
                    continue
                ri, rj = _find(i), _find(j)
                if ri != rj:
                    parent[rj] = ri
                    if sh_hit and not gram_hit:
                        stat["simhash_merged"] += 1

        groups: Dict[int, List[int]] = {}
        for i in range(n):
            if usable[i]:
                groups.setdefault(_find(i), []).append(i)

        def _longest(members: List[int]) -> int:
            """簇代表 = 正文最长者；同长取 index 小者（items 已按 score 降序）。"""
            return max(members, key=lambda i: (len(items[i].get("text") or ""), -i))

        survivors: List[int] = [_longest(m) for m in groups.values()]
        grouped = {i for m in groups.values() for i in m}
        # 取样为空（不可用）的条目各自成单条：不参与聚类，也不被丢
        survivors += [i for i in range(n) if i not in grouped]

        # 取前 target：按合成 rank_score 降序（与交付/截断同一排序键；不足则全取）
        survivors.sort(key=lambda i: (-float(items[i].get("rank_score")
                                              or items[i].get("score") or 0.0), i))
        kept_idx = survivors[:target] if target > 0 else survivors
        kept = [items[i] for i in kept_idx]

        stat["after"] = len(kept)
        stat["dropped"] = n_items - len(kept)          # 合并 + 截断
        stat["merged"] = n_items - len(survivors)      # 只算簇合并
        stat["elapsed"] = round(time.perf_counter() - t0, 2)
        self.logger.info(
            "Similar dedup: %d -> %d (dropped=%d merged=%d simhash=%d thr=%.2f %.2fs)",
            n_items, len(kept), stat["dropped"], stat["merged"],
            stat["simhash_merged"], threshold, stat["elapsed"],
        )
        return kept, stat

    # ---------- PDF 附件摘要（响应内联，V10.3） ----------
    def _render_pdf_digest_section(self, pdfs: List[Dict]) -> str:
        """pdf_sources → 限额摘要文本，追加在响应正文之后。

        每份摘要取条目自带的 digest（_dump_pdf 时抽好）；总额度
        pdf_digest_max_total 字符，超了按清单顺序截断并标注——归档里每份 PDF
        仍带完整摘要，需要时读归档。
        """
        cap = self.config.pdf_digest_max_total
        if cap <= 0:
            return ""
        reused_n = sum(1 for p in pdfs if p.get("reused"))
        lines = [
            f"## PDF 附件摘要（{len(pdfs)} 份落盘：新增 {len(pdfs) - reused_n}"
            f"、复用缓存 {reused_n}；摘要总额度 {cap} 字）",
            "全文路径与原始 URL 见归档「PDF 附件」节；⚠️ 摘要只是局部，引用结论前"
            "必要时按 path 读原文核对。",
        ]
        used = 0
        for p in pdfs:
            title = str(p.get("title") or "") or "(无标题)"
            tag = "（本轮复用缓存，未重新下载）" if p.get("reused") else ""
            d = (p.get("digest") or "").strip()
            if used >= cap:
                lines.append(f"\n**{title}**{tag}\n（摘要总额度已用完，未摘取；全文见归档）")
                continue
            if not d:
                lines.append(f"\n**{title}**{tag}\n（无可抽取文本，可能是扫描件）")
                continue
            room = cap - used
            if len(d) > room:
                d = d[:room] + "……[摘要因总额度截断]"
            used += len(d)
            lines.append(f"\n**{title}**{tag}\n{d}")
        return "\n".join(lines)

    async def _call_search_core(self, query: str,
                                limit: Optional[int] = None) -> Dict:
        """调用 search_engine 统一入口 search.py 进行多源搜索。

        search.py = Go CLI（多引擎并发聚合 + URL 去重 + 可达性预检）
                  + ranking_meta/rank.py 统计学习排序。
        limit：本查询每引擎最多返回的来源数；引擎集合与密钥都由 CLI 自己决定，
        本层既不传 key、也不指定引擎。返回全量候选（不硬截断，低质候选由抓取层消化）。
        """
        # ⚠️ search.py import 时有进程级副作用：sys.stdout.reconfigure()
        # （Windows 控制台编码修复），有意保留不包裹。
        _se_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_engine")
        if _se_dir not in sys.path:
            sys.path.insert(0, _se_dir)
        from search import search as ranked_search

        per_query = limit or self.config.max_urls_to_try

        # ranked_search 是同步函数（内部 subprocess），必须丢线程执行，否则阻塞事件循环。
        resp = await asyncio.to_thread(
            ranked_search,
            query,
            limit=per_query,
        )
        # 全量候选，不硬截断：低质淘汰由抓取层按 content_score 消化。
        results = resp or []
        return {"results": results}


    # ---------- 辅助方法 ----------
    def _extract_primary_text(self, search_data: Dict) -> str:
        results = search_data.get("results", [])
        if not results:
            return ""
        texts = []
        for r in results[:5]:  # 前5条合并，提高初始内容量
            t = r.get("text") or r.get("content") or r.get("snippet") or ""
            if not t:
                continue
            # 逐来源清理尾部 boilerplate（与抓取链路一致）
            try:
                t = self.scrapling._cleanup_footer(t)
            except Exception:
                pass
            if t:
                texts.append(t)
        return "\n\n---\n\n".join(texts) if texts else ""

    def _extract_urls(self, search_data: Dict) -> List[str]:
        urls = []
        for r in search_data.get("results", []):
            if "url" in r:
                urls.append(r["url"])
        return urls

    @staticmethod
    def _build_sources_meta(
        items: List[Dict[str, Any]],
        cut_urls: Optional[Set[str]] = None,
        url_dates: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """整理逐条来源的判据清单（编号与正文 `## 来源:` 小节顺序一一对应）。

        字段：n 序号 / url / score 质量分（0~1）/ truncated 是否完整纳入 /
        date 发布日期（ISO）。
        ⚠️ date 只对 serpapi/metaso/exa/qianfan 可得，空串=该来源未提供，
        调用方不得据此推断发布日期；涉及时效的结论必须说明日期不可得。
        """
        cut = cut_urls or set()
        dates = url_dates or {}
        out: List[Dict[str, Any]] = []
        for i, d in enumerate(items, start=1):
            out.append({
                "n": i,
                "url": d.get("url") or "",
                "score": round(float(d.get("score", 0.0)), 3),
                # V10.4：权威度与覆盖率并列透出——调用方据此判断"谁说的可信"
                # 与"讲了几成"，不再只看形态分。
                "authority": round(float(d.get("authority", 0.0)), 2),
                "coverage": round(float(d.get("coverage", 0.0)), 2),
                "truncated": (d.get("url") or "") in cut,
                # 日期原文来自候选阶段（aggregate 只做透传，不解析），此处归一成 ISO
                "date": helpers.normalize_date(dates.get(d.get("url") or "") or ""),
            })
        return out

    @staticmethod
    def _interleave_by_host(urls: List[str], cap: int) -> List[str]:
        """同 host 限条重排：每个域名最多 `cap` 条留在前段，超额的顺延到队尾。

        只重排、不丢弃 —— 前段凑不满抓取目标时抓取层会继续向后取，总量不减。
        动机：聚合站（toutiao/sina/baijiahao）常占满高分名额，独立 host 数被压低
        → 语义去重收敛不出足够独立簇。用少量分数换独立信源数。
        """
        head: List[str] = []
        tail: List[str] = []
        seen: Dict[str, int] = {}
        for u in urls:
            host = helpers.host_of(u)
            if not host:
                # 解析不出的 URL 不参与限条（避免它们互相挤占）。
                head.append(u)
                continue
            seen[host] = seen.get(host, 0) + 1
            (head if seen[host] <= cap else tail).append(u)
        return head + tail

    def _save_archive(self, query: str, result: Dict) -> Optional[str]:
        """把完整检索结果落盘成一份 markdown 归档。

        落盘位置：本次搜索任务（= 一个 MCP 进程，与账本同生命周期）一个时间戳
        文件夹，文件以关键词命名，序号是轮次：

            temp/md/2026年 09月23日 22：09/01_食品微生物检验 采样方案.md

        为什么是 markdown：归档正文本来就来自 trafilatura 的 markdown 输出，
        原样落盘（旧的 markdown→HTML 转换层已删：要重新解析一遍、格式易脱节、
        多付约 8% token）。结构（与返回正文同源，只多两段归档独有内容）：

            # {query}
            来源: … | 耗时: … | 抓取: N 条 | 引用: N 条
            ## PDF 附件（N 份）      ← 未进正文池的 PDF 清单
            **[1] example.com**      ← 来源标记（编号按首次出现顺序，URL 去重）
            ## 来源: https://example.com/a
            {正文}

        ⚠️ 正文经 helpers.reflow_paragraphs 重组段落：只动空白、一个字不改
        （tests/test_archive.py 锁死不变量）。
        """
        answer = result.get("answer", "")
        if not answer:
            return None
        meta = result.get("metadata", {})
        # 必须用未截断全文：answer 可能已被 max_total_chars 截断，只写 answer 会丢尾部来源
        full_text = meta.get("_full_combined") or answer

        global _ARCHIVE_SESSION_DIR, _ARCHIVE_SEQ
        if _ARCHIVE_SESSION_DIR is None:
            _ARCHIVE_SESSION_DIR = os.path.join(ARCHIVE_DIR, _archive_stamp())
            os.makedirs(_ARCHIVE_SESSION_DIR, exist_ok=True)
            # 时间戳只到分钟：同分钟重启落同一文件夹，序号必须续号防静默覆盖。
            _ARCHIVE_SEQ = _archive_resume_seq(_ARCHIVE_SESSION_DIR)
        os.makedirs(_ARCHIVE_SESSION_DIR, exist_ok=True)
        _ARCHIVE_SEQ += 1
        path = os.path.join(
            _ARCHIVE_SESSION_DIR, f"{_ARCHIVE_SEQ:02d}_{_archive_filename(query)}.md"
        )

        # ---------- 正文 + 逐节来源标记（编号按首次出现顺序，URL 去重） ----------
        cap = self.config.archive_max_sources
        kept_urls: List[str] = []
        body_lines: List[str] = []
        stopped = False
        for line in full_text.split("\n"):
            m = re.match(r'^##\s*来源:\s*(\S+)\s*$', line.strip())
            if m:
                url = m.group(1)
                if url not in kept_urls:
                    if 0 < cap <= len(kept_urls):
                        # 已落盘 cap 个来源：本节及其后全部丢弃（截断单位 = 整节）
                        stopped = True
                    if not stopped:
                        kept_urls.append(url)
                        body_lines.append(f"**[{len(kept_urls)}] {helpers.host_of(url)}**")
                        body_lines.append("")
                if stopped:
                    continue
            if stopped:
                continue
            body_lines.append(line)

        # ---------- PDF 附件区（正文之前：条目不在正文里，读者按需打开文件） ----------
        pdf_lines: List[str] = []
        pdf_list = meta.get("pdf_sources") or []
        if pdf_list:
            pdf_lines.append(f"## PDF 附件（{len(pdf_list)} 份）")
            pdf_lines.append("")
            for p in pdf_list:
                title = str(p.get("title") or "") or "(无标题)"
                n_bytes = int(p.get("bytes") or 0)
                size_s = (
                    f"{n_bytes / 1048576:.1f} MB" if n_bytes >= 1048576
                    else f"{n_bytes / 1024:.0f} KB"
                )
                n_pages = int(p.get("pages") or 0)
                pages_s = f" · {n_pages} 页" if n_pages else ""
                reuse_s = " · 本轮复用缓存" if p.get("reused") else ""
                pdf_lines.append(f"- **{title}**")
                pdf_lines.append(f"  - {size_s}{pages_s}{reuse_s} · 未纳入正文")
                pdf_lines.append(f"  - 本地路径: `{p.get('path') or ''}`")
                pdf_lines.append(f"  - 原始地址: {p.get('url') or ''}")
                # 关键片段摘要（V10.3）：归档不吃响应额度，逐份给全。
                digest = (p.get("digest") or "").strip()
                if digest:
                    pdf_lines.append("  - 关键片段（server 端抽取，局部内容，引用前建议读原文核对）:")
                    for seg in digest.split("\n"):
                        seg = seg.strip()
                        if seg:
                            pdf_lines.append(f"    > {seg}")
            pdf_lines.append("")

        # 兜底：搜索摘要降级路径没有来源头，退回列出候选 URL。
        fallback_lines: List[str] = []
        if not kept_urls and meta.get("search_urls"):
            fallback_lines.append("## 候选来源（本次未抓到正文）")
            fallback_lines.append("")
            for u in meta["search_urls"]:
                fallback_lines.append(f"- {u}")
            fallback_lines.append("")

        # ⚠️ source 读顶层 result["source"]（metadata 里从来没有这个键）。
        meta_line = (
            f"来源: {result.get('source', '')} | "
            f"耗时: {float(meta.get('elapsed') or 0):.2f}s | "
            f"抓取: {int(meta.get('fetched_count') or 0)} 条 | "
            f"引用: {len(kept_urls)} 条"
        )

        doc = "\n".join(
            [f"# {query}", "", meta_line, ""]
            + pdf_lines
            + fallback_lines
            + ["---", ""]
            + body_lines
        ).rstrip() + "\n"
        # 截断点常落在小节分隔线上：去掉归档末尾孤立的分割线。
        doc = re.sub(r"\n+-{3,}\s*$", "\n", doc)
        # 段落重组（只动空白）放最后：先完成结构处理，避免折行干扰来源头识别。
        doc = helpers.reflow_paragraphs(doc)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(doc)
            self.logger.info("Archive saved: %s", path)
            return path
        except Exception as e:
            self.logger.error("Archive export failed: %s", e)
            return None
