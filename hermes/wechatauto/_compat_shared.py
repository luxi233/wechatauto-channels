"""Vendored ``gateway.platforms._shared`` helpers for Hermes versions where the
shared module only ships ``get_scoped_secret`` / ``profile_scoped`` /
``coerce_port`` (e.g. 0.21.0, ~Sep 2026). Bodies byte-adapted from upstream
NousResearch/hermes-agent main; dependencies that already exist in 0.21.0's
``_shared`` are imported rather than duplicated.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any, Callable, Iterable, Optional

from gateway.platforms._shared import get_scoped_secret, profile_scoped


def extra_or_secret(extra: Optional[dict], key: str, env: str, default: Any = "",
                    *, blank_is_unset: bool = True) -> Any:
    """env → ``config.extra[key]`` → ``default``（上游语义原样保留：env 空值视为未设，
    YAML 显式 False/0 是真值）。"""
    if env:
        env_value = get_scoped_secret(env, None)
        if env_value is not None and str(env_value).strip():
            return env_value
    value = (extra or {}).get(key)
    if value is None or (blank_is_unset and isinstance(value, str) and not value.strip()):
        return default
    return value


def yaml_env_setter() -> Callable[[str, Any], None]:
    """``set_env(name, value)``：只在 var 未设时写 ``os.environ``，多 profile scope 下不写。"""
    skip = profile_scoped()

    def set_env(name: str, value: Any) -> None:
        if value is None or skip or os.getenv(name):
            return
        os.environ[name] = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)

    return set_env


def send_error(message: Any) -> dict:
    """Standalone-sender 失败信封（复用宿主 redact 后的 _error）。"""
    from tools.send_message_senders import _error
    return _error(str(message))


_YAML_KINDS: dict = {
    "lower": (lambda cfg, key: key in cfg, lambda v: str(v).lower()),
    "str": (lambda cfg, key: cfg.get(key) not in (None, ""), str),
    "csv": (lambda cfg, key: cfg.get(key) is not None, lambda v: v),
    "json": (lambda cfg, key: key in cfg, json.dumps),
}


def apply_yaml_bridge(cfg: dict, spec: Iterable) -> Optional[dict]:
    """表驱动 ``apply_yaml_config_fn``：每行 ``(yaml_key, ENV_VAR, kind)`` 把 YAML 值种进 extra
    并经 ``yaml_env_setter`` 桥到 env；无匹配返回 ``None``。"""
    set_env = yaml_env_setter()
    seeded: dict = {}
    for key, env, kind in spec:
        applies, encode = _YAML_KINDS[kind]
        if applies(cfg, key):
            seeded[key] = cfg[key]
            set_env(env, encode(cfg[key]))
    return seeded or None


def seed_extra_from_env(spec: Iterable, *,
                        home_env: Optional[str] = None, home_default: str = "") -> dict:
    """表驱动 ``env_enablement_fn``：每行 ``(ENV_VAR, extra_key, conv)`` 读 profile-scoped
    值，空白跳过；``home_env`` 追加 ``home_channel`` 字典。"""
    seed: dict = {}
    for env, key, conv in spec:
        raw = str(get_scoped_secret(env, "") or "").strip()
        if not raw:
            continue
        with contextlib.suppress(ValueError):
            seed[key] = conv(raw) if conv else raw
    if home_env:
        home = str(get_scoped_secret(home_env, "") or "").strip() or home_default
        if home:
            seed["home_channel"] = {"chat_id": home, "name": get_scoped_secret(f"{home_env}_NAME", "Home")}
    return seed
