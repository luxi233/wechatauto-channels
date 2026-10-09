"""wechatauto-channels 共享核心 —— 把 wechatauto-replica 规范化成通用渠道原语。

Hermes 适配器直接 import；OpenClaw 渠道插件经 bridge.py 的 HTTP sidecar
走同一套逻辑。本模块对 wechatauto 全部做**懒导入**，以便在非 Windows
环境下也能 import 本包做单测（真正 start/send 才需要 Windows + 已登录微信）。

消息身份判定（踩坑记录，来自 wechatbot-new v2.2.8）：
``real_sender_id`` 是**分片内**短编号，写死 ``== 2`` 会把换分片前的自己
算成对方。判定「是不是我发的」只用正向证据：
  ① 消息行 ``status == 2``（微信给自己消息的标记，跨分片稳定）；
  ② 表情/图片 XML 里 ``fromusername`` == 本机 wxid；
  ③ ``sender_username``（经 SenderName2Id 反查）== 本机 wxid。
整个会话都拿不到正向证据时才退回 ``sender_id in {1,2}``
（stardome 生产实测：4.1.13.x 出站行也用过 ``1``）。

@判定（踩坑记录，来自 stardome v5）：正文 ``@昵称`` 可被粘贴伪造，唯一
可靠证据是消息 ``source`` 里的 ``<atuserlist>``；``<refermsg>`` 引用我
视同 @。上游 ``_msg_row_to_dict`` 不投影这两个字段，本模块在 ``start()``
时给 WeChatDB 打幂等投影补丁（上游原生支持则自动跳过）。
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional

# wechatauto-replica >=1.2.3 内置拟人节奏层（rhythm.py），默认 natural 档：
# 两次写动作至少隔 2.5–6s、120s 内第 7 次写吃 30–75s 冷却。agent 分段回复
# 一条就是 3–5 次写动作，会被节流阀卡死体感（stardome 实测）。默认 off 精确
# 还原加层之前的行为；要反风控节奏档时外部 env 覆盖即可，不用改代码。
# 必须在 wechatauto 首次读取 env 之前设置——本模块对 wechatauto 懒导入，安全。
os.environ.setdefault("WECHATAUTO_RHYTHM", "off")

logger = logging.getLogger(__name__)

_MSG_TABLE_RE = re.compile(r"^Msg_[0-9a-f]{32}$")
_XML_SELF_RE = re.compile(r'fromusername\s*=\s*["\']?\s*([A-Za-z0-9_\-@]+)')
# 群聊 DB 行 content 前缀 "wxid_xxx:\n正文" 里的 wxid 是真实发言人权威源
# （sender_id 对本机只是数字索引，stardome 生产实测落过 "3"）。
_GROUP_SENDER_PREFIX_RE = re.compile(r"^(wxid_[0-9A-Za-z_]+):\s*", re.S)
_ATLIST_RE = re.compile(r"<atuserlist[^>]*>(.*?)</atuserlist>", re.S | re.I)
_REFERMSG_RE = re.compile(r"<refermsg>.*?</refermsg>", re.S | re.I)
_XML_TAG_RE = re.compile(r"<%s>(.*?)</%s>", re.S | re.I)
# 微信文本框实际上限远高于此；留安全边际并给 UIA/OCR 路径减负
DEFAULT_MAX_TEXT = 4000

# 上游 MSG_TYPE_NAMES 给的是中文显示名（"文本"/"图片"/"文件/链接/卡片"…），
# 渠道事件统一成英文 canon tag，两个宿主的下游 mapping 才有意义。
# （审计发现：直接拿上游中文名去比 "image" 永假，媒体下载曾是死代码。）
_TYPE_CANON = {
    "文本": "text", "图片": "image", "语音": "voice", "视频": "video",
    "动画表情": "emoji", "位置": "location", "文件/链接/卡片": "file",
    "音视频通话": "call", "系统消息": "system", "红包": "hongbao",
    "引用": "quote",
}


def _canon_type(raw: Any) -> str:
    t = str(raw or "text").strip()
    if not t:
        return "text"
    if t in _TYPE_CANON:
        return _TYPE_CANON[t]
    low = t.lower()
    return _TYPE_CANON.get(t, low)


_XML_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.S)


def _xml_title(xml: str) -> str:
    """文件/链接/卡片 XML 里的 <title>（文件名/链接标题）——唯一的
    用户可读字段，值得保留进 [type] 占位。"""
    m = _XML_TITLE_RE.search(xml)
    return (m.group(1).strip() if m else "")[:80]


# ---------------------------------------------------------------------------
# 出库行投影补丁：at_usernames / quoted（上游 _msg_row_to_dict 不投影）
# ---------------------------------------------------------------------------

def _source_to_text(source: Any) -> Optional[str]:
    """消息行 source 字段解成文本；加密块/解不开返回 None（三态：缺席≠没@）。"""
    if source is None:
        return None
    if isinstance(source, bytes):
        if not source:
            return ""
        if source[:4] == b"\x28\xb5\x2f\xfd":  # zstd 压缩帧（微信 4.x 长字段）
            try:
                from wechatauto import db as _dbm
                zstd = _dbm._get_zstd_module()
                if zstd is None:
                    return None
                return _dbm._zstd_decompress(zstd, source)
            except Exception:
                return None
        try:
            return source.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return str(source)


def _parse_at_usernames(source: Any) -> Optional[List[str]]:
    """<atuserlist> → wxid 列表。list=权威（空=确定没@）；None=不可判定。"""
    text = _source_to_text(source)
    if text is None or "atuserlist" not in text:
        return None if text is None else []
    m = _ATLIST_RE.search(text)
    if not m:
        return []
    inner = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
    return [t.strip() for t in re.split(r"[,，;；\s]+", inner) if t.strip()]


def _refermsg_field(block: str, tag: str) -> Optional[str]:
    m = re.search(_XML_TAG_RE.pattern % (tag, tag), block)
    if not m:
        return None
    v = m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip()
    return v or None


def _parse_quoted(r: Any) -> Optional[Dict[str, Any]]:
    """type-49 引用消息：appmsg XML 提取 <refermsg> + 外层 <title>。

    返回 {"quoted": {sender,sender_name,text}, "reply_text": str|None}：
    - quoted.sender：群聊里 fromusr 是群 id、真实被引人在 chatusr；
      私聊里 fromusr 即被引人。
    - reply_text：appmsg <title> = 用户这次的回复正文（引用外层）。
    """
    raw = None
    for key in ("message_content", "compress_content"):
        try:
            raw = r[key]
        except (KeyError, IndexError):
            continue
        if raw:
            break
    text = _source_to_text(raw)
    if text is None or "refermsg" not in text:
        return None
    m = re.search(r"<refermsg>(.*?)</refermsg>", text, re.S | re.I)
    if not m:
        return None
    block = m.group(1)
    fromusr = _refermsg_field(block, "fromusr")
    chatusr = _refermsg_field(block, "chatusr")
    sender = (chatusr if (fromusr and fromusr.endswith("@chatroom") and chatusr)
              else (fromusr or chatusr))
    if not sender:
        return None
    # 外层 <title> 是用户回复正文；先剥掉 refermsg 块防止拿到被引标题
    outer = text[:m.start()] + text[m.end():]
    reply_text = _refermsg_field(outer, "title")
    return {
        "quoted": {
            "sender": sender,
            "sender_name": _refermsg_field(block, "displayname"),
            "text": _refermsg_field(block, "content"),
        },
        "reply_text": reply_text,
    }


def _install_row_projection(db_or_cls: Any) -> bool:
    """给 WeChatDB._msg_row_to_dict 幂等投影 at_usernames/quoted。

    上游若原生投影（源码出现 atuserlist/at_usernames/quoted）自动跳过。
    """
    try:
        cls = db_or_cls if inspect.isclass(db_or_cls) else type(db_or_cls)
    except Exception:
        return False
    original = getattr(cls, "_msg_row_to_dict", None)
    if original is None or getattr(original, "_wac_projection", False):
        return False
    try:
        src = inspect.getsource(original)
    except (OSError, TypeError):
        src = ""
    if src and ("atuserlist" in src or "at_usernames" in src or "quoted" in src):
        return False

    def patched(self: Any, r: Any) -> dict:
        out = original(self, r)
        try:
            out["at_usernames"] = _parse_at_usernames(r["source"])
        except Exception:
            out["at_usernames"] = None
        try:
            parsed = _parse_quoted(r)
        except Exception:
            parsed = None
        out["quoted"] = parsed["quoted"] if parsed else None
        if parsed and parsed.get("reply_text"):
            # 引用回复：上游把 type-49 显示成「文件/链接/卡片」走 file 路由，
            # 用户写在 <title> 的正文被吞——还原成引用文本（审计 D4+D5）。
            out["content"] = parsed["reply_text"]
            out["type"] = "引用"
        return out

    patched.__name__ = getattr(original, "__name__", "_msg_row_to_dict")
    patched._wac_projection = True
    cls._msg_row_to_dict = patched
    return True


@dataclass
class ChannelEvent:
    """归一化入站消息事件 —— 两个框架共用的传输格式。"""

    id: str               # 稳定去重键 f"{chat_id}:{sort_seq|local_id}"
    chat_id: str          # 会话 username（wxid_* 或 *@chatroom）
    chat_type: str        # "dm" | "group"
    chat_name: str        # 会话显示名（昵称/备注/群名）
    sender_id: str        # 发送者 username；私聊=对方 wxid，群=成员 wxid，自己=本机 wxid
    sender_name: str      # 发送者显示名
    is_self: bool         # 是否本机发出（含其他端同步的自己消息）
    type: str             # text/image/voice/video/emoji/location/file/call/system/hongbao/quote
    text: str             # 正文（非文本消息给类型占位符）
    timestamp: float      # create_time
    local_id: int
    sort_seq: int
    media_path: Optional[str] = None   # 已解密下载到本地的媒体路径（可选）
    # None=不可判定（走文本兜底）；list=权威（空=确定没@）
    at_usernames: Optional[List[str]] = None
    # 被引用消息 {sender,sender_name,text}；quoted.sender==本机 wxid 视同被@
    quoted: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ChannelCore:
    """WeChatDB + Listener + guia 发送 的薄封装，输出 ChannelEvent。

    Args:
        account:        WeChatDB 账号目录名；None = 自动探测最近活跃账号
        db_dir:         微信数据根目录；None = 自动探测
        poll_interval:  Listener 轮询间隔秒
        download_media: 是否对图片/语音/文件消息尝试本地解密下载
        media_dir:      媒体落地目录；None = 跟随 wechatauto 默认
        on_event:       ChannelEvent 回调（在 listener 工作线程里跑，禁止阻塞）
    """

    def __init__(
        self,
        account: Optional[str] = None,
        db_dir: Optional[str] = None,
        poll_interval: float = 1.0,
        download_media: bool = False,
        media_dir: Optional[str] = None,
        on_event: Optional[Callable[[ChannelEvent], None]] = None,
    ):
        self._account = account
        self._db_dir = db_dir
        self._poll_interval = poll_interval
        self._download_media = download_media
        self._media_dir = media_dir
        self.on_event = on_event

        self.db = None
        self._listener = None
        self._media = None
        self.self_wxid = ""
        self.self_nick = ""
        # chat -> (self_sender_ids, 是否拿到过正向证据)
        self._self_cache: Dict[str, tuple] = {}
        self._self_lock = threading.Lock()
        self._name_cache: Dict[str, str] = {}
        self._name_lock = threading.Lock()
        # 真实微信窗口只有一个：并发发送会共享搜索框/输入框互相拣结果
        # （实测：群回复与私聊回复撞上时一起落进私聊）。进程内串行兜底。
        # 注意锁是每进程一把：gateway in-process 插件与 bridge sidecar
        # 若同时运行仍会竞争窗口——同一时刻只应有一个宿主在写。
        # Note: 串话/群聊发不出 RCA 与取舍 — 见
        # .agents/notes/implemented/bug-fix/2026-10-07-send-race-displayname.md
        self._send_lock = threading.Lock()
        self._wx_gui = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        """初始化 DB（首次密钥扫描 ~6s，阻塞）并启动全局监听。返回自检信息。"""
        from wechatauto.db import Listener, WeChatDB  # 懒导入：Windows-only

        # 先打 replica 已知缺陷补丁再实例化——补丁是进程内 monkeypatch，
        # 必须在 WeChatDB/Listener 创建前生效（上游已修则自动跳过）。
        from .replica_compat import apply_replica_patches

        apply_replica_patches()
        _install_row_projection(WeChatDB)
        self.db = WeChatDB(db_dir=self._db_dir, account=self._account)
        info = self.db.get_self_info() or {}
        self.self_wxid = (self.db.wxid or info.get("username") or "").strip()
        self.self_nick = (info.get("nick_name") or "").strip()

        if self._download_media:
            try:
                from wechatauto.media import MediaDownloader

                self._media = MediaDownloader(self.db)
                # 图片 AES 密钥是瞬态的（内存里只在看图时驻留）；扫描失败不阻塞启动
                self._media.detect_image_key()
            except Exception as e:  # noqa: BLE001
                logger.warning("MediaDownloader 初始化失败（媒体下载降级为占位符）: %s", e)
                self._media = None

        # add_all + discover：跟随新出现的会话；回调在每会话一条的串行工作线程里跑
        # PermissionError 单独放宽预算——Windows 上 os.replace 会被瞬时文件占用
        # （杀软实时扫描新写出的解密库）打挂，1.5s 间隔的 5 次不够跨扫描窗口。
        last_exc: Optional[Exception] = None
        attempts = 0
        deadline = time.time() + 90
        while attempts < 40 and time.time() < deadline:
            attempts += 1
            listener = Listener(self.db, interval=self._poll_interval)
            try:
                listener.add_all(self._on_raw_message, discover=True)
                listener.start()
                self._listener = listener
                logger.info("listener started (attempt=%d)", attempts)
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                is_perm = isinstance(e, PermissionError)
                logger.warning("listener registration failed (attempt %d): %r",
                               attempts, e)
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if hasattr(self.db, "_invalidate_cache"):
                        self.db._invalidate_cache()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(4.0 if is_perm else 1.5)
        else:
            raise RuntimeError(
                f"listener registration failed after {attempts} attempts: "
                f"{last_exc!r}")
        return {"wxid": self.self_wxid, "nickname": self.self_nick,
                "account": self.db.account, "media": bool(self._media)}

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    # ------------------------------------------------------------------
    # 读侧
    # ------------------------------------------------------------------
    def list_chats(self, limit: int = 200) -> List[Dict[str, Any]]:
        """会话列表：id=会话 username，type=dm/group，name=显示名。"""
        out = []
        for s in (self.db.get_sessions(limit=limit) or []):
            uname = s.get("username") or ""
            out.append({
                "id": uname,
                "type": self._chat_type(uname),
                "name": self._display_name(uname),
                "unread": s.get("unread", s.get("unread_count", 0)),
            })
        return out

    def chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"id": chat_id, "type": self._chat_type(chat_id),
                "name": self._display_name(chat_id)}

    def resolve_target(self, handle: str) -> Optional[str]:
        """昵称/备注/群名/wxid → 会话 username。API 只认 username（AGENTS.md 红线）。"""
        h = (handle or "").strip()
        if not h:
            return None
        if h.startswith("wxid_") or h.startswith("gh_") or h.endswith("@chatroom"):
            return h
        if h == "filehelper":
            return h
        # 群名优先，其次联系人昵称/备注
        found = self.db.group_name_to_id(h)
        if found:
            return found
        found = self.db.username_by_nickname(h)
        if found:
            return found
        hits = self.db.search_contact(h)
        if hits:
            return hits[0].get("username")
        return None

    def recent_context(self, chat: str, before_seq: int = 0,
                       before_local_id: int = 0, limit: int = 8,
                       max_age_s: float = 900.0) -> Dict[str, Any]:
        """@触发前的近期群聊上文。

        群聊里图片/文件消息无法带 @——用户典型姿势是先发媒体、再补一条
        @机器人的文字。只派发 @ 消息会把前文媒体整条丢掉（Hermes 侧实测
        反馈）。这里把该会话最近 limit 条**非本机**消息整理成
        ``{"lines", "media"}``：lines 为时间序 ``"昵称: 正文/[类型]"``
        参考行；media 为解密下载到的附件 ``[{path,type}]``——download_media
        关闭时由 ``_lazy_media`` 只在这里懒建 downloader（图片密钥是瞬态
        的，失败就只剩 [类型] 占位行）。
        ``before_seq``/``before_local_id`` 排除触发消息自身与更新的行；
        ``max_age_s`` 防止把几小时前的旧消息误当上文。
        设计权衡见 .agents/notes/implemented/bug-fix/
        2026-10-08-mention-context-media.md
        """
        lines: List[str] = []
        media: List[Dict[str, str]] = []
        if not self.db or limit <= 0:
            return {"lines": lines, "media": media}
        try:
            rows = self.db.get_messages(chat, limit=max(limit * 3, 12)) or []
        except Exception:  # noqa: BLE001
            return {"lines": lines, "media": media}
        now = time.time()
        picked: List[ChannelEvent] = []
        for m in rows:
            try:
                norm = self._normalize(chat, m)
            except Exception:  # noqa: BLE001
                continue
            if not norm or norm.is_self:
                continue
            # 系统消息（撤回通知等 sysmsg/system 行）不是用户上文
            if norm.type == "system" or "<sysmsg" in (norm.text or "")[:64]:
                continue
            if before_seq and norm.sort_seq >= before_seq:
                continue
            if before_local_id and norm.local_id == before_local_id:
                continue
            if max_age_s > 0 and norm.timestamp \
                    and now - norm.timestamp > max_age_s:
                continue
            picked.append(norm)
            if len(picked) >= limit:
                break
        for norm in reversed(picked):  # DB 最新在前 → 反回时间序
            body = norm.text.strip() or f"[{norm.type}]"
            lines.append(f"{norm.sender_name}: {body}")
            mp = norm.media_path
            if not mp and norm.type in ("image", "voice", "video", "file"):
                # 上文媒体惰性下载：download_media 全局开关关闭时也在这里
                # 懒建 downloader——只在 @触发时为这几条上文解密附件，
                # 不给每条入站媒体都落盘。图片密钥是瞬态的，失败就留 [类型]
                # 占位行，不影响正文。
                dl = self._lazy_media()
                if dl:
                    try:
                        mp = dl.download_media(chat, norm.local_id,
                                               save_dir=self._media_dir)
                    except Exception:  # noqa: BLE001
                        mp = None
            if mp:
                media.append({"path": mp, "type": norm.type})
        return {"lines": lines, "media": media}

    def lazy_download_media(self, chat: str, local_id: int) -> Optional[str]:
        """按需解密一条媒体消息——与 download_media 全局开关解耦。

        供 adapter 在"确定要派发"之后调用（mention 门后）：DM 直发图片、
        群里 @触发时上文媒体都由它解密，而不是给每条入站媒体都落盘。
        图片密钥是瞬态的，发送时刻太久的附件可能已不可解。"""
        dl = self._lazy_media()
        if not dl:
            return None
        try:
            return dl.download_media(chat, local_id, save_dir=self._media_dir)
        except Exception:  # noqa: BLE001
            return None

    def _lazy_media(self):
        """惰性 MediaDownloader（与 download_media 全局开关解耦）。"""
        dl = getattr(self, "_ctx_media", None)
        if dl is None:
            try:
                from wechatauto.media import MediaDownloader
                dl = MediaDownloader(self.db)
                dl.detect_image_key()
            except Exception:  # noqa: BLE001
                dl = False  # 探测失败只记一次，不反复重试
            self._ctx_media = dl
        return dl or None

    # ------------------------------------------------------------------
    # 写侧（GUI 驱动，调用方负责放到线程池）
    # ------------------------------------------------------------------
    def _gui(self):
        """惰性共享 WeChatGUI。

        ``quick_send`` 每次调用新建实例——``_current_chat`` 会话复用、
        ``_cached_db`` 密钥缓存、UIA 驱动全部冷启动，逐条发送每次都要
        重走 open_chat/搜索框定位的主要耗时就是这么来的。
        """
        if self._wx_gui is None:
            from wechatauto.guia import WeChatGUI
            self._wx_gui = WeChatGUI()
        return self._wx_gui

    def _who_name(self, username: str) -> str:
        """GUI 发送目标转成显示名。

        微信搜索框只认昵称/备注/微信号/群名，不认 wxid/@chatroom id
        （replica 的 ``_resolve_search_keyword`` 只查 search_contact，
        群聊在其中没有记录）。传 id 必搜不到、还会在搜索框/侧栏空转到
        超时；``uia.current_chat()`` 返回的也是显示名，传名后
        ``current_chat() == who`` 的免搜索复用路径才真正生效。
        查不到显示名时 ``_display_name`` 原样回落 username。
        """
        if username.startswith(("wxid_", "gh_")) \
                or username.endswith("@chatroom") \
                or username == "filehelper":
            return self._display_name(username)
        return username

    def _send_mark(self, username: str) -> int:
        """发送前落库水位（sort_seq 上限）；verify 只认不比它旧的行。"""
        try:
            rows = self.db.get_messages(username, limit=8) or []
            return max((int(r.get("sort_seq") or 0) for r in rows), default=0)
        except Exception:  # noqa: BLE001
            return 0

    def _verify_sent(self, username: str, text: Optional[str], mark: int,
                     timeout: float = 10.0) -> bool:
        """用 ChannelCore 已开的 db 按会话 username 回读确认。

        replica 自带的 verify 另建 WeChatDB（重复密钥扫描）且按显示名
        反解 username——群聊在 search_contact 里查不到，口径对不上。
        这里直接拿消息表 username 校验：打进错误会话的消息在本会话表里
        查不到，顺带覆盖串话漏检。text=None 用于附件：只认水位后有自己
        发出的新行（附件行正文是 XML，不做逐字比对）。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rows = self.db.get_messages(username, limit=10) or []
            except Exception:  # noqa: BLE001
                rows = []
            for m in rows:
                if int(m.get("sort_seq") or 0) < mark:
                    continue
                if not self._is_self(username, m):
                    continue
                if text is None or str(m.get("content") or "") == text:
                    return True
            time.sleep(1.0)
        return False

    def send_text(self, target: str, text: str, verify: bool = False) -> Dict[str, Any]:
        username = self.resolve_target(target)
        if not username:
            return {"ok": False, "error": f"无法解析发送对象: {target!r}"}
        mark = self._send_mark(username) if verify else 0
        gui = self._gui()
        # send_msg 的盲复用快路径（who==_current_chat 时直接往 _last_input_box
        # 输入）不校验真实前台会话——用户手动切窗后旧输入框元素会把消息打进
        # 别的会话。置空后走 current_chat()/标题 OCR 校验过的路径，照样复用。
        gui._last_input_box = None
        with self._send_lock:
            resp = gui.send_msg(text, self._who_name(username), verify=False)
        # WxResponse.status 是中文（成功/失败/错误），兼容英文取值。
        # Note: 误判会导致 Hermes 侧兜底重发 — 见
        # .agents/notes/implemented/bug-fix/2026-10-07-hermes-v0.21-compat.md
        ok = str(resp.get("status", "")) in ("成功", "success")
        if ok and verify:
            ok = self._verify_sent(username, text, mark)
            return {"ok": ok,
                    "message": "已发送并确认" if ok else "已发送但数据库未确认",
                    "to": username}
        return {"ok": ok, "message": resp.get("message"), "to": username}

    def send_file(self, target: str, path: str, image: bool = False,
                  verify: bool = False) -> Dict[str, Any]:
        username = self.resolve_target(target)
        if not username:
            return {"ok": False, "error": f"无法解析发送对象: {target!r}"}
        mark = self._send_mark(username) if verify else 0
        gui = self._gui()
        with self._send_lock:
            resp = (gui.send_image if image else gui.send_file)(
                path, self._who_name(username), verify=False)
        # WxResponse.status 是中文（成功/失败/错误），兼容英文取值
        ok = str(resp.get("status", "")) in ("成功", "success")
        if ok and verify:
            ok = self._verify_sent(username, None, mark)
            return {"ok": ok,
                    "message": "已发送并确认" if ok else "已发送但数据库未确认",
                    "to": username}
        return {"ok": ok, "message": resp.get("message"), "to": username}

    # ------------------------------------------------------------------
    # 入站归一化
    # ------------------------------------------------------------------
    def _on_raw_message(self, msg: dict, listener) -> None:
        """db.Listener 回调（每会话串行工作线程）。"""
        chat = msg.get("username") or ""
        if not chat:
            return
        try:
            ev = self._normalize(chat, msg)
            if ev and self.on_event:
                self.on_event(ev)
        except Exception:  # noqa: BLE001 — 单条坏消息不能杀死监听线程
            logger.exception("normalize message failed: chat=%s local_id=%s",
                             chat, msg.get("local_id"))

    def _normalize(self, chat: str, msg: dict) -> Optional[ChannelEvent]:
        is_group = self._chat_type(chat) == "group"
        sender_username = (msg.get("sender_username") or "").strip()
        is_self = self._is_self(chat, msg)

        content = msg.get("content")
        text = content if isinstance(content, str) else ""

        if is_self:
            sender_id = self.self_wxid or "self"
            sender_name = self.self_nick or "me"
        elif is_group:
            sender_id = sender_username
            if not sender_id or sender_id.isdigit() or sender_id == chat:
                # 权威源兜底：群消息 content 前缀 "wxid_…:\n" 才是真发言人
                m = _GROUP_SENDER_PREFIX_RE.match(text)
                if m:
                    sender_id = m.group(1)
            if not sender_id:
                sender_id = str(msg.get("sender_id") or "")
            sender_name = self._display_name(sender_id) if sender_id else "?"
        else:
            # 私聊里 sender_username 可能为空（对方正是会话本身）
            sender_id = sender_username or chat
            sender_name = self._display_name(sender_id)

        # 剥掉群消息 content 的 "wxid_…:" 前缀，正文只留用户文本
        if is_group:
            m = _GROUP_SENDER_PREFIX_RE.match(text)
            if m:
                text = text[m.end():].lstrip("\n")

        mtype = _canon_type(msg.get("type"))
        stripped = text.lstrip()
        if (mtype != "text"
                and (stripped.startswith("<?xml")
                     or stripped.startswith("<msg")
                     or stripped.startswith("<sysmsg"))):
            # 媒体/系统行的 content 是信封 XML（aeskey/cdn/md5 等元数据），
            # 不是用户可见正文——原样泄给 agent 会被当成"这是什么"来回答。
            title = _xml_title(stripped)
            text = f"[{mtype}] {title}" if title else f"[{mtype}]"
        elif not stripped:
            text = f"[{mtype}]"

        media_path = None
        if self._media and mtype in ("image", "voice", "video", "file"):
            try:
                media_path = self._media.download_media(
                    chat, msg["local_id"], save_dir=self._media_dir)
            except Exception as e:  # noqa: BLE001
                logger.debug("media download failed chat=%s id=%s: %s",
                             chat, msg.get("local_id"), e)

        # sort_seq 是跨分片单调水印；local_id 跨分片会重复，去重键必须用它
        seq = int(msg.get("sort_seq") or 0)
        return ChannelEvent(
            id=f"{chat}:{seq or int(msg.get('local_id') or 0)}",
            chat_id=chat,
            chat_type="group" if is_group else "dm",
            chat_name=self._display_name(chat),
            sender_id=sender_id,
            sender_name=sender_name,
            is_self=is_self,
            type=mtype,
            text=text,
            timestamp=float(msg.get("create_time") or time.time()),
            local_id=int(msg.get("local_id") or 0),
            sort_seq=seq,
            media_path=media_path,
            at_usernames=msg.get("at_usernames"),
            quoted=msg.get("quoted"),
        )

    # ------------------------------------------------------------------
    # 「自己发的」判定 —— 见模块 docstring，不能用 sender_id==2 一刀切
    # ------------------------------------------------------------------
    def _is_self(self, chat: str, msg: dict) -> bool:
        su = (msg.get("sender_username") or "").strip()
        if su and self.self_wxid and su == self.self_wxid:
            return True
        m = _XML_SELF_RE.search(str(msg.get("content") or ""))
        if m and self.self_wxid and m.group(1) == self.self_wxid:
            return True
        sid = str(msg.get("sender_id") or "")
        self_ids, decided = self._self_ids(chat)
        if sid and sid in self_ids:
            return True
        if su and su != self.self_wxid:
            return False
        # 没有任何正向证据时才退回 {1,2}（4.1.13.x 出站行实测用过 1）
        return not decided and sid in {"1", "2"}

    def _self_ids(self, chat: str) -> tuple:
        """(本机 sender_id 集合, 是否拿到正向证据)；每会话缓存。"""
        with self._self_lock:
            cached = self._self_cache.get(chat)
        if cached is not None:
            return cached
        votes: Dict[str, List[int]] = {}
        try:
            conns = self.db._msg_conns(chat)
        except Exception as e:  # noqa: BLE001
            logger.debug("status votes 读取失败 chat=%s: %s", chat, e)
            conns = None
        try:
            for conn, table in conns or []:
                if not _MSG_TABLE_RE.match(str(table)):
                    continue
                try:
                    rows = conn.execute(
                        'SELECT real_sender_id, status FROM "%s" '
                        "ORDER BY sort_seq DESC LIMIT 200" % table
                    ).fetchall()
                except Exception:  # noqa: BLE001
                    continue
                for sid, st in rows:
                    v = votes.setdefault(str(sid), [0, 0])
                    v[0] += 1
                    if st == 2:
                        v[1] += 1
        finally:
            # _msg_conns 的连接所有权在调用方，必须显式关闭：
            # 泄漏的句柄会在下次快照重建时挡住 os.replace（Windows）。
            closed = set()
            for conn, _ in conns or []:
                if id(conn) not in closed:
                    closed.add(id(conn))
                    conn.close()
        self_ids = {sid for sid, (n, c) in votes.items()
                    if c >= max(1, int(0.3 * n))}
        result = (self_ids, bool(votes))
        with self._self_lock:
            self._self_cache[chat] = result
        return result

    # ------------------------------------------------------------------
    # 展示名（带缓存）
    # ------------------------------------------------------------------
    def _display_name(self, username: str) -> str:
        if not username:
            return ""
        with self._name_lock:
            if username in self._name_cache:
                return self._name_cache[username]
        name = ""
        try:
            if username.endswith("@chatroom"):
                name = self.db.group_id_to_name(username) or ""
            if not name:
                name = self.db.get_nickname(username) or ""
        except Exception:  # noqa: BLE001
            name = ""
        name = name or username
        with self._name_lock:
            self._name_cache[username] = name
        return name

    @staticmethod
    def _chat_type(username: str) -> str:
        return "group" if username.endswith("@chatroom") else "dm"


def split_text(text: str, limit: int = DEFAULT_MAX_TEXT) -> List[str]:
    """长文本分块：优先段落边界，其次换行/空格。"""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks, rest = [], text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 3:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


class EventBuffer:
    """给 bridge 用的有界事件缓冲 + 长轮询游标。"""

    def __init__(self, capacity: int = 2000):
        self._cond = threading.Condition()
        self._events: List[Dict[str, Any]] = []
        self._seq = 0
        self._capacity = capacity

    def push(self, ev: ChannelEvent) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "event": ev.to_dict()})
            if len(self._events) > self._capacity:
                self._events = self._events[-self._capacity:]
            self._cond.notify_all()

    def wait(self, cursor: int, timeout: float) -> Dict[str, Any]:
        """返回 seq > cursor 的事件；timeout 内无事件则返回空列表。"""
        deadline = time.time() + max(0.0, timeout)
        with self._cond:
            # cursor > 当前最大 seq 在本进程内不可能出现——只能是桥重启后
            # seq 归零、而客户端还攥着旧实例的游标。归零重放整个缓冲区
            # （消费方按事件 id 去重），否则该客户端会失明直到队列重新
            # 涨到旧游标高度（真实事故：第三方拉起新桥，adapter 的
            # cursor=20 把所有 seq<=20 的消息静默丢弃）。
            if cursor > self._seq:
                cursor = 0
            while True:
                batch = [e for e in self._events if e["seq"] > cursor]
                if batch:
                    return {"cursor": batch[-1]["seq"], "events": batch}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"cursor": cursor, "events": []}
                self._cond.wait(timeout=remaining)
