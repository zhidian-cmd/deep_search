"""Deep Search MCP Server V10.0 配置（搜索改用 Go CLI metasearch_cli_windows_amd64.exe）。

关于 API Key：本文件**不硬编码任何凭据**。密钥由 Go CLI 自行管理
（`metasearch_cli_windows_amd64.exe apikey set <引擎> <KEY>`），进程环境变量亦可被 CLI 读取。
"""
import os
from dataclasses import dataclass

# 统一的运行时产物根目录（日志 / 归档）：所有 agent 宿主共用同一份，便于管理。
CACHE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")
LOG_DIR = os.path.join(CACHE_ROOT, "logs")
# 归档目录（markdown）——归档正文来自 trafilatura 的 markdown 输出，原样落盘。
ARCHIVE_DIR = os.path.join(CACHE_ROOT, "md")
# PDF 原文件留档：抓取时魔数命中 %PDF- 即原样落盘，路径随交付返回。
# ⚠️ 只写不自动清理，长期跑要自己按量裁。
PDF_DIR = os.path.join(CACHE_ROOT, "pdf")


@dataclass
class DeepSearchConfig:
    """Deep Search MCP V10.0 配置"""

    # ---------- Scrapling 抓取配置 ----------
    scrapling_timeout: int = 30
    min_content_length: int = 30        # 判空兜底阈值：只管"是不是空的"，质量交给打分
    # **最终额度池** = 最终交付条数：内容相似去重后按 content_score 降序取前 N 条，
    # 不足则全取；同时决定归档来源数（archive_max_sources 只是天花板）。
    # ⚠️ 与 provider 侧请求条数（text_sources_per_query）、并发槽位数三者互不耦合，
    #    数值相同纯属巧合。
    max_urls_to_try: int = 15
    # 每次固定抓取的 URL 条数，且等全部落地才返回 —— 慢通道（L1/L2）才不会被
    # "收满即停"cancel 掉，降级链才真正参与。29 是实测值。
    fetch_url_count: int = 29
    scrape_budget: float = 45.0         # 整批抓取全局预算（秒）：须 > 单条熔断才有意义
    fetch_timeout_per_url: int = 35     # 单条整链硬熔断（秒）：到点单独放弃，不影响其余

    # 隐身浏览器标签池并发：整批共用一个浏览器实例，再往上收益递减。
    stealthy_max_pages: int = 10

    # ---------- 交互式抓取配置 ----------
    lazy_load_scroll_times: int = 3          # 默认滚动次数
    lazy_load_wait_ms: int = 1000            # 每次滚动后等待时间（毫秒）

    # ---------- 超时 ----------
    overall_timeout: int = 120

    # ---------- 单查询文本来源数 ----------
    # ⚠️ 这是**请求条数**，非最终条数：传给聚合层的 limit，与各引擎自有天花板
    #    共同决定候选量。引擎全通时合计约 80+ 条候选——候选池留深是故意的，
    #    下游去重会减条数。
    text_sources_per_query: int = 15

    # ---------- 正文质量过滤（filters.py） ----------
    # content_score 连续分，单档阈值：score >= 阈值保留，否则丢弃。
    content_threshold: float = 0.4

    # ---------- 内容相似去重（唯一去重闸，位于抓取过滤后、拼 answer 前） ----------
    semantic_dedup_enabled: bool = True
    # 字符 3-gram 包含度阈值（|A∩B|/min(|A|,|B|)）：同文/同源转载 ≥0.6、
    # 独立条目 <0.3；0.60 定位是「趋近多样」而非「保证互不相同」。
    semantic_dedup_ngram_threshold: float = 0.60
    # 参与编码的正文前缀长度：实测 512 与 1500 字判定结果基本一致，取 512 省成本。
    semantic_dedup_max_chars: int = 512
    # 同 host 限条：同一域名最多排前几条，超额顺延队尾（不丢弃，不减抓取总量）。
    # 动机：聚合站占满高分名额会让独立信源不足。0 = 关闭。
    same_host_cap: int = 2

    # ---------- 首页/根域名标题一致性兜底 ----------
    # 短路径 URL 抓到的常是"又长又像文章"的首页/推广页（正文分 0.92~1.00，
    # 其他闸全拦不住）。用「页面 <title> ↔ 候选标题」Jaccard 一致性判疑似首页。
    homepage_guard_enabled: bool = True
    homepage_guard_path_depth: int = 1      # 只查根域名/1级路径；深路径文章页不受影响
    homepage_guard_min_title_overlap: float = 0.15  # 标题 Jaccard 低于此值判不一致
    homepage_guard_min_title_len: int = 8   # 两侧标题任一短于此值不判（防误伤短标题页）

    # ---------- query 相关性闸 ----------
    # content_score 判"像不像正文"，本闸判"讲不讲这件事"：比对标题+正文与 query
    # 的最长连续公共片段，短于门槛判跑题移出主链路（移出而非压分——压分后去重
    # 仍可能回填）。零依赖、确定性可解释；空 query / 语言不对等一律放行。
    relevance_guard_enabled: bool = True
    relevance_guard_min_overlap: int = 3    # 共享片段下限 3 个汉字（英文按单词整体算）

    # ---------- 输出控制 ----------
    # 单次调用返回的最大字符数，0 = 不限制。实测 PDF 全文类可达 325K 字符，
    # 不截会撑爆 MCP 宿主 token 上限（结果被落盘，调用方拿不到正文）。
    # 需要完整全文时读归档 markdown（temp/md/）。
    max_total_chars: int = 60000

    # 归档最多落盘前 N 名来源，截断单位是整节。
    # ⚠️ 本值是天花板而非收缩：实际落盘数 = min(本值, max_urls_to_try)，
    #    归档与 answer 同源于 items —— 单抬本值无效。0 = 不限制。
    archive_max_sources: int = 20

    # ---------- 三维合成排序（V10.4） ----------
    # content_score 照旧管形态（0.4 闸门不变）；authority_score（信源权威度，
    # filters_keywords.AUTHORITY_TIERS）与 query_coverage（query 词覆盖率，
    # 批内 idf 加权）是两个独立维度，合成 rank_score 决定交付/截断顺序。
    # 权重经三轮调参（2026-10-03），样本滚雪球：粮油 59 条 → 10 领域 209 条
    # → 20 领域 764 条 / 58 题 / 491 host（现场实弹采集，留一领域外推 10/10
    # 稳定）。最优 a 随样本扩大持续上移（0.25→0.35→0.40），c 收敛到 0.10：
    # 终版 0.50/0.40/0.10 在 20 个领域全部 ≥ 初版无一回退，与 c=0.05 的激进
    # 点差距 0.001。coverage 保留一票覆盖"相关性闸放行的擦边页"。
    # 数据与标注：tests/_eval_dataset_v3.json（AI 辅助标注，非人工金标）。
    authority_enabled: bool = True
    coverage_enabled: bool = True
    rank_w_form: float = 0.50
    rank_w_authority: float = 0.40
    rank_w_coverage: float = 0.10
    # V10.5：rank_items 默认改乘性合成 form×(0.55+0.45·auth)×(0.8+0.4·cov)，
    # 弱核心词页再 ×0.45（食品包装 60 来源评审：线性加权下权威度只是平票
    # 依据，SEO 软文/文本汤/跑题报告凭形态分就能压过权威页）。False = 回退
    # V10.4 线性语义（上面三个权重只在线性分支生效）。
    rank_multiplicative: bool = True

    # ---------- PDF 附件摘要内联（V10.3） ----------
    # 命中 PDF 落盘时在 server 端直接抽正文关键片段内联进响应。动机：原设计
    # "正文不返回、路径在归档"依赖调用方自觉消费，而消费纪律写在 SKILL.md 里
    # —— MCP 直调时不会加载，实测 8 轮检索 pdf_dumps>0 全部被漏看，标准原文
    # 级材料整批丢失。摘要窗口按关键词打分选取（摘要/结论/工艺/参数 + query 词）。
    pdf_digest_enabled: bool = True
    pdf_digest_pages: int = 12            # 参与抽取的最大页数（页数再多边际递减）
    pdf_digest_win: int = 300             # 片段窗口长度（字符）
    pdf_digest_max_per_pdf: int = 1200    # 单份 PDF 摘要额度（字符）
    pdf_digest_max_total: int = 6000      # 单次响应全部 PDF 摘要总额度（字符）

    # ---------- PDF 落盘缓存（V10.3） ----------
    # 按 URL hash 跨轮复用：命中即不重新下载/写盘（元数据与摘要直接复用），
    # mtime 不再被覆盖，"本轮新增了哪些 PDF"可判。PDF 内容极少变，同字节数
    # 即视为未变。
    pdf_cache_enabled: bool = True

    # ---------- simhash 跨源去重（V10.3） ----------
    # 与 3-gram 包含度 OR 组合：包含度（单窗+多窗）管"同文/镜像子串"，
    # simhash 兜"整篇复用但重排/换字"——gram 类判据对重排全盲，simhash 看
    # 全段词汇分布才看得见。64-bit 汉明距离 ≤ 4 判同文（独立文章实测 >10，
    # 阈值保守防误杀）。
    simhash_enabled: bool = True
    simhash_hamming_threshold: int = 4
