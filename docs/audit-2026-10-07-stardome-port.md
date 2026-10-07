# 审计记录：对照 stardome v5 生产经验的改进（2026-10-07）

参照物：luxi233/stardome `py_bridge/wechatauto_v2_server.py` +
`wechatauto_compat.py`（同一上游 wechatauto-replica 的重度生产用户）。

## 发现与处置

| # | 发现 | 证据 | 处置 |
|---|------|------|------|
| D1 | 消息类型名是中文（`文本`/`图片`/`文件/链接/卡片`），core 里 `mtype in ("image",…)` 永假——媒体下载是死代码 | 上游 `db.MSG_TYPE_NAMES` | `_canon_type` 归一化为英文 tag |
| D2 | 事件 id 用 `local_id`，跨分片重复会去重吞消息 | 上游 `get_message_row` 注释 | 改用 `sort_seq` 水印 |
| D3 | `is_self` 兜底只判 `sender_id==2` | stardome 实测 4.1.13.x 出站行用 `1` | 兜底 `{1,2}`（仍限无正向证据时） |
| D4 | 引用消息被上游显示成「文件/链接/卡片」走 file 路由、正文被吞 | stardome 审计 D4+D5 | `_msg_row_to_dict` 投影补丁：`type=引用` + `<title>` 正文还原 |
| D5 | `@` 判定走正文文本，可粘贴伪造 | stardome compat 补丁 | `<atuserlist>` 投影 → `at_usernames` 三态（list 权威 / None 兜底） |
| D6 | 无单实例锁，双 bridge 共享解密缓存 | wechatbot-new v2.2.5 WAL 损坏先例 | `%TEMP%` 文件锁（msvcrt/fcntl） |
| D7 | bridge 静默死亡无取证手段 | stardome 2026-09-22 两次 code=1 事故 | faulthandler + excepthook + atexit → `bridge-fatal.log` |
| D8 | 上游 ≥1.2.3 默认 rhythm=natural 节流（2.5–6s/写、冷却 30–75s），agent 分段回复被卡 | stardome 生产注释 + 上游 rhythm.py | `WECHATAUTO_RHYTHM=off` setdefault（env 可覆盖） |
| D9 | listener 注册无重试 | stardome `_ensure_listener` 5 次重试 | 5 次重试 + `_invalidate_cache` |

## 没搬的东西（Alternatives considered）

- **stdio JSON-RPC 传输**（stardome 用 stdout 推事件）：我们的 HTTP bridge 对
  OpenClaw 这种「插件可能被宿主随时启停」的形态更合适——进程隔离 + 可独立
  诊断（curl /health）。不换。
- **stardome 的整套 wechatauto_compat**（UIA COM 重试/unicode 剪贴板/send_confirm
  等 9 个补丁）：针对上游 1.2.2.2；我们 floor 是 1.2.4.4，部分已原生修复。
  先只拿投影补丁，其余真机复现再补——不预置无证据的复杂度。
- **朋友圈/群成员事件**：渠道定位是消息收发，moments 不进 MVP。

## 验证

- Python `unittest discover`：34/34（含新增 test_bridge_e2e.py：stub
  wechatauto → 真 HTTP 端点全链路）
- OpenClaw 侧：tsc 0 error + vitest 8/8
- 真机（Windows + 微信 4.x 登录态）收发仍未验证——契约级证据到此为止。
