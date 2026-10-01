"""自适应降权：单函数入口，纯标准库，无依赖。

用法：
    from .filters_learn import penalty_delta, current_delta

    delta = penalty_delta("xueqiu.com", fetch_ok=False, score=0.1)
    # delta =  0.0  → 不降权（样本不足 / 表现正常）
    # delta = -0.4  → 降权 0.4
    # delta = +0.1  → 提权 0.1
    cur = current_delta("xueqiu.com")   # 只读查询，不累积观察（filters.domain_weight 用）

设计：
- element 可以是域名、URL、任意字符串；含 "/" 时自动取 host。弱标签由调用方传入。
- 统计持久化到 temp/logs/penalty_stats.json；每次 delta 变化写审计日志
  temp/logs/filter_learning.log（一行一事件，见 _audit）。
- 唯一的时间机制是统计衰减（30 天）：久未观察的元素统计减半直至清空，避免僵尸样本。
  已降权元素（delta_date > 0）跳过衰减，由下次观察重新判定。
- 降权下限：delta 最小 -0.5，filters 侧 final_weight 最小 0.1（永不归零消失）。
  动态层只做软降权，不触碰 url_blacklisted（误判最坏 = 排名靠后，不是消失）。
- _judge 档位：抓不到 -0.5 / 成功率低 -0.5 / 低分高 -0.4 / 慢站 -0.3 / 良好 +0.1。
- 统计文件域名上限 1000：超限按 n 升序淘汰最冷门（降权中的记录最后淘汰）。
- 并发保护：统计文件只有本进程一个写者，进程内互斥锁足够；读→改→写的原子性
  由 _save 的 tmp + os.replace 保证。
- current_delta 带 30s TTL 只读缓存：content_score 高频调用不必每次读盘。
- 排查明细（n / 均分 / 延迟）直接读 temp/logs/penalty_stats.json。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse

_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")
# 统计文件路径：**模块级可变**，测试改绑它即完全隔离（审计日志路径由它同目录派生）。
# 落在 temp/logs/ 子目录，与运行日志、审计日志集中一处。
_STATS_PATH = os.path.join(_BASE_DIR, "logs", "penalty_stats.json")

# 统计衰减：30 天未见 → 减半
_DECAY_DAYS = 30

_MIN_SAMPLES = 5
_LOW_SCORE = 0.25
_LOW_RATE = 0.6
_FETCH_RATE = 0.2
_SLOW_LATENCY = 25.0     # 秒：单条整链硬熔断 35s，25s+ 已在超时边缘
_MAX_DOMAINS = 1000      # 统计文件域名上限，超限淘汰最冷门

# current_delta 只读缓存 {host: (ts, 统计文件路径, delta)}；路径一起存是为了
# 测试改绑 _STATS_PATH 后旧缓存自动失效（命中前比对路径）。
_READ_CACHE_TTL = 30.0
_read_cache: Dict[str, tuple] = {}
# 容量兜底：TTL 只在读取时跳过过期条目、从不主动删除，长驻进程的 host 键会一直
# 累积。超限先清过期项，仍超限则整体清空（缓存只影响命中率，清空零正确性影响）。
_READ_CACHE_MAX = 4096


def _norm(element: str) -> str:
    e = str(element or "").strip().lower()
    if not e:
        return ""
    if "/" in e:
        h = urlparse(e).hostname or ""
        e = h or e
    return e.removeprefix("www.")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path: str, stats: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 进程内互斥锁：统计文件只有一个写者（本进程），锁只防同进程内潜在并发调用。
# 原子性由 _save 的 tmp + os.replace 保证。
_stats_lock = threading.Lock()


def _new_record() -> dict:
    return {
        "n": 0,               # 总观察次数
        "ok": 0,              # 抓取成功次数
        "score_sum": 0.0,     # 成功抓取的 content_score 累计
        "low": 0,             # 低分/命中模板或乱码的次数
        "lat_sum": 0.0,       # 抓取延迟累计（秒，含失败：失败≈熔断时长）
        "lat_n": 0,           # 延迟样本数
        "last": 0.0,          # 最后一次观察时间
        "delta": 0.0,         # 上次判定的降权幅度
        "delta_date": 0.0,    # 权重改变日期（0 = 未降权；>0 = 跳过 30 天衰减）
    }


def _lat_avg(s: dict) -> float:
    n = s.get("lat_n", 0) or 0
    return (s.get("lat_sum", 0.0) or 0.0) / n if n else 0.0


def _judge(s: dict) -> float:
    """按当前统计给出降权幅度。档位：-0.5 抓不到 / -0.4 低质 / -0.3 慢 / +0.1 良好。"""
    n, ok, low = s.get("n", 0), s.get("ok", 0), s.get("low", 0)
    if n < _MIN_SAMPLES:
        return 0.0
    avg = s["score_sum"] / ok if ok else 0.0
    fetch_rate, low_rate = ok / n, low / n
    if ok == 0 and n >= 8:
        return -0.5
    if fetch_rate < _FETCH_RATE:
        return -0.5
    if avg < _LOW_SCORE and low_rate > _LOW_RATE:
        return -0.4
    if _lat_avg(s) > _SLOW_LATENCY:
        return -0.3
    if avg > 0.7 and n >= 10:
        return 0.1
    return 0.0


def _audit_path() -> str:
    """审计日志路径：**跟随统计文件同目录**（测试改绑 _STATS_PATH 即一起隔离）。"""
    return os.path.join(os.path.dirname(os.path.abspath(_STATS_PATH)), "filter_learning.log")


def _audit(key: str, s: dict, note: str) -> None:
    """审计日志：一行一事件。失败静默（日志不应影响主流程）。"""
    try:
        log_path = _audit_path()
        os.makedirs(os.path.dirname(log_path) or _BASE_DIR, exist_ok=True)
        n, ok, low = s.get("n", 0), s.get("ok", 0), s.get("low", 0)
        avg = s["score_sum"] / ok if ok else 0.0
        low_rate = low / n if n else 0.0
        line = (f"{time.strftime('%Y-%m-%d')} {key} {note}"
                f" (avg={avg:.2f} low={low_rate:.2f} n={n})")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _cap(stats: dict) -> None:
    """统计文件域名上限：超限按 n 升序淘汰最冷门；降权中的记录最后淘汰。"""
    if len(stats) <= _MAX_DOMAINS:
        return
    evictable = [h for h, s in stats.items() if not ((s.get("delta") or 0) < 0)]
    for h in sorted(evictable, key=lambda h: stats[h].get("n", 0)):
        if len(stats) <= _MAX_DOMAINS:
            break
        del stats[h]


def penalty_delta(
    element: str,
    *,
    fetch_ok: Optional[bool] = None,
    score: Optional[float] = None,
    template_hit: bool = False,
    garbled_hit: bool = False,
    latency: Optional[float] = None,
) -> float:
    """累积一次观察，返回该元素的降权幅度（写 + 读合一入口）。

    参数：
        element: 域名 / URL / 任意字符串（含 "/" 时取 host）。
        fetch_ok: 本次抓取是否成功。
        score: content_score 得分（0~1）。
        template_hit: 是否命中模板特征（强负信号；与低分原因区分）。
        garbled_hit: 是否命中乱码特征（强负信号）。
        latency: 本次抓取耗时（秒）。慢站（超时边缘）是隐性成本，值得降权。

    返回：
        float。负值=降权，0.0=不降权（冷启动 / 正常），正值=提权。
    """
    key = _norm(element)
    if not key:
        return 0.0
    path = _STATS_PATH
    now = time.time()

    with _stats_lock:
        stats = _load(path)

        # ---------- 1. 累积本次观察 ----------
        s = stats.setdefault(key, _new_record())
        s["n"] += 1
        s["last"] = now
        if fetch_ok is True:
            s["ok"] += 1
            if score is not None:
                s["score_sum"] += float(score)
        if score is not None and float(score) < _LOW_SCORE:
            s["low"] += 1
        if template_hit or garbled_hit:
            s["low"] += 1
        if latency is not None and float(latency) > 0:
            s["lat_sum"] = (s.get("lat_sum", 0.0) or 0.0) + float(latency)
            s["lat_n"] = (s.get("lat_n", 0) or 0) + 1

        # ---------- 2. 统计衰减：30 天未见且未降权的元素，减半 ----------
        for h in list(stats):
            rec = stats[h]
            if rec.get("delta_date", 0.0) > 0:
                continue  # 已降权元素不衰减，由下次观察重新判定
            age = (now - rec.get("last", 0)) / 86400
            if age > _DECAY_DAYS:
                rec["n"] = max(1, rec["n"] // 2)
                rec["score_sum"] *= 0.5
                rec["lat_sum"] = (rec.get("lat_sum", 0.0) or 0.0) * 0.5
                rec["lat_n"] = (rec.get("lat_n", 0) or 0) // 2
                if rec["n"] <= 1 and rec.get("last", 0) and age > _DECAY_DAYS * 2:
                    del stats[h]

        # ---------- 3. 重新判定 delta，变化时记录日期 + 审计 ----------
        delta = _judge(s)
        if delta != s.get("delta", 0.0):
            s["delta"] = delta
            if delta < 0:
                s["delta_date"] = now       # 新降权：记录日期（>0 即跳过 30 天衰减）
            else:
                s["delta_date"] = 0.0       # 归零或提权：清空标记
            _audit(key, s, f"delta={delta:+.1f}")

        _cap(stats)
        _save(path, stats)

    _read_cache.pop(key, None)
    return round(delta, 3)


def current_delta(element: str) -> float:
    """只读查询当前 delta，不累积观察（filters.domain_weight 用）。

    未记录 / 未降权 → 0.0。带 TTL 只读缓存：content_score 对每条 URL 都会取
    域名权重，缓存避免一轮抓取内对同一域名反复读盘。
    """
    key = _norm(element)
    if not key:
        return 0.0
    path = _STATS_PATH
    now = time.time()
    hit = _read_cache.get(key)
    if hit and (now - hit[0]) < _READ_CACHE_TTL and hit[1] == path:
        return hit[2]
    s = _load(path).get(key) or {}
    delta = float(s.get("delta", 0.0) or 0.0)
    if len(_read_cache) >= _READ_CACHE_MAX:
        for k in [k for k, v in _read_cache.items() if now - v[0] >= _READ_CACHE_TTL]:
            del _read_cache[k]
        if len(_read_cache) >= _READ_CACHE_MAX:
            _read_cache.clear()
    _read_cache[key] = (now, path, delta)
    return delta
