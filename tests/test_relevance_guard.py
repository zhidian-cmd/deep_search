# -*- coding: utf-8 -*-
"""V9.2 query 相关性闸离线测试（纯函数，无网络、无状态写入）。

样本取自 2026-09-20 端到端测评（query「固态电池 2026 量产进展 主要厂商 时间表」，
报告 .workbuddy/eval/eval_20260920_224350.json）：当时 12 条成品里 3 条同词异义页
（百度百科「固态硬盘」/「固态」词条、三星 SSD 页）且分数最高，全被本闸拦下。
"""
import sys

from _harness import check, summary

from deep_search import filters

Q = "固态电池 2026 量产进展 主要厂商 时间表"
MIN = 3   # config.relevance_guard_min_overlap 默认值

def verdict(text, query=Q, title=""):
    """复刻 skill 层的判定：None（无法判定）与 >= 门槛 一律放行。"""
    ov = filters.query_overlap(text, query, title)
    return ("KEEP" if ov is None or ov >= MIN else "REMOVE"), ov


print("== #1 真实测评样本：同词异义页必须被拦下 ==")
legit = [
    "## 来源: https://auto.gasgoo.com/news/202601/5I70441198C501.shtml\n\n"
    "备受行业关注、呼声高涨的固态电池，2026年被行业公认为量产元年……",
    "## 来源: https://chejiahao.autohome.com.cn/info/25021131\n\n"
    "比亚迪，2027年小批量生产。广汽，2026年……固态电池装车时间表",
]
for t in legit:
    v, ov = verdict(t)
    check("正文放行", v == "KEEP", f"overlap={ov}")

offtopic = [
    ("百度百科「固态硬盘」",
     "## 来源: https://baike.baidu.com/item/%E5%9B%BA%E6%80%81%E7%A1%AC%E7%9B%98/453510\n\n"
     "固态硬盘（Solid State Disk）是用固态存储芯片阵列制成的硬盘，"
     "由控制单元和存储单元组成，主控芯片负责读写调度……"),
    ("百度百科「固态」词条",
     "## 来源: https://baike.baidu.com/item/%E5%9B%BA%E6%80%81/11056794\n\n"
     "结合物体的微粒间距离很小，作用力很大。固态是物质存在的一种状态……"),
    ("三星 SSD 产品页",
     "## 来源: https://www.samsung.com.cn/ssd/\n\n### 870 QVO，860 PRO\n"
     "将计算性能提升到新的高度，三星固态硬盘为你的电脑注入动力……"),
]
for name, t in offtopic:
    v, ov = verdict(t)
    check(f"跑题拦截：{name}", v == "REMOVE", f"overlap={ov}")

print("== #2 URL 里带 query 不算命中（出处头必须剔除）==")
t = "## 来源: https://www.baidu.com/s?wd=固态电池2026量产时间表\n\n这是一篇讲足球比赛战术的文章。"
v, ov = verdict(t)
check("URL 命中被忽略", v == "REMOVE", f"overlap={ov}")

print("== #3 标题命中可救回正文不含词组的页面 ==")
v, ov = verdict("## 来源: https://x.example/a\n\n本次大会共发布三项成果。", Q, title="固态电池量产时间表公布")
check("标题命中放行", v == "KEEP", f"overlap={ov}")

print("== #4 语言不对等 / 无判别力 query 一律放行（fail-open）==")
v, ov = verdict("## 来源: https://en.example/a\n\nSolid-state battery pilot line starts in 2026.", Q)
check("中文 query × 英文页 → 无法判定", ov is None and v == "KEEP", f"overlap={ov}")
for q in ["补贴 政策 调整", "", "   ", "2026 2027"]:
    v, ov = verdict("## 来源: https://x/a\n\n完全无关的正文。", q)
    check(f"无判别力 query 放行：{q!r}", ov is None and v == "KEEP", f"overlap={ov}")

print("== #5 英文 query 按单词判定（词边界，不做子串假匹配）==")
v, ov = verdict("## 来源: https://x/a\n\nPython 3.13 adds a new REPL.", "python 新特性")
check("英文词命中放行", v == "KEEP", f"overlap={ov}")
v, ov = verdict("## 来源: https://x/a\n\nHe said the air is fresh.", "python 新特性")
check("'said'/'air' 不算命中 python", v == "REMOVE", f"overlap={ov}")

print("== #6 已知边界：3 字子串命中即放行（宁可漏杀，不误杀）==")
v, ov = verdict("## 来源: https://x/a\n\n固态电容常用于主板供电电路。", Q)
check("含'固态电'的页放行（记为已知局限）", v == "KEEP", f"overlap={ov}")

sys.exit(summary())
