# 每群触发模式表：at / reply / listen（V5 chat-policy 最小移植）

日期：2026-10-08 · 状态：已实现并部署（hermes venv + plugin 同步，gateway 已重启）

## 问题与动因

adapter 的群聊触发此前只有两个全局开关：`GROUP_ALLOW_FROM`（准入）+
`GROUP_REQUIRE_MENTION`（是否 @才回）。用户调研 V5 后要求借鉴其
per-chat 模式表——白名单内的群不该只有一种行为：测试群想全量响应、
旁听群想只录不答。

## 语义

`WECHATAUTO_CHAT_POLICY` 指向 JSON（mtime 热加载）：

```json
{"default": "at",
 "rules": [{"chat": "测试群", "mode": "reply"},
           {"chat": "家庭群", "mode": "listen"}]}
```

- `at`：@/引用我才触发（沿用 require_mention 语义 + 上文注入）
- `reply`：该群每条消息都触发——等价于单群关闭 require_mention
- `listen`：永不触发。历史本就在 WeChatDB 快照里，新增两个只读出口：
  bridge `GET /context?chat=<名/id>&n=` 与
  `python -m wechatauto_channels recent <chat>`（agent 经 Hermes 终端
  工具可直跑，无需 bridge token）。

`chat` 匹配 chat_id 或显示名，首条命中生效；无文件时 mode 由
`group_require_mention` 全局推出，行为与旧版逐字节一致。

## 为什么这样修

- **listen 不另建存储**：V5 的 listen 靠 v5-history.db 落库；我们天然有
  WeChatDB 快照（`mode=ro` 多读者安全）。缺的从来不是"录"，是"出口"——
  所以本次的成本大头是两个查询口而非策略本身。
- **fail-compatible 而非 V5 的 fail-closed-to-listen**：V5 配置坏了静默
  是安全选择（防插错话）；我们这份文件是可选增强，坏了回到全局开关
  语义更符合"未配置=旧版"的心智，坏文件也沿用上一份好的配置。
- **reply_expected 修正**：reply 模式下 `mentioned` 恒 False，直接沿用
  旧式会把全量触发标成"不期待回复"——按 mode 重新计算。
- **不实现 `smart`**（语义相关度判定）：要额外跑一个判定模型，成本与
  误判都不值；`at`+上文注入已覆盖主诉场景。
- **不实现 sender 级规则**：V5 的 `{"chat","sender","mode"}`（老板秒回）
  有真实价值但本轮范围控制在会话级；schema 留有扩展空间。

## Alternatives considered

- **env CSV `群名:mode` 列表**：群名可能含逗号/冒号，且改配置要重启。
  JSON 文件 + mtime 热加载与 V5 同构，运维姿势已被证明。
- **不实现，让用户继续只用全局开关**：可行但每加一个"例外群"都要
  全局妥协（要么全开回复要么全关），这正是 V5 policy 表存在的理由。
- **listen 消息也派发进 Hermes 但标不回复**：0.21.0 的 MessageEvent
  不消费 reply_expected，派了就会真回——否决。

## 验证

- `test_chat_policy.py` 9 例：id/名匹配、首条生效、文件 default 覆盖
  env default、非法规则跳过、热加载、坏文件沿用/首坏回落。
- `test_bridge_e2e.py` +2：`/context` 正常返回上文行、缺参 400。
- 全量 48 例通过。
