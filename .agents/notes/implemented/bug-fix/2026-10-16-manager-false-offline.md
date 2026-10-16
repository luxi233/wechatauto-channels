# Agent Note: 管理台健康探针超时不再误报离线

Status: implemented

## Problem

Bridge 和 Octop 的进程、端口与 API 都正常时，长驻管理台仍可能把两者显示为
“离线”。管理台此前把 `/health` 的单次超时直接等同于进程死亡，而且五个平台
探针在同一线程中无隔离：任一探针抛异常，整轮状态都不会进入 UI 队列。

## Decision

管理台状态采集统一经过 `manager_status`。Bridge 使用三态语义：`/health`
成功为“正常”，HTTP 探针失败但配置中的 TCP 端口可连接为“忙碌（端口在线）”，
只有 HTTP 与 TCP 都失败才显示“离线”。其余平台探针逐项隔离异常，让已经取得的
状态继续刷新，不因单项失败冻结整张状态卡。

Octop 同样区分“渠道已连接”“后端在线但渠道状态待确认”“后端不可达”。渠道列表
API 短暂失败时不再把 `connected=None` 直接解释为断线。

watchdog 仍以 `/health` 为强健康信号，并在 TCP 存活时禁止重复拉起桥；本改动没有
放宽消息收发验收，也不改变 Bridge 或 Octop 的协议。

## Alternatives considered

- 延长 `/health` 超时：只能降低误报频率，无法区分“忙碌”和“死亡”，也不能解决
  单探针异常冻结整轮刷新。
- 看到红字就重启 Bridge/Octop：会打断正常业务，并可能触发桥单实例锁冲突。
- 仅重启管理台：能暂时恢复显示，但不修复下一次探针抖动后的相同误报。

## Consequences

- 收益：状态卡与 watchdog 使用一致的存活语义，忙碌服务不再被标成离线；单个平台
  探针失败时其余状态仍可见。
- 代价：TCP 存活不能证明业务完全健康，因此明确显示为橙色“忙碌”，不冒充绿色
  “正常”；真实在线仍必须以 `/health` 和 Octop runtime API 为准。
- 验证：单测覆盖 HTTP 超时但 TCP 存活、单探针异常不影响 Octop 状态两条路径；
  真机管理台重启后 Bridge 与 Octop 进程 PID 保持不变，实时探针持续返回在线。

## Verification

- 全非 e2e 测试 `115 passed`，Ruff 全部通过。
- Python 3.12 `compileall` 通过，`0.1.9` wheel/sdist 均包含
  `manager.py` 与三态状态采集实现。
- Bridge `/health` 返回在线账号“李乐儿”，Octop 首页返回 HTTP 200。
