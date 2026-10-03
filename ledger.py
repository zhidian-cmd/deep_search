# -*- coding: utf-8 -*-
"""跨搜索来源账本（**只在进程内存**）+ 跨轮去重判据（L1 / L3）。

为什么需要这一层：一次检索内部的三道闸（CLI 多引擎合并 → 排名器 URL 去重 → 正文
3-gram 收敛）都只看**当前这一批**。同一轮对话里换个关键词再搜，上一轮给过的来源会被
**重新交付一遍**。这一层把"前面给过什么"记在进程内存：

  L1  规范化 URL 全等             净的是 http/https、`?utm_*`、尾斜杠、`%xx` 大小写变体
  L3  与账本里的正文比包含度      净的是**跨站转载** —— 只有正文能判的那一类

⚠️ 不能直接移植 CLI 的去重替代这一层：CLI 判决有 `veto:cross_site`「跨注册域永不
合并」——单次查询内的正确设计，而跨轮要处理的恰恰是"同一篇标准被几十个站转载"。
L2（标题/摘要近似）评估后不做：URL 归一等不掉的同站换 URL 形态实测罕见。

账本内容与生命周期：
  · URL（原始形 + 比对形）与首次交付轮次 —— 全轮保留（约 100 字节/条）。
  · 交付过的**正文** —— 只服务 L3，按总字符预算保留最近若干轮，超了整轮清掉正文。
  · 进程重启即清零，不落盘 —— 跨会话的重复没有意义，也就没有清理/损坏恢复要管。
  · ⚠️ **只记交付过的**条目：被淘汰的可能是本轮排名靠后而非没价值，
    记进来就没有回归可能。
"""
from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from . import filters


# ========== L1 判据：规范化 URL ==========

# 只剥**无歧义**的点击/归因参数（对齐 CLI 的 minimal 档）。
# ⚠️ 刻意不含 spm / from / ref / share_token 等：它们在个别站点有语义
#    （`?from=chapter2` 真能指到不同章节），而跨轮剔除是**不可逆**的 ——
#    口径是"宁放过不杀错"。
_TRACKING_MINIMAL = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_social", "utm_brand",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid", "srsltid",
    "mc_cid", "mc_eid", "igshid", "wbraid", "gbraid",
    "_ga", "_gl", "vero_id", "wickedid",
})

_RE_HEX_ESC = re.compile(r"%([0-9a-fA-F]{2})")
# 路径里原样保留的 ASCII 标点。必须含 `%` —— 否则 `%2F` 会被二次编码成 `%252F`。
_PATH_SAFE = string.punctuation


def _upper_hex(s: str) -> str:
    """把 `%xx` 统一成 `%XX`（`%e7` 与 `%E7` 是同一页）；非合法转义的裸 `%` 原样保留。"""
    return _RE_HEX_ESC.sub(lambda m: "%" + m.group(1).upper(), s)


def _esc_path(path: str) -> str:
    """路径 → 可比形态：非 ASCII 按 UTF-8 百分号编码，ASCII 标点原样。

    刻意**不做** unquote→quote 往返：那会把 `%2F`（编码的分隔符）误解成层级，
    让 `/a%2Fb` 与 `/a/b` 错并成一条。
    """
    return _upper_hex(quote(path, safe=_PATH_SAFE))


def _norm_query(raw_query: str) -> str:
    """query → 剥归因参数 + 解码后按 (键, 值) 排序再编码。

    于是 `?write` 与 `?write=`、`?q=a+b` 与 `?q=a%20b` 归一后相等。
    """
    if not raw_query:
        return ""
    pairs = [
        (k, v) for k, v in parse_qsl(raw_query, keep_blank_values=True)
        if k.lower() not in _TRACKING_MINIMAL
    ]
    pairs.sort()
    return urlencode(pairs)


def norm_url(raw: str) -> str:
    """URL → 用于比对的规范化形，**不含 scheme**（http/https 是同一页的两种入口）。

    解析失败 / 无 host → 退化为整串小写：仍要求精确相等才合并，不会误杀。
    判据移植自 CLI 的 NormURL，改这里要同步它。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        u = urlparse(s)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        # 畸形端口等 —— urlparse 只在访问 .port 时抛
        return s.lower()
    if not host:
        return s.lower()
    scheme = (u.scheme or "").lower()
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None                     # 与 scheme 等价的默认端口：剥掉
    if port:
        host = f"{host}:{port}"
    path = _esc_path(u.path or "") or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    q = _norm_query(u.query or "")
    return f"{host}{path}?{q}" if q else f"{host}{path}"


# ========== L3 判据：正文相似度 ==========

def grams(text: str) -> set:
    """字符 3-gram（先去全部空白）。全仓唯一实现：同批去重与跨轮比对共用。"""
    t = "".join((text or "").split())
    if len(t) < 3:
        return {t} if t else set()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def containment(a: set, b: set) -> float:
    """**包含度** |A∩B| / min(|A|,|B|)；任一为空 → 0。

    不用 Jaccard/覆盖率：要抓的是"同源转载各加一段导语"——共享前缀全同、
    尾巴各不同，包含度看共享那一侧，覆盖率被尾巴稀释。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / min(len(a), len(b)) if inter else 0.0


# ========== L4 判据：simhash 正文指纹（V10.3） ==========

# 参与指纹的正文长度（字符，先去空白）。固定常量不经 config：账本里存过的
# 指纹（Entry.sh）与后续比对必须同一口径，配置可变会新旧错配。
_SH_SAMPLE = 2000
_SIMHASH_BITS = 64


def simhash64(text: str) -> int:
    """正文 → 64-bit simhash 指纹（字符 3-gram 为特征，md5 截 64 位，频次加权）。

    与 3-gram 包含度互补：包含度取样在正文前缀（512 字符），镜像站改写
    前缀/导语就判不出；simhash 看全段词汇分布，前缀改写不敏感。
    返回 0 = 无有效指纹（调用方必须把 0 当"没有指纹"处理，不得参与比对）。
    """
    t = "".join((text or "").split())[:_SH_SAMPLE]
    if len(t) < 3:
        return 0
    counts: Dict[str, int] = {}
    for i in range(len(t) - 2):
        g = t[i:i + 3]
        counts[g] = counts.get(g, 0) + 1
    v = [0] * _SIMHASH_BITS
    for g, w in counts.items():
        h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:8], "big")
        for b in range(_SIMHASH_BITS):
            v[b] += w if (h >> b) & 1 else -w
    out = 0
    for b in range(_SIMHASH_BITS):
        if v[b] > 0:
            out |= 1 << b
    return out


def hamming(a: int, b: int) -> int:
    """两个指纹的汉明距离（0 = 全同）。"""
    return bin((a ^ b) & ((1 << _SIMHASH_BITS) - 1)).count("1")


# ========== L5 判据：多窗口包含度（V10.3） ==========
# 实测教训：单窗口（前 512 字）包含度对"镜像站截掉原文开头再挂自己导语"全盲；
# simhash 对这类也不敏感（截断错位扰动全部 3-gram，64-bit 汉明距离拉到 20+）。
# 镜像的本质是**内容子串包含**——把比对改成多窗口取最大包含度，只要镜像保留
# 了原文任一整段窗口，就必然有一对窗口 containment 冲上阈值。
_GRAM_SLICE = 512
_GRAM_SLICES = 4      # 前 4 × 512 = 2048 字符，4 个不滑窗的切片


def grams_slices(text: str) -> list:
    """正文 → 多个切片的 3-gram 集合列表（供 containment_multi 比对）。"""
    t = "".join((text or "").split())
    out = []
    for k in range(_GRAM_SLICES):
        seg = t[k * _GRAM_SLICE:(k + 1) * _GRAM_SLICE]
        if seg:
            out.append(grams(seg))
    return out or [set()]


def containment_multi(a_list: list, b_list: list) -> float:
    """两组切片的**最大**包含度：任一对窗口重合即判出（镜像子串包含）。"""
    best = 0.0
    for ga in a_list:
        if not ga:
            continue
        for gb in b_list:
            s = containment(ga, gb)
            if s > best:
                best = s
                if best >= 1.0:
                    break
    return best


# ========== 账本 ==========

# 交付正文的内存预算（字符）。约 60KB/轮（15 条 × 4KB）→ 25 轮。超了从最老那轮
# 整轮清掉正文；元数据一律保留（见 _evict_bodies）。
_BODY_BUDGET_CHARS = 1_500_000


@dataclass
class Entry:
    """账本里的一条来源（跨轮唯一，按规范化 URL 索引）。"""
    url: str                    # 交付时的原始 URL（报告用）
    nurl: str                   # norm_url 后的比对形
    round_no: int               # 首次交付发生在第几轮
    body: str = ""              # 仅交付过的网页有；PDF 留空（原文件在 temp/pdf）
    sh: int = 0                 # 正文的 simhash 指纹（0 = 无；服务跨轮 L4 比对）


@dataclass
class _Round:
    """一轮检索交付了什么（只用于整轮淘汰正文）。"""
    no: int
    nurls: List[str] = field(default_factory=list)
    body_chars: int = 0


_BY_NURL: Dict[str, Entry] = {}
_ROUNDS: List[_Round] = []


def filter_candidates(urls: List[str]) -> Tuple[List[str], List[Dict]]:
    """L1：剔掉本会话已经交付过的候选。

    返回 `(要抓的, 被剔的)`；被剔条目带首次交付的轮次（日志用）。
    不因"新的凑不满名额"而回填：重复来源没有交付价值，抓取窗口自然后滑。
    """
    kept: List[str] = []
    dropped: List[Dict] = []
    for u in urls:
        nurl = norm_url(u)
        e = _BY_NURL.get(nurl) if nurl else None
        if e is None:
            kept.append(u)
        else:
            dropped.append({"url": u, "round": e.round_no})
    return kept, dropped


def filter_bodies(items: List[Dict], limit: int, threshold: float,
                  sh_threshold: int = 0) -> Tuple[List[Dict], List[Dict]]:
    """L3+L4+L5：抓回的正文与**账本里交付过的正文**比相似度，超阈值即剔。

    L3 = 单窗口 3-gram 包含度（> threshold，前缀同文）；L4 = simhash 汉明距离
    （≤ sh_threshold，整篇复用）；L5 = 多窗口最大包含度（镜像子串包含——前缀
    被改写的转载）。三者 OR 组合；sh_threshold=0 时 L4 关闭。
    判据与同批去重同一套，同一份数据不能有两个答案。
    """
    bodies = [(e, grams(e.body[:limit]), grams_slices(e.body), e.sh)
              for e in _BY_NURL.values() if e.body]
    bodies = [t for t in bodies if t[1] or t[2] != [set()] or t[3]]
    if not bodies:
        return list(items), []
    kept: List[Dict] = []
    dropped: List[Dict] = []
    for it in items:
        raw = filters.strip_source_header(it.get("text") or "")
        g = grams(raw[:limit])
        gs = grams_slices(raw)
        sh = simhash64(raw) if sh_threshold > 0 else 0
        hit: Optional[Entry] = None
        best = 0.0
        how = ""
        for e, eg, egs, esh in bodies:
            s = containment(g, eg) if (g and eg) else 0.0
            ms = containment_multi(gs, egs) if gs != [set()] and egs != [set()] else 0.0
            if ms > s:
                s = ms
            if s > best:
                hit, best, how = e, s, "gram"
            if (sh and esh and hit is None
                    and hamming(sh, esh) <= sh_threshold):
                hit, best, how = e, 1.0, f"simhash(d={hamming(sh, esh)})"
        if hit is not None and (best > threshold or how.startswith("simhash")):
            dropped.append({
                "url": it.get("url") or "", "sim": round(best, 3),
                "dup_of": hit.url, "how": how,
            })
        else:
            kept.append(it)
    return kept, dropped


def record(items: Iterable[Dict], pdf_urls: Iterable[str] = ()) -> _Round:
    """把本轮**实际交付**的条目写进账本。

    items：交付出去的条目（`{url, text}`）。
    pdf_urls：PDF 附件的原始地址（只记 URL；原文件已在 temp/pdf，正文不重复存）。
    """
    rnd = _Round(no=len(_ROUNDS) + 1)
    for it in items or []:
        _put(it.get("url") or "", it.get("text") or "", rnd)
    for u in pdf_urls or ():
        _put(u, "", rnd)
    _ROUNDS.append(rnd)
    _evict_bodies()
    return rnd


def _put(url: str, body: str, rnd: _Round) -> None:
    nurl = norm_url(url)
    if not nurl:
        return
    e = _BY_NURL.get(nurl)
    if e is None:
        e = Entry(url=url, nurl=nurl, round_no=rnd.no)
        _BY_NURL[nurl] = e
        rnd.nurls.append(nurl)
    if len(body) > len(e.body):
        # 同一 URL 拿到更长的正文（理论上不会再出现：它下轮会被 L1 剔掉）
        rnd.body_chars += len(body) - len(e.body)
        e.body = body
        # 指纹随正文更新（strip_source_header 与 L3 比对口径一致：元数据不参与）
        try:
            e.sh = simhash64(filters.strip_source_header(body))
        except Exception:
            e.sh = 0


def _evict_bodies() -> None:
    """正文超预算 → 从**最老那轮**整轮清掉正文。

    元数据一律不清：URL 级记忆必须跨全部轮次，否则第 3 轮会重新交付第 1 轮的来源。
    """
    total = sum(r.body_chars for r in _ROUNDS)
    while total > _BODY_BUDGET_CHARS and len(_ROUNDS) > 1:
        old = _ROUNDS.pop(0)
        for nurl in old.nurls:
            e = _BY_NURL.get(nurl)
            if e is not None and e.body:
                e.body = ""
        total -= old.body_chars


def reset() -> None:
    """清空账本（测试用；进程重启等价于此）。"""
    _BY_NURL.clear()
    _ROUNDS.clear()
