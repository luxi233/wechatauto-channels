# 文件发送假阳性：粘贴被静默吞掉后盲按回车报"已发送"

- lifecycle: implemented
- class: bug-fix
- date: 2026-10-12

## 背景

用户报"确实没收到"：两次 `send_file` 桥层均返回成功，但目标会话消息库
里一条文件消息都没落库——全链路假阳性。

现场复现出的根因链有两环：

1. **焦点没落进输入框**。`focus_input()` 的点击点在探测框上沿
   `y0 + min(24, h//4)`——刻意贴上沿是为了避开工具栏，但微信 4.x 的
   探测框与实际输入区有几像素偏移，点击落到消息区，Ctrl+V 被静默吞掉。
   对照实验：同一剪贴板内容，点探测框**中心**（`send_input_click`）后
   文件卡片草稿立刻出现。
2. **非图片分支从不验证草稿**。replica `_paste_attachment_and_send`
   对文件是"粘贴 → sleep(2) → 盲按回车 → 无条件 True"。粘贴失败 =
   对着空输入框按回车 = 报成功。图片分支有 `_input_box_has_color_draft`
   检测，文件分支什么都没有。

## Decision

在 `replica_compat.py` 新增 `file_paste_verified` 补丁（沿用源码特征
检测 + `_wac_safe` 幂等标记，见
[replica compat 层](2026-10-08-replica-compat-layer.md)），整体替换
`_paste_attachment_and_send`：

- 聚焦改为 `send_input_click` 探测框**中心**。
- 草稿判据用**发送按钮颜色**而非输入框像素：绿色 = 有可发送草稿，
  灰回 = 草稿已消费。`_wac_send_button_enabled` 采样
  `send_button_region` 内绿像素（g>140, r<100, b<140, >20 个）。
- 粘贴最多重试 3 次，等按钮变绿；超时返回 False，链路报真实 502。
- 发送按 Enter 后等按钮变灰；不变灰则 OCR 定位"发送"字样点击兜底；
  最终仍无法确认返回 False。

## Alternatives considered

- **修剪贴板格式**（补 `CFSTR_PREFERREDDROPEFFECT`）：对照实验证明
  带 DropEffect 的 PowerShell `SetFileDropList` 同样粘贴失败——不是
  剪贴板元数据问题。放弃。
- **用 `_input_box_has_text`（深色像素 >20）做发送确认**：第一版补丁
  就这么干的，实测文件真发出去了却报失败——空输入框的闪烁光标 +
  消息区溢出像素造成假阳性。发送按钮颜色是更强的状态信号，弃用。
- **改 `focus_input` 本身的点击点**：会波及所有调用方（文本粘贴也走
  它），文本链路目前工作正常。只在附件粘贴路径内替换点击行为，爆炸
  半径最小。
- **文件对话框自动化**（"发送文件"菜单 → 文件选择器）：不依赖剪贴板，
  但要弹窗交互、串行化更复杂。CF_HDROP 路线在焦点正确时已验证可用，
  不替换协议层。

## 验证

- `test_replica_compat.py` 新增 8 项：补丁应用/上游已修跳过/形态不认识
  跳过、文件不存在→False、粘贴被吞→False、粘贴+Enter 成功、OCR 兜底、
  发送未确认→False、中心点击坐标断言。全套 20 过。
- 真实链路 E2E：补丁打进 Hermes venv、桥重启后 `POST /send_file`
  `{verify:true}` 返回 `已发送并确认`，文件卡片出现在会话且 DB 回读
  命中 sort_seq 水位线。
- 中途还暴露并修了第三层缺陷：`verify_send` 默认关闭 + 桥层 verify
  也默认不传——已把 Octop 渠道 `verify_send` 置 true（`PATCH
  /agents/main/channels/{cid}`，config merge 后 `group_allow` 完好）。

## 遗留

- 按钮颜色判据钉的是当前微信 4.x 的绿/灰主题；主题改版会同时击穿
  `upstream already fixed` 检测和按钮采样——届时补丁整体走
  `skipped:unrecognized`，fail-safe 方向正确。
- 桥层 `verify` 仍是非必传参数；依赖调用方（Octop adapter 已开、
  Hermes adapter 待查）自觉开启。
