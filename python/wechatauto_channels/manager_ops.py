"""manager_ops — wechatauto 管理台的无 UI 操作层。

manager.py（tkinter 界面）只做渲染与调度；所有探测、配置读写、
进程管理、Octop API 交互都在这里，保证可用 pytest 覆盖。

设计边界：
- 全部 stdlib（sqlite3 / urllib / hmac / json），不依赖 PyJWT/PyYAML——
  manager 可能跑在非 hermes venv 的 python 上。
- Octop config.yaml 修改用定向文本编辑而非全量 yaml round-trip，
  避免吞掉用户注释与格式。
- bridge 无 /shutdown 端点，stop 只能 taskkill——这是 replica 侧边界。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# paths / config
# ---------------------------------------------------------------------------

def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


def manager_dir() -> Path:
    """~/.wechatauto，可用 WECHATAUTO_HOME 覆盖（测试隔离用）。"""
    return _env_path("WECHATAUTO_HOME", Path.home() / ".wechatauto")


def log_dir() -> Path:
    """%TEMP%/wechatauto-channels，可用 WECHATAUTO_LOG_DIR 覆盖。"""
    return _env_path(
        "WECHATAUTO_LOG_DIR",
        Path(os.environ.get("TEMP", str(Path.home()))) / "wechatauto-channels",
    )


def manager_cfg_path() -> Path:
    return manager_dir() / "manager.json"


def manager_log_path() -> Path:
    return log_dir() / "manager.log"


def bridge_stdout_log_path() -> Path:
    return log_dir() / "bridge-stdout.log"


# 默认快照，仅作展示/兼容；函数内部一律走上面的动态解析，
# 保证测试导入后设置环境变量即可完全隔离真实 ~/.wechatauto 与日志。
MANAGER_DIR = manager_dir()
MANAGER_CFG = MANAGER_DIR / "manager.json"
LOG_DIR = log_dir()
MANAGER_LOG = LOG_DIR / "manager.log"
BRIDGE_STDOUT_LOG = LOG_DIR / "bridge-stdout.log"

DEFAULT_CFG = {
    "bridge": {
        "exe": "",            # 空 = 自动探测
        "host": "127.0.0.1",
        "port": 18765,
        "token": "octop-e2e-token",
        "extra_args": [],
    },
    "supervise": {
        "enabled": True,
        "interval_s": 15,
        "restart_cooldown_s": 60,
        "octop_reinject": True,   # Octop 升级覆盖注入的 adapter 后自动重注入
        "auto_open_resources": True,  # 新建 KB/连接器/技能包自动开默认声明
    },
    "hermes_home": "",        # 空 = 自动探测
    "octop_home": "",         # 空 = 自动探测
    "repo_root": "",          # wechatauto-channels clone，注入用
    "octop_repo": "",         # octop-gateway clone，空 = 探测 repo_root 兄弟目录
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        disk = json.loads(manager_cfg_path().read_text(encoding="utf-8"))
        for k, v in disk.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    manager_dir().mkdir(parents=True, exist_ok=True)
    manager_cfg_path().write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def log_line(text: str) -> None:
    """追加一行到 manager.log（同时也是 GUI 日志窗的一个来源）。"""
    log_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with manager_log_path().open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# WeChat client
# ---------------------------------------------------------------------------

def _checked_run(argv: list[str], timeout: float) -> tuple[int, str]:
    """subprocess.run + capture_output 在 Windows 有死锁坑：超时 kill 后
    communicate() 会 join 读管线程，若孙进程继承了管道句柄，EOF 永不
    到来——timeout 形同虚设，调用线程永久泄漏（生产实测探针线程堆积）。
    输出走临时文件：无管道 → 无读管线程 → timeout 语义真实生效。"""
    log_dir().mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=log_dir(), suffix=".out")
    try:
        with os.fdopen(fd, "wb") as fh:
            r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=timeout, creationflags=_no_window())
        out = Path(tmp).read_bytes().decode("utf-8", errors="replace")
        return r.returncode, out
    finally:
        Path(tmp).unlink(missing_ok=True)


def _checked_output(argv: list[str], timeout: float) -> str:
    return _checked_run(argv, timeout)[1]


def wechat_running() -> bool:
    try:
        return "Weixin.exe" in _checked_output(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"], 10)
    except Exception:
        return False


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# bridge process
# ---------------------------------------------------------------------------

_BRIDGE_EXE_CANDIDATES = [
    r"C:\Users\{u}\AppData\Local\hermes\hermes-agent\venv\Scripts\wechatauto-bridge.exe",
]


def find_bridge_exe(cfg: dict) -> str:
    configured = cfg["bridge"].get("exe") or ""
    if configured and Path(configured).exists():
        return configured
    which = shutil.which("wechatauto-bridge.exe") or shutil.which("wechatauto-bridge")
    if which:
        return which
    user = os.environ.get("USERNAME", "")
    for pat in _BRIDGE_EXE_CANDIDATES:
        p = Path(pat.format(u=user))
        if p.exists():
            return str(p)
    return ""


def bridge_health(cfg: dict, timeout: float = 5.0) -> dict | None:
    """GET /health；成功返回 dict，任何失败返回 None。"""
    b = cfg["bridge"]
    url = f"http://{b['host']}:{b['port']}/health"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {b['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _bridge_running() -> bool:
    """桥在跑 = 端口能建立 TCP 连接（比按进程名找更准，pythonw 形态也覆盖）。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 18765), timeout=1):
            return True
    except OSError:
        return False


def bridge_pid() -> int | None:
    """桥进程的 pid（exe 或 pythonw 形态）；没跑返回 None。"""
    try:
        out = _checked_output(["tasklist", "/FO", "CSV", "/NH", "/V"], 10)
        # exe 形态
        m = re.search(r'"wechatauto-bridge\.exe","(\d+)"', out)
        if m:
            return int(m.group(1))
        # pythonw -m wechatauto_channels.bridge 形态：wmi 查命令行
        wmi = _checked_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine "
             "-match 'wechatauto_channels\\.bridge|wechatauto-bridge' } | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            15).strip()
        return int(wmi) if wmi.isdigit() else None
    except Exception:
        return None


def _bridge_argv(cfg: dict, exe: str) -> list[str]:
    """构造桥启动 argv。优先用 venv 的 pythonw -m 模块形态：
    wechatauto-bridge.exe 是 PyInstaller console 包，其孙进程会向
    默认终端 AllocConsole 弹黑窗；pythonw 是 windowed 子系统，全链路无 console。"""
    tail = ["--token", cfg["bridge"]["token"],
            *cfg["bridge"].get("extra_args", [])]
    exe_path = Path(exe)
    pythonw = exe_path.parent / "pythonw.exe"
    if exe_path.name.lower() == "wechatauto-bridge.exe" and pythonw.exists():
        return [str(pythonw), "-m", "wechatauto_channels.bridge", *tail]
    return [exe, *tail]


def bridge_start(cfg: dict) -> tuple[bool, str]:
    """拉起桥。stdout/stderr 落到 bridge-stdout.log 供日志窗消费。"""
    exe = find_bridge_exe(cfg)
    if not exe:
        return False, "找不到 wechatauto-bridge.exe（设置里可手动指定路径）"
    log_dir().mkdir(parents=True, exist_ok=True)
    args = _bridge_argv(cfg, exe)
    try:
        # CREATE_NO_WINDOW + CREATE_BREAKAWAY_FROM_JOB：隐藏 console（对
        # pythonw 形态为无害空操作）+ 脱离拉起者的 Job 清理连坐。
        # 注意不能用 DETACHED_PROCESS：exe 形态的孙进程无 console 可继承，
        # 会自行 AllocConsole 在默认终端弹黑窗。
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, close_fds=True,
                stdout=open(bridge_stdout_log_path(), "ab"),
                stderr=subprocess.STDOUT, creationflags=flags)
        except OSError:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, close_fds=True,
                stdout=open(bridge_stdout_log_path(), "ab"),
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # spawn 成功 ≠ 进程活着：-m 缺 __main__ 守卫那类 bug 会让桥
        # rc=0 静默秒退，watchdog 空转一整夜才被发现。等一个启动窗口，
        # 早夭就立刻读 stdout 尾部把死因报出来。
        time.sleep(4)
        rc = proc.poll()
        if rc is not None:
            tail = tail_lines(bridge_stdout_log_path(), 15) or "(无输出)"
            log_line(f"bridge died instantly rc={rc}: {tail[-200:]}")
            return False, f"桥启动后 {rc} 退出：{tail[-200:]}"
        log_line(f"bridge started pid={proc.pid} exe={exe}")
        return True, f"已启动 pid={proc.pid}"
    except Exception as e:
        log_line(f"bridge start failed: {e}")
        return False, str(e)


def bridge_stop() -> tuple[bool, str]:
    pid = bridge_pid()
    if not pid:
        return True, "桥没在运行"
    rc, out = _checked_run(["taskkill", "/F", "/PID", str(pid)], 15)
    ok = rc == 0
    log_line(f"bridge stop pid={pid} rc={rc}")
    return ok, out.strip()


def bridge_wait_healthy(cfg: dict, timeout_s: float = 45.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if bridge_health(cfg, timeout=3):
            return True
        time.sleep(1.5)
    return False


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------

def hermes_home(cfg: dict) -> Path | None:
    if cfg.get("hermes_home"):
        p = Path(cfg["hermes_home"])
        if p.exists():
            return p
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
    return p if p.exists() else None


def hermes_status(cfg: dict) -> dict:
    home = hermes_home(cfg)
    st = {"installed": False, "plugin": False, "whitelisted": False,
          "platform_enabled": None, "connected": None, "home": str(home or "")}
    if not home:
        return st
    st["installed"] = True
    st["plugin"] = (home / "plugins" / "wechatauto" / "plugin.yaml").exists()
    cfg_yaml = home / "config.yaml"
    if cfg_yaml.exists():
        text = cfg_yaml.read_text(encoding="utf-8", errors="replace")
        st["whitelisted"] = bool(
            re.search(r"plugins:[\s\S]*?enabled:[\s\S]*?wechatauto", text))
        m = re.search(
            r"platforms:\s*\n(?:\s+.+\n)*?\s+wechatauto:\s*\n\s+enabled:\s*(\w+)",
            text)
        if m:
            st["platform_enabled"] = m.group(1).lower() in ("true", "yes")
    gs = home / "gateway_state.json"
    if gs.exists():
        try:
            state = json.loads(gs.read_text(encoding="utf-8"))
            plat = state.get("platforms", {}).get("wechatauto", {})
            st["connected"] = plat.get("state")
        except Exception:
            pass
    return st


def hermes_set_platform_enabled(cfg: dict, enabled: bool) -> tuple[bool, str]:
    """定向改 config.yaml 里 platforms.wechatauto.enabled，不动其它行。"""
    home = hermes_home(cfg)
    if not home:
        return False, "未检测到 Hermes 安装"
    p = home / "config.yaml"
    text = p.read_text(encoding="utf-8")
    pat = re.compile(
        r"(platforms:\s*\n(?:\s+.+\n)*?\s+wechatauto:\s*\n\s+enabled:\s*)\w+")
    if not pat.search(text):
        return False, "config.yaml 里没有 platforms.wechatauto.enabled 段"
    new = pat.sub(rf"\g<1>{'true' if enabled else 'false'}", text, count=1)
    p.write_text(new, encoding="utf-8")
    log_line(f"hermes platform wechatauto.enabled -> {enabled}")
    return True, "已写入，重启 gateway 生效"


def hermes_inject_plugin(cfg: dict) -> tuple[bool, str]:
    """把仓库 hermes/wechatauto 插件拷进 ~/.hermes/plugins/ + 白名单。"""
    home = hermes_home(cfg)
    repo = cfg.get("repo_root") or ""
    src = Path(repo) / "hermes" / "wechatauto" if repo else None
    if not home:
        return False, "未检测到 Hermes 安装"
    if not src or not src.exists():
        return False, "manager.json 的 repo_root 未设置或插件目录不存在"
    dst = home / "plugins" / "wechatauto"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    # 确保 plugins.enabled 白名单含 wechatauto
    cfg_yaml = home / "config.yaml"
    text = cfg_yaml.read_text(encoding="utf-8") if cfg_yaml.exists() else ""
    if not re.search(r"plugins:[\s\S]*?enabled:[\s\S]*?wechatauto", text):
        if re.search(r"(plugins:\s*\n\s+enabled:\s*\n)", text):
            text = re.sub(r"(plugins:\s*\n\s+enabled:\s*\n)",
                          r"\g<1>    - wechatauto\n", text, count=1)
        else:
            text += "\nplugins:\n  enabled:\n    - wechatauto\n"
        cfg_yaml.write_text(text, encoding="utf-8")
    log_line(f"hermes plugin injected -> {dst}")
    return True, f"已注入 {dst}"


def hermes_gateway_restart(cfg: dict) -> tuple[bool, str]:
    home = hermes_home(cfg)
    if not home:
        return False, "未检测到 Hermes"
    exe = home / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
    if not exe.exists():
        return False, f"找不到 {exe}"
    try:
        rc, raw = _checked_run([str(exe), "gateway", "restart"], 120)
        out = raw.strip()[-400:]
        log_line(f"hermes gateway restart rc={rc} {out[:120]}")
        return rc == 0, out or f"rc={rc}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Octop
# ---------------------------------------------------------------------------

def octop_home(cfg: dict) -> Path | None:
    if cfg.get("octop_home"):
        p = Path(cfg["octop_home"])
        if p.exists():
            return p
    # WECHATAUTO_OCTOP_HOME：测试/多实例隔离真实 ~/.octop 用
    env = os.environ.get("WECHATAUTO_OCTOP_HOME")
    p = Path(env) if env else Path.home() / ".octop"
    return p if p.exists() else None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_jwt(secret: str | bytes, sub: str, uname: str, role: str,
             ttl: int = 3600) -> str:
    """stdlib 手写 HS256 JWT，不依赖 PyJWT。"""
    key = secret.encode() if isinstance(secret, str) else secret
    now = int(time.time())
    head = _b64url(json.dumps(
        {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(
        {"sub": str(sub), "uname": uname, "role": role,
         "iat": now, "exp": now + ttl},
        separators=(",", ":")).encode())
    sig = _b64url(hmac.new(key, f"{head}.{body}".encode(),
                           hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def _octop_jwt(cfg: dict) -> tuple[str, Path] | tuple[None, None]:
    home = octop_home(cfg)
    if not home:
        return None, None
    db = home / "octop.db"
    if not db.exists():
        return None, None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        secret = conn.execute(
            "SELECT v FROM secrets WHERE k='jwt'").fetchone()[0]
        uid, uname, role = conn.execute(
            "SELECT id, username, role FROM users ORDER BY id LIMIT 1"
        ).fetchone()
    except (sqlite3.Error, TypeError, IndexError):
        return None, None   # 残缺 DB（比如测试库没有 secrets 表）按离线处理
    finally:
        conn.close()
    return mint_jwt(secret, uid, uname, role), db


def octop_api(cfg: dict, method: str, path: str,
              body: dict | None = None) -> tuple[int, dict | str]:
    """调运行中 Octop 的本地 API。返回 (http_code, payload|error)。"""
    token, _ = _octop_jwt(cfg)
    if not token:
        return -1, "无法签发 JWT（octop.db 缺失或无权）"
    url = f"http://127.0.0.1:8088/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:300]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def octop_status(cfg: dict) -> dict:
    home = octop_home(cfg)
    st = {"installed": False, "adapter_files": False, "registered": False,
          "channel_row": False, "connected": None, "name": "",
          "channel_id": "", "home": str(home or ""), "frontend_patched": False}
    if not home:
        return st
    pkg = home / "portable" / "packages" / "octop_gateway"
    st["installed"] = pkg.exists()
    st["adapter_files"] = (
        pkg / "channels" / "wechatauto" / "channel.py").exists()
    init = pkg / "channels" / "__init__.py"
    if init.exists():
        text = init.read_text(encoding="utf-8", errors="replace")
        st["registered"] = bool(
            '"wechatauto": "octop_gateway.channels.wechatauto"' in text
            and re.search(r'WECHATAUTO\s*=\s*"wechatauto"', text)
            and '"wechatauto": "WechatAutoChannel"' in text)
    assets = home / "portable" / "packages" / "octop" / "dashboard" / "assets"
    for js in assets.glob("index.*.js"):
        if "wechatauto" in js.read_text(encoding="utf-8", errors="replace"):
            st["frontend_patched"] = True
            break
    db = home / "octop.db"
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT channel_id, name, enabled FROM channels "
                "WHERE kind='wechatauto' LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row:
            st["channel_row"] = True
            st["channel_id"], st["name"] = row[0], row[1]
    if st["channel_row"]:
        code, payload = octop_api(
            cfg, "GET", "/agents/main/channels")
        if code == 200 and isinstance(payload, list):
            for ch in payload:
                if ch.get("kind") == "wechatauto":
                    st["connected"] = ch.get("runtime", {}).get("connected")
    return st


def octop_channel_set_enabled(cfg: dict, enabled: bool) -> tuple[bool, str]:
    st = octop_status(cfg)
    cid = st.get("channel_id")
    if not cid:
        return False, "octop.db 里没有 wechatauto 渠道行（先注入）"
    code, payload = octop_api(
        cfg, "PATCH", f"/agents/main/channels/{cid}",
        body={"enabled": enabled})
    ok = code in (200, 204)
    log_line(f"octop channel enabled={enabled} -> http {code}")
    return ok, f"HTTP {code}"


def octop_restart(cfg: dict) -> tuple[bool, str]:
    code, payload = octop_api(cfg, "POST", "/update/restart")
    log_line(f"octop restart -> http {code} {payload}")
    return code in (200, 202), f"HTTP {code} {payload}"


def octop_auto_open_defaults(cfg: dict) -> list[str]:
    """新资源自动开默认——watchdog 周期调用，幂等。

    框架设计是"默认注入+模型自决策"：default_open 的资源进每轮候选，
    渠道消息没人替用户点选，所以新资源默认关 = IM 渠道永远裸奔。
    这里把三类资源的默认声明自动补齐：
      - knowledge_bases.default_open=0 → 1（列级开关）
      - connectors.config_json.default_open → true（JSON 内嵌）
      - skill_packages 新包 → 追加进 main agent 的 skill_package_ids
    返回本次翻动的资源描述列表，无翻动返回 []。
    """
    home = octop_home(cfg)
    if not home:
        return []
    db = home / "octop.db"
    if not db.exists():
        return []
    flipped: list[str] = []
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        for rid, kb_id, name in cur.execute(
                "SELECT id, knowledge_base_id, name FROM knowledge_bases"
                " WHERE default_open=0").fetchall():
            cur.execute(
                "UPDATE knowledge_bases SET default_open=1,"
                " updated_at=strftime('%s','now') WHERE id=?", (rid,))
            flipped.append(f"kb:{name or kb_id}")
        for rid, dname, cj in cur.execute(
                "SELECT id, display_name, config_json FROM connectors"
                ).fetchall():
            try:
                c = json.loads(cj) if cj else {}
            except (ValueError, TypeError):
                c = {}
            if not isinstance(c, dict):
                c = {}
            if c.get("default_open") is True:
                continue
            c["default_open"] = True
            cur.execute(
                "UPDATE connectors SET config_json=?,"
                " updated_at=strftime('%s','now') WHERE id=?",
                (json.dumps(c, ensure_ascii=False), rid))
            flipped.append(f"connector:{dname or rid}")
        pkg_rows = cur.execute(
            "SELECT skill_package_id, name FROM skill_packages").fetchall()
        if pkg_rows:
            row = cur.execute(
                "SELECT skill_package_ids FROM agents WHERE agent_id='main'"
            ).fetchone()
            bound: set[str] = set()
            if row and row[0]:
                try:
                    raw = json.loads(row[0])
                    if isinstance(raw, list):
                        bound = {str(x) for x in raw}
                except (ValueError, TypeError):
                    pass
            missing = [(pid, name) for pid, name in pkg_rows
                       if str(pid) not in bound]
            if missing:
                bound.update(str(pid) for pid, _ in missing)
                cur.execute(
                    "UPDATE agents SET skill_package_ids=?,"
                    " updated_at=strftime('%s','now') WHERE agent_id='main'",
                    (json.dumps(sorted(bound), ensure_ascii=False),))
                flipped.extend(f"skill:{name or pid}" for pid, name in missing)
        conn.commit()
    except sqlite3.Error as e:
        log_line(f"auto_open_defaults db error: {e}")
    finally:
        conn.close()
    return flipped


def octop_group_allowlist(cfg: dict) -> dict:
    """群白名单视图：桥 /chats 里的群 ∪ 当前 config_json.group_allow。

    人只见群名不见群号——GUI 展示 name，底层存 ``*@chatroom`` id。
    群来源是桥的会话表：发起过消息（被微信记录）的群天然在册。
    返回 {groups: [{id,name,allowed,stale}], enforced, error}。
    """
    st = {"groups": [], "enforced": False, "error": ""}
    home = octop_home(cfg)
    if not home:
        st["error"] = "octop home 未找到"
        return st
    db = home / "octop.db"
    allow: set[str] = set()
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT config_json FROM channels WHERE kind='wechatauto'"
                " LIMIT 1").fetchone()
            if row and row[0]:
                cj = json.loads(row[0])
                raw = cj.get("group_allow") or []
                if isinstance(raw, list):
                    allow = {str(x) for x in raw}
        except (sqlite3.Error, ValueError, TypeError):
            pass
        finally:
            conn.close()
    st["enforced"] = bool(allow)
    b = cfg["bridge"]
    req = urllib.request.Request(
        f"http://{b['host']}:{b['port']}/chats",
        headers={"Authorization": f"Bearer {b['token']}"})
    try:
        # get_sessions 会撞 DB 快照合并的锁重试——间歇几秒是常态，8s 太短
        with urllib.request.urlopen(req, timeout=15) as resp:
            chats = json.loads(resp.read().decode("utf-8")).get("chats") or []
    except Exception as e:
        st["error"] = f"桥 /chats 不可达: {e}"
        chats = []
    seen: set[str] = set()
    for c in chats:
        if c.get("type") != "group" or not c.get("id"):
            continue
        seen.add(c["id"])
        st["groups"].append({
            "id": c["id"], "name": c.get("name") or c["id"],
            "allowed": c["id"] in allow, "stale": False})
    for gid in sorted(allow - seen):  # 已退出/无记录的群：保留展示可摘除
        st["groups"].append({"id": gid, "name": gid, "allowed": True,
                             "stale": True})
    return st


def octop_group_allowlist_set(cfg: dict, ids: list[str]) -> tuple[bool, str]:
    """改 config_json.group_allow。空列表 = 关闭过滤。

    首选走 PATCH /agents/main/channels/{cid} —— update_channel 会写库并
    原地 unregister/re-register 渠道（配置热重载内建），单次原子调用。
    直接写 DB + toggle 的旧路子有个竞态：后端缓存的 config 会在渠道
    重注册时把直写的值覆盖回去（白名单"看着写了实际没生效"）。
    后端不可达时退回直写 DB，下次渠道注册时生效。
    """
    st = octop_status(cfg)
    cid = st.get("channel_id")
    if not cid:
        return False, "没有 wechatauto 渠道行（先注入）"
    ids = [str(x) for x in ids]
    # 期望值持久化到 manager.json——独立于 octop.db。dashboard 保存渠道时
    # 会整段替换 config_json 且不认识的字段会丢，watchdog 靠这份备份自愈。
    cfg["octop_allowlist"] = sorted(ids)
    try:
        save_config(cfg)
    except OSError:
        pass
    n = len(ids)
    desc = "已关闭过滤（全部群可触发）" if not ids else f"白名单 {n} 个群"
    code, detail = octop_api(cfg, "GET", f"/agents/main/channels/{cid}")
    if code == 200 and isinstance(detail, dict):
        conf = detail.get("config")
        conf = dict(conf) if isinstance(conf, dict) else {}
        conf["group_allow"] = ids
        code, _ = octop_api(
            cfg, "PATCH", f"/agents/main/channels/{cid}",
            body={"config": conf})
        ok = code in (200, 204)
        log_line(f"group_allow set via api: {sorted(ids)} -> http {code}")
        return ok, f"{desc}；渠道热重载 {'ok' if ok else f'HTTP {code}'}"
    # 后端离线：直写 DB 兜底，渠道下次注册时生效
    home = octop_home(cfg)
    if not home or not (home / "octop.db").exists():
        return False, "octop.db 未找到"
    conn = sqlite3.connect(str(home / "octop.db"))
    try:
        row = conn.execute(
            "SELECT id, config_json FROM channels WHERE kind='wechatauto'"
            " LIMIT 1").fetchone()
        try:
            c = json.loads(row[1]) if row and row[1] else {}
        except (ValueError, TypeError):
            c = {}
        if not isinstance(c, dict):
            c = {}
        c["group_allow"] = ids
        conn.execute(
            "UPDATE channels SET config_json=?,"
            " updated_at=strftime('%s','now') WHERE id=?",
            (json.dumps(c, ensure_ascii=False), row[0]))
        conn.commit()
    except sqlite3.Error as e:
        return False, f"写库失败: {e}"
    finally:
        conn.close()
    log_line(f"group_allow set via db (backend down): {sorted(ids)}")
    return True, f"{desc}；后端离线，下次启动生效"


def octop_allowlist_guard(cfg: dict) -> str | None:
    """白名单漂移自愈——watchdog 周期调用，幂等。

    ``group_allow`` 是我们塞进 channel config_json 的自定义字段，Octop
    dashboard 保存渠道时整段替换 config、不认识的字段会静默丢掉。
    期望值存在 manager.json（octop_allowlist），发现渠道 config 里的
    group_allow 与期望不一致就重放一次（走 API 路径，自带热重载）。
    返回修复描述；无漂移/未设过白名单返回 None。
    """
    home = octop_home(cfg)
    if not home or not (home / "octop.db").exists():
        return None
    desired = cfg.get("octop_allowlist")
    if not isinstance(desired, list) or not desired:
        # bootstrap：渠道上已有 group_allow（老版本手工应用过）但
        # manager.json 还没期望值——领养进 cfg，之后才有修复依据。
        adopted = _channel_group_allow(home)
        if adopted:
            cfg["octop_allowlist"] = sorted(adopted)
            try:
                save_config(cfg)
            except OSError:
                pass
        return None
    current = sorted(_channel_group_allow(home))
    want = sorted(str(x) for x in desired)
    if current == want:
        return None
    ok, msg = octop_group_allowlist_set(cfg, want)
    note = f"漂移修复 {'ok' if ok else 'failed'}: {msg}"
    log_line(f"allowlist guard: config={current} expected={want} -> {note}")
    return f"group_allow 漂移已修复（{len(want)} 个群）" if ok else None


def _channel_group_allow(home: Path) -> list[str]:
    """只读渠道 config_json 里的 group_allow（无行/无字段返回 []）。"""
    try:
        conn = sqlite3.connect(f"file:{home / 'octop.db'}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT config_json FROM channels WHERE kind='wechatauto'"
            " LIMIT 1").fetchone()
        conn.close()
        cj = json.loads(row[0]) if row and row[0] else {}
        raw = cj.get("group_allow") if isinstance(cj, dict) else None
        return [str(x) for x in raw] if isinstance(raw, list) else []
    except (sqlite3.Error, ValueError, TypeError):
        return []


# Octop 升级会整体覆盖 portable 包——本地补丁和注入的 adapter 一样会被抹。
# send_file_to_user 越界补丁：上游把 workspace 外的绝对路径抛 PermissionError
# 穿透 langgraph 杀掉整轮（生产实锤"一直处理出错"），降级为 ToolException
# 让模型读错误文本自纠。marker 幂等，watchdog 周期核查、丢了就重打。
_SEND_FILE_PATCH_MARKER = "PATCH(local)"
_SEND_FILE_PATCH_ANCHOR = (
    "        local = workspace.materialize_local(file_path)\n")
_SEND_FILE_PATCH_BLOCK = (
    "        try:\n"
    "            local = workspace.materialize_local(file_path)\n"
    "        except (PermissionError, ValueError) as exc:\n"
    "            # PATCH(local): 越界路径此前会让 PermissionError 穿透 langgraph\n"
    "            # 杀掉整轮 stream（用户端只见\"处理出错\"）。降级为 ToolException：\n"
    "            # 模型读到错误文本后可自行把文件拷进工作区再试。\n"
    "            raise ToolException(\n"
    "                f\"send_file_to_user: {exc}. Copy the file into the \"\n"
    "                \"workspace first, then retry with the workspace path.\"\n"
    "            ) from exc\n")


# send_file_to_user 群语义补丁：docstring 只写 "to the user"，模型在群聊
# 语境读成"仅私聊"而拒绝调用（2026-10-12 实锤：群里要 PDF 两轮均回
# "无法发送文件"，从未调工具）。改为"发到当前会话（群/私聊皆可）"。
_SEND_FILE_DOC_MARKER = "current chat"
_SEND_FILE_DOC_ANCHOR = (
    '        """Send a file to the user through the chat channel.\n')
_SEND_FILE_DOC_BLOCK = (
    '        """Send a file to the current chat through the channel.\n'
    "\n"
    "        Delivers to whatever conversation triggered this turn — DMs and\n"
    "        group chats alike; in a group it is posted into that group.\n")


def octop_send_file_patch(cfg: dict) -> str | None:
    """send_file_to_user 补丁存活检查（越界降级 + 群语义），缺失则重打。"""
    home = octop_home(cfg)
    if not home:
        return None
    target = (home / "portable" / "packages" / "octop_harness"
              / "builtin" / "tools" / "send_file.py")
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")
    applied: list[str] = []
    if _SEND_FILE_PATCH_MARKER not in text:
        if _SEND_FILE_PATCH_ANCHOR not in text:
            log_line("send_file patch: 越界锚点不匹配（上游已改版？）——人工核查")
        else:
            text = text.replace(
                _SEND_FILE_PATCH_ANCHOR, _SEND_FILE_PATCH_BLOCK, 1)
            applied.append("越界降级")
    if _SEND_FILE_DOC_MARKER not in text:
        if _SEND_FILE_DOC_ANCHOR not in text:
            log_line("send_file patch: docstring 锚点不匹配（上游已改版？）——人工核查")
        else:
            text = text.replace(
                _SEND_FILE_DOC_ANCHOR, _SEND_FILE_DOC_BLOCK, 1)
            applied.append("群语义 docstring")
    if not applied:
        return None
    target.write_text(text, encoding="utf-8")
    for stale in (target.parent / "__pycache__").glob("send_file*.pyc"):
        stale.unlink(missing_ok=True)
    desc = "+".join(applied)
    log_line(f"send_file patch: {desc} 已（重新）应用，后端重启后生效")
    return f"send_file_to_user {desc}补丁已重打（下次后端重启生效）"


def adapter_golden() -> Path:
    """adapter 源码金本：不依赖 dev clone 存活，注入成功时自动回写保鲜。"""
    return manager_dir() / "adapter-src" / "wechatauto"


# 兼容旧引用（测试/外部脚本可能直接读常量）
ADAPTER_GOLDEN = adapter_golden()


def _octop_adapter_src(cfg: dict) -> Path | None:
    """定位 adapter 源码目录：octop_repo 显式 > repo_root/octop/ 布局 >
    repo_root 兄弟的 octop-gateway/src/ 布局 > ~/.wechatauto 金本。"""
    rel = Path("src") / "octop_gateway" / "channels" / "wechatauto"
    if cfg.get("octop_repo"):
        cand = Path(cfg["octop_repo"]) / rel
        return cand if cand.exists() else None
    repo = cfg.get("repo_root") or ""
    if repo:
        for cand in (Path(repo) / "octop" / "octop_gateway" / "channels"
                     / "wechatauto",
                     Path(repo).parent / "octop-gateway" / rel):
            if cand.exists():
                return cand
    golden = adapter_golden()
    return golden if golden.exists() else None


def octop_inject_adapter(cfg: dict) -> tuple[bool, str]:
    """把仓库 octop adapter 拷进 portable 包 + 注册三处声明。"""
    home = octop_home(cfg)
    src = _octop_adapter_src(cfg)
    if not home:
        return False, "未检测到 Octop"
    pkg = home / "portable" / "packages" / "octop_gateway" / "channels"
    if not pkg.exists():
        return False, "octop_gateway 包不存在"
    if not src:
        return False, "找不到 adapter 源码（检查 repo_root/octop_repo）"
    dst = pkg / "wechatauto"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    golden = adapter_golden()
    if src != golden:  # 用更新鲜的源回写金本
        golden.parent.mkdir(parents=True, exist_ok=True)
        if golden.exists():
            shutil.rmtree(golden)
        shutil.copytree(src, golden,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    init = pkg / "__init__.py"
    text = init.read_text(encoding="utf-8")
    # 注册共三处，各自幂等补齐（与仓库 channels/__init__.py 对齐）
    if '"wechatauto": "octop_gateway.channels.wechatauto"' not in text:
        text = re.sub(
            r'(_CHANNEL_MAP\s*:\s*dict\[str, str\]\s*=\s*\{)',
            r'\g<1>\n    "wechatauto": "octop_gateway.channels.wechatauto",',
            text, count=1)
    if not re.search(r'WECHATAUTO\s*=\s*"wechatauto"', text):
        text = re.sub(
            r'(class ChannelKind\(StrEnum\):[\s\S]*?)(\n    \w+ = "[^"]+")'
            r'(\n\n+)',
            r'\g<1>\g<2>\n    WECHATAUTO = "wechatauto"\g<3>',
            text, count=1)
    if '"wechatauto": "WechatAutoChannel"' not in text:
        text = re.sub(
            r'(_CLASS_NAMES\s*:\s*dict\[str, str\]\s*=\s*\{)',
            r'\g<1>\n    "wechatauto": "WechatAutoChannel",',
            text, count=1)
    init.write_text(text, encoding="utf-8")
    log_line("octop adapter injected")
    return True, "adapter 已注入并注册"


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------

def openclaw_status(cfg: dict) -> dict:
    st = {"installed": False, "plugin": False, "home": ""}
    exe = shutil.which("openclaw") or shutil.which("openclaw.cmd")
    home = Path.home() / ".openclaw"
    st["home"] = str(home)
    st["installed"] = bool(exe) or home.exists()
    if home.exists():
        st["plugin"] = any(home.glob("**/openclaw-wechatauto*"))
    return st


# ---------------------------------------------------------------------------
# log sources（GUI 日志窗消费）
# ---------------------------------------------------------------------------

def log_sources(cfg: dict) -> dict[str, Path]:
    sources = {
        "manager.log": manager_log_path(),
        "bridge-fatal.log": log_dir() / "bridge-fatal.log",
        "bridge-stdout.log": bridge_stdout_log_path(),
    }
    home = hermes_home(cfg)
    if home:
        sources["hermes errors.log"] = home / "logs" / "errors.log"
        sources["hermes gateway.log"] = home / "logs" / "gateway.log"
    oh = octop_home(cfg)
    if oh:
        sources["octop.log"] = oh / "logs" / "octop.log"
    return {k: v for k, v in sources.items() if v}


def tail_lines(path: Path, n: int = 300) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
        lines = data.splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"<无法读取 {path}: {e}>"
