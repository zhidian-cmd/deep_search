# -*- coding: utf-8 -*-
"""取消路径「显式关闭连接」回归测试（V9.2，2026-09-20）。

背景（同日端到端测评）：进程退出时报 `unclosed transport` / `I/O on closed pipe`。
隔离实测：CLI 搜索子进程单独跑、stealthy 浏览器单独跑都**不复现**，只有
"抓够目标后取消在途 httpx 请求"这条路径复现 → 即 web_fetch 第 0 级。

本测试用本地黑洞 TCP 服务（accept 后不回响应）制造"永久等在途"的请求，
再用抓取预算/单条熔断触发取消，断言：
  ① 被取消的 httpx client 全部 is_closed；
  ② 兜底登记表清空（没有残号留给进程退出时爆炸）。

不触外网、不写生产状态 —— penalty_delta 已打桩，否则会往
temp/logs/penalty_stats.json 写 127.0.0.1 的假失败样本、污染真实学习状态。
"""
import asyncio, contextlib, sys

from _harness import check, summary

from deep_search.config import DeepSearchConfig
from deep_search import web_fetch as W


@contextlib.contextmanager
def record_clients(created):
    """记录 web_fetch 内部创建的每个 httpx client，供"是否被关"断言。"""
    real = W.httpx.AsyncClient
    def factory(*a, **k):
        c = real(*a, **k)
        created.append(c)
        return c
    W.httpx.AsyncClient = factory
    try:
        yield
    finally:
        W.httpx.AsyncClient = real


async def black_hole():
    """只 accept、读了请求就不回应答的 TCP 服务：httpx 会一直等。"""
    async def handler(reader, writer):
        try:
            await reader.read(1)
            await asyncio.sleep(3600)
        except Exception:
            pass
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def new_manager(**over):
    cfg = DeepSearchConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    mgr = W.ScraplingManager(cfg)
    # 链条止步于第 0 级：本测试只测 httpx 的连接回收，不去惊动浏览器
    # ⚠️ _httpx_first 返回 (text, page_title, status) 三元组，PDF 时 text 位是
    #    `_PDF_DONE` 哨兵。这里还原成 _fetch_chain 的对外契约：
    #    _PDF_DONE / (text, page_title) / None。
    async def chain_httpx_only(url, host_cache, *a, **k):
        text, title, _status = await mgr._httpx_first(url, host_cache)
        if text is W._PDF_DONE:
            return W._PDF_DONE
        return (text, title) if text else None
    mgr._fetch_chain = chain_httpx_only
    return mgr


async def main():
    W.filters_learn.penalty_delta = lambda *a, **k: 0.0   # 学习层打桩，不落盘

    server, port = await black_hole()
    urls = [f"http://127.0.0.1:{port}/p{i}" for i in range(20)]

    print("== 1. 抓取预算到点取消在途请求 ==")
    created = []
    with record_clients(created):
        mgr = new_manager(scrape_budget=None, fetch_timeout_per_url=30)
        res = await mgr.fetch_all(urls, fetch_count=W.MAX_CONCURRENT_FETCHES, budget=0.6)
    check("预算到点无成品返回", res == [], f"len={len(res)}")
    check("确有并发请求被创建", len(created) == W.MAX_CONCURRENT_FETCHES, f"created={len(created)}")
    check("被取消的 client 全部显式关闭",
          all(c.is_closed for c in created),
          f"未关 {sum(1 for c in created if not c.is_closed)} 个")
    check("登记表已清空（无残号）", not mgr._open_clients, f"leftover={len(mgr._open_clients)}")

    print("== 2. 单条熔断（wait_for 超时）取消 ==")
    created2 = []
    with record_clients(created2):
        mgr2 = new_manager(fetch_timeout_per_url=0.6)
        res2 = await mgr2.fetch_all(urls[:2], fetch_count=2, budget=None)
    check("熔断后无成品返回", res2 == [], f"len={len(res2)}")
    check("熔断路径的 client 也全部关闭",
          created2 and all(c.is_closed for c in created2),
          f"created={len(created2)} 未关={sum(1 for c in created2 if not c.is_closed)}")
    check("登记表已清空", not mgr2._open_clients, f"leftover={len(mgr2._open_clients)}")

    print("== 3. 兜底：残留登记也能被关掉 ==")
    mgr3 = new_manager()
    stray = W.httpx.AsyncClient()
    mgr3._open_clients.add(stray)
    await mgr3._close_leftover_clients()
    check("残留 client 被关闭", stray.is_closed and not mgr3._open_clients)

    server.close()
    await server.wait_closed()
    return summary()

sys.exit(asyncio.run(main()))
