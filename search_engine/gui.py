"""metasearch 极简操作界面（tkinter）——与 search.py 同目录，直接进程内导入。

设计原则（用户明确要求）：
  * 极其轻量：界面与核心操作挤在这一个文件里；
  * **没有下拉框**：8 个引擎直接摊平在面板上——先免费（打"免费"标），后付费；
  * 付费区每行给三样东西：**每月免费额度** + **官网配置跳转** + **填 Key 按钮**
    （点它弹一个小窗：一个输入框 + 一个保存按钮；保存即写入，点空白处 / Esc / 失焦即关）；
  * 免费引擎里只有 anysearch 接受 Key（选填，不配则匿名低限流），故单独留一个按钮；
    bing / quark 直连，压根没有 Key 这一说；
  * 搜索走**本目录的 search.py**（`from search import search`），进程内直接调用，
    不起子进程——search() 内部已完成 CLI 多引擎聚合 + ranking_meta 统计排序，
    这里拿到的就是排序后的结果；
  * 输出 = **引擎贡献表 + 带序号的精简列表**（标题 / 引擎与名次 / URL / 摘要，
    摘要固定 500 字上限）。引擎贡献表是**给测试用的**：一眼看出哪个引擎在干活、
    哪个在空转，以及某条结果是几家共识还是独苗；score 等打分内部量仍不打印
    —— 给下游 AI 的完整 JSON 走 deep_search MCP；
  * 密钥管理用 bin/metasearch_cli_windows_amd64.exe（apikey set/unset/list）：密钥落盘
    %APPDATA%/metasearch_cli/.env，全局共享，与 MCP 搜索后端同库同钥匙，
    每个 provider 只允许存一个 key（写入即原位替换）。

⚠️ 额度是**厂商公开免费额度**，随时会改，界面上写的是便于一眼比较的量级，
   不是账单凭据——真要掏钱前以官网控制台为准。改额度/网址改本文件顶部的表。

用法：在终端里 `python gui.py` 启动（必须有控制台，结果才有地方打）。
"""

import json
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk, messagebox

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 搜索：进程内直连 search.py（V9 CLI 桥接 + 排序都在里面）
try:
    from search import search as _ranked_search
except Exception as _exc:  # search.py 缺失/损坏时 GUI 其余功能不受影响
    print(f"搜索模块不可用（{_exc}）", file=sys.stderr)
    _ranked_search = None

# 密钥管理：bin/ 里的 CLI（key 全局落盘 %APPDATA%/metasearch_cli/.env）
KEY_CLI = _HERE / "bin" / "metasearch_cli_windows_amd64.exe"
SNIPPET_CAP = 500   # 给用户看的摘要固定上限（字）


@dataclass(frozen=True)
class Engine:
    """面板一行 = 一个引擎。quota 是"免费额度/量级"，url 是拿 Key 的控制台页。"""
    id: str            # CLI 里的 provider 名
    label: str         # 面板显示名
    quota: str         # 免费额度（月度/日度，写清单位）
    url: str = ""      # 官网配置页（空 = 没法跳转）
    key: bool = False  # 是否接受 API Key


# ---- 免费引擎（排在前面，打"免费"标）----
FREE_ENGINES = [
    Engine("bing", "Bing", "无需 Key，直连国内版，单页 9~10 条"),
    Engine("quark", "Quark", "无需 Key，移动站纯 HTTP，可翻页"),
    Engine(
        "anysearch", "AnySearch", "1,000 次/天（Key 选填，不配则匿名低限流）",
        "https://anysearch.com/console/api-keys", key=True,
    ),
]

# ---- 付费引擎（需 Key；列的是免费额度，不是套餐价）----
PAID_ENGINES = [
    Engine("exa", "Exa", "$10/月（≈1,000 次搜索）· 注册另送 $20",
           "https://dashboard.exa.ai/api-keys", key=True),
    Engine("tavily", "Tavily", "1,000 credits/月（advanced 计 2 credits）",
           "https://app.tavily.com/", key=True),
    Engine("serpapi", "SerpAPI", "250 次/月",
           "https://serpapi.com/manage-api-key", key=True),
    Engine("qianfan", "千帆 AI 搜索", "50 次/天（≈1,500 次/月）",
           "https://console.bce.baidu.com/qianfan", key=True),
    Engine(
        "metaso", "秘塔 Metaso",
        "每日刷新 100 点（当日有效）· 搜索计 10 点/次 ≈ 10 次/天 · 注册另送长期积分",
        "https://metaso.cn/search-api", key=True,
    ),
]

ENGINES = FREE_ENGINES + PAID_ENGINES
PROVIDERS = [e.id for e in ENGINES]              # 引擎贡献表里查"谁在空转"用
KEY_ENGINES = {e.id: e for e in ENGINES if e.key}  # 能存 key 的（含 anysearch 选填）


def key_cli_ready() -> bool:
    return KEY_CLI.is_file()


def print_engine_stats(items) -> None:
    """打印引擎贡献表（测试用：哪个引擎在干活、哪个在空转，一眼可见）。

    两列：**命中条数**（这个引擎返回了几条）和**独占条数**（只有它一家命中的）。
    "独占"才是"这引擎值不值得留"的关键指标：命中数高却从不独占的引擎，只是把
    别人已有的结果重发一遍（多耗一份配额、零新信息）。换引擎配置时看这一列。
    """
    hits, solo = {}, {}
    for it in items:
        engines = [e for e in (it.get("engine") or []) if e]
        for e in engines:
            hits[e] = hits.get(e, 0) + 1
        if len(engines) == 1:
            solo[engines[0]] = solo.get(engines[0], 0) + 1
    if not hits:
        print("（本次结果不含 engine 字段，无法统计引擎贡献 —— CLI 输出格式变了？）\n")
        return
    width = max(len(e) for e in hits)
    print(f"== 引擎贡献（本次共 {len(items)} 条结果）==")
    for e, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {e:<{width}}  命中 {n:>3} 条   其中独占 {solo.get(e, 0):>3} 条")
    idle = [p for p in PROVIDERS if p not in hits]
    if idle:
        # "这个引擎是不是没配 key / 挂了"的第一现场
        print(f"  未命中：{', '.join(idle)}   ← 可能没配 key，或本次确实无结果")
    print()


def run_search(keyword: str) -> None:
    """搜索：search.py 排序后 → 引擎贡献表 + 带序号精简列表（含引擎与名次）。"""
    if _ranked_search is None:
        print("搜索模块不可用（search.py 导入失败，见启动时的报错）")
        return
    items = _ranked_search(keyword, limit=15)
    print(f"共 {len(items)} 条（按高价值排序；摘要 ≤{SNIPPET_CAP} 字）\n")
    print_engine_stats(items)
    for i, it in enumerate(items, 1):
        title = " ".join(str(it.get("title") or "").split())
        snippet = " ".join(str(it.get("snippet") or "").split())[:SNIPPET_CAP]
        # 引擎 + 该引擎给出的最好名次：共识条（多家命中）一眼可辨，
        # 也是排查"为什么这条排前面"的第一手证据。
        pos = it.get("positions") or {}
        engines = " ".join(
            f"{e}#{pos.get(e, '?')}" for e in (it.get("engine") or []) if e
        )
        print(f"[{i}] {title}")
        print(f"    引擎: {engines or '—'}")
        print(f"    {it.get('url', '')}")
        if snippet:
            print(f"    摘要: {snippet}")
        print()


def save_key(provider: str, key: str) -> int:
    """写入并保存 key：写入新的就删掉原来有的（apikey set = 原位替换，每个 provider 只存一个）。

    key 走 stdin 的 `-` 形式，不放进 argv：命令行参数会暴露在进程列表里，
    同机其他用户开任务管理器就能看到明文。
    """
    return subprocess.run(
        [str(KEY_CLI), "apikey", "set", provider, "-"],
        input=key + "\n",
        text=True,
        cwd=str(KEY_CLI.parent),
    ).returncode


def delete_key(provider: str) -> int:
    return subprocess.run(
        [str(KEY_CLI), "apikey", "unset", provider],
        cwd=str(KEY_CLI.parent),
    ).returncode


def fetch_key_status() -> dict:
    """读一次 CLI 的密钥台账：provider -> (是否已配, 脱敏串)。

    面板上"已配置/未配置"靠它刷新——写盘后必须重读，不能凭界面记忆。
    """
    try:
        r = subprocess.run(
            [str(KEY_CLI), "apikey", "list", "-json"],
            cwd=str(KEY_CLI.parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        data = json.loads(r.stdout)
    except Exception as exc:  # 台账读不出来不该把界面拖死：退化成"未知"，不影响填 key
        print(f"读取密钥状态失败（{exc}）", file=sys.stderr)
        return {}
    return {
        k.get("provider"): (bool(k.get("configured")), k.get("masked") or "")
        for k in data.get("keys", [])
    }


class KeyDialog(tk.Toplevel):
    """填 Key 的小弹窗：一个输入框 + 一个保存按钮。

    退出方式三种：保存成功、Esc、点空白处（弹窗内空白 / 主窗口 / 焦点跑到别处）。
    ⚠️ 故意不加 grab_set —— 抢了指针焦点，点外面就没反应了，"点空白即关"会失效。
    """

    def __init__(self, master, engine: Engine, on_save):
        super().__init__(master)
        self.engine = engine
        self.on_save = on_save

        self.title(f"{engine.label} — 填写 API Key")
        self.resizable(False, False)
        self.transient(master)
        self.configure(padx=14, pady=12)

        frm = ttk.Frame(self)
        frm.grid()
        self.frm = frm

        ttk.Label(
            frm, text=f"{engine.label}（{engine.id}）", font=("Microsoft YaHei UI", 10, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frm, text=f"免费额度：{engine.quota}", foreground="#666"
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))

        self.var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.var, width=52)
        entry.grid(row=2, column=0, columnspan=2, sticky="we", ipady=3)
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._save())
        entry.bind("<Escape>", lambda _e: self._close())

        self.hint = tk.StringVar(value="粘贴 Key 后回车即保存；点空白处退出")
        ttk.Label(frm, textvariable=self.hint, foreground="#888").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 4)
        )

        ttk.Button(frm, text="保存", width=12, command=self._save).grid(
            row=4, column=0, sticky="e", padx=(0, 4)
        )
        ttk.Button(frm, text="取消", width=12, command=self._close).grid(row=4, column=1)

        # 点空白（弹窗本体 / 内层 frame 的空白）退出；点在输入框或按钮上不算空白
        self.bind("<Button-1>", self._on_click)
        self.bind("<Escape>", lambda _e: self._close())
        # 焦点跑到弹窗外面（点了主窗口或别的应用）也退出
        self.bind("<FocusOut>", lambda _e: self.after(150, self._close_if_focus_lost))

    def _on_click(self, event):
        if event.widget is self or event.widget is self.frm:
            self._close()

    def _close_if_focus_lost(self):
        try:
            if not self.winfo_exists():
                return
            widget = self.focus_get()
            if widget is None or widget.winfo_toplevel() is not self:
                self._close()
        except Exception:
            self._close()

    def _save(self):
        key = self.var.get().strip()
        if not key:
            self.hint.set("Key 是空的——填了再保存，或直接点空白处退出")
            return
        self._close()
        self.on_save(self.engine, key)

    def _close(self):
        if self.winfo_exists():
            self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.dialog = None
        self.key_state = {}        # provider -> (状态 Label, 清除 Button)
        self.key_configured = {}   # provider -> 是否已配 key（决定清除按钮可不可按）
        root.title("metasearch")
        root.resizable(False, False)

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(root)
        frm.grid(sticky="nsew", **pad)

        # ---- 第 0 行：关键词 + 搜索 ----
        ttk.Label(frm, text="关键词:").grid(row=0, column=0, sticky="e", **pad)
        self.query = ttk.Entry(frm, width=52)
        self.query.grid(row=0, column=1, sticky="we", **pad)
        self.query.bind("<Return>", lambda _e: self.on_search())
        self.btn_search = ttk.Button(frm, text="搜索", command=self.on_search)
        self.btn_search.grid(row=0, column=2, **pad)

        # ---- 第 1 块：免费引擎（无需付费，排最前）----
        free_box = ttk.LabelFrame(frm, text="免费引擎")
        free_box.grid(row=1, column=0, columnspan=3, sticky="we", padx=8, pady=(8, 2))
        self._build_free_rows(free_box)

        # ---- 第 2 块：付费引擎（额度 + 官网跳转 + 填 Key）----
        paid_box = ttk.LabelFrame(frm, text="付费引擎（填 Key 即用，先吃免费额度）")
        paid_box.grid(row=2, column=0, columnspan=3, sticky="we", padx=8, pady=2)
        self._build_paid_rows(paid_box)

        # ---- 状态行 ----
        self.status = tk.StringVar(value="结果打印在本命令行窗口：引擎贡献表 + 带序号精简列表（高价值在前）")
        ttk.Label(frm, textvariable=self.status, foreground="#666").grid(
            row=3, column=0, columnspan=3, sticky="w", **pad
        )

        frm.columnconfigure(1, weight=1)
        self.refresh_keys()

    # ---------- 面板搭建 ----------
    def _build_free_rows(self, box):
        for i, eng in enumerate(FREE_ENGINES):
            ttk.Label(box, text=eng.label, width=12, anchor="w").grid(
                row=i, column=0, padx=(8, 4), pady=3, sticky="w"
            )
            ttk.Label(
                box, text="免费", width=6, anchor="center",
                foreground="#0a7d33", font=("Microsoft YaHei UI", 9, "bold"),
            ).grid(row=i, column=1, padx=4)
            ttk.Label(box, text=eng.quota, width=46, anchor="w", foreground="#555").grid(
                row=i, column=2, padx=4, sticky="w"
            )
            if eng.url:
                ttk.Button(box, text="官网配置", width=10,
                           command=lambda e=eng: self.open_url(e)).grid(
                    row=i, column=3, padx=3
                )
            if eng.key:  # 只有 anysearch：Key 选填，不配也能跑（匿名限流更低）
                ttk.Button(box, text="填写 Key（选填）", width=16,
                           command=lambda e=eng: self.open_key_dialog(e)).grid(
                    row=i, column=4, padx=3
                )

    def _build_paid_rows(self, box):
        for i, eng in enumerate(PAID_ENGINES):
            ttk.Label(box, text=eng.label, width=12, anchor="w").grid(
                row=i, column=0, padx=(8, 4), pady=3, sticky="w"
            )
            ttk.Label(box, text=eng.quota, width=46, anchor="w", foreground="#555").grid(
                row=i, column=1, padx=4, sticky="w"
            )
            state = ttk.Label(box, text="读取中…", width=22, anchor="w")
            state.grid(row=i, column=2, padx=(4, 8), sticky="w")
            ttk.Button(box, text="填写 Key", width=10,
                       command=lambda e=eng: self.open_key_dialog(e)).grid(
                row=i, column=3, padx=3
            )
            if eng.url:
                ttk.Button(box, text="官网配置", width=10,
                           command=lambda e=eng: self.open_url(e)).grid(
                    row=i, column=4, padx=3
                )
            clear = ttk.Button(box, text="清除", width=5, state="disabled",
                               command=lambda e=eng: self.on_delete(e))
            clear.grid(row=i, column=5, padx=3)
            self.key_state[eng.id] = (state, clear)

    # ---------- 事件 ----------
    def on_search(self):
        kw = self.query.get().strip()
        if not kw:
            self.status.set("请输入关键词")
            return
        self._run("搜索中…（结果输出到控制台）", lambda: run_search(kw))

    def open_url(self, engine: Engine):
        """跳官网拿 Key：用系统默认浏览器，不在 tkinter 里造浏览器。"""
        if not engine.url:
            self.status.set(f"{engine.label} 没有配置页地址")
            return
        webbrowser.open(engine.url)
        self.status.set(f"已打开 {engine.label} 配置页：{engine.url}")

    def open_key_dialog(self, engine: Engine):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog._close()          # 一次只留一个弹窗，避免叠窗口
        self.dialog = KeyDialog(self.root, engine, self.on_save_key)

    def on_save_key(self, engine: Engine, key: str):
        self._run(
            f"{engine.label} 保存密钥中…",
            lambda: save_key(engine.id, key),
            done=lambda rc, _res: (
                self.status.set(
                    f"{engine.label} 密钥已保存（旧密钥已被覆盖）"
                    if rc == 0
                    else f"{engine.label} 保存失败（rc={rc}，详见控制台）"
                ),
                self.refresh_keys(),
            ),
        )

    def on_delete(self, engine: Engine):
        self._run(
            f"{engine.label} 删除密钥中…",
            lambda: delete_key(engine.id),
            done=lambda rc, _res: (
                self.status.set(
                    f"{engine.label} 密钥已删除"
                    if rc == 0
                    else f"{engine.label} 删除失败（rc={rc}，详见控制台）"
                ),
                self.refresh_keys(),
            ),
        )

    def refresh_keys(self):
        """重读 CLI 台账 → 刷新"已配置/未配置"与清除按钮可用性。"""
        self._run("读取密钥状态…", fetch_key_status,
                  done=lambda _rc, res: self._paint_keys(res))

    def _paint_keys(self, status):
        """status 可能是 dict（正常）也可能是 None/异常值——一律按空台账处理。"""
        status = status if isinstance(status, dict) else {}
        for pid, (label, clear) in self.key_state.items():
            configured, masked = status.get(pid, (False, ""))
            self.key_configured[pid] = configured
            if configured:
                label.configure(text=f"已配置 {masked}", foreground="#0a7d33")
                clear.configure(state="normal")
            else:
                label.configure(text="未配置", foreground="#a00")
                clear.configure(state="disabled")
        missing = [e.label for e in PAID_ENGINES
                   if not status.get(e.id, (False, ""))[0]]
        self.status.set(
            f"未配 Key：{'、'.join(missing)}（这些引擎本次不会参与搜索）"
            if missing
            else f"{len(PAID_ENGINES)} 个付费引擎 Key 均已配置（免费引擎无需 Key）"
        )

    # ---------- 后台执行 ----------
    def _run(self, msg, fn, done=None):
        self._set_buttons(False)
        self.status.set(msg)

        def worker():
            rc, result = 0, None
            try:
                result = fn()
            except Exception as exc:
                print("执行失败: %s" % exc, file=sys.stderr)
                rc = 1

            def finish():
                self._set_buttons(True)
                if done:
                    done(rc, result)

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _set_buttons(self, enabled):
        self.btn_search["state"] = "normal" if enabled else "disabled"
        for pid, (_label, clear) in self.key_state.items():
            # 清除按钮有两重开关：任务跑着 → 禁用；没配 key → 也禁用
            usable = enabled and self.key_configured.get(pid, False)
            clear["state"] = "normal" if usable else "disabled"


def main():
    if not key_cli_ready():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "metasearch", f"未找到密钥管理 CLI：{KEY_CLI}"
        )
        sys.exit(1)

    print(f"== metasearch 控制台就绪（密钥 CLI: {KEY_CLI}）==")
    print("   搜索 = 进程内调 search.py（CLI 聚合 + 排序）→ 引擎贡献表 + 带序号精简列表\n")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
