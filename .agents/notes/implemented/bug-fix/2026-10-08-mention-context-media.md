# @门槛丢弃前发媒体：mention 触发时拼接近期群聊上文

日期：2026-10-08 · 状态：已实现并部署（hermes venv + plugin 同步，gateway 已重启）

## 问题

`group_require_mention=true`（默认）下 adapter 对非 @ 群消息 `return` 整条
丢弃。典型姿势"先发一张图、再补一条 @文字"因此失效：图片本身无法携带
@，被门槛丢弃；等 @ 消息到达时 agent 只看到文字，拿不到图，无法回答。
用户反馈原话："如果这个时候我还后面再补艾特的话，他又把前面的图片丢弃了，
那就没法回复了"。

## 为什么这样修

- **保留 @门槛的语义**：非 @ 消息依旧不触发 agent（token、打扰度都不变），
  只是不再把"上文"一并烧掉。修的是上下文可见性，不是触发条件。
- **触发时回读 DB 而非入站时暂存**：备选方案是 adapter 内存里缓存非@消息
  环形队列，@时拼接。被否——重启即丢、DM/群混驻时内存口径复杂，而且
  WeChatDB 本身就是真相源，`get_messages` 一次查询即可，无需再造一份
  状态。代价是每次 @ 多一次 DB 读（几十行），可忽略。
- **上文从 adapter 注入正文**：Hermes `MessageEvent` 无独立"参考上下文"
  字段，0.21.0 也不消费 `reply_expected` 做"记但不回"。把上文拼进
  `ev.text` 并用 `[群聊上文·未@消息仅作参考]` 头显式标界 + `———` 分隔
  触发正文，是让模型不误把旧消息当新指令的最简可靠做法。
- **媒体走惰性下载而非开全量 `download_media`**：全局开关会让每个 DM/群
  的每张入站图片都解密落盘。`_lazy_media` 只在 @触发时为这几条上文建
  downloader——解密面从"所有入站媒体"收敛到"被 @ 会话的近 8 条"。
- **有界**：条数 `group_context`（默认 8，`WECHATAUTO_GROUP_CONTEXT`，0=关）
  + 时间窗 900s 硬编码上限 + `before_seq`/`before_local_id` 排除触发行
  自身。排除 `is_self`——自己上一条回复不是"用户上下文"。

## 放弃的

- 每条非 @ 消息都喂给 agent 自行判断（NO_REPLY 模式）：token 成本和
  插嘴风险都不可控，与 @门槛初衷相悖。
- V5 式全量历史库 + recall：那是另一套 runtime 的存储层，不在本渠道
  的边界内；本次取的是同一思想的最小实现（记于 DB、触及时才取）。
- `WECHATAUTO_GROUP_CONTEXT_MEDIA` 单独开关：惰性路径已足够收敛，
  多一个 env 只是配置噪音。

## 验证

- `python/tests/test_core.py` 新增 `TestRecentContext` 3 例：媒体进入上文、
  self/陈旧消息排除、条数上限与时间序；全量 37 例通过。
- 部署：`recent_context` 与 `group_context` 均已落位 hermes venv/plugin，
  gateway 07:39 重启后双渠道 connected。
- 未验：真实群"发图→补@"端到端（图片 AES 密钥是瞬态的，发图后短时间内
  @ 才能解密成功；超时只剩 `[image]` 占位行——这是微信平台侧限制）。
