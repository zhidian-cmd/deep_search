# -*- coding: utf-8 -*-
"""归档（markdown）与段落重组的离线测试。

锁三件事：
  ① **重组是纯空白改动** —— 去掉所有空白后必须一字不差（这条不变量是"在哪儿分段
     不用判断"的全部依据，故单独用真实形状的样本压）；
  ② 分段规则：源站硬折的续行接回上一行、短行（小标题）不接、结构行不动、
     **渲染后**（空行分段）不再出现几千字一块；
  ③ `_save_archive` 的产物形态：全 markdown（无 HTML 标签）、来源标记按首次出现
     编号、整节截断、元信息行的 `来源:` 有值（曾因读错层级一直是空串）。

不写生产目录（ARCHIVE_DIR 重定向到临时目录）。
"""
import os
import re
import sys
import tempfile

from _harness import check, summary

from deep_search import helpers
from deep_search import config as C

C.ARCHIVE_DIR = tempfile.mkdtemp()
from deep_search.config import DeepSearchConfig          # noqa: E402
from deep_search import skill as S                       # noqa: E402

S.ARCHIVE_DIR = C.ARCHIVE_DIR


def nows(s: str) -> str:
    """去掉所有空白 —— 重组不变量就比这个。"""
    return "".join(s.split())


def render_paragraphs(md: str):
    """粗略模拟 markdown 渲染：空行分段，段内单换行合并。用来量"渲染后是不是一坨"。"""
    paras, cur = [], []
    for line in md.split("\n"):
        if line.strip():
            cur.append(line.strip())
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return paras


def main():
    print("== 1. 重组：纯空白改动 ==")
    src = "\n".join([
        "3.1.1 样品的采集应遵循随机性、代表性的原则。",
        "3.2 采样方案",
        "3.2.2 采样方案分为二级和三级采样方案。",
        "例如:n=5,c=2,m=100 CFU/g,M=1 000 CFU/g。含义是从一批产品中采集5个样品,若",   # 硬折（无标点）
        "5个样品的检验结果均小于或等于m值(≤100CFU/g),则这种情况是允许的;若≤2个样品的结果",  # 硬折续行
        "(X)位于m值和M值之间(100CFU/g<X≤1 000 CFU/g),则这种情况也是允许的;",
        "3.2.3 各类食品的采样方案按食品安全相关标准的规定执行。",
    ])
    out = helpers.reflow_paragraphs(src)
    check("去掉空白后一字不差", nows(out) == nows(src))

    print("== 2. 分段规则 ==")
    paras = render_paragraphs(out)
    check("渲染成多段（不是一坨）", len(paras) >= 5, f"段数={len(paras)}")
    check("硬折的续行接回同一段（'…若' 不单独成段）",
          any("若5个样品的检验结果" in "".join(p) for p in paras))
    check("续行没被接成残句（'为允许的' 这类半句不单独成段）",
          not any("".join(p).endswith("结果") or "".join(p).startswith("(X)") for p in paras))
    check("短标题不接正文（'3.2 采样方案' 自己一段）",
          any(p == ["3.2 采样方案"] for p in paras),
          repr([p for p in paras if "采样方案" in p[0]][:2]))

    print("== 3. 超长段落切分 ==")
    long_para = ("3.1.1 样品的采集应遵循随机性、代表性的原则。" * 6)
    out2 = helpers.reflow_paragraphs(long_para)
    check("长段切成多段", len(render_paragraphs(out2)) >= 2,
          f"段数={len(render_paragraphs(out2))}")
    check("切分后仍一字不差", nows(out2) == nows(long_para))

    # 真实样本：抓取时丢了标点的 955 字一条（归档里实测存在），只能按字符数硬折
    nopunct = "样品的采集与处理食品检验样品采集的原则：1所采样品应具有代表性每批食品应随机抽取一定数量的样品" * 12
    out3 = helpers.reflow_paragraphs(nopunct)
    ps3 = render_paragraphs(out3)
    check("无标点长段也切（走字符数硬折）", len(ps3) > 1, f"段数={len(ps3)}")
    check("硬折后仍一字不差", nows(out3) == nows(nopunct))
    # 该段整段无句末标点 → 硬折上限 = _PARA_LIMIT
    check("无标点段每段不超过 120 字",
          all(len("".join(p)) <= 120 for p in ps3),
          f"最长={max(len(''.join(p)) for p in ps3)}")

    print("== 4. 结构行不动 ==")
    table = "| 项目 | 2010 版 | 2016 版 |"
    code = "```python"
    body = "def f():  return 1"
    link = "[某标准全文](https://example.com/a/b)"
    lst = "- 列表项"
    text = "正文一句。"
    out4 = helpers.reflow_paragraphs("\n".join([table, code, body, "```", link, lst, text]))
    check("表格行原样保留", table in out4)
    check("代码围栏内容原样保留（不被并进正文）", body in out4)
    check("含链接的行原样保留", link in out4)
    check("列表项原样保留", lst in out4)
    kinds = [render_paragraphs(out4)[i][0] for i in range(len(render_paragraphs(out4)))]
    check("表格/代码/链接/列表各自成段，未与正文混段",
          sum(1 for p in render_paragraphs(out4) if len(p) > 1) == 1 and
          len(render_paragraphs(out4)) >= 6, repr(kinds))

    print("== 5. _save_archive 产物形态 ==")
    sections = [
        f"## 来源: https://s{i}.example.com/a/b\n\n第 {i} 篇正文。{'很长的内容。' * 40}\n"
        for i in (1, 2, 3)
    ]
    body = "\n\n---\n\n".join(sections)
    cfg = DeepSearchConfig()
    cfg.archive_max_sources = 2
    sk = S.DeepSearchSkill(cfg)
    result = {
        "source": "scrapling_filtered",
        "answer": body,
        "metadata": {
            "_full_combined": body,
            "elapsed": 1.5,
            "fetched_count": 3,
            "search_urls": ["https://s1.example.com/a/b"],
            "pdf_sources": [{
                "title": "GB 4789.1—2016", "path": "D:\\x\\a.pdf",
                "bytes": 2048, "pages": 3, "url": "https://std.example.com/a.pdf",
            }],
        },
    }
    path = sk._save_archive("测试 查询", result)
    check("落盘成功", bool(path) and os.path.isfile(path), repr(path))
    doc = open(path, encoding="utf-8").read()
    check("后缀 .md", path.endswith(".md"))
    # 落盘位置：本会话一个时间戳文件夹（年月日+时分）+ 以关键词命名的文件（轮次序号前缀）
    sess = os.path.dirname(path)
    check("文件放在时间戳文件夹里（2026年 09月23日 22：09 式，月/日补零）",
          os.path.dirname(sess) == C.ARCHIVE_DIR and
          re.fullmatch(r"\d{4}年 \d{2}月\d{2}日 \d{2}：\d{2}", os.path.basename(sess)) is not None,
          os.path.basename(sess))
    check("文件夹名用的是**全角**冒号（半角 : 在 Windows 上建不出来）",
          "：" in os.path.basename(sess) and ":" not in os.path.basename(sess),
          os.path.basename(sess))
    check("文件以关键词命名（带轮次序号、关键词间是空格）",
          os.path.basename(path) == "01_测试 查询.md", os.path.basename(path))
    sk._save_archive("测试 查询", result)
    check("同一会话第二个文件落在同一文件夹里、序号递增",
          sorted(os.listdir(sess)) == ["01_测试 查询.md", "02_测试 查询.md"],
          sorted(os.listdir(sess)))
    # 关键词里的 Windows 非法字符（冒号很常见：GB 4789.1:2016）—— 原先只替换空格与斜杠
    p_bad = sk._save_archive("GB 4789.1:2016 标准/解读", result)
    check("含 : 和 / 的关键词也能落盘（原先会直接失败）",
          bool(p_bad) and os.path.isfile(p_bad), repr(p_bad))
    check("非法字符被换成 -",
          os.path.basename(p_bad) == "03_GB 4789.1-2016 标准-解读.md", os.path.basename(p_bad))
    check("空关键词有兜底名", S._archive_filename("   ") == "query", S._archive_filename("   "))
    check("空白归一（多个空格压成一个、首尾不留）",
          S._archive_filename("  a   b  ") == "a b", S._archive_filename("  a   b  "))
    # 时间戳只到分钟 → 同一分钟内重启的会话会落到同一文件夹，序号必须续号而不是覆盖。
    # 把取时间戳的函数固定成"就是刚才那个文件夹"，这样无论测试跑多久都是同一目录。
    S._ARCHIVE_SESSION_DIR, S._ARCHIVE_SEQ = None, 0
    _orig_stamp = S._archive_stamp
    S._archive_stamp = lambda: os.path.basename(sess)
    try:
        p_resume = sk._save_archive("测试 查询", result)
    finally:
        S._archive_stamp = _orig_stamp
    check("同分钟新会话续号，不覆盖上一个会话的文件",
          os.path.basename(p_resume) == "04_测试 查询.md", os.path.basename(p_resume))
    check("目录里 4 个文件都在（没被覆盖）", len(os.listdir(sess)) == 4, sorted(os.listdir(sess)))
    check("全 markdown（无 HTML 标签）", "<p>" not in doc and "<h1>" not in doc)
    check("元信息行的来源有值（曾一直是空）", "来源: scrapling_filtered |" in doc,
          doc.split("\n")[2][:60])
    check("PDF 区含页数", "3 页" in doc)
    check("来源标记按首次出现编号", "**[1] s1.example.com**" in doc and "**[2] s2.example.com**" in doc)
    check("整节截断：第 3 个来源整节丢弃", "s3.example.com" not in doc)
    check("引用条数 = 实际上限", "引用: 2 条" in doc)
    check("PDF 区两行都在页数行前（列表没被段落重组拆散）",
          "- **GB 4789.1—2016**" in doc and "  - 2 KB · 3 页 · 未纳入正文" in doc)
    # 摘要行、标题、来源标记各自成段 —— 渲染后不会粘成一整块
    heads = render_paragraphs(doc)
    check("渲染后标题/元信息/来源标记各自成段",
          ["# 测试 查询"] in heads and any(p[0].startswith("来源: scrapling") for p in heads)
          and any(p == ["**[1] s1.example.com**"] for p in heads),
          repr([p for p in heads if p[0].startswith(("**[", "# ", "来源:"))][:4]))
    # 渲染后每一段的字数上限（'很长的内容。' 相连的一段最多 2×上限）
    worst = max(len("".join(p)) for p in heads)
    check("渲染后没有超长块（≤240 字）", worst <= 240, f"最长={worst}")
    # 折行不变量：被保留的每一节，去掉空白后必须一字不差地出现在归档里。
    # 逐节比、而非整段比 —— 归档会在每节标题上方插入 `**[n] 域名**` 标记，
    # 整段拼起来会被那些新增字符打断（第 3 节则被上限整节丢弃）。
    for idx, sec in enumerate(sections[:2], 1):
        check(f"第 {idx} 节正文重组后一字不差", nows(sec) in nows(doc))

    print("== 6. 兜底路径（没有 ## 来源: 头）==")
    r2 = {"source": "exa_fallback", "answer": "摘要正文",
          "metadata": {"fetched_count": 0, "search_urls": ["https://a.com/1", "https://b.com/2"]}}
    doc2 = open(sk._save_archive("q", r2), encoding="utf-8").read()
    check("列出候选来源", "## 候选来源（本次未抓到正文）" in doc2 and "https://a.com/1" in doc2)
    check("候选来源是列表（没被拆成多段）",
          "\n- https://a.com/1\n- https://b.com/2\n" in doc2, repr(doc2[-120:]))

    return summary()


sys.exit(main())
