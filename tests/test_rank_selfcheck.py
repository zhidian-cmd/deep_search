# -*- coding: utf-8 -*-
"""ranking_meta 排序器最小自检（V9.6：从 rank.py 的 __main__ 迁入 tests/）。

原来 `rank.py` 里带一段 `_self_check()` + `__main__` 打印器 —— 那是**开发期脚手架**，
却随包发到了生产环境（导入即带 `sys.argv` 分支与 `print` 输出，还多一个 `import sys`）。
职责上它属于测试，故整体迁到这里；rank.py 侧对应删除。

用法：
    python tests/test_rank_selfcheck.py                      # 跑结构保证自检
    python tests/test_rank_selfcheck.py <results.json>       # 打印排序结果

⚠️ json 必须是 **CLI 输出形态**（`{"results":[…], "query":"…"}` 或裸列表），条目需带
`url` / `title` / `snippet` / `positions` —— merge 只做 URL 去重、不再做形态归一
（原先那套"逐引擎 jsonl"转换已删，见 rank.py::merge）。
"""
import json
import sys

from _harness import RANK_META   # 顺带完成路径/编码前奏

if RANK_META not in sys.path:
    sys.path.insert(0, RANK_META)

from rank import rank  # noqa: E402  （按路径直导，与 search.py 的加载方式一致）


def self_check() -> None:
    """最小自检：结构保证 —— 带价格/壳页特征的条目必须排在干净条目之后。"""
    raw = [
        {"engine": ["exa"], "positions": {"exa": 1}, "title": "量子计算进展权威综述",
         "date": "2026-01-01", "url": "https://a.example/x", "snippet": "量子计算" * 60},
        {"engine": ["serpapi"], "positions": {"serpapi": 1},
         "title": "量子计算进展 ¥199 元起 立即下单", "date": "",
         "url": "https://b.shop.example/product/1", "snippet": "量子计算" * 20},
        {"engine": ["bing"], "positions": {"bing": 20}, "title": "量子计算", "date": "",
         "url": "https://c.example/y", "snippet": "量子计算" * 2},
    ]
    out = rank(raw, "量子计算进展")
    good, dirty = out[0], out[1:]
    assert good["url"] == "https://a.example/x", good
    assert good["score"] > max(d["score"] for d in dirty), out
    print("self-check ok:", [(o["url"].split("/")[2], o["score"]) for o in out])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        self_check()
    else:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data["results"] if isinstance(data, dict) else data
        query = data.get("query", "") if isinstance(data, dict) else ""
        for i, it in enumerate(rank(rows, query), 1):
            print(f"{i:3d} {it['score']:+.4f} {it['title'][:56]}")
