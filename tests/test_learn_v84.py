# -*- coding: utf-8 -*-
"""学习层离线测试：分档判定 / 30 天衰减 / 白名单 / 上限 / 并发 / 审计。

全用临时统计文件（改绑 fl._STATS_PATH），不污染真实统计。
V9.14 删除了「31 天复查 → 临时硬禁 → 黑名单建议」升级链，对应三节测试随之移除。
"""
import sys, os, json, time, tempfile, threading

from _harness import check, summary

from deep_search import filters_learn as fl
from deep_search import filters

tmpdir = tempfile.mkdtemp()
stats = os.path.join(tmpdir, "penalty_stats.json")
# 隔离方式（本文件是全仓唯一需要隔离学习状态的地方）：**改绑模块级的统计文件
# 路径**。审计日志路径由它同目录派生（_audit_path），所以改绑一处就完全隔离。
# 原先每个调用点都要传一遍 stats_path —— 而生产调用方从来只传默认值，那套参数
# 已删（只为测试存在的形参不该长在 5 个函数的签名上）。
fl._STATS_PATH = stats
audit = os.path.join(tmpdir, "filter_learning.log")

def load():
    with open(stats, encoding="utf-8") as f:
        return json.load(f)

def hack(host, **fields):
    """直接改统计文件模拟时间流逝/历史状态。"""
    d = load()
    d.setdefault(host, fl._new_record()).update(fields)
    with open(stats, "w", encoding="utf-8") as f:
        json.dump(d, f)
    fl._read_cache.pop(host, None)

print("== #1/#4 信号完整性：失败路径 + 延迟 ==")
for i in range(8):
    d = fl.penalty_delta("https://dead.example.com/x", fetch_ok=False, latency=30.0)
check("8 次抓取失败 → delta=-0.5", d == -0.5, f"delta={d}")
rec = load()["dead.example.com"]
check("失败也计入延迟样本", rec["lat_n"] == 8 and rec["ok"] == 0, f"lat_n={rec['lat_n']} ok={rec['ok']}")

print("== 冷启动 ==")
d = fl.penalty_delta("https://cold.example.com/a", fetch_ok=True, score=0.9)
check("样本不足 → 0.0", d == 0.0)

print("== #4 慢站档 ==")
for i in range(6):
    d = fl.penalty_delta("https://slow.example.com/a", fetch_ok=True, score=0.8, latency=28.0)
check("均分高但 6×28s → delta=-0.3", d == -0.3, f"delta={d}")

print("== #2 显式 template/garbled 标签 ==")
for i in range(6):
    d = fl.penalty_delta("https://waf.example.com/a", fetch_ok=True, score=0.1,
                         template_hit=True)
rec = load()["waf.example.com"]
check("template_hit 计入 low", rec["low"] >= 6, f"low={rec['low']} n={rec['n']}")
# -0.4 档：均分低（0.1 < 0.25）且低分率高（100% > 60%）。
# 原先这里断言的是 snapshot() 给出的原因文案"均分低"，snapshot 已删 → 改为直接断言幅度。
check("模板页低分 → delta=-0.4", d == -0.4, f"delta={d}")

print("== #5 current_delta 只读 ==")
before = json.dumps(load(), sort_keys=True)
cur = fl.current_delta("https://dead.example.com/y")
after = json.dumps(load(), sort_keys=True)
check("current_delta 不改统计", before == after, f"cur={cur}")
check("current_delta 读到 -0.5", cur == -0.5)
fl._read_cache.clear()

print("== #6/#8/#11 filters.domain_weight 动态合入 + 白名单 + 下限 ==")
fl._read_cache.clear()   # 本节改看 filters 侧的合入结果，先前缓存一律作废
check("静态 1.0 + delta(-0.3) → 0.7", abs(filters._domain_weight("https://slow.example.com/a") - 0.7) < 1e-9,
      f"w={filters._domain_weight('https://slow.example.com/a')}")
hack("who.int", delta=-0.5, delta_date=time.time())
check("白名单 who.int 忽略 delta", abs(filters._domain_weight("https://www.who.int/news/x") - 1.4) < 1e-9,
      f"w={filters._domain_weight('https://www.who.int/news/x')}")

print("== #16 统计文件上限 1000 ==")
d = load()
for i in range(1005):
    d[f"filler{i}.example.com"] = {"n": 1, "ok": 1, "score_sum": 0.5, "low": 0,
                                    "last": time.time(), "delta": 0.0, "delta_date": 0.0}
with open(stats, "w", encoding="utf-8") as f:
    json.dump(d, f)
fl.penalty_delta("https://one-more.example.com/a", fetch_ok=True, score=0.9)
check("超限淘汰到 <=1000", len(load()) <= 1000, f"len={len(load())}")
check("降权记录不被淘汰", "slow.example.com" in load() and "dead.example.com" in load())

print("== #15 统计文件互斥（并发写不损坏） ==")
# V9.6 起保护统计文件的只有进程内互斥锁（msvcrt/fcntl 跨进程锁已删，包装它的
# _file_lock 上下文管理器 V9.14 也删了）。本项改为直接压它：并发读→改→写必须
# 仍得到可解析的 JSON，且一条记录都不丢。
concurrent = os.path.join(tmpdir, "concurrent.json")
# 并发项要压**另一个**统计文件：把全局路径切过去，跑完切回来。
fl._STATS_PATH = concurrent

def _hammer(i):
    for j in range(10):
        fl.penalty_delta(f"h{i}.example.com", fetch_ok=True, score=0.9)

threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(8)]
for t in threads: t.start()
for t in threads: t.join()
try:
    conc = json.load(open(concurrent, encoding="utf-8"))
    check("并发 80 次写入后 JSON 完好、8 域名齐", len(conc) == 8, f"hosts={len(conc)}")
except Exception as e:
    check("并发 80 次写入后 JSON 完好、8 域名齐", False, repr(e))
fl._STATS_PATH = stats   # 切回主统计文件，供下面的审计项使用
fl._read_cache.clear()

print("== #12 审计日志 ==")
# 审计日志路径由统计文件路径同目录派生（_audit_path），无需另行指定
hack("audit.example.com", n=0, ok=0)
for i in range(6):
    fl.penalty_delta("audit.example.com", fetch_ok=False)
lines = open(audit, encoding="utf-8").read()
check("审计含 delta 变化行", "audit.example.com delta=-0.5" in lines)
check("审计含统计后缀", "(avg=0.00 low=0.00 n=5)" in lines)
prod_log = os.path.join(fl._BASE_DIR, "logs", "filter_learning.log")
prod_content = open(prod_log, encoding="utf-8").read() if os.path.isfile(prod_log) else ""
check("审计未写入生产日志", "audit.example.com" not in prod_content)

print("== #13 统计文件本身就是排查入口 ==")
# 排查路径：直接读 temp/logs/penalty_stats.json —— 故这里验的是
# **落盘记录自带全部判据**，读文件即可回答"这个站为什么被降权"。
rec = load()["slow.example.com"]
check("统计字段齐全、可直接读", all(k in rec for k in
      ("n", "ok", "score_sum", "low", "lat_sum", "lat_n", "last", "delta", "delta_date")),
      f"keys={sorted(rec)}")

sys.exit(summary("RESULT"))
