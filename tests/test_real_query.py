# -*- coding: utf-8 -*-
"""真实 query 端到端验收（**需联网**）：归档 markdown 不得出现模板页/乱码页。

用法：python tests/test_real_query.py
会真的发一次检索（消耗引擎配额），故不进离线测试批次。
"""
import asyncio
import re
import sys

from _harness import check, summary

from deep_search.skill import DeepSearchSkill
from deep_search.config import DeepSearchConfig

QUERY = "2026年中秋节放假安排 调休"

# 归档里出现这些串 = 抓回来的是模板页/挑战页而非正文
BAD_MARKS = [
    ("Enable JavaScript", "模板:JS提示"),
    ("{_waf_", "模板:WAF token"),
    ("登录后查看", "模板:登录墙"),
    ("访问过于频繁", "模板:频控"),
    ("人机验证", "模板:验证"),
]


async def main():
    cfg = DeepSearchConfig()
    print("max_urls_to_try(生效额度池):", cfg.max_urls_to_try)
    skill = DeepSearchSkill(cfg)
    # 不传 max_urls_to_try —— 验收必须测**默认口径**（V9.7 起最终额度池 15）。
    # 显式传值会绕过 config，测出来的就不是线上行为。
    r = await skill.execute(QUERY)
    meta = r.get("metadata", {})
    print("success:", r.get("success"), "| source:", r.get("source"),
          "| elapsed: %.1fs" % meta.get("elapsed", 0))
    print("fetched_count:", meta.get("fetched_count"), "| truncated:", meta.get("truncated"))
    print("semantic_dedup:", meta.get("semantic_dedup"))
    # 降权/质量分明细不再随 metadata 返回（透出通道是**字符串**，这些字段到不了调用方，已删）。
    # 排查"这个站为什么排得低"直接读落盘文件：temp/logs/penalty_stats.json（快照）、
    # temp/logs/filter_learning.log（每次 delta 变化一行审计）。
    print("archive_path:", meta.get("archive_path"))
    err = meta.get("error") or meta.get("warning")
    if err:
        print("error/warning:", err)
    print("answer 长度:", len(r.get("answer", "")))
    print("answer 前 500 字:\n", (r.get("answer") or "")[:500])

    check("整轮 success=True", r.get("success") is True, f"error/warning={err!r}")

    archive_path = meta.get("archive_path")
    check("归档已落盘且给出了路径", bool(archive_path), repr(archive_path))
    if not archive_path:
        return summary()

    with open(archive_path, encoding="utf-8") as f:
        doc = f.read()

    print("\n== 归档验收 ==")
    m = re.search(r"抓取: (\d+) 条 \| 引用: (\d+) 条", doc)
    check("元信息行可解析", bool(m), "" if m else "没匹配到「抓取: N 条 | 引用: M 条」")
    if m:
        # 交付条数口径：候选充足时应等于最终额度池；候选不足则"不足全取"。
        # 两者都算正常，故只报数字、不判死。
        n_archived = int(m.group(2))
        note = "相等" if n_archived == cfg.max_urls_to_try else "候选不足（不足全取）"
        print(f"  max_urls_to_try={cfg.max_urls_to_try}  归档引用数={n_archived}  （{note}）")

    n_tag = len(re.findall(r"<[a-zA-Z/][^>]*>", doc))
    # 归档必须是 markdown：原先落 HTML，改存 markdown 后若有标签残留，
    # 说明某处又把这层转换加回来了。
    check("无 HTML 标签残留", n_tag == 0, f"标签数={n_tag}")

    hits = [(mk, lab) for mk, lab in BAD_MARKS if mk.lower() in doc.lower()]
    check("无模板页/乱码页标记", not hits,
          "命中: " + ", ".join(f"'{mk}'({lab})" for mk, lab in hits))
    return summary()


sys.exit(asyncio.run(main()))
