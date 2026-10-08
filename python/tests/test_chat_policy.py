"""chat_policy 纯逻辑单测：模式解析、id/名匹配、热加载、fail-compatible。"""

import json
import os
import tempfile
import time
import unittest

from wechatauto_channels.chat_policy import ChatPolicy, load_policy


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class TestChatPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "chat-policy.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_file_returns_default(self):
        p = ChatPolicy(self.path)
        self.assertEqual(p.mode_for("111@chatroom", "项目群"), "at")
        self.assertEqual(p.mode_for("x", "y", "reply"), "reply")

    def test_rules_match_by_name_and_id(self):
        _write(self.path, {"default": "at", "rules": [
            {"chat": "家庭群", "mode": "listen"},
            {"chat": "111@chatroom", "mode": "reply"},
        ]})
        p = ChatPolicy(self.path)
        # 显示名命中
        self.assertEqual(p.mode_for("999@chatroom", "家庭群"), "listen")
        # chat_id 命中
        self.assertEqual(p.mode_for("111@chatroom", "别的名"), "reply")
        # 未命中回落文件 default
        self.assertEqual(p.mode_for("222@chatroom", "别的群"), "at")

    def test_file_default_overrides_env_default(self):
        _write(self.path, {"default": "reply", "rules": []})
        p = ChatPolicy(self.path)
        # 文件存在时 default 由文件说了算，不再吃 env 语义
        self.assertEqual(p.mode_for("x@chatroom", "群"), "reply")

    def test_first_rule_wins(self):
        _write(self.path, {"default": "at", "rules": [
            {"chat": "群A", "mode": "listen"},
            {"chat": "群A", "mode": "reply"},
        ]})
        self.assertEqual(ChatPolicy(self.path).mode_for("x", "群A"), "listen")

    def test_invalid_mode_rule_skipped_not_fatal(self):
        _write(self.path, {"default": "at", "rules": [
            {"chat": "群A", "mode": "bogus"},
            {"chat": "群A", "mode": "listen"},
            "garbage",
        ]})
        self.assertEqual(ChatPolicy(self.path).mode_for("x", "群A"), "listen")

    def test_hot_reload(self):
        _write(self.path, {"default": "at", "rules": [
            {"chat": "群A", "mode": "at"}]})
        p = ChatPolicy(self.path)
        self.assertEqual(p.mode_for("x", "群A"), "at")
        # 保证 mtime 变化（Windows 文件时间粒度）
        time.sleep(0.05)
        _write(self.path, {"default": "at", "rules": [
            {"chat": "群A", "mode": "listen"}]})
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        self.assertEqual(p.mode_for("x", "群A"), "listen")

    def test_corrupt_file_keeps_last_good(self):
        _write(self.path, {"default": "at", "rules": [
            {"chat": "群A", "mode": "listen"}]})
        p = ChatPolicy(self.path)
        self.assertEqual(p.mode_for("x", "群A"), "listen")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        # 坏文件不改已生效配置
        self.assertEqual(p.mode_for("x", "群A"), "listen")

    def test_corrupt_first_load_falls_back_to_env_default(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("}{")
        p = ChatPolicy(self.path)
        self.assertEqual(p.mode_for("x", "群A", "reply"), "reply")

    def test_load_policy_blank_returns_none(self):
        self.assertIsNone(load_policy(""))
        self.assertIsNone(load_policy("   "))
        self.assertIsNotNone(load_policy(self.path))


if __name__ == "__main__":
    unittest.main()
