# search.py — 对外统一接口：搜索 + 排序
"""搜索 + 排序桥接层：
  * 搜索：metasearch_cli_windows_amd64.exe（Go 多引擎并发聚合 + URL 去重 + 可达性预检）
  * 排序：ranking_meta/ 统计学习排序器（域名 P(坏) 表 + 结构特征，1400 条人工标注
    拟合，6 折留出 AUC 0.765 / bad@10 0.183；被取代的旧 RRF 融合 AUC 仅 0.536 ——
    "多引擎命中加权"恰好把广告顶上去）

条目协议（对下游 skill.py）：engine / positions / url / title / snippet / date，
另附 score（pos − 1.6·ad − 1.15·bad）。score 为负 = 命中广告/无效证据，
必然排在所有干净条目之后（结构保证）。

密钥：CLI 用自己的密钥库（`... apikey list` 查看），引擎集合由已配 key 自动决定。
路径解析：CLI 走环境变量 METASEARCH_CLI → 本目录 bin/；排序模型 ranking_meta/model.json。
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台编码修复（⚠️ 进程级副作用：import 本模块即生效，skill 层知悉并依赖）
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

_HERE = Path(__file__).resolve().parent

# ---- CLI 路径解析（固定发布名，下载后原样放进 bin/ 即可，不必改名） ----
_BIN_DIR = _HERE / "bin"
CLI_NAME = "metasearch_cli_windows_amd64.exe"
_CLI_CANDIDATES = [
    os.environ.get("METASEARCH_CLI", ""),
    str(_BIN_DIR / CLI_NAME),
]
CLI_PATH = next((Path(p) for p in _CLI_CANDIDATES if p and Path(p).is_file()), None)

# ---- 排序器（ranking_meta/，自包含）----
_META_DIR = _HERE / "ranking_meta"
if str(_META_DIR) not in sys.path:
    sys.path.insert(0, str(_META_DIR))
try:
    from rank import rank as _meta_rank
except Exception as _exc:  # pragma: no cover
    _meta_rank = None
    logger.error("ranking_meta 不可用（%s）—— 排序将退化为 CLI 原样顺序", _exc)


def _cli_search(query: str, limit: int) -> list:
    """调 CLI 聚合搜索，stdout 即纯 JSON（实测确认，无前后杂行）。零临时文件。"""
    if CLI_PATH is None:
        raise RuntimeError(
            f"未找到搜索 CLI（找过 METASEARCH_CLI 环境变量，以及 {_BIN_DIR / CLI_NAME}）"
            f"—— 请从 Releases 下载 {CLI_NAME} 直接放进 {_BIN_DIR}（不必改名），"
            "或设置环境变量 METASEARCH_CLI 指向可执行文件"
        )
    r = subprocess.run(
        [str(CLI_PATH), query, "-limit", str(limit)],
        cwd=str(CLI_PATH.parent),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"metasearch_cli rc={r.returncode}: {r.stderr[:300]}")
    data = json.loads(r.stdout)
    return data["results"] if isinstance(data, dict) else data


def search(query: str, limit: int = 15) -> list:
    """对外接口：CLI 多引擎聚合 → ranking_meta 统计排序。

    参数：
        query: 搜索词
        limit: 每引擎最大结果数（下游抓取层自行做预算控制）
    返回：
        按 score 降序的条目列表（额外带 score 字段）。
    """
    t0 = time.monotonic()
    items = _cli_search(query, limit)

    # 排序：rank.py 自带 URL 合并 + 结构保证（广告/无效必沉底）
    if _meta_rank is not None and items:
        items = _meta_rank(items, query)
    elif items:
        # 极端降级：按 CLI 返回顺序（各引擎拼接序），保底可用
        logger.warning("排序器不可用，按 CLI 原样顺序返回")

    logger.warning("search(%r): %d 条, 耗时 %.2fs（CLI 聚合 + 排序）",
                   query, len(items), time.monotonic() - t0)
    return items
