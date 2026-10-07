"""wechatauto_channels.core 离线单测 —— 用 FakeDB 桩掉 wechatauto，不依赖 Windows/微信。

覆盖三条最危险的逻辑：
  1. 「是不是我发的」判定（status==2 正向证据 + XML fromusername + sender_id==2 兜底）
  2. 事件归一化（dm/group、sender 解析、显示名缓存）
  3. split_text 分块 + EventBuffer 长轮询游标
"""

import sqlite3
import sys
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechatauto_channels.core import (  # noqa: E402
    ChannelCore, ChannelEvent, EventBuffer, split_text,
)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """模拟分片连接：rows = [(real_sender_id, status), ...]（按表名区分会话）。"""

    def __init__(self, rows, table):
        self._rows, self._table = rows, table

    def execute(self, sql, params=()):
        assert f'FROM "{self._table}"' in sql
        return FakeCursor([(sid, st) for sid, st in self._rows])


class FakeDB:
    """ChannelCore 用到的最小 DB 面。"""

    wxid = "wxid_self001"
    account = "wxid_self001_ab12"

    def __init__(self, votes_rows=None, names=None, groups=None):
        self._votes_rows = votes_rows or []
        self._names = names or {}
        self._groups = groups or {}

    def get_self_info(self):
        return {"username": self.wxid, "nick_name": "小助手"}

    def _msg_conns(self, user):
        table = "Msg_" + "0" * 31 + "1"
        return [(FakeConn(self._votes_rows, table), table)]

    def get_nickname(self, u):
        return self._names.get(u, u)

    def group_id_to_name(self, u):
        return self._groups.get(u)

    def group_name_to_id(self, n):
        rev = {v: k for k, v in self._groups.items()}
        return rev.get(n)

    def username_by_nickname(self, n):
        rev = {v: k for k, v in self._names.items()}
        return rev.get(n)

    def search_contact(self, kw):
        return [{"username": u} for u, name in self._names.items() if kw in name]

    def get_sessions(self, limit=100):
        return [{"username": u, "unread": 0} for u in self._names]


def make_core(votes=None, **kw):
    core = ChannelCore()
    core.db = FakeDB(votes_rows=votes,
                     names={"wxid_friend": "好友甲"},
                     groups={"111@chatroom": "项目群"})
    core.self_wxid = FakeDB.wxid
    core.self_nick = "小助手"
    return core


def msg(sender_id=1, sender_username="wxid_friend", content="hi", **kw):
    d = dict(local_id=1, type="text", sender_id=sender_id,
             sender_username=sender_username, create_time=1700000000,
             content=content, sort_seq=100)
    d.update(kw)
    return d


class TestSelfDetection(unittest.TestCase):
    """status==2 正向证据优先；sender_id==2 只在完全没有证据时兜底。"""

    def test_status_votes_identify_self(self):
        # 分片里 sid=7 的消息全是 status==2 → sid 7 是「我」
        core = make_core(votes=[(7, 2), (7, 2), (1, 0), (1, 0)])
        self.assertTrue(core._is_self("wxid_friend", msg(sender_id=7, sender_username="")))
        self.assertFalse(core._is_self("wxid_friend", msg(sender_id=1)))

    def test_sender_id_2_not_self_when_evidence_exists(self):
        # 有正向证据时，sid==2 不自动算自己（换分片后 2 可能是对方）
        core = make_core(votes=[(7, 2), (7, 2), (2, 0), (2, 0)])
        self.assertFalse(core._is_self("wxid_friend", msg(sender_id=2, sender_username="")))

    def test_sender_id_2_fallback_when_no_evidence(self):
        core = make_core(votes=[])
        self.assertTrue(core._is_self("wxid_friend", msg(sender_id=2, sender_username="")))

    def test_xml_fromusername_is_self(self):
        core = make_core(votes=[])
        m = msg(sender_id=9, sender_username="",
                content='<emoji fromusername="wxid_self001" type="1"/>')
        self.assertTrue(core._is_self("wxid_friend", m))

    def test_sender_username_equals_wxid_is_self(self):
        core = make_core(votes=[(3, 0), (3, 0)])
        m = msg(sender_id=3, sender_username="wxid_self001")
        self.assertTrue(core._is_self("wxid_friend", m))


class TestNormalize(unittest.TestCase):
    def test_dm_event(self):
        core = make_core(votes=[(1, 0)])
        ev = core._normalize("wxid_friend", msg())
        self.assertIsInstance(ev, ChannelEvent)
        self.assertEqual(ev.chat_type, "dm")
        self.assertEqual(ev.chat_id, "wxid_friend")
        self.assertEqual(ev.sender_id, "wxid_friend")
        self.assertEqual(ev.sender_name, "好友甲")
        self.assertFalse(ev.is_self)
        self.assertEqual(ev.id, "wxid_friend:1")

    def test_group_event_sender_resolution(self):
        core = make_core(votes=[(1, 0)])
        ev = core._normalize("111@chatroom",
                             msg(sender_username="wxid_bob", sender_id=5))
        self.assertEqual(ev.chat_type, "group")
        self.assertEqual(ev.chat_name, "项目群")
        self.assertEqual(ev.sender_id, "wxid_bob")

    def test_self_event(self):
        core = make_core(votes=[(2, 2), (1, 0)])
        ev = core._normalize("wxid_friend", msg(sender_id=2, sender_username=""))
        self.assertTrue(ev.is_self)
        self.assertEqual(ev.sender_name, "小助手")

    def test_non_text_placeholder(self):
        core = make_core(votes=[(1, 0)])
        ev = core._normalize("wxid_friend", msg(type="image", content=""))
        self.assertEqual(ev.text, "[image]")


class TestSplitText(unittest.TestCase):
    def test_short(self):
        self.assertEqual(split_text("abc"), ["abc"])
        self.assertEqual(split_text(""), [])

    def test_paragraph_boundary(self):
        text = "甲" * 3000 + "\n\n" + "乙" * 2000
        chunks = split_text(text, 4000)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("甲"))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))


class TestEventBuffer(unittest.TestCase):
    def test_cursor_and_longpoll(self):
        buf = EventBuffer()
        ev = ChannelEvent(id="a:1", chat_id="a", chat_type="dm", chat_name="A",
                          sender_id="a", sender_name="A", is_self=False,
                          type="text", text="hi", timestamp=1.0,
                          local_id=1, sort_seq=1)
        buf.push(ev)
        r = buf.wait(0, 0.01)
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["cursor"], 1)
        # 已读过的游标 → 长轮询超时返回空
        t0 = time.time()
        r2 = buf.wait(1, 0.05)
        self.assertEqual(r2["events"], [])
        self.assertGreaterEqual(time.time() - t0, 0.04)
        # 推送唤醒等待者
        def later():
            time.sleep(0.02)
            buf.push(ev)
        threading.Thread(target=later, daemon=True).start()
        r3 = buf.wait(1, 1.0)
        self.assertEqual(len(r3["events"]), 1)


class TestResolveTarget(unittest.TestCase):
    def test_wxid_passthrough(self):
        core = make_core()
        self.assertEqual(core.resolve_target("wxid_abc"), "wxid_abc")
        self.assertEqual(core.resolve_target("111@chatroom"), "111@chatroom")
        self.assertEqual(core.resolve_target("filehelper"), "filehelper")

    def test_nickname_and_group(self):
        core = make_core()
        self.assertEqual(core.resolve_target("好友甲"), "wxid_friend")
        self.assertEqual(core.resolve_target("项目群"), "111@chatroom")
        self.assertIsNone(core.resolve_target("不存在的人"))


if __name__ == "__main__":
    unittest.main()
