"""wechatauto-channels HTTP sidecar —— 给 OpenClaw 渠道插件用的本地桥。

只用 Python 标准库（不引 fastapi）：绑定 127.0.0.1 + Bearer token 鉴权。
OpenClaw 侧通过 ``GET /events?cursor=N&timeout_ms=30000`` 长轮询入站消息，
通过 ``POST /send`` / ``POST /send_file`` 出站。

端点::

    GET  /health                      → {ok, wxid, nickname, media}
    GET  /chats                       → {chats: [{id,type,name,unread}]}
    GET  /chat?id=<username>          → {id,type,name}
    GET  /resolve?handle=<name>       → {username} | {username: null}
    GET  /events?cursor=N&timeout_ms=T→ {cursor, events:[{seq,event}]}
    POST /send       {to, text, verify?}
    POST /send_file  {to, path, image?}
"""

from __future__ import annotations

import argparse
import atexit
import faulthandler
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .core import ChannelCore, EventBuffer, split_text, DEFAULT_MAX_TEXT

logger = logging.getLogger(__name__)

# ── 单实例锁 ────────────────────────────────────────────────────────────────
# 两个 bridge 同账号共用解密缓存 = wechatbot-new v2.2.5 的 WAL 缓存损坏类事故。
# Windows 用 msvcrt.byte-lock，POSIX 用 fcntl.flock；都不可用时降级放行并告警。
_INSTANCE_LOCK = None


def _acquire_instance_lock() -> None:
    global _INSTANCE_LOCK
    if _INSTANCE_LOCK is not None:
        return
    path = os.path.join(tempfile.gettempdir(), "wechatauto-channels-bridge.lock")
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        handle.close()
        raise RuntimeError(
            "已有另一个 wechatauto bridge 在运行（同账号共享解密缓存会损坏），"
            "请先停掉旧实例") from e
    except ImportError:  # 极端平台两个都没有：放行但告警
        handle.close()
        logger.warning("平台无 msvcrt/fcntl —— 单实例锁不可用")
        return
    _INSTANCE_LOCK = handle
    logger.info("single-instance lock acquired: %s", path)


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK
    handle, _INSTANCE_LOCK = _INSTANCE_LOCK, None
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass
    try:
        handle.close()
    except Exception:  # noqa: BLE001
        pass


# ── 崩溃取证 ────────────────────────────────────────────────────────────────
# stardome 教训（2026-09-22 两次 code=1 静默死亡，stderr 无 traceback）：
# faulthandler 抓原生层崩溃（comtypes/UIA 段错误），excepthook 抓未处理异常，
# atexit 标记正常退出——「无 atexit 记录」本身就是外部击杀的直接证据。
_CRASH_FP = None


def _crash_write(text: str) -> None:
    for sink in (sys.stderr, _CRASH_FP):
        try:
            if sink is not None:
                sink.write(text)
                sink.flush()
        except Exception:  # noqa: BLE001
            pass


def _install_crash_diagnostics() -> None:
    global _CRASH_FP
    log_dir = os.path.join(tempfile.gettempdir(), "wechatauto-channels")
    try:
        os.makedirs(log_dir, exist_ok=True)
        _CRASH_FP = open(os.path.join(log_dir, "bridge-fatal.log"),
                         "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_CRASH_FP)
    except Exception:  # noqa: BLE001
        try:
            faulthandler.enable()
        except Exception:  # noqa: BLE001
            pass

    def on_uncaught(exc_type, exc, tb):
        _crash_write("\n===== BRIDGE FATAL (uncaught) %s =====\n%s\n" % (
            time.strftime("%F %T"),
            "".join(traceback.format_exception(exc_type, exc, tb))))

    def on_thread_crash(args):
        _crash_write("\n===== BRIDGE THREAD CRASH (%s) %s =====\n%s\n" % (
            getattr(args.thread, "name", "?"), time.strftime("%F %T"),
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))))

    def on_atexit():
        _crash_write("\n===== BRIDGE EXIT %s =====\n" % time.strftime("%F %T"))

    sys.excepthook = on_uncaught
    threading.excepthook = on_thread_crash
    atexit.register(on_atexit)

MAX_LONGPOLL_MS = 55_000
MAX_BODY_BYTES = 64 * 1024


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class BridgeState:
    def __init__(self, core: ChannelCore, token: str):
        self.core = core
        self.token = token
        self.buffer = EventBuffer()
        self._send_lock = threading.Lock()  # GUI 发送全局串行，避免并发抢输入框


def make_handler(state: BridgeState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wechatauto-bridge/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默默认日志，走 logger
            logger.debug("bridge: " + fmt, *args)

        # -- 鉴权 ----------------------------------------------------
        def _authorized(self) -> bool:
            if not state.token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {state.token}"

        def _deny(self) -> bool:
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return True

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > MAX_BODY_BYTES:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        # -- 路由 ----------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return self._deny()
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            path = parsed.path.rstrip("/")

            if path == "/health":
                return _json_response(self, 200, {
                    "ok": True, "wxid": state.core.self_wxid,
                    "nickname": state.core.self_nick,
                    "media": bool(state.core._media),
                })
            if path == "/chats":
                return _json_response(self, 200,
                                      {"chats": state.core.list_chats()})
            if path == "/chat":
                cid = (q.get("id") or [""])[0]
                return _json_response(self, 200, state.core.chat_info(cid))
            if path == "/resolve":
                handle = (q.get("handle") or [""])[0]
                return _json_response(self, 200,
                                      {"username": state.core.resolve_target(handle)})
            if path == "/events":
                try:
                    cursor = int((q.get("cursor") or ["0"])[0])
                    timeout_ms = min(int((q.get("timeout_ms") or ["30000"])[0]),
                                     MAX_LONGPOLL_MS)
                except ValueError:
                    return _json_response(self, 400,
                                          {"ok": False, "error": "bad cursor/timeout"})
                return _json_response(
                    self, 200,
                    state.buffer.wait(cursor, timeout_ms / 1000.0))
            return _json_response(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._deny()
            path = self.path.rstrip("/")
            body = self._body()
            to = (body.get("to") or "").strip()
            if not to:
                return _json_response(self, 400,
                                      {"ok": False, "error": "missing 'to'"})

            # GUI 发送会操纵真实微信窗口：全局串行
            with state._send_lock:
                if path == "/send":
                    text = body.get("text") or ""
                    verify = bool(body.get("verify"))
                    results = []
                    ok = True
                    for chunk in split_text(text, DEFAULT_MAX_TEXT):
                        r = state.core.send_text(to, chunk, verify=verify)
                        results.append(r)
                        ok = ok and r.get("ok", False)
                        if not r.get("ok"):
                            break
                    return _json_response(self, 200 if ok else 502,
                                          {"ok": ok, "results": results})
                if path == "/send_file":
                    fp = body.get("path") or ""
                    r = state.core.send_file(to, fp,
                                             image=bool(body.get("image")),
                                             verify=bool(body.get("verify")))
                    return _json_response(self, 200 if r.get("ok") else 502, r)
            return _json_response(self, 404, {"ok": False, "error": "not found"})

    return Handler


def serve_thread(host: str, port: int, token: str,
                 core: ChannelCore) -> "tuple":
    """非阻塞启动（测试/嵌入式用）。返回 (thread, httpd)；httpd.shutdown() 停止。"""
    _install_crash_diagnostics()
    _acquire_instance_lock()
    state = BridgeState(core, token)
    core.on_event = state.buffer.push
    info = core.start()
    logger.info("bridge up: account=%s wxid=%s nick=%s",
                info["account"], info["wxid"], info["nickname"])
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    t = threading.Thread(target=httpd.serve_forever,
                         daemon=True, name="wac-bridge-http")
    t.start()
    logger.info("listening on http://%s:%d (token %s)",
                host, httpd.server_address[1], "required" if token else "DISABLED")
    return t, httpd


def serve(host: str, port: int, token: str, core: ChannelCore) -> None:
    t, httpd = serve_thread(host, port, token, core)
    try:
        t.join()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
        httpd.shutdown()
        httpd.server_close()
        _release_instance_lock()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wechatauto-bridge")
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址（默认仅本机回环，勿改）")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--token", default=None,
                    help="Bearer token；缺省时生成随机值并打印到 stderr")
    ap.add_argument("--account", default=None, help="微信账号目录名（多账号时指定）")
    ap.add_argument("--db-dir", default=None, help="微信数据根目录（自动探测失败时指定）")
    ap.add_argument("--poll-interval", type=float, default=1.0)
    ap.add_argument("--download-media", action="store_true",
                    help="尝试解密下载图片/语音/文件到本地")
    ap.add_argument("--media-dir", default=None)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning("绑定非回环地址 %s —— 本桥没有 TLS，请确认你知道在做什么",
                       args.host)
    token = args.token or secrets.token_urlsafe(24)
    if not args.token:
        print(f"[bridge] generated token: {token}", flush=True)

    core = ChannelCore(account=args.account, db_dir=args.db_dir,
                       poll_interval=args.poll_interval,
                       download_media=args.download_media,
                       media_dir=args.media_dir)
    serve(args.host, args.port, token, core)
    return 0
