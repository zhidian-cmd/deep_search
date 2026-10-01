"""网页抓取 + Trafilatura 密度提取（直导模式）。

降级链（成本递增，Ln 是下标、**第 n+1 级**是序号，两套说法别混）：
  L0 httpx 直抓（第 1 级，轻量静态 HTTP：连不通/非 200/无正文 → 升级；连接层失败按域名缓存）
  L1 AsyncFetcher.get（第 2 级，静态 HTTP，curl_cffi 指纹模拟，对付轻量反爬）
  L2 StealthyFetcher（第 3 级，隐身浏览器渲染 + 滚动 + Cookie）：不花钱，兜底，
     且只有它能处理需要滚动/等待/交互的页面

PDF 支链（L0 内）：判魔数 → 原文件落盘 temp/pdf → 记 pdf_sources 清单 → 出链。
不解析正文、不进正文池（真 PDF 三级链都读不出正文，解析文本过 content_score 时
噪声与正文同分，且一条 29 万字会挤光整批 max_total_chars）。

L2 跳过判据：硬阻断 / 二进制解码失败不升级（见 _skip_l2_reason）。
超时单位注意：AsyncFetcher.get 传**秒**；StealthyFetcher.async_fetch 传**毫秒**。

⚠️ 本模块封装第三方 `scrapling` 库，故名 `web_fetch` 避免命名空间冲突 —— 否则
`from scrapling.fetchers import ...` 会把本模块自身当作 `scrapling` 模块加载并抛
`'scrapling' is not a package`。
"""
import asyncio
import hashlib
import html as _html_lib
import logging
import os
import re
import time
from collections import deque
from typing import Dict, List, Optional
from urllib.parse import urlparse, unquote

# 三个抓取/解析依赖都是 requirements.txt 里的**硬依赖**，不做"没装就降级"——
# 真没装就在 import 处响亮地报错。
import httpx
import trafilatura

from deep_search.config import PDF_DIR, DeepSearchConfig
from deep_search import helpers
from deep_search import filters
from deep_search import filters_learn

# L0 httpx 直抓超时（秒）：非 WAF 普通站 2s 内出响应，慢站也不误杀，
# 仍远小于 L1/L2 各自的 scrapling_timeout，保证升级链不被拖慢。
_HTTPX_TIMEOUT = 12.0

# httpx 请求头：带浏览器 UA 降低被拒概率（httpx 无 TLS 指纹模拟，WAF 站会在此
# 被 403 —— 这正是设计行为，升级到 AsyncFetcher 处理）。
_HTTPX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------- 降级链"跳过 L2"判据 ----------
# L2 隐身浏览器唯一独有的能力是「执行 JS」。只有"JS 可能救得回来"的失败才值得
# 付浏览器成本；下面三类注定救不了，不进 L2（判据全部来自实测）：
#   · 404/410：资源本身没了，任一级出现即可判死；
#   · 403/451：必须 L0 与 L1 双命中 —— L0 是 httpx（无 TLS 指纹）被 WAF 403 属常态，
#     单看它等于把整类站判死；L1 指纹模拟仍 403 的，浏览器层再试概率极低；
#   · 二进制解码失败 + URL 含 "pdf"：浏览器渲染 PDF 阅读器同样拿不到正文，
#     全历史 .pdf 无一成功。必须带 URL 限定：GBK 页误标 charset 会抛同样的错，
#     而那种情况浏览器恰恰能救（自己嗅探 charset）。
_GONE_STATUSES = frozenset({404, 410})
_HARD_BLOCK_STATUSES = frozenset({403, 451})
_PDF_MAGIC = b"%PDF-"
# PDF 支链的"已终结"哨兵：_fetch_chain 用它告诉 _worker —— 本条是 PDF，文件已
# 落盘、清单已记。⚠️ 不能用 None 代替：None 会走失败分支记一次 fetch_ok=False，
# 把该域名的自适应降权学习带偏（PDF 落盘恰恰是成功）。
_PDF_DONE = object()


# PDF 元数据里"有名无实"的标题：非空但没有区分度，命中就当没标题，走首页文本兜底。
# 实测案例：标准原件的元数据 title 就是「行业标准」四个字。
_GENERIC_PDF_TITLES = frozenset({
    "行业标准", "国家标准", "地方标准", "企业标准", "团体标准", "国际标准",
    "untitled", "unknown", "pdf", "doc", "文档", "新建", "幻灯片", "课件",
})


def _generic_pdf_title(t: str) -> bool:
    """元数据标题是否"有名无实"：过短（<4 字符）、命中泛词表、或 Office 导出前缀。"""
    if not t or len(t) < 4:
        return True
    low = t.lower()
    if low in _GENERIC_PDF_TITLES:
        return True
    return low.startswith(("microsoft word", "microsoft powerpoint", "microsoft excel"))


def _first_title_lines(text: str, cap: int = 120) -> str:
    """首页文本 → 标题候选：逐行挑，跳过封面编号/帽头行与过短行。

    标准件封面开头常是「ICS 11.020 / CCS C 50 / 中华人民共和国卫生行业标准」
    这类无信息行，逐行滤掉后拼出来的才是能认出的名字。
    """
    out: List[str] = []
    total = 0
    for line in (text or "").split("\n"):
        s = " ".join(line.split())
        if len(s) < 4 or re.match(r"^(ICS|CCS)\b", s, re.I) \
                or re.match(r"^\d+(\.\d+)*$", s) or s.startswith("中华人民共和国"):
            continue
        out.append(s)
        total += len(s) + 1
        if total >= cap:
            break
    return " ".join(out)[:cap]


def _is_pdf_payload(data: bytes) -> bool:
    """魔数判据（唯一可信）：content-type 常被站点写错，URL 后缀会被查询串带偏。"""
    return bool(data) and data[:1024].find(_PDF_MAGIC) >= 0


def _skip_l2_reason(l0_status, l1_status, l1_error: str, url: str) -> str:
    """该不该跳过 L2：返回非空 = 理由（JS 救不了），空串 = 照常升级。"""
    if l0_status in _GONE_STATUSES or l1_status in _GONE_STATUSES:
        return "404/410 gone"
    if l0_status in _HARD_BLOCK_STATUSES and l1_status in _HARD_BLOCK_STATUSES:
        return "403/451 blocked at both L0 and L1"
    if "codec can't decode" in (l1_error or "") and "pdf" in url.lower():
        return "binary decode failure on a PDF"
    return ""


# ========== 引用角标清理 ==========
# Trafilatura 原样保留 <sup>[18]</sup> 这类上标引用，学术/期刊页残留可达数百个，
# 密度提取后统一正则删除。
_RE_SUP_CITE = re.compile(
    r"<sup\b[^>]*>\s*(?:\[[^\]]*\]\s*)+\s*</sup>",
    re.I,
)
# 裸引用角标 [18] / [9-10]；(?!\() 保护 markdown 链接 [text](url) 不被误删
_RE_BARE_CITE = re.compile(r"\[\d+(?:\s*[-–—,]\s*\d+)*\](?!\()")

# 页面 <title>：抓取链顺带提取，供 skill 层做「页面标题 ↔ 候选标题」一致性检查。
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _page_title(html: str) -> str:
    """从 HTML 中提取 <title> 文本（解实体、去标签、压缩空白）。失败返回空串。"""
    if not html:
        return ""
    m = _RE_TITLE.search(html)
    if not m:
        return ""
    t = re.sub(r"<[^>]+>", "", m.group(1))
    try:
        t = _html_lib.unescape(t)
    except Exception:
        pass
    return re.sub(r"\s+", " ", t).strip()[:200]

# 抓取并发上限（滑动窗口槽位数）。
# ⚠️ 本值只管并发度，不决定交付条数 —— 交付条数是 config.max_urls_to_try，
#   抓取总量是 config.fetch_url_count，三者互不耦合。
# ⚠️ 抬高收益有限：总耗时 ≈ 最慢一条而非"最慢两批"（实测 fetch 阶段 35s 里
#   20 条在前 11s 完成，尾巴被 1~2 条极慢 URL 独占）；代价是 L2 瞬时并发
#   headless 实例更多，内存/CPU 峰值上升。
MAX_CONCURRENT_FETCHES = 15

# 取消后的"回收窗口"（秒）：cancel() 打不断卡在同步阻塞调用里的任务，此时
# gather 会无限等待把整条链拖到 120s 全局超时；3s 足够正常回收，打不断的放弃。
_CANCEL_GRACE = 3.0

# 被放弃的抓取任务：持引用直到它真正结束并消费迟到异常，避免 CPython 打出
# "Task was destroyed but it is pending!" 污染日志。刻意不设数量上限：
# _reap_task 必然销号，永久卡死的任务本来就由事件循环持着引用。
_ABANDONED_TASKS: set = set()


def _abandon_task(task) -> None:
    """登记一个已放弃的抓取任务：持引用等它结束，并在 done 回调里销号。"""
    _ABANDONED_TASKS.add(task)
    task.add_done_callback(_reap_task)


def _reap_task(task) -> None:
    _ABANDONED_TASKS.discard(task)
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:  # noqa: BLE001 - CancelledError 等一律吞掉
            pass


class ScraplingManager:
    def __init__(self, config: DeepSearchConfig, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger("deep_search")
        # 在飞的 httpx client 登记表：正常路径随用随销号；取消打不断的残号由
        # _fetch_direct 退出时统一兜底关闭。
        self._open_clients: set = set()
        # 整批共用一个隐身浏览器实例（标签池）：懒建 + 退出时统一关。
        self._stealthy_session = None
        self._stealthy_lock = asyncio.Lock()
        self._stealthy_broken = False   # 起不来就整批退回旧的逐条模式，不反复重试
        # PDF 支链留档清单：[{url, path, bytes, pages, title}]，由 L0 追加，
        # fetch_all 每次开跑清空。这些条目不进 fetch_all 的返回值。
        self.pdf_sources: List[Dict] = []
        try:
            from scrapling.fetchers import AsyncFetcher, StealthyFetcher, AsyncStealthySession
            self._async_fetcher = AsyncFetcher
            self._stealthy_fetcher = StealthyFetcher
            self._stealthy_session_cls = AsyncStealthySession
            self.logger.info("Scrapling direct import mode enabled (AsyncFetcher + StealthyFetcher)")
        except ImportError as e:
            self.logger.error("Scrapling import failed: %s", e)
            raise

    async def fetch_all(
        self,
        urls: List[str],
        fetch_count: int,
        budget: Optional[float] = None,
        interactions: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """固定条数并行抓取：返回 [{url, text, score}]，按 score 降序。

        - 抓取量固定 fetch_count 条（从 urls 队首按序取），且全部落地才返回 ——
          慢通道（L1/L2）的条目才有机会回来，降级链不被"收满即停"架空。
        - 过滤：score >= content_threshold 保留，否则丢弃。
        - budget：整批全局预算（秒），到点取消未完成条目；单条另有
          fetch_timeout_per_url 硬熔断，到点单独放弃不影响其余。
        - ⚠️ PDF 不在返回值里：那类条目走 L0 支链（落盘 + 记清单），
          调用方读 `self.pdf_sources` 取。
        """
        # 每轮一份干净的 PDF 清单：必须放在最前（早于任何 return），否则 urls
        # 为空时会把上一轮的清单留给调用方 —— 那是幽灵结果。
        self.pdf_sources = []
        if not urls:
            return []

        unique_urls = helpers.normalize_urls(urls)
        if not unique_urls:
            return []

        # 滚动/等待次数在这里一次解到底：没传 interactions 用 config 默认值，
        # 传了按指令覆盖。下游直接拿最终值，不再各自兜底。
        scroll_times = self.config.lazy_load_scroll_times
        wait_ms = self.config.lazy_load_wait_ms
        if interactions:
            for action in interactions:
                if action.get("type") == "scroll":
                    scroll_times = action.get("times", scroll_times)
                elif action.get("type") == "wait":
                    wait_ms = action.get("ms", wait_ms)

        return await self._fetch_direct(
            list(unique_urls), fetch_count, scroll_times, wait_ms, budget
        )

    async def _httpx_first(self, url: str, dead_hosts: Dict[str, bool]) -> tuple:
        """L0（第 1 级）：httpx 直抓，请求本身就是可达性探测。

        - 连接层失败（DNS/连接超时）：按域名缓存，同域后续 URL 直接跳过本级；
        - 响应体是 PDF（魔数判据）：落盘 + 记清单，不解析正文、不进降级链；
        - 200 且提取出正文：直接消费，省掉 L1 的重复抓取；
        - 200 但无正文（SPA/JS 渲染页）或 4xx/5xx（WAF/反爬）：升级。

        返回 `(text, page_title, status)`：status 是留给降级链的证据
        （_skip_l2_reason 据此判断 L2 还能不能救）。PDF 时 text 位放 `_PDF_DONE`
        哨兵：文件已落盘，`_fetch_chain` 不得再升级。
        """
        host = helpers.host_of(url)
        if not host or dead_hosts.get(host):
            return None, None, None
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=_HTTPX_TIMEOUT, headers=_HTTPX_HEADERS,
        )
        self._open_clients.add(client)
        try:
            resp = await client.get(url)
        except Exception as e:
            # 连接层失败 = 该域当前不可达，缓存后同域直接跳过本级。
            # 注意：HTTP 状态错误（403/500）不走这里 —— 那是"站活着但拒绝"，
            # 不能缓存成 dead，否则同域其余 URL 会被误跳过升级链。
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) or "connect" in str(e).lower():
                dead_hosts[host] = True
            self.logger.warning("httpx level-0 failed for %s: %s", url, e)
            return None, None, None
        finally:
            # ⚠️ 不能用 async with 代替：取消路径下 __aexit__ 的清理不保证跑完，
            # 连接池会悬空（见 _close_client）。放 finally 里显式关一次。
            await self._close_client(client)

        if resp.status_code != 200:
            self.logger.info(
                "httpx level-0 HTTP %d, escalating: %s", resp.status_code, url
            )
            return None, None, resp.status_code

        # PDF 三步支链：判魔数 → 落盘 → 记清单 → 出链，不解析正文。
        body = resp.content or b""
        if _is_pdf_payload(body):
            entry = self._dump_pdf(body, url)
            if entry:
                self.pdf_sources.append(entry)
                self.logger.info(
                    "PDF dumped: %s (%d bytes, title=%r) -> %s",
                    url, entry["bytes"], entry["title"], entry["path"],
                )
            else:
                # 落盘失败不升级：L1/L2 同样读不出正文，升级只是白占并发槽。
                self.logger.warning("PDF dump failed, dropped: %s", url)
            return _PDF_DONE, None, resp.status_code

        html_text = resp.text or ""
        if not html_text.strip():
            self.logger.info("httpx level-0 empty body, escalating: %s", url)
            return None, None, resp.status_code
        page_title = _page_title(html_text)
        text = self._extract_text(html_text, url)
        if text:
            self.logger.info("httpx fetch OK: %s (%d chars)", url, len(text))
            return text, page_title, resp.status_code
        self.logger.info("httpx level-0 no main content, escalating: %s", url)
        return None, None, resp.status_code

    def _pdf_meta(self, data: bytes, url: str) -> Dict:
        """读 PDF 的标题与页数（只为清单服务，不抽正文）。

        PDF 不进正文池的三条依据：无 DOM、页眉页脚周期重复，content_score 眼里
        不是"低质"是"字多"（29 万字仍拿 0.88）；截断只能字符硬切；一条 29 万字
        是整批 60000 额度的 5 倍，会把其余来源全挤出去。

        标题优先级：PDF 元数据 title（先过泛词关）→ 第一页文本开头 → URL 末段
        （国内标准类 PDF 元数据标题经常为空，必须抽第一页兜底）。
        懒导入 pymupdf：绝大多数 query 遇不到 PDF，不白付 import 成本。
        """
        title, pages = "", 0
        import pymupdf              # 正式包名（`fitz` 别名已弃用告警）
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                pages = doc.page_count
                raw = ((doc.metadata or {}).get("title") or "").strip()
                if not _generic_pdf_title(raw):
                    title = raw
                if not title and pages:
                    title = _first_title_lines(doc[0].get_text() or "")
        except Exception as e:
            self.logger.warning("PDF meta failed for %s: %s", url, e)
        if not title:
            # URL 末段兜底；端点式 URL 拿不到名字只能空着，页数/字节数仍给得出。
            title = unquote(urlparse(url).path.rsplit("/", 1)[-1] or "")
        return {"title": title.strip(), "pages": pages}

    def _dump_pdf(self, data: bytes, url: str) -> Optional[Dict]:
        """PDF 原文件落盘到 PDF_DIR，返回清单条目；任何失败返回 None。

        落盘是唯一能长期留住 PDF 内容的形态（原始 URL 会失效/挂付费墙）。
        文件名 = 解析出的标题 + `_{sha1(url)[:8]}.pdf`：同 URL 幂等、撞名不互覆盖；
        标题截 60 字（Windows 路径长度上限）、末尾自带 `.pdf` 要剥掉。
        ⚠️ 本目录只写不删，没有自动回收（长期部署要自己按量裁）。
        """
        meta = self._pdf_meta(data, url)            # 先解析：文件名要用它
        stem = helpers.safe_filename(meta.get("title") or "", 60)
        if stem.lower().endswith(".pdf"):
            stem = stem[:-4].strip(" .")
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        name = f"{stem}_{digest}.pdf" if stem else f"pdf_{digest}.pdf"
        try:
            os.makedirs(PDF_DIR, exist_ok=True)
            path = os.path.join(PDF_DIR, name)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            self.logger.warning("PDF dump failed for %s: %s", url, e)
            return None
        entry = {"url": url, "path": path, "bytes": len(data)}
        entry.update(meta)
        return entry

    async def _close_client(self, client) -> None:
        """显式关闭 httpx client（幂等、不抛业务异常），尤其覆盖取消路径。

        不能用 `async with` 自己收拾：worker 被取消时栈上 await 抛 CancelledError，
        `__aexit__` 的清理在取消语境下不保证跑完，连接池悬空 → 长驻进程里逐次
        累积，直到解释器退出才报 `unclosed transport` / `I/O on closed pipe`。
        取消照旧向外传播，关闭失败静默。**销号放在关闭成功之后**：关失败的留
        在登记表里，让 _fetch_direct 退出的兜底再试一次。
        """
        if client.is_closed:
            self._open_clients.discard(client)
            return
        try:
            await client.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if client.is_closed:
            self._open_clients.discard(client)

    async def _close_leftover_clients(self) -> None:
        """兜底：关掉仍登记在册的 httpx client（正常路径已随用随销号）。"""
        for client in list(self._open_clients):
            await self._close_client(client)

    async def _fetch_direct(
        self,
        pending: List[str],
        fetch_count: int,
        scroll_times: int = 0,
        wait_ms: int = 0,
        budget: Optional[float] = None,
    ) -> List[Dict]:
        """固定条数抓取 + 单档过滤。

        退出条件是核心：抓满 fetch_count 条**且在途全部落地**才返回 —— 若看
        len(collected) 收满即停，L0 快返回的一批会把升级到 L1/L2 的慢条目全
        cancel 掉（降级链被架空）。槽位常满，空出即从队列补下一个。
        质量判定只在这里（worker 返回后）；_fetch_chain 内保持纯抓取降级链。
        返回 collected（按 score 降序）；低于阈值的条目就地丢弃。
        """
        loop = asyncio.get_running_loop()
        queue = deque(pending)
        scanned = 0
        collected: List[Dict] = []
        hi = self.config.content_threshold

        host_cache: Dict[str, bool] = {}
        # L0 连接层死域缓存 + 学习观察去重（同域一轮只计一次，与 skill 层一致）。
        learned_hosts: set = set()

        def _learn(url: str, **obs) -> None:
            host = helpers.host_of(url)
            if not host or host in learned_hosts:
                return
            learned_hosts.add(host)
            try:
                filters_learn.penalty_delta(url, **obs)
            except Exception as e:
                self.logger.warning("penalty_delta failed for %s: %s", host, e)

        async def _worker(url: str) -> Optional[Dict]:
            t0 = time.time()
            try:
                chain = await asyncio.wait_for(
                    self._fetch_chain(url, host_cache, scroll_times, wait_ms),
                    timeout=self.config.fetch_timeout_per_url,
                )
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Per-URL timeout (%.0fs), dropped alone: %s",
                    self.config.fetch_timeout_per_url, url,
                )
                _learn(url, fetch_ok=False, latency=time.time() - t0)
                return None
            if chain is _PDF_DONE:
                # PDF 支链：文件已落盘、清单已记。既不记 fetch_ok=False（那是成功
                # 留档），也不进 collected（没有正文，不参与正文排序）。
                self.logger.info("PDF branch done (dumped), not in text pool: %s", url)
                return None
            if not chain:
                _learn(url, fetch_ok=False, latency=time.time() - t0)
                return None
            text, page_title = chain
            latency = time.time() - t0
            if not text:
                _learn(url, fetch_ok=False, latency=latency)
                return None
            score = filters.content_score(text, url)
            return {
                "url": url, "text": text, "score": score, "latency": latency,
                "page_title": page_title,
                "template_hit": filters.is_template_page(text),
                "garbled_hit": filters.is_garbled(text),
            }

        inflight: Dict[asyncio.Future, str] = {}
        deadline = (loop.time() + budget) if budget else None

        def _launch() -> bool:
            nonlocal scanned
            if not queue or scanned >= fetch_count:
                return False
            url = queue.popleft()
            scanned += 1
            t = asyncio.ensure_future(_worker(url))
            inflight[t] = url
            return True

        try:
            while inflight or (scanned < fetch_count and queue):
                while len(inflight) < MAX_CONCURRENT_FETCHES and _launch():
                    pass
                if not inflight:
                    break
                timeout = None
                if deadline is not None:
                    timeout = max(0.0, deadline - loop.time())
                    if timeout <= 0:
                        self.logger.warning(
                            "Global fetch budget %.1fs reached, cancelling %d pending URLs",
                            budget, len(inflight),
                        )
                        break
                done, _ = await asyncio.wait(
                    set(inflight), return_when=asyncio.FIRST_COMPLETED, timeout=timeout
                )
                if not done:
                    if deadline is not None:
                        self.logger.warning(
                            "Global fetch budget %.1fs reached, cancelling %d pending URLs",
                            budget, len(inflight),
                        )
                    break
                for t in done:
                    url = inflight.pop(t, None)
                    try:
                        res = t.result()
                    except Exception as e:
                        self.logger.warning("Fetch worker crashed for %s: %s", url, e)
                        continue
                    if res is None:
                        continue  # 失败/空内容：槽位释放，继续补位
                    s = res["score"]
                    if s >= hi:
                        collected.append(res)
                        self.logger.info(
                            "Accepted (%.2f): %s (%d chars)",
                            s, res["url"], len(res["text"]),
                        )
                    else:
                        self.logger.info("Dropped low-quality (%.2f): %s", s, res["url"])
                        # 学习层最重要的负样本（WAF 乱码页 fetch_ok=True 但 score≈0，
                        # 不记就永远学不到）
                        _learn(
                            res["url"], fetch_ok=True, score=s,
                            template_hit=bool(res.get("template_hit")),
                            garbled_hit=bool(res.get("garbled_hit")),
                            latency=res.get("latency"),
                        )
        finally:
            for t in inflight:
                t.cancel()
                _abandon_task(t)
            if inflight:
                # ⚠️ 不能 gather：cancel 打不断卡在同步阻塞调用里的抓取任务
                # （Scrapling 浏览器通道），gather 会无限等待拖到 120s 全局超时。
                # 改用 asyncio.wait：给 _CANCEL_GRACE 秒正常回收，到点即返。
                await asyncio.wait(list(inflight), timeout=_CANCEL_GRACE)
            # 取消路径的显式收尾：回收窗口到点仍未结束的任务不会自己关连接池。
            await self._close_leftover_clients()
            # 整批结束（正常/预算到点/异常）都要收掉共用浏览器，
            # 否则长驻进程里每批残留一个 chromium driver。
            await self._close_stealthy_session()

        collected.sort(key=lambda d: d["score"], reverse=True)
        if scanned:
            self.logger.info(
                "Fetch done: scanned=%d collected=%d (fetch_count=%d)",
                scanned, len(collected), fetch_count,
            )
        return collected

    async def _get_stealthy_session(self):
        """整批共用的隐身浏览器会话（懒建）。起不来返回 None，整批退回旧模式。

        不逐条 async_fetch：那是每条 URL 起一个新浏览器，冷启动重复付 N 次
        （实测同批同并发 2.2 倍耗时，结果完全一致）。
        `solve_cloudflare` 实测否决（慢 3.5 倍且一条没多拿）；指纹参数代价 ~1%，留作保险。
        """
        if self._stealthy_session is not None or self._stealthy_broken:
            return self._stealthy_session
        async with self._stealthy_lock:
            if self._stealthy_session is None and not self._stealthy_broken:
                try:
                    s = self._stealthy_session_cls(
                        max_pages=self.config.stealthy_max_pages,
                        headless=True,
                        timeout=self.config.scrapling_timeout * 1000,
                        # ⚠️ google_search 是 scrapling 的反爬伪装参数（伪造 Google
                        # referer），与搜索引擎 provider 无关。
                        google_search=True,
                        network_idle=True,
                        hide_canvas=True,
                        block_webrtc=True,
                        allow_webgl=True,
                    )
                    await s.start()
                    self._stealthy_session = s
                    self.logger.info("Stealthy session started (max_pages=%d)",
                                     self.config.stealthy_max_pages)
                except Exception as e:
                    self._stealthy_broken = True
                    self.logger.warning("Stealthy session unavailable, per-URL mode: %s", e)
        return self._stealthy_session

    async def _close_stealthy_session(self) -> None:
        """整批结束时关掉共用浏览器；关不掉也要清标记，别让后续批次复用半死的实例。"""
        s, self._stealthy_session = self._stealthy_session, None
        if s is None:
            return
        try:
            await asyncio.wait_for(s.close(), timeout=10)
        except Exception as e:
            self.logger.warning("Stealthy session close failed: %s", e)

    async def _stealthy_fetch(self, url: str, **kwargs):
        """浏览器抓取包一层独立任务 + shield。

        直接 await 时，取消若打在 `playwright.start()` 拉起 driver 的过程中，
        `__aexit__` 永远不会被调用 —— 已 spawn 的 driver 子进程永久残留（实测
        进程退出时刷 unclosed transport）。shield 后取消只断开外层等待，内部
        任务照常跑完并走自己的清理；引用与迟到异常由 _abandon_task 兜住。
        代价：被取消的那次抓取最多多活 scrapling_timeout 那么久（后台自净）。
        """
        sess = await self._get_stealthy_session()
        if sess is not None:
            # 浏览器级参数已在会话上配好，逐条只传 page_action（滚动/等待）
            task = asyncio.ensure_future(sess.fetch(url, **kwargs))
        else:
            task = asyncio.ensure_future(
                self._stealthy_fetcher.async_fetch(url, headless=True, **kwargs))
        _abandon_task(task)
        return await asyncio.shield(task)

    async def _fetch_chain(
        self,
        url: str,
        host_cache: Dict[str, bool],
        scroll_times: int,
        wait_ms: int,
    ) -> Optional[tuple]:
        """单条完整降级链：L0 httpx → L1 AsyncFetcher → L2 StealthyFetcher（浏览器兜底）。

        排序原则：便宜的先上，免费的兜底。scroll_times / wait_ms 是已解好的
        最终值（fetch_all 里解过一次），本层不再兜底。
        返回 (text, page_title)、`_PDF_DONE`（PDF 已落盘，不升级不进正文池）
        或 None（该条失败/无正文）。
        """
        # L0：连不通（按域缓存判死）/ 非 200 / 无正文 → 升级；
        # status 留作"L2 还能不能救"的证据。
        text, page_title, l0_status = await self._httpx_first(url, host_cache)
        if text is _PDF_DONE:
            return _PDF_DONE
        if text:
            return text, page_title

        # L1：AsyncFetcher.get —— 快速 HTTP，静态
        l1_status = None
        l1_error = ""
        try:
            resp = await self._async_fetcher.get(
                url,
                timeout=self.config.scrapling_timeout,
            )
            # ⚠️ 状态码守卫：4xx/5xx（含代理返回的 502 错误页）一律升级，
            # 不得把错误页文本当正文消费。
            status = getattr(resp, "status", None)
            l1_status = status
            if status is not None and status >= 400:
                self.logger.warning("Direct fetch HTTP %d, escalating: %s", status, url)
            elif resp and resp.html_content:
                page_title = _page_title(str(resp.html_content))
                text = self._extract_text(str(resp.html_content), url)
                if text:
                    self.logger.info("Direct fetch OK: %s (%d chars)", url, len(text))
                    return text, page_title
                self.logger.info("Direct fetch short/empty content, escalating: %s", url)
            else:
                self.logger.warning("Direct fetch no response: %s", url)
        except Exception as e:
            l1_error = str(e)
            self.logger.warning("Direct fetch failed for %s: %s", url, e)

        # L0、L1 都失败 → 判"L2 还值不值得上"（硬阻断与二进制正是整批尾巴的全部来源）。
        skip = _skip_l2_reason(l0_status, l1_status, l1_error, url)
        if skip:
            self.logger.warning("L2 skipped (%s), no JS fix possible: %s", skip, url)
            return None

        # L2：浏览器渲染，支持滚动/等待/Cookie（免费兜底）
        try:
            resp = await self._stealthy_fetch(
                url,
                # 浏览器级参数在会话上配好了；逐条只传渲染相关项。
                # `headless` 不在 StealthFetchParams 里，退回旧逐条模式时才补。
                timeout=self.config.scrapling_timeout * 1000,
                # ⚠️ google_search 是 scrapling 的反爬伪装参数（自动附加 Google
                # referer 降低被拒概率），与 Google 搜索引擎无关，勿误删。
                google_search=True,
                network_idle=True,
                page_action=self._build_page_action(scroll_times, wait_ms),
            )
            status = getattr(resp, "status", None)
            if status is not None and status >= 400:
                self.logger.warning("Stealthy fetch HTTP %d: %s", status, url)
            elif resp and resp.html_content:
                page_title = _page_title(str(resp.html_content))
                text = self._extract_text(str(resp.html_content), url)
                if text:
                    self.logger.info("Stealthy fetch OK: %s (%d chars)", url, len(text))
                    return text, page_title
                self.logger.warning("Stealthy fetch short content: %s", url)
            else:
                self.logger.warning("Stealthy fetch no response: %s", url)
        except Exception as e:
            self.logger.warning("Stealthy fetch failed for %s: %s", url, e)

        self.logger.warning("All fetch methods failed for: %s", url)
        return None

    def _extract_text(self, html: str, url: str) -> Optional[str]:
        """Trafilatura 提取正文并拼接来源头；空/过短返回 None（供升级判定）。"""
        if not html:
            return None
        try:
            md = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                include_formatting=True,
                output_format="markdown",
            )
        except Exception as e:
            self.logger.warning("Trafilatura failed for %s: %s", url, e)
            return None
        if not md:
            return None
        # 先清理尾部 boilerplate 再判长：否则"正文被清理后达标"的内容会被误判
        # 为空壳而升级/丢弃（判空兜底只该管"是不是空的"，质量交给打分）。
        md = self._cleanup_footer(md)
        md = self._strip_citations(md)
        # 拦截/挑战页：反爬前置层返回 HTTP 200 的 reCAPTCHA / CF 挑战页，可见文字
        # 只有几十字，恰好越过判空被当"抓到正文"—— 降级链就此终止，真内容
        # （只有浏览器级拿得到）永远拿不到。实测 PMC 63 字假正文 → 浏览器级
        # 10857 字真正文。见 filters.is_block_page。
        if filters.is_block_page(md):
            self.logger.info("Block/challenge page (not content), escalating: %s", url)
            return None
        if len(md.strip()) <= self.config.min_content_length:
            return None
        return f"## 来源: {url}\n\n{md.strip()}"

    def _build_page_action(self, scroll_times: int, wait_ms: int):
        """传给 StealthyFetcher 的 page_action 回调：mouse.wheel 触发懒加载 + 等待。

        无滚动/等待需求时返回 None，避免浏览器额外开销。
        """
        if scroll_times <= 0 and wait_ms <= 0:
            return None

        async def _action(page):
            for _ in range(max(1, int(scroll_times))):
                try:
                    await page.mouse.wheel(0, 600)
                except Exception:
                    pass
                try:
                    await page.wait_for_timeout(max(int(wait_ms), 300))
                except Exception:
                    pass

        return _action

    def _strip_citations(self, text: str) -> str:
        """删除密度提取后残留的引用角标（<sup>[18]</sup> / 裸 [18]）。"""
        if not text:
            return text
        text = _RE_SUP_CITE.sub("", text)
        text = _RE_BARE_CITE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text

    def _cleanup_footer(self, text: str) -> str:
        """清理尾部 boilerplate（页脚、推荐、免责声明等）。"""
        footer_patterns = [
            r'\n#{1,6}\s*(VIP|APP专享|热门推荐|滚动播报|文章来源|AI声明|发布时间|最后更新|编辑推荐|大家都在看).*$',
            r'\n---+\s*$',
            r'\n(?:©|Copyright|版权所有|All Rights Reserved).*$',
            r'\n(?:声明|免责|注意|提示|免责声明).*?$',
            r'\n(?:责任编辑|责编|编辑[:：]|审核[:：]|校对[:：]|作者[:：]).*?$',
        ]

        for pattern in footer_patterns:
            text = re.sub(pattern, '', text, flags=re.MULTILINE | re.DOTALL)

        lines = text.split('\n')
        cleaned_lines = []
        short_run = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                short_run = []
                continue
            if len(stripped) <= 20 and not re.search(r'[。！？.!?\uff01\uff1f]', stripped):
                short_run.append(line)
            else:
                short_run = []
                cleaned_lines.append(line)

        if len(short_run) < 3:
            cleaned_lines.extend(short_run)

        return '\n'.join(cleaned_lines)
