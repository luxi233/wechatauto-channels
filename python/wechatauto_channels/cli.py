"""``python -m wechatauto_channels recent <chat>`` — 近期上文只读查询。

listen 模式的查询出口之一（另一个是 bridge ``GET /context``）：agent 经
Hermes 终端工具直接跑这条命令即可读到某个群的最近消息，无需持有
bridge token。

只建 WeChatDB 读连接（replica 走解密快照 + ``mode=ro``），不起 Listener、
不碰 GUI——与正在运行的 gateway/bridge 共存安全；数据新鲜度取决于最近
一次快照刷新（通常即运行中宿主的上一次轮询）。
"""

from __future__ import annotations

import argparse
import json
import sys


def recent_main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="wechatauto-recent",
        description="查询某会话近期消息上文（群名/昵称/wxid 均可）")
    ap.add_argument("chat", help="会话标识：显示名/群名/wxid/@chatroom")
    ap.add_argument("-n", "--limit", type=int, default=8,
                    help="最多取几条（默认 8，上限 50）")
    ap.add_argument("--max-age", type=float, default=900.0,
                    help="超过该秒数的消息不取（默认 900）")
    ap.add_argument("--account", default=None, help="微信账号目录名")
    ap.add_argument("--db-dir", default=None, help="微信数据根目录")
    ap.add_argument("--media-dir", default=None, help="媒体解密输出目录")
    ap.add_argument("--workdir", default=None,
                    help="解密快照工作目录（默认独立目录——不碰 gateway"
                         " 的共享快照。共享写入有投毒风险：本进程重建时"
                         " WAL 合并失败会替换成缺近消息的快照并盖stamp，"
                         " gateway 下次轮询沿用它一起变瞎。独立目录的"
                         " 代价是偶发读到偏旧快照，重跑一次即可）")
    ap.add_argument("--json", action="store_true",
                    help="输出原始 JSON 而非人类可读行")
    args = ap.parse_args(argv)

    import os
    import tempfile
    import time

    from wechatauto.db import WeChatDB  # 懒导入：Windows-only

    from .core import ChannelCore, _install_row_projection

    _install_row_projection(WeChatDB)
    workdir = args.workdir or os.path.join(
        tempfile.gettempdir(), "wechatauto_db_recent_cli")
    core = ChannelCore(account=args.account, db_dir=args.db_dir,
                       media_dir=args.media_dir)
    # 不走 core.start()——那会拉起 Listener 轮询线程。手动挂 db 即可，
    # recent_context 只需要 db/媒体惰性下载两个字段。
    core.db = WeChatDB(db_dir=args.db_dir, account=args.account,
                       workdir=workdir)

    target = core.resolve_target(args.chat) or args.chat
    ctx = core.recent_context(target, limit=max(1, min(args.limit, 50)),
                              max_age_s=args.max_age)
    if not ctx.get("lines") and not ctx.get("media"):
        # 微信写入高峰时 WAL 合并会失败落到仅主库快照（近消息全在 WAL
        # 里）——空结果先怀疑陈旧快照：换一个全新 workdir 强制重建一次
        # （旧实例的 ro 句柄会让删文件/盖 stamp 都不干净，新目录最省事）。
        time.sleep(2)
        core.db = WeChatDB(db_dir=args.db_dir, account=args.account,
                           workdir=workdir + "_retry")
        ctx = core.recent_context(target, limit=max(1, min(args.limit, 50)),
                                  max_age_s=args.max_age)
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        for line in ctx.get("lines") or []:
            print(line)
        for m in ctx.get("media") or []:
            print(f"[media:{m['type']}] {m['path']}")
        if not ctx.get("lines") and not ctx.get("media"):
            print("(无近期上文)", file=sys.stderr)
    return 0
