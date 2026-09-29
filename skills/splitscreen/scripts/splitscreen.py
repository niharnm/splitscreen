#!/usr/bin/env python3
"""Splitscreen: give every coding agent its own screen of your app.

A screen binds one worktree to one app port, one CDP port, one agent-browser
session, and the processes it starts. Screens never share ports, so parallel
agents cannot drive each other's app. See ../SKILL.md.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, NoReturn, Optional, Set, Tuple

APP_BASE = int(os.environ.get("SPLITSCREEN_APP_BASE", "4100"))
CDP_BASE = int(os.environ.get("SPLITSCREEN_CDP_BASE", "9300"))
SLOTS = int(os.environ.get("SPLITSCREEN_SLOTS", "400"))
SCREEN_ID = re.compile(r"[0-9a-f]{12}")
# Local requests must not go through HTTP_PROXY or similar settings.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"splitscreen: {message}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_config() -> None:
    if SLOTS < 1:
        fail("SPLITSCREEN_SLOTS must be at least 1")
    for name, base in (("APP", APP_BASE), ("CDP", CDP_BASE)):
        if base < 1024 or base + SLOTS - 1 > 65535:
            fail(f"{name} ports {base}-{base + SLOTS - 1} fall outside 1024-65535")
    if APP_BASE < CDP_BASE + SLOTS and CDP_BASE < APP_BASE + SLOTS:
        fail("app and CDP port ranges overlap")


# State ----------------------------------------------------------------------


def state_home() -> Path:
    override = os.environ.get("SPLITSCREEN_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "splitscreen"


def screens_dir() -> Path:
    return state_home() / "screens"


def screen_dir(screen: dict) -> Path:
    return screens_dir() / screen["id"]


@contextlib.contextmanager
def registry_lock() -> Iterator[None]:
    home = state_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "registry.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_screen(screen_id: str) -> Optional[dict]:
    path = screens_dir() / screen_id / "screen.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        fail(f"cannot read {path}: {error}")


def load_all_screens() -> Dict[str, dict]:
    screens: Dict[str, dict] = {}
    base = screens_dir()
    if not base.is_dir():
        return screens
    for entry in sorted(base.iterdir()):
        if SCREEN_ID.fullmatch(entry.name) and (entry / "screen.json").is_file():
            screen = load_screen(entry.name)
            if screen is not None:
                screens[entry.name] = screen
    return screens


def save_screen(screen: dict) -> None:
    directory = screen_dir(screen)
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / "screen.json.tmp"
    staging.write_text(json.dumps(screen, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, directory / "screen.json")


@contextlib.contextmanager
def launch_lock(screen: dict) -> Iterator[None]:
    """Serialize starting and stopping processes within one screen."""
    directory = screen_dir(screen)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "launch.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_processes(screen: dict, name: str, record: Optional[dict]) -> None:
    """Set or remove one process record, merging with the latest saved screen."""
    with registry_lock():
        fresh = load_screen(screen["id"]) or screen
        processes = fresh.setdefault("processes", {})
        if record is None:
            processes.pop(name, None)
        else:
            processes[name] = record
        save_screen(fresh)
    screen["processes"] = fresh["processes"]


def remove_screen_dir(screen_id: str) -> None:
    target = screens_dir() / screen_id
    if not SCREEN_ID.fullmatch(screen_id) or target.parent != screens_dir():
        fail(f"refusing to remove unexpected path {target}")
    shutil.rmtree(target, ignore_errors=False)


# Identity -------------------------------------------------------------------


def resolve_root(explicit: Optional[str]) -> Path:
    start = Path(explicit).expanduser() if explicit else Path.cwd()
    if not start.is_dir():
        fail(f"not a directory: {start}")
    try:
        probe = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return start.resolve()
    if probe.returncode == 0 and probe.stdout.strip():
        return Path(probe.stdout.strip()).resolve()
    return start.resolve()


def screen_id_for(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def session_for(root: Path, screen_id: str) -> str:
    # The branch tells worktrees apart when they share a folder name.
    label = root.name
    try:
        branch = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        branch = ""
    if branch:
        label = branch
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24].strip("-")
    return f"screen-{slug or 'root'}-{screen_id[:6]}"


# Ports ----------------------------------------------------------------------


def slot_ports(slot: int) -> Tuple[int, int]:
    return APP_BASE + slot, CDP_BASE + slot


def port_listening(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def port_bindable(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def port_free(port: int) -> bool:
    return not port_listening(port) and port_bindable(port)


def listener_pids(port: int) -> Optional[List[int]]:
    """PIDs this user can see listening on the port, or None when no tool can list them."""
    if shutil.which("lsof"):
        probe = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, check=False,
        )
        return sorted({int(token) for token in probe.stdout.split() if token.isdigit()})
    if shutil.which("ss"):
        probe = subprocess.run(
            ["ss", "-Hltnp", "sport", "=", f":{port}"], capture_output=True, text=True, check=False,
        )
        return sorted({int(pid) for pid in re.findall(r"pid=(\d+)", probe.stdout)})
    return None


def describe_listeners(port: int) -> str:
    pids = listener_pids(port)
    if not pids:
        return ""
    described = []
    for pid in pids:
        command = ps_field(pid, "command") or "?"
        described.append(f"PID {pid} ({command[:80]})")
    return " by " + ", ".join(described)


def cdp_version(port: int) -> Optional[dict]:
    try:
        with DIRECT.open(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


# Processes ------------------------------------------------------------------


def ps_field(pid: int, field: str) -> Optional[str]:
    probe = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, check=False,
    )
    value = " ".join(probe.stdout.split())
    return value or None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    """Whether the group has a member that has not exited.

    Exited but unreaped members (zombies) do not count. macOS answers EPERM when
    signaling a group of only zombies, while Linux reports it as alive.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    probe = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, check=False)
    for line in probe.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid) and not parts[1].startswith("Z"):
            return True
    return False


def record_state(record: dict) -> str:
    """Return running, stopped, foreign, or unverified.

    Ownership is proven only by the recorded leader PID still running with its
    recorded start time. A live group whose leader is gone, or a record with no
    start time, is unverified: the group ID may have been reused after the
    original group exited, so it must not be signaled automatically.
    """
    if not group_alive(record["pgid"]):
        return "stopped"
    start = record.get("start")
    if not start or not pid_alive(record["pid"]):
        return "unverified"
    if ps_field(record["pid"], "lstart") != start:
        return "foreign"
    return "running"


def screen_has_live_process(screen: dict) -> bool:
    return any(
        record_state(record) in ("running", "unverified") for record in screen.get("processes", {}).values()
    )


def group_members(pgid: int) -> List[Tuple[int, str]]:
    probe = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,command="], capture_output=True, text=True, check=False,
    )
    members = []
    for line in probe.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[1] == str(pgid):
            members.append((int(parts[0]), parts[2] if len(parts) == 3 else ""))
    return members


def terminate_group(pgid: int) -> bool:
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return not group_alive(pgid)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # Reap group members that are children of this process, if any.
            with contextlib.suppress(ChildProcessError):
                os.waitpid(-pgid, os.WNOHANG)
            if not group_alive(pgid):
                return True
            time.sleep(0.2)
    return not group_alive(pgid)


def owned_by_group(port: int, pgid: int) -> Optional[bool]:
    """Whether every listener on the port is in the process group.

    Returns None when no tool can list listeners. No visible listener on a
    listening port means its owner is a process this user cannot inspect,
    which is never one the screen started.
    """
    pids = listener_pids(port)
    if pids is None:
        return None
    if not pids:
        return False if port_listening(port) else None
    return all(ps_field(pid, "pgid") == str(pgid) for pid in pids)


def tail(path: str, lines: int = 30) -> str:
    try:
        data = Path(path).read_bytes()[-12000:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


# Screens ----------------------------------------------------------------------


def claim_is_active(screen: dict) -> bool:
    return Path(screen["root"]).exists() or screen_has_live_process(screen)


def pick_slot(screen_id: str, taken: Set[int]) -> int:
    start = int(screen_id, 16) % SLOTS
    for offset in range(SLOTS):
        slot = (start + offset) % SLOTS
        if slot in taken:
            continue
        app_port, cdp_port = slot_ports(slot)
        if port_free(app_port) and port_free(cdp_port):
            return slot
    fail(f"no free screen among {SLOTS} slots; run `splitscreen.py list --prune` or stop idle screens")


def claim_screen(root: Path, move: bool = False) -> dict:
    """Return this worktree's screen, creating it on first use.

    An existing screen keeps its slot even when its ports are busy, because the
    usual owner is this agent's own server. Use move=True to pick a new slot.
    """
    screen_id = screen_id_for(root)
    with registry_lock():
        screens = load_all_screens()
        taken = {
            screen["slot"] for other_id, screen in screens.items()
            if other_id != screen_id and claim_is_active(screen)
        }
        mine = screens.get(screen_id)
        if mine is not None and not move and 0 <= mine["slot"] < SLOTS and mine["slot"] not in taken:
            app_port, cdp_port = slot_ports(mine["slot"])
            if (mine["app_port"], mine["cdp_port"]) != (app_port, cdp_port):
                if screen_has_live_process(mine):
                    # Its processes are bound to the recorded ports; moving the numbers would orphan them.
                    print(f"splitscreen: note: keeping ports {mine['app_port']}/{mine['cdp_port']} while processes run; "
                          "stop the screen to apply the new port bases", file=sys.stderr)
                else:
                    mine["app_port"], mine["cdp_port"] = app_port, cdp_port
                    save_screen(mine)
            return mine
        if mine is not None and screen_has_live_process(mine):
            fail("this screen still has running processes; run `splitscreen.py stop` before moving it")
        if mine is not None:
            taken.add(mine["slot"])
        slot = pick_slot(screen_id, taken)
        app_port, cdp_port = slot_ports(slot)
        screen = {
            "id": screen_id,
            "root": str(root),
            "slot": slot,
            "app_port": app_port,
            "cdp_port": cdp_port,
            "session": session_for(root, screen_id),
            "created_at": now_iso(),
            "processes": {},
        }
        save_screen(screen)
        return screen


def screen_values(screen: dict) -> Dict[str, str]:
    directory = screen_dir(screen)
    return {
        "SCREEN_ROOT": screen["root"],
        "SCREEN_DIR": str(directory),
        "APP_PORT": str(screen["app_port"]),
        "CDP_PORT": str(screen["cdp_port"]),
        "APP_URL": f"http://localhost:{screen['app_port']}",
        "AGENT_BROWSER_SESSION": screen["session"],
        "ELECTRON_USER_DATA_DIR": str(directory / "electron-user-data"),
    }


def launch(screen: dict, name: str, argv: List[str], display: str) -> Tuple[subprocess.Popen, dict]:
    directory = screen_dir(screen)
    (directory / "electron-user-data").mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{name}.log"
    env = dict(os.environ)
    env.update(screen_values(screen))
    env["PORT"] = str(screen["app_port"])
    with open(log_path, "ab") as log:
        log.write(f"\n--- {now_iso()} {name}: {display}\n".encode("utf-8"))
        log.flush()
        process = subprocess.Popen(
            argv, cwd=os.getcwd(), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    start = ps_field(process.pid, "lstart")
    for _ in range(20):
        if start or process.poll() is not None:
            break
        time.sleep(0.1)
        start = ps_field(process.pid, "lstart")
    if not start and process.poll() is None:
        terminate_group(process.pid)
        fail(f"could not read the start time of PID {process.pid}, so it could not be tracked; stopped it")
    record = {
        "pid": process.pid,
        "pgid": process.pid,
        "start": start,
        "cmd": display,
        "cwd": os.getcwd(),
        "log": str(log_path),
        "started_at": now_iso(),
    }
    try:
        update_processes(screen, name, record)
    except (OSError, SystemExit) as error:
        # Without a saved record nothing could stop this group later.
        terminate_group(process.pid)
        fail(f"could not record {name} ({error}); stopped PID {process.pid}")
    return process, record


def wait_ready(process: Optional[subprocess.Popen], record: dict, ready: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        if process is not None:
            process.poll()
        if not group_alive(record["pgid"]):
            return False
        time.sleep(0.4)
    return ready()


def stop_process(screen: dict, name: str, force: bool = False) -> Tuple[bool, str]:
    record = screen.get("processes", {}).get(name)
    if record is None:
        return True, f"{name}: not started by this screen"
    state = record_state(record)
    if state == "foreign":
        update_processes(screen, name, None)
        return True, f"{name}: PID {record['pid']} now belongs to another process; left it running"
    if state == "stopped":
        update_processes(screen, name, None)
        return True, f"{name}: already stopped"
    if state == "unverified" and not force:
        members = "; ".join(f"PID {pid} {command[:80]}" for pid, command in group_members(record["pgid"]))
        return False, (
            f"{name}: leader PID {record['pid']} is gone but process group {record['pgid']} is still alive "
            f"({members or 'no members listed'}). Ownership cannot be verified, so nothing was signaled. "
            "If these are your processes, run `splitscreen.py stop --force`."
        )
    if not terminate_group(record["pgid"]):
        return False, f"{name}: process group {record['pgid']} did not exit"
    update_processes(screen, name, None)
    return True, f"{name}: stopped PID {record['pid']}"


def reject_busy(port: int, role: str) -> NoReturn:
    fail(
        f"{role} port {port} is in use{describe_listeners(port)}. Do not kill it. If it is your own "
        "server started outside splitscreen.py, stop it or keep using it; otherwise run `splitscreen.py env --move`.",
        code=2,
    )


def require_owner(screen: dict, name: str, record: dict, port: int, launched: bool) -> None:
    owner = owned_by_group(port, record["pgid"])
    if owner is None:
        print(f"splitscreen: warning: cannot check who listens on port {port}; install lsof (or ss on Linux) "
              "to verify it belongs to this screen", file=sys.stderr)
        return
    if owner:
        return
    held = describe_listeners(port) or " by a process this user cannot inspect"
    if launched:
        failed_start(screen, name, record,
                     f"did not bind port {port}; it is held{held}. The command may have moved to another port")
    fail(f"{name} is running as PID {record['pid']}, but port {port} is held{held}. "
         "Run `splitscreen.py stop`, then start it again")


def failed_start(screen: dict, name: str, record: dict, reason: str) -> NoReturn:
    output = tail(record["log"])
    # This invocation just launched the group, so it is known to be ours.
    stop_process(screen, name, force=True)
    if output:
        print(output, file=sys.stderr)
    fail(f"{name} {reason}; full log: {record['log']}")


# Browser discovery ------------------------------------------------------------


def find_chrome() -> Optional[str]:
    for variable in ("AGENT_BROWSER_EXECUTABLE_PATH", "CHROME_PATH"):
        candidate = os.environ.get(variable)
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def close_agent_browser(session: str) -> Tuple[bool, Optional[str]]:
    """Close the screen's agent-browser session. Returns (succeeded, message)."""
    binary = shutil.which("agent-browser")
    if binary is None:
        return True, None
    try:
        listing = subprocess.run(
            [binary, "session", "list", "--json"], capture_output=True, text=True, timeout=20, check=False,
        )
        sessions = json.loads(listing.stdout or "{}").get("data", {}).get("sessions", [])
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return False, f"agent-browser: could not list sessions ({error})"
    names = {entry if isinstance(entry, str) else entry.get("name") for entry in sessions}
    if session not in names:
        return True, "agent-browser: no open session"
    try:
        closing = subprocess.run(
            [binary, "--session", session, "close"], capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"agent-browser: close failed ({error})"
    if closing.returncode != 0:
        return False, f"agent-browser: close exited {closing.returncode}: {closing.stderr.strip()[:200]}"
    return True, f"agent-browser: closed session {session}"


# Commands -------------------------------------------------------------------


def cmd_env(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    screen = claim_screen(root, move=args.move)
    values = screen_values(screen)
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for key, value in values.items():
            print(f"export {key}={shlex.quote(value)}")
    if not screen_has_live_process(screen):
        for role, port in (("app", screen["app_port"]), ("CDP", screen["cdp_port"])):
            if port_listening(port):
                print(
                    f"splitscreen: note: {role} port {port} is already in use{describe_listeners(port)}. "
                    "If that is not your own server, run `splitscreen.py env --move`.",
                    file=sys.stderr,
                )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    screen = claim_screen(resolve_root(args.root))
    port = {"app": screen["app_port"], "cdp": screen["cdp_port"], "none": None}[args.wait]
    if args.wait == "cdp":
        def ready() -> bool:
            return cdp_version(port) is not None
    else:
        def ready() -> bool:
            return port is None or port_listening(port)

    process: Optional[subprocess.Popen] = None
    with launch_lock(screen):
        screen = load_screen(screen["id"]) or screen
        existing = screen.get("processes", {}).get("serve")
        state = record_state(existing) if existing is not None else "stopped"
        if state == "unverified":
            fail("an earlier serve group is still alive but unverified; check it with `splitscreen.py status`, "
                 "then run `splitscreen.py stop --force`")
        if state == "running" and args.restart:
            stopped, message = stop_process(screen, "serve")
            print(message)
            if not stopped:
                return 1
            state = "stopped"
        if state == "running":
            record = existing
        else:
            if port is not None and not port_free(port):
                reject_busy(port, args.wait)
            process, record = launch(screen, "serve", ["/bin/sh", "-c", args.cmd], args.cmd)

    if process is None:
        if not wait_ready(None, record, ready, args.timeout):
            fail(f"serve is running as PID {record['pid']} but not ready; log {record['log']}")
        if port is not None:
            require_owner(screen, "serve", record, port, launched=False)
        print(f"serve: already running and ready, PID {record['pid']}; log {record['log']}")
        return 0
    if port is None:
        print(f"serve: started PID {record['pid']}; log {record['log']}")
        return 0
    if not wait_ready(process, record, ready, args.timeout):
        if record_state(record) == "running":
            failed_start(screen, "serve", record, f"was not ready on port {port} after {args.timeout:.0f}s")
        failed_start(screen, "serve", record, "exited before it was ready")
    require_owner(screen, "serve", record, port, launched=True)
    target = screen_values(screen)["APP_URL"] if args.wait == "app" else f"CDP port {port}"
    print(f"serve: ready at {target}, PID {record['pid']}; log {record['log']}")
    return 0


def cmd_chrome(args: argparse.Namespace) -> int:
    screen = claim_screen(resolve_root(args.root))
    port = screen["cdp_port"]
    process: Optional[subprocess.Popen] = None
    with launch_lock(screen):
        screen = load_screen(screen["id"]) or screen
        existing = screen.get("processes", {}).get("chrome")
        state = record_state(existing) if existing is not None else "stopped"
        if state == "unverified":
            fail("an earlier chrome group is still alive but unverified; check it with `splitscreen.py status`, "
                 "then run `splitscreen.py stop --force`")
        if state == "running" and cdp_version(port) is None:
            stopped, message = stop_process(screen, "chrome")
            print(message)
            if not stopped:
                return 1
            state = "stopped"
        if state == "running":
            record = existing
        else:
            if not port_free(port):
                reject_busy(port, "CDP")
            binary = find_chrome()
            if binary is None:
                fail("no Chrome or Chromium found; set AGENT_BROWSER_EXECUTABLE_PATH or CHROME_PATH")
            argv = [
                binary,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={screen_dir(screen) / 'chrome-profile'}",
                "--no-first-run",
                "--no-default-browser-check",
                # Fewer background processes per screen; the same flags Puppeteer uses by default.
                "--disable-background-networking",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
                "--disable-sync",
            ]
            if not args.headed:
                argv.append("--headless=new")
            if sys.platform.startswith("linux") and os.geteuid() == 0:
                argv.append("--no-sandbox")
            argv.append("about:blank")
            process, record = launch(screen, "chrome", argv, shlex.join(argv))

    if process is None:
        require_owner(screen, "chrome", record, port, launched=False)
        info = cdp_version(port) or {}
        print(f"chrome: already running on CDP port {port} ({info.get('Browser', '?')})")
        print(f"next: agent-browser connect {port}")
        return 0
    if not wait_ready(process, record, lambda: cdp_version(port) is not None, args.timeout):
        failed_start(screen, "chrome", record, f"did not open CDP port {port} within {args.timeout:.0f}s")
    require_owner(screen, "chrome", record, port, launched=True)
    info = cdp_version(port) or {}
    print(f"chrome: ready on CDP port {port} ({info.get('Browser', '?')}), PID {record['pid']}")
    print(f"next: agent-browser connect {port}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    screen = load_screen(screen_id_for(root))
    if screen is None:
        print(f"no screen for {root}")
        return 0
    ok, closed = close_agent_browser(screen["session"])
    if closed:
        print(closed)
    with launch_lock(screen):
        screen = load_screen(screen["id"]) or screen
        for name in list(screen.get("processes", {})):
            stopped, message = stop_process(screen, name, force=args.force)
            ok = ok and stopped
            print(message)
    if args.release:
        if not ok:
            fail("not releasing the screen while a process or browser session is still open")
        with registry_lock():
            remove_screen_dir(screen["id"])
        print(f"released screen {screen['session']} and deleted {screen_dir(screen)}")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    screen = load_screen(screen_id_for(root))
    if screen is None:
        print(f"no screen for {root}; claim one with: eval \"$(python3 {Path(__file__).resolve()} env)\"")
        return 1
    print(f"screen     {screen['session']}  slot {screen['slot']}")
    print(f"root     {screen['root']}")
    app_state = "listening" if port_listening(screen["app_port"]) else "free"
    print(f"app      {screen_values(screen)['APP_URL']}  {app_state}{describe_listeners(screen['app_port'])}")
    info = cdp_version(screen["cdp_port"])
    cdp_state = f"CDP up ({info.get('Browser', '?')})" if info else "no CDP endpoint"
    print(f"cdp      {screen['cdp_port']}  {cdp_state}")
    processes = screen.get("processes", {})
    if not processes:
        print("process  none")
    for name, record in processes.items():
        print(f"process  {name}: {record_state(record)}, PID {record['pid']}, log {record['log']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.prune:
        with registry_lock():
            # Decide from state read under the lock, not from an earlier snapshot.
            for screen_id, screen in load_all_screens().items():
                if not Path(screen["root"]).exists() and not screen_has_live_process(screen):
                    remove_screen_dir(screen_id)
                    print(f"pruned {screen['session']} ({screen['root']})")
    screens = load_all_screens()
    if not screens:
        print("no screens")
        return 0
    for screen in sorted(screens.values(), key=lambda item: item["slot"]):
        running = [name for name, record in screen.get("processes", {}).items() if record_state(record) == "running"]
        missing = "" if Path(screen["root"]).exists() else "  (worktree missing)"
        print(
            f"{screen['app_port']:>5} {screen['cdp_port']:>5}  {screen['session']:<36} "
            f"{','.join(running) or '-':<14} {screen['root']}{missing}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    validate_config()
    parser = argparse.ArgumentParser(prog="splitscreen.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="worktree path (default: the Git worktree of the current directory)")
    commands = parser.add_subparsers(dest="command", required=True)

    env = commands.add_parser("env", help="claim this worktree's screen and print shell exports")
    env.add_argument("--json", action="store_true", help="print JSON instead of export lines")
    env.add_argument("--move", action="store_true", help="move the screen to a new slot with free ports")
    env.set_defaults(handler=cmd_env)

    serve = commands.add_parser("serve", help="start the dev server or Electron app in the background")
    serve.add_argument("--cmd", required=True, help="shell command; runs with PORT=$APP_PORT and screen variables")
    serve.add_argument("--wait", choices=("app", "cdp", "none"), default="app",
                       help="readiness check: app port (default), CDP endpoint (Electron), or none")
    serve.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for readiness")
    serve.add_argument("--restart", action="store_true", help="stop the running server first")
    serve.set_defaults(handler=cmd_serve)

    chrome = commands.add_parser("chrome", help="start a dedicated Chrome on this screen's CDP port")
    chrome.add_argument("--headed", action="store_true", help="show the browser window")
    chrome.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the CDP endpoint")
    chrome.set_defaults(handler=cmd_chrome)

    stop = commands.add_parser("stop", help="close the session and stop processes this screen started")
    stop.add_argument("--release", action="store_true", help="also drop the port claim and delete screen files")
    stop.add_argument("--force", action="store_true",
                      help="also stop process groups whose leader exited, after you have checked them")
    stop.set_defaults(handler=cmd_stop)

    status = commands.add_parser("status", help="show this worktree's screen")
    status.set_defaults(handler=cmd_status)

    listing = commands.add_parser("list", help="show every screen on this machine")
    listing.add_argument("--prune", action="store_true", help="drop screens whose worktree no longer exists")
    listing.set_defaults(handler=cmd_list)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
