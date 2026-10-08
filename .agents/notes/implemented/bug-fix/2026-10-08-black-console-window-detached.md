# 黑窗复活事件：DETACHED_PROCESS 管不住孙进程

## 现象

杀掉一个标题为 `wechatauto-bridge.exe` 的 Windows Terminal 黑窗后，
它又在每次桥被守护逻辑拉起时复活。

## 根因（进程树实锤）

```
wechatauto-bridge.exe (DETACHED_PROCESS 拉起，无 console)
    └─ python.exe (PyInstaller 引导器起的内嵌解释器，console 子系统)
        └─ conhost.exe ← 它 AllocConsole → 默认终端 WT 弹黑窗
```

`DETACHED_PROCESS` 只保证**桥自己**没有 console。PyInstaller 引导器
会再起一个 console 子系统的 `python.exe` 孙进程——孙进程没有任何可
继承的 console，于是自己 `AllocConsole`，Windows 11 默认终端设置把它
渲染成一个可见的 Windows Terminal 窗口。

## 修正

`CREATE_NO_WINDOW`（不带 DETACHED）给桥一个**隐藏 console**，孙进程
直接继承这个隐藏 console，不再 AllocConsole。实弹验证：
watchdog 拉起新桥后 `WindowsTerminal` 进程数 = 0，`/health` 正常。

同时新增 `_bridge_argv()`：venv 里存在 `pythonw.exe` 时优先用
`pythonw -m wechatauto_channels.bridge` 形态——windowed 解释器
全链路零 console，连 PyInstaller 孙进程都不存在，是更彻底的形式。
该形态在下次 GUI/adapter 自然重启时生效。

## 对照实验记录

| 拉起方 | 标志位 | 结果 |
|---|---|---|
| adapter 旧代码 | `DETACHED\|NPG\|NO_WINDOW` | 有窗（DETACHED 胜出，孙进程自分配）|
| GUI 旧代码 | `DETACHED\|NPG\|BREAKAWAY` | 有窗 |
| GUI 新代码 | `NO_WINDOW\|NPG\|BREAKAWAY` | **无窗** ✅ |

## 教训

- `DETACHED_PROCESS` 和 `CREATE_NO_WINDOW` 不是"加强版彼此"：
  前者是**不给 console**，后者是**给看不见的 console**。多进程链
  （exe → 孙进程）场景下，孙子要的是"有个可继承的隐藏 console"，
  不是"没有 console"。
- Windows 的默认终端宿主机制让 console 分配不再隐形——Win10 时代
  这只会出个 conhost 黑框一闪而过，Win11 + WT 默认终端会开一个
  持久命名的窗口，用户体验完全不同。
