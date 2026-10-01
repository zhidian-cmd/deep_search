# -*- coding: utf-8 -*-
"""低商业价值页降权（V10.1）回归：分档判定 / content_score 压分 / 抓取前分区。

实证背景（2026-09-26 微生物仪器主题两轮实测）：b2b168 店铺页 ×2 被交付、
gov.cn 询价公告排第 1，第 2 轮收集率 55%（历史 72~83%）—— 抓取名额烧在
过不了 content_score 的页面上，是"不足全取"降级的根因。
"""
import sys

from _harness import check, summary

from deep_search import filters
from deep_search.filters_keywords import LOW_VALUE_HOST_CAP, LOW_VALUE_PATH_FACTOR

# ---------- 1) 店铺档：host 子串 → 档 2 ----------
check("b2b168 店铺子域 → 档 2", filters.low_value_tier("https://lab212.cn.b2b168.com/sell/show.htm") == 2)
check("hc360 → 档 2", filters.low_value_tier("https://www.hc360.com/supplier/123.html") == 2)
check("1688 详情页 → 档 2", filters.low_value_tier("https://detail.1688.com/offer/789.html") == 2)
check("made-in-china → 档 2", filters.low_value_tier("https://www.made-in-china.com/products/a.html") == 2)
check("gov.cn 不被 china.cn 子串误伤", filters.low_value_tier("https://www.gov.cn/xinwen/2026.htm") == 0)
check("chinacnas.cn 不被 china.cn 子串误伤", filters.low_value_tier("https://chinacnas.cn/cms/show-7605.html") == 0)

# ---------- 2) 路径档：任意 host 的采购/产品路径 → 档 1 ----------
check("公告公示路径 → 档 1（实测 R2 首条）", filters.low_value_tier("https://www.whjhq.gov.cn/xwdt/gggs/18381922.html") == 1)
check("百分号编码中文路径 → 档 1", filters.low_value_tier("https://site.example.cn/%E8%AF%A2%E4%BB%B7/2026/123.html") == 1)  # /询价/
check("非编码中文路径 → 档 1", filters.low_value_tier("https://site.example.cn/zhaobiao/公告.html") == 1)
check("招标路径 → 档 1", filters.low_value_tier("https://ggzy.example.cn/zhaobiao-2026.html") == 1)
check("产品目录路径 → 档 1", filters.low_value_tier("https://www.klcfilter.com/product/123.html") == 1)
check("query 带采购词 → 档 1", filters.low_value_tier("https://site.example.cn/list?page=2&kw=%E6%B1%82%E8%B4%AD") == 1)  # kw=求购

# ---------- 3) 正常页不命中 ----------
check("百科词条不命中（/item/ 不在表内）", filters.low_value_tier("https://baike.baidu.com/item/高压蒸汽灭菌器") == 0)
check("仪器资讯正文不命中", filters.low_value_tier("https://www.foodmate.net/yiqi/31/show_165969.html") == 0)
check("标准详情页不命中", filters.low_value_tier("https://ndls.cnis.ac.cn/standard/detail/e0f1967b4b722c685cf8a44129a68cd6") == 0)
check("production 系路径不误伤（/product 精确形态）", filters.low_value_tier("https://site.example.cn/production-guide/01.html") == 0)
check("空 URL → 0", filters.low_value_tier("") == 0)

# ---------- 4) content_score：店铺档封顶 / 路径档打折 ----------
body = "仪器的使用方法与维护。\n" * 80  # 长正文 + 标点 + 段落，软分应接近满分
plain = filters.content_score(body, "https://www.example.org/guide/01.html")
capped = filters.content_score(body, "https://lab212.cn.b2b168.com/sell/show.htm")
# 对照必须是同域 URL —— 域名权重（gov.cn 1.4 vs 未知站 1.0）会改软分基线
gov_plain = filters.content_score(body, "https://www.whjhq.gov.cn/xwdt/news/1.html")
halved = filters.content_score(body, "https://www.whjhq.gov.cn/xwdt/gggs/1.html")
check(f"普通域软分较高（{plain}）", plain >= 0.5)
check(f"店铺域封顶 {capped} ≤ {LOW_VALUE_HOST_CAP}", capped <= LOW_VALUE_HOST_CAP)
check(f"路径档 = 同域普通分 ×{LOW_VALUE_PATH_FACTOR}", abs(halved - round(gov_plain * LOW_VALUE_PATH_FACTOR, 4)) < 0.01)
check("路径档下强内容仍可过 0.4 阈值", halved >= 0.4)
check("低分正文被路径档压得更低", filters.content_score("短正文。", "https://site.example.cn/product/x.html") < 0.4)

# ---------- 5) 抓取前分区：稳定、只重排不丢弃 ----------
urls = [
    "https://www.foodmate.net/yiqi/31/show_165969.html",
    "https://lab212.cn.b2b168.com/sell/show.htm",
    "https://baike.baidu.com/item/高压蒸汽灭菌器",
    "https://www.whjhq.gov.cn/xwdt/gggs/18381922.html",
]
ordered, demoted = filters.partition_low_value(urls)
check("分区后总数不变", len(ordered) == len(urls))
check("沉底清单 = 2 条低价值页", len(demoted) == 2 and urls[1] in demoted and urls[3] in demoted)
check("正常页保持原序在前", ordered[:2] == [urls[0], urls[2]])
check("低价值页保持原序在后", ordered[2:] == [urls[1], urls[3]])
check("全部正常页时分区原样返回", filters.partition_low_value([urls[0], urls[2]]) == ([urls[0], urls[2]], []))

sys.exit(summary("低价值页降权 V10.1"))
