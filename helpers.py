"""通用辅助函数：URL 归一化、发布日期归一化、统一 logger 装配、段落重组。

纯函数 + 标准库，无第三方依赖（logger 落盘路径取自 config.LOG_DIR）。
"""
import logging
import os
import re
from datetime import date, timedelta
from typing import List
from urllib.parse import urlparse

from .config import LOG_DIR


def setup_logger(name: str) -> logging.Logger:
    """统一 logger 装配：StreamHandler + LOG_DIR 文件 Handler（同名 handler 去重）。

    级别固定 INFO：全仓没有一处 logger.debug()，"调成 DEBUG" 是空操作。
    """
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    if not lg.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        lg.addHandler(ch)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, "deep_search.log")
        if not any(getattr(h, "baseFilename", "") == log_path for h in lg.handlers):
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
    except Exception:
        pass  # 日志落盘失败不应影响主流程
    return lg


def normalize_urls(urls: List[str]) -> List[str]:
    """去重 + 仅保留 http/https（防 SSRF 基础）。保持首次出现的顺序。"""
    return [u for u in dict.fromkeys(urls) if urlparse(u).scheme in ("http", "https")]


def host_of(url: str) -> str:
    """URL → 规范化主机名：小写 + 去 `www.`；解析不出返回空串（不抛异常）。

    全仓唯一口径（此前这段表达式在多处手抄，漏改一处就是"同站被判成两个域"）。
    刻意**不做**的两处（不是漏改）：url_blacklisted 要**原始** host（黑名单条目
    带 www）；filters_learn._norm 的入参可能是裸域名，另有原串兜底语义。
    """
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


# Windows 文件名非法字符与控制符。文本里出现冒号很常见（`GB 4789.1:2016`），
# 不清洗会让落盘直接失败。
_RE_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(text: str, limit: int = 50) -> str:
    """文本 → 可作文件名的主体：非法字符换 `-`，空白归一成一个空格，截 limit 字。

    空白保留成空格：`GB 4789.1 采样方案` 比 `GB_4789.1_采样方案` 更像它本来的样子。
    句点/空格结尾在 Windows 上非法，故剥掉。返回空串 = 挑不出可用名，
    兜底名由调用方决定（归档用 `query`、PDF 落盘用 `pdf`）。
    归档与 PDF 落盘共用这一份实现。
    """
    name = _RE_ILLEGAL_FILENAME.sub("-", (text or "").strip())
    return re.sub(r"\s+", " ", name).strip(" .")[:limit]


# ---------- 段落重组（归档落盘用） ----------
# 段落宽度上限。不是可调开关 —— 要改宽度改这一个数。
_PARA_LIMIT = 120
# "上一行没写句末标点"就是源站把一句话硬折断了，要接回上一行。
# 但短行不构成硬折证据 —— "3.2 采样方案"这类小标题同样不带标点。
_CONTINUATION_MIN = 30
# 句末标点：分号也算 —— 标准条文里"…；…"全靠分号分句
_SENTENCE_ENDS = "。！？；!?;"


def reflow_paragraphs(text: str) -> str:
    """重组段落，让 markdown **渲染后**也是分段落的。**只动空白。**

    markdown 里单个换行渲染时会合并成同一段，归档正文的换行完全取决于原网页
    结构。规则（全是机械规则，不需要人判断）：
      1. 源站硬折的续行接回上一行 —— 判据是"上一物理行不以句末标点结尾且不短
         于 _CONTINUATION_MIN"；
      2. 一句话结束 → 一段，段后插空行；
      3. 一段超过 _PARA_LIMIT → 按句末标点切（整段无标点才按字符硬折）。

    **不变量**：折行前后把空白全去掉再比，必须一字不差（tests/test_archive.py 锁死）。
    不动的行：表格、代码围栏及其内部、含 markdown 链接的行、标题与列表 ——
    在它们内部插空行/换行会破坏结构。
    """
    out: List[str] = []
    buf = ""
    prev = ""            # 上一物理行（判断它是不是被硬折开的半句话）
    in_fence = False
    last_kind = ""       # 上一输出行的类型（同类列表/表格行才允许贴着，见 _push）

    def _push(line: str, kind: str) -> None:
        """输出一行。相邻行之间插空行（= 渲染时分成两段），只有同类的列表项
        与表格行允许贴着 —— 它们之间插空行会把表格拆散、把列表变成松列表。"""
        nonlocal last_kind
        if out and out[-1] != "" and not (kind == last_kind and kind in ("list", "table")):
            out.append("")
        out.append(line)
        last_kind = kind

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        for chunk in _split_paragraph(buf):
            _push(chunk, "text")
        buf = ""

    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith("```"):
            flush()
            in_fence = not in_fence
            _push(raw, "fence")
            prev = ""
            continue
        if in_fence:
            out.append(raw)
            continue
        if not s:
            flush()
            if out and out[-1] != "":
                out.append("")
            prev = ""
            continue
        if _is_atomic(raw):
            flush()
            _push(raw, _kind(raw))
            prev = s
            continue
        # 续行判定：上一行没写句末标点、又不短 → 它被硬折了，接回去
        if buf and len(prev) >= _CONTINUATION_MIN and not _at_sentence_end(prev, len(prev) - 1):
            buf = _join(buf, s)
        else:
            flush()
            buf = s
        prev = s
        if _at_sentence_end(s, len(s) - 1):
            flush()
    flush()
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _kind(line: str) -> str:
    """输出行的类型：只有连续的 list / table 行允许之间不插空行。"""
    s = line.strip()
    if line[:1] in (" ", "\t") or s[:2] in ("- ", "* ", "+ "):
        return "list"
    if s.startswith("|"):
        return "table"
    return "text"


def _is_atomic(line: str) -> bool:
    """结构行：不参与接续、不参与折行，原样保留。"""
    s = line.strip()
    return (
        line[:1] in (" ", "\t")                       # 缩进行（多层列表/延续）
        or s.startswith(("#", "|", ">", "---"))       # 标题 / 表格 / 引用 / 分割线
        or s[:2] in ("- ", "* ", "+ ")                # 列表项
        or "](http" in s                              # 含链接：折了会把链接劈开
    )


def _at_sentence_end(s: str, i: int) -> bool:
    """s[i] 是否句末标点。英文句点只在**后跟空白**时算句末，否则
    `GB 4789.1—2016`、`resPdfShow.do` 会被当成句子断掉。"""
    ch = s[i]
    if ch in _SENTENCE_ENDS:
        return True
    return ch in ".!?" and (i + 1 >= len(s) or s[i + 1].isspace())


def _join(a: str, b: str) -> str:
    """接回被硬折的两行。英文单词之间补一个空格（否则 `the`+`end` 粘成
    `theend`），中文直接相接。"""
    if a[-1].isascii() and not a[-1].isspace() and b[:1].isascii() and b[:1] and not b[0].isspace():
        return a + " " + b
    return a + b


def _split_paragraph(p: str) -> List[str]:
    """一段 → 渲染时的若干段：按句末标点累积到 _PARA_LIMIT 才切；无标点的整段
    按字符数硬折。"""
    if "](http" in p:                  # 含链接的长段不切，免得把链接劈成两半
        return [p]
    chunks: List[str] = []
    buf = ""
    for i in range(len(p)):
        buf += p[i]
        if _at_sentence_end(p, i) and len(buf) >= _PARA_LIMIT:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)

    out: List[str] = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        # 硬折上限：整段无句末标点时折到 _PARA_LIMIT；有句子的段放到 2× 才动手
        # —— 宁可行长，也不在一句话中间切。
        hard = _PARA_LIMIT if not any(ch in _SENTENCE_ENDS for ch in c) else _PARA_LIMIT * 2
        while len(c) > hard:
            out.append(c[:hard])
            c = c[hard:]
        out.append(c)
    return out


# ---------- 发布日期归一化 ----------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH_DATE = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})")
_CN_DATE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")
_EN_DATE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_REL_AGO = re.compile(r"^(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago$", re.I)
# 相对时间倒推用的近似天数（够做月/年级别的时效判断）
_REL_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _safe_date(year: int, month: int, day: int) -> str:
    """构造 ISO 日期串；非法组合（如 2 月 30 日）返回空串而不是抛异常。"""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def normalize_date(raw: str) -> str:
    """把各引擎的日期文本归一成 ISO 日期（YYYY-MM-DD）；识别不了返回空串。

    各引擎格式互不相同：exa 是 RFC3339、metaso 是「2026年01月01日」、
    serpapi 可能是「2026年1月8日 / Jan 8, 2026 / 3 months ago」。
    ⚠️ 相对时间只能按 30 天/月、365 天/年倒推，精度约 ±数天。
    **返回空串 = 该来源未提供日期或无法识别，调用方不得据此推断发布日期。**
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    m = _ISO_DATE.match(s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _SLASH_DATE.match(s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _CN_DATE.match(s)
    if m:
        # 「2026年3月」无日 → 按当月 1 日
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    m = _EN_DATE.match(s)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            return _safe_date(int(m.group(3)), mon, int(m.group(2)))
    m = _REL_AGO.match(s)
    if m:
        days = int(m.group(1)) * _REL_UNIT_DAYS[m.group(2).lower()]
        return (date.today() - timedelta(days=days)).isoformat()
    return ""
