# -*- coding: utf-8 -*-
"""跨领域样本采集器（V3 大批量版）。

用法：python _live_collect_v3.py <query文件>
- query 文件：每行一个 query，# 开头为注释
- 断点续跑：跳过 JSONL 中已采集过的 query
- 每条结果 append 到 _eval_live.jsonl（与 V2 同一份，字段一致）
"""
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, r"D:/Runtimelibrary/SERVER/MCP")
from deep_search.skill import DeepSearchSkill
from deep_search.config import DeepSearchConfig

OUT = r"D:/Runtimelibrary/SERVER/MCP/deep_search/tests/_eval_live.jsonl"


def load_queries(path):
    qs = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            qs.append(line)
    return qs


def done_queries():
    if not os.path.exists(OUT):
        return set()
    done = set()
    with open(OUT, encoding="utf-8") as f:
        for line in f:
            try:
                done.add(json.loads(line).get("query"))
            except Exception:
                continue
    return done


async def main():
    qfile = sys.argv[1] if len(sys.argv) > 1 else ""
    queries = load_queries(qfile)
    done = done_queries()
    todo = [q for q in queries if q not in done]
    print(f"query 总数 {len(queries)}，已完成 {len(done & set(queries))}，本轮待跑 {len(todo)}", flush=True)
    sk = DeepSearchSkill(DeepSearchConfig())
    t0 = time.time()
    for i, q in enumerate(todo, 1):
        try:
            r = await sk.execute(q)
            meta = r.get("metadata", {})
            parts = re.split(r'^##\s*来源:\s*(\S+)\s*$', r.get("answer") or "", flags=re.M)
            bodies = {parts[j]: parts[j + 1] for j in range(1, len(parts) - 1, 2)}
            with open(OUT, "a", encoding="utf-8") as out:
                for s in meta.get("sources") or []:
                    url = s.get("url") or ""
                    out.write(json.dumps({
                        "query": q, "url": url, "form": s.get("score"),
                        "authority": s.get("authority"),
                        "coverage": s.get("coverage"),
                        "len": len(bodies.get(url, "")),
                        "text": bodies.get(url, "")[:2500],
                    }, ensure_ascii=False) + "\n")
            print(f"[{i}/{len(todo)}] +{len(meta.get('sources') or [])} <- {q} "
                  f"(pdf={len(meta.get('pdf_sources') or [])}) elapsed={time.time()-t0:.0f}s",
                  flush=True)
        except Exception as e:
            print(f"[{i}/{len(todo)}] {q} FAILED: {type(e).__name__}: {e}", flush=True)
    print("DONE", flush=True)


asyncio.run(main())
