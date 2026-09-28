"""Deep Search MCP V10.0 - 自包含深度搜索服务器（纯模块导入，无子进程）

自包含单一入口：宿主只需注册这一个 MCP（server.py）。搜索由 Go 二进制
metasearch_cli_windows_amd64.exe 多引擎聚合完成（search_engine/search.py 桥接 + ranking_meta
统计排序），抓取由 web_fetch.py（Scrapling 降级链 + PDF 落盘支链）完成，
skill.py 为主编排器。

变更记录见 CHANGELOG.md（V10.0 及历史归档）。
"""
__version__ = "10.0"

# V9.6：不再 re-export DeepSearchConfig / DeepSearchSkill —— 全仓零引用（server.py
# 与 tests 都是从各自模块直接 import），却让任何 `import deep_search`（哪怕只为读
# __version__）必然拖起 scrapling / httpx / trafilatura 整条重链。
