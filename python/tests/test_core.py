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

    def test_sender_id_1_fallback_when_no_evidence(self):
        # 生产实测（stardome）：4.1.13.x 出站行 sender_id 用过 1
        core = make_core(votes=[])
        self.assertTrue(core._is_self("wxid_friend", msg(sender_id=1, sender_username="")))

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
        # 去重键用 sort_seq（跨分片单调）；local_id 跨分片重复不可作键
        self.assertEqual(ev.id, "wxid_friend:100")

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

    def test_chinese_type_canonicalized(self):
        # 上游 MSG_TYPE_NAMES 给中文显示名；canon 化后下游才认得 image/file
        core = make_core(votes=[(1, 0)])
        ev = core._normalize("wxid_friend", msg(type="图片", content=""))
        self.assertEqual(ev.type, "image")
        self.assertEqual(ev.text, "[image]")
        ev2 = core._normalize("wxid_friend", msg(type="文件/链接/卡片", content=""))
        self.assertEqual(ev2.type, "file")
        ev3 = core._normalize("wxid_friend", msg(type="文本", content="hi"))
        self.assertEqual(ev3.type, "text")

    def test_group_sender_wxid_prefix_recovery(self):
        # 群聊 content 前缀 "wxid_…:\n" 是真实发言人权威源（sender_id 只是数字索引）
        core = make_core(votes=[(3, 0)])
        ev = core._normalize(
            "111@chatroom",
            msg(sender_id=3, sender_username="", content="wxid_bob:\n早上好"))
        self.assertEqual(ev.sender_id, "wxid_bob")
        self.assertEqual(ev.text, "早上好")

    def test_event_carries_at_and_quoted(self):
        core = make_core(votes=[(1, 0)])
        ev = core._normalize("111@chatroom", msg(
            sender_username="wxid_bob",
            at_usernames=["wxid_self001"],
            quoted={"sender": "wxid_self001", "text": "原话"}))
        self.assertEqual(ev.at_usernames, ["wxid_self001"])
        self.assertEqual(ev.quoted["sender"], "wxid_self001")


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


class TestAtUsernamesParsing(unittest.TestCase):
    """atuserlist XML 投影：list=权威，None=不可判定。"""

    def test_list_extracted(self):
        from wechatauto_channels.core import _parse_at_usernames
        src = ('<msgsource><atuserlist><![CDATA[wxid_self001,wxid_bob]]>'
               '</atuserlist></msgsource>').encode()
        self.assertEqual(_parse_at_usernames(src), ["wxid_self001", "wxid_bob"])

    def test_no_source_is_none(self):
        from wechatauto_channels.core import _parse_at_usernames
        self.assertIsNone(_parse_at_usernames(None))

    def test_source_without_atlist_is_empty_list(self):
        from wechatauto_channels.core import _parse_at_usernames
        self.assertEqual(_parse_at_usernames(b"<msgsource/>"), [])


class TestQuotedParsing(unittest.TestCase):
    """type-49 引用消息：<refermsg> + 外层 <title>。"""

    APPMSG = (
        '<appmsg><title>同意你的看法</title><type>57</type>'
        '<refermsg><fromusr>111@chatroom</fromusr>'
        '<chatusr>wxid_self001</chatusr>'
        '<displayname>小助手</displayname>'
        '<content>原消息内容</content></refermsg></appmsg>')

    def test_group_quote_sender_is_chatusr(self):
        from wechatauto_channels.core import _parse_quoted
        r = {"message_content": self.APPMSG.encode(), "compress_content": b""}
        parsed = _parse_quoted(r)
        self.assertIsNotNone(parsed)
        # 群聊：fromusr 是群 id，真实被引人在 chatusr
        self.assertEqual(parsed["quoted"]["sender"], "wxid_self001")
        self.assertEqual(parsed["reply_text"], "同意你的看法")

    def test_dm_quote_sender_is_fromusr(self):
        from wechatauto_channels.core import _parse_quoted
        xml = ('<appmsg><title>收到</title>'
               '<refermsg><fromusr>wxid_self001</fromusr>'
               '<content>原文</content></refermsg></appmsg>').encode()
        parsed = _parse_quoted({"message_content": xml})
        self.assertEqual(parsed["quoted"]["sender"], "wxid_self001")

    def test_non_quote_is_none(self):
        from wechatauto_channels.core import _parse_quoted
        self.assertIsNone(_parse_quoted({"message_content": b"plain text"}))


class TestRowProjectionPatch(unittest.TestCase):
    """_install_row_projection：幂等 + 投影 at_usernames/quoted + 引用还原。"""

    def _fake_cls(self):
        class FakeWeChatDB:
            def _msg_row_to_dict(self, r):
                return {"local_id": r["local_id"], "type": "文本",
                        "content": r.get("message_content", ""),
                        "sender_id": r.get("real_sender_id", 1),
                        "sender_username": "", "sort_seq": 1}
        return FakeWeChatDB

    def test_projection_installed_and_idempotent(self):
        from wechatauto_channels.core import _install_row_projection
        cls = self._fake_cls()
        self.assertTrue(_install_row_projection(cls))
        self.assertFalse(_install_row_projection(cls))  # 幂等
        row = {"local_id": 1, "real_sender_id": 1,
               "source": b"<msgsource><atuserlist>wxid_a</atuserlist></msgsource>",
               "message_content": "hi", "compress_content": b""}
        out = cls()._msg_row_to_dict(row)
        self.assertEqual(out["at_usernames"], ["wxid_a"])
        self.assertIsNone(out["quoted"])

    def test_quote_rewritten_to_text(self):
        from wechatauto_channels.core import _install_row_projection
        cls = self._fake_cls()
        _install_row_projection(cls)
        row = {"local_id": 2, "real_sender_id": 5, "source": b"",
               "message_content": TestQuotedParsing.APPMSG.encode(),
               "compress_content": b""}
        out = cls()._msg_row_to_dict(row)
        # 引用回复不再走 file 路由：type=引用 + content=外层 title 正文
        self.assertEqual(out["type"], "引用")
        self.assertEqual(out["content"], "同意你的看法")
        self.assertEqual(out["quoted"]["sender"], "wxid_self001")


class TestRhythmDefault(unittest.TestCase):
    def test_rhythm_defaults_off(self):
        # 上游 >=1.2.3 默认 natural 会卡死 agent 分段回复；core import 即落 off
        import os
        self.assertEqual(os.environ.get("WECHATAUTO_RHYTHM"), "off")


if __name__ == "__main__":
    unittest.main()
