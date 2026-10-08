# bridge /media 端点：桥外 adapter 的懒解密出口

## 背景

Octop 渠道实弹验收暴露：群图消息的 event 不带 `media_path`（下载是
opt-in），adapter 只能给 agent 发 `[image]` 占位符——agent 看不到图，
拿会话上下文脑补出"铜价行情表"回答一张虾图。

## 决策

新增 `GET /media?chat=X&local_id=N` → `{ok, path}`，内部调
`ChannelCore.lazy_download_media`。消费方（Octop adapter）在
`parse_inbound` 给未下载媒体发 `wca-media://chat/local_id` 虚拟 URL，
`fetch_remote_media` 时才真正调桥解密——框架的 `should_persist_media`
决定哪些媒体值得取回，天然就是 V5 的"mention 门后懒下载"语义。

## Alternatives considered

- **桥在 push event 前预下载所有媒体**：实现最简单，但被动消息（群里
  没被@的图）也全部落盘解密——V5 显式反对过这个（decrypt 是重活，
  图片密钥瞬态还可能失败）。不做。
- **adapter 在 _dispatch_event 时同步下载**：pre-gate，同样下载被动
  消息；且 dispatch 是同步热路径，不该塞 HTTP 调用。不做。
- **复用 /context 的 media 字段**：/context 按会话拉 N 条顺带下载，
  粒度不对（adapter 要的是单条消息的按需取回）。不做。

## 边界

- 图片 AES 密钥瞬态：`{ok:false, path:null}` 是正常降级，不是 bug。
- 本地 id 校验：`local_id<=0` 或缺参 → 400。
