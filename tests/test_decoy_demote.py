# -*- coding: utf-8 -*-
"""V10.6 诱饵桶消费端自检：_demote_decoy_urls 的沉底语义。

CLI 侧（internal/coherence）只负责打标；本测试锁 Python 消费端的契约：
命中引擎全属诱饵集的 URL 沉到抓取窗口尾部，只重排不丢弃，其余 fail-open。

用法：python tests/test_decoy_demote.py
"""
from _harness import check, summary

from deep_search.skill import DeepSearchSkill

RESULTS = [
    {"url": "https://a.example/rust", "engine": ["bing"]},                  # 纯诱饵引擎
    {"url": "https://b.example/rust", "engine": ["bing", "anysearch"]},     # 有干净引擎命中
    {"url": "https://c.example/rust", "engine": ["anysearch"]},             # 干净引擎
    {"url": "https://d.example/rust", "engine": []},                        # engine 缺失
    {"url": "https://e.example/rust"},                                      # 无 engine 字段
    {"url": "https://f.example/rust", "engine": ["bing", "quark"]},         # 双诱饵
]
URLS = [r["url"] for r in RESULTS]
DECOY = {"bing", "quark"}


def test_demote():
    keep_demoted, demoted = DeepSearchSkill._demote_decoy_urls(RESULTS, DECOY, URLS)
    check("纯诱饵引擎 URL 沉底",
          "https://a.example/rust" in demoted and "https://f.example/rust" in demoted,
          str(demoted))
    check("多引擎命中（含干净引擎）不沉底", "https://b.example/rust" not in demoted)
    check("干净引擎不沉底", "https://c.example/rust" not in demoted)
    check("engine 为空/缺失 fail-open",
          "https://d.example/rust" not in demoted and "https://e.example/rust" not in demoted)
    check("只重排不丢弃（数量守恒）",
          len(keep_demoted) == len(URLS) and sorted(keep_demoted) == sorted(URLS))
    check("沉底条目在队尾",
          keep_demoted[-2:] == ["https://a.example/rust", "https://f.example/rust"]
          or set(keep_demoted[-2:]) == {"https://a.example/rust", "https://f.example/rust"},
          str(keep_demoted))


def test_empty_decoy():
    keep_demoted, demoted = DeepSearchSkill._demote_decoy_urls(RESULTS, set(), URLS)
    check("空诱饵集不动任何 URL", demoted == [] and keep_demoted == URLS)


def test_order_preserved():
    keep_demoted, _ = DeepSearchSkill._demote_decoy_urls(
        RESULTS, DECOY, ["https://c.example/rust", "https://a.example/rust",
                         "https://b.example/rust", "https://f.example/rust"])
    check("各自段内保持原序",
          keep_demoted == ["https://c.example/rust", "https://b.example/rust",
                           "https://a.example/rust", "https://f.example/rust"],
          str(keep_demoted))


if __name__ == "__main__":
    test_demote()
    test_empty_decoy()
    test_order_preserved()
    import sys
    sys.exit(summary("诱饵桶消费端"))
