# -*- coding: utf-8 -*-
"""跨领域样本采集器（V2）：11 个 query 走生产流水线，产出标注底料 JSONL。

用法：python _live_collect.py <起始序号>
从 QUERIES[起始序号-1] 开始跑（1-based），续跑时跳过已有 query。
"""
import asyncio
import json
import re
import sys
import time

sys.path.insert(0, r"D:/Runtimelibrary/SERVER/MCP")
from deep_search.skill import DeepSearchSkill
from deep_search.config import DeepSearchConfig

QUERIES = [
    "白酒 食品安全国家标准 蒸馏酒 GB",
    "微生物 检验 培养基 高压灭菌锅",
    "固态电池 电解质 量产 工艺",          # 上一轮已跑，续跑会跳过
    "量子计算 纠错 量子比特",
    "行政处罚 听证 程序 时限",
    "2型糖尿病 降糖药 指南 二甲双胍",
    "大模型 LoRA 微调 显存",
    "MySQL 索引 优化 慢查询",
    "空气净化器 CADR 选购 参数",
    "光伏 逆变器 MPPT 效率",
    "跨境电商 出口 报关 流程",
    "高血压 降压药 联合用药",
]

OUT = r"D:/Runtimelibrary/SERVER/MCP/deep_search/tests/_eval_live.jsonl"


async def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sk = DeepSearchSkill(DeepSearchConfig())
    t0 = time.time()
    for i, q in enumerate(QUERIES[start - 1:], start=start):
        try:
            r = await sk.execute(q)
            meta = r.get("metadata", {})
            parts = re.split(r'^##\s*来源:\s*(\S+)\s*$', r.get("answer") or "", flags=re.M)
            bodies = {parts[j]: parts[j + 1] for j in range(1, len(parts) - 1, 2)}
            with open(OUT, "a", encoding="utf-8") as out:
                for s in meta.get("sources") or []:
                    url = s.get("url") or ""
                    rec = {
                        "query": q, "url": url, "form": s.get("score"),
                        "authority": s.get("authority"),
                        "coverage": s.get("coverage"),
                        "len": len(bodies.get(url, "")),
                        "text": bodies.get(url, "")[:2500],
                    }
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{i}/12] {q} -> {len(meta.get('sources') or [])} sources "
                  f"(pdf={len(meta.get('pdf_sources') or [])}) elapsed={time.time()-t0:.0f}s",
                  flush=True)
        except Exception as e:
            print(f"[{i}/12] {q} FAILED: {type(e).__name__}: {e}", flush=True)
    print("DONE", flush=True)


asyncio.run(main())
