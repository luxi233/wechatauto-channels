"""bridge 端到端测试 —— stub 掉 wechatauto（Listener/WeChatDB/guia），
真起 HTTP server 走完整链路：Listener 回调 → _normalize → EventBuffer →
GET /events 长轮询；POST /send → resolve_target → quick_send。

无 Windows/微信依赖；验证的是「HTTP 契约 + 事件管线 + 鉴权」这一段。
"""

import json
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── wechatauto stub（必须在 import bridge 前装好）──────────────────────────

_sent = []
_listener_cb = {}


class _FakeListener:
    def __init__(self, db, interval=1.0):
        self.db = db
        self.interval = interval

    def add_all(self, cb, discover=False):
        _listener_cb["cb"] = cb

    def start(self):
        pass

    def stop(self):
        pass


class _FakeWeChatDB:
    wxid = "wxid_self001"
    account = "wxid_self001_ab12"

    def __init__(self, db_dir=None, account=None):
        pass

    def get_self_info(self):
        return {"username": self.wxid, "nick_name": "小助手"}

    def get_sessions(self, limit=200):
        return [{"username": "wxid_friend", "unread": 1}]

    def _msg_conns(self, user):
        return []

    def get_nickname(self, u):
        return {"wxid_friend": "好友甲"}.get(u, u)

    def group_id_to_name(self, u):
        return None

    def group_name_to_id(self, n):
        return None

    def username_by_nickname(self, n):
        return {"好友甲": "wxid_friend"}.get(n)

    def search_contact(self, kw):
        return []


def _fake_quick_send(text, who, verify=False):
    _sent.append(("text", who, text))
    return {"status": "success", "message": "ok"}


def _fake_quick_send_file(path, who, verify=False):
    _sent.append(("file", who, path))
    return {"status": "success", "message": "ok"}


def _install_wechatauto_stub():
    pkg = types.ModuleType("wechatauto")
    db_mod = types.ModuleType("wechatauto.db")
    db_mod.Listener = _FakeListener
    db_mod.WeChatDB = _FakeWeChatDB
    guia_mod = types.ModuleType("wechatauto.guia")
    guia_mod.quick_send = _fake_quick_send
    guia_mod.quick_send_file = _fake_quick_send_file
    guia_mod.quick_send_image = _fake_quick_send_file
    media_mod = types.ModuleType("wechatauto.media")
    pkg.db = db_mod
    pkg.guia = guia_mod
    pkg.media = media_mod
    sys.modules.setdefault("wechatauto", pkg)
    sys.modules["wechatauto.db"] = db_mod
    sys.modules["wechatauto.guia"] = guia_mod
    sys.modules["wechatauto.media"] = media_mod


_install_wechatauto_stub()

from wechatauto_channels import bridge as bridge_mod  # noqa: E402
from wechatauto_channels.core import ChannelCore  # noqa: E402


def _req(port, method, path, body=None, token="tok-e2e"):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


class TestBridgeE2E(unittest.TestCase):
    _httpd = None
    _thread = None
    port = None

    @classmethod
    def setUpClass(cls):
        core = ChannelCore()
        cls._thread, cls._httpd = bridge_mod.serve_thread(
            "127.0.0.1", 0, "tok-e2e", core)
        cls.port = cls._httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        if cls._httpd:
            cls._httpd.shutdown()
            cls._httpd.server_close()
        bridge_mod._release_instance_lock()

    # ── 鉴权 ────────────────────────────────────────────────────────────
    def test_01_unauthorized(self):
        code, body = _req(self.port, "GET", "/health", token=None)
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))

    def test_02_health(self):
        code, body = _req(self.port, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["wxid"], "wxid_self001")

    # ── 入站：listener 回调 → 事件 → 长轮询 ─────────────────────────────
    def test_03_inbound_event_longpoll(self):
        cb = _listener_cb.get("cb")
        self.assertIsNotNone(cb, "listener 未注册回调")
        # 模拟上游推送一条 DM
        cb({"username": "wxid_friend", "local_id": 42, "sort_seq": 9001,
            "type": "文本", "sender_id": 1, "sender_username": "wxid_friend",
            "create_time": 1700000000, "content": "端到端 hello"}, None)
        code, body = _req(self.port, "GET", "/events?cursor=0&timeout_ms=2000")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["events"]), 1)
        ev = body["events"][0]["event"]
        self.assertEqual(ev["chat_id"], "wxid_friend")
        self.assertEqual(ev["type"], "text")          # 中文类型名已被 canon
        self.assertEqual(ev["text"], "端到端 hello")
        self.assertEqual(ev["id"], "wxid_friend:9001")  # sort_seq 去重键
        self.assertFalse(ev["is_self"])
        self.assertEqual(body["cursor"], body["events"][-1]["seq"])
        # 同一游标再拉 → 空（长轮询不重复投递）
        code, body2 = _req(self.port, "GET",
                           f"/events?cursor={body['cursor']}&timeout_ms=500")
        self.assertEqual(body2["events"], [])

    # ── 出站：resolve → quick_send ──────────────────────────────────────
    def test_04_send_text(self):
        code, body = _req(self.port, "POST", "/send",
                          {"to": "好友甲", "text": "回执测试"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn(("text", "wxid_friend", "回执测试"), _sent)

    def test_05_send_unresolvable(self):
        code, body = _req(self.port, "POST", "/send",
                          {"to": "不存在的人", "text": "x"})
        self.assertEqual(code, 502)
        self.assertFalse(body["ok"])

    def test_06_resolve_endpoint(self):
        code, body = _req(self.port, "GET",
                          "/resolve?handle=" + urllib.parse.quote("好友甲"))
        self.assertEqual(body["username"], "wxid_friend")

    def test_07_unknown_path_404(self):
        code, _ = _req(self.port, "GET", "/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
