"""抓取层正文质量过滤 —— 独立文件，web_fetch 只抓不判质。

content_score(text, url) -> 0~1 连续分（不是布尔过滤）：
- 硬否决（压到极低分，不是直接丢）：
    url_blacklisted(url) 命中 → 0.0
    is_block_page(text) 命中  → 0.0   （反爬挑战页 / WAF 页）
    is_garbled(text) 命中     → 0.0
    is_template_page(text) 命中 → 0.05
- 软信号加权（归一化 0~1）：
    长度合理性 0.35 + 标点密度 0.20 + 段落结构 0.20
    + 中英文占比 0.15 + 域名权重 0.10
- 弱模板标记 / 负向营销词累计扣分，正向学术词小幅加分。
- 置信度折扣（最后一步）：score *= min(1, L/200) —— 短文本的比值型软信号
  样本太小不可信，按证据量打折；与「长度=质量」无关。
- 低商业价值页降权（V10.1，置信度折扣之后）：B2B 店铺域 → 封顶 0.2；
  采购/产品路径 → ×0.5（low_value_tier 分档，表在 filters_keywords）。

调用方语义：score >= content_threshold (0.4) 保留，否则丢弃（单档阈值）。
中英文占比：max(cjk_ratio, ascii_word_ratio) —— 两种语言都不吃亏。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from .filters_keywords import (
    URL_BLACKLIST_HOST_SUBSTRINGS,
    URL_BLACKLIST_PATH_PATTERNS,
    TEMPLATE_STRONG_MARKERS,
    TEMPLATE_WEAK_MARKERS,
    GARBLED_PATTERNS,
    GARBLED_THRESHOLDS,
    BLOCK_PAGE_MAX_LEN,
    BLOCK_PAGE_MARKERS,
    DOMAIN_WEIGHTS,
    LOW_VALUE_HOST_CAP,
    LOW_VALUE_PATH_FACTOR,
    LOW_VALUE_HOST_SUBSTRINGS,
    LOW_VALUE_PATH_PATTERNS,
    TEXT_SOUP_HOSTS,
    TEXT_SOUP_PATH_PATTERNS,
    MT_REPORT_MILL_DOMAINS,
    FRONT_GATE_GENERIC_TERMS,
    WEAK_CORE_FACTOR,
    WHITELIST_HOSTS,
    QUALITY_POSITIVE_MARKERS,
    QUALITY_NEGATIVE_MARKERS,
    PUNCTUATION_CHARS,
    AUTHORITY_TIERS,
    AUTHORITY_DEFAULT,
)
from . import filters_learn
from . import helpers

_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_RE_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
# 控制字符（排除 \t \n \r）：is_garbled 的非打印比例一次扫完，替代逐字符循环
_RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# 抓取层 _extract_text 注入的出处头「## 来源: <url>」—— 元数据，不是正文
_RE_SOURCE_HEADER = re.compile(r"^\s*#{0,3}\s*来源\s*[:：]\s*\S+\s*", re.I)
_TEMPLATE_HEAD = 3000  # 模板/营销标记通常在页头与页尾，只扫前后窗口

_STRONG_LOWER = tuple(m.lower() for m in TEMPLATE_STRONG_MARKERS)
_WEAK_LOWER = tuple(m.lower() for m in TEMPLATE_WEAK_MARKERS)
_BLOCK_LOWER = tuple(m.lower() for m in BLOCK_PAGE_MARKERS)


def strip_source_header(text: str) -> str:
    """去掉抓取层注入的出处头「## 来源: <url>」。

    它是元数据不是正文：URL 长度会冒充正文长度、稀释置信度折扣（实测知乎
    35 字登录墙 + 63 字出处头 → 折扣 0.175 虚高到 0.46），URL 里的 query 还会
    让相关性闸全部误命中。相似度比对同样不能带它 —— 同域两篇无关文章会因
    URL 前缀相同而虚高。三处读者（质量分、两个相似度闸）共用这一份。
    """
    return _RE_SOURCE_HEADER.sub("", (text or "").strip(), count=1).strip()


# ---------- 硬否决信号 ----------

def url_blacklisted(url: str) -> bool:
    """URL 黑名单：host 子串 + (host, path) 组合，不做全 URL 子串匹配。

    泛化子串只允许在 host 上用完整域名（"download." 含 "ad." 的教训），
    路径级垃圾用 PATH_PATTERNS 组合匹配。
    """
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if any(s in host for s in URL_BLACKLIST_HOST_SUBSTRINGS):
        return True
    path_and_query = ((p.path or "") + ("?" + p.query if p.query else "")).lower()
    if path_and_query:
        for host_sub, path_sub in URL_BLACKLIST_PATH_PATTERNS:
            if host_sub in host and path_sub in path_and_query:
                return True
    return False


def low_value_tier(url: str) -> int:
    """低商业价值页分档（0=正常 / 1=路径档软降权 / 2=店铺档封顶）。

    表与分档依据见 filters_keywords.LOW_VALUE_* 注释块。只判 URL 形态、
    不看正文 —— 抓取前分区也要用它，那时还没有正文。
    """
    if not url:
        return 0
    try:
        p = urlparse(url)
    except Exception:
        return 0
    host = (p.hostname or "").lower()
    if host and any(s in host for s in LOW_VALUE_HOST_SUBSTRINGS):
        return 2
    path_and_query = ((p.path or "") + ("?" + p.query if p.query else "")).lower()
    if path_and_query:
        try:
            decoded = unquote(path_and_query).lower()
        except Exception:
            decoded = path_and_query
        for pat in LOW_VALUE_PATH_PATTERNS:
            if pat in path_and_query or pat in decoded:
                return 1
    return 0


def text_soup_url(url: str) -> bool:
    """文本汤页判定（V10.5）：预览壳/表单壳的站点形态规则。

    判 URL 不判正文——这类页（夸克文档预览、问卷表单）的形态信号失灵，
    正文打分不可靠，只有 URL 形态稳定。表见 filters_keywords.TEXT_SOUP_*。
    """
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if any(s in host for s in TEXT_SOUP_HOSTS):
        return True
    path = (p.path or "").lower()
    if path:
        for host_sub, path_sub in TEXT_SOUP_PATH_PATTERNS:
            if host_sub in host and path_sub in path:
                return True
    return False


def partition_low_value(urls: List[str]) -> Tuple[List[str], List[str]]:
    """稳定分区：低商业价值 URL 沉到队列尾部（抓取窗口之外），只重排不丢弃。

    返回 `(ordered, demoted)`：ordered = 正常页（原序）+ 低价值页（原序），
    demoted = 被沉底的 URL（供日志与 metadata 计数）。池子比抓取窗口浅时，
    沉底的 URL 仍会被抓 —— 降权语义，不是拉黑。
    """
    normal: List[str] = []
    demoted: List[str] = []
    for u in urls:
        (demoted if low_value_tier(u) else normal).append(u)
    return normal + demoted, demoted


def is_garbled(text: str) -> bool:
    """乱码 / WAF 页判定：正则特征 + 非打印比例 + 中文占比兜底。"""
    t = text or ""
    if not t.strip():
        return True
    for pat in GARBLED_PATTERNS:
        try:
            if re.search(pat, t):
                return True
        except re.error:
            continue
    n = len(t)
    th = GARBLED_THRESHOLDS
    non_printable = len(_RE_CTRL.findall(t)) / n
    if non_printable > th["non_printable_ratio"]:
        return True
    if n > th["min_len_for_chinese_check"]:
        cjk_ratio = len(_RE_CJK.findall(t)) / n
        ascii_words = len(_RE_ASCII_WORD.findall(t))
        # 既无中文又几乎无英文单词：大概率是编码失败/JS 压缩产物
        if cjk_ratio < th["chinese_ratio_min"] and ascii_words < 5:
            return True
    return False


def is_template_page(text: str) -> bool:
    """模板/提示页判定：JS 提示、登录墙、验证页、404 等（扫首尾窗口）。"""
    t = (text or "").strip()
    if not t:
        return False
    head = t[:_TEMPLATE_HEAD].lower()
    tail = t[-_TEMPLATE_HEAD:].lower() if len(t) > _TEMPLATE_HEAD else ""
    return any(m in head or m in tail for m in _STRONG_LOWER)


def is_block_page(text: str) -> bool:
    """拦截 / 挑战页判定：HTTP 200 的"假正文"（reCAPTCHA / Cloudflare / WAF）。

    与 is_template_page 的区别：后者查**站内**壳页（登录墙、404），压分即可；
    本函数查**站外**反爬前置层塞进来的挑战页 —— 必须升级抓取链（返回 None
    让降级链继续），压分只是把它丢掉，真内容仍然没拿到。两个调用点：
    web_fetch._extract_text 的判空闸（主用途）+ content_score 硬否决（兜底）。

    ⚠️ 只对短页面生效（BLOCK_PAGE_MAX_LEN）：挑战页可见文字极少（已知样本
    63~356 字），而正常正文完全可能引用 "Just a moment" 这类字眼，整页匹配
    会误杀长文。这里是"判空"不是"判质"。
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > BLOCK_PAGE_MAX_LEN:
        return False
    low = t.lower()
    return any(m in low for m in _BLOCK_LOWER)


# ---------- 软信号 ----------

def _domain_weight(url: str) -> float:
    """静态权重 + 动态学习 delta：final = clamp(static + current_delta, 0.1, 1.5)。

    WHITELIST_HOSTS 命中 → 忽略动态层（用户手动纠偏出口）；动态层只做软降权，
    永不触碰 url_blacklisted（自动误判最坏 = 排名靠后，不是消失）。
    """
    host = helpers.host_of(url)
    best = None
    for domain, w in DOMAIN_WEIGHTS.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, w)
    static_w = best[1] if best else 1.0
    if any(host == h or host.endswith("." + h) for h in WHITELIST_HOSTS):
        return static_w  # 白名单：忽略动态 delta
    try:
        dyn = filters_learn.current_delta(host)
    except Exception:
        dyn = 0.0  # 学习层故障不影响打分主流程
    return max(0.1, min(1.5, static_w + dyn))


def _soft_scores(text: str, url: str) -> float:
    """软信号加权：长度 0.35 + 标点 0.20 + 段落 0.20 + 语言 0.15 + 域名 0.10。"""
    n = max(len(text), 1)
    # 长度合理性 0.35：200~8000 满分；30 以下 0；超长轻衰减（boilerplate 堆积）
    L = len(text)
    if L >= 200:
        len_s = 1.0
        if L > 8000:
            len_s *= max(0.7, 1.0 - (L - 8000) / 40000.0 * 0.3)
    elif L >= 30:
        len_s = (L - 30) / 170.0
    else:
        len_s = 0.0
    # 标点密度 0.20：健康区间 ~0.01~0.05；无标点（base64/JS）趋 0；标点轰炸减半
    puncts = sum(text.count(c) for c in PUNCTUATION_CHARS)
    p = puncts / n
    if p >= 0.01:
        punct_s = min(1.0, p / 0.05)
        if p > 0.3:
            punct_s *= 0.5
    else:
        punct_s = p / 0.01
    # 段落结构 0.20：多行且实质行占比高；单行但够长（整段输出）也给满分
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        para_s = 0.0
    elif len(lines) <= 2:
        para_s = 1.0 if sum(len(l) for l in lines) >= 300 else 0.3
    else:
        substantial = sum(1 for l in lines if len(l.strip()) >= 15)
        para_s = min(
            1.0,
            0.5 * (substantial / len(lines)) + 0.5 * min(1.0, len(lines) / 6.0),
        )
    # 中英文占比 0.15：max(cjk, ascii)，两种语言都不吃亏
    cjk_ratio = len(_RE_CJK.findall(text)) / n
    ascii_ratio = sum(len(w) for w in _RE_ASCII_WORD.findall(text)) / n
    lang_s = min(1.0, max(cjk_ratio, ascii_ratio) / 0.4)
    # 域名权重 0.10：0.5~1.5 → 0~1
    dw = max(0.0, min(1.0, (_domain_weight(url) - 0.5) / 1.0))
    return 0.35 * len_s + 0.20 * punct_s + 0.20 * para_s + 0.15 * lang_s + 0.10 * dw


# ---------- 对外主入口 ----------


def content_score(text: str, url: str = "") -> float:
    """正文质量连续分（0~1）。打分逻辑见模块 docstring。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    # 剔掉出处头：URL 长度会冒充正文长度、稀释置信度折扣
    t = strip_source_header(t)
    if not t:
        return 0.0
    if url and url_blacklisted(url):
        return 0.0
    if is_block_page(t):
        return 0.0
    if is_garbled(t):
        return 0.0
    if is_template_page(t):
        return 0.05

    score = _soft_scores(t, url)

    head = t[:_TEMPLATE_HEAD].lower()
    tail = t[-_TEMPLATE_HEAD:].lower()
    scan = head + tail
    weak_hits = sum(1 for m in _WEAK_LOWER if m in scan)
    score -= min(0.30, weak_hits * 0.03)
    neg_hits = sum(1 for m in QUALITY_NEGATIVE_MARKERS if m in scan)
    score -= min(0.30, neg_hits * 0.10)
    pos_hits = sum(1 for m in QUALITY_POSITIVE_MARKERS if m in scan)
    score += min(0.15, pos_hits * 0.03)

    # 置信度折扣（最后一步）：比值型软信号在短文本上不可靠（35 字登录墙的
    # cjk_ratio 天然接近 1，能被抬过阈值）。按证据量线性折扣，与"长度=质量"无关。
    score *= min(1.0, len(t) / 200.0)

    # 低商业价值页降权（V10.1）：店铺档封顶 / 路径档打折。放在置信度折扣之后
    # —— 它是"页面类型"判据，不该被证据量稀释。表与场景见 filters_keywords。
    tier = low_value_tier(url) if url else 0
    if tier == 2:
        score = min(score, LOW_VALUE_HOST_CAP)
    elif tier == 1:
        score *= LOW_VALUE_PATH_FACTOR

    # 文本汤页降权（V10.5）：预览壳/表单壳封顶。同样在置信度折扣之后——
    # 站点形态判据，不参与软信号。表与"为何不做复读率检测"见 filters_keywords。
    if url and text_soup_url(url):
        score = min(score, LOW_VALUE_HOST_CAP)

    return round(max(0.0, min(1.0, score)), 4)


# ========== 信源权威度分（V10.4：独立第二维度，与 content_score 正交） ==========

def authority_score(url: str) -> float:
    """URL → 信源权威度 0~1（AUTHORITY_TIERS 最长后缀匹配，未命中 0.50）。

    只判"谁说的"，不判"说得干不干净"（那是 content_score）——两者正交：
    厂商广告页可以是干净正文（形态分高）但权威度低；标准原文页带表格断行
    （形态分平平）但权威度满分。静态表不打分迭代，接 filters_learn 的降权
    是刻意不做：学习层抓的是形态/延迟信号，掺进权威度会互相污染。
    """
    host = (urlparse(url).hostname or "").lower()
    best = None
    for dom, tier in AUTHORITY_TIERS.items():
        if host == dom or host.endswith("." + dom):
            if best is None or len(dom) > len(best[0]):
                best = (dom, tier)
    return best[1] if best else AUTHORITY_DEFAULT


# ========== query 词覆盖率（V10.4：连续信号，取代"3 字过闸"的单一判据） ==========

_RE_COV_SPLIT = re.compile(r"[\s,，、;；]+")
_COV_TERM_MIN_LEN = 2       # 短于 2 字的 token 无判别力
_COV_NO_TERM_DEFAULT = 0.5  # query 拆不出词时的中性分（不奖不罚）


def query_coverage(texts: List[str], query: str) -> List[float]:
    """批量计算每条正文对 query 的**词覆盖率** 0~1（与 query 同序）。

    覆盖率 = Σ idf(词)·命中(词) / Σ idf(词)，命中 = 词的精确子串出现在正文
    （大小写归一）。idf 按**本批**文档频率算：全批都命中的词（如检索稻谷时
    的"稻谷"）自动降权，只在少数文档出现的词（如"蒸谷米"）权重高——这是
    把旧 3 字过闸的"命中/不命中"升级成连续信号的关键：过闸只回答"沾不沾边"，
    覆盖率回答"这篇讲了这个问题的几成"。

    批内统计是刻意设计：coverage 的用途是**同批排序**（合成 rank_score），
    跨批可比性不需要。query 拆不出词（纯单字/空）时返回中性 0.5 列表。
    """
    terms = [t for t in _RE_COV_SPLIT.split((query or "").strip())
             if len(t) >= _COV_TERM_MIN_LEN]
    n = len(texts)
    if not terms or n == 0:
        return [_COV_NO_TERM_DEFAULT] * n
    low_texts = [(t or "").lower() for t in texts]
    idfs = {}
    for t in terms:
        tl = t.lower()
        df = sum(1 for tx in low_texts if tl in tx)
        idfs[t] = math.log(1.0 + n / (1.0 + df))
    out = []
    for tx in low_texts:
        w_sum = h_sum = 0.0
        for t, w in idfs.items():
            w_sum += w
            if t in tx:
                h_sum += w
        out.append(round(h_sum / w_sum, 4) if w_sum else _COV_NO_TERM_DEFAULT)
    return out


# ========== 合成排序（V10.4 线性 → V10.5 乘性，可回退） ==========

def rank_items(items: List[Dict[str, Any]],
               w_form: float = 0.60,
               w_auth: float = 0.25,
               w_cov: float = 0.15,
               multiplicative: bool = True) -> List[Dict[str, Any]]:
    """三维合成分，降序返回。

    **乘性（V10.5 默认）**：rank_score = form × (0.5 + auth) × (0.9 + 0.2·cov)，
    条目带 weak_core=True 时再 × WEAK_CORE_FACTOR。

    动机（2026-10-07 食品包装 4 轮 60 来源用户评审）：线性加权下权威度只是
    平票依据——form=1.0/auth=0.50 的 SEO 软文恒压过 form=0.88/auth=0.95 的
    权威页，夸克预览文本汤 0.98、跑题报告都能进前五。乘性把权威度升为
    "一票否决级"权重。

    - auth 因子 (0.5+auth)：auth=0.5（未命中兜底）→ 1.0 中性不奖不罚；
      auth∈[0.28, 0.95] → 因子 [0.78, 1.45]——auth=0.5 与 0.95 之间差
      1.45 倍，权威页对营销页近乎一票否决（首版 0.55+0.45·auth 太弱，
      与 cov 因子叠加后实测 0.929 vs 0.93 惜败，已废弃）。
    - cov 因子 (0.9+0.2·cov)：cov=0.5（中性缺省）→ 1.0；保留 V10.4 的
      "覆盖率给擦边页一票"，量级压到 ±10%——首版 ±20% 时 cov=1.0 的
      SEO 页能借 cov 翻盘，同样废弃。
    - weak_core ×0.45：query 核心词没进标题+前 500 字的跑题页（HALS 添加剂
      报告、烩面机厂商词条实测案例），由 core_term_front_hits 判定。

    **线性（multiplicative=False）**：V10.4 语义原样保留
    rank_score = w_form·form + w_auth·auth + w_cov·cov，作为回退开关
    （config.rank_multiplicative=False）。

    rank_score 同时被 truncate_by_score 与去重幸存者排序消费——三处读者
    共用一个排序键，不会漂移。
    """
    for d in items:
        form = float(d.get("score") or 0.0)
        auth = float(d.get("authority") or 0.0)
        cov = float(d.get("coverage") or 0.0)
        if multiplicative:
            rs = form * (0.5 + auth) * (0.9 + 0.2 * cov)
            if d.get("weak_core"):
                rs *= WEAK_CORE_FACTOR
        else:
            rs = (w_form * form + w_auth * auth + w_cov * cov)
        d["rank_score"] = round(rs, 4)
    return sorted(items, key=lambda d: d.get("rank_score", 0.0), reverse=True)


def rank_by_content_score(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 content_score 降序排列，返回新列表（生产者都已打好分）。

    ⚠️ V10.4 起主链路改用 rank_items（三维合成）；本函数保留给只需要形态序
    的调用方与旧测试。
    """
    return sorted(items, key=lambda d: d.get("score", 0.0), reverse=True)


def truncate_by_score(items: List[Dict[str, Any]], max_chars: int) -> Tuple[str, Set[str]]:
    """按 score 降序累加正文到 max_chars（低分先排除），条目间以 --- 分隔。

    返回 `(text, cut_urls)`：
      - text：拼好的正文。空正文不占额度；超限即断，其后条目全部不纳入；
        临界条目剩余额度 >100 时被切一刀（否则整条丢弃）。
      - cut_urls：**未完整纳入**的来源 url 集合（部分截断 + 完全未纳入），
        调用方据此标注「哪条正文不能当完整信源引用」。
    """
    parts: List[str] = []
    total = 0
    cut: Set[str] = set()
    exhausted = False
    # 排序键与交付顺序同源（rank_score，V10.4；旧数据无此键回退 score）——
    # 截断砍的必须是"合成排序的队尾"，不是形态分队尾。
    for it in sorted(items, key=lambda d: d.get("rank_score", d.get("score", 0.0)),
                     reverse=True):
        t = (it.get("text") or "").strip()
        if not t:
            continue
        url = it.get("url") or ""
        if exhausted:
            cut.add(url)
            continue
        if total + len(t) <= max_chars:
            parts.append(t)
            total += len(t)
            continue
        cut.add(url)          # 临界条目：部分截断，或剩余额度不足 100 而整条丢弃
        exhausted = True
        remain = max_chars - total
        if remain > 100:
            parts.append(t[:remain])
    return "\n\n---\n\n".join(parts), cut


# ========== 首页/根域名标题一致性兜底（见 config.homepage_guard_*） ==========


def _norm_title(s: str) -> str:
    """标题归一化：去空白/去标点/小写，保留中英文与数字。"""
    s = (s or "").strip()
    s = re.sub(r"[\s\u3000]+", "", s)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s.lower())


def _jaccard_bigram(a: str, b: str) -> float:
    """字符二元组 Jaccard（中文比整串等值更稳，英文因字母对共现偏高）。"""
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    if len(a) == 1 and len(b) == 1:
        return 1.0 if a == b else 0.0
    ba = {a[i:i + 2] for i in range(len(a) - 1)}
    bb = {b[i:i + 2] for i in range(len(b) - 1)}
    u = ba | bb
    return len(ba & bb) / len(u) if u else 0.0


def homepage_title_mismatch(
    url: str,
    page_title: str,
    candidate_title: str,
    *,
    query: str = "",
    max_path_depth: int = 1,
    min_overlap: float = 0.15,
    min_title_len: int = 8,
) -> bool:
    """短路径 URL 上，页面 <title> 与候选标题明显不一致 → 疑似首页/跳转/推广页。

    设计：首页型垃圾正文分 0.92~1.00，其他闸全拦不住；真实垃圾案例的标题
    Jaccard 0.000~0.027，合法深路径文章 0.28+，0.15 阈值留足余量。
    只查路径深度 ≤ max_path_depth 的 URL；两侧标题任一为空/过短不判；
    候选标题与 query 几乎一致（引擎回显，无判别力）不判。
    """
    try:
        path = (urlparse(url).path or "").strip("/")
        depth = len([s for s in path.split("/") if s])
    except Exception:
        return False
    if depth > max_path_depth:
        return False

    cand = _norm_title(candidate_title)
    page = _norm_title(page_title)
    if len(cand) < min_title_len or len(page) < min_title_len:
        return False

    qn = _norm_title(query)
    if qn and len(cand) >= 6 and _jaccard_bigram(cand, qn) > 0.8:
        return False

    return _jaccard_bigram(cand, page) < min_overlap


# ========== query 相关性闸（见 config.relevance_guard_*） ==========
# 与 content_score 正交 —— 后者问"像不像正文"，本函数问"是不是在讲这件事"。

_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_RE_QUERY_EN = re.compile(r"[A-Za-z]{3,}")
# 中文 query 撞上"几乎不含中文"的页时字符重叠无判别力 → 判"无法判定"。
# 用占比不用绝对字数：绝对阈值会把短中文页误判成外文页、反而全部放行。
_LANG_MISMATCH_CJK_RATIO = 0.05


def query_overlap(text: str, query: str, title: str = "") -> Optional[int]:
    """query 与「页面标题 + 正文」共享的**最长连续片段**长度（字符数）。

    返回 None = **无法判定**（query 为空 / 全是数字与单字 / 中文 query 撞上
    几乎无中文的页面）—— 调用方必须放行（fail-open）。

    为什么是"最长连续片段"：query 常是不带空格的长串，切 2-gram 后"量产/
    时间表"这类通用词会稀释覆盖率误杀真正文；余弦要模型。这里只问一句：
    这页有没有跟 query 连续重合若干字的片段（如"固态电池"）。判定方向单一
    （只做减法）：命中即放行，不存在"匹配得不够漂亮"被误杀。
    英文按**单词**判定（词边界匹配）—— 避免 "ai" 命中 "said" 的子串假阳性。

    ⚠️ 判别力下限：query 必须有 ≥3 字连续中文串或 ≥3 字母英文词，否则一律
    返回 None（放行）—— 没有判别力的 query 不该开这道闸。
    """
    q = (query or "").strip()
    if not q:
        return None
    runs = _RE_CJK_RUN.findall(q)
    words = _RE_QUERY_EN.findall(q)
    if not words and not any(len(r) >= 3 for r in runs):
        return None

    t = (text or "").strip()
    if t:
        # 剔掉出处头 —— URL 里可能原样带着 query，留着会让每一页都"命中"。
        t = strip_source_header(t)
    hay = f"{title or ''}\n{t}".lower()
    if not hay.strip():
        return 0

    best = 0
    for run in runs:
        for size in range(len(run), 0, -1):
            if size <= best:
                break
            if any(run[i:i + size] in hay for i in range(len(run) - size + 1)):
                best = size
                break
    for w in words:
        wl = w.lower()
        if len(wl) > best and re.search(rf"\b{re.escape(wl)}\b", hay):
            best = len(wl)
    if best == 0 and runs and not words:
        # 纯中文 query × 几乎不含中文的页：这是"没法比"，不是"跑题"。
        # query 里有英文词时词边界匹配对任何语言都有效，就不再豁免。
        cjk_ratio = len(_RE_CJK.findall(hay)) / max(len(hay), 1)
        if cjk_ratio < _LANG_MISMATCH_CJK_RATIO:
            return None
    return best


# ========== 弱核心词前窗闸（V10.5，见 filters_keywords.WEAK_CORE_FACTOR） ==========
_FRONT_HEAD = 500  # 前窗长度：标题 + 正文前 500 字

_RE_HAS_LETTER = re.compile(r"[\u4e00-\u9fffA-Za-z]")


def core_term_front_hits(text: str, query: str, title: str = "",
                         head: int = _FRONT_HEAD) -> int:
    """query 核心词在「标题 + 正文前 head 字」内的**最大单词条频**（泛词除外）。

    返回 99 = 无法判定（query 拆不出词 / 全是泛词 / 中文 query 撞英文页），
    调用方放行。

    三个实测教训（2026-10-07 食品包装归档标定）：
    - 取 max 不取 sum：HALS 跑题报告前窗 sum 恰好 = 2（市场规模×1+趋势×1）
      压线通过；专名"食品包装"才是分离点——跑题页 0 次、切题页 5~10 次。
    - 泛词排除（FRONT_GATE_GENERIC_TERMS）："市场规模"这类词在切题页和跑题
      页的前窗都会出现 1~2 次，max 口径下仍能把跑题页抬过阈值（实测 ×2），
      必须剔除，只让专名参与统计。
    - 纯数字词（"2025"）无主题判别力，不参与统计。

    判定：< 2 判弱相关（weak_core，排序时 × WEAK_CORE_FACTOR）；判定窗口
    是"页面前窗"而非全文——跑题页的跑题证据恰恰在开头，全文口径的
    relevance_guard 与 coverage 都已放行过它们。
    """
    terms = [t.lower() for t in _RE_COV_SPLIT.split((query or "").strip())
             if len(t) >= _COV_TERM_MIN_LEN
             and _RE_HAS_LETTER.search(t)
             and t not in FRONT_GATE_GENERIC_TERMS]
    if not terms:
        return 99
    window = f"{title or ''}\n{(text or '')[:head]}".lower()
    if not window.strip():
        return 99
    # 中文 query 撞英文页：前窗几乎无中文时字符频次无判别力，fail-open
    if any(_RE_CJK.search(t) for t in terms):
        cjk_ratio = len(_RE_CJK.findall(window)) / max(len(window), 1)
        if cjk_ratio < _LANG_MISMATCH_CJK_RATIO:
            return 99
    return max(window.count(t) for t in terms)


# ========== 机翻报告工厂打标（V10.5，见 filters_keywords.MT_REPORT_MILL_DOMAINS） ==========

_RE_ZH_PATH = re.compile(r"^/(zh|cn)(?:[/\-]|$)|zh-cn")


def mt_report_mill_flag(url: str) -> bool:
    """报告工厂的**中文子页** → True（疑似机翻，数字未经核实）。

    只打标不拦：页面仍有信息量，但机器翻译的市场数字多次实测自相矛盾
    （同页两个量级、单位换算掉零），调用方引用前必须回查英文原页。
    英文原页不打标。
    """
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in MT_REPORT_MILL_DOMAINS):
        return False
    return bool(_RE_ZH_PATH.search((p.path or "").lower()))
