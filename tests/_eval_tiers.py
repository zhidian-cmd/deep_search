# -*- coding: utf-8 -*-
"""人工标注：本次会话 8 轮真实检索的 46 个 host → 权威度层级（调参与验收共用）。

层级口径（0~1）：
  0.95  gov.cn / .edu.cn / 科研院所 / 标准平台 / 学会期刊 / 国际研究所
  0.62~0.72  主流媒体 / 权威企业官网
  0.55~0.58  百科
  0.45~0.52  垂直内容 / 健康门户 / 正版书籍转载平台
  0.35~0.38  问答 / 自媒体 / 论文库
  0.28  文库下载站 / 厂商营销 / 内容农场
未知 host 兜底 0.50（不奖不罚）。
"""
TIERS = {
    # ---- 0.95 ----
    "lscb.shandong.gov.cn": .95, "wjw.hubei.gov.cn": .95, "lshwzcbj.hunan.gov.cn": .95,
    "ndls.org.cn": .95, "zwxb.chinacrops.org": .95, "ricesci.cn": .95,
    "gxaas.net": .95, "haas.cn": .95, "cgm.haut.edu.cn": .95,
    "knowledgebank.irri.org": .95,
    # ---- 0.62~0.72 ----
    "thepaper.cn": .72, "cyol.com": .72, "huxiu.com": .68,
    "shimadzu.com.cn": .70, "cnrice.com.cn": .70, "antpedia.com": .62,
    # ---- 0.55~0.58 ----
    "baike.baidu.com": .58, "baike.baidu.hk": .58, "yixue.com": .58,
    "newton.com.tw": .55,
    # ---- 0.45~0.52 ----
    "health.baidu.com": .48, "youlai.cn": .48, "maigoo.com": .45,
    "jucanw.com": .45, "miaoshou.net": .45, "hbcbly.com": .48,
    "imarket.qq.com": .52, "nwczrj.qq.com": .52, "m.inf.qq.com": .52,
    "mwenku.read.qq.com": .52, "inews.qq.com": .55, "page.sm.cn": .45,
    "gf.cabr-fire.com": .50,
    # ---- 0.35~0.38 ----
    "weibo.com": .38, "blog.sina.com.cn": .35, "nongyelu.com": .38,
    "360qiwen.com": .35, "knowcat.cn": .38, "gwyoo.com": .38,
    "qianqiantushu.com": .35, "6miu.com": .38,
    # ---- 0.28 ----
    "cucdc.com": .28, "ricemillmachinerys.com": .28,
    "taizyagromachine.com": .28, "pwsannong.com": .28,
}


def auth_of(url: str) -> float:
    """host → 权威度层级。最长后缀匹配；www. 前缀归一；未知 0.50。"""
    host = (url or "").lower().split("//")[-1].split("/")[0]
    host = re.sub(r"^www\.", "", host)
    best = None
    for dom, t in TIERS.items():
        d = re.sub(r"^www\.", "", dom)
        if host == d or host.endswith("." + d):
            if best is None or len(d) > len(best[0]):
                best = (d, t)
    return best[1] if best else 0.50


# 人工跑题/低质标注（url 子串 → rel；1=切题 0.5=边缘 0=跑题；未标注=1）
REL = {
    "shimadzu.com.cn": 0.0,    # 质谱仪器新闻：讲稻米检测，不是籽粒结构
    "view.inews.qq.com": 0.5,  # 稻米产业新闻
    "antpedia.com": 0.5,       # 检测行业资讯
    "nongyelu.com": 0.5,       # 问答页
    "360qiwen.com": 0.5,       # 泛泛而谈的稻谷加工
    "taizyagromachine.com": 0.5,
    "qianqiantushu.com": 0.0,  # 图书目录页，无正文
    "cucdc.com": 0.5,          # 文库流出的流程罗列
    "ricemillmachinerys.com": 0.5,
    "6miu.com": 0.5,           # 专利文书
    "gwyoo.com": 0.5,
    "gf.cabr-fire.com": 0.5,
}


def rel_of(url: str) -> float:
    for k, v in REL.items():
        if k in (url or ""):
            return v
    return 1.0
