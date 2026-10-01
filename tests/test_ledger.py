# -*- coding: utf-8 -*-
"""跨搜索账本（`ledger.py`）的离线测试。

锁五件事：
  ① L1 判据 `norm_url`：抓的是 URL 的**写法变体**（http/https、?utm_*、尾斜杠、%xx
     大小写、query 顺序），且**不误杀**不同页（路径分段、`?from=` 这类有语义的参数）；
  ② L3 判据 `grams` / `containment`：与同批去重同一份实现，含"共享前缀"这一关键形态；
  ③ 账本记账：只记**交付过的**、跨轮认得出；
  ④ 正文预算：超预算从**最老那轮**清正文，而**元数据永不丢**（第 3 轮仍要拦住第 1 轮）；
  ⑤ 接线：连搜三轮（打桩，不联网）—— 每轮都不重复交付前几轮的来源，第二轮还会被
     L3 拦住"换了 URL 的同一篇"。

不写生产目录（ARCHIVE_DIR 重定向到临时目录）。
"""
import asyncio
import hashlib
import sys
import tempfile

from _harness import check, summary

from deep_search import ledger
from deep_search import config as C

C.ARCHIVE_DIR = tempfile.mkdtemp()
from deep_search.config import DeepSearchConfig          # noqa: E402
from deep_search import skill as S                       # noqa: E402

S.ARCHIVE_DIR = C.ARCHIVE_DIR


def _stub(sk, cands):
    """给 skill 打桩：搜索返回给定候选、抓取对每条造一段互不相同的正文。

    `cands` 可以是 URL 字符串，也可以是 (url, text) 二元组 —— 后者用来指定某条
    抓回来的正文（跨轮雷同的用例靠它）。
    """
    fixed = {}
    urls = []
    for c in cands:
        if isinstance(c, tuple):
            fixed[c[0]] = c[1]
            urls.append(c[0])
        else:
            urls.append(c)
    idx = {u: i for i, u in enumerate(urls)}

    async def fake_search(query, limit=15):
        return {"results": [
            {"url": u, "title": f"标题{idx[u]}", "snippet": "摘要", "date": "2026-09-23",
             "engine": ["exa"], "positions": {"exa": idx[u] + 1}}
            for u in urls
        ]}

    async def fake_fetch(pending, fetch_count, budget=None, interactions=None):
        out = []
        for u in pending:
            # 正文必须互不相同，否则会被同批内容判重合成一条：默认用 URL 的 sha1 填充。
            body = fixed.get(u) or (hashlib.sha1(u.encode()).hexdigest() + " ") * 14
            out.append({"url": u, "score": 0.9, "page_title": f"标题{idx.get(u, 0)}",
                        "text": f"## 来源: {u}\n\n关键词甲 关键词乙 {body}采样方案正文。"})
        return out

    sk._call_search_core = fake_search
    sk.scrapling.fetch_all = fake_fetch


def main():
    print("== 1. L1 判据：URL 归一 ==")
    n = ledger.norm_url
    same = [
        ("https://a.com/x", "http://a.com/x"),                       # scheme 等价
        ("https://a.com/x/", "https://a.com/x"),                     # 尾斜杠
        ("https://a.com/x?utm_source=exa&utm_medium=cpc", "https://a.com/x"),  # 归因参数
        ("https://a.com/x?id=7&page=2", "https://a.com/x?page=2&id=7"),        # query 顺序
        ("https://a.com/%e7%b4%ab", "https://a.com/%E7%B4%AB"),       # %xx 大小写
        ("https://A.COM/x", "https://a.com/x"),                      # host 大小写
        ("https://a.com:443/x", "https://a.com/x"),                  # scheme 默认端口
        ("https://a.com/紫色", "https://a.com/%E7%B4%AB%E8%89%B2"),   # 明码 vs 百分号
        ("https://a.com?write", "https://a.com?write="),             # 空值参数
    ]
    for a, b in same:
        check(f"同一页：{a[:38]}", n(a) == n(b), f"{n(a)} vs {n(b)}")
    diff = [
        ("https://a.com/x/1", "https://a.com/x/2"),                  # 不同页
        ("https://a.com/a", "https://b.com/a"),                      # 不同站
        ("https://a.com/x?from=chapter2", "https://a.com/x"),        # ?from= 有语义，刻意不剥
        ("https://a.com/a%2Fb", "https://a.com/a/b"),                # 编码的分隔符 ≠ 层级
    ]
    for a, b in diff:
        check(f"不同页：{a[:38]}", n(a) != n(b), f"{n(a)} == {n(b)}")
    check("空 URL → 空串", n("") == "" and n(None) == "")
    check("畸形 → 退化小写而不抛", n("not a url") == "not a url")

    print("== 2. L3 判据：3-gram 包含度 ==")
    inner = "垃圾" * 10 + "量子计算是未来十年最重要的技术方向之一" + "尾巴" * 30
    wrapper = "量子计算是未来十年最重要的技术方向之一" + "导语" * 20
    check("共享片段 → 包含度高（>0.6）",
          ledger.containment(ledger.grams(inner), ledger.grams(wrapper)) > 0.6,
          round(ledger.containment(ledger.grams(inner), ledger.grams(wrapper)), 3))
    check("无关文本 → 接近 0",
          ledger.containment(ledger.grams("固态电池量产工艺"), ledger.grams("清明上河图鉴赏")) < 0.1)
    check("空集 → 0（不构成相同证据）",
          ledger.containment(set(), ledger.grams("abc")) == 0.0)
    check("完全相同 → 1.0", ledger.containment(ledger.grams("同一段文字"), ledger.grams("同一段文字")) == 1.0)
    check("只按字符不看空白", ledger.grams("a b c") == ledger.grams("abc"))

    print("== 3. 账本记账与 L1 剔除 ==")
    ledger.reset()
    r1 = ledger.record([
        {"url": "https://a.com/x", "text": "甲正文" * 50},
        {"url": "https://b.com/y?utm_source=exa", "text": "乙正文" * 50},
    ])
    check("记了 2 条", len(ledger._BY_NURL) == 2, len(ledger._BY_NURL))
    keep, drop = ledger.filter_candidates([
        "https://a.com/x",                     # 完全一样
        "http://A.COM/x/",                     # 写法变体 → 也是同一页
        "https://b.com/y",                     # 剥掉 utm 后同一页
        "https://c.com/new",                   # 新来源
    ])
    check("剔掉 3 条已给过的", len(drop) == 3, repr([d["url"] for d in drop]))
    check("留下 1 条新的", keep == ["https://c.com/new"], repr(keep))
    check("被剔条目带首次轮次", all(d["round"] == r1.no for d in drop))
    check("新来源不是因为撞别的键被剔", ledger.filter_candidates(["https://c.com/new"])[1] == [])

    print("== 4. L3 剔除：换了 URL 的同一篇 ==")
    # 账本里 https://a.com/x 的正文是「甲正文」*50 —— 这一条换了个站放同样的内容
    dup_body = "甲正文" * 50
    keep2, drop2 = ledger.filter_bodies(
        [{"url": "https://other.com/copy", "text": f"## 来源: x\n\n{dup_body}"},
         {"url": "https://other.com/fresh", "text": "完全不相干的一篇新文章" * 5}],
        limit=512, threshold=0.6,
    )
    check("雷同的剔掉", len(drop2) == 1 and drop2[0]["url"] == "https://other.com/copy", repr(drop2))
    check("剔掉时指认了跟谁重复", drop2[0]["dup_of"] == "https://a.com/x", drop2[0]["dup_of"])
    check("相似度报出来（1.0 = 同一篇）", drop2[0]["sim"] == 1.0, drop2[0]["sim"])
    check("不雷同的留下", [i["url"] for i in keep2] == ["https://other.com/fresh"])
    # 账本里没有正文时（PDF-only 轮次、被预算清过的轮次）不能凭空集误杀
    for e in ledger._BY_NURL.values():
        e.body = ""
    check("账本里没有正文 → 一条都不剔",
          ledger.filter_bodies([{"url": "https://other.com/copy", "text": dup_body}], 512, 0.6)[1] == [])

    print("== 5. 正文预算：丢最老那轮的正文，元数据永不丢 ==")
    ledger.reset()
    big = "长正文" * 200_000        # 单条约 60 万字符，三轮即超 150 万预算
    for i in range(3):
        ledger.record([{"url": f"https://p{i}.com/a", "text": big}])
    body_chars = sum(r.body_chars for r in ledger._ROUNDS)
    check("正文被淘汰（预算生效）", body_chars <= ledger._BODY_BUDGET_CHARS, body_chars)
    check("元数据一条没丢（三轮都在）", len(ledger._BY_NURL) == 3, len(ledger._BY_NURL))
    check("最老那轮的正文已清", not ledger._BY_NURL[ledger.norm_url("https://p0.com/a")].body)
    check("最新那轮的正文还在", bool(ledger._BY_NURL[ledger.norm_url("https://p2.com/a")].body))
    check("清掉正文后 L1 仍然拦得住第 1 轮的来源",
          ledger.filter_candidates(["https://p0.com/a"])[0] == [])

    print("== 6. 接线：连搜三轮（打桩，不联网）==")
    ledger.reset()
    sk = S.DeepSearchSkill(DeepSearchConfig())
    # 60 个候选：三轮 × 15 条刚好够，用来验证"每轮都还能交付满一轮"
    _stub(sk, [f"https://cand/{i}" for i in range(60)])
    seen = set()
    for rnd, q in enumerate(["关键词甲", "关键词乙", "关键词丙"], 1):
        m = asyncio.run(sk.execute(q))["metadata"]
        got = {s["url"] for s in m["sources"]}
        check(f"第 {rnd} 轮不重复交付前几轮",
              not (got & seen), repr(sorted(got & seen)[:3]))
        check(f"第 {rnd} 轮交付满一轮", len(got) == 15, len(got))
        if rnd > 1:
            check(f"第 {rnd} 轮剔掉了前几轮的全部来源",
                  len(m.get("skipped_repeats") or []) == len(seen),
                  f"{len(m.get('skipped_repeats') or [])} vs {len(seen)}")
        seen |= got

    print("== 7. 接线：第二轮的同一篇换了个 URL（L3 拦）==")
    ledger.reset()
    sk2 = S.DeepSearchSkill(DeepSearchConfig())
    body_a = "量子计算是未来十年最重要的技术方向之一" + "正文" * 60
    # 独苗用 sha1 填充：重复汉字（如"独苗"*30）之间包含度会虚高，测不出"不误杀"
    u1 = (hashlib.sha1(b"only1").hexdigest() + " ") * 14
    u2 = (hashlib.sha1(b"only2").hexdigest() + " ") * 14
    _stub(sk2, [("https://mirror-a.com/x", body_a), ("https://only1.com/a", u1)])
    m1 = asyncio.run(sk2.execute("关键词甲"))["metadata"]
    check("第一轮交付 2 条", len(m1["sources"]) == 2, len(m1["sources"]))
    # 第二轮：同一篇换了站（URL 完全不同，L1 拦不住），另加一条真正的新来源
    _stub(sk2, [("https://mirror-b.com/y", body_a), ("https://only2.com/b", u2)])
    m2 = asyncio.run(sk2.execute("关键词乙"))["metadata"]
    check("L1 拦不住它（URL 确实不同）", not m2.get("skipped_repeats"), m2.get("skipped_repeats"))
    dropped = m2.get("cross_round_dropped") or []
    check("L3 在正文层拦住了它（只拦它一条）",
          len(dropped) == 1 and dropped[0]["url"] == "https://mirror-b.com/y", repr(dropped))
    check("交付里只剩真正的新来源",
          [s["url"] for s in (m2.get("sources") or [])] == ["https://only2.com/b"],
          [s["url"] for s in (m2.get("sources") or [])])

    return summary()


sys.exit(main())
