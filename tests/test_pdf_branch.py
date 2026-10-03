# -*- coding: utf-8 -*-
"""PDF 支链回归测试（V9.13，2026-09-22 / 标题口径见下）。

背景：PDF 抽出的正文在 content_score 眼里是"字多≠好"——无 DOM、扁平阅读序，
页眉页脚页码周期性重复、跨页断句、表格散架，噪声与正文同分（实测 294451 字的
那份仍拿 0.88）；截断只能字符级硬切；一条 29 万字 PDF 还是整批共用的
max_total_chars(60000) 的 5 倍。结论是它**不该和 HTML 一起排序**，故 V9.13 把
它整条移出正文池：原文件落盘留档，交付只给「标题 + 路径」，正文由调用方按需读。

本测试锁三件事：
  ① 落盘的是**原字节**（不是解析后的文本）、**文件名取自解析出的标题**（不是域名），
     且同 URL 幂等；
  ② 清单字段齐全、标题有兜底链（PDF 元数据 → 首页文本 → URL 末段）；命名剥掉标题
     自带的 `.pdf`、同名标题靠 hash 区分、连名字都挑不出时退回 `pdf_<hash>`；
  ③ 支链语义：`_PDF_DONE` 哨兵出链、不进 fetch_all 返回值、**不记 fetch_ok=False**
     （PDF 落盘是成功，记失败会把它域的自适应降权带偏）。

不触外网（httpx client 打成假件）、不写生产 temp/pdf（PDF_DIR 重定向到临时目录）、
penalty_delta 打桩。
"""
import asyncio, contextlib, glob, os, re, sys, tempfile

from _harness import check, summary

from deep_search.config import DeepSearchConfig
from deep_search import web_fetch as W


def make_pdf(page_texts):
    """内存合成 PDF（每项一页），返回字节。

    用真 PDF 库现造而不是塞一段假头部：本测试要验的正是"从 PDF 里读出**页数**与
    **首页文本标题**"，那种假数据验不了。
    """
    import pymupdf
    doc = pymupdf.open()
    for t in page_texts:
        page = doc.new_page()
        page.insert_text((40, 60), t, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


class _FakeResp:
    def __init__(self, body, status):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")


class _FakeClient:
    """只回一个固定响应的 httpx.AsyncClient 替身（够 _httpx_first 用）。"""
    def __init__(self, body, status):
        self._body, self._status = body, status
        self.is_closed = False

    async def get(self, url):
        return _FakeResp(self._body, self._status)

    async def aclose(self):
        self.is_closed = True


@contextlib.contextmanager
def fake_httpx(body, status=200):
    real = W.httpx.AsyncClient
    W.httpx.AsyncClient = lambda *a, **k: _FakeClient(body, status)
    try:
        yield
    finally:
        W.httpx.AsyncClient = real


async def main():
    W.filters_learn.penalty_delta = lambda *a, **k: 0.0   # 学习层打桩，不落盘

    real_pdf_dir = W.PDF_DIR
    tmp = tempfile.mkdtemp()
    W.PDF_DIR = tmp                       # _dump_pdf 读模块全局，改写即重定向
    cfg = DeepSearchConfig()
    mgr = W.ScraplingManager(cfg)
    pdf = make_pdf(["GB 4789.17-2024 food microbiology sampling standard", "page two"])

    try:
        print("== 1. 元信息：页数 + 标题兜底链 ==")
        meta = mgr._pdf_meta(pdf, "https://a/x.pdf")
        check("页数正确", meta["pages"] == 2, f"pages={meta['pages']}")
        check("元数据标题为空时取首页文本",
              "GB 4789.17-2024" in meta["title"], repr(meta["title"][:50]))
        bad = mgr._pdf_meta(b"not a pdf at all", "https://a/GB4789.1-2016.pdf")
        check("打开失败时不抛、用 URL 末段兜底",
              bad["title"] == "GB4789.1-2016.pdf" and bad["pages"] == 0, repr(bad))

        print("== 1b. 元数据标题是泛词时，不算数、走首页文本兜底 ==")
        # 实测案例：一份 WS/T 805—2022 的元数据 title 就是「行业标准」—— 非空但无用
        import pymupdf as _pm
        _d = _pm.open()
        _p = _d.new_page()
        _p.insert_text((40, 60), "GB 4789.17-2024 meat products sampling standard", fontsize=9)
        _d.set_metadata({"title": "行业标准"})
        generic_pdf = _d.tobytes()
        _d.close()
        g = mgr._pdf_meta(generic_pdf, "https://a/g.pdf")
        check("泛词标题被当没标题，取首页文本",
              "GB 4789.17-2024" in g["title"], repr(g["title"][:50]))
        W.PDF_DIR = os.path.join(tmp, "generic")   # 独立目录落盘，不干扰第 2 节的文件计数
        eg = mgr._dump_pdf(generic_pdf, "https://a/generic.pdf")
        W.PDF_DIR = tmp
        check("文件名不再叫「行业标准_…」",
              bool(eg) and not os.path.basename(eg["path"]).startswith("行业标准"),
              os.path.basename(eg["path"]) if eg else "None")
        check("Office 导出前缀同样算泛词",
              W._generic_pdf_title("Microsoft Word - report") and not W._generic_pdf_title("GB 4789.1—2016"))

        print("== 2. 落盘：原字节 + 命名 + 幂等 + 字段齐全 ==")
        entry = mgr._dump_pdf(pdf, "https://a/x.pdf")
        check("返回清单条目", isinstance(entry, dict), repr(entry)[:60])
        check("文件已写入（重定向后的临时目录）",
              bool(entry) and os.path.isfile(entry["path"]))
        check("落盘的是**原字节**，不是解析后的文本",
              bool(entry) and open(entry["path"], "rb").read() == pdf)
        check("字节数记录正确", bool(entry) and entry["bytes"] == len(pdf))
        check("字段齐全 url/path/bytes/pages/title（V10.3 起 + digest/key/reused）",
              bool(entry) and {"url", "path", "bytes", "pages", "title"} <= set(entry)
              and {"digest", "key", "reused"} <= set(entry),
              repr(sorted(entry)) if entry else "")
        check("元信息随落盘一并进入清单（pages/title）",
              bool(entry) and entry["pages"] == 2 and "GB 4789.17-2024" in entry["title"],
              repr({k: entry[k] for k in ("pages", "title")}) if entry else "")
        bn = os.path.basename(entry["path"])
        check("文件名用**解析出的标题**，不是域名",
              bn.startswith("GB 4789.17-2024") and "a_" not in bn, bn)
        check("文件名以 _<8位hash>.pdf 结尾（同 URL 幂等、撞名不覆盖）",
              re.fullmatch(r".+_[0-9a-f]{8}\.pdf", bn) is not None, bn)
        check("标题里不会叠出 .pdf_.pdf", ".pdf_" not in bn, bn)
        n_before = len(glob.glob(os.path.join(tmp, "*.pdf")))
        mgr._dump_pdf(pdf, "https://a/x.pdf")
        check("同 URL 幂等（重跑覆盖同一路径，不堆副本）",
              len(glob.glob(os.path.join(tmp, "*.pdf"))) == n_before,
              f"files={len(glob.glob(os.path.join(tmp, '*.pdf')))} (前 {n_before})")

        print("== 2b. 命名：撞名不覆盖 / 剥后缀 / 挑不出名字时的兜底 ==")
        # 同一份 PDF、不同 URL → 标题相同，靠 hash 区分，两个文件都得在
        mgr._dump_pdf(pdf, "https://other.com/y.pdf")
        files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(tmp, "*.pdf")))
        check("同名标题不互相覆盖（hash 区分）", len(files) == 2, files)
        # 标题的最后一档兜底是 URL 末段，自带 .pdf —— 必须剥掉再把 .pdf 拼回去
        legacy = mgr._dump_pdf(b"not a pdf", "https://a/GB4789.1-2016.pdf")
        check("URL 末段兜底时剥掉自带后缀（GB4789.1-2016_xxx.pdf）",
              bool(legacy) and re.fullmatch(r"GB4789\.1-2016_[0-9a-f]{8}\.pdf",
                                            os.path.basename(legacy["path"])) is not None,
              os.path.basename(legacy["path"]) if legacy else "None")
        # 连 URL 末段都挑不出（形如 https://a/）→ 退回 pdf_<hash>.pdf
        blank = mgr._dump_pdf(b"not a pdf", "https://a/")
        check("挑不出名字时退回 pdf_<hash>.pdf",
              bool(blank) and re.fullmatch(r"pdf_[0-9a-f]{8}\.pdf",
                                           os.path.basename(blank["path"])) is not None,
              os.path.basename(blank["path"]) if blank else "None")

        print("== 3. 落盘失败：只丢本条，绝不抛 ==")
        W.PDF_DIR = os.path.join(tmp, "bad\x00dir")     # 非法路径
        try:
            failed = mgr._dump_pdf(pdf, "https://a/y.pdf")
            check("返回 None 而不抛异常", failed is None)
        except Exception as e:
            check("返回 None 而不抛异常", False, f"抛了 {type(e).__name__}: {e}")
        finally:
            W.PDF_DIR = tmp

        print("== 4. 支链语义：哨兵出链，不走 L1/L2 ==")
        mgr2 = W.ScraplingManager(cfg)
        with fake_httpx(pdf):
            got = await mgr2._httpx_first("https://a/z.pdf", {})
        check("_httpx_first 返回三元组，text 位是 _PDF_DONE 哨兵",
              isinstance(got, tuple) and len(got) == 3 and got[0] is W._PDF_DONE,
              f"len={len(got) if isinstance(got, tuple) else '?'} text={got[0]!r}")
        check("标题位为 None（PDF 不产正文，不进正文池）", got[1] is None)
        check("清单已登记 1 条", len(mgr2.pdf_sources) == 1)
        with fake_httpx(pdf):
            chain = await mgr2._fetch_chain("https://a/z2.pdf", {}, 0, 0)
        check("_fetch_chain 返回 _PDF_DONE（不升级 L1/L2）",
              chain is W._PDF_DONE, repr(chain)[:40])

        print("== 5. fetch_all：PDF 不进返回值、不记失败 ==")
        learn_calls = []
        W.filters_learn.penalty_delta = lambda *a, **k: learn_calls.append(a) or 0.0
        mgr3 = W.ScraplingManager(cfg)
        with fake_httpx(pdf):
            res = await mgr3.fetch_all(["https://a/p.pdf"], fetch_count=1)
        check("返回值为空（PDF 不进正文池）", res == [], f"len={len(res)}")
        check("清单有 1 条", len(mgr3.pdf_sources) == 1)
        check("**没有**记 fetch_ok=False（落盘是成功，不是抓取失败）",
              learn_calls == [], f"calls={learn_calls}")

        print("== 6. 对照：HTML 响应不触发支链 ==")
        html = (b"<html><head><title>T</title></head><body>"
                + b"<p>hello world this is a normal article body</p>" * 30
                + b"</body></html>")
        mgr4 = W.ScraplingManager(cfg)
        with fake_httpx(html):
            got4 = await mgr4._httpx_first("https://a/h.html", {})
        check("text 位不是哨兵、且拿到了正文",
              isinstance(got4[0], str) and bool(got4[0]), repr(got4[0])[:40])
        check("标题位有值（页面 <title>）", got4[1] == "T", repr(got4[1]))
        check("未产生 pdf_sources", mgr4.pdf_sources == [], repr(mgr4.pdf_sources))

    finally:
        W.PDF_DIR = real_pdf_dir
        for f in glob.glob(os.path.join(tmp, "*")):
            try: os.remove(f)
            except Exception: pass
        try: os.rmdir(tmp)
        except Exception: pass

    return summary()


sys.exit(asyncio.run(main()))
