"""Deep Search MCP Server V10.0 — FastMCP 入口（纯模块导入，无子进程）

自包含单一入口：宿主只需注册这一个 MCP。
search_engine/（多源搜索 + ranking_meta 学习排序）与 web_fetch.py（网页抓取）
全部为同进程 Python 模块导入，不拉起任何子 MCP 进程（CLI 只是 subprocess 调用）。
"""

import os
import sys
from typing import Any, Dict, Optional

# ===== 路径管理 =====
# 把 MCP 父目录加入 sys.path，使 `import deep_search` 及其子包能按绝对包名解析。
_MCP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MCP_ROOT not in sys.path:
    sys.path.insert(0, _MCP_ROOT)

from mcp.server.fastmcp import FastMCP

from deep_search.skill import DeepSearchSkill
from deep_search.config import DeepSearchConfig
from deep_search import helpers

# 日志装配统一走 helpers.setup_logger（StreamHandler + LOG_DIR 文件落盘，handler 去重）
logger = helpers.setup_logger("deep_search_mcp")


# skill 模块级单例（首次调用时惰性创建并复用），进程内多次 MCP 调用共享。
_skill: Optional[DeepSearchSkill] = None


def _get_skill() -> DeepSearchSkill:
    global _skill
    if _skill is None:
        _skill = DeepSearchSkill(config=DeepSearchConfig())
        logger.info("deep_search skill initialized (no sub-processes, pure module imports)")
    return _skill


mcp = FastMCP("deep_search")


_SHORT_URL_LIMIT = 58      # 来源清单逐条渲染，超长省略中后段


def _short_url(url: str) -> str:
    """紧凑化 URL（去 scheme 与 www.，超长省略中后段）。"""
    s = (url or "").strip()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
            break
    if s.startswith("www."):
        s = s[4:]
    return s if len(s) <= _SHORT_URL_LIMIT else s[: _SHORT_URL_LIMIT - 1] + "…"


def _render_sources(sources: list) -> str:
    """渲染 per-source 判据清单（编号与正文 `## 来源:` 小节一致）。

    本工具返回的是**字符串**，metadata 里的结构化字段若不渲染出来就等同于不存在
    —— truncated 这类判据必须落到文本里，调用方才能识别正文不完整的来源。
    清单里不可能出现"本会话已经给过"的来源：那些在候选阶段就被
    ledger.filter_candidates 剔掉了，条数记在返回头的 `skipped_repeats=N`。
    """
    if not sources:
        return ""
    lines = [
        "Sources (逐条来源判据；score=正文质量分 0~1；"
        "date=来源发布日期，—=该来源未提供，不得据此推断):"
    ]
    for s in sources:
        flags = []
        if s.get("truncated"):
            flags.append("truncated")
        flag = ("  [" + ", ".join(flags) + "]") if flags else ""
        date = s.get("date") or "—"
        lines.append(
            f"  {s.get('n')}. {date:<10} {s.get('score')}  "
            f"{_short_url(s.get('url') or '')}{flag}"
        )
    return "\n".join(lines) + "\n\n"


def _render_result(result: dict) -> str:
    """把 execute 的结果渲染成返回字符串：统计行 → Sources 清单 → 各来源正文。

    抽成模块级函数（V10.2）：pdf_dumps 计数需要测试，inline 在工具函数里够不着。
    """
    if result.get("success"):
        answer = result.get("answer", "")
        meta = result.get("metadata", {})
        prefix = []
        # ⚠️ source 在顶层 result["source"]，不在 metadata 里（那里从来没有这个键）。
        source = result.get("source", "")
        if source:
            prefix.append(f"Source: {source}")
        if meta.get("fetched_count") is not None:
            prefix.append(f"fetched={meta['fetched_count']}")
        # 语义去重：抓回条数 → 独立条数（差额是同源转载被合并掉的）。
        sem = meta.get("semantic_dedup") or {}
        if sem.get("before") is not None and sem.get("after") is not None:
            prefix.append(f"dedup {sem['before']}→{sem['after']}")
        # 截断：调用方须据以标注"该来源不得当完整证据引用"。
        if meta.get("truncated"):
            prefix.append(f"truncated=yes(原文 {meta.get('original_length')} 字符)")
        # 归档路径指针：agent 需要完整未截断全文时读它
        archive_path = meta.get("archive_path")
        if archive_path:
            prefix.append(f"Archive: {archive_path}")
        # 本会话已给过、这次直接剔掉的来源数：不在清单与正文里，不报一句
        # 调用方会以为"这次就这些"；极端情况候选全给过 → fetched=0，看它才知道原因。
        skipped = meta.get("skipped_repeats") or []
        if skipped:
            prefix.append(f"skipped_repeats={len(skipped)}(本会话已给过，正文见前几次归档)")
        # 抓回之后才判出的跨轮重复：URL 不同、正文同一篇（跨站转载）。
        cross = meta.get("cross_round_dropped") or []
        if cross:
            prefix.append(f"cross_round={len(cross)}(与已给过来源正文雷同)")
        # PDF 落盘计数（V10.2）：落盘支链的正文不进结果，只有这个数能让调用方
        # 知道"有附件存在"——实测漏看时 agent 会把标准原文级的材料整个丢掉。
        pdfs = meta.get("pdf_sources") or []
        if pdfs:
            prefix.append(f"pdf_dumps={len(pdfs)}(正文未返回；附件节与原文路径见归档)")
        head = (" · ".join(prefix) + "\n\n") if prefix else ""
        # 逐条来源清单（含截断标记）：先给判据再给内容
        return head + _render_sources(meta.get("sources") or []) + answer

    meta = result.get("metadata", {}) or {}
    return "Search failed: " + (
        meta.get("error") or meta.get("warning") or "unknown error"
    )


@mcp.tool()
async def deep_search(
    query: str,
    interactions: Optional[list] = None,
) -> str:
    """深度搜索：search_engine 多源聚合搜索（Go CLI 聚合 + ranking_meta 打分）+ Scrapling 网页抓取。

    检索结果**始终**落盘一份 markdown 归档（deep_search/temp/md），路径见返回头。
    响应命中 PDF 即原文件落盘 temp/pdf/（正文不进结果）：返回头 `pdf_dumps=N`
    提示数量，附件清单与原文路径在归档的「PDF 附件」节，要正文直接读 `path`。
    签名刻意收窄：没有 time_range / engine / max_results / save_archive 参数 ——
    它们是绕过流水线的旁路开关（engine 单跑曾把候选池从约 96 条塌到 6 条），
    留在 schema 里会制造"调用方能控制它"的错觉，均已从签名移除。
    引擎集合由 search_engine 依据 CLI 自己已配的 API Key 决定，无需调用方介入。

    Args:
        query: 检索关键词（不要传整句，先精分为 2-8 个核心词）
        interactions: 交互式抓取指令列表，如
            [{"type": "scroll", "times": 3}, {"type": "wait", "ms": 1500}]
            （适用于 JS 懒加载页面；静态抓取内容不足时会自动升级浏览器+滚动）
    """
    skill = _get_skill()

    kwargs: Dict[str, Any] = {}
    if interactions:
        kwargs["interactions"] = list(interactions)

    try:
        result = await skill.execute(query=query, **kwargs)
    except Exception as e:
        logger.exception("deep_search failed: %s", e)
        return f"Search failed: {type(e).__name__}: {e}"

    if not isinstance(result, dict):
        return str(result)

    return _render_result(result)


if __name__ == "__main__":
    # 无参启动：MCP 宿主只走 stdio，传输/主机/端口与日志级别没有可调项。
    mcp.run()
