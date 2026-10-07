"""wechatauto-channels —— 把微信 4.x Windows 客户端包装成通用消息渠道。

底层：wechatauto-replica（读=本地 SQLCipher 库解密，写=UIA+OCR 驱动真实客户端）。
用法：
  - Hermes 平台插件直接 ``from wechatauto_channels.core import ChannelCore``
  - OpenClaw 渠道插件经 ``python -m wechatauto_channels.bridge`` 的本地 HTTP 桥接入
"""

from .core import ChannelCore, ChannelEvent, EventBuffer, split_text

__version__ = "0.1.0"
__all__ = ["ChannelCore", "ChannelEvent", "EventBuffer", "split_text"]
