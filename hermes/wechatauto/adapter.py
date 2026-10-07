"""WeChat Local (wechatauto-replica) platform adapter for Hermes Agent.

在**已登录微信 4.x 的 Windows 本机**上把个人微信号接入 Hermes：
读走本地数据库解密（wechatauto-replica ``WeChatDB`` + ``Listener``），
写走 UIA+OCR 驱动真实客户端（``quick_send`` / ``quick_send_file``）。

与 Hermes 内置 ``weixin``（腾讯 iLink Bot API）的区别：
  - iLink：官方通道、跨平台、扫码即通，但只有私聊、依赖腾讯侧可用性；
  - 本适配器：本地驱动、支持**群聊**、朋友圈/历史导出可做后续扩展，
    仅限 Windows 本机、依赖微信客户端在线。

config.yaml ``gateway.platforms.wechatauto.extra`` keys:
    account / db_dir / poll_interval / download_media / media_dir
    dm_policy ("pairing" 默认 | allowlist | open | disabled)
    group_policy ("allowlist" 默认 | open | disabled)
    allow_from / group_allow_from  (wxid 或显示名列表)
    group_require_mention (默认 true：群里只响应 @机器人 的消息)
    send_verify (发送后回读数据库确认，默认 false —— verify 会拖慢发送)
Env 覆盖：WECHATAUTO_*（如 WECHATAUTO_HOME_CHANNEL 给 cron 投递）。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

try:
    from gateway.platforms._shared import (
        extra_or_secret,
        seed_extra_from_env as _seed_extra_from_env,
        apply_yaml_bridge,
        send_error,
    )
except ImportError:  # 旧版 hermes（如 0.21.0）_shared 无这些 helper → 用插件内 vendor
    from ._compat_shared import (
        extra_or_secret,
        seed_extra_from_env as _seed_extra_from_env,
        apply_yaml_bridge,
        send_error,
    )
try:
    from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
except ImportError:  # 旧版 hermes 无此 mixin → 用插件内 vendor
    from ._compat_policy import OwnAccessPolicyMixin
try:
    from gateway.platforms.event import MessageEvent, MessageType
except ImportError:  # 旧版 hermes（如 0.21.0）事件类在 platforms.base
    from gateway.platforms.base import MessageEvent, MessageType

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _preserve_root_logging():
    """wechatauto 的 wxlog 在**首次 import** 时会清空 root handlers、
    把日志全转去 stderr——在 gateway 进程内会把 Hermes 的 QueueHandler
    管线连根拔掉（三个 log 文件从此静默）。快照并在导入后还原。
    Note: 与中文 status 误判、插件兼容层的完整 RCA —
    见 .agents/notes/implemented/bug-fix/2026-10-07-hermes-v0.21-compat.md"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

PLATFORM = "wechatauto"
ENV_PREFIX = "WECHATAUTO"
TRUTHY = {"1", "true", "yes", "on"}

# 微信是纯文本通道：markdown 原样发出去全是噪音，至少剥掉最常见记号
_MD_RULES = (
    (r"```\w*\n?", ""),
    (r"`([^`\n]+)`", r"\1"),
    (r"!\[([^\]]*)\]\(([^)]+)\)", r"\2"),
    (r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)"),
    (r"\*\*(.+?)\*\*", r"\1"),
    (r"__(.+?)__", r"\1"),
    (r"(?<!\w)\*(?!\*)(.+?)(?<!\*)\*(?!\w)", r"\1"),
    (r"^#{1,6}\s*", ""),
)
# 群消息里 @某人 的文本形态：@昵称 + U+2005/U+2006 或空白
_MENTION_TAIL_RE = "[   \t]*"

# yaml key -> (ENV, kind) —— apply_yaml_bridge 的表驱动声明
_YAML_SPEC = (
    ("account", f"{ENV_PREFIX}_ACCOUNT", "str"),
    ("db_dir", f"{ENV_PREFIX}_DB_DIR", "str"),
    ("poll_interval", f"{ENV_PREFIX}_POLL_INTERVAL", "str"),
    ("download_media", f"{ENV_PREFIX}_DOWNLOAD_MEDIA", "lower"),
    ("media_dir", f"{ENV_PREFIX}_MEDIA_DIR", "str"),
    ("send_verify", f"{ENV_PREFIX}_SEND_VERIFY", "lower"),
    ("group_require_mention", f"{ENV_PREFIX}_GROUP_REQUIRE_MENTION", "lower"),
    ("dm_policy", f"{ENV_PREFIX}_DM_POLICY", "lower"),
    ("group_policy", f"{ENV_PREFIX}_GROUP_POLICY", "lower"),
    ("allow_from", f"{ENV_PREFIX}_ALLOWED_USERS", "csv"),
    ("group_allow_from", f"{ENV_PREFIX}_GROUP_ALLOW_FROM", "csv"),
)


def _env(extra: dict, key: str, default: Any = "") -> Any:
    """``WECHATAUTO_<KEY>`` 覆盖 config.yaml ``extra.<key>``（profile 作用域读取）。"""
    return extra_or_secret(extra, key, f"{ENV_PREFIX}_{key.upper()}", default)


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in TRUTHY


def _csv(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _strip_markdown(text: str) -> str:
    for pat, repl in _MD_RULES:
        text = re.sub(pat, repl, text, flags=re.M if pat.startswith("^") else 0)
    return text


def _msg_type(mt: str) -> MessageType:
    return {
        "image": MessageType.PHOTO,
        "voice": MessageType.VOICE,
        "video": MessageType.VIDEO,
        "file": MessageType.DOCUMENT,
        "emoji": MessageType.STICKER,
        "location": MessageType.LOCATION,
    }.get(mt, MessageType.TEXT)


def _media_mime(mt: str) -> str:
    return {"image": "image/*", "voice": "audio/*",
            "video": "video/*", "file": "application/octet-stream"}.get(mt, "")


class WeChatLocalAdapter(OwnAccessPolicyMixin, BasePlatformAdapter):
    """驱动本机微信客户端的 Hermes 平台适配器。"""

    ALLOW_ALL_ENV_PREFIX = ENV_PREFIX  # OwnAccessPolicyMixin: <PREFIX>_ALLOW_ALL_USERS

    supports_code_blocks = False
    typed_command_prefix = "/"
    splits_long_messages = True  # send() 内部按 split_text 分块
    interactive_resume = True
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = getattr(config, "extra", {}) or {}
        self.account = str(_env(extra, "account", "") or "") or None
        self.db_dir = str(_env(extra, "db_dir", "") or "") or None
        self.poll_interval = float(_env(extra, "poll_interval", 1.0) or 1.0)
        self.download_media = _truthy(_env(extra, "download_media"), False)
        self.media_dir = str(_env(extra, "media_dir", "") or "") or None
        self.send_verify = _truthy(_env(extra, "send_verify"), False)
        self.group_require_mention = _truthy(
            _env(extra, "group_require_mention"), True)

        # OwnAccessPolicyMixin 契约字段
        self._dm_policy = str(_env(extra, "dm_policy", "pairing") or "pairing")
        self._group_policy = str(
            _env(extra, "group_policy", "allowlist") or "allowlist")
        self._allow_from = _csv(_env(extra, "allowed_users",
                                     extra.get("allow_from", [])))
        self._group_allow_from = _csv(_env(extra, "group_allow_from",
                                           extra.get("group_allow_from", [])))

        self._core = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seen: set = set()
        self._seen_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "WeChat Local"

    # 白名单不只认 wxid：显示名/备注同样算数（群白名单同规则）
    def _entry_matches(self, entries: Iterable[str], target: str) -> bool:
        target = str(target or "")
        if target in entries:
            return True
        if self._core:
            try:
                name = self._core._display_name(target)
                if name and name in entries:
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _make_core(self):
        with _preserve_root_logging():
            from wechatauto_channels.core import ChannelCore  # 懒导入：含 Windows-only 依赖

        return ChannelCore(
            account=self.account, db_dir=self.db_dir,
            poll_interval=self.poll_interval,
            download_media=self.download_media, media_dir=self.media_dir,
            on_event=self._on_core_event)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._loop = asyncio.get_running_loop()
        if not self._acquire_platform_lock(
                PLATFORM, self.account or "default",
                "本机微信账号（同一账号只能被一个 gateway 驱动）"):
            return False
        try:
            self._core = await asyncio.to_thread(self._make_core)
            info = await asyncio.to_thread(self._core.start)
        except Exception as e:  # DB 初始化失败：多半是微信未登录/非 Windows
            logger.error("wechatauto: core start failed — %s", e)
            self._release_platform_lock()
            return self._fail("core_start_failed",
                              f"{e}（需要 Windows + 已登录的微信 4.x）", retryable=True)
        self._mark_connected()
        self._wire_plugin_handlers(None)
        logger.info("wechatauto: connected as %s (%s), media=%s",
                    info.get("nickname"), info.get("wxid"), info.get("media"))
        return True

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        if self._core:
            await asyncio.to_thread(self._core.stop)
        self._core = None
        self._mark_disconnected()

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if not self._core:
            return SendResult(success=False, error="not connected", retryable=True)
        plain = _strip_markdown(content or "").strip()
        if not plain:
            return SendResult(success=False, error="empty message")
        from wechatauto_channels.core import split_text
        last_id = None
        for chunk in split_text(plain, self.MAX_MESSAGE_LENGTH):
            try:
                r = await asyncio.to_thread(
                    self._core.send_text, chat_id, chunk, self.send_verify)
            except Exception as e:  # GUI 路径不可预期，统一收口
                logger.error("wechatauto: send raised", exc_info=True)
                return SendResult(success=False, error=str(e), retryable=True)
            if not r.get("ok"):
                # 确定性失败（目标解析不了/窗口找不到）——重试没用
                return SendResult(success=False,
                                  error=r.get("message") or "send failed")
            last_id = f"{r.get('to') or chat_id}:sent"
        return SendResult(success=True, message_id=last_id)

    async def _send_local_file(self, chat_id: str, path_or_url: str,
                               caption: Optional[str], reply_to,
                               metadata, *, as_image: bool) -> SendResult:
        """统一媒体发送：本地路径直发；http(s) 先下载到临时目录再发。"""
        if not self._core:
            return SendResult(success=False, error="not connected", retryable=True)
        path = path_or_url
        if path.startswith(("http://", "https://")):
            try:
                path = await self._download_to_tmp(path)
            except Exception as e:
                return SendResult(success=False,
                                  error=f"媒体下载失败: {e}", retryable=True)
        ext = (path.rsplit(".", 1)[-1] or "").lower()
        image = as_image or ext in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
        try:
            r = await asyncio.to_thread(
                self._core.send_file, chat_id, path, image, self.send_verify)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        if r.get("ok") and caption:
            await self.send(chat_id, caption, reply_to=reply_to, metadata=metadata)
        return SendResult(success=bool(r.get("ok")),
                          error=None if r.get("ok") else r.get("message"))

    @staticmethod
    async def _download_to_tmp(url: str) -> str:
        import tempfile
        import urllib.request

        def _dl() -> str:
            suffix = "." + url.split("?")[0].rsplit(".", 1)[-1] if "." in url.split("?")[0] else ".bin"
            fd, tmp = tempfile.mkstemp(prefix="wechat_media_", suffix=suffix[:8])
            with os.fdopen(fd, "wb") as f:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    f.write(resp.read())
            return tmp

        return await asyncio.to_thread(_dl)

    async def send_image(self, chat_id: str, image_url: str,
                         caption: Optional[str] = None, reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send_local_file(chat_id, image_url, caption,
                                           reply_to, metadata, as_image=True)

    async def send_image_file(self, chat_id: str, image_path: str,
                              caption: Optional[str] = None,
                              reply_to: Optional[str] = None,
                              metadata: Optional[Dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._send_local_file(chat_id, image_path, caption,
                                           reply_to, metadata, as_image=True)

    async def send_document(self, chat_id: str, file_path: str,
                            caption: Optional[str] = None,
                            file_name: Optional[str] = None,
                            reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None,
                            **kwargs) -> SendResult:
        return await self._send_local_file(chat_id, file_path, caption,
                                           reply_to, metadata, as_image=False)

    async def send_video(self, chat_id: str, video_path: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_local_file(chat_id, video_path, caption,
                                           reply_to, metadata, as_image=False)

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        # 微信没有独立语音消息发送通道，按文件发
        return await self._send_local_file(chat_id, audio_path, caption,
                                           reply_to, metadata, as_image=False)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """微信没有对外 typing API —— no-op。"""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if self._core:
            info = self._core.chat_info(chat_id)
            return {"name": info.get("name") or chat_id,
                    "type": info.get("type", "dm")}
        return {"name": chat_id,
                "type": "group" if chat_id.endswith("@chatroom") else "dm"}

    # ------------------------------------------------------------------
    # 入站：Listener 工作线程 → asyncio loop
    # ------------------------------------------------------------------
    def _on_core_event(self, ev) -> None:
        """ChannelCore 回调（在 listener 的每会话工作线程里跑，禁止阻塞）。"""
        if self._loop is None or self._loop.is_closed():
            return
        with self._seen_lock:
            if ev.id in self._seen:
                return
            self._seen.add(ev.id)
            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
        asyncio.run_coroutine_threadsafe(self._dispatch(ev), self._loop)

    async def _dispatch(self, ev) -> None:
        if ev.is_self:
            return  # 本机发的消息不回环给 agent
        if ev.chat_type == "dm":
            if not self._is_dm_intake_allowed(ev.sender_id):
                logger.debug("wechatauto: DM from %s denied by policy=%s",
                             ev.sender_id, self._dm_policy)
                return
        elif not self._is_group_allowed(ev.chat_id, ev.sender_id):
            logger.debug("wechatauto: group %s denied by policy=%s",
                         ev.chat_id, self._group_policy)
            return
        text = ev.text
        mentioned = False
        if ev.chat_type == "group" and self.group_require_mention:
            nick = (self._core.self_nick if self._core else "") or ""
            wxid = (self._core.self_wxid if self._core else "") or ""
            # at_usernames（list）是权威证据：空=确定没@；None=不可判定走文本兜底
            quoted_self = bool(
                wxid and getattr(ev, "quoted", None)
                and ev.quoted.get("sender") == wxid)  # 引用我视同@
            if isinstance(getattr(ev, "at_usernames", None), list):
                mentioned = quoted_self or (bool(wxid)
                                            and wxid in ev.at_usernames)
            else:
                mentioned = quoted_self or bool(nick and f"@{nick}" in text)
            if not mentioned:
                return  # 群里没点名的消息不进队列（旁听/摘要是后续扩展）
            # 命中后把 @片段从正文剥掉（atuserlist 命中时正文也可能带残留）
            if nick and f"@{nick}" in text:
                text = re.sub(rf"@{re.escape(nick)}{_MENTION_TAIL_RE}",
                              "", text).strip()
        if not text.strip():
            return
        source = self.build_source(
            chat_id=ev.chat_id, chat_name=ev.chat_name,
            chat_type="group" if ev.chat_type == "group" else "dm",
            user_id=ev.sender_id, user_name=ev.sender_name)
        event = MessageEvent(
            text=text, message_type=_msg_type(ev.type), source=source,
            message_id=ev.id, raw_message=ev.to_dict(),
            timestamp=(datetime.datetime.fromtimestamp(ev.timestamp)
                       if ev.timestamp else datetime.datetime.now()))
        # reply_expected 是新版字段（旧版 MessageEvent 无此 kwarg）：setattr 兼容两版
        event.reply_expected = True if ev.chat_type == "dm" else mentioned or None
        if ev.media_path:
            event.media_urls = [ev.media_path]
            event.media_types = [_media_mime(ev.type)]
        await self.handle_message(event)


# ── 插件注册（Hermes 插件系统契约）────────────────────────────────────────

def check_requirements() -> bool:
    """被动探测：wechatauto_channels/wechatauto 可 import（不安装、不联网）。"""
    import importlib.util
    return (importlib.util.find_spec("wechatauto_channels") is not None
            and importlib.util.find_spec("wechatauto") is not None)


def validate_config(config) -> bool:
    """本渠道无必填配置：账号自动探测，登录态就是微信客户端本身。"""
    return check_requirements()


def is_connected(config) -> bool:
    return validate_config(config)


def interactive_setup() -> None:
    """``hermes gateway setup`` 交互向导。"""
    from hermes_cli.setup import (
        get_env_value, print_header, print_info, print_success, print_warning,
        prompt, prompt_yes_no, save_env_value)

    print_header("WeChat Local (wechatauto-replica)")
    print_info("本渠道驱动当前 Windows 机器上已登录的微信 4.x 客户端。",
               "前置条件：pip install wechatauto-channels（自动装 wechatauto-replica），",
               "微信 PC 客户端已登录并保持后台运行，64 位 Python。")
    if not check_requirements():
        print_warning("未检测到 wechatauto_channels / wechatauto —— 请先 "
                      "pip install wechatauto-channels 再回来配置。")
        return
    print()
    if prompt_yes_no("群聊里只允许 @机器人 的消息触发回复（推荐）？", True):
        save_env_value(f"{ENV_PREFIX}_GROUP_REQUIRE_MENTION", "true")
    else:
        save_env_value(f"{ENV_PREFIX}_GROUP_REQUIRE_MENTION", "false")
    if prompt_yes_no("启用媒体下载（图片/语音/文件解密到本地）？", False):
        save_env_value(f"{ENV_PREFIX}_DOWNLOAD_MEDIA", "true")
    home = prompt("cron/通知默认投递到哪个聊天（昵称或群名，留空跳过）",
                  default=get_env_value(f"{ENV_PREFIX}_HOME_CHANNEL") or "")
    if home:
        save_env_value(f"{ENV_PREFIX}_HOME_CHANNEL", home.strip())
    allowed = prompt("私聊白名单（wxid/昵称，逗号分隔；留空=走配对流程）",
                     default=get_env_value(f"{ENV_PREFIX}_ALLOWED_USERS") or "")
    if allowed:
        save_env_value(f"{ENV_PREFIX}_ALLOWED_USERS", allowed.replace(" ", ""))
    print_success("配置已写入 ~/.hermes/.env —— 重启 gateway 生效")


def _env_enablement() -> dict:
    """env-only 配置也要进 gateway status / connected_platforms。"""
    spec = tuple((env, key, None) for yaml_key, env, _kind in _YAML_SPEC
                 for key in [yaml_key])
    return _seed_extra_from_env(spec, home_env=f"{ENV_PREFIX}_HOME_CHANNEL")


def _apply_yaml(yaml_cfg: dict, platform_cfg) -> Optional[dict]:
    return apply_yaml_bridge(yaml_cfg, _YAML_SPEC)


def _parse_target_ref(ref: str):
    """微信侧目标可以是 wxid/*@chatroom 或显示名 —— 一律收下，真正解析在发送侧。"""
    ref = (ref or "").strip()
    return (ref, None) if ref else None


async def _standalone_send(pconfig, chat_id: str, message: str, *,
                           thread_id=None, media_files=None,
                           force_document=False) -> dict:
    """cron 独立进程投递：无 gateway 时拉起最小 core 只为解析+发送。"""
    if not check_requirements():
        return send_error("wechatauto_channels / wechatauto not installed")
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        with _preserve_root_logging():
            from wechatauto.db import WeChatDB
            from wechatauto_channels.core import ChannelCore, split_text

        core = ChannelCore(account=str(_env(extra, "account", "") or "") or None,
                           db_dir=str(_env(extra, "db_dir", "") or "") or None)
        # 只起 DB 不启监听：resolve_target 需要 DB，发送本身走 GUI
        core.db = WeChatDB(db_dir=core._db_dir, account=core._account)
        info = core.db.get_self_info() or {}
        core.self_wxid = (core.db.wxid or info.get("username") or "").strip()
        core.self_nick = (info.get("nick_name") or "").strip()
        last_ok, err, last_id = True, None, None
        for chunk in split_text(_strip_markdown(message), 4000):
            r = await asyncio.to_thread(core.send_text, chat_id, chunk)
            if not r.get("ok"):
                last_ok, err = False, r.get("message")
                break
            last_id = f"{r.get('to')}:sent"
        if last_ok:
            return {"success": True, "message_id": last_id}
        return send_error(f"wechatauto standalone send failed: {err}")
    except Exception as e:
        logger.debug("wechatauto standalone send raised", exc_info=True)
        return send_error(f"wechatauto standalone send failed: {e}")


def register(ctx):
    """Hermes 插件入口。"""
    ctx.register_platform(
        name=PLATFORM,
        label="WeChat Local",
        adapter_factory=WeChatLocalAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="pip install wechatauto-channels（Windows + 微信 4.x 已登录）",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml,
        cron_deliver_env_var=f"{ENV_PREFIX}_HOME_CHANNEL",
        parse_target_ref_fn=_parse_target_ref,
        standalone_sender_fn=_standalone_send,
        allowed_users_env=f"{ENV_PREFIX}_ALLOWED_USERS",
        allow_all_env=f"{ENV_PREFIX}_ALLOW_ALL_USERS",
        max_message_length=4000,
        emoji="💬",
        pii_safe=False,  # 微信号=真人身份，wxid 本身就是 PII
        allow_update_command=True,
        platform_hint=(
            "You are chatting via WeChat (personal account, local Windows client). "
            "Plain text only — no markdown, no code blocks, no reactions. "
            "In group chats you see every message only when mentioned; the sender "
            "is identified in context. Keep replies concise and conversational."))
