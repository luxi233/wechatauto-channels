"""manager_ops 离线单测 —— JWT、配置读写、Hermes 开关编辑；不碰真进程/网络。"""

import base64
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechatauto_channels import manager_ops as ops  # noqa: E402


class TestMintJwt(unittest.TestCase):
    def test_hs256_roundtrip(self):
        secret = "s3cret"
        token = ops.mint_jwt(secret, sub="7", uname="admin", role="admin")
        head, body, sig = token.split(".")
        signing = f"{head}.{body}".encode()
        expect = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), signing, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(sig, expect)
        payload = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.assertEqual(payload["sub"], "7")
        self.assertEqual(payload["role"], "admin")

    def test_bytes_secret(self):
        token = ops.mint_jwt(b"k" * 32, sub="1", uname="a", role="admin")
        self.assertEqual(len(token.split(".")), 3)


class TestHermesToggle(unittest.TestCase):
    YAML = (
        "plugins:\n"
        "  enabled:\n"
        "    - weixin\n"
        "    - wechatauto\n"
        "platforms:\n"
        "  weixin:\n"
        "    enabled: true\n"
        "  wechatauto:\n"
        "    enabled: false\n"
        "  discord:\n"
        "    enabled: true\n"
    )

    def _mk(self, tmp):
        home = Path(tmp) / "hermes"
        home.mkdir()
        (home / "config.yaml").write_text(self.YAML, encoding="utf-8")
        cfg = ops.DEFAULT_CFG | {"hermes_home": str(home)}
        import copy
        return copy.deepcopy(cfg), home

    def test_enable(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        ok, _ = ops.hermes_set_platform_enabled(cfg, True)
        self.assertTrue(ok)
        text = (home / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("wechatauto:\n    enabled: true", text)
        # 不动其它平台
        self.assertIn("discord:\n    enabled: true", text)
        self.assertIn("weixin:\n    enabled: true", text)

    def test_status_reads_flag(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        st = ops.hermes_status(cfg)
        self.assertTrue(st["installed"])
        self.assertTrue(st["whitelisted"])
        self.assertFalse(st["platform_enabled"])

    def test_missing_platform_key(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text("platforms:\n  weixin: {}\n",
                                          encoding="utf-8")
        cfg = ops.DEFAULT_CFG | {"hermes_home": str(home)}
        import copy
        cfg = copy.deepcopy(cfg)
        ok, msg = ops.hermes_set_platform_enabled(cfg, True)
        self.assertFalse(ok)
        self.assertIn("没有", msg)


class TestAutoOpenDefaults(unittest.TestCase):
    """octop_auto_open_defaults：新资源自动补默认声明，幂等。"""

    def _mk_octop(self, tmp):
        import sqlite3
        home = Path(tmp) / ".octop"
        home.mkdir()
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.executescript("""
            CREATE TABLE knowledge_bases (
                id INTEGER PRIMARY KEY, knowledge_base_id TEXT,
                owner_user_id INT, name TEXT, default_open INT,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE connectors (
                id INTEGER PRIMARY KEY, display_name TEXT,
                config_json TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE skill_packages (
                id INTEGER PRIMARY KEY, skill_package_id TEXT, name TEXT);
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY, skill_package_ids TEXT,
                updated_at TEXT);
            INSERT INTO agents (agent_id, skill_package_ids)
                VALUES ('main', NULL);
        """)
        conn.commit()
        conn.close()
        import copy
        return copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)}), home

    def _db(self, home):
        import sqlite3
        c = sqlite3.connect(str(home / "octop.db"))
        c.row_factory = sqlite3.Row
        return c

    def test_flips_all_resource_kinds(self):
        cfg, home = self._mk_octop(tempfile.mkdtemp())
        conn = self._db(home)
        conn.execute(
            "INSERT INTO knowledge_bases (id, knowledge_base_id,"
            " owner_user_id, name, default_open) VALUES (1,'K1',1,'泵库',0)")
        conn.execute(
            "INSERT INTO connectors (id, display_name, config_json)"
            " VALUES (1,'邮箱',NULL)")
        conn.execute(
            "INSERT INTO skill_packages (id, skill_package_id, name)"
            " VALUES (1,'P1','爬虫包')")
        conn.commit(); conn.close()

        flipped = ops.octop_auto_open_defaults(cfg)
        self.assertEqual(len(flipped), 3)

        conn = self._db(home)
        self.assertEqual(conn.execute(
            "SELECT default_open FROM knowledge_bases").fetchone()[0], 1)
        self.assertTrue(json.loads(conn.execute(
            "SELECT config_json FROM connectors").fetchone()[0])
            ["default_open"])
        self.assertEqual(json.loads(conn.execute(
            "SELECT skill_package_ids FROM agents WHERE agent_id='main'"
        ).fetchone()[0]), ["P1"])
        conn.close()

        # 幂等：再跑一次零翻动
        self.assertEqual(ops.octop_auto_open_defaults(cfg), [])

    def test_preserves_existing_bindings(self):
        cfg, home = self._mk_octop(tempfile.mkdtemp())
        conn = self._db(home)
        conn.execute(
            "UPDATE agents SET skill_package_ids='[\"OLD\"]'")
        conn.execute(
            "INSERT INTO skill_packages (id, skill_package_id, name)"
            " VALUES (1,'NEW','新包')")
        conn.commit(); conn.close()

        flipped = ops.octop_auto_open_defaults(cfg)
        self.assertEqual(flipped, ["skill:新包"])
        conn = self._db(home)
        self.assertEqual(sorted(json.loads(conn.execute(
            "SELECT skill_package_ids FROM agents").fetchone()[0])),
            ["NEW", "OLD"])
        conn.close()

    def test_no_db_returns_empty(self):
        import copy
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": "/nonexistent"})
        # octop_home 不存在时回退 ~/.octop 探测；本机有真 octop.db 时
        # 该用例会翻动真实库——跳过避免误伤。
        if (Path.home() / ".octop" / "octop.db").exists():
            self.skipTest("real octop.db present")
        self.assertEqual(ops.octop_auto_open_defaults(cfg), [])


class TestConfigRoundTrip(unittest.TestCase):
    def test_defaults(self):
        cfg = ops.load_config()
        self.assertEqual(cfg["bridge"]["port"], 18765)
        self.assertTrue(cfg["supervise"]["enabled"])

    def test_deep_merge(self):
        MANAGER_DIR = ops.MANAGER_DIR
        MANAGER_DIR.mkdir(parents=True, exist_ok=True)
        backup = ops.MANAGER_CFG.read_bytes() if ops.MANAGER_CFG.exists() else None
        try:
            ops.MANAGER_CFG.write_text(
                json.dumps({"bridge": {"port": 9999}}), encoding="utf-8")
            cfg = ops.load_config()
            self.assertEqual(cfg["bridge"]["port"], 9999)
            self.assertEqual(cfg["bridge"]["host"], "127.0.0.1")
        finally:
            if backup is None:
                ops.MANAGER_CFG.unlink(missing_ok=True)
            else:
                ops.MANAGER_CFG.write_bytes(backup)


class TestOpenClawDetect(unittest.TestCase):
    def test_returns_shape(self):
        st = ops.openclaw_status(ops.load_config())
        self.assertIn("installed", st)
        self.assertIn("plugin", st)


if __name__ == "__main__":
    unittest.main()
