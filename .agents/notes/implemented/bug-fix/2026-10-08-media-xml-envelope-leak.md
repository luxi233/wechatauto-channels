# 媒体信封 XML 泄漏为消息正文（线上事故修复）

日期：2026-10-08 · 状态：已修复并部署

## 事故

私聊里用户发一张图片问"这是什么"，bot 回答的是图片消息的
**XML 元数据**——aeskey、CDN 地址、尺寸、MD5 被逐字段解读，
还按安全口径附了"不要转发"提醒。回答本身合规，但答非所问：
agent 看到的是信封 XML，不是图片。

## 根因

`_normalize` 只在 `text.strip()` 为空时才填 `[type]` 占位。
媒体行（image/voice/file/sysmsg…）的 `content` 不是空串而是
信封 XML（`<?xml><msg><img aeskey=…>`），于是原文直接成为
`ev.text`。两个受害面：

1. DM 媒体事件本身（私聊无 @门槛，图片行就是触发事件）
2. `recent_context` 上文行——群里"发图→补@"时上文同样带 XML

附带发现：`recent_context` 还会把撤回 sysmsg 的原始 XML 当上
文行输出。

## 修法

`_normalize`：非 `text` 类型且正文以 `<?xml`/`<msg`/`<sysmsg`
开头 → 替换为 `[type]` 占位；文件/卡片类顺提 `<title>`（文件名、
链接标题是唯一用户可读字段）→ `[file] 周报.pdf`。修一处同时
覆盖事件路径与上文路径——这正是"修 bug 类不修症状"。
`recent_context` 追加 `type=="system"` 过滤替代脆弱的文本匹配。

## 验证

- 回归测试 2 例（信封→占位、sysmsg 不进上文），全量 50 过。
- 实测同一条消息：`阿巴阿巴: [image]` + 惰性解密出
  `wxid_lcgs0jz0xvdr52_490.jpg`——图片本体也到位了。

## 后续补齐（同日第二轮）

占位符上线后实测发现：触发事件自身的媒体仍没附件——`_normalize`
只在 `download_media` 全局开时才填 `media_path`，`_lazy_media`
此前只服务上文路径。补 `lazy_download_media(chat, local_id)` 并在
adapter **mention/模式门之后**调用：只对确定要派发的消息解密，
不给每条入站媒体落盘。图片密钥瞬态——刚发的图基本必解成功，
隔久的只剩占位行是平台限制。

## 顺带学到的（replica 层，未改）

- `WeChatDB` workdir 是解密快照目录，`os.replace`+stamp 协调。
  第二个写者（CLI）在 WAL 合并失败时会**投毒共享快照**
  （applied=-1 盖 stamp），gateway 下次轮询沿用陈旧快照一起变瞎。
  → CLI 必须用独立 workdir（cli.py `--workdir` 默认独立目录）。
- WAL 合并失败回落"仅主库快照"：主库 checkpoint 后数据其实完整，
  告警偏悲观；但近消息若只在 WAL 里就真缺。空结果换新 workdir
  强制重建重试一次即可。
