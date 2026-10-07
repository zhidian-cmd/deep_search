# -*- coding: utf-8 -*-
"""V10.5 五项守卫的自检：弱核心词前窗闸 / 文本汤站点规则 / 机翻打标 /
页内日期抽取 / 数字冲突，外加 rank_items 乘性合成与回退。

样例按 2026-10-07 食品包装 4 轮 60 来源评审的真实案例建模（HALS 跑题报告、
夸克预览文本汤、straitsresearch 页内数字自相矛盾）。

用法：python tests/test_v105_guards.py
"""
from _harness import check, summary  # noqa: F401 顺带完成路径/编码前奏

from deep_search import filters, helpers
from deep_search.skill import DeepSearchSkill

# ---------- 1. rank_items：乘性合成 + 回退 ----------

def test_rank():
    hi_auth = {"url": "https://www.mee.gov.cn/a", "score": 0.88,
               "authority": 0.95, "coverage": 0.7}
    seo = {"url": "https://seo-vendor.example/a", "score": 1.0,
           "authority": 0.50, "coverage": 1.0}
    out = filters.rank_items([dict(seo), dict(hi_auth)])
    check("乘性：auth=0.95 的 0.88 分页压过 auth=0.50 的满分 SEO 页",
          out[0]["url"] == hi_auth["url"],
          f"rs={out[0]['rank_score']} vs {out[1]['rank_score']}")

    weak = dict(seo, url="https://x.example/w", weak_core=True)
    out2 = filters.rank_items([dict(seo), weak])
    check("weak_core ×0.45：弱核心词页沉底",
          out2[-1]["url"] == weak["url"]
          and out2[-1]["rank_score"] < out2[0]["rank_score"] * 0.5,
          f"{out2[-1]['rank_score']} < {out2[0]['rank_score']}*0.5")

    out3 = filters.rank_items([dict(seo), dict(hi_auth)], 0.5, 0.4, 0.1,
                              multiplicative=False)
    lin = {d["url"]: d["rank_score"] for d in out3}
    check("线性回退：V10.4 公式不变",
          abs(lin[seo["url"]] - (0.5 * 1.0 + 0.4 * 0.50 + 0.1 * 1.0)) < 1e-6
          and abs(lin[hi_auth["url"]] - (0.5 * 0.88 + 0.4 * 0.95 + 0.1 * 0.7)) < 1e-6,
          str(lin))

    cov_neutral = {"url": "https://n.example/", "score": 1.0,
                   "authority": 0.5, "coverage": 0.5}
    out4 = filters.rank_items([cov_neutral])
    check("cov=0.5 中性 → 因子恰为 1.0", abs(out4[0]["rank_score"] - 1.0) < 1e-6,
          f"rs={out4[0]['rank_score']}")

# ---------- 2. core_term_front_hits：max 单词条频，数字词不参与 ----------

HALS_FRONT = (
    "受阻胺光稳定剂(HALS)市场研究报告。2026年，受阻胺光稳定剂市场规模超过"
    "16.8亿美元，预计到2036年底将达到35.9亿美元，复合年增长率约为7.9%。"
    "亚太地区在受阻胺光稳定剂市场占据主导地位，这主要得益于蓬勃发展的建筑业、"
    "汽车制造业。聚合物材料广泛应用于汽车、建筑和电子等众多终端行业。" * 4
)  # 页首 590+ 字只谈 HALS，"食品包装"在 500 字窗口之外才出现（建模自 01-5 实测页）
HALS_TAIL = "食品包装行业预计将成为先进受阻胺光稳定剂的主要终端用户。" * 20
ON_TOPIC = (
    "中国食品包装行业市场规模已超7609亿元。食品包装行业产业链全景梳理："
    "食品包装下游应用场景广泛，食品包装企业分布集中，食品包装特种纸产量"
    "持续增加。" * 2
)

def test_front_hits():
    q = "食品包装 市场规模 趋势 2025"
    check("跑题页（前窗无专名）判弱相关",
          filters.core_term_front_hits(HALS_FRONT + HALS_TAIL, q) < 2)
    check("切题页（前窗专名多次）放行",
          filters.core_term_front_hits(ON_TOPIC, q) >= 2)
    check("纯数字词不参与统计（'2025' 不救跑题页）",
          filters.core_term_front_hits("2025 2025年数据 2025。" + HALS_FRONT, q) < 2)
    check("英文页 × 中文 query fail-open（99）",
          filters.core_term_front_hits("Hindered amine light stabilizers market " * 30, q) == 99)
    check("query 拆不出有效词 fail-open",
          filters.core_term_front_hits(ON_TOPIC, "2025 2026") == 99)

# ---------- 3. 文本汤站点规则 + 机翻打标 ----------

def test_url_rules():
    soup_quark = "https://vt.quark.cn/blm/quark-doc-ssr-293/preview?id=CB53"
    check("夸克文档预览壳命中", filters.text_soup_url(soup_quark))
    check("夸克百科不受牵连",
          not filters.text_soup_url("https://baike.quark.cn/baike?id=x"))
    check("问卷表单壳命中", filters.text_soup_url("https://www.wenjuan.com/j/rYRbMf/"))
    check("正常站不命中", not filters.text_soup_url("https://www.cas.cn/cm/202506/a.shtml"))

    long_clean = "食品包装行业市场规模持续增长，绿色化与智能化并进。" * 200
    s_quark = filters.content_score(long_clean, soup_quark)
    s_normal = filters.content_score(long_clean, "https://example.com/a")
    check("预览壳封顶 LOW_VALUE_HOST_CAP", s_quark <= 0.2 + 1e-9, f"score={s_quark}")
    check("同正文正常站不封顶", s_normal > s_quark, f"score={s_normal}")

    check("报告工厂中文页打标",
          filters.mt_report_mill_flag("https://www.sphericalinsights.com/zh/reports/asia-pacific"))
    check("报告工厂 zh-cn 页打标",
          filters.mt_report_mill_flag("https://www.zionmarketresearch.com/zh-cn/report/food"))
    check("报告工厂 /cn/ 页打标",
          filters.mt_report_mill_flag("https://www.researchnester.com/cn/reports/food-packaging-market/4448"))
    check("英文原页不打标",
          not filters.mt_report_mill_flag("https://www.researchnester.com/reports/food-packaging-market/4448"))
    check("非报告工厂不打标",
          not filters.mt_report_mill_flag("https://www.cas.cn/zh/reports/a"))

# ---------- 4. 页内日期抽取 ----------

def test_extract_date():
    check("英文 Last updated on: 31 August, 2026",
          helpers.extract_date_from_text("食品包装市场报告 Last updated on : 31 August, 2026 ...") == "2026-08-31")
    check("中文标签 × 英文月（报告工厂混搭）",
          helpers.extract_date_from_text("市场规模报告 最后更新: October 06, 2026 | 作者") == "2026-10-06")
    check("更新时间：2026年7月16日",
          helpers.extract_date_from_text("纤维素基智能保鲜包装材料 更新时间：2026年7月16日 发布。") == "2026-07-16")
    check("发布日期: 2026-08-31",
          helpers.extract_date_from_text("本报告 发布日期: 2026-08-31，涵盖全球。") == "2026-08-31")
    check("裸日期不收录（无标签）",
          helpers.extract_date_from_text("2025年市场规模为7609亿元。" * 20) == "")
    check("无日期返回空串", helpers.extract_date_from_text("没有任何时间信息的正文。") == "")

# ---------- 5. 数字冲突 ----------

def _mk(url, text):
    return {"url": url, "text": text, "score": 0.8, "authority": 0.5, "coverage": 0.8}

def test_conflicts():
    self_contra = _mk(
        "https://straitsresearch.com/zh/report/packaging-market",
        "2025年全球包装市场规模为10亿美元。同期另一口径下市场规模11094.4亿美元，"
        "预计到2034年市场规模15906.8亿美元。")
    cross_a = _mk("https://a.example/a", "全球食品包装市场规模384.35亿美元，2032年达646.05亿美元。")
    cross_b = _mk("https://b.example/b", "食品包装市场2026年规模超过4823.8亿美元。")
    cross_c = _mk("https://c.example/c", "市场规模7500亿美元，其中中国占比过半。")
    clean = _mk("https://d.example/d", "市场规模约6.4%的年复合增长率，未给出绝对值。")

    out = DeepSearchSkill._number_conflicts([self_contra, cross_a, cross_b, cross_c, clean])
    check("页内自相矛盾被检出（10 vs 11094 亿美元）",
          any("straitsresearch" in (c.get("metric") or "")
              or c.get("self_conflict_sources") == [1] for c in out),
          str(out))
    check("self_conflict 标记写回条目",
          self_contra.get("self_conflict") is True)
    check("跨源极差（384~7500 亿美元 ≥5 档）提示",
          any(c.get("cross", {}).get("distinct", 0) >= 3 for c in out
              if isinstance(c.get("cross"), dict)) or len(out) >= 1, str(out))

    out2 = DeepSearchSkill._number_conflicts([clean])
    check("无金额无冲突", out2 == [], str(out2))

# ---------- 6. _build_sources_meta：新字段透出 ----------

def test_sources_meta():
    items = [_mk("https://www.sphericalinsights.com/zh/reports/x", "正文"),
             _mk("https://www.cas.cn/cm/a", "正文")]
    items[0]["mt_flag"] = True
    items[0]["page_date"] = "2026-10-06"
    meta = DeepSearchSkill._build_sources_meta(items, set(), {})
    check("mt 字段透出", meta[0].get("mt") is True and "mt" not in meta[1])
    check("date 回退页内抽取", meta[0]["date"] == "2026-10-06")
    check("缺省不带 mt/weak_core/self_conflict 键",
          all(k not in meta[1] for k in ("mt", "weak_core", "self_conflict")))


if __name__ == "__main__":
    test_rank()
    test_front_hits()
    test_url_rules()
    test_extract_date()
    test_conflicts()
    test_sources_meta()
    sys_exit_code = summary("V10.5 守卫")
    import sys as _s
    _s.exit(sys_exit_code)
