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

    if logger_module is not None:
        patch_logger_file_handler(logger_module)
    patch_get_self_info_close(db_module)
    patch_open_transient_retry(db_module)
    _LOG.info("replica compat patches: %s", _APPLIED)
    return dict(_APPLIED)


def patch_status() -> Dict[str, str]:
    """当前补丁状态（未调用过 apply 则空）。"""
    return dict(_APPLIED)
