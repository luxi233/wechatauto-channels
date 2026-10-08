# Agent Note: Windows 管理台（wechatauto-manager）——桥的监护人有了脸

Status: implemented

## Problem

桥进程三次猝死，死因均与代码质量无关：无看护人 + 从 agent 的临时
exec shell 拉起（Windows Job Object 父子连坐，`Start-Process` 脱离
控制台但脱离不了 Job）。Hermes 渠道稳是因为 ChannelCore 跑在
gateway 进程内、而 gateway 有 `Hermes_Gateway` 计划任务（每分钟
重启、999 次）看护——桥两头都没有。

用户同时提出更高一层需求：一个能管理三平台（Hermes / OpenClaw /
Octop）安装、启停、插件注入、看日志的常驻 GUI。

## Decision

在仓库 `python/` 包里新增两个模块（`manager_ops.py` + `manager.py`），
入口点 `wechatauto-manager`，tkinter 实现。

- `manager_ops.py`：纯 stdlib 逻辑层。三平台探测（路径/文件/DB/API）、
  Hermes `platforms.wechatauto.enabled` 定向 yaml 编辑、Octop HS256 JWT
  手写签发（hmac/hashlib，不依赖 PyJWT）+ REST 封装、桥的
  health/spawn/stop、日志源枚举。全部可单测。
- `manager.py`：tkinter UI。顶部状态卡（每 8s 异步探测刷新）、桥控制区
  （启动/停止/重启/自动守护开关）、平台三 Tab、日志 Tab（下拉选源 +
  跟随滚动）、设置 Tab（注册/取消开机自启）。
- 守护线程：interval 探 /health → 死亡 → cooldown（60s 防抖）→
  `CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS` 拉起 → 计数。

## Alternatives considered

- **PySide6/pywebview**：更现代但引入 ~100MB 依赖；管理台不需要 —
  tkinter 与 bridge 同一 venv 零成本。
- **Windows 服务 / schtasks**：本机 `Register-ScheduledTask` 与
  `schtasks /create` 均被权限拒绝；Startup 文件夹 bat + 常驻 GUI
  是同权限级下的等效方案。
- **PyInstaller 单 exe**：免装 Python 但丧失"和 bridge 同环境演进"的
  一致性；入口点脚本 + `pythonw -m` 已够用，后续可再打包。
- **OpenClaw 管理自动化**：本机未装 OpenClaw，做了探测 + 指引而不做
  盲写路径——npm 全局插件的安装不该由 GUI 猜。

## Consequences

收益：桥有了以 GUI 形态存在的监护人（用户看得见、关得掉、自启可配）；
三平台的启停/注入从"改文件+敲命令"降维成按钮；日志统一入口。

代价/边界：

- GUI 必须运行在**用户会话**内且不被 agent Job 收养——用
  explorer/WMI/Startup bat 拉起；从 DSH exec 直接 `pythonw -m` 拉起的
  实例仍会连坐死亡，这是交付文档要强调的操作边界。
- `wechatauto-bridge.exe` 正在被 Octop 消费时无法覆盖安装（文件锁），
  模块级更新可直接拷 site-packages 绕过。
- 登录自启 bat 内容必须纯 ASCII——中文用户名路径经 cmd 按 GBK 读会
  乱码，`%LOCALAPPDATA%` 展开是唯一安全写法（此前的 bridge bat 因此
  从未生效，本次一并修复）。
- manager.log 会记录测试触发的伪操作行（`log_line` 走真实路径），
  属可接受的噪声。
