# replica compat 补丁层：replica 已知缺陷的进程内修复

- lifecycle: implemented
- class: bug-fix
- date: 2026-10-08

## 背景

2026-10-08 桥失明事故的根因是两个 replica 缺陷叠加（详见同日
`bridge-blind-self-lock-and-logger-perm.md`）：`logger.py` 文件日志相对路径
在 CWD 不可写时把可容忍的降级路径变成致命错误；`db.py::get_self_info` 泄漏
sqlite 连接导致 `os.replace` 自锁。当时直接改了 site-packages——重装或升级
wechatauto-replica 即丢失，属于"修了但没沉淀"。

本补丁层把三个修复收敛为进程内 monkeypatch（`replica_compat.py`），由
`ChannelCore.start()` 在实例化 `WeChatDB` 前调用 `apply_replica_patches()`，
bridge / Hermes adapter / CLI 全部自动覆盖。

## 补丁清单

| 补丁 | 检测特征（跳过条件） | 修复内容 |
|---|---|---|
| `logger_file_handler` | 源码含 `gettempdir`/`expanduser` | 日志目录→`%TEMP%/wechatauto_logs`；OSError→哨兵 `False` 永不抛出；`_ensure_file_logger` 兜底吞一切异常 |
| `get_self_info_close` | 源码含 `conn.close()` | 重写为 `try/finally: conn.close()` |
| `open_transient_retry` | `_open` 源码含 `os.replace`/`_merge_wal`/`_open_unlocked`（不认识则跳过） | 瞬态 OSError/PermissionError 重试 2 次（0.3s/1s），RLock 串行 |

检测用 `inspect.getsource`：只对**识别出的缺陷形态**动手，形态不认识一律
`skipped:unrecognized shape` 并记日志——宁漏勿错改。

## Alternatives considered

- **不改，继续直改 site-packages**：重装即丢，且无法随仓库分发给
  Hermes/Octop/OpenClaw 三个宿主。放弃。
- **fork replica 上游仓库**：维护成本高，replica 迭代快，fork 会迅速腐烂；
  且上游修复后我们仍按旧版跑。monkeypatch + 源码特征检测可自动让位。
- **抄 V5 全套 compat**（快照重拍换路径、`_merge_wal` 页数修复、listener
  退避）：V5 钉的是 replica 1.2.1，错误签名不同（`数据库合并失败(文件被
  微信并发改写)` 在我们版本不存在）。只采纳其架构思想（进程内补丁+特征
  检测+幂等标记），补丁面按我们实测过的缺陷收敛。
- **把调试插桩（conn 注册表+调用栈 dump、Listener 全栈前 N 次）也常驻**：
  是本次定位的胜负手，但常驻有内存/噪音成本。决定不放进补丁层，保留在
  事故复盘 note 里作为排查手法记录。

## 验证

- 12 项假模块单测：buggy 形态应用、上游已修跳过、不认识形态跳过、哨兵
  不抛、重试收敛。
- 真实 replica（已带手工修复的 site-packages）上 `apply_replica_patches()`
  返回 `skipped:upstream already absolute` / `skipped:upstream already
  closes` / `applied`——检测路径本身得到实证。

## 遗留

- 手工打进 site-packages 的 db.py/logger.py 修复与补丁层结果等价，保留
  无害（检测会跳过）；上游更新后补丁层自动接管，不再需要手工补丁。
- replica 上游仓库若接受这些修复，补丁自动转 skipped——补丁层即"带着
  退役开关"的技术债。
