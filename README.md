# wechatauto-channels

把 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 打造成
**Hermes Agent** 和 **OpenClaw** 的个人微信消息渠道。

读走本地 SQLCipher 数据库解密，写走 UIA+OCR 驱动真实客户端 —— 支持**私聊和群聊**，
覆盖官方 iLink 通道（Hermes 内置 `weixin` / OpenClaw `@tencent-weixin/openclaw-weixin`）
做不到的群消息场景。

## 架构

```
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ Hermes 平台插件           │   │ OpenClaw 渠道插件（TS）            │
│ hermes/wechatauto/     │   │ openclaw/openclaw-wechatauto/   │
│  adapter.py (直 import)   │   │  monitor.ts → channelRuntime     │
└──────────┬───────────────┘   └──────────────┬───────────────────┘
           │                                  │ HTTP (127.0.0.1:18765)
           │                    ┌─────────────▼───────────────────┐
           │                    │ python -m wechatauto_channels        │
           └───────────────────►│  /events 长轮询 · /send · /send_file │
                               └─────────────┬───────────────────┘
                                             │ wechatauto-replica
                               ┌─────────────▼───────────────────┐
                               │ Windows 本机微信 4.x（已登录）    │
                               │ 读: SQLCipher DB · 写: UIA+OCR  │
                               └─────────────────────────────────┘
```

单一事实源：`python/wechatauto_channels/core.py`（`ChannelCore`）做归一化——
消息→`ChannelEvent`、昵称→username 解析、「是否自己发的」多证据判定。
Hermes 适配器直接 import；OpenClaw 插件通过本地 HTTP 桥接同一套逻辑。

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
cd python && python -m unittest discover -s tests -v    # 34 项，无需微信
#    含 test_bridge_e2e.py：stub wechatauto → 真 HTTP 端到端
cd openclaw/openclaw-wechatauto && npm test             # 8 项契约测试
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
