# wechatauto-channels

把 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 打造成
**Octop**、**Hermes Agent** 和 **OpenClaw** 的个人微信消息渠道，
并附带一个 Windows 管理台 GUI 负责拉起、守护和运维整条链路。

读走本地 SQLCipher 数据库解密，写走 UIA+OCR 驱动真实客户端 —— 支持**私聊和群聊**，
覆盖官方 iLink 通道（Hermes 内置 `weixin` / OpenClaw `@tencent-weixin/openclaw-weixin`）
做不到的群消息场景。

## 架构

```
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────────┐
│ Hermes 平台插件    │ │ Octop 渠道适配器   │ │ OpenClaw 渠道插件（TS）    │
│ hermes/wechatauto│ │ (octop-gateway   │ │ openclaw/openclaw-       │
│  直 import core   │ │  channels/wca)   │ │  wechatauto              │
└────────┬─────────┘ └────────┬─────────┘ └──────────┬───────────────┘
         │                    │ HTTP (127.0.0.1:18765, Bearer token)
         │          ┌─────────▼──────────────────────────────────┐
         │          │ pythonw -m wechatauto_channels.bridge        │
         └─────────►│ /events 长轮询 · /send · /media · /context · │
                   │ /chats · /health(boot_id) · 单实例锁          │
                   └─────────┬──────────────────────▲─────────────┘
                             │ wechatauto-replica    │ 拉起/探活/日志
                   ┌─────────▼─────────┐   ┌────────┴─────────────┐
                   │ 本机微信 4.x 客户端 │   │ 管理台 GUI + watchdog │
                   │ 读 DB · 写 UIA/OCR │   │ (manager.py, tkinter)│
                   └───────────────────┘   └──────────────────────┘
```

单一事实源：`python/wechatauto_channels/core.py`（`ChannelCore`）做归一化——
消息→`ChannelEvent`、昵称→username 解析、「是否自己发的」多证据判定、
收发节流与串行化。Hermes 适配器直接 import；Octop/OpenClaw 通过本地
HTTP 桥消费同一套逻辑（Octop 适配器源码在
[octop-gateway](https://github.com/TencentCloud/octop-gateway) 的
`channels/wechatauto` 下，管理台可一键注入）。

## Bridge HTTP 接口

`GET` 全部需 `Authorization: Bearer <token>`（`--token` 或自动生成打印到控制台）：

| 端点 | 说明 |
|---|---|
| `/health` | 活性 + `wxid`/`nickname` + **`boot_id`**（消费方据此发现桥被重启并重置游标） |
| `/events?cursor=&timeout_ms=` | 长轮询事件流；响应回显 `boot_id`，`cursor > 当前 seq` 自动归零重放 |
| `/send` `/send_file` (POST) | 发送文本/文件（全局串行锁，防 UIA 串台） |
| `/context?chat=&n=` | 某会话最近 n 条消息（listen/旁听出口） |
| `/media?chat=&local_id=` | 按需解密单条媒体消息（图片密钥瞬态，过期返回 `ok:false`） |
| `/chats` `/chat?id=` `/resolve?handle=` | 会话列表 / 会话信息 / 名字→username 解析 |

另有不依赖 bridge 的只读出口：`python -m wechatauto_channels recent <群名或wxid>`
（独立 workdir 的快照读，可与运行中 bridge 共存）。

## 前置条件

- Windows 10/11，**64 位 Python 3.9+**
- 微信 PC 客户端 **4.1.12+** 已登录并保持后台运行
- `pip install wechatauto-channels`（开发期：`pip install -e python/` + `pip install wechatauto-replica`）
- 发送路径（UIA+OCR）额外需要：`pip install winsdk pypinyin`
- ⚠️ 不要以管理员身份运行 gateway/bridge——提权进程读不到普通权限微信的内存密钥

## Hermes

把 `hermes/wechatauto/` 整个目录放进 `~/.hermes/plugins/`：

```bash
cp -r hermes/wechatauto ~/.hermes/plugins/
hermes gateway setup   # 或直接写 ~/.hermes/.env
hermes gateway restart
```

配置项（`WECHATAUTO_*` env 或 `config.yaml` → `gateway.platforms.wechatauto.extra`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `account` | 自动探测 | 微信账号目录（多账号机必填） |
| `dm_policy` | `pairing` | 私聊准入：`pairing`/`allowlist`/`open`/`disabled` |
| `group_policy` | `allowlist` | 群聊准入：`allowlist`/`open`/`disabled` |
| `allowed_users` / `allow_from` | — | 私聊白名单（wxid 或显示名） |
| `group_allow_from` | — | 群白名单（chatroom id 或群名） |
| `group_require_mention` | `true` | 群里只响应 @机器人 的消息 |
| `download_media` | `false` | 解密下载图片/语音/文件供 agent 读取 |
| `send_verify` | `false` | 发送后回读 DB 确认（更慢但更可证） |
| `home_channel` | — | cron/通知默认投递目标 |

## OpenClaw

```bash
# 1) 在微信所在的 Windows 机器上启动桥
python -m wechatauto_channels --port 18765
#    控制台会打印一次性 token，复制它

# 2) 安装插件（本地目录或打包后 npm 包）
openclaw plugins install ./openclaw/openclaw-wechatauto
openclaw config set plugins.entries.wechatauto.enabled true

# 3) 配置渠道
openclaw config set channels.wechatauto.bridgeUrl http://127.0.0.1:18765
openclaw config set channels.wechatauto.bridgeToken <上一步的 token>
openclaw config set channels.wechatauto.enabled true

openclaw wechatauto status   # 健康检查
```

群聊默认 `allowlist` + `requireMention`：只有 `groupAllowFrom` 里的群、且消息里
@了机器人昵称，才会派发给 agent。

## 管理台 GUI（推荐入口）

```bash
pythonw -m wechatauto_channels.manager    # 无控制台窗口；重复启动自动聚焦已有实例
```

一个 tkinter 面板包揽整条链路的运维：

- **状态卡**：微信 / bridge / Hermes / Octop / OpenClaw 实时探活
- **桥控制**：拉起、停止、重启；15s watchdog 探活 + 60s 拉起冷却 +
  spawn 早夭检测（起来 4s 内死掉会把退出码和 stderr 尾部写进日志）
- **Octop 运维**：渠道注册注入 / 启停热重载 / 升级后适配器自动重注入 /
  **群白名单**（按群名勾选，底层存 chat_id）/ 新建知识库·连接器·技能包
  **自动开启默认注入**（60s 巡检，`supervise.auto_open_resources` 可关）
- **Hermes 运维**：`platforms.wechatauto` 启停、插件注入、gateway 重启
- **日志查看器**：manager.log / bridge stdout / fatal 崩溃日志一页看全
- **守护链**：Windows 计划任务 `Wechatauto_Manager_Watchdog` 每 10 分钟
  检查管理台本身，GUI 挂了也能被拉回

## replica_compat —— 进程内补丁层

`replica_compat.py` 对上游 wechatauto-replica 的已知缺陷做**幂等
monkeypatch**（源码特征检测，上游修好自动跳过）：

- 日志 file handler 改落到 `%TEMP%`（上游相对路径在 System32 CWD 下会
  `PermissionError`）
- `get_self_info` 的连接泄漏修复（不关连接会自锁 `contact.db`）
- DB 快照打开/合并的瞬时 `OSError`/`PermissionError` 重试

改 vendored 副本的活法重装即丢，这层不会。

## 「是不是我发的」判定（关键实现细节）

微信 4.x 消息表里的 `real_sender_id` 是**分片内短编号**，同一个人换分片就换号——
写死 `== 2` 会把旧历史里的自己算成对方（wechatbot-new v2.2.8 踩过的坑）。
本项目的判定只信正向证据（`core.py::_is_self`）：

1. 消息行 `status == 2`（跨分片稳定）—— 按会话取最近 200 行聚合投票
2. 表情/图片 XML 里 `fromusername` == 本机 wxid
3. `sender_username`（SenderName2Id 反查）== 本机 wxid

整个会话都拿不到正向证据时才退回 `sender_id in {1,2}`
（stardome 生产实测：4.1.13.x 的出站行用过 `1`，只判 `2` 会漏）。

## 「@机器人」判定（群聊门控）

正文 `@昵称` 可以被粘贴伪造，不是证据。本项目在 `core.start()` 时给
`WeChatDB._msg_row_to_dict` 打**幂等投影补丁**（上游原生支持则自动跳过），
把 `source` 列里的 `<atuserlist>` 和 appmsg 里的 `<refermsg>` 投影进消息 dict：

- `at_usernames`（三态）：list=权威（空列表=确定没@人）；`None`=不可判定，
  两端适配器退回文本 `@昵称` 兜底
- `quoted`：引用回复里 `sender==本机 wxid` **视同被@**；顺带修复上游把
  引用消息显示成「文件/链接/卡片」走 file 路由、正文被吞的问题
  （stardome 审计 D4+D5），`type` 归一化为 `quote`，正文还原 `<title>` 文本

## 其他实现要点

- **消息类型**：上游 `MSG_TYPE_NAMES` 是中文显示名（`文本`/`图片`/`文件/链接/卡片`…），
  `core.py::_canon_type` 统一归一成英文 tag（`text`/`image`/`file`…），
  两个宿主的下游 mapping 只用认英文。
- **事件去重键**：`{chat}:{sort_seq}`——`local_id` 跨分片会重复，`sort_seq`
  才是单调水印。
- **群聊发言人**：`sender_username` 缺失/纯数字时，从 content 前缀
  `wxid_…:` 恢复真实发言人 wxid（stardome 实测 sender_id 落过 `3`）。
- **发送节流**：上游 ≥1.2.3 自带拟人节奏层（默认 `natural` 会把 agent 的
  分段回复卡出 2.5–6s 间隔）；本包默认 `WECHATAUTO_RHYTHM=off`
  （env 可覆盖回 natural/calm）。
- **bridge 加固**：单实例锁（`%TEMP%\wechatauto-channels-bridge.lock`，
  防止两个 bridge 共享解密缓存）+ 崩溃取证（faulthandler/excepthook/atexit
  → `%TEMP%\wechatauto-channels\bridge-fatal.log`）。

## 测试

```bash
cd python && python -m unittest discover -s tests -v    # 60+ 项，无需微信
#    test_bridge_e2e.py 需在无活动 bridge 的机器上跑（会占单实例锁）
#    test_replica_compat.py 走 pytest
cd openclaw/openclaw-wechatauto && npm test             # 契约测试
```

真实收发验证必须在一台登录微信的 Windows 机器上跑：
`python -m wechatauto_channels` 起来后 `curl http://127.0.0.1:18765/health`。

## 风险与边界

- **合规**：本地数据库解密 + UI 自动化属于灰色方案，仅限操作自己的账号；
  有账号风控可能。低频率、带白名单使用，别群发。
- **Windows-only**：bridge/adapter 必须跑在登录微信的那台机器上。
- **发送占用真实窗口**：`send` 会移动/聚焦微信窗口，发送期间别动鼠标键盘。
- **上游维护**：wechatauto-replica 作者更新频率低；微信升级可能打破解密逻辑，
  升级微信前先自测。
- 本渠道与官方 iLink 通道**并存不冲突**：只用私聊且账号有资格时优先 iLink
  （零维护、官方背书）；需要群聊/朋友圈/全量历史时用本渠道。

## License

Apache-2.0（与 wechatauto-replica 一致）。注意：参考实现
[wechatbot-new](https://github.com/fanyuantaier/wechatbot-new) 是 GPL-3.0——
本项目只借鉴其排障结论，不复制其代码。
