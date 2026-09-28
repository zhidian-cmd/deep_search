"""搜索结果打分排序（只用标准库，不联网）。

输入：CLI 输出的结果集（``{"results":[...]}``，条目协议见 search.py）。
输出：按分数降序的列表，每条挂上 ``score``。

**没有黑名单/白名单。** 唯一学出来的表是**域名 P(坏)**（model.json，由训练侧的
learn.py 贝叶斯收缩拟合后同步过来 —— 训练侧代码不随包分发，重训后需把 model.json
手动拷回本目录）。
"某个词像广告"的词表（title/body/url 三张）在 12 关键词消融里是**负贡献**，已整体删除：
标题只有 ~10 个词元撑不起跨关键词一致性检验，正文表学出来的全是"机构语言"反向词。
软文/导购话术（"十大品牌排行榜"）在 metadata 层就是看不见——这是记录在案的已知边界。

本文件只剩两类：

* **结构特征**：URL 形态、价格数字模式、正文链接密度、同域名霸屏条数、
  乱码/标签汤 —— 与话术无关，新站、新文案也拦得住；
* **查表证据**：``dom_bad``（域名坏率，学出来的唯一一张表）。

分数结构::

    score = pos - P_AD*ad - P_BAD*bad
    pos   = W_TITLE*rel_t + W_REL*rel + W_QUAL*qual + W_SRC*src + W_FRESH*fresh

``pos`` 上界是 1，而 ``ad``/``bad`` 的惩罚系数都 > 1，所以坏度接近 1 的条目
最高只能是负分、**必然**排在任何干净条目之后 —— "坏的在末尾"是结构保证。
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import date

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.json")

# --- 权重（只动这些数字；不要在留出集上调它们，见 README「留出纪律」） --------
P_AD = 1.6           # 商业推销惩罚（>1 才有"必沉底"的结构保证；--tune 在训练折上选出）
P_BAD = 1.15         # 无效惩罚

W_TITLE = 0.20       # 标题覆盖查询词
W_REL = 0.38         # 正文覆盖查询词
W_QUAL = 0.18        # 内容可读性 / 深度
W_SRC = 0.14         # 来源位置 + 引擎可信度
W_FRESH = 0.05       # 时效
W_PATH = 0.05        # URL 路径深度（正项加起来 = 1.00）

# 时效宽限：发布 **2 年内**都算满分（用户指定的宽限口径），其后按 5 年线性衰减到 0。
# 与 W_FRESH 是两回事：W_FRESH 决定时效在总分里占多重（0.05），本值决定"多新算新"。
_FRESH_GRACE_YEARS = 2

RANK_DECAY = 0.35    # 名次衰减：src_rank = 1/(1+0.35*(best-1))

AD_DOM, AD_PRICE, AD_LINK, AD_BURST = 0.55, 0.30, 0.20, 0.15
BAD_DOM = 0.35

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_KANA = re.compile(r"[\u3040-\u30ff]")
_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
_URL_IN_BODY = re.compile(r"https?://")
_MD_LINK = re.compile(r"\]\(")
# 价格/货币的**数字模式**（不是词表）：¥199、99元、9.9折、50%
_PRICE = re.compile(r"[¥￥$]\s?\d|\d+(\.\d+)?\s*元|\d+(\.\d+)?\s*折|\d+(\.\d+)?\s*%")
# 查询词元用：英文词含 +._- 等版本号字符
_WORD = re.compile(r"[a-z0-9][a-z0-9+._-]*")


# ---------------------------------------------------------------------------
# 查询词元（原 learn.py 唯一的生产调用函数，已并入本文件）
# ---------------------------------------------------------------------------
def terms(text: str) -> set[str]:
    """中文取 2-gram，英文取长度 ≥2 的词。不需要分词依赖。"""
    low = text.lower()
    out: set[str] = set()
    for run in _CJK.findall(low):
        if len(run) < 2:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    out.update(w for w in _WORD.findall(low) if len(w) >= 2)
    return out


_model: dict | None = None


def get_model() -> dict:
    """取当前模型；没有 model.json 时退回"只有结构特征"的退化模式。"""
    global _model
    if _model is None:
        _model = _load_model()
    return _model


def _load_model() -> dict:
    """读 model.json；没有就退回"只有结构特征"的退化模式。"""
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {"prior": {"bad_rate": 0.4, "n_examples": 0}, "dom": {}, "missing": True}


def _host(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# 学出来的证据：只剩域名表（词表/引擎表已删，见 README「拆掉了什么」）
# ---------------------------------------------------------------------------
def _dom_bad(it: dict, model: dict) -> float:
    """域名 P(坏)，贝叶斯收缩过的查表值；没见过的域名退回全局先验。"""
    dom = model["dom"].get(it["_host"])
    return dom[0] if dom else model["prior"]["bad_rate"]


# ---------------------------------------------------------------------------
# 结构特征（无词表，只看形状）
# ---------------------------------------------------------------------------
def _struct(it: dict) -> tuple[float, float, float]:
    """(价格数字模式, 链接密度, 无效结构)。都跟具体词汇无关。"""
    price = 1.0 if _PRICE.search(it["title"]) else 0.0
    body = it["snippet"]
    links = len(_URL_IN_BODY.findall(body)) + len(_MD_LINK.findall(body))
    link = min(1.0, links / 10.0)
    n = max(1, len(body))
    broken = 0.0
    if len(body) < 30:
        broken += 1.0
    ctrl = body.count("\ufffd") + sum(1 for c in body[:2000] if ord(c) < 32 and c not in "\n\r\t")
    if ctrl / n > 0.002:
        broken += 1.0
    tags = sum(len(m.group(0)) for m in _TAG.finditer(body[:3000]))
    if tags / min(n, 3000) > 0.05:
        broken += 1.0
    return price, link, min(1.0, broken)


def _burst(pool: list[dict]) -> dict:
    """同域名在本 query 下出现几条 —— 站群/霸屏是结构信号，与内容无关。"""
    n: dict[str, int] = {}
    for it in pool:
        n[it["_host"]] = n.get(it["_host"], 0) + 1
    return n


# ---------------------------------------------------------------------------
# 相关度 / 可读性 / 时效
# ---------------------------------------------------------------------------
def _idf(pool: list[dict], qterms: set[str]) -> dict:
    """查询词在**本池内**的稀有度。越稀有越能区分跑题。"""
    n = max(1, len(pool))
    text = [it["_text"] for it in pool]
    return {t: math.log(1 + n / (1 + sum(1 for x in text if t in x))) for t in qterms}


def _rel(title: str, text: str, idf: dict) -> float:
    """标题覆盖 + 正文覆盖。标题权重远高于正文（正文长，覆盖是廉价的）。

    原签名里还有个 `qterms` 形参，函数体从没用过 —— 它要的词权重全在 `idf` 里。
    """
    total = sum(idf.values()) or 1.0
    tl, tx = title.lower(), text.lower()
    ht = sum(w for t, w in idf.items() if t in tl)
    hx = sum(w for t, w in idf.items() if t in tx)
    return min(1.0, (1.35 * ht + 0.30 * hx) / (1.35 * total))


def _phrase(title: str, query: str) -> float:
    parts = [p.strip().lower() for p in query.split() if len(p.strip()) >= 2]
    if not parts:
        return 0.0
    tl = title.lower()
    return sum(1 for p in parts if p in tl) / len(parts)


def _lang(query: str, text: str) -> float:
    """语种不合 → 相关度打折。中文查询返回日文假名页，用户读不了。

    这是**字符类**规则不是词表：只看假名字符占比，不含任何具体词汇。
    （实测：池内假名占比 >3% 的 5 条全部被判坏。）
    """
    if _KANA.search(query) or not _CJK.search(query):
        return 1.0
    return 0.25 if len(_KANA.findall(text)) > 0.03 * max(1, len(text)) else 1.0


def _qual(it: dict) -> float:
    """可读性 / 内容深度。

    两个**跨关键词实测同向**的信号（见 README「特征筛选」）：

    * 正文绝对长度：ok 中位 340 字 vs bad 中位 178 字；
    * 正文/标题长度比：坏条目标题相对更长的同时正文更薄（软文/壳页特征），
      比值越小的越像套话页。
    """
    body = it["snippet"]
    n = len(body)
    depth = min(1.0, n / 500.0)
    ratio = min(1.0, (n / (len(it["title"]) + 1)) / 20.0)
    cjk = len(_CJK.findall(body))
    density = cjk * 2 / max(1, n)
    dup = 0.0
    sents = [s.strip() for s in re.split(r"[。！？\n]+", body) if len(s.strip()) >= 8]
    if len(sents) >= 4:
        dup = 1.0 - len(set(sents)) / len(sents)
    punct = len(re.findall(r"[，。！？；：、,.!?;:]", body)) / max(1.0, n / 120.0)
    return (0.34 * depth + 0.22 * ratio + 0.22 * min(1.0, density / 0.45)
            + 0.10 * min(1.0, punct) + 0.12 * (1.0 - min(1.0, dup)))


def _path_depth(url: str) -> float:
    """URL 路径深度。实测 5/5 个关键词同向：坏条目路径更浅
    （导购/推广页常落在浅层目录，正经资料在更深的文章路径里）。"""
    path = (url or "").split("//")[-1]
    return min(1.0, path.count("/") / 4.0)


def _src(it: dict) -> float:
    """来源可信 = 最好名次的平滑衰减。

    **命中引擎数不给任何加分**：实测坏率随命中引擎数上升（1 个 0.405 / 2 个 0.477 /
    3 个 0.533），多引擎共识里唯一有用的信息（更好名次）已经被"取最好名次"吃掉了。
    曾经还乘过"学出来的引擎坏率/跑题率"，12 关键词消融显示贡献 ≈ 0，已删。
    """
    pos = it["positions"] or {}
    best = min(pos.values()) if pos else 20
    return 1.0 / (1.0 + RANK_DECAY * (max(1, best) - 1))


def _fresh(it: dict) -> float:
    """时效分：**发布两年内满分**，其后按 5 年线性衰减到 0；取不到年份给 0.5。

    年份对比用**当前年份**（`date.today().year`）。这里此前写死 `2026 -`，
    跨年后不更新会把所有条目的"年龄"少算一岁、时效分整体前移。
    未来日期（年份大于今年）算年龄为负 → 走满分分支，不需要单独处理。
    """
    m = re.match(r"(\d{4})", it.get("date") or "")
    if not m:
        return 0.5
    age = date.today().year - int(m.group(1))
    return 1.0 if age <= _FRESH_GRACE_YEARS else max(0.0, 1.0 - (age - _FRESH_GRACE_YEARS) / 5.0)


# ---------------------------------------------------------------------------
# 归一 / 合并 / 打分
# ---------------------------------------------------------------------------
def merge(raw: list[dict]) -> list[dict]:
    """把 CLI 的结果集按 URL 去重成一个待排序池。

    **本层只做去重，不做形态归一**：条目协议（`engine[]` / `positions` / `title` /
    `snippet` / `url` / `date`）与 CLI 输出同构（见 search.py 与 README），
    所有条目直接就是打分器要的形状。

    这里曾有一段"逐引擎 jsonl"转换分支（按规范化 URL 合并、把 `engine` 字符串
    收成数组、`rank` 填进 `positions`）——它是训练侧喂 jsonl 时留下的，而训练侧
    不随包分发，全仓唯一会走那条路的是自检脚本。已删：形态一旦真的漂移，宁可
    在下游**响亮地报错**，也不要在一片看似正常的排序里悄悄按另一套规则合并。
    """
    pool: dict[str, dict] = {}
    for r in raw:
        pool.setdefault(r["url"], r)
    return list(pool.values())


def score_pool(pool: list[dict], query: str) -> list[dict]:
    """给池内每条算并挂上 ``score``。"""
    model = get_model()
    for it in pool:   # 先补派生字段（burst / idf / 结构特征都要读）
        it["_host"] = _host(it["url"])
        it["_text"] = f"{it['title']}\n{it['snippet']}"
    burst = _burst(pool)
    qterms = terms(query)
    idf = _idf(pool, qterms)
    for it in pool:
        lang = _lang(query, it["_text"])
        rel_t = _phrase(it["title"], query) * lang
        rel = _rel(it["title"], it["_text"], idf) * lang
        qual, fresh = _qual(it), _fresh(it)
        dom_bad = _dom_bad(it, model)
        price, link, broken = _struct(it)
        src = _src(it)
        depth = _path_depth(it["url"])
        pos = (W_TITLE * rel_t + W_REL * rel + W_QUAL * qual
               + W_SRC * src + W_FRESH * fresh + W_PATH * depth)
        ad = max(0.0, min(1.0, AD_DOM * dom_bad + AD_PRICE * price
                          + AD_LINK * link
                          + AD_BURST * min(1.0, (burst[it["_host"]] - 1) / 3.0)))
        bad = max(0.0, min(1.0, broken + BAD_DOM * dom_bad))
        it["score"] = round(pos - P_AD * ad - P_BAD * bad, 6)
    return pool


def rank(raw: list[dict], query: str) -> list[dict]:
    """未排序结果 → 排序结果。分数降序，同分按最好名次、再按 URL 稳定排序。

    曾试过在尾部加"标题近重复去重"（rrfgs 步骤 7 的思路），实测有害：
    bad@10 0.350 → 0.433 —— 标注集里那些"同模板 Q&A 页"恰恰是标成 ok 的
    正当条目，把它们挤出前排反而放进了坏条目。详见 README「实测否掉」。

    `feat`（12 项分项特征）曾随每条一起挂出"便于复盘"，但生产链路上它**一路
    无人读**（skill 只取 url/title/snippet/date），唯一读者是自检脚本的打印器，
    每条却要多付 12 次 round()。已删 —— 要看分项，用同一个函数在 REPL 里跑一遍即可。
    """
    pool = score_pool(merge(raw), query)
    pool.sort(key=lambda it: (-it["score"],
                              min(it["positions"].values()) if it["positions"] else 99,
                              it["url"]))
    return pool
