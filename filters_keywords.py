"""抓取层正文质量过滤的常量表（关键词与打分逻辑分离）。

⚠️ 使用约束（必须遵守）：
1. 以下所有常量都只是 filters.content_score() 的输入信号，
   不是"命中即丢"的布尔过滤器。
2. URL 黑名单命中 → content_score 返回 0.0；模板强标记命中 → 0.05；
   乱码命中 → 0.0。
3. 弱标记、软信号累计扣分/加分，最终落在 0~1。
4. 域名权重用最长后缀匹配：blog.openai.com 命中 openai.com。
5. 最终由调用方决定（V9.5：单档阈值）：
   - score >= 0.4                → 保留
   - score < 0.4                 → 丢弃
6. 低价值商业页（V10.1）走独立分档（见 LOW_VALUE_* 小节）：店铺档 score
   封顶 0.2、路径档 ×0.5，并参与抓取前分区 —— 同样是降权不是拉黑。

黑名单分类口径（V8.3 裁决）：
- 黑名单**只放**：内容农场 / 文档站 / 聚合页 / 缓存镜像 / 广告追踪域。
- 官方媒体（新华网/人民网/央视等）**不在黑名单**，只在 DOMAIN_WEIGHTS 里
  体现权重 —— 它们是合法信源。
- 子串分两类，不做全 URL 子串匹配（"download." 含 "ad." 的误匹配教训）：
  - URL_BLACKLIST_HOST_SUBSTRINGS：只匹配 host；
  - URL_BLACKLIST_PATH_PATTERNS：(host子串, path子串) 同时命中才拦。
"""
from __future__ import annotations

# ========== URL 黑名单：host 子串（强负信号，命中 → score 0.0） ==========
URL_BLACKLIST_HOST_SUBSTRINGS = [
    # ---- 内容农场 / 低质自媒体 ----
    "baijiahao.baidu.com",
    "toutiao.com",
    "jianshu.com",
    "tieba.baidu.com",
    "zhidao.baidu.com",
    # ---- 文档下载站（SEO 垃圾，正文不可读）----
    "wenku.baidu.com",
    "docin.com",
    "doc88.com",
    "book118.com",
    "51wendang.com",
    "taodocs.com",
    "renrendoc.com",
    "360doc.com",
    "360doc.cn",
    "doc.mbalib.com",
    # ---- 搜索结果页 / 聚合壳（不是内容页）----
    "www.baidu.com",
    "m.baidu.com",
    "cn.bing.com",
    "www.bing.com",
    "www.google.com",
    "news.google.com",
    "www.sogou.com",
    "www.so.com",
    "webcache.googleusercontent.com",
    "cache.baiducontent.com",
    "translate.google.com",
    "r.jina.ai",
    # ---- 广告 / 追踪域（完整域名，不用 "ad."/"ads." 泛化子串）----
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "amazon-adsystem.com",
    "adservice.google.com",
    "cpro.baidu.com",
    "pos.baidu.com",
    "cbjs.baidu.com",
    "hm.baidu.com",
    "cnzz.com",
    "umeng.com",
    "talkingdata.com",
    "google-analytics.com",
    "googletagmanager.com",
    "appsflyer.com",
    "branch.io",
]

# ========== URL 黑名单：host+path 组合（同时命中才拦） ==========
# 用于"同一 host 下的特定路径是垃圾，其余路径是正常内容"的场景
URL_BLACKLIST_PATH_PATTERNS = [
    ("sohu.com", "/a/"),          # 搜狐自媒体聚合文（主站其他路径不受影响）
    ("163.com", "/dy/"),          # 网易自媒体
    ("baidu.com", "/s?"),         # 百度搜索结果页
    ("bing.com", "/search"),
    ("google.com", "/search"),
    ("sogou.com", "/web?"),
    ("so.com", "/s?"),
    ("weibo.com", "/search"),
]

# ========== 低商业价值页降权：B2B 店铺 / 采购公告 / 产品路径（V10.1，2026-09-26） ==========
# 场景（微生物仪器主题两轮实测）：设备类 query 的候选池被 B2B 店铺页与采购
# 公告污染 —— 第 2 轮 29 个抓取名额只收回 16 个过线正文（收集率 55%，历史
# 72~83%），交付清单混入 b2b168 店铺页 ×2，gov.cn 询价公告还排第 1。抓取名额
# 烧在过不了 content_score 的页面上，正是"不足全取"降级的根因。
#
# 定位：**降权不是拉黑** —— 这类页偶尔正是目标（询价公告可能恰好列出设备
# 清单），所以不进 URL_BLACKLIST 短路 0.0，分两档软处理：
#   - 店铺档 LOW_VALUE_HOST_SUBSTRINGS：纯 B2B 批发/店铺市场，几乎不可能是
#     "使用方法/维护/标准"型内容 → content_score 上限压到 LOW_VALUE_HOST_CAP；
#   - 路径档 LOW_VALUE_PATH_PATTERNS：任意 host 的采购/产品路径 → 分数 ×
#     LOW_VALUE_PATH_FACTOR（内容真强仍可过 0.4 阈值）。
# 两档共用判定 low_value_tier()（filters.py），服务两处：
#   1. 抓取前：skill 主流程把命中 URL **稳定分区**沉到候选队列尾部 —— 池子
#      深（> fetch_url_count）时一个抓取名额都不花；池子浅时照样抓（只重排
#      不丢弃，与 _interleave_by_host 同哲学）；
#   2. 抓取后：content_score 末步按档压分兜底。
# ⚠️ gov.cn 不豁免路径档（whjhq.gov.cn/xwdt/gggs/ 排第 1 的教训）：域名权重
#    与白名单管"信源可信度"，管不了"页面类型是否跑题"。
# ⚠️ 路径匹配对 unquote 解码后的 path+query 做 —— 引擎给的中文路径常是
#    百分号编码，不解码永远匹配不上中文模式串。
LOW_VALUE_HOST_CAP = 0.2      # 店铺档：content_score 封顶值（0.4 阈值之下）
LOW_VALUE_PATH_FACTOR = 0.5   # 路径档：content_score 乘性折扣

# 纯 B2B 市场 / 店铺域（host 子串匹配，同黑名单口径：只放完整域名，防
# "download." 含 "ad." 式误匹配）
LOW_VALUE_HOST_SUBSTRINGS = [
    "b2b168.com",           # 实测命中：lab212.cn.b2b168.com 店铺页被交付
    "gongchang.com",        # 中国工厂网
    "hc360.com",            # 慧聪网
    "1688.com",             # 阿里批发
    "made-in-china.com",
    "china.cn",             # 中国网上市场（B2B；gov.cn 不含此子串，不受影响）
    "gongye360.com",        # 中国工业电器网
]

# 路径级低价值信号：**不配 host**，任意站点的这些路径都算商业页。
# 黑名单 PATH_PATTERNS 要求 (host, path) 双命中防误伤；本表是降权不是拉黑
# （误伤代价 = 排名靠后 / 沉出抓取窗口），可以接受更宽的匹配面。
# 模式一律小写；拼音缩写不带尾斜杠的形态用两个变体覆盖（/xxx 与 xxx-）。
LOW_VALUE_PATH_PATTERNS = [
    # ---- 采购 / 招投标 / 询价公告（政府与企业站常见拼音路径段）----
    "/gggs", "gggs-",          # 公告公示（实测命中：/xwdt/gggs/18381922.html）
    "/zhaobiao", "zhaobiao-",  # 招标
    "/zhongbiao",              # 中标
    "/cggg",                   # 采购公告
    "/cgxx",                   # 采购信息
    "/xunjia",                 # 询价
    "/qiugou",                 # 求购
    "/gongying",               # 供应
    "/jiage",                  # 价格
    # ---- 通用电商 / 产品目录路径 ----
    "/product/", "/product-", "/product.",
    "/products", "/shop", "/store", "/mall",
    "/price/", "/prices", "/offer", "/supply", "/sell/",
    # ---- 中文路径段（引擎偶发返回非编码路径）----
    "询价", "招标公告", "中标公告", "采购公告", "求购",
]

# ========== 文本汤页：站点形态降权（V10.5，2026-10-07） ==========
# 场景（食品包装 4 轮 60 来源用户评审）：夸克文档预览壳（vt.quark.cn/blm/
# quark-doc-ssr）把多篇无关文档拼成一团"文本汤"，形态信号（长度/标点/段落）
# 全部失灵——同类两页一个 0.00 一个 0.98，纯看运气；问卷表单壳（wenjuan.com）
# 只有一列题目，也拿过 0.929。
# 定位：与 LOW_VALUE 同哲学——**降权不是拉黑**，封顶 LOW_VALUE_HOST_CAP。
# ⚠️ 刻意不做"正文复读率"检测：实测（同批归档标定）夸克汤页字符 10~24-gram
#    复读率 0.011，与正常文章 0.000~0.055 完全分不开——它是"多篇无关文档
#    拼接"而非"复读"，重复率假说被数据否决，只留站点形态规则。
TEXT_SOUP_HOSTS = [
    "wenjuan.com",          # 问卷表单壳（题目列表，无数据）
]

# host + path 同时命中才拦：quark 主域下 baike.quark.cn 词条是可用信源，
# 只有 /blm/quark-doc-ssr 文档预览壳是汤。
TEXT_SOUP_PATH_PATTERNS = [
    ("quark.cn", "/blm/quark-doc-ssr"),
]

# ========== 机翻报告工厂（V10.5，2026-10-07） ==========
# 场景（同上评审）：市场研究站的**中文子页**是付费报告落地页的机器翻译，数字
# 自相矛盾且换算掉零（straitsresearch 同页"10 亿美元"与"11094.4 亿美元"并存，
# zion 的 $384B 译成"384.35 亿美元"）。打标不是拦——页面仍有信息量，但调用方
# 必须知道"数字未经核实，建议查英文原页"。
# ⚠️ 只对已知报告工厂域生效 + 路径带中文区标记才打标；这些站的英文原页不打。
MT_REPORT_MILL_DOMAINS = [
    "sphericalinsights.com",
    "straitsresearch.com",
    "zionmarketresearch.com",
    "researchnester.com",
    "gminsights.com",
    "fortunebusinessinsights.com",
    "databridgemarketresearch.com",
    "mordorintelligence.com",
    "globalmarketinsights.com",
    "towardspackaging.com",
]

# ========== 弱核心词降权（V10.5，2026-10-07） ==========
# query 核心词没进「标题 + 正文前 500 字」的页判弱相关，rank_score × 本因子。
# 动机与实测案例见 filters.core_term_front_hits。是降权不是否决——真内容页
# 靠正文质量仍能翻回来。
WEAK_CORE_FACTOR = 0.45

# 前窗统计的**泛词排除表**：这些词在任何主题的市场/政策/技术检索里都会高频
# 出现，无主题判别力（实测：HALS 跑题报告前窗"市场规模"恰好 ×2，sum/max
# 口径都能压线通过；专名"食品包装"才是 0 次 vs 5+ 次的分离点）。
# 只在 core_term_front_hits 里排除，query_coverage 不受影响。
FRONT_GATE_GENERIC_TERMS = {
    "市场规模", "市场空间", "市场容量", "市场份额", "市场分析",
    "发展趋势", "趋势", "现状", "前景", "预测", "分析", "报告", "政策",
}

# ========== 模板检测：强标记（命中 → score 0.05） ==========
TEMPLATE_STRONG_MARKERS = [
    "Enable JavaScript and cookies to continue",
    "请开启JavaScript",
    "请开启 JavaScript",
    "您的浏览器版本过低",
    "登录后查看",
    "请登录",
    "立即登录",
    "注册后查看",
    "滑动验证",
    "拖动滑块",
    "人机验证",
    "访问过于频繁",
    "请求过于频繁",
    "操作太频繁",
    "系统检测到异常",
    "安全验证",
    # ⚠️ 不用裸 "Forbidden" / "Access Denied"（2026-09-12 实测修正）：
    # 它们是常见英文词 —— "Forbidden City"（故宫，连中文攻略页只要摘要里
    # 提到英文名都会中招）与 "Access Denied Errors in Linux"（讨论权限的
    # 正常文章）都会被判成模板页、内容分直接压到 0.05。
    # 改用带状态码的精确形式，与同表的 502/503/504 保持一致。
    "403 Forbidden",
    "403 Access Denied",
    "页面不存在",
    "内容已删除",
    "该内容已被发布者删除",
    "该网页无法正常运作",
    "网站维护中",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
    "未找到页面",
    "您访问的页面不存在",
    "内容不存在",
    "资源不存在",
    "权限不足",
    "需要登录",
    "登录以继续",
    "请先登录",
    "扫码登录",
    "微信扫一扫",
    "打开APP",
    "下载APP",
]

# ========== 模板检测：弱标记（累计扣分，每命中一个 -0.03，封顶 -0.30） ==========
TEMPLATE_WEAK_MARKERS = [
    "首页", "导航", "热门推荐", "相关推荐", "猜你喜欢",
    "版权声明", "免责声明", "版权所有", "All Rights Reserved", "Copyright",
    "责任编辑", "责编", "校对",
    "本文来源：", "本文来自：", "本文转载自",
    "如有侵权", "请联系删除", "商务合作", "投稿",
    "网站地图", "关于我们", "联系方式", "隐私政策", "用户协议", "使用条款",
    "Cookie", "cookies", "JavaScript", "验证码",
]

# ========== 乱码 / WAF / Base64 检测 ==========
GARBLED_PATTERNS = [
    r"\{_waf_[a-z0-9]+",
    r"_waf_[a-f0-9]+",
    r"tOYw3VuDHpEZ",
    r"[A-Za-z0-9+/]{100,}={0,2}",          # 长 base64
    r"[\x00-\x08\x0b\x0c\x0e-\x1f]",       # 控制字符
    r"\ufffd",                             # 替换字符
    r"[^\x00-\x7f\u4e00-\u9fff\s]{20,}",   # 连续非中英文字符
]

GARBLED_THRESHOLDS = {
    "non_printable_ratio": 0.15,   # 非打印字符比例 > 0.15 判乱码
    "chinese_ratio_min": 0.05,     # 中文占比 < 0.05 且英文单词 < 5 且长度 > 200 判乱码
    "min_len_for_chinese_check": 200,
}

# ========== 拦截 / 挑战页：HTTP 200 的"假正文"（V9.4，2026-09-21） ==========
# 场景：反爬前置层（Google reCAPTCHA / Cloudflare / WAF）对可疑请求返回
# **HTTP 200** 的挑战页而非 403 —— 状态码守卫看不见它，而页面可见文字极短
# （几十~几百字符）却恰好越过 web_fetch 的判空阈值 min_content_length(30)，
# 于是被判成"抓到正文"：**抓取降级链就此终止，永远不升级到浏览器级**。
#
# 实测（2026-09-21，全部实测数据）：
#   - pmc.ncbi.nlm.nih.gov：httpx 连续 6/6 次拿到 20KB reCAPTCHA 挑战页 HTML
#     （确定性，非概率），trafilatura 提出 63 字提示
#     「正在检查浏览器以便让您访问 pmc.ncbi.nlm.nih.gov…如果系统在 5 秒后未
#      自动将您重定向，请点击 此处」→ 判空闸放行 → content_score 0.19 → 丢弃。
#     真内容 10857 字，**只有浏览器级 StealthyFetcher 能拿到**；中间级
#     AsyncFetcher 返回 203 + 「Cookies must be enabled…and reload this page」
#     5522 字 HTML → 提出 137 字，同样是垃圾（也会被原判空闸放行）。
#   - 对照：cochrane.org / msdmanuals.cn / cn-healthcare.com 同级无挑战页，
#     各自拿到 2232 / 6874 / 1728 字真正文（未受影响）。
#
# 判据：**短页面 + 命中特征串**。加长度上限是必需的 —— 正常正文里引用
# "Just a moment" / "Access Denied" 完全可能（讲爬虫与反爬的文章就会），
# 而真实挑战页的可见文字极少。1200 字对已知样本（最长 356 字）留了数十倍余量。
BLOCK_PAGE_MAX_LEN = 1200

# 特征串**必须小写**（判定时对整页文本 lower() 后子串匹配）。
# 只收录"正常正文里不会出现的整串标识"，不用裸词 —— 承 TEMPLATE_STRONG_MARKERS
# 对裸 "Forbidden" / "Access Denied" 的教训（2026-09-12 实测修正）。
BLOCK_PAGE_MARKERS = [
    # ---- Google reCAPTCHA 挑战页（实测命中）----
    "正在检查浏览器",
    "未自动将您重定向",
    "recaptcha/challengepage",
    "recaptchachallengepageui",
    "checking your browser",
    # ---- Cloudflare 交互式挑战 / JS 质询 ----
    "just a moment...",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    # ---- NCBI 系「Cookies 未启用」提示页（实测命中，AsyncFetcher 203 分支）----
    "cookies must be enabled",
    "and reload this page to continue",
    # ---- 通用 WAF / 人机校验 ----
    "verify you are human",
    "正在验证您是否是真人",
    "请验证您是真人",
    "don't have permission to access",     # 实测：mayoclinic 403 页 "You don't have permission to access … on this server. Reference #18."
    "your request has been blocked",
    "您的请求已被拦截",
    "unusual traffic from your computer network",
]

# ========== 域名权重表：0.5~1.5，未知站 1.0（最长后缀匹配） ==========
# 注意：官方媒体在这里（加分），不在 URL 黑名单 —— V8.3 裁决。
DOMAIN_WEIGHTS = {
    # ---- 顶级权威：1.4~1.5 ----
    "openai.com": 1.5,
    "arxiv.org": 1.5,
    "nature.com": 1.5,
    "science.org": 1.5,
    "pubmed.ncbi.nlm.nih.gov": 1.5,
    "ncbi.nlm.nih.gov": 1.5,
    "deepmind.com": 1.5,
    "who.int": 1.4,
    "nih.gov": 1.4,
    "cdc.gov": 1.4,
    "nasa.gov": 1.4,
    "un.org": 1.4,
    "whitehouse.gov": 1.4,
    "congress.gov": 1.4,
    "acm.org": 1.4,
    "ieee.org": 1.4,
    "sciencedirect.com": 1.4,
    "springer.com": 1.4,
    "wiley.com": 1.4,
    "tandfonline.com": 1.4,
    "sagepub.com": 1.4,
    "doi.org": 1.4,
    "anthropic.com": 1.4,
    "gov.cn": 1.4,
    "cas.cn": 1.4,
    "cass.cn": 1.4,
    "nsfc.gov.cn": 1.4,
    "noaa.gov": 1.3,
    "worldbank.org": 1.3,
    "imf.org": 1.3,
    "oecd.org": 1.3,
    "europa.eu": 1.3,
    "xinhuanet.com": 1.3,
    "news.cn": 1.3,
    "caixin.com": 1.3,
    "github.com": 1.3,
    "microsoft.com": 1.3,
    "apple.com": 1.3,
    "ibm.com": 1.3,
    "nvidia.com": 1.3,
    "intel.com": 1.3,
    "amd.com": 1.3,
    "semanticscholar.org": 1.3,
    "openreview.net": 1.3,
    "mlr.press": 1.3,
    "jmlr.org": 1.3,
    "aclweb.org": 1.3,
    "neurips.cc": 1.3,
    "icml.cc": 1.3,
    "ssrn.com": 1.3,
    "cnki.net": 1.3,
    "wanfangdata.com.cn": 1.3,
    "pkulaw.com": 1.3,
    "lawinfochina.com": 1.3,
    "edu.cn": 1.3,
    # ---- 中立偏可信：1.0~1.2 ----
    "people.com.cn": 1.2,
    "yicai.com": 1.2,
    "researchgate.net": 1.2,
    "thepaper.cn": 1.1,
    "jiemian.com": 1.1,
    "21jingji.com": 1.1,
    "stcn.com": 1.1,
    "cnstock.com": 1.1,
    "chinanews.com.cn": 1.1,
    "chinadaily.com.cn": 1.1,
    "globaltimes.cn": 1.1,
    "bjnews.com.cn": 1.1,
    "infzm.com": 1.1,
    "qstheory.cn": 1.1,
    "xuexi.cn": 1.1,
    "12371.cn": 1.1,
    "huanqiu.com": 1.0,
    "guancha.cn": 1.0,
    "thecover.cn": 1.0,
    "youth.cn": 1.0,
    "cyol.com": 1.0,
    "gmw.cn": 1.0,
    # ---- 低质 / 聚合 / 内容农场：0.5~0.8 ----
    # ⚠️ V9.6：本表**不再收录 URL 黑名单域** —— 命中 URL_BLACKLIST_HOST_SUBSTRINGS
    #    的域名，其任何 URL 都在 content_score 里**短路返回 0.0**（早于 _domain_weight），
    #    权重永远读不到。已删 23 条：baijiahao / toutiao / jianshu / tieba / zhidao /
    #    wenku / docin / doc88 / book118 / 51wendang / taodocs / renrendoc / 360doc(.com/.cn)
    #    以及全部广告-追踪-缓存域（doubleclick / googleadservices / googlesyndication /
    #    amazon-adsystem / adservice.google.com / webcache.googleusercontent.com /
    #    cache.baiducontent.com / translate.google.com / r.jina.ai）。
    #    注：sohu.com / 163.com 只在**路径**黑名单里，权重仍可达，保留。
    "sohu.com": 0.6,
    "163.com": 0.6,
    "sina.com.cn": 0.7,
    "qq.com": 0.7,
    "ifeng.com": 0.7,
    "zhihu.com": 0.8,
    "douban.com": 0.8,
    "chinaz.com": 0.7,
    "36kr.com": 0.8,
    "huxiu.com": 0.8,
    "tmtpost.com": 0.8,
    "leiphone.com": 0.8,
    "ithome.com": 0.8,
    "cnbeta.com": 0.7,
    "xueqiu.com": 0.8,
    "wallstreetcn.com": 0.8,
    "cls.cn": 0.8,
    "eastmoney.com": 0.9,
    "jrj.com.cn": 0.7,
    "hexun.com": 0.7,
    "10jqka.com.cn": 0.7,
    # ---- 广告 / 追踪 / 缓存域：**整块已删**（V9.6）----
    # 这些域全在 URL_BLACKLIST_HOST_SUBSTRINGS 里，content_score 对它们一律短路
    # 0.0，权重读不到；黑白两处重复登记只会让人以为「权重还在起作用」。
}

# ========== 白名单：命中即忽略动态学习 delta（用户纠偏出口） ==========
# 用途："我知道这个站被误判了" —— 白名单站点只按静态 DOMAIN_WEIGHTS 计分，
# filters_learn 的降权/提权对它们不生效。自动层永远不改这张表。
WHITELIST_HOSTS = [
    "gov.cn",
    "who.int",
    "nih.gov",
    "un.org",
]

# ========== 正文质量软信号关键词 ==========
QUALITY_POSITIVE_MARKERS = [
    "摘要", "引言", "方法", "结果", "讨论", "结论",
    "参考文献", "附录", "致谢",
    "Abstract", "Introduction", "Method", "Result",
    "Discussion", "Conclusion", "References",
    "研究表明", "调查显示", "报告指出", "数据显示",
    "根据", "实验", "统计", "分析",
]

QUALITY_NEGATIVE_MARKERS = [
    "广告", "推广", "赞助", "点击购买", "立即抢购",
    "限时优惠", "扫码关注", "加微信", "免费领取",
    "点击下载", "注册即送", "充值", "返现",
    "赌博", "博彩", "彩票",
    "代购", "刷单", "兼职", "日赚", "月入过万",
]

PUNCTUATION_CHARS = "。！？，、；：,.!?;:"

# ========== 信源权威度层级表（V10.4，独立于 content_score 的第二打分维度） ==========
# 动机（2026-10-03 粮油加工 8 轮检索实测）：content_score 管"像不像干净正文"，
# 管不了"信源可不可信"——厂商选型广告页 0.972 与行业标准 0.956 同分同区，
# 营销内容凭文案干净就能压过权威来源。本表把"权威度"拆成独立分数并列透出，
# 排序时与形态分、query 覆盖率合成 rank_score（权重见 config.rank_w_*）。
#
# 层级口径（0~1）：
#   0.95  gov.cn / edu.cn / 科研院所 / 标准平台 / 顶级期刊与数据库
#   0.85~0.92  国际组织 / 专业数据库 / 法律数据库 / 行业学会
#   0.62~0.80  主流媒体 / 权威企业官网 / 专业社区（dxy、github 等）
#   0.55~0.60  百科 / 头部垂直媒体
#   0.45~0.52  垂直内容 / 健康门户 / 书籍转载 / 参数评测站 / 公众号
#   0.35~0.42  问答 / 自媒体 / 论文库 / 范文考试站
#   0.28~0.30  文库下载站 / 厂商营销 / 内容农场 / 商城
# ⚠️ 与 DOMAIN_WEIGHTS 刻意不同值：那张表服务形态分（0.5~1.5 乘性权重），
#    本表是独立 0~1 分。最长后缀匹配（www. 归一），未命中兜底 0.50。
# ⚠️ 覆盖面声明：本表 ~150 条为常见站先验，**未知站一律 0.5**（不奖不罚）；
#    表只提供"一眼能认的大站"，长尾判断交给调用方看 cov 与正文本身。
AUTHORITY_TIERS = {
    # ---- 通用后缀：政府 / 高校 / 科研（先给足，覆盖面最大）----
    "gov.cn": 0.95, "edu.cn": 0.92, "ac.cn": 0.92,
    "cas.cn": 0.95, "cass.cn": 0.95, "caas.cn": 0.95, "cae.cn": 0.95,
    # ---- 国际组织 / 国际标准 ----
    "who.int": 0.95, "fao.org": 0.90, "un.org": 0.90, "oecd.org": 0.85,
    "worldbank.org": 0.85, "imf.org": 0.85, "europa.eu": 0.85,
    "iso.org": 0.90, "iec.ch": 0.90, "irri.org": 0.90, "cgiar.org": 0.90,
    # ---- 顶级期刊 / 学术数据库 ----
    "nature.com": 0.92, "science.org": 0.92, "cell.com": 0.92,
    "thelancet.com": 0.92, "nejm.org": 0.92, "bmj.com": 0.92,
    "jamanetwork.com": 0.92, "pubmed.ncbi.nlm.nih.gov": 0.95,
    "ncbi.nlm.nih.gov": 0.95, "arxiv.org": 0.90, "psyarxiv.com": 0.82,
    "sciencedirect.com": 0.90, "springer.com": 0.90, "link.springer.com": 0.90,
    "wiley.com": 0.90, "tandfonline.com": 0.90, "sagepub.com": 0.90,
    "ieee.org": 0.90, "acm.org": 0.90, "aps.org": 0.90, "rsc.org": 0.90,
    "acs.org": 0.90, "mdpi.com": 0.78, "frontiersin.org": 0.78,
    "plos.org": 0.82, "journals.plos.org": 0.82,
    "cnki.net": 0.88, "wanfangdata.com.cn": 0.88, "cqvip.com": 0.85,
    "ssrn.com": 0.85, "semanticscholar.org": 0.85, "doi.org": 0.90,
    "openreview.net": 0.85, "researchgate.net": 0.80, "paperswithcode.com": 0.75,
    # ---- 标准与法规平台 ----
    "gb688.cn": 0.95, "ndls.org.cn": 0.95, "cssn.cn": 0.90,
    "pkulaw.com": 0.85, "lawinfochina.com": 0.85, "chinacourt.org": 0.80,
    "law-lib.com": 0.60,
    # ---- 科研院所 / 学会（实测命中 + 常见）----
    "ricesci.cn": 0.95, "zwxb.chinacrops.org": 0.95, "gxaas.net": 0.95,
    "haas.cn": 0.95, "cdas.cn": 0.85, "ags.org.cn": 0.85, "csa.org.cn": 0.85,
    # ---- 官方媒体 ----
    "xinhuanet.com": 0.78, "news.cn": 0.78, "people.com.cn": 0.75,
    "qstheory.cn": 0.75, "cctv.com": 0.75, "cnr.cn": 0.72,
    "china.com.cn": 0.72, "gmw.cn": 0.72, "chinanews.com.cn": 0.72,
    "cyol.com": 0.72, "china.com.cn": 0.72,
    # ---- 市场化媒体 / 财经 ----
    "caixin.com": 0.75, "yicai.com": 0.72, "thepaper.cn": 0.72,
    "21jingji.com": 0.70, "jiemian.com": 0.70, "stcn.com": 0.68,
    "cnstock.com": 0.68, "bjnews.com.cn": 0.68, "infzm.com": 0.68,
    "huxiu.com": 0.68, "36kr.com": 0.62, "wallstreetcn.com": 0.62,
    "cls.cn": 0.65, "eastmoney.com": 0.60, "tmtpost.com": 0.60,
    "leiphone.com": 0.60, "huanqiu.com": 0.60, "guancha.cn": 0.58,
    "ifeng.com": 0.58, "ithome.com": 0.58, "cnbeta.com": 0.55,
    "jrj.com.cn": 0.55, "hexun.com": 0.55, "10jqka.com.cn": 0.55,
    "xueqiu.com": 0.50,
    # ---- 权威企业官网 / 专业社区 ----
    "cnrice.com.cn": 0.70, "shimadzu.com.cn": 0.70, "huawei.com": 0.70,
    "microsoft.com": 0.70, "apple.com": 0.70, "ibm.com": 0.70,
    "nvidia.com": 0.70, "intel.com": 0.70, "amd.com": 0.70,
    "github.com": 0.70, "huggingface.co": 0.70, "pytorch.org": 0.70,
    "tensorflow.org": 0.70, "stackoverflow.com": 0.68, "dxy.cn": 0.68,
    "msdmanuals.com": 0.70, "msdmanuals.cn": 0.70, "antpedia.com": 0.62,
    "w3.org": 0.80, "rfc-editor.org": 0.80, "mozilla.org": 0.65,
    # ---- 百科 ----
    "baike.baidu.com": 0.58, "baike.baidu.hk": 0.58, "yixue.com": 0.58,
    "newton.com.tw": 0.55, "baike.com": 0.50, "sogou.com": 0.50,
    "wikipedia.org": 0.60, "zhihu.com": 0.55, "mbalib.com": 0.52,
    # ---- 垂直内容 / 健康门户 / 书籍转载 / 评测参数站 ----
    "health.baidu.com": 0.48, "youlai.cn": 0.48, "hbcbly.com": 0.48,
    "maigoo.com": 0.45, "jucanw.com": 0.45, "miaoshou.net": 0.45,
    "page.sm.cn": 0.45, "gf.cabr-fire.com": 0.50, "inews.qq.com": 0.55,
    "qq.com": 0.50, "sohu.com": 0.45, "163.com": 0.45, "sina.com.cn": 0.45,
    "haodf.com": 0.55, "39.net": 0.45, "xywy.com": 0.45, "120ask.com": 0.35,
    "zol.com.cn": 0.50, "pconline.com.cn": 0.50, "it168.com": 0.50,
    "autohome.com.cn": 0.55, "smzdm.com": 0.45, "mafengwo.cn": 0.45,
    "ctrip.com": 0.50, "bjx.com.cn": 0.60, "solarbe.com": 0.55,
    "mp.weixin.qq.com": 0.45, "douban.com": 0.50, "bilibili.com": 0.45,
    "infoq.cn": 0.55, "csdn.net": 0.42, "juejin.cn": 0.45,
    "cnblogs.com": 0.45, "segmentfault.com": 0.48, "oschina.net": 0.45,
    "v2ex.com": 0.35, "runoob.com": 0.40, "w3school.com.cn": 0.40,
    # ---- 问答 / 自媒体 / 论文库 / 范文考试站 ----
    "weibo.com": 0.38, "blog.sina.com.cn": 0.35, "nongyelu.com": 0.38,
    "360qiwen.com": 0.35, "knowcat.cn": 0.38, "gwyoo.com": 0.38,
    "qianqiantushu.com": 0.35, "6miu.com": 0.28, "xiaohongshu.com": 0.35,
    "douyin.com": 0.30, "yjbys.com": 0.35, "ruiwen.com": 0.35,
    "unjs.com": 0.35, "liuxue86.com": 0.35, "diyifanwen.com": 0.35,
    "oh100.com": 0.35, "51test.net": 0.40,
    # ---- 文库下载 / 厂商营销 / 内容农场 / 商城 ----
    "cucdc.com": 0.28, "ricemillmachinerys.com": 0.28,
    "taizyagromachine.com": 0.28, "pwsannong.com": 0.28,
    "taobao.com": 0.30, "tmall.com": 0.30, "jd.com": 0.40,
}
AUTHORITY_DEFAULT = 0.50     # 未命中兜底：不奖不罚
