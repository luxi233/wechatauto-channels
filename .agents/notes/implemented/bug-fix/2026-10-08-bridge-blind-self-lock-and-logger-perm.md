# 桥"活着但永久失明"：双 bug 叠加复盘

日期：2026-10-08 · 类别：bug-fix · 影响面：wechatauto bridge 消息链路（收/发）

## 症状

桥进程健康（`/health` 200、HTTP 正常），但 `/events` 恒空、Octop 收不到任何
消息、`listener poll error: PermissionError(13, '拒绝访问。')` 每轮刷屏；
同时 workdir 里 `contact__contact.db` 被持续锁定。

## 根因（两个独立 bug 叠加，缺一不可解释全部现象）

### Bug A：`wxlog` 文件日志用相对路径，在受管进程下必然 PermissionError

`wechatauto/logger.py` 的 `setup_file_logger()` 用 `Path("wechatauto_logs")`
**相对路径**。桥由 supervisor/WMI 以服务式拉起时 `CWD=C:\Windows\System32`
（或其它受保护目录），`mkdir`/`FileHandler` 直接 `PermissionError(13)`。

致命点在于抛错位置：`_open_unlocked` 里 "WAL 合并失败→降级为仅主库快照"
是一个**本应容忍的降级分支**，该分支第一行是 `wxlog.warning(...)`——
警告本身炸了，把整个 `_open` 变成异常，向上穿过 `get_new_messages`
变成 poll error。于是微信一持续写入（WAL 合并失败是常态）→ 降级路径 →
日志器爆炸 → 每轮轮询失败 → 桥"活着但瞎"。

表象误导性极强：`PermissionError(13)` 让人第一反应是 os.replace/文件锁，
真实抛错点是 `logging.FileHandler` 的 open。**`except` 只打 `%r` 摘要
掩盖了栈——打了全栈才一击定位**。教训：诊断 daemon 循环里的周期异常，
先打一次完整 traceback 再谈猜测。

### Bug B：`get_self_info` 泄漏 SQLite 连接 → contact.db 自锁

`db.py::get_self_info` 是全部 `_open` 调用点中唯一没写
`try/finally: conn.close()` 的（同函数族 `search_contact`/`get_nickname`/
`username_by_nickname`/`get_sessions` 都有）。桥启动时 `core.start()`
调一次，contact.db 句柄永久泄漏；下次该库需要重建快照时
`os.replace(best, dst)` 被自己的句柄挡住（Windows 上目标被持有打开句柄
即 PermissionError，POSIX 语义不成立）。杀进程锁即释放——决定性实验。

另发现 `wechatauto_channels/core.py::_self_ids` 调 `_msg_conns()` 后
同样不关连接（所有权在调用方），靠 GC 兜底，一并修复。

## 修复

| 位置 | 改动 |
|---|---|
| `wechatauto/logger.py` | 日志目录改为 `tempfile.gettempdir()/wechatauto_logs`（绝对可写路径）；FileHandler 创建包 try/OSError，失败置 `file_handler=False` 哨兵（不每轮重试、永不炸调用方） |
| `wechatauto/db.py::get_self_info` | 补 `try/finally: conn.close()` |
| `wechatauto_channels/core.py::_self_ids` | `_msg_conns` 结果在 finally 中去重关闭（已同步仓库副本） |
| `wechatauto/db.py`（诊断） | `_open` 返回处登记 (dst, conn, stack) 到有界注册表(400)；os.replace 重试耗尽时 `_dump_live_conns` 打印仍存活连接的调用栈；`Listener._run` 前 2 次异常打全栈 |
| `wechatauto_channels/manager_ops.py` | `octop_channel_set_enabled` 的渠道主键从 `id` 改为 `channel_id`（ULID）——此前 PATCH 恒 404 |

注意：`wechatauto`（replica）是第三方安装包，db.py/logger.py 的修复只落在
hermes venv 的 site-packages；**重新 pip install 会丢**，上游修复需另行跟进。

## Alternatives considered

- **给 os.replace 无限重试**：最强理由是"等持锁者释放总能成功"——但对
  自锁场景是死等（自己永远不会释放自己），只是推迟失败；保留有限重试
  只用于外部瞬时占用（AV 扫描），根因必须修泄漏。
- **怀疑 Defender/外部持锁**：Defender 实时扫描确实会造成瞬时
  PermissionError（保留重试有价值），但杀桥即释放的实验证明持锁者是自己；
  且真正的 PermissionError 源头（logger 相对路径）与文件锁无关。
- **桥进程启动时 chdir 到可写目录**：能绕过 Bug A 但治标——相对路径的
  日志目录在任何非常规 CWD 下都会复炸；修库本身才是一劳永逸。
- **去掉文件日志**：最小改动但丧失现场日志能力；改为"失败降级"既保诊断
  又不影响主链路，成本相当。

## 验证

- 修复后新桥（watchdog 拉起）启动以来 **0 poll error**
- `contact__contact.db` 等全部 workdir 文件 FREE
- `/events` 收到积压消息 `你好呀`/`在吗？`；渠道重启归零 cursor 后
  Octop 日志出现 `query='你好呀\n在吗？'`、`capture_turn asst_chars=9`
- 消息库 DM 表新增 `sender=1 status=2` 出站行——回复真实送达微信

## 参考

- 代码：`wechatauto/db.py`（installed site-packages）、
  `wechatauto/logger.py`、`wechatauto_channels/core.py::_self_ids`、
  `wechatauto_channels/manager_ops.py::octop_status`
- 相关 note：`feature/2026-10-08-manager-gui-supervisor.md`（守护架构）
