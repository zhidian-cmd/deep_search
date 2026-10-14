# -*- coding: utf-8 -*-
"""V10.7 抓取配额经济性回归：黑名单沉底 / 学习层负样本沉底 / 同批失败 host 跳过。

实证背景（2026-10-14 诊断，temp/logs/penalty_stats.json 实测）：
- wenku/docin/book118/renrendoc 等黑名单站跨轮 100+ 次抓取尝试几乎全 0 分
  （URL_BLACKLIST 此前只在抓取后 content_score 生效，先抓再判纯属烧名额）；
- toutiao 系 44 次尝试 lat_avg 23~24s、delta 已判 -0.4 仍每轮照试
  （current_delta 唯一消费点是抓后打分，候选选择不读它）；
- "Per-URL timeout" 全史 84 次，单条熔断 35s 白耗。

三个机制共同保证"降权不拉黑"：池子深时不花名额，池子浅时照样抓。
不触外网：分区是纯函数，抓取链 _fetch_chain 打桩。
"""
import asyncio
import json
import os
import sys
import tempfile
import time

from _harness import check, summary

from deep_search import filters, filters_learn, helpers
from deep_search import web_fetch as W
from deep_search.config import DeepSearchConfig

# ---------- 1) 黑名单域名沉底：稳定分区、只重排不丢弃 ----------
print("== 1. partition_blacklisted：黑名单域名沉到抓取窗口尾部 ==")
urls = [
    "https://www.foodmate.net/yiqi/31/show_165969.html",
    "https://wenku.baidu.com/view/abc123.html",
    "https://baike.baidu.com/item/高压蒸汽灭菌器",
    "https://max.book118.com/html/2026/0101/123.shtml",
    "https://www.renrendoc.com/paper/456.html",
    "https://www.baidu.com/s?wd=x",
]
ordered, demoted = filters.partition_blacklisted(urls)
check("分区后总数不变", len(ordered) == len(urls))
check("沉底清单 = 4 条黑名单页",
      len(demoted) == 4
      and urls[1] in demoted and urls[3] in demoted
      and urls[4] in demoted and urls[5] in demoted)
check("正常页保持原序在前", ordered[:2] == [urls[0], urls[2]])
check("黑名单页保持原序在后", ordered[2:] == [urls[1], urls[3], urls[4], urls[5]])
check("全部正常页时分区原样返回",
      filters.partition_blacklisted([urls[0], urls[2]]) == ([urls[0], urls[2]], []))
check("空 URL 不误伤", filters.partition_blacklisted(["", urls[0]])[1] == [])
check("docin/doc88/51wendang/taodocs/360doc 也在表内",
      all(filters.url_blacklisted(f"https://www.{d}/x")
          for d in ["docin.com", "doc88.com", "51wendang.com",
                    "taodocs.com", "360doc.com"]))

# ---------- 2) 学习层负样本沉底：delta 阈值 + 白名单豁免 ----------
print("== 2. partition_learned_negative：学习信号提前到候选选择消费 ==")
orig_stats_path = filters_learn._STATS_PATH
tmp_stats = os.path.join(tempfile.mkdtemp(prefix="ds107_"), "penalty_stats.json")
now = time.time()
stats = {
    "dead.example.com": {  # ok==0 且 n≥8 → _judge 判 -0.5（抓不到）
        "n": 12, "ok": 0, "score_sum": 0.0, "low": 0,
        "lat_sum": 240.0, "lat_n": 8, "last": now,
        "delta": -0.5, "delta_date": now,
    },
    "junk.example.com": {  # 全抓到但全 0 分 → -0.4（低质）
        "n": 10, "ok": 10, "score_sum": 0.0, "low": 10,
        "lat_sum": 30.0, "lat_n": 10, "last": now,
        "delta": -0.4, "delta_date": now,
    },
    "slow.example.com": {  # 慢站 -0.3：慢不等于差，阈值之上不沉
        "n": 10, "ok": 10, "score_sum": 8.0, "low": 0,
        "lat_sum": 280.0, "lat_n": 10, "last": now,
        "delta": -0.3, "delta_date": now,
    },
    "www.gov.cn": {  # 白名单域即使被记 -0.5 也豁免（用户纠偏出口不被覆盖）
        "n": 12, "ok": 0, "score_sum": 0.0, "low": 0,
        "lat_sum": 240.0, "lat_n": 8, "last": now,
        "delta": -0.5, "delta_date": now,
    },
    "healthy.example.com": {
        "n": 10, "ok": 10, "score_sum": 9.0, "low": 0,
        "lat_sum": 5.0, "lat_n": 10, "last": now,
        "delta": 0.1, "delta_date": 0.0,
    },
}
try:
    os.makedirs(os.path.dirname(tmp_stats), exist_ok=True)
    with open(tmp_stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False)
    filters_learn._STATS_PATH = tmp_stats
    filters_learn._read_cache.clear()  # 改绑路径后旧缓存自动失效，这里显式清一道

    cand = [
        "https://www.foodmate.net/yiqi/31/show_165969.html",
        "https://www.dead.example.com/a/1.html",
        "https://baike.baidu.com/item/高压蒸汽灭菌器",
        "https://junk.example.com/p/2.html",
        "https://www.slow.example.com/p/3.html",
        "https://www.gov.cn/xinwen/2026.htm",
        "https://healthy.example.com/p/4.html",
    ]
    ordered, demoted = filters.partition_learned_negative(
        cand, max_delta=DeepSearchConfig().learned_negative_max_delta)
    check("分区后总数不变", len(ordered) == len(cand))
    check("沉底清单 = dead + junk 两条",
          len(demoted) == 2 and cand[1] in demoted and cand[3] in demoted)
    check("慢站 -0.3 不沉（阈值 -0.4）", cand[4] not in demoted)
    check("白名单 gov.cn 豁免（delta=-0.5 也不动）", cand[5] not in demoted)
    check("健康 host 不沉", cand[6] not in demoted)
    check("正常页保持原序在前",
          ordered[:5] == [cand[0], cand[2], cand[4], cand[5], cand[6]])
    check("分区只读不写统计（统计文件未被改）",
          json.load(open(tmp_stats, encoding="utf-8")) == stats)

    # 学习层故障 fail-open：统计文件不可读 → delta 视为 0 → 全部不沉
    filters_learn._STATS_PATH = os.path.join(
        tempfile.mkdtemp(prefix="ds107b_"), "no_such_dir", "x.json")
    filters_learn._read_cache.clear()
    check("学习层故障 fail-open（不可读路径 → 全部不沉）",
          filters.partition_learned_negative(
              ["https://dead.example.com/a"], max_delta=-0.4)[1] == [])
finally:
    filters_learn._STATS_PATH = orig_stats_path
    filters_learn._read_cache.clear()

# ---------- 3) 同批失败 host 跳过：不占尝试名额、队列向后补位 ----------
print("== 3. _fetch_direct 同批失败 host 跳过 ==")


async def skip_scenarios():
    orig_conc = W.MAX_CONCURRENT_FETCHES
    orig_pd = W.filters_learn.penalty_delta
    W.filters_learn.penalty_delta = lambda *a, **k: 0.0   # 学习层打桩，不落盘
    W.MAX_CONCURRENT_FETCHES = 1  # 串行化：bad1 失败后才有机会跳过 bad2
    GOOD = "第一段正文内容。\n第二段也有实质信息。\n" * 30
    try:
        mgr = W.ScraplingManager(DeepSearchConfig())

        async def chain(url, host_cache, *a, **k):
            calls.append(url)
            if helpers.host_of(url) == "bad.example.com":
                return None  # 硬失败（超时/空结果同路径）
            return (GOOD, "标题")

        mgr._fetch_chain = chain

        # 3a. bad1 失败后，bad2 不再尝试、也不占 fetch_count 名额
        calls = []
        pending = ["https://bad.example.com/1", "https://bad.example.com/2",
                   "https://good.example.com/1", "https://good.example.com/2"]
        res = await mgr._fetch_direct(list(pending), 4)
        got = [d["url"] for d in res]
        check("bad1 硬失败后 bad2 不再尝试",
              calls == [pending[0], pending[2], pending[3]], f"calls={calls}")
        check("两篇好文全部收回", sorted(got) == sorted(pending[2:]), f"got={got}")
        check("坏域正文不入池", all("bad.example.com" not in u for u in got))

        # 3b. 跳过不烧名额：fetch_count=3 时仍能抓到 2 篇好文
        #     （若跳过计入 scanned，名额会被 bad2 白占，好文只剩 1 篇）
        calls = []
        pending = ["https://bad.example.com/1", "https://bad.example.com/2",
                   "https://good.example.com/1", "https://good.example.com/2",
                   "https://good.example.com/3"]
        res = await mgr._fetch_direct(list(pending), 3)
        got = [d["url"] for d in res]
        check("跳过不占尝试名额（count=3 仍收回 2 篇好文）",
              got == [pending[2], pending[3]], f"got={got}")
        check("bad2 确实没被尝试", pending[1] not in calls, f"calls={calls}")

        # 3c. 失败 host 的首条 URL 已给过机会（一票否决的是同批再试，不是整个 host）
        check("host 首条 URL 被正常尝试", pending[0] in calls)
    finally:
        W.MAX_CONCURRENT_FETCHES = orig_conc
        W.filters_learn.penalty_delta = orig_pd

asyncio.run(skip_scenarios())

sys.exit(summary("V10.7 抓取配额经济性"))
