# Agent Note: 一键接入引导器与宿主接入按钮

Status: implemented

## Problem

用户画像分两台机器：生活机用 Hermes、工作机用 Octop，问"零基础小白能不能
一键安装"。盘点真实链路后发现：

- Hermes 侧 ~70% 已按钮化（注入/守护都在 GUI），缺"到达管理台之前"的全部步骤
- Octop 侧只有 ~50%：前端补丁是历史手工打的（不在注入流程）、渠道行要在
  dashboard 手工建、纯 Octop 机器没有 Hermes venv 可借
- OpenClaw 侧要走 `openclaw` CLI 的 plugins install + config set

之前整套部署是跨多次会话手工攒的，知识不沉淀成脚本，换台机器就重新踩坑。

## Decision

- **install.bat（仓库根目录）**：按机器角色分支的引导器——探测 Python
  的顺序是 Hermes venv → Octop portable runtime → `~/.wechatauto/venv`
  → py launcher/PATH。Hermes 机器把包装进 Hermes 的 venv；纯 Octop 机器
  自建 `~/.wechatauto/venv`，不碰系统 Python。然后写 `repo_root` 到
  `~/.wechatauto/manager.json`、注册自启、创建「WeChatAuto 管理台」桌面
  快捷方式并拉起管理台。快捷方式使用 `--respawn`，重复双击不会多开实例；
  中文文件名由 PowerShell Unicode 码点生成，不依赖 `cmd.exe` 当前代码页。
- **管理台三个按钮**，ops 层三个 `*_onboard` 函数，全部幂等：
  - `hermes_onboard`：拷插件目录 + `plugins.enabled` 白名单 +
    `platforms.wechatauto.enabled=true` + 重启 gateway（复用既有 inject）
  - `octop_onboard`：注入 adapter + `octop_patch_frontend`（dashboard
    前端锚点补丁，渠道卡片可见）+ `octop_create_channel_row`（直插
    `octop.db`，含 token/bridge_cmd）+ 重启验证连接
  - `openclaw_onboard`：`plugins install` 本地目录 + `config set`
    bridgeUrl/bridgeToken/enabled
- **渠道行直插 sqlite 而非 API**：Octop 的 `ChannelKind` 枚举不认识
  `wechatauto`，API 建行会 422。直插 `channels` 表是唯一通路；已有行
  不覆盖（保 `group_allow` 等用户配置），缺字段才补。
- **前端补丁按锚点插入**：`_FE_MEMBERS`/`_FE_FIELDS_BLOCK` 定位打包 JS
  里的渠道枚举锚点，幂等（已有 `wechatauto` 字样即跳过）；锚点找不到
  就如实报 `anchors missing`，不静默。
- **同步脚本 `scripts/sync_octop_adapter.py`**：octop-gateway 仓的
  `channels/wechatauto` 是金本，一键同步到本仓 `manager/adapter-src`，
  注入和 watchdog 共用同一份。

## Alternatives considered

- **installer.exe / 打包成单文件**：bat + 系统/便携 Python 足够，
  PyInstaller 打包引入杀毒误报和体积，不值。
- **OpenClaw 全自动**：openclaw CLI 本身是外部前置，没装就跳过并提示；
  npm 包没发布到 registry，只能 `plugins install` 本地目录——UI 文案
  已写明，不误导用户去 npm 搜。
- **API 建行**：枚举不认（见上），等上游把 `wechatauto` 收进 ChannelKind
  后可改回 API 通路，直插代码保留作降级。
- **安装器内嵌 winsdk**：winsdk 是可选依赖（send-ocr extra），强制装
  会在 Python 3.13+ 上撞构建失败；install.bat 走 extras，失败降级提示
  而不是阻塞主链路。

## Implementation

- `install.bat`（新，含登录自启与桌面快捷方式）、`scripts/sync_octop_adapter.py`（新）
- `python/wechatauto_channels/manager_ops.py`：`hermes_onboard` /
  `octop_onboard` / `openclaw_onboard` / `octop_patch_frontend` /
  `octop_create_channel_row` / `_fe_ensure` / `_FE_*` 常量
- `python/wechatauto_channels/manager.py`：三个页签各加一个「一键接入」按钮
- `python/tests/test_manager_ops.py`：前端补丁锚点/幂等、建行与
  `group_allow` 保留、离线 onboard 和状态三态语义均有回归覆盖
- 版本 0.1.7 → 0.1.8

## Consequences

- 收益：安装入口、桌面快捷方式、登录自启和三种宿主接入集中在同一管理台，
  补丁层继续随各自分发单元自动应用。
- 代价：`install.bat` 仍依赖 Windows PowerShell 与可用的 Python 3.9–3.12；
  OpenClaw 接入仍要求机器预先安装 `openclaw` CLI。

## Verification

- 全非 e2e 115 项绿、Ruff 净，Python 3.12 `compileall` 通过
- wheel/sdist 构建成功，产物元数据为 `0.1.9` 且包含管理台状态修复
- Unicode 快捷方式 smoke test 生成 `WeChatAuto 管理台.lnk`，参数包含
  `-m wechatauto_channels.manager --respawn`
- 已部署到运行中的 Hermes venv（`import wechatauto_channels.manager_ops`
  验证三个 onboard 函数存在），管理台进程已用 WMI 重拉，新代码生效
- ⚠️ 真机回归点：工作机（纯 Octop、无 Hermes）双击 install.bat 走
  `~/.wechatauto/venv` 分支——测试只覆盖函数级，venv 自建路径要真机验
