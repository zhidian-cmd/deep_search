# -*- coding: utf-8 -*-
"""排序 replay 门禁（V10.6）：fixture 快照 + 基线自锁。

数据集：
  - _eval_dirty_v1.json  脏批（2026-10-07 食品包装归档真实正文，34 条，
    含汤页/跑题/机翻/SEO 边缘样本，rel 人工标注）
  - _eval_dataset_v3.json 净批（20 领域 58 题 764 条，既有）
  两个数据集都只存 form/authority/coverage/rel/weak_core 数值——replay 离线
  可重复，不碰网络、不碰归档。

门禁（对 _rank_baseline.json 的已提交基线）：
  1. 快照锁：每题 multiplicative 排序的 top3 URL 序列与基线一致——
     未来任何改排序键的行为都必须显式 bless（`python tests/test_rank_replay.py bless`
     + CHANGELOG 说明），否则测试红。
  2. 聚合不回退：top1_rel / top5_rel / top5跑题数 不得劣于基线。
  3. 逐题不回退：每题 top1_rel 不得劣于基线（借鉴 free-search evals/ranking
     的"改进总量、不回退单题"规则）。
  4. 质量地板：脏批每题 top3 无 rel=0 条目、每题 top1_rel ≥ 0.5。
  5. 线性回退路径：V10.4 线性公式独立断言（乘性改动不得波及回退开关）。

用法：
    python tests/test_rank_replay.py            # 跑门禁
    python tests/test_rank_replay.py bless      # 重新生成基线（有意行为，须记 CHANGELOG）
"""
import json
import os
import sys

from _harness import check, summary

from deep_search import filters

_HERE = os.path.dirname(os.path.abspath(__file__))
DIRTY = os.path.join(_HERE, "_eval_dirty_v1.json")
CLEAN = os.path.join(_HERE, "_eval_dataset_v3.json")
BASELINE = os.path.join(_HERE, "_rank_baseline.json")

W = (0.50, 0.40, 0.10)  # V10.4 线性权重（回退分支用）


def _load(path):
    doc = json.load(open(path, encoding="utf-8"))
    items = doc["items"] if isinstance(doc, dict) else doc
    by_q = {}
    for it in items:
        d = dict(it)
        d["score"] = float(d["form"])
        d["authority"] = float(d["authority"])
        d["coverage"] = float(d["coverage"])
        d["rel"] = float(d["rel"])
        by_q.setdefault(d["query"], []).append(d)
    return by_q


def _rank_all(by_q, multiplicative=True):
    """→ {query: [top3 url]}, 聚合与逐题指标。"""
    top3, top1_rel, top5_rel, off_top5, per_q = {}, 0.0, 0.0, 0, {}
    for q, its in by_q.items():
        ranked = filters.rank_items([dict(x) for x in its],
                                    W[0], W[1], W[2],
                                    multiplicative=multiplicative)
        top3[q] = [x["url"] for x in ranked[:3]]
        r1 = ranked[0]["rel"]
        t5 = ranked[:5]
        top1_rel += r1
        top5_rel += sum(x["rel"] for x in t5) / len(t5)
        off_top5 += sum(1 for x in t5 if x["rel"] == 0.0)
        per_q[q] = {"top1_rel": r1}
    n = len(by_q)
    metrics = {"top1_rel": round(top1_rel / n, 4),
               "top5_rel": round(top5_rel / n, 4),
               "off_top5": off_top5}
    return top3, metrics, per_q


def _check_set(by_q, name):
    if not os.path.exists(BASELINE):
        print(f"FAIL  基线文件缺失：python tests/test_rank_replay.py bless 生成后重跑")
        return
    base = json.load(open(BASELINE, encoding="utf-8"))[name]
    top3, metrics, per_q = _rank_all(by_q)
    check(f"[{name}] 快照锁：top3 URL 序列", top3 == base["top3"],
          "" if top3 == base["top3"] else _first_diff(top3, base["top3"]))
    for k in ("top1_rel", "top5_rel"):
        check(f"[{name}] 聚合不回退 {k}: {metrics[k]} >= {base['metrics'][k]}",
              metrics[k] >= base["metrics"][k] - 1e-9)
    check(f"[{name}] 聚合不回退 off_top5: {metrics['off_top5']} <= {base['metrics']['off_top5']}",
          metrics["off_top5"] <= base["metrics"]["off_top5"])
    regressed = [q for q, m in per_q.items()
                 if m["top1_rel"] < base["per_q"].get(q, {}).get("top1_rel", 0) - 1e-9]
    check(f"[{name}] 逐题不回退", not regressed, str(regressed[:3]))


def _first_diff(a, b):
    for q in a:
        if a[q] != b.get(q):
            return f"{q[:24]}: {a[q][:2]} != {b.get(q, [])[:2]}"
    return "dict mismatch"


def test_dirty_floors(by_q):
    """质量地板：只锁当前已成立的行为（构建时逐项验证过）。"""
    for q, its in by_q.items():
        ranked = filters.rank_items([dict(x) for x in its])
        t3 = ranked[:3]
        check(f"[地板] top3 无跑题（rel=0）: {q[:24]}",
              all(x["rel"] != 0.0 for x in t3),
              str([(x["rel"], x["url"][:40]) for x in t3 if x["rel"] == 0.0]))
        check(f"[地板] top1_rel >= 0.5: {q[:24]}", ranked[0]["rel"] >= 0.5)


def test_linear_invariant():
    """线性回退分支独立断言（与乘性改动解耦）。"""
    seo = {"score": 1.0, "authority": 0.50, "coverage": 1.0}
    gov = {"score": 0.88, "authority": 0.95, "coverage": 0.7}
    rs_seo = W[0] * seo["score"] + W[1] * seo["authority"] + W[2] * seo["coverage"]
    rs_gov = W[0] * gov["score"] + W[1] * gov["authority"] + W[2] * gov["coverage"]
    check("线性：SEO 页 0.8 < 权威页 0.89（V10.4 语义不变）",
          abs(rs_seo - 0.8) < 1e-9 and abs(rs_gov - 0.89) < 1e-9,
          f"{rs_seo} / {rs_gov}")
    out = filters.rank_items([dict(seo, url="a"), dict(gov, url="b")],
                             W[0], W[1], W[2], multiplicative=False)
    check("线性：排序键为加权分", abs(out[0]["rank_score"] - rs_gov) < 1e-9)


def bless():
    by_q_dirty = _load(DIRTY)
    by_q_clean = _load(CLEAN)
    base = {}
    for name, by_q in (("dirty", by_q_dirty), ("v3", by_q_clean)):
        top3, metrics, per_q = _rank_all(by_q)
        base[name] = {"top3": top3, "metrics": metrics, "per_q": per_q}
    json.dump(base, open(BASELINE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    print(f"基线已写入 {BASELINE}")
    for name, m in base.items():
        print(f"  {name}: {m['metrics']}  ({len(m['top3'])} 题)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "bless":
        bless()
        sys.exit(0)
    _check_set(_load(DIRTY), "dirty")
    _check_set(_load(CLEAN), "v3")
    test_dirty_floors(_load(DIRTY))
    test_linear_invariant()
    sys.exit(summary("rank replay 门禁"))
