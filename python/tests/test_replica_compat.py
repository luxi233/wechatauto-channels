"""replica_compat 离线单测 —— 用假模块验证补丁的应用/跳过判定与行为。

不依赖真实 wechatauto（Windows-only）；每个用例构造独立假类，避免
补丁状态在用例间泄漏（补丁直接改类，全局 _APPLIED 表不在这层测）。
"""
import logging
import sys
import tempfile
import types
from pathlib import Path

import pytest

from wechatauto_channels import replica_compat as rc


# ── 假 wechatauto 构件 ──────────────────────────────────────────────


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append(sql)

        class _R:
            def fetchone(self_inner):
                return ("wxid_self", "李乐儿", None)

        return _R()

    def close(self):
        self.closed = True


def _fake_db_module(get_self_info_src_style: str = "buggy"):
    """构造带 WeChatDB 的假 db 模块。

    buggy  : get_self_info 不关 conn（复刻上游缺陷形态）
    fixed  : 上游已修（含 conn.close()）
    alien  : 形态不认识（应跳过）
    """
    mod = types.ModuleType("fake_wechatauto_db")

    class WeChatDB:
        wxid = "wxid_self"

        def __init__(self):
            self._db_files = [("contact\\contact.db", "/x/contact.db", 0)]
            self.last_conn = None
            self.open_calls = 0

        def _open(self, rel):
            self.open_calls += 1
            self.last_conn = _FakeConn()
            return self.last_conn

    if get_self_info_src_style == "buggy":
        def get_self_info(self):
            import os
            for rel, path, _ in self._db_files:
                if os.path.basename(path) != "contact.db":
                    continue
                conn = self._open(rel)
                row = conn.execute(
                    "SELECT username FROM contact WHERE username=? LIMIT 1",
                    (self.wxid,),
                ).fetchone()
                if row:
                    return {"username": row[0]}
                return {}
            return {}
    elif get_self_info_src_style == "fixed":
        def get_self_info(self):
            import os
            for rel, path, _ in self._db_files:
                if os.path.basename(path) != "contact.db":
                    continue
                conn = self._open(rel)
                try:
                    row = conn.execute(
                        "SELECT username FROM contact WHERE username=? LIMIT 1",
                        (self.wxid,),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    return {"username": row[0]}
                return {}
            return {}
    else:  # alien
        def get_self_info(self):
            return {"username": "via_totally_other_mechanism"}

    WeChatDB.get_self_info = get_self_info
    mod.WeChatDB = WeChatDB
    return mod


def _fake_logger_module(setup_style: str = "buggy"):
    """构造带 WechatautoLogger 的假 logger 模块。

    buggy : setup_file_logger 用相对路径（复刻上游缺陷形态）
    fixed : 上游已改用绝对路径（含 gettempdir）
    alien : 形态不认识
    """
    mod = types.ModuleType("fake_wechatauto_logger")

    class WechatautoLogger:
        name = "fake_wechatauto"

        def __init__(self):
            self.logger = logging.getLogger(self.name)
            self.file_handler = None

        if setup_style == "buggy":
            def setup_file_logger(self):
                # 复刻上游：相对路径 + 无 OSError 兜底
                log_dir = Path("wechatauto_logs")
                log_dir.mkdir(parents=True, exist_ok=True)
                self.file_handler = logging.FileHandler(
                    log_dir / "app.log", encoding="utf-8"
                )
        elif setup_style == "fixed":
            def setup_file_logger(self):
                log_dir = Path(tempfile.gettempdir()) / "wechatauto_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                self.file_handler = logging.FileHandler(
                    log_dir / "app.log", encoding="utf-8"
                )
        else:
            def setup_file_logger(self):
                self.file_handler = "alien_impl"

        def _ensure_file_logger(self):
            if self.file_handler is None:
                self.setup_file_logger()

    mod.WechatautoLogger = WechatautoLogger
    return mod


# ── logger 补丁 ──────────────────────────────────────────────────────


class TestLoggerPatch:
    def test_buggy_shape_patched_and_uses_tempdir(self, tmp_path, monkeypatch):
        mod = _fake_logger_module("buggy")
        assert rc.patch_logger_file_handler(mod) == "applied"

        inst = mod.WechatautoLogger()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        inst.setup_file_logger()
        assert isinstance(inst.file_handler, logging.FileHandler)
        assert str(tmp_path) in inst.file_handler.baseFilename
        inst.file_handler.close()

    def test_unwritable_dir_sets_sentinel_not_raise(self, monkeypatch):
        mod = _fake_logger_module("buggy")
        rc.patch_logger_file_handler(mod)
        inst = mod.WechatautoLogger()

        def _boom(*a, **k):
            raise PermissionError(13, "拒绝访问")

        monkeypatch.setattr(Path, "mkdir", _boom)
        inst._ensure_file_logger()  # 不得抛出
        assert inst.file_handler is False
        # 哨兵生效：再次调用不重复尝试（mkdir 仍被拒，但直接短路）
        inst._ensure_file_logger()
        assert inst.file_handler is False

    def test_upstream_fixed_skipped(self):
        mod = _fake_logger_module("fixed")
        assert rc.patch_logger_file_handler(mod).startswith("skipped")

    def test_alien_shape_skipped(self):
        mod = _fake_logger_module("alien")
        assert rc.patch_logger_file_handler(mod) == "skipped:unrecognized shape"


# ── get_self_info 补丁 ───────────────────────────────────────────────


class TestSelfInfoPatch:
    def test_buggy_shape_patched_conn_closed(self):
        mod = _fake_db_module("buggy")
        assert rc.patch_get_self_info_close(mod) == "applied"
        db = mod.WeChatDB()
        info = db.get_self_info()
        assert info["username"] == "wxid_self"
        assert db.last_conn.closed is True

    def test_upstream_fixed_skipped(self):
        mod = _fake_db_module("fixed")
        assert rc.patch_get_self_info_close(mod) == "skipped:upstream already closes"
        # 未打补丁：原实现本就关 conn，行为不变
        db = mod.WeChatDB()
        db.get_self_info()
        assert db.last_conn.closed is True

    def test_alien_shape_skipped(self):
        mod = _fake_db_module("alien")
        assert rc.patch_get_self_info_close(mod) == "skipped:unrecognized shape"


# ── _open 重试补丁 ────────────────────────────────────────────────────


class TestOpenRetryPatch:
    def test_transient_oserror_retried(self):
        mod = types.ModuleType("fake_db")
        calls = []

        class WeChatDB:
            def _open(self, rel):
                # os.replace inside
                calls.append(rel)
                if len(calls) < 3:
                    raise PermissionError(13, "暂时占用")
                return "conn"

        mod.WeChatDB = WeChatDB
        assert rc.patch_open_transient_retry(mod) == "applied"
        assert mod.WeChatDB()._open("x") == "conn"
        assert len(calls) == 3

    def test_persistent_error_raises_last(self, monkeypatch):
        mod = types.ModuleType("fake_db")

        class WeChatDB:
            def _open(self, rel):
                """_open_unlocked os.replace"""
                raise PermissionError(13, "永远占用")

        mod.WeChatDB = WeChatDB
        # docstring 里的特征词让 _src 检测通过
        assert rc.patch_open_transient_retry(mod) == "applied"
        with pytest.raises(PermissionError):
            mod.WeChatDB()._open("x")

    def test_unrecognized_shape_skipped(self):
        mod = types.ModuleType("fake_db")

        class WeChatDB:
            def _open(self, rel):
                return object()

        mod.WeChatDB = WeChatDB
        assert rc.patch_open_transient_retry(mod) == "skipped:unrecognized shape"


# ── apply_replica_patches 装配 ───────────────────────────────────────


def test_apply_with_fake_wechatauto(monkeypatch):
    pkg = types.ModuleType("wechatauto")
    db_mod = _fake_db_module("buggy")
    log_mod = _fake_logger_module("buggy")
    monkeypatch.setitem(sys.modules, "wechatauto", pkg)
    monkeypatch.setitem(sys.modules, "wechatauto.db", db_mod)
    monkeypatch.setitem(sys.modules, "wechatauto.logger", log_mod)
    rc._APPLIED.clear()
    status = rc.apply_replica_patches()
    assert status["get_self_info_close"] == "applied"
    assert status["logger_file_handler"] == "applied"
    rc._APPLIED.clear()


def test_apply_without_wechatauto(monkeypatch):
    monkeypatch.delitem(sys.modules, "wechatauto", raising=False)
    monkeypatch.delitem(sys.modules, "wechatauto.db", raising=False)
    monkeypatch.delitem(sys.modules, "wechatauto.logger", raising=False)
    real_import = __import__

    def _no_wechatauto(name, *a, **k):
        if name.startswith("wechatauto") and "channels" not in name:
            raise ImportError(name)
        return real_import(name, *a, **k)

    import builtins

    monkeypatch.setattr(builtins, "__import__", _no_wechatauto)
    rc._APPLIED.clear()
    status = rc.apply_replica_patches()
    assert status["_import"].startswith("skipped")
    rc._APPLIED.clear()
