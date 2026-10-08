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
