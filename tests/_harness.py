# -*- coding: utf-8 -*-
"""测试公共前奏 + 极简断言计数器。

七个测试文件此前各自手抄了同一段开头（插入仓库根到 sys.path、把 stdout 换成
UTF-8、定义 PASS/FAIL 与 check()），约 70 行重复。抽到这里后，各测试只需
`from _harness import check, summary`。

**import 本模块本身就有副作用**（sys.path 插入 + stdout 换编码），所以在测试文件里
它必须出现在 `from deep_search import ...` **之前**。不需要计数器的脚本写
`import _harness`（加 `# noqa: F401`）即可。

用法（各测试文件）：
    from _harness import check, summary      # 顺带完成路径/编码前奏

    check("等价于一个断言", 1 + 1 == 2)
    sys.exit(summary())                      # 打印 PASS/FAIL 汇总并以状态码收尾
"""
import io
import os
import sys

# .../deep_search/tests/_harness.py → 仓库根（含 deep_search 包的那一级）
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
PKG_DIR = os.path.join(ROOT, "deep_search")
RANK_META = os.path.join(PKG_DIR, "search_engine", "ranking_meta")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    """计一次断言；失败只记账不打断言，汇总时统一反映到退出码。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def summary(label: str = "结果") -> int:
    """打印汇总行，返回 FAIL 数（测试文件用 sys.exit(summary()) 收尾）。"""
    print(f"\n== {label}：PASS={PASS} FAIL={FAIL} ==")
    return 1 if FAIL else 0
