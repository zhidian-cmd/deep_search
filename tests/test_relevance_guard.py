# -*- coding: utf-8 -*-
"""query 相关性闸回归（V9.2 建，V10.7 改双信号）。

样本取自 2026-09-20 端到端测评（query「固态电池 2026 量产进展 主要厂商 时间表」，
报告 .workbuddy/eval/eval_20260920_224350.json）：当时 12 条成品里 3 条同词异义页
（百度百科「固态硬盘」/「固态」词条、三星 SSD 页）且分数最高，全被本闸拦下。

V10.7（2026-10-14）：片段闸叠加词级救援信号（query_term_hits，命中 ≥2 词即放行）。
动机：混合 query（2 字核心词 + 1 个 ≥3 字词）下只共享 2 字核心词的高相关页被
整批误杀——果蔬 3 轮实测（temp/logs/deep_search.log 2026-10-14 14:45~14:58）：
中科院/农业工程学报/食品工业科技 DOI 等 11 篇被移出、Q3c 交付只剩 3 条。
本测试锁住：旧样本判定不回退 + 新救援场景生效 + 词级信号永不新增移出。
"""
import sys

from _harness import check, summary

from deep_search import filters

Q = "固态电池 2026 量产进展 主要厂商 时间表"
MIN = 3        # config.relevance_guard_min_overlap 默认值
MIN_TERMS = 2  # config.relevance_guard_min_term_hits 默认值（V10.7）

def verdict(text, query=Q, title=""):
    """复刻 skill 层判定（V10.7 双信号）：任一判相关即放行，都判跑题才移出。"""
    ov = filters.query_overlap(text, query, title)
    hits = filters.query_term_hits(text, query, title)
    frag_ok = ov is None or ov >= MIN
    terms_ok = hits is None or hits >= MIN_TERMS
    return ("KEEP" if frag_ok or terms_ok else "REMOVE"), ov, hits


print("== #1 真实测评样本：同词异义页必须被拦下（旧判定不回退）==")
legit = [
    "## 来源: https://auto.gasgoo.com/news/202601/5I70441198C501.shtml\n\n"
    "备受行业关注、呼声高涨的固态电池，2026年被行业公认为量产元年……",
    "## 来源: https://chejiahao.autohome.com.cn/info/25021131\n\n"
    "比亚迪，2027年小批量生产。广汽，2026年……固态电池装车时间表",
]
for t in legit:
    v, ov, hits = verdict(t)
    check("正文放行", v == "KEEP", f"ov={ov} hits={hits}")

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
    v, ov, hits = verdict(t)
    check(f"跑题拦截：{name}", v == "REMOVE", f"ov={ov} hits={hits}")

print("== #2 URL 里带 query 不算命中（出处头必须剔除）==")
t = "## 来源: https://www.baidu.com/s?wd=固态电池2026量产时间表\n\n这是一篇讲足球比赛战术的文章。"
v, ov, hits = verdict(t)
check("URL 命中被忽略", v == "REMOVE", f"ov={ov} hits={hits}")

print("== #3 标题命中可救回正文不含词组的页面 ==")
v, ov, hits = verdict("## 来源: https://x.example/a\n\n本次大会共发布三项成果。", Q, title="固态电池量产时间表公布")
check("标题命中放行", v == "KEEP", f"ov={ov} hits={hits}")

print("== #4 语言不对等 / 无判别力 query 一律放行（fail-open）==")
v, ov, hits = verdict("## 来源: https://en.example/a\n\nSolid-state battery pilot line starts in 2026.", Q)
check("中文 query × 英文页 → 无法判定", ov is None and hits is None and v == "KEEP", f"ov={ov} hits={hits}")
for q in ["补贴 政策 调整", "", "   ", "2026 2027"]:
    v, ov, hits = verdict("## 来源: https://x/a\n\n完全无关的正文。", q)
    check(f"无判别力 query 放行：{q!r}", ov is None and v == "KEEP", f"ov={ov} hits={hits}")

print("== #5 英文 query 按单词判定（词边界，不做子串假匹配）==")
v, ov, hits = verdict("## 来源: https://x/a\n\nPython 3.13 adds a new REPL.", "python 新特性")
check("英文词命中放行", v == "KEEP", f"ov={ov} hits={hits}")
v, ov, hits = verdict("## 来源: https://x/a\n\nHe said the air is fresh.", "python 新特性")
check("'said'/'air' 不算命中 python", v == "REMOVE", f"ov={ov} hits={hits}")

print("== #6 已知边界：3 字子串命中即放行（宁可漏杀，不误杀）==")
v, ov, hits = verdict("## 来源: https://x/a\n\n固态电容常用于主板供电电路。", Q)
check("含'固态电'的页放行（记为已知局限）", v == "KEEP", f"ov={ov} hits={hits}")

print("== #7 V10.7 果蔬误杀回归：只共享 2 字核心词的高相关页放行 ==")
# 2026-10-14 14:58 轮真实被移出的样本（日志 Off-topic removed, shared 2 chars），
# 正文重写为不含「酶活性」3 字连续片段的形态 —— 旧片段闸必杀，新词级信号必救。
QF = "果蔬 采后 病害 侵染 生理 防御 酶活性"
rescued = [
    ("中科院（cas.cn）",
     "## 来源: https://www.cas.cn/syky/202606/t20260617_5112740.shtml\n\n"
     "荔枝采后病害控制研究获进展——团队解析病原菌侵染过程的调控机制。"),
    ("农业工程学报（tcsae.org）",
     "## 来源: http://tcsae.org/cn/article/id/20030405\n\n"
     "酵母菌对果蔬采后病害防治的研究进展……拮抗酵母在生物防治中的应用效果。"),
    ("食品工业科技 DOI 页（spgykj.com）",
     "## 来源: https://www.spgykj.com/article/doi/10.13386/j.issn1002-0306.2024110105?viewType=HTML\n\n"
     "芽孢杆菌防控果蔬采后侵染性病害及其作用机制研究进展……处理组相关酶的活性显著提高。"),
]
for name, t in rescued:
    v, ov, hits = verdict(t, QF)
    check(f"救援放行：{name}", v == "KEEP" and hits >= 2, f"ov={ov} hits={hits}")

print("== #8 词级信号不豁免真跑题：命中 0~1 词仍由片段闸移出 ==")
offtopic_qf = [
    ("量子计算页", "## 来源: https://x/a\n\n量子计算芯片的低温封装工艺与稀释制冷机选型指南。"),
    ("仅命中单词的擦边页", "## 来源: https://x/b\n\n园艺产品的采后处理技术概述，不涉及病原与防控。"),
]
for name, t in offtopic_qf:
    v, ov, hits = verdict(t, QF)
    check(f"跑题拦截：{name}", v == "REMOVE", f"ov={ov} hits={hits}")

print("== #9 词级信号永不新增移出：纯 2 字词 query 仍 fail-open ==")
# 全 2 字词 query 下片段闸本就返回 None（无判别力）——词级信号只做救援，
# 不得让这种 query 开始移出页面（行为与 V10.6 完全一致）。
v, ov, hits = verdict("## 来源: https://x/a\n\n完全无关的正文。", "果蔬 采后 病害")
check("纯 2 字词 query 放行不变", ov is None and v == "KEEP", f"ov={ov} hits={hits}")
# 单词 query（<2 有效词）：词级信号不成立（None），回退片段信号
v, ov, hits = verdict("## 来源: https://x/a\n\n固态电容用于主板供电。", "固态电池")
check("单词 query 回退片段信号", hits is None and v == "KEEP", f"ov={ov} hits={hits}")

sys.exit(summary())
