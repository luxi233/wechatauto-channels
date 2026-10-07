# Agent Note: 发送串行化 + id→显示名解析 —— 修复并发串话与群聊发不出

Status: implemented

## Problem

生产实测两个症状（同一根因链上的两个果子）：

1. **串话**：私聊和群聊消息同时到达时，两条回复都落进私聊窗口。
2. **搜索浪费/群聊发不出**：每条回复前都要走搜索框，而搜索框搜不到
   wxid/@chatroom id——群回复在搜索框/侧栏空转 ~75s 后失败。

根因三点（replica `guia.py` + 本层调用方式叠加）：

- `quick_send` 每次调用 `WeChatGUI()` 新建实例，`_current_chat` /
  `_last_input_box` / `_cached_db` / UIA 驱动全部冷启动——逐条发送
  每次都重走 open_chat/搜索定位，是主要耗时点。
- `who` 传的是会话 username（wxid/`@chatroom`）。`uia.current_chat()`
  返回**显示名**，与 id 比对永假 → open_chat 每次都跑；且
  `_resolve_search_keyword` 只查 `db.search_contact`（群聊不在通讯录
  记录里）→ 搜索框收到原始 id，永远无结果。
- **无任何发送锁**：Hermes gateway 每会话一个 worker 线程，并发
  `core.send_text` → 两个 `WeChatGUI` 实例驱动同一个真实窗口、共享
  同一个搜索框。线程 A 搜 id 无果时会消费线程 B 键入的搜索词/结果，
  或 B 打开 DM 后 A 的 send_text 直接落进当前焦点会话。

## Decision

`ChannelCore` 写侧三件套：

- **`_send_lock`（`threading.Lock`）**：`send_text`/`send_file` 全程
  持锁。锁是每进程一把——gateway in-process 插件与 bridge sidecar
  若同时运行仍竞争同一窗口（bridge HTTP 层原本就有自己的
  `state._send_lock`，两层锁顺序一致无死锁）；同一时刻只应有一个
  宿主在写。
- **共享 `WeChatGUI` 实例（`_gui()` 惰性创建）**：`_current_chat`、
  `_cached_db`（密钥扫描 ~6s）、UIA 驱动跨调用复用，连续同会话发送
  命中 replica 的复用路径。
- **`_who_name()`**：`wxid_*`/`gh_*`/`*@chatroom`/`filehelper` 先经
  `_display_name()`（`group_id_to_name`/`get_nickname`，带缓存）解析
  成显示名再交给 GUI 层；`to` 回包仍是 username。`uia.current_chat()
  == who` 的免搜索判断从此真命中。

验证口径同时修正：`verify=True` 时改用本层 `_verify_sent`（拿
ChannelCore 已开的 `self.db` 按会话 username 回读，带 sort_seq 水位；
附件只认水位后有自己发出的新行）。replica 自带 verify 另建 WeChatDB
且按显示名反解 username，群聊口径对不上。按 username 校验还有个副
作用：打进错误会话的消息在目标表里查不到，串话会被 verify 捕获。

盲快路径兜底：`send_msg` 的 `who == _current_chat and _last_input_box`
快路径不校验真实前台会话，用户手动切窗后旧输入框元素可能属于别的
会话——每次发送前置空 `gui._last_input_box`，走 `current_chat()`/
标题 OCR 校验过的路径，复用收益保留。

## Alternatives considered

- **改 replica `_resolve_search_keyword` 让它处理 @chatroom**：能修
  群聊搜索，但 `current_chat() == who`（名 vs id）仍永假，每条还是
  重搜；且 replica 是上游 PyPI 包，本层控制面更稳。作为上游建议保留。
- **在 adapter/gateway 层加锁**：adapter 可以，但 bridge sidecar、
  OpenClaw、任何未来宿主都要各自记得加——放进 ChannelCore 一次到位。
- **复用 `_last_input_box` 盲快路径不换**：省一次探测换来串话隐患，
  用户手动切窗是真实场景，放弃。
- **verify 继续走 replica**：`_verify_usernames` 对群名查
  search_contact 必空 → 回退拿群名查消息表 → 永远 False → 触发
  Hermes 兜底重发（上一轮刚修过的坑），放弃。

## Consequences

收益：并发发送进程内串行，串话路径物理封死；群聊发送从"必失败"
变成可命中；同会话连发跳过重复搜索；verify 口径与消息表一致且能
检出串话。

代价与边界：

- 跨进程（gateway + bridge 同时跑）锁无效，靠部署约定一个写宿主。
- 显示名解析引入同名歧义风险（同昵称联系人/同群名），搜索框本来就
  只认名字，风险与原来等同。
- `send_msg` 盲快路径被禁用，同会话连发仍走 UIA `current_chat()`
  校验路径，比盲路径多一次树查询。
- `pi-wechat-worker` 的 `py_bridge` 是独立实现，同类问题（并发写、
  id 直接进搜索框）需要在其仓库单独评估。
