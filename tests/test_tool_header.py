# -*- coding: utf-8 -*-
"""MCP 返回头渲染（`server._render_result`）的离线测试（V10.2）。

锁三件事：
  ① `pdf_dumps=N` 只在 `metadata.pdf_sources` 非空时出现——落盘支链的正文不进
     结果，这个计数是调用方知道"有附件存在"的唯一线索（实测漏看 = 标准原文级
     材料整份丢掉）；
  ② 既有统计行字段一个不少、顺序稳定（pdf_dumps 排在尾部，追加字段不破坏旧读法）；
  ③ 失败路径照旧走 `Search failed:`（error 优先于 warning）。

不触外网、不写生产 temp（纯函数断言）。
"""
import sys

from _harness import check, summary

from deep_search.server import _render_result


def _ok_result(**extra):
    meta = {
        "fetched_count": 15,
        "semantic_dedup": {"before": 22, "after": 15},
        "sources": [
            {"n": 1, "url": "https://a.example/x", "score": 0.9, "truncated": False, "date": "2026-03-18"},
        ],
    }
    meta.update(extra)
    return {"success": True, "answer": "## 来源: https://a.example/x\n\n正文",
            "source": "scrapling_filtered", "metadata": meta}


def main():
    print("== 1. pdf_dumps 只在 pdf_sources 非空时出现 ==")
    out = _render_result(_ok_result(
        pdf_sources=[{"url": "https://a.com/b.pdf", "path": "temp/pdf/x.pdf"}] * 2))
    check("2 份 PDF → 计数出现", "pdf_dumps=2" in out, [l for l in out.split("\n") if "pdf_dumps" in l])
    check("带指引语（附件节在归档）", "附件节与原文路径见归档" in out)
    out0 = _render_result(_ok_result(pdf_sources=[]))
    check("空清单 → 不出现（常态，不制造噪声）", "pdf_dumps" not in out0)
    out_none = _render_result(_ok_result())
    check("键缺失 → 不出现", "pdf_dumps" not in out_none)

    print("== 2. 既有字段一个不少，pdf_dumps 在尾部 ==")
    out = _render_result(_ok_result(
        truncated=True, original_length=78000,
        archive_path=r"D:\temp\md\01_测试.md",
        skipped_repeats=[{"url": "https://a.com/x", "round": 1}],
        cross_round_dropped=[{"url": "https://b.com/y", "dup_of": "https://a.com/x", "sim": 0.9}],
        pdf_sources=[{"url": "https://a.com/b.pdf", "path": "p"}],
    ))
    head = out.split("\n\n")[0]
    order = ["Source:", "fetched=15", "dedup 22→15", "truncated=yes",
             "Archive:", "skipped_repeats=1", "cross_round=1", "pdf_dumps=1"]
    pos = [head.find(s) for s in order]
    check("全部字段在统计行", all(p >= 0 for p in pos), head[:120])
    check("顺序稳定（追加字段在尾部）", pos == sorted(pos), pos)
    check("Sources 清单仍在正文前", "Sources (逐条来源判据" in out)

    print("== 3. 失败路径不变 ==")
    check("error 优先", _render_result({"success": False, "metadata": {"error": "E1", "warning": "W1"}})
          == "Search failed: E1")
    check("只有 warning 也渲染", _render_result({"success": False, "metadata": {"warning": "W2"}})
          == "Search failed: W2")

    return summary()


sys.exit(main())
