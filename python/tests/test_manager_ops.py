"""manager_ops 离线单测 —— JWT、配置读写、Hermes 开关编辑；不碰真进程/网络。"""

import base64
import copy
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# 必须在 import manager_ops 之前设置：本模块所有路径（manager.json、
# manager.log、adapter 金本）都经 WECHATAUTO_HOME/WECHATAUTO_LOG_DIR
# 动态解析。不隔离的话，单测会把 hermes.enabled/group_allow 之类的
# 假操作日志写进真实 manager.log（生产已踩过），甚至摸到真 octop.db。
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="wca-test-"))
os.environ["WECHATAUTO_HOME"] = str(_TMP_ROOT / ".wechatauto")
os.environ["WECHATAUTO_LOG_DIR"] = str(_TMP_ROOT / "logs")
os.environ["WECHATAUTO_OCTOP_HOME"] = str(_TMP_ROOT / ".octop")

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
        # octop_home 不存在 → 回退 WECHATAUTO_OCTOP_HOME（模块级已指向
        # 不存在的 tmp 路径）→ octop_home 返回 None → 空结果。
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": "/nonexistent"})
        self.assertEqual(ops.octop_auto_open_defaults(cfg), [])


class TestGroupAllowlist(unittest.TestCase):
    """群白名单：GUI 视图的群来自桥 /chats，名单写 config_json.group_allow。"""

    def _mk_octop(self, tmp, config=None):
        home = Path(tmp) / ".octop"
        home.mkdir()
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.executescript("""
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY, channel_id TEXT, kind TEXT,
                name TEXT, enabled INT, config_json TEXT,
                created_at TEXT, updated_at TEXT);
        """)
        conn.execute(
            "INSERT INTO channels (channel_id, kind, name, enabled,"
            " config_json) VALUES ('C1','wechatauto','微',1,?)",
            (json.dumps(config or {}, ensure_ascii=False),))
        conn.commit()
        conn.close()
        return copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)})

    @staticmethod
    def _fake_chats(chats):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"chats": chats}).encode()

        return mock.patch.object(
            ops.urllib.request, "urlopen", return_value=_Resp())

    def test_view_lists_groups_with_allow_state(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(),
            config={"group_allow": ["111@chatroom"]})
        chats = [
            {"id": "111@chatroom", "type": "group", "name": "有点东西"},
            {"id": "222@chatroom", "type": "group", "name": "另一个群"},
            {"id": "wxid_peer", "type": "dm", "name": "阿巴阿巴"},
        ]
        with self._fake_chats(chats):
            st = ops.octop_group_allowlist(cfg)
        self.assertTrue(st["enforced"])
        self.assertEqual([g["id"] for g in st["groups"]],
                         ["111@chatroom", "222@chatroom"])
        self.assertTrue(st["groups"][0]["allowed"])
        self.assertFalse(st["groups"][1]["allowed"])

    def test_view_empty_allow_not_enforced(self):
        cfg = self._mk_octop(tempfile.mkdtemp())
        with self._fake_chats(
                [{"id": "g@chatroom", "type": "group", "name": "G"}]):
            st = ops.octop_group_allowlist(cfg)
        self.assertFalse(st["enforced"])
        self.assertFalse(st["groups"][0]["allowed"])

    def test_view_keeps_stale_ids(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"group_allow": ["gone@chatroom"]})
        with self._fake_chats([]):
            st = ops.octop_group_allowlist(cfg)
        self.assertEqual(len(st["groups"]), 1)
        self.assertTrue(st["groups"][0]["stale"])
        self.assertTrue(st["groups"][0]["allowed"])

    def test_bridge_down_keeps_db_state(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"group_allow": ["111@chatroom"]})
        with mock.patch.object(ops.urllib.request, "urlopen",
                               side_effect=OSError("down")):
            st = ops.octop_group_allowlist(cfg)
        self.assertTrue(st["error"])
        self.assertTrue(st["enforced"])
        self.assertTrue(st["groups"][0]["stale"])

    def test_set_via_api_patches_full_config(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"token": "t", "other": 1})
        calls = []

        def fake_api(_cfg, method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return 200, {"config": {"token": "t", "other": 1}}
            return 200, {}

        with mock.patch.object(ops, "octop_api", side_effect=fake_api):
            ok, msg = ops.octop_group_allowlist_set(cfg, ["111@chatroom"])
        self.assertTrue(ok, msg)
        patch = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(len(patch), 1)
        sent = patch[0][2]["config"]
        self.assertEqual(sent["group_allow"], ["111@chatroom"])
        self.assertEqual(sent["token"], "t")       # 合并而非覆盖
        self.assertEqual(sent["other"], 1)

    def test_set_empty_disables_filter(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"group_allow": ["x@chatroom"]})

        def fake_api(_cfg, method, path, body=None):
            if method == "GET":
                return 200, {"config": {"group_allow": ["x@chatroom"]}}
            return 200, {}

        with mock.patch.object(ops, "octop_api", side_effect=fake_api):
            ok, _ = ops.octop_group_allowlist_set(cfg, [])
        self.assertTrue(ok)

    def test_set_db_fallback_when_backend_down(self):
        cfg = self._mk_octop(tempfile.mkdtemp())
        with mock.patch.object(ops, "octop_api", return_value=(-1, "down")):
            ok, msg = ops.octop_group_allowlist_set(cfg, ["111@chatroom"])
        self.assertTrue(ok, msg)
        conn = sqlite3.connect(str(Path(cfg["octop_home"]) / "octop.db"))
        cj = json.loads(conn.execute(
            "SELECT config_json FROM channels").fetchone()[0])
        conn.close()
        self.assertEqual(cj["group_allow"], ["111@chatroom"])

    def test_set_persists_desired_allowlist(self):
        # 期望值落 manager.json——dashboard 抹掉渠道字段后靠它自愈
        cfg = self._mk_octop(tempfile.mkdtemp())

        def fake_api(_c, method, _p, body=None):
            return (200, {"config": {}}) if method == "GET" else (200, {})

        with mock.patch.object(ops, "octop_api", side_effect=fake_api):
            ok, _ = ops.octop_group_allowlist_set(cfg, ["111@chatroom"])
        self.assertTrue(ok)
        disk = json.loads(ops.manager_cfg_path().read_text("utf-8"))
        self.assertEqual(disk["octop_allowlist"], ["111@chatroom"])

    def test_guard_no_drift_noop(self):
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"group_allow": ["111@chatroom"]})
        cfg["octop_allowlist"] = ["111@chatroom"]
        with mock.patch.object(ops, "octop_api",
                               side_effect=AssertionError("不该调用")):
            self.assertIsNone(ops.octop_allowlist_guard(cfg))

    def test_guard_restores_wiped_field(self):
        # 渠道 config 里的 group_allow 被整段替换丢掉 → 从期望值重放
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"token": "t"})  # 无 group_allow
        cfg["octop_allowlist"] = ["111@chatroom"]
        calls = []

        def fake_api(_c, method, _p, body=None):
            calls.append(method)
            return (200, {"config": {"token": "t"}}) if method == "GET" \
                else (200, {})

        with mock.patch.object(ops, "octop_api", side_effect=fake_api):
            note = ops.octop_allowlist_guard(cfg)
        self.assertIsNotNone(note)
        self.assertIn("PATCH", calls)

    def test_guard_bootstraps_existing_allowlist(self):
        # 老部署：渠道有 group_allow 但 manager.json 没期望值 → 领养不修复
        cfg = self._mk_octop(
            tempfile.mkdtemp(), config={"group_allow": ["g@chatroom"]})
        with mock.patch.object(ops, "octop_api",
                               side_effect=AssertionError("不该调用")):
            self.assertIsNone(ops.octop_allowlist_guard(cfg))
        self.assertEqual(cfg["octop_allowlist"], ["g@chatroom"])


class TestSendFilePatch(unittest.TestCase):
    """send_file_to_user 越界补丁：升级覆盖后 watchdog 应能重打。"""

    def _mk(self, tmp, body=None):
        home = Path(tmp) / "octop"
        target = (home / "portable" / "packages" / "octop_harness"
                  / "builtin" / "tools" / "send_file.py")
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)})
        if body is not None:
            target.parent.mkdir(parents=True)
            target.write_text(body, encoding="utf-8")
        return cfg, target

    def test_applies_patch_when_missing(self):
        cfg, target = self._mk(
            tempfile.mkdtemp(),
            "from langchain_core.tools import ToolException, tool\n"
            "        local = workspace.materialize_local(file_path)\n")
        self.assertIsNotNone(ops.octop_send_file_patch(cfg))
        text = target.read_text(encoding="utf-8")
        self.assertIn("PATCH(local)", text)
        self.assertIn("raise ToolException(", text)
        self.assertIn("workspace.materialize_local", text)

    def test_idempotent_when_marker_present(self):
        cfg, target = self._mk(
            tempfile.mkdtemp(), "        local = "
            "workspace.materialize_local(file_path)\n")
        ops.octop_send_file_patch(cfg)
        before = target.read_text(encoding="utf-8")
        self.assertIsNone(ops.octop_send_file_patch(cfg))
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_anchor_mismatch_leaves_file_untouched(self):
        cfg, target = self._mk(tempfile.mkdtemp(), "# upstream rewrote\n")
        self.assertIsNone(ops.octop_send_file_patch(cfg))
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "# upstream rewrote\n")

    def test_missing_file_returns_none(self):
        cfg, _ = self._mk(tempfile.mkdtemp(), body=None)
        self.assertIsNone(ops.octop_send_file_patch(cfg))

    _DOC_ANCHOR = ('        """Send a file to the user through the '
                   'chat channel.\n')

    def test_doc_patch_applies_group_semantics(self):
        cfg, target = self._mk(tempfile.mkdtemp(), self._DOC_ANCHOR)
        self.assertIsNotNone(ops.octop_send_file_patch(cfg))
        text = target.read_text(encoding="utf-8")
        self.assertIn("current chat", text)
        self.assertIn("group chats alike", text)

    def test_doc_patch_independent_of_boundary_patch(self):
        # 越界补丁已打（有 marker）但 docstring 未修 → 仍应补 docstring
        cfg, target = self._mk(
            tempfile.mkdtemp(),
            "# PATCH(local)\n" + self._DOC_ANCHOR)
        self.assertIsNotNone(ops.octop_send_file_patch(cfg))
        self.assertIn("current chat",
                      target.read_text(encoding="utf-8"))

    def test_doc_patch_idempotent(self):
        cfg, target = self._mk(tempfile.mkdtemp(), self._DOC_ANCHOR)
        ops.octop_send_file_patch(cfg)
        before = target.read_text(encoding="utf-8")
        self.assertIsNone(ops.octop_send_file_patch(cfg))
        self.assertEqual(target.read_text(encoding="utf-8"), before)


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


class TestOctopFrontendPatch(unittest.TestCase):
    """dashboard index.*.js 的 wechatauto 渠道卡片补丁：锚点插入 + 幂等。"""

    JS = (
        'const CHANNEL_KEYS=["weixin","mqtt"],'
        'CHANNEL_LABEL_KEYS={weixin:"channels.label_weixin"},'
        'CHANNEL_LABELS={weixin:"微信"},'
        'CHANNEL_ICONS={weixin:weixinIcon},'
        'CHANNEL_URLS={weixin:"https://x"},'
        'F={xiaoyi:[{name:"ak"}],mqtt:[{name:"host",label:"Broker"}]};'
    )

    def _mk(self, tmp, js=None):
        home = Path(tmp) / ".octop"
        assets = (home / "portable" / "packages" / "octop" / "dashboard"
                  / "assets")
        assets.mkdir(parents=True)
        target = assets / "index.abc.js"
        target.write_text(js if js is not None else self.JS,
                          encoding="utf-8")
        return copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)}), target

    def test_patches_all_anchors(self):
        cfg, js_path = self._mk(tempfile.mkdtemp())
        ok, msg = ops.octop_patch_frontend(cfg)
        self.assertTrue(ok, msg)
        js = js_path.read_text(encoding="utf-8")
        self.assertIn('"wechatauto"', js)
        self.assertIn('wechatauto:"微信Auto"', js)
        self.assertIn('wechatauto:weixinIcon', js)
        self.assertIn('wechatauto:[{name:"base_url"', js)
        self.assertIn('wechatauto:"https://github.com/luxi233/'
                      'wechatauto-channels"', js)

    def test_idempotent(self):
        cfg, js_path = self._mk(tempfile.mkdtemp())
        ops.octop_patch_frontend(cfg)
        once = js_path.read_text(encoding="utf-8")
        ok, msg = ops.octop_patch_frontend(cfg)
        self.assertTrue(ok)
        self.assertIn("幂等", msg)
        self.assertEqual(js_path.read_text(encoding="utf-8"), once)

    def test_no_bundle_fails(self):
        home = Path(tempfile.mkdtemp()) / ".octop"
        home.mkdir(parents=True)
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)})
        ok, _ = ops.octop_patch_frontend(cfg)
        self.assertFalse(ok)

    def test_missing_anchors_report(self):
        cfg, _ = self._mk(tempfile.mkdtemp(), js='const X={a:1};')
        ok, msg = ops.octop_patch_frontend(cfg)
        self.assertFalse(ok)
        self.assertIn("锚点", msg)


class TestOctopChannelRow(unittest.TestCase):
    """建行：直插 octop.db，幂等不覆盖既有 config（保护 group_allow）。"""

    def _mk(self, tmp):
        home = Path(tmp) / ".octop"
        home.mkdir()
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.executescript("""
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT UNIQUE, user_id INT);
            INSERT INTO agents (agent_id, user_id) VALUES ('main', 1);
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                kind TEXT NOT NULL, name TEXT NOT NULL,
                config_json TEXT NOT NULL, enabled INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                UNIQUE(agent_id, name));
        """)
        conn.commit()
        conn.close()
        return copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": str(home)}), home

    def test_creates_row_with_bridge_config(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        ok, msg = ops.octop_create_channel_row(cfg)
        self.assertTrue(ok, msg)
        conn = sqlite3.connect(str(home / "octop.db"))
        row = conn.execute(
            "SELECT channel_id, agent_id, kind, enabled, config_json"
            " FROM channels WHERE kind='wechatauto'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "main")
        self.assertEqual(row[3], 1)
        conf = json.loads(row[4])
        self.assertEqual(conf["token"], "octop-e2e-token")
        self.assertTrue(conf["verify_send"])
        self.assertIn("group_context", conf)

    def test_idempotent_preserves_existing(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.execute(
            "INSERT INTO channels (channel_id, agent_id, user_id, kind,"
            " name, config_json, enabled, created_at, updated_at)"
            " VALUES ('EXISTING','main',1,'wechatauto','旧',"
            " '{\"group_allow\":[\"111@chatroom\"]}',1,1,1)")
        conn.commit()
        conn.close()
        ok, msg = ops.octop_create_channel_row(cfg)
        self.assertTrue(ok)
        self.assertIn("已存在", msg)
        conn = sqlite3.connect(str(home / "octop.db"))
        n = conn.execute(
            "SELECT COUNT(*) FROM channels WHERE kind='wechatauto'"
        ).fetchone()[0]
        conf = conn.execute(
            "SELECT config_json FROM channels WHERE channel_id='EXISTING'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertIn("111@chatroom", conf)

    def test_no_agents_fails(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.execute("DELETE FROM agents")
        conn.commit()
        conn.close()
        ok, msg = ops.octop_create_channel_row(cfg)
        self.assertFalse(ok)


class TestOctopOnboard(unittest.TestCase):
    """一键接入编排：注入+前端补丁+建行；后端不在线时停在建行后。"""

    def _mk(self, tmp):
        home = Path(tmp) / ".octop"
        pkg = (home / "portable" / "packages" / "octop_gateway" / "channels")
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            '_CHANNEL_MAP: dict[str, str] = {\n'
            '    "weixin": "octop_gateway.channels.weixin",\n'
            '}\n\n'
            'class ChannelKind(StrEnum):\n'
            '    WEIXIN = "weixin"\n\n\n'
            '_CLASS_NAMES: dict[str, str] = {\n'
            '    "weixin": "WeixinChannel",\n'
            '}\n', encoding="utf-8")
        assets = (home / "portable" / "packages" / "octop" / "dashboard"
                  / "assets")
        assets.mkdir(parents=True)
        (assets / "index.x.js").write_text(
            TestOctopFrontendPatch.JS, encoding="utf-8")
        conn = sqlite3.connect(str(home / "octop.db"))
        conn.executescript("""
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT UNIQUE, user_id INT);
            INSERT INTO agents (agent_id, user_id) VALUES ('main', 1);
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                kind TEXT NOT NULL, name TEXT NOT NULL,
                config_json TEXT NOT NULL, enabled INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                UNIQUE(agent_id, name));
        """)
        conn.commit()
        conn.close()
        # adapter 源码（cfg["octop_repo"] 指定）
        repo = Path(tmp) / "octop-gateway"
        src = (repo / "src" / "octop_gateway" / "channels" / "wechatauto")
        src.mkdir(parents=True)
        (src / "channel.py").write_text("# adapter", encoding="utf-8")
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {
            "octop_home": str(home), "octop_repo": str(repo)})
        return cfg, home

    def test_full_onboard_offline_backend(self):
        cfg, home = self._mk(tempfile.mkdtemp())
        with mock.patch.object(ops, "_octop_health", return_value=False):
            ok, msg = ops.octop_onboard(cfg)
        self.assertTrue(ok, msg)
        self.assertIn("已注入", msg)
        self.assertIn("octop.db", msg)
        # adapter 文件真的进去了 + __init__ 三处注册
        ch = (home / "portable" / "packages" / "octop_gateway" / "channels")
        self.assertTrue((ch / "wechatauto" / "channel.py").exists())
        init = (ch / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"wechatauto": "octop_gateway.channels.wechatauto"',
                      init)
        # 渠道行已建
        conn = sqlite3.connect(str(home / "octop.db"))
        row = conn.execute(
            "SELECT kind FROM channels WHERE kind='wechatauto'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        # 前端补丁打了
        js = (home / "portable" / "packages" / "octop" / "dashboard"
              / "assets" / "index.x.js").read_text(encoding="utf-8")
        self.assertIn('"wechatauto"', js)

    def test_onboard_not_installed(self):
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"octop_home": "/nonexistent"})
        ok, msg = ops.octop_onboard(cfg)
        self.assertFalse(ok)


class TestOpenClawOnboard(unittest.TestCase):
    def _mk(self, tmp):
        repo = Path(tmp) / "repo"
        src = repo / "openclaw" / "openclaw-wechatauto"
        src.mkdir(parents=True)
        (src / "openclaw.plugin.json").write_text("{}")
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"repo_root": str(repo)})
        return cfg

    def test_runs_install_and_config_sets(self):
        cfg = self._mk(tempfile.mkdtemp())
        calls = []

        def fake_run(argv, timeout):
            calls.append(argv)
            return 0, "ok"

        with mock.patch.object(ops.shutil, "which",
                               return_value="C:\\oc\\openclaw.exe"), \
                mock.patch.object(ops, "_checked_run", fake_run):
            ok, msg = ops.openclaw_onboard(cfg)
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0][1:3], ["plugins", "install"])
        self.assertEqual(calls[3][1:3], ["config", "set"])
        self.assertIn("bridgeToken", calls[3][3])
        self.assertEqual(calls[3][4], "octop-e2e-token")

    def test_missing_cli(self):
        cfg = self._mk(tempfile.mkdtemp())
        with mock.patch.object(ops.shutil, "which", return_value=None):
            ok, msg = ops.openclaw_onboard(cfg)
        self.assertFalse(ok)
        self.assertIn("openclaw", msg)

    def test_install_failure_reports_step(self):
        cfg = self._mk(tempfile.mkdtemp())
        calls = []

        def fake_run(argv, timeout):
            calls.append(argv)
            return (1, "boom") if "install" in argv else (0, "ok")

        with mock.patch.object(ops.shutil, "which",
                               return_value="openclaw"), \
                mock.patch.object(ops, "_checked_run", fake_run):
            ok, msg = ops.openclaw_onboard(cfg)
        self.assertFalse(ok)
        self.assertIn("boom", msg)


class TestHermesOnboard(unittest.TestCase):
    def _mk(self, tmp, yaml_text):
        home = Path(tmp) / "hermes"
        (home / "hermes-agent" / "venv" / "Scripts").mkdir(parents=True)
        (home / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
         ).write_text("x")
        (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
        repo = Path(tmp) / "repo"
        plug = repo / "hermes" / "wechatauto"
        plug.mkdir(parents=True)
        (plug / "adapter.py").write_text("# plugin", encoding="utf-8")
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {
            "hermes_home": str(home), "repo_root": str(repo)})
        return cfg, home

    def test_onboard_inject_enable_restart(self):
        cfg, home = self._mk(tempfile.mkdtemp(), "platforms:\n  weixin:\n    enabled: true\n")
        with mock.patch.object(ops, "_checked_run",
                               return_value=(0, "ok")):
            ok, msg = ops.hermes_onboard(cfg)
        self.assertTrue(ok, msg)
        self.assertTrue(
            (home / "plugins" / "wechatauto" / "adapter.py").exists())
        text = (home / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("wechatauto:\n    enabled: true", text)
        self.assertIn("weixin:\n    enabled: true", text)

    def test_onboard_missing_hermes(self):
        cfg = copy.deepcopy(ops.DEFAULT_CFG | {"hermes_home": "/nonexistent"})
        ok, _ = ops.hermes_onboard(cfg)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
