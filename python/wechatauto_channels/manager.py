"""wechatauto-manager — Windows 常驻管理台（tkinter，stdlib 零依赖）。

职责：
- 状态总览：微信 / bridge / Hermes / Octop / OpenClaw
- bridge 保活：守护线程定时探 /health，死亡自动拉起（冷却防抖）
- 平台管理：Hermes/Octop 的启用禁用、插件注入、重启
- 日志查看：manager.log / bridge 双日志 / hermes / octop

进程模型说明：本程序被设计为由用户会话启动（开机自启 bat 或双击），
其子进程（bridge）不在任何 agent shell 的 Job Object 内——这正是
之前「桥被连坐杀死」问题的结构性解法。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from . import manager_ops as ops

GREEN, RED, GREY, ORANGE = "#1a7f37", "#cf222e", "#6e7781", "#bf8700"


class ManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("wechatauto 管理台")
        self.geometry("860x560")
        self.minsize(720, 480)
        self.cfg = ops.load_config()
        self._ui_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._restart_count = 0
        self._last_restart = 0.0
        self._last_octop_check = 0.0
        self._last_reinject = 0.0
        self._last_auto_open = 0.0
        self._build_ui()
        self._start_watchdog()
        self.after(500, self._drain_queue)
        self.after(8000, self._refresh_loop)
        ops.log_line("manager gui started")

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self.status_labels: dict[str, tk.Label] = {}
        for key in ("wechat", "bridge", "hermes", "octop", "openclaw"):
            lbl = tk.Label(top, text=f"{key}: ?", fg=GREY,
                           font=("Microsoft YaHei UI", 10, "bold"),
                           padx=10, pady=4, relief="groove")
            lbl.pack(side="left", padx=4)
            self.status_labels[key] = lbl

        bridge = ttk.LabelFrame(self, text="Bridge（消息桥）", padding=6)
        bridge.pack(fill="x", padx=6, pady=4)
        ttk.Button(bridge, text="启动", command=self._bridge_start).pack(
            side="left", padx=4)
        ttk.Button(bridge, text="停止", command=self._bridge_stop).pack(
            side="left", padx=4)
        ttk.Button(bridge, text="重启", command=self._bridge_restart).pack(
            side="left", padx=4)
        self.sv_var = tk.BooleanVar(
            value=self.cfg["supervise"]["enabled"])
        ttk.Checkbutton(
            bridge, text="自动守护（掉线自动拉起）", variable=self.sv_var,
            command=self._toggle_supervise).pack(side="left", padx=12)
        self.restart_lbl = tk.Label(bridge, text="守护重启: 0 次", fg=GREY)
        self.restart_lbl.pack(side="right", padx=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)
        self._build_hermes_tab(nb)
        self._build_octop_tab(nb)
        self._build_openclaw_tab(nb)
        self._build_log_tab(nb)
        self._build_settings_tab(nb)

    def _build_hermes_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Hermes")
        self.h_status = tk.Text(f, height=8, width=90, state="disabled",
                                font=("Consolas", 9))
        self.h_status.pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="注入插件",
                   command=lambda: self._run_bg(
                       ops.hermes_inject_plugin, "注入插件")).pack(
            side="left", padx=4)
        ttk.Button(row, text="启用 wechatauto",
                   command=lambda: self._run_bg(
                       lambda c: ops.hermes_set_platform_enabled(c, True),
                       "启用平台")).pack(side="left", padx=4)
        ttk.Button(row, text="停用 wechatauto",
                   command=lambda: self._run_bg(
                       lambda c: ops.hermes_set_platform_enabled(c, False),
                       "停用平台")).pack(side="left", padx=4)
        ttk.Button(row, text="重启 gateway",
                   command=lambda: self._run_bg(
                       ops.hermes_gateway_restart, "重启 gateway")).pack(
            side="left", padx=4)

    def _build_octop_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Octop")
        self.o_status = tk.Text(f, height=8, width=90, state="disabled",
                                font=("Consolas", 9))
        self.o_status.pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="注入 adapter",
                   command=lambda: self._run_bg(
                       ops.octop_inject_adapter, "注入 adapter")).pack(
            side="left", padx=4)
        ttk.Button(row, text="启用渠道",
                   command=lambda: self._run_bg(
                       lambda c: ops.octop_channel_set_enabled(c, True),
                       "启用渠道")).pack(side="left", padx=4)
        ttk.Button(row, text="停用渠道",
                   command=lambda: self._run_bg(
                       lambda c: ops.octop_channel_set_enabled(c, False),
                       "停用渠道")).pack(side="left", padx=4)
        ttk.Button(row, text="重启 Octop 后端",
                   command=self._octop_restart_confirm).pack(
            side="left", padx=4)

    def _build_openclaw_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="OpenClaw")
        self.c_status = tk.Text(f, height=10, width=90, state="disabled",
                                font=("Consolas", 9))
        self.c_status.pack(fill="x")
        tk.Label(
            f, fg=GREY, justify="left",
            text="OpenClaw 插件是 npm 包（openclaw-wechatauto），"
                 "经 HTTP 桥接入本机。\n"
                 "安装后此页会显示检测状态；未安装时可运行\n"
                 "  npm i -g openclaw-wechatauto").pack(anchor="w", pady=8)

    def _build_log_tab(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="日志")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.log_src = ttk.Combobox(bar, state="readonly", width=30)
        self.log_src.pack(side="left")
        self.log_src.bind("<<ComboboxSelected>>", lambda _e: self._load_log())
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动刷新+跟随", variable=self.follow_var
                        ).pack(side="left", padx=8)
        ttk.Button(bar, text="刷新", command=self._load_log).pack(
            side="left", padx=4)
        self.log_text = tk.Text(f, font=("Consolas", 9), wrap="none")
        vs = ttk.Scrollbar(f, orient="vertical",
                           command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vs.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

    def _build_settings_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="设置")
        ttk.Button(f, text="注册开机自启（本管理台+桥监护）",
                   command=self._register_autostart).pack(anchor="w", pady=4)
        ttk.Button(f, text="取消开机自启",
                   command=self._remove_autostart).pack(anchor="w", pady=4)
        tk.Label(f, fg=GREY, justify="left", text=(
            "配置文件: ~/.wechatauto/manager.json\n"
            "可改 bridge.exe / token / 端口 / 守护间隔；保存后重启管理台生效。"
        )).pack(anchor="w", pady=10)

    # ------------------------------------------------------------- actions

    def _run_bg(self, fn, label: str):
        def work():
            try:
                ok, msg = fn(self.cfg)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            self._ui_queue.put(("op", (label, ok, msg)))
        threading.Thread(target=work, daemon=True).start()

    def _octop_restart_confirm(self):
        if messagebox.askyesno(
                "确认", "重启 Octop 后端？（约 10 秒，渠道会自动重连）"):
            self._run_bg(ops.octop_restart, "重启 Octop")

    def _bridge_start(self):
        self._run_bg(ops.bridge_start, "启动桥")

    def _bridge_stop(self):
        def stop(_c):
            return ops.bridge_stop()
        self._run_bg(stop, "停止桥")

    def _bridge_restart(self):
        def restart(_c):
            ops.bridge_stop()
            time.sleep(1)
            ok, msg = ops.bridge_start(self.cfg)
            if ok:
                ops.bridge_wait_healthy(self.cfg, timeout_s=45)
            return ok, msg
        self._run_bg(restart, "重启桥")

    def _toggle_supervise(self):
        self.cfg["supervise"]["enabled"] = self.sv_var.get()
        ops.save_config(self.cfg)
        ops.log_line(
            f"supervise enabled={self.sv_var.get()}")

    def _register_autostart(self):
        startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
                   / "Start Menu" / "Programs" / "Startup")
        import sys
        pyw = Path(sys.executable).with_name("pythonw.exe")
        bat = startup / "wechatauto-manager.bat"
        bat.write_text(
            f'@echo off\r\nstart "" "{pyw}" -m wechatauto_channels.manager\r\n',
            encoding="ascii")
        messagebox.showinfo("开机自启", f"已写入 {bat}")

    def _remove_autostart(self):
        bat = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup"
               / "wechatauto-manager.bat")
        bat.unlink(missing_ok=True)
        messagebox.showinfo("开机自启", "已移除")

    # ----------------------------------------------------------- watchdog

    def _start_watchdog(self):
        def loop():
            while not self._stop.is_set():
                interval = max(5, int(
                    self.cfg["supervise"].get("interval_s", 15)))
                self._stop.wait(interval)
                if self._stop.is_set():
                    break
                if not self.cfg["supervise"]["enabled"]:
                    continue
                now = time.time()
                self._auto_open_octop_resources(now)
                self._maybe_reinject_octop(now)
                if ops.bridge_health(self.cfg):
                    continue
                cooldown = self.cfg["supervise"].get(
                    "restart_cooldown_s", 60)
                if now - self._last_restart < cooldown:
                    ops.log_line("watchdog: bridge down, cooldown 中")
                    continue
                ops.log_line("watchdog: bridge down, 自动拉起")
                ok, msg = ops.bridge_start(self.cfg)
                self._last_restart = now
                self._restart_count += 1
                ops.log_line(
                    f"watchdog restart #{self._restart_count}: {msg}")
        threading.Thread(target=loop, daemon=True).start()

    def _maybe_reinject_octop(self, now: float):
        """Octop 升级会覆盖 portable 包里的注入 adapter。丢了就自动重注入。

        触发条件：已安装 + DB 有渠道行（说明曾经注入过）+ adapter 文件或
        _CHANNEL_MAP 注册丢失。扫描间隔 60s，重注入动作冷却 10min；
        注入成功后重启后端加载新代码。
        """
        if not self.cfg["supervise"].get("octop_reinject", True):
            return
        if now - self._last_octop_check < 60:
            return
        self._last_octop_check = now
        st = ops.octop_status(self.cfg)
        if not (st["installed"] and st["channel_row"]):
            return
        if st["adapter_files"] and st["registered"]:
            return
        if now - self._last_reinject < 600:
            return
        self._last_reinject = now
        ops.log_line("watchdog: octop adapter 丢失（疑似升级覆盖），自动重注入")
        ok, msg = ops.octop_inject_adapter(self.cfg)
        ops.log_line(f"watchdog reinject: ok={ok} {msg}")
        if ok:
            code, _ = ops.octop_api(self.cfg, "POST", "/update/restart")
            ops.log_line(f"watchdog reinject: backend restart http={code}")

    def _auto_open_octop_resources(self, now: float):
        """新建的 KB/连接器/技能包默认关闭 → IM 渠道永远裸奔。
        每 60s 扫一次，自动把默认声明补齐（default_open / 绑定到 main）。"""
        if not self.cfg["supervise"].get("auto_open_resources", True):
            return
        if now - self._last_auto_open < 60:
            return
        self._last_auto_open = now
        for item in ops.octop_auto_open_defaults(self.cfg):
            ops.log_line(f"auto-open: {item} 已开启默认注入")

    # ------------------------------------------------------------ refresh

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "op":
                    label, ok, msg = payload
                    ops.log_line(f"op[{label}] ok={ok} {msg}")
                    if not ok:
                        messagebox.showwarning(label, msg)
        except queue.Empty:
            pass
        self.after(400, self._drain_queue)

    def _refresh_loop(self):
        def probe():
            st = {
                "wechat": ops.wechat_running(),
                "bridge": ops.bridge_health(self.cfg),
                "hermes": ops.hermes_status(self.cfg),
                "octop": ops.octop_status(self.cfg),
                "openclaw": ops.openclaw_status(self.cfg),
            }
            self._ui_queue.put(("status", st))
        threading.Thread(target=probe, daemon=True).start()
        self.after(8000, self._refresh_loop)
        if self.follow_var.get():
            self._load_log()

    def _set_status(self, key, text, color):
        self.status_labels[key].config(text=f"{key}: {text}", fg=color)

    def _apply_status(self, st):
        self._set_status("wechat", "运行中" if st["wechat"] else "未运行",
                         GREEN if st["wechat"] else RED)
        h = st["bridge"]
        if h and h.get("ok"):
            self._set_status("bridge", f"正常 {h.get('nickname','')}", GREEN)
        else:
            self._set_status("bridge", "离线", RED)
        hs = st["hermes"]
        if not hs["installed"]:
            self._set_status("hermes", "未安装", GREY)
        elif hs["connected"] == "connected":
            self._set_status("hermes", "已连接", GREEN)
        elif hs["platform_enabled"] is False:
            self._set_status("hermes", "已停用", ORANGE)
        else:
            self._set_status("hermes", "待接入", ORANGE)
        os_ = st["octop"]
        if not os_["installed"]:
            self._set_status("octop", "未安装", GREY)
        elif os_["connected"]:
            self._set_status("octop", "已连接", GREEN)
        elif os_["channel_row"]:
            self._set_status("octop", "已断开", RED)
        else:
            self._set_status("octop", "未注入", ORANGE)
        cs = st["openclaw"]
        self._set_status("openclaw",
                         "已安装" if cs["installed"] else "未安装",
                         GREEN if cs["installed"] else GREY)
        self._fill_text(self.h_status, _fmt_hermes(hs))
        self._fill_text(self.o_status, _fmt_octop(os_))
        self._fill_text(self.c_status, _fmt_openclaw(cs))

    def _fill_text(self, widget: tk.Text, text: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _load_log(self):
        sources = ops.log_sources(self.cfg)
        cur = self.log_src.get()
        if not cur:
            vals = list(sources)
            self.log_src["values"] = vals
            if vals:
                self.log_src.set("manager.log"
                                 if "manager.log" in sources else vals[0])
                cur = self.log_src.get()
        path = sources.get(cur)
        if not path:
            return
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", ops.tail_lines(path, 400))
        self.log_text.see("end")

    # dispatch status updates from probe thread
    def _apply_queued_status(self):
        pass

    def destroy(self):
        self._stop.set()
        ops.log_line("manager gui closed")
        super().destroy()


def _fmt_hermes(st: dict) -> str:
    return (f"home:           {st['home'] or '未检测到'}\n"
            f"已安装:         {st['installed']}\n"
            f"插件已注入:     {st['plugin']}\n"
            f"plugins.enabled:{st['whitelisted']}\n"
            f"platform 开关:  {st['platform_enabled']}\n"
            f"gateway 状态:   {st['connected'] or '未知'}\n\n"
            f"说明：Hermes 插件为进程内直连，不依赖桥；\n"
            f"      「重启 gateway」约需 20-40 秒。")


def _fmt_octop(st: dict) -> str:
    return (f"home:          {st['home'] or '未检测到'}\n"
            f"已安装:        {st['installed']}\n"
            f"adapter 已注入:{st['adapter_files']}\n"
            f"已注册 _MAP:   {st['registered']}\n"
            f"渠道行:        {st['channel_row']}\n"
            f"前端补丁:      {st.get('frontend_patched')}\n"
            f"渠道名:        {st['name']}\n"
            f"runtime:       connected={st['connected']}\n\n"
            f"说明：注入后需重启 Octop 后端才会加载新渠道代码；\n"
            f"      渠道行需在 DB 中存在（注入按钮不建行）。")


def _fmt_openclaw(st: dict) -> str:
    return (f"home:      {st['home']}\n"
            f"已安装:    {st['installed']}\n"
            f"插件已装:  {st['plugin']}\n")


# status 轮询泵 —— probe 线程塞 queue，主线程取
def _status_pump(app: ManagerApp):
    try:
        while True:
            kind, payload = app._ui_queue.get_nowait()
            if kind == "status":
                app._apply_status(payload)
    except queue.Empty:
        pass
    app.after(300, lambda: _status_pump(app))


def main():
    # 单实例守卫：两个 GUI = 两个 watchdog 抢同一个桥，虽无害但会造成
    # 重复拉起和日志错乱。lock 文件随进程退出自动释放。
    lock_path = ops.LOG_DIR / "manager.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = open(lock_path, "a+b")  # noqa: SIM115 - 生命周期=进程
    try:
        import msvcrt
        lock_fp.seek(0)  # 所有实例锁同偏移，a+ 的 EOF 语义会导致各锁各的
        msvcrt.locking(lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
    except (ImportError, OSError):
        # --respawn：计划任务守护者调用，已有实例时静默退出不弹窗
        if "--respawn" not in sys.argv:
            messagebox.showwarning("wechatauto 管理台", "已有实例在运行（托盘/后台）。本窗口退出。")
        return
    app = ManagerApp()
    app.after(300, lambda: _status_pump(app))
    app.mainloop()


if __name__ == "__main__":
    main()
