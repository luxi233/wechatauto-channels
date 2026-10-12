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


# ── 文件粘贴验证补丁 ──────────────────────────────────────────────────


def _fake_guia_module(paste_style: str = "buggy"):
    """构造带 WeChatGUI 的假 guia 模块。

    buggy : _paste_attachment_and_send 是上游缺陷形态（无草稿检测、
            无 click_send 兜底）
    fixed : 上游已验证草稿（含 _input_box_has_text）
    alien : 形态不认识
    """
    mod = types.ModuleType("fake_wechatauto_guia")
    mod.VK_A = 0x41
    mod.VK_DELETE = 0x2E
    mod.VK_V = 0x56
    mod.VK_RETURN = 0x0D

    class _Input:
        def __init__(self, gui):
            self.gui = gui
            self.clicks = []
            self.keys = []

        def send_input_click(self, x, y):
            self.clicks.append((x, y))
            self.gui.focused = True

        def key(self, vk, ctrl=False, shift=False):
            self.keys.append(vk)
            g = self.gui
            if vk == mod.VK_V and g.focused and g.paste_works:
                g.button_enabled = True   # 粘贴产生草稿 → 发送按钮转绿
            if vk == mod.VK_RETURN and g.button_enabled and g.enter_works:
                g.button_enabled = False  # 回车消费草稿 → 按钮转灰

    class WeChatGUI:
        origin_x = 100
        origin_y = 50
        send_button_region = (0, 0, 100, 40)

        def __init__(self):
            self._input = _Input(self)
            self.clipboard_calls = 0
            self.draft_results = []
            self.ocr_calls = 0
            self.ocr_results = []
            self.wx_clicks = []
            self.focused = False
            self.button_enabled = False
            self.paste_works = True
            self.enter_works = True
            self.ocr_click_works = True

        def _grab_screen(self, rect):
            """按 button_enabled 返回全绿/全灰假图像，喂给真实的
            _wac_send_button_enabled 像素判定。"""
            color = (0, 200, 0) if self.button_enabled else (200, 200, 200)

            class _Img:
                size = (10, 10)

                def load(self):
                    return {(x, y): color
                            for y in range(10) for x in range(10)}

            return _Img()

        def _rel_to_screen(self, rel):
            return rel

        def get_input_box(self):
            return (10, 20, 210, 120)

        def copy_files_to_clipboard(self, paths):
            self.clipboard_calls += 1
            return True

        def _input_box_has_color_draft(self, box=None, wait=6.0):
            return self.draft_results.pop(0) if self.draft_results else False

        def ocr(self, region):
            self.ocr_calls += 1
            return self.ocr_results

        def wx_click(self, x, y):
            self.wx_clicks.append((x, y))
            if self.ocr_click_works and self.button_enabled:
                self.button_enabled = False  # 按钮点击消费草稿

    if paste_style == "buggy":
        def _paste_attachment_and_send(self, path, box=None):
            if box is None:
                box = self.get_input_box()
            for _ in range(3):
                if not self.copy_files_to_clipboard([path]):
                    return False
                if path.endswith(".png"):
                    if self._input_box_has_color_draft(box):
                        break
            return True
    elif paste_style == "fixed":
        def _paste_attachment_and_send(self, path, box=None):
            if self._input_box_has_text(box):
                return self.click_send()
            return False
    else:
        def _paste_attachment_and_send(self, path, box=None):
            return "alien"

    WeChatGUI._paste_attachment_and_send = _paste_attachment_and_send
    mod.WeChatGUI = WeChatGUI
    return mod


class TestFilePastePatch:
    def test_buggy_shape_patched(self):
        mod = _fake_guia_module("buggy")
        assert rc.patch_file_paste_verified(mod) == "applied"

    def test_upstream_fixed_skipped(self):
        mod = _fake_guia_module("fixed")
        assert rc.patch_file_paste_verified(mod).startswith("skipped")

    def test_alien_shape_skipped(self):
        mod = _fake_guia_module("alien")
        assert rc.patch_file_paste_verified(mod) == "skipped:unrecognized shape"

    def test_paste_swallowed_returns_false_never_enters(self, tmp_path):
        """粘贴被吞（草稿不出现）→ 3 次重试后诚实 False，不按回车。"""
        mod = _fake_guia_module("buggy")
        rc.patch_file_paste_verified(mod)
        gui = mod.WeChatGUI()
        gui.paste_works = False  # 粘贴永远被吞：按钮不变绿
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        # _wait_send_button 内部有时间等待——把 sleep 打薄
        import wechatauto_channels.replica_compat as _rc
        orig_sleep = _rc.time.sleep
        _rc.time.sleep = lambda s: None
        try:
            assert gui._paste_attachment_and_send(str(f)) is False
        finally:
            _rc.time.sleep = orig_sleep
        assert gui.clipboard_calls == 3
        assert mod.VK_RETURN not in gui._input.keys

    def test_happy_path_paste_enter_sends(self, tmp_path):
        """粘贴出草稿 + 回车发送 → True，且用中心点击聚焦。"""
        mod = _fake_guia_module("buggy")
        rc.patch_file_paste_verified(mod)
        gui = mod.WeChatGUI()
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orig_sleep = rc.time.sleep
        rc.time.sleep = lambda s: None
        try:
            assert gui._paste_attachment_and_send(str(f)) is True
        finally:
            rc.time.sleep = orig_sleep
        # box (10,20,210,120) 中心 rel (110,70) → screen (210,120)
        assert gui._input.clicks[0] == (210, 120)
        assert mod.VK_RETURN in gui._input.keys

    def test_enter_fails_ocr_button_fallback(self, tmp_path):
        """回车发不出 → OCR 定位「发送」按钮点击兜底。"""
        mod = _fake_guia_module("buggy")
        rc.patch_file_paste_verified(mod)
        gui = mod.WeChatGUI()
        gui.enter_works = False  # 回车不生效
        gui.ocr_results = [("发送", 50, 10, 40, 20)]
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orig_sleep = rc.time.sleep
        rc.time.sleep = lambda s: None
        try:
            assert gui._paste_attachment_and_send(str(f)) is True
        finally:
            rc.time.sleep = orig_sleep
        assert gui.ocr_calls >= 1
        assert gui.wx_clicks  # OCR 按钮被点了

    def test_send_never_confirmed_returns_false(self, tmp_path):
        """草稿在但回车/按钮都发不出 → False。"""
        mod = _fake_guia_module("buggy")
        rc.patch_file_paste_verified(mod)
        gui = mod.WeChatGUI()
        gui.enter_works = False
        gui.ocr_click_works = False
        gui.ocr_results = [("发送", 50, 10, 40, 20)]
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orig_sleep = rc.time.sleep
        rc.time.sleep = lambda s: None
        try:
            assert gui._paste_attachment_and_send(str(f)) is False
        finally:
            rc.time.sleep = orig_sleep

    def test_missing_file_returns_false(self):
        mod = _fake_guia_module("buggy")
        rc.patch_file_paste_verified(mod)
        gui = mod.WeChatGUI()
        assert gui._paste_attachment_and_send("/nonexistent/x.pdf") is False
        assert gui.clipboard_calls == 0


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
