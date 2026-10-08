"""每会话（群聊）触发模式表——V5 chat-policy 的最小移植。

模式：
- ``at``     仅被 @/被引用时触发（其余消息仍由 recent_context 录作上文）
- ``reply``  该群每条消息都触发（等价于对该群关闭 require_mention）
- ``listen`` 永不触发——历史仍留在 WeChatDB，可通过 bridge ``/context``
             或 ``python -m wechatauto_channels recent`` 按需查询

来源：``WECHATAUTO_CHAT_POLICY`` 指向的 JSON 文件（mtime 热加载）::

    {"default": "at",
     "rules": [{"chat": "有点东西", "mode": "at"},
               {"chat": "家庭群",   "mode": "listen"},
               {"chat": "测试群",   "mode": "reply"}]}

``chat`` 匹配 chat_id（…@chatroom）或显示名；第一条命中的规则生效。
加载失败沿用上一份成功配置；首次失败/文件缺失等价于无文件——回落到
env 的 ``group_require_mention`` 行为（fail-compatible，不因配置文件
问题改变既有运行语义）。规则里的非法 mode 跳过该条而不是整体失效。

设计权衡见 .agents/notes/implemented/feature/
2026-10-08-group-chat-modes.md
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MODES = ("at", "reply", "listen")


class ChatPolicy:
    """线程安全程度：dispatch 在 gateway 单线程事件循环里调用 mode_for，
    热加载只做 stat+json.loads，不加锁。"""

    def __init__(self, path: str):
        self._path = path
        self._mtime = -1.0
        self._default = "at"
        self._rules: List[Dict[str, str]] = []
        self._loaded = False  # 是否成功加载过一次

    def mode_for(self, chat_id: str, chat_name: str = "",
                 default: str = "at") -> str:
        """返回该群的有效模式。无文件/未加载 → 返回 ``default``。"""
        self._reload_if_changed()
        if not self._loaded:
            return default
        cid = (chat_id or "").strip()
        cname = (chat_name or "").strip()
        for r in self._rules:
            c = r.get("chat", "")
            if c and (c == cid or c == cname):
                return r["mode"]
        return self._default

    def _reload_if_changed(self) -> None:
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return  # 文件不存在/不可读 → 保持现状（首次=无文件语义）
        if mtime == self._mtime and self._loaded:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat-policy %s 加载失败，沿用上一份配置: %r",
                           self._path, exc)
            self._mtime = mtime  # 记住坏文件 mtime，不反复重读
            return
        rules: List[Dict[str, str]] = []
        for r in (raw.get("rules") or []):
            if not isinstance(r, dict):
                continue
            chat = str(r.get("chat") or "").strip()
            mode = str(r.get("mode") or "").strip().lower()
            if chat and mode in MODES:
                rules.append({"chat": chat, "mode": mode})
            else:
                logger.warning("chat-policy 非法规则跳过: %r", r)
        default = str(raw.get("default") or "at").strip().lower()
        self._rules = rules
        self._default = default if default in MODES else "at"
        self._mtime = mtime
        self._loaded = True
        logger.info("chat-policy %s 已加载: default=%s rules=%d",
                    self._path, self._default, len(rules))


def load_policy(path: str) -> Optional[ChatPolicy]:
    """由调用方解析好的路径构建策略对象；空路径返回 None。

    env/extra 的读取归 adapter（profile-scoped ``extra_or_secret``），
    本模块不直接摸 ``os.environ``。"""
    path = (path or "").strip()
    return ChatPolicy(path) if path else None
