"""wechatauto-channels 共享核心 —— 把 wechatauto-replica 规范化成通用渠道原语。

Hermes 适配器直接 import；OpenClaw 渠道插件经 bridge.py 的 HTTP sidecar
走同一套逻辑。本模块对 wechatauto 全部做**懒导入**，以便在非 Windows
环境下也能 import 本包做单测（真正 start/send 才需要 Windows + 已登录微信）。

消息身份判定（踩坑记录，来自 wechatbot-new v2.2.8）：
``real_sender_id`` 是**分片内**短编号，写死 ``== 2`` 会把换分片前的自己
算成对方。判定「是不是我发的」只用正向证据：
  ① 消息表 ``status == 2``（微信给自己消息的标记，跨分片稳定）；
  ② 表情/图片 XML 里 ``fromusername`` == 本机 wxid；
  ③ ``sender_username``（经 SenderName2Id 反查）== 本机 wxid。
整个会话都拿不到正向证据时，才退回 ``sender_id == 2``。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MSG_TABLE_RE = re.compile(r"^Msg_[0-9a-f]{32}$")
_XML_SELF_RE = re.compile(r'fromusername\s*=\s*["\']?\s*([A-Za-z0-9_\-@]+)')
# 微信文本框实际上限远高于此；留安全边际并给 UIA/OCR 路径减负
DEFAULT_MAX_TEXT = 4000


@dataclass
class ChannelEvent:
    """归一化入站消息事件 —— 两个框架共用的传输格式。"""

    id: str               # 稳定去重键 f"{chat_id}:{local_id}"
    chat_id: str          # 会话 username（wxid_* 或 *@chatroom）
    chat_type: str        # "dm" | "group"
    chat_name: str        # 会话显示名（昵称/备注/群名）
    sender_id: str        # 发送者 username；私聊=对方 wxid，群=成员 wxid，自己=本机 wxid
    sender_name: str      # 发送者显示名
    is_self: bool         # 是否本机发出（含其他端同步的自己消息）
    type: str             # text/image/voice/video/emoji/file/link/…
    text: str             # 正文（非文本消息给类型占位符）
    timestamp: float      # create_time
    local_id: int
    sort_seq: int
    media_path: Optional[str] = None   # 已解密下载到本地的媒体路径（可选）

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

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        """初始化 DB（首次密钥扫描 ~6s，阻塞）并启动全局监听。返回自检信息。"""
        from wechatauto.db import Listener, WeChatDB  # 懒导入：Windows-only

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

        self._listener = Listener(self.db, interval=self._poll_interval)
        # add_all + discover：跟随新出现的会话；回调在每会话一条的串行工作线程里跑
        self._listener.add_all(self._on_raw_message, discover=True)
        self._listener.start()
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

    # ------------------------------------------------------------------
    # 写侧（GUI 驱动，调用方负责放到线程池）
    # ------------------------------------------------------------------
    def send_text(self, target: str, text: str, verify: bool = False) -> Dict[str, Any]:
        username = self.resolve_target(target)
        if not username:
            return {"ok": False, "error": f"无法解析发送对象: {target!r}"}
        from wechatauto.guia import quick_send

        resp = quick_send(text, username, verify=verify)
        ok = str(resp.get("status", "")).lower() == "success"
        return {"ok": ok, "message": resp.get("message"), "to": username}

    def send_file(self, target: str, path: str, image: bool = False,
                  verify: bool = False) -> Dict[str, Any]:
        username = self.resolve_target(target)
        if not username:
            return {"ok": False, "error": f"无法解析发送对象: {target!r}"}
        from wechatauto.guia import quick_send_file, quick_send_image

        resp = (quick_send_image if image else quick_send_file)(path, username, verify=verify)
        ok = str(resp.get("status", "")).lower() == "success"
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
        sender_username = (msg.get("sender_username") or "").strip()
        is_self = self._is_self(chat, msg)
        if is_self:
            sender_id = self.self_wxid or "self"
            sender_name = self.self_nick or "me"
        elif self._chat_type(chat) == "dm":
            # 私聊里 sender_username 可能为空（对方正是会话本身）
            sender_id = sender_username or chat
            sender_name = self._display_name(sender_id)
        else:
            sender_id = sender_username or str(msg.get("sender_id") or "")
            sender_name = self._display_name(sender_id) if sender_id else "?"

        mtype = (msg.get("type") or "text").lower()
        text = msg.get("content")
        if not isinstance(text, str) or not text.strip():
            text = f"[{mtype}]"

        media_path = None
        if self._media and mtype in ("image", "voice", "video", "file"):
            try:
                media_path = self._media.download_media(
                    chat, msg["local_id"], save_dir=self._media_dir)
            except Exception as e:  # noqa: BLE001
                logger.debug("media download failed chat=%s id=%s: %s",
                             chat, msg.get("local_id"), e)

        return ChannelEvent(
            id=f"{chat}:{msg.get('local_id')}",
            chat_id=chat,
            chat_type=self._chat_type(chat),
            chat_name=self._display_name(chat),
            sender_id=sender_id,
            sender_name=sender_name,
            is_self=is_self,
            type=mtype,
            text=text,
            timestamp=float(msg.get("create_time") or time.time()),
            local_id=int(msg.get("local_id") or 0),
            sort_seq=int(msg.get("sort_seq") or 0),
            media_path=media_path,
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
        # 没有任何正向证据时才退回 ==2
        return not decided and sid == "2"

    def _self_ids(self, chat: str) -> tuple:
        """(本机 sender_id 集合, 是否拿到正向证据)；每会话缓存，LRU 无必要。"""
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
            while True:
                batch = [e for e in self._events if e["seq"] > cursor]
                if batch:
                    return {"cursor": batch[-1]["seq"], "events": batch}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"cursor": cursor, "events": []}
                self._cond.wait(timeout=remaining)
