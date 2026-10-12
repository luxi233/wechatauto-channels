"""wechatauto-replica 已知缺陷的进程内兼容补丁。

不修改 site-packages：所有补丁在本进程内 monkeypatch，重装/升级 replica
不会丢失。每个补丁先做源码特征检测——上游已修复（或形态不认识）时自动
跳过，绝不按猜测硬改。

Note: 补丁清单与取舍见
.agents/notes/implemented/bug-fix/2026-10-08-replica-compat-layer.md

用法：在首次构造 WeChatDB / Listener 之前调用 ``apply_replica_patches()``。
幂等，可重复调用。
"""

from __future__ import annotations

import inspect
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_LOG = logging.getLogger("wechatauto_channels.replica_compat")

_APPLIED: Dict[str, str] = {}  # patch_name -> "applied" | "skipped:<reason>"
_OPEN_RETRY_LOCK = threading.RLock()
_OPEN_RETRY_DELAYS = (0.3, 1.0)


def _src(obj: Any) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def _record(name: str, status: str) -> None:
    _APPLIED[name] = status
    _LOG.debug("replica patch %s: %s", name, status)


def patch_logger_file_handler(logger_module: Any) -> str:
    """修 wxlog 文件日志的相对路径炸点。

    上游 ``setup_file_logger`` 用 ``Path("wechatauto_logs")`` 相对 CWD 建目录，
    宿主进程 CWD 不可写（服务/受管进程常见 CWD=System32）时 mkdir/FileHandler
    抛 PermissionError——且抛出点在 ``wxlog.warning()`` 内部，会把「WAL 合并
    失败→降级旧快照」这种本应容忍的降级变成致命错误（2026-10-08 实锤：
    桥进程因此活着但永久失明）。

    补丁：日志目录改为 ``tempfile.gettempdir()/wechatauto_logs`` 绝对路径；
    任何 OSError 只置哨兵 ``file_handler=False``（``_ensure_file_logger`` 的
    ``is None`` 检查对 False 返回 False，天然停止重试），文件日志失败永不
    波及调用方。
    """
    name = "logger_file_handler"
    cls = getattr(logger_module, "WechatautoLogger", None)
    if cls is None:
        _record(name, "skipped:no WechatautoLogger")
        return _APPLIED[name]
    original = getattr(cls, "setup_file_logger", None)
    if original is None:
        _record(name, "skipped:no setup_file_logger")
        return _APPLIED[name]
    if getattr(original, "_wac_safe", False):
        _record(name, "skipped:already patched")
        return _APPLIED[name]
    src = _src(original)
    if "gettempdir" in src or "expanduser" in src:
        _record(name, "skipped:upstream already absolute")
        return _APPLIED[name]
    if "wechatauto_logs" not in src:
        _record(name, "skipped:unrecognized shape")
        return _APPLIED[name]

    param_mod = sys.modules.get("wechatauto.param")
    wx_param = getattr(param_mod, "WxParam", None)

    def setup_file_logger(self: Any) -> None:  # noqa: ANN001 - mirrors upstream
        enabled = getattr(wx_param, "ENABLE_FILE_LOGGER", True) if wx_param else True
        if not enabled or self.file_handler is not None:
            return
        try:
            log_dir = Path(tempfile.gettempdir()) / "wechatauto_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime

            log_file = log_dir / f"app_{datetime.now():%Y%m%d}.log"
            handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError:
            # 文件日志不可写不是致命错误——置哨兵停止重试，绝不波及调用方。
            self.file_handler = False
            return
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] [%(levelname)s] "
                "[%(filename)s:%(lineno)d]  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setLevel(logging.DEBUG)
        self.file_handler = handler
        self.logger.addHandler(handler)

    setup_file_logger._wac_safe = True  # type: ignore[attr-defined]
    cls.setup_file_logger = setup_file_logger

    # 双保险：即便未来上游换了自己的 setup 实现且其中抛非 OSError，
    # _ensure_file_logger 也不允许把异常传回调用方。
    orig_ensure = getattr(cls, "_ensure_file_logger", None)
    if orig_ensure is not None and not getattr(orig_ensure, "_wac_safe", False):
        def _ensure_file_logger(self: Any) -> None:
            try:
                orig_ensure(self)
            except Exception:
                self.file_handler = False

        _ensure_file_logger._wac_safe = True  # type: ignore[attr-defined]
        cls._ensure_file_logger = _ensure_file_logger

    _record(name, "applied")
    return _APPLIED[name]


def patch_get_self_info_close(db_module: Any) -> str:
    """修 ``WeChatDB.get_self_info`` 泄漏 sqlite 连接。

    上游该方法是全库唯一开 ``_open`` 连接后不 ``close()`` 的调用点；泄漏的
    句柄在 Windows 上挡住下一轮快照重建的 ``os.replace``（目标被持有打开
    句柄即 PermissionError）——桥自锁失明的主犯之一。
    """
    name = "get_self_info_close"
    cls = getattr(db_module, "WeChatDB", None)
    original = getattr(cls, "get_self_info", None) if cls else None
    if original is None:
        _record(name, "skipped:no get_self_info")
        return _APPLIED[name]
    if getattr(original, "_wac_safe", False):
        _record(name, "skipped:already patched")
        return _APPLIED[name]
    src = _src(original)
    if "conn.close()" in src:
        _record(name, "skipped:upstream already closes")
        return _APPLIED[name]
    if "self._open(rel)" not in src or "contact.db" not in src:
        _record(name, "skipped:unrecognized shape")
        return _APPLIED[name]

    import os

    def get_self_info(self: Any) -> dict:
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            try:
                row = conn.execute(
                    "SELECT username, nick_name, remark FROM contact "
                    "WHERE username=? LIMIT 1",
                    (self.wxid,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return {
                    "username": row[0],
                    "nick_name": row[1],
                    "remark": row[2],
                }
            return {}
        return {}

    get_self_info._wac_safe = True  # type: ignore[attr-defined]
    cls.get_self_info = get_self_info
    _record(name, "applied")
    return _APPLIED[name]


def patch_open_transient_retry(db_module: Any) -> str:
    """给 ``WeChatDB._open`` 加瞬态 OS 错误重试。

    Windows 上快照重建的 ``os.replace`` 会被短时持有句柄（杀软实时扫描、
    句柄释放延迟）打出 PermissionError；虽上游已把 WAL 合并失败降级为旧
    快照，短重试能直接拿到新鲜数据、减少失明窗口。只重试 OSError 系，
    逻辑错误原样抛出。
    """
    name = "open_transient_retry"
    cls = getattr(db_module, "WeChatDB", None)
    original = getattr(cls, "_open", None) if cls else None
    if original is None:
        _record(name, "skipped:no _open")
        return _APPLIED[name]
    if getattr(original, "_wac_safe", False):
        _record(name, "skipped:already patched")
        return _APPLIED[name]
    src = _src(original)
    if "os.replace" not in src and "_merge_wal" not in src and "_open_unlocked" not in src:
        _record(name, "skipped:unrecognized shape")
        return _APPLIED[name]

    def _open(self: Any, rel: str) -> Any:
        last_exc: Optional[OSError] = None
        for delay in (0.0, *_OPEN_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                with _OPEN_RETRY_LOCK:
                    return original(self, rel)
            except (PermissionError, OSError) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    _open._wac_safe = True  # type: ignore[attr-defined]
    cls._open = _open
    _record(name, "applied")
    return _APPLIED[name]


def patch_file_paste_verified(guia_module: Any) -> str:
    """修 ``WeChatGUI._paste_attachment_and_send`` 的静默假成功。

    上游缺陷有两层（2026-10-12 实锤，M1说明书.pdf 两次"已发送"均未落库）：

    1. ``focus_input`` 点击点在 ``y0 + min(24, h//4)``——贴探测框上沿。
       探测框上沿比真实输入框高几个像素时，点击落到消息区，输入框从未
       获得焦点，Ctrl+V 的文件粘贴被微信 4.x 静默吞掉（不弹任何错误）。
    2. 非图片分支**不验证草稿**：粘贴后 sleep 2s 盲按回车、无条件返回
       True——对空输入框按回车等于没发，链路却一路报"已发送"。

    补丁：①改用 ``send_input_click`` 点输入框**中心**（实测稳定聚焦）；
    ②文件草稿用 ``_input_box_has_text`` 检测（文件卡片文件名是深色文字，
    命中其深色像素阈值），未就绪重试粘贴；③发送复用 ``click_send``
    （回车→验证清空→OCR「发送」按钮兜底）；④草稿始终未出现返回
    False——宁可报真实失败也不报假成功。
    """
    name = "file_paste_verified"
    cls = getattr(guia_module, "WeChatGUI", None)
    original = getattr(cls, "_paste_attachment_and_send", None) if cls else None
    if original is None:
        _record(name, "skipped:no _paste_attachment_and_send")
        return _APPLIED[name]
    if getattr(original, "_wac_safe", False):
        _record(name, "skipped:already patched")
        return _APPLIED[name]
    src = _src(original)
    if "_input_box_has_text" in src or "click_send" in src:
        _record(name, "skipped:upstream already verifies")
        return _APPLIED[name]
    if "copy_files_to_clipboard" not in src or "_input_box_has_color_draft" not in src:
        _record(name, "skipped:unrecognized shape")
        return _APPLIED[name]

    import os

    wxlog = getattr(guia_module, "wxlog", _LOG)
    rhythm = getattr(guia_module, "rhythm", None)
    VK_A = getattr(guia_module, "VK_A", 0x41)
    VK_DELETE = getattr(guia_module, "VK_DELETE", 0x2E)
    VK_V = getattr(guia_module, "VK_V", 0x56)
    VK_RETURN = getattr(guia_module, "VK_RETURN", 0x0D)

    def _send_button_enabled(self: Any) -> bool:
        """「发送」按钮变绿 = 存在可发送草稿；灰色 = 输入框为空。

        比 ``_input_box_has_text`` 可靠：后者按深色像素计数，会被空输入
        框里闪烁的光标和消息区溢出像素打成假阳性（2026-10-12 实锤：
        文件已发出但 click_send 凭它误判"草稿仍在"返回 False）。
        """
        try:
            x0, y0, x1, y1 = self.send_button_region
            img = self._grab_screen(self._rel_to_screen((x0, y0, x1, y1)))
            px = img.load()
            green = 0
            for yy in range(0, img.size[1], 2):
                for xx in range(0, img.size[0], 2):
                    r, g, b = px[xx, yy][:3]
                    if g > 140 and r < 100 and b < 140:
                        green += 1
            return green > 20
        except Exception:  # noqa: BLE001 - 截图失败按"不可用"处理
            return False

    def _wait_send_button(self: Any, enabled: bool, wait: float) -> bool:
        deadline = time.time() + wait
        while time.time() < deadline:
            if self._wac_send_button_enabled() == enabled:
                return True
            time.sleep(0.4)
        return False

    def _paste_attachment_and_send(self: Any, path: str,
                                   box: Any = None) -> bool:
        if not os.path.isfile(path):
            wxlog.debug(f'文件不存在：{path}')
            return False
        is_image = path.lower().endswith(
            ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
        if box is None:
            box = self.get_input_box()
        wxlog.debug(f'粘贴前输入框探测：{box}')
        if not box:
            return False
        x0, y0, x1, y1 = box
        draft_ok = False
        for attempt in range(3):
            # 输入框中心真实点击聚焦。上游 focus_input 点 y0+24 顶边，
            # 探测框上沿偏高几像素就落到消息区——文件粘贴随之被吞。
            self._input.send_input_click(
                self.origin_x + (x0 + x1) // 2,
                self.origin_y + (y0 + y1) // 2)
            time.sleep(0.5)
            if not self.copy_files_to_clipboard([path]):
                return False
            time.sleep(0.3)
            self._input.key(VK_A, ctrl=True)
            self._input.key(VK_DELETE)
            self._input.key(VK_V, ctrl=True)
            if is_image:
                draft_ok = self._input_box_has_color_draft(box, wait=6.0)
            else:
                # 文件草稿 = 「发送」按钮转绿。光标/消息区都不会点亮它，
                # 比数深色像素干净。
                draft_ok = _wait_send_button(self, True, 5.0)
            wxlog.debug(f'粘贴尝试 {attempt + 1} 草稿检测：{draft_ok}')
            if draft_ok:
                break
        if not draft_ok:
            # 上游原行为是仍然盲按回车并返回 True（假成功）。这里必须
            # 诚实失败，让 send_file / 桥 / adapter 层层报真实错误。
            wxlog.warning(
                f'附件粘贴未检测到草稿（{os.path.basename(path)}），放弃发送')
            return False
        # 发送：回车 → 按钮转灰（草稿被消费）才算发出；未转灰则
        # OCR 定位「发送」按钮点击兜底。全程不碰 _input_box_has_text。
        for attempt in range(3):
            if rhythm is not None:
                rhythm.gate('send-file')
            self._input.key(VK_RETURN)
            if _wait_send_button(self, False, 2.0):
                return True
            wxlog.debug(f'回车后发送按钮仍可用（attempt={attempt}），改用按钮点击')
            try:
                res = self.ocr(self.send_button_region)
            except Exception:  # noqa: BLE001
                res = []
            clicked = False
            for text, ox, oy, ow, oh in res:
                if '发送' in (text or ''):
                    px_, py_ = (self.origin_x + ox + ow // 2,
                                self.origin_y + oy + oh // 2)
                    if rhythm is not None:
                        px_, py_ = rhythm.point((self.origin_x + ox,
                                                 self.origin_y + oy,
                                                 self.origin_x + ox + ow,
                                                 self.origin_y + oy + oh))
                    self.wx_click(px_, py_)
                    clicked = True
                    break
            if not clicked:
                # 按钮区域 OCR 不到「发送」——可能已被消费/布局异常，
                # 以按钮灰态再确认一次收尾。
                return not self._wac_send_button_enabled()
            if _wait_send_button(self, False, 2.0):
                return True
        return False

    _paste_attachment_and_send._wac_safe = True  # type: ignore[attr-defined]
    cls._paste_attachment_and_send = _paste_attachment_and_send
    cls._wac_send_button_enabled = _send_button_enabled
    _record(name, "applied")
    return _APPLIED[name]


def apply_replica_patches() -> Dict[str, str]:
    """对进程内的 wechatauto-replica 应用全部已知缺陷补丁。幂等。

    返回 ``{patch_name: "applied" | "skipped:<reason>"}``，便于日志/诊断。
    """
    if _APPLIED:
        return dict(_APPLIED)
    try:
        import wechatauto.db as db_module  # type: ignore
    except Exception:
        _record("_import", "skipped:wechatauto not importable")
        return dict(_APPLIED)
    try:
        import wechatauto.logger as logger_module  # type: ignore
    except Exception:
        logger_module = None

    try:
        import wechatauto.guia as guia_module  # type: ignore
    except Exception:
        guia_module = None

    if logger_module is not None:
        patch_logger_file_handler(logger_module)
    patch_get_self_info_close(db_module)
    patch_open_transient_retry(db_module)
    if guia_module is not None:
        patch_file_paste_verified(guia_module)
    _LOG.info("replica compat patches: %s", _APPLIED)
    return dict(_APPLIED)


def patch_status() -> Dict[str, str]:
    """当前补丁状态（未调用过 apply 则空）。"""
    return dict(_APPLIED)
