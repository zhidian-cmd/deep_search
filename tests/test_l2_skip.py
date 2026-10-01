# -*- coding: utf-8 -*-
"""L2 跳过判据回归测试（V9.12，2026-09-22）。

背景：实测 fetch 阶段 23.6~35.5s 里，尾巴**全部**由"注定失败"的条目决定 ——
两级都 403/404 的硬阻断，以及 curl_cffi 解不动的 PDF。L2（隐身浏览器）唯一
独有的能力是执行 JS，这两类它救不了，却要占并发槽一路挂到熔断。本测试锁住
判据的**边界**：该跳的跳，不该跳的不能跳。

⚠️ PDF 支链（落盘留档）的测试在 `tests/test_pdf_branch.py` —— V9.13 起 PDF 不再
解析正文、不再进正文池，与本节判据是两件事，别混在一起改。

不触外网：全是纯函数断言。
"""
import sys

from _harness import check, summary

from deep_search import web_fetch as W


def main():
    print("== 1. 魔数判据（唯一可信的 PDF 信号）==")
    check("标准头", W._is_pdf_payload(b"%PDF-1.7\n...") is True)
    check("头前有垃圾字节也认（1024 窗口内查找）",
          W._is_pdf_payload(b"\x00\x01junk%PDF-1.5\n") is True)
    check("HTML 不认", W._is_pdf_payload(b"<html><body>x</body></html>") is False)
    check("空体不认", W._is_pdf_payload(b"") is False)
    check("None 不认", W._is_pdf_payload(None) is False)

    print("== 2. 硬阻断：404 单级即判死；403 必须双级 ==")
    check("L0 404 → 跳", bool(W._skip_l2_reason(404, None, "", "http://a/b")))
    check("L1 404 → 跳", bool(W._skip_l2_reason(None, 404, "", "http://a/b")))
    check("L0 410 → 跳", bool(W._skip_l2_reason(410, None, "", "http://a/b")))
    check("两级 403 → 跳", bool(W._skip_l2_reason(403, 403, "", "http://a/b")))
    check("两级 451 → 跳", bool(W._skip_l2_reason(451, 451, "", "http://a/b")))
    # 这两条是本测试的重点：单级 403 不足以判死，否则会把整类 WAF 站误杀
    check("仅 L0 403 → 不跳（httpx 无 TLS 指纹，被 WAF 403 是常态）",
          W._skip_l2_reason(403, None, "", "http://a/b") == "")
    check("仅 L1 403 → 不跳（curl_cffi 被拦，浏览器仍有救回可能）",
          W._skip_l2_reason(None, 403, "", "http://a/b") == "")

    print("== 3. 二进制解码失败：必须带 .pdf 限定 ==")
    dec = "'utf-8' codec can't decode byte 0xb5 in position 26: invalid start byte"
    check("解码失败 + .pdf → 跳",
          bool(W._skip_l2_reason(200, None, dec, "https://a/b/x.pdf")))
    check("解码失败 + 查询串里的 .pdf → 跳",
          bool(W._skip_l2_reason(200, None, dec, "https://a/blob?filename=x.pdf")))
    # PDF 藏在端点后面、没有后缀可依（实测 eol.ctbu.edu.cn/.../resPdfShow.do）
    check("解码失败 + 端点式 PDF（无 .pdf 后缀）→ 跳",
          bool(W._skip_l2_reason(200, None, dec, "https://a/meol/resPdfShow.do?resId=1")))
    # GBK 页被站点误标 charset 时抛的是同一个异常，浏览器恰恰能救 → 不能跳
    check("解码失败但不是 PDF → 不跳（GBK 页误标 charset，浏览器能救）",
          W._skip_l2_reason(200, None, dec, "https://a/b.html") == "")
    check("是 PDF 但不是解码失败 → 不跳（交给 L2 试）",
          W._skip_l2_reason(200, None, "timeout", "https://a/b.pdf") == "")

    print("== 4. 正常失败路径不被误伤 ==")
    check("连接失败（无 status 无 error）→ 不跳",
          W._skip_l2_reason(None, None, "", "http://a/b") == "")
    check("200 但无正文（SPA）→ 不跳",
          W._skip_l2_reason(200, 200, "", "http://a/b") == "")
    check("L1 500 → 不跳（瞬时错误，浏览器可能拿到）",
          W._skip_l2_reason(200, 500, "", "http://a/b") == "")

    return summary()


sys.exit(main())
