"""bridge 端到端测试 —— stub 掉 wechatauto（Listener/WeChatDB/guia），
真起 HTTP server 走完整链路：Listener 回调 → _normalize → EventBuffer →
GET /events 长轮询；POST /send → resolve_target → quick_send。

无 Windows/微信依赖；验证的是「HTTP 契约 + 事件管线 + 鉴权」这一段。
"""

import json
import sys
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

    def get_messages(self, username, limit=10):
        return [{"local_id": 7, "sort_seq": 9000, "type": 1,
                 "sender_username": "wxid_friend",
                 "create_time": time.time() - 5, "content": "上文一条"}]

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


class _FakeGUI:
    """core 现在持有共享 WeChatGUI 实例走 send_msg/send_file，
    不再是模块级 quick_send——stub 类形态保持一致。"""

    def __init__(self):
        self._last_input_box = None

    def send_msg(self, text, who=None, verify=False):
        _sent.append(("text", who, text))
        return {"status": "success", "message": "ok"}

    def send_file(self, path, who=None, verify=False):
        _sent.append(("file", who, path))
        return {"status": "success", "message": "ok"}

    send_image = send_file


def _install_wechatauto_stub():
    pkg = types.ModuleType("wechatauto")
    db_mod = types.ModuleType("wechatauto.db")
    db_mod.Listener = _FakeListener
    db_mod.WeChatDB = _FakeWeChatDB
    guia_mod = types.ModuleType("wechatauto.guia")
    guia_mod.WeChatGUI = _FakeGUI
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

    # ── 出站：resolve → 显示名 → gui.send_msg ─────────────────────────
    def test_04_send_text(self):
        code, body = _req(self.port, "POST", "/send",
                          {"to": "好友甲", "text": "回执测试"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        # GUI 层收到的是显示名（搜索框不认 wxid/@chatroom id），
        # 回包 results 里的 to 仍是会话 username
        self.assertIn(("text", "好友甲", "回执测试"), _sent)
        self.assertEqual(body["results"][0].get("to"), "wxid_friend")

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

    # ── listen 出口：/context 近期上文查询 ───────────────────────────
    def test_08_context_endpoint(self):
        code, body = _req(self.port, "GET",
                          "/context?chat=" + urllib.parse.quote("好友甲"))
        self.assertEqual(code, 200)
        self.assertIn("好友甲: 上文一条", body["lines"])
        self.assertEqual(body["media"], [])

    def test_09_context_missing_chat_400(self):
        code, _ = _req(self.port, "GET", "/context")
        self.assertEqual(code, 400)

    # ── 出站幂等：client_msg_id 去重窗口 ──────────────────────────────
    def test_10_send_dedup_client_msg_id(self):
        _sent.clear()
        body = {"to": "好友甲", "text": "幂等测试",
                "client_msg_id": "mid-e2e-dedup"}
        code, r1 = _req(self.port, "POST", "/send", body)
        self.assertEqual(code, 200)
        self.assertTrue(r1["ok"])
        code, r2 = _req(self.port, "POST", "/send", body)
        self.assertEqual(code, 200)
        self.assertTrue(r2["deduplicated"])
        # 第二次命中缓存——底层 send_msg 只被真正调用一次
        self.assertEqual(_sent, [("text", "好友甲", "幂等测试")])

    def test_11_send_failure_not_deduped(self):
        # 失败结果不缓存：重试必须真发，否则一次 502 永久吞掉该消息
        body = {"to": "不存在的人", "text": "x", "client_msg_id": "mid-fail"}
        code1, _ = _req(self.port, "POST", "/send", body)
        self.assertEqual(code1, 502)
        code2, r2 = _req(self.port, "POST", "/send", body)
        self.assertEqual(code2, 502)
        self.assertNotIn("deduplicated", r2)


class TestBridgeDedupUnit(unittest.TestCase):
    """幂等表纯逻辑——不起 server、不占实例锁，任何机器都能跑。"""

    def _state(self):
        return bridge_mod.BridgeState(core=None, token="t")

    def test_lookup_hit_returns_cached(self):
        st = self._state()
        st.dedup_store("m1", 200, {"ok": True, "results": []})
        self.assertEqual(st.dedup_lookup("m1"), (200, {"ok": True,
                                                      "results": []}))

    def test_lookup_miss_and_expiry(self):
        st = self._state()
        self.assertIsNone(st.dedup_lookup("nope"))
        st.dedup_store("old", 200, {"ok": True})
        # 手动把记录时间戳推到 TTL 之外
        mid, ts, payload = "old", time.time() - 700, {"ok": True}
        st._dedup[mid] = (ts, 200, payload)
        self.assertIsNone(st.dedup_lookup("old"))

    def test_eviction_caps_size(self):
        st = self._state()
        for i in range(bridge_mod._DEDUP_MAX + 10):
            st.dedup_store(f"m{i}", 200, {"ok": True})
        self.assertLessEqual(len(st._dedup), bridge_mod._DEDUP_MAX)


if __name__ == "__main__":
    unittest.main()
