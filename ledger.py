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


def filter_bodies(items: List[Dict], limit: int, threshold: float) -> Tuple[List[Dict], List[Dict]]:
    """L3：抓回的正文与**账本里交付过的正文**比包含度，超阈值即剔（跨站转载）。

    只与正文比（账本里没正文的条目只剩 L1 那层记忆）。
    判据与阈值和同批去重同一套，同一份数据不能有两个答案。
    """
    bodies = [(e, grams(e.body[:limit])) for e in _BY_NURL.values() if e.body]
    bodies = [(e, g) for e, g in bodies if g]
    if not bodies:
        return list(items), []
    kept: List[Dict] = []
    dropped: List[Dict] = []
    for it in items:
        g = grams(filters.strip_source_header(it.get("text") or "")[:limit])
        hit: Optional[Entry] = None
        best = 0.0
        for e, eg in bodies:
            s = containment(g, eg)
            if s > best:
                hit, best = e, s
        if hit is not None and best > threshold:
            dropped.append({
                "url": it.get("url") or "", "sim": round(best, 3), "dup_of": hit.url,
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
