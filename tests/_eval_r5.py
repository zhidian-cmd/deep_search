# -*- coding: utf-8 -*-
"""R5：大规模多领域验收（batch1+2+粮油）。

用法：python _eval_r5.py
输出：
  1. 数据规模（条数/query数/host数/领域数）
  2. 基线 vs 候选权重的全量与分领域指标
  3. 分领域网格最优（权重稳定性）
  4. 留一领域外推（泛化性）
"""
import json
import re
import sys
from collections import defaultdict
from urllib.parse import urlparse

sys.path.insert(0, r"D:/Runtimelibrary/SERVER/MCP")
from deep_search import filters

DOMAIN_OF = {
    # 粮油
    "糙米 营养价值 为什么 碾米 精白米 食用品质 保存": "粮油食品",
    "稻谷 籽粒 结构 特点 颖壳 糙米 皮层 胚乳 胚": "粮油食品",
    "米粉 产品特征 关键工艺 榨粉 切粉 蒸粉 老化": "粮油食品",
    "蒸谷米 营养价值 提高 原理 水热处理 维生素 糊化": "粮油食品",
    "稻谷 制米 工艺流程 清理 砻谷 碾米 成品整理 目的": "粮油食品",
    "稻谷清理 方法 原理 常用设备 筛选 风选 去石 磁选": "粮油食品",
    "免洗米 工艺要点 质量要求 上光 抛光 残留糠粉": "粮油食品",
    "米粉 加工工艺 流程 洗米 磨浆 蒸粉 挤丝 复蒸 干燥 直条米粉": "粮油食品",
    # batch1
    "固态电池 电解质 量产 工艺": "新能源产业",
    "量子计算 纠错 量子比特": "前沿科技",
    "行政处罚 听证 程序 时限": "法律政务",
    "2型糖尿病 降糖药 指南 二甲双胍": "医药健康",
    "大模型 LoRA 微调 显存": "AI开发",
    "MySQL 索引 优化 慢查询": "后端开发",
    "空气净化器 CADR 选购 参数": "家电消费",
    "光伏 逆变器 MPPT 效率": "新能源产业",
    "跨境电商 出口 报关 流程": "外贸政务",
    "高血压 降压药 联合用药": "医药健康",
}

# 明显跑题/低质 host 或 URL 片段 → rel（未标注=1；用于 low_bad 计数）
REL_PATCH = {
    "vt.quark.cn": .5, "iconson.top": .5, "www.oryoy.com": .5, "jishuzhan.net": .5,
    "kaifayun.com": .5, "duoke360.com": .5, "idctop.com": .5, "www.58codes.com": .5,
    "www.eshutong.com": .5, "www.yjsshiwu.com": .5, "www.sh-zhongshen.com": .5,
    "www.capital-peak.com": .5, "www.17430.com.cn": .5, "m.chwang.com": .5,
    "www.chwang.com": .5, "shenzhen.11467.com": .5, "m.joybuy.de": .5,
    "dir.jd.com": .5, "swt.fujian.gov.cn": .5, "guba.sina.com.cn": .5,
    "m.zuotishi.com": .5, "duanjuso.com": .5,
}


def rel_of(url):
    for k, v in REL_PATCH.items():
        if k in url:
            return v
    return 1.0


def load_all():
    items = []
    # 粮油（旧归档数据集，需重算 form）
    for d in json.load(open("deep_search/tests/_eval_dataset.json", encoding="utf-8")):
        d["text"] = f"## 来源: {d['url']}\n\n" + d["text"]
        d["form"] = filters.content_score(d["text"], d["url"])
        d["domain"] = "粮油食品"
        items.append(d)
    # live（batch1+2，form 用生产值）
    for r in (json.loads(l) for l in open("deep_search/tests/_eval_live.jsonl", encoding="utf-8")):
        r["domain"] = DOMAIN_OF.get(r["query"], "其他")
        items.append(r)
    # 去重 (query,url)
    seen, out = set(), []
    for d in items:
        k = (d["query"], d["url"])
        if k in seen:
            continue
        seen.add(k)
        d["rel"] = rel_of(d["url"])
        d.setdefault("authority", filters.authority_score(d["url"]))
        out.append(d)
    return out


def main():
    ALL = load_all()
    by_q = defaultdict(list)
    for d in ALL:
        by_q[d["query"]].append(d)
    for q, items in by_q.items():
        covs = filters.query_coverage([d["text"] for d in items], q)
        for d, c in zip(items, covs):
            d["coverage"] = c
    domains = sorted(set(d["domain"] for d in ALL))
    hosts = set(urlparse(d["url"]).hostname for d in ALL)
    print(f"== 数据规模: {len(ALL)} 条 / {len(by_q)} query / {len(hosts)} host / {len(domains)} 领域")
    for dom in domains:
        qs = [q for q in by_q if by_q[q][0]["domain"] == dom]
        print(f"   {dom}: {len(qs)} 题 {sum(len(by_q[q]) for q in qs)} 条")

    def ev(w_f, w_a, w_c, queries=None):
        qs = queries if queries is not None else list(by_q)
        t5a, firsts, low5 = [], [], 0
        for q in qs:
            items = by_q[q]
            ranked = sorted(items, key=lambda d: -(w_f * d["form"] + w_a * d["authority"]
                                                   + w_c * d["coverage"]))
            top = ranked[:5]
            t5a.append(sum(x["authority"] for x in top) / len(top))
            firsts.append(ranked[0]["authority"])
            low5 += sum(1 for x in top if x["authority"] <= 0.4 and x.get("rel", 1) < 1)
        return {"top5_auth": round(sum(t5a) / len(t5a), 3),
                "first_auth": round(sum(firsts) / len(firsts), 3), "low_bad": low5}

    print("\n== 基线 vs 候选权重（全量） ==")
    for name, w in (("基线", (1, 0, 0)), ("V10.4初版", (0.60, 0.25, 0.15)),
                    ("候选A", (0.55, 0.35, 0.10)), ("外推点", (0.60, 0.40, 0.05))):
        r = ev(*w)
        print(f"  {name:10s} top5_auth={r['top5_auth']} first_auth={r['first_auth']} low_bad={r['low_bad']}")

    print("\n== 分领域：基线/候选A/本域最优 ==")
    domain_opt = {}
    for dom in domains:
        qs = [q for q in by_q if by_q[q][0]["domain"] == dom]
        best, best_key = None, None
        for w_a in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45):
            for w_c in (0.05, 0.10, 0.15, 0.20, 0.25):
                w_f = 1 - w_a - w_c
                if w_f < 0.35:
                    continue
                r = ev(w_f, w_a, w_c, qs)
                key = (-r["top5_auth"], r["low_bad"])
                if best_key is None or key < best_key:
                    best_key, best = key, (round(w_a, 2), round(w_c, 2), r)
        domain_opt[dom] = best
        b = ev(1, 0, 0, qs)
        a = ev(0.55, 0.35, 0.10, qs)
        print(f"  {dom:6s}({len(qs)}题): 基线={b['top5_auth']} 候选A={a['top5_auth']} "
              f"本域最优={best[2]['top5_auth']}(a={best[0]},c={best[1]})")

    print("\n== 留一领域外推 ==")
    stable = 0
    for dom in domains:
        train_qs = [q for q in by_q if by_q[q][0]["domain"] != dom]
        test_qs = [q for q in by_q if by_q[q][0]["domain"] == dom]
        best, best_key = None, None
        for w_a in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45):
            for w_c in (0.05, 0.10, 0.15, 0.20, 0.25):
                w_f = 1 - w_a - w_c
                if w_f < 0.35:
                    continue
                r = ev(w_f, w_a, w_c, train_qs)
                key = (-r["top5_auth"], r["low_bad"])
                if best_key is None or key < best_key:
                    best_key, best = key, (round(w_a, 2), round(w_c, 2))
        r_ext = ev(1 - best[0] - best[1], best[0], best[1], test_qs)
        r_own = domain_opt[dom][2]
        gap = r_own["top5_auth"] - r_ext["top5_auth"]
        stable += gap <= 0.02
        print(f"  留出[{dom:6s}] 外推a={best[0]},c={best[1]} top5_auth={r_ext['top5_auth']} "
              f"本域最优={r_own['top5_auth']} 差={gap:+.3f}")
    print(f"\n外推稳定: {stable}/{len(domains)}")
    json.dump([{k: d.get(k) for k in ("query", "url", "domain", "form", "authority",
                                       "coverage", "rel", "len")}
               for d in ALL],
              open("deep_search/tests/_eval_dataset_v3.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    print("v3 数据集已保存: deep_search/tests/_eval_dataset_v3.json")


if __name__ == "__main__":
    main()
