# Octop 群白名单：GUI 群名勾选 ↔ adapter group_allow 过滤

日期：2026-10-09

## 为什么

Octop 链路此前没有群准入：任何群 @ 李乐儿都会唤醒 agent（Hermes 侧的
`WECHATAUTO_GROUP_ALLOW_FROM` 白名单设计没有对应物）。用户要求白名单制，
且明确约束：**人看不到群号只能看到群名**——所以名单的发现和勾选必须以
群名呈现，底层存 `*@chatroom` id。

## 设计

- **群来源**：桥 `GET /chats`（replica `get_sessions` 会话表）——发起过
  消息、被微信记录的群天然在册，不需要额外"见过"登记簿。
- **过滤点**：`WechatAutoChannel._dispatch_event` 最早处（is_self 之后、
  去重之前）。非名单群直接丢弃——不进队列、不进被动缓冲，等于该群对
  agent 完全不可见。空列表 = 关闭过滤（向后兼容）。
- **生效方式**：写 `config_json.group_allow` + PATCH enabled off/on
  热重载，不用重启后端。
- **stale 行**：白名单里的 id 若不在会话表（群已退/无记录），GUI 仍展示
  并标记，可取消勾选摘除——否则名单里的死 id 永远清理不掉。

## 放弃了什么

- **桥端过滤**：bridge 是有意的"哑管道"（持 token 即授权），策略留在
  消费方 adapter——OpenClaw/Hermes 走桥时各有各的白名单语义。
- **deny 名单**：只要 allow 就够当前需求；空 allow + 全勾等价全开，
  deny 是 YAGNI。
- **GUI 里键盘输入群名**：名字解析有同名歧义风险（两个同名的群），
  强制从会话表发现可以避免用户输入不可校验的名字。

## 落点

- octop-gateway `channels/wechatauto/channel.py`：`group_allow` 配置 +
  `_dispatch_event` 过滤（commit 78f2ba2，fork main 远端 9ad43031）
- wechatauto-channels `manager_ops.py`：`octop_group_allowlist{,_set}`；
  `manager.py`：Octop 页"群白名单"勾选区（commit 1155681，远端 5e0ad8a3）
- 测试：adapter 侧 3 项 + ops 侧 6 项，全绿

## 验收

- `octop_group_allowlist` 实测返回 3 个真实群（有点东西/永远的大家庭/
  AI员工养成计划），name↔id 映射正确
- set 后 octop.db `config_json.group_allow` 落库，渠道 toggle 日志显示
  `resuming cursor=19 (boot_id match)`——持久游标同时验证
