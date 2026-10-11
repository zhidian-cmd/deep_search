# -*- coding: utf-8 -*-
"""V10.6 四项修改的功能级验证（不触网，用已落盘的真实 PDF）。"""
import sys
sys.path.insert(0, r"D:\Runtimelibrary\SERVER\MCP")

from deep_search import web_fetch as W
from deep_search import filters
from deep_search.config import DeepSearchConfig

ok = 0
fail = 0

def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name} {detail}")
    else:
        fail += 1
        print(f"  FAIL  {name} {detail}")

print("== 1. 标题泛词关：排版工程残留名 ==")
cases = [
    ("23期食品广告.indd", True),          # 本次实测案例
    ("untitled", True),
    ("report_final", True),
    ("毕业论文打印稿.docx", True),
    ("宰后肉品嫩化技术及其作用机理研究进展", False),
    ("GB 4789.17-2024 食品微生物学检验", False),
    ("2024年度肉制品行业研究报告", False),
]
for t, want in cases:
    got = W._generic_pdf_title(t)
    check(repr(t), got == want, f"-> polluted={got}")

print("== 2. 真实 PDF：新摘要（2400 额度 + 均匀分段 + 小节目录） ==")
pdf_path = r"D:\Runtimelibrary\SERVER\MCP\deep_search\temp\pdf\23期食品广告.indd_62da4197.pdf"
data = open(pdf_path, "rb").read()

import logging

class ScraplingManagerStub:
    config = DeepSearchConfig()
    _current_query = "肉僵直 解僵 过程 原理"
    logger = logging.getLogger("verify")
    _pdf_digest = W.ScraplingManager._pdf_digest
    _DIGEST_KW = W.ScraplingManager._DIGEST_KW

stub = ScraplingManagerStub()
meta = W.ScraplingManager._pdf_meta(stub, data, "https://example.com/23期食品广告.indd_62da4197.pdf")
print("  title:", repr(meta["title"]))
print("  title_source:", meta["title_source"])
print("  digest 长度:", len(meta["digest"]))
print("  --- digest 内容 ---")
print("  " + meta["digest"].replace("\n", "\n  ")[:1500])
print("  -------------------")
check("标题穿到首页兜底（非 .indd 残留名）", "食品广告" not in meta["title"] and len(meta["title"]) > 8)
check("title_source=page", meta["title_source"] == "page")
check("摘要比旧版显著变长（>1800 字符）", len(meta["digest"]) > 1800, f"len={len(meta['digest'])}")
check("摘要含小节目录", "【小节目录】" in meta["digest"])
check("关键片段多个（≥3 个省略号分隔）", meta["digest"].count("……") >= 2)
check("摘要长度不超单份额度", len(meta["digest"]) <= ScraplingManagerStub().config.pdf_digest_max_per_pdf + 50)

print("== 3. 权威度：百科降档 + 期刊补录 + 学术特征加分 ==")
check("百科 0.58→0.50", filters.authority_score("https://baike.baidu.com/item/x") == 0.50)
check("期刊集群补录 0.82", filters.authority_score("https://xdspkj.ijournals.cn/html/2023/3/p.html") == 0.82)
check("粮油期刊补录 0.70", filters.authority_score("http://www.mpmpc.cn/magarc/p.html") == 0.70)
scholar_text = ("doi:10.1234/food.2023.456  ISSN 1000-3060  收稿日期：2023-01-05  "
                "基金项目：国家自然科学基金（32170001）  中图分类号：TS251.1")
s1 = filters.authority_score_ex("https://unknown-journal.example.cn/a/paper.html", scholar_text)
check("未知学术站 0.50 + 0.20 = 0.70", abs(s1 - 0.70) < 1e-9, f"got={s1}")
s2 = filters.authority_score_ex("https://unknown-journal.example.cn/a/paper.html", "普通网页正文，无学术指纹")
check("无指纹不加分", abs(s2 - 0.50) < 1e-9, f"got={s2}")
s3 = filters.authority_score_ex("https://www.gov.cn/x/a.html", scholar_text)
check("高权威站不加分（保持 0.95）", abs(s3 - 0.95) < 1e-9, f"got={s3}")
s4 = filters.authority_score_ex("https://blog.example.com/post/1", "参考文献列表 doi:10.1007/s13197-018-3251-6")
check("博客仅弱特征：DOI 号命中 +0.10", abs(s4 - 0.60) < 1e-9, f"got={s4}")

print(f"== 结果：PASS={ok} FAIL={fail} ==")
sys.exit(1 if fail else 0)
