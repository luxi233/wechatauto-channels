# 外部拉起桥 → adapter 游标卡死丢消息

日期：2026-10-09 ・ 关联提交：`cb17f76`（channels）/ `fcb628d`（octop-gateway）

## 现象

上午 10:30 后阿巴阿巴私聊「介绍一下飞越真空泵」「在吗」均无回复。桥
`/health` 正常、`/events` 队列里两条消息都在，但 Octop 端零处理。

## 根因（机制级）

`EventBuffer.wait(cursor)` 只返回 `seq > cursor` 的事件，超时时**原样
回显客户端 cursor**。桥 10:05 猝死后由 GUI watchdog 抢先拉起（adapter
当时只数到 2 次连续失败，未到 3 次的重生阈值）——adapter 内存 cursor
=20 属于旧桥的序号空间，新桥 seq 只到 7。每次 poll 都过滤为空，回显
的 cursor=20 又让 adapter 继续保持旧值 → **永久失明**，连未来的消息
（seq 8..20）也一并丢弃。

更深一层：boot_id 校验只存在于 `start()` 和 adapter 自己 spawn 的路径
——「第三方拉起桥」这个场景设计时漏了，而它恰恰是守护体系分工下最常
见的路径。

## 修复（三层互补）

1. `EventBuffer.wait`：`cursor > self._seq` 在本进程内不可能发生 →
   归零重放整个缓冲区（消费方按事件 id 去重）。桥侧兜底，保护所有
   消费方（含不写 boot_id 检查的旧 adapter）。
2. `/events` 响应回显 `boot_id`：给消费方中途发现换实例的能力。
3. Octop adapter `_poll_loop`：每次 poll 核对 boot_id，不匹配 →
   更新 `_bridge_boot_id` + `cursor=0` + 持久化。持久化的 boot_id
   不更新的话，下次 adapter 重启会因文件里旧 boot_id 误判又把游标
   清零重放。

## 验证

- 单测：`test_stale_cursor_replays_buffer`（buffer 越界重放）、
  `test_external_bridge_respawn_detected_via_events_boot_id`（poll
  中途换 boot_id → 归零 + 落盘）。
- 生产实弹：杀桥 → watchdog 拉起（boot_id 4d70ec98→1ea04cf3）→
  octop.log 打出 `bridge restarted externally ...; resetting cursor`
  → 链路自愈，期间零人工。

## 教训

- 「检测逻辑覆盖了启动路径」不等于「覆盖了所有换实例的路径」——
   supervision 越分散（GUI/adapter/bat 三个拉起方），状态核对越要
  放在每次请求里，不能只放在生命周期钩子。
- 止血与根治要分步：先 toggle 渠道补投积压消息（消息救回来了），
  再修代码；反过来会先丢证据。
- 这是本月第三次「重启语义」事故（cursor 归零、seq 重放、外部
  拉起），同一族缺陷。以后凡是「进程可替换 + 序号会重置」的协议，
  协议本身必须带实例标识，别指望客户端记得。
