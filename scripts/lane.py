#!/usr/bin/env python3
"""Per-worktree test lanes for parallel agents.

A lane binds one worktree to one app port, one CDP port, one agent-browser
session, and the processes the lane starts. Lanes never share ports, so
parallel agents cannot drive each other's app. See ../SKILL.md.
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

APP_BASE = int(os.environ.get("PARALLEL_APP_TESTING_APP_BASE", "4100"))
CDP_BASE = int(os.environ.get("PARALLEL_APP_TESTING_CDP_BASE", "9300"))
SLOTS = int(os.environ.get("PARALLEL_APP_TESTING_SLOTS", "400"))
LANE_ID = re.compile(r"[0-9a-f]{12}")
# Local requests must not go through HTTP_PROXY or similar settings.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"lane: {message}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_config() -> None:
    if SLOTS < 1:
        fail("PARALLEL_APP_TESTING_SLOTS must be at least 1")
    for name, base in (("APP", APP_BASE), ("CDP", CDP_BASE)):
        if base < 1024 or base + SLOTS - 1 > 65535:
            fail(f"{name} ports {base}-{base + SLOTS - 1} fall outside 1024-65535")
    if APP_BASE < CDP_BASE + SLOTS and CDP_BASE < APP_BASE + SLOTS:
        fail("app and CDP port ranges overlap")


# State ----------------------------------------------------------------------


def state_home() -> Path:
    override = os.environ.get("PARALLEL_APP_TESTING_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "parallel-app-testing"


def lanes_dir() -> Path:
    return state_home() / "lanes"


def lane_dir(lane: dict) -> Path:
    return lanes_dir() / lane["id"]


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


def load_lane(lane_id: str) -> Optional[dict]:
    path = lanes_dir() / lane_id / "lane.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        fail(f"cannot read {path}: {error}")


def load_all_lanes() -> Dict[str, dict]:
    lanes: Dict[str, dict] = {}
    base = lanes_dir()
    if not base.is_dir():
        return lanes
    for entry in sorted(base.iterdir()):
        if LANE_ID.fullmatch(entry.name) and (entry / "lane.json").is_file():
            lane = load_lane(entry.name)
            if lane is not None:
                lanes[entry.name] = lane
    return lanes


def save_lane(lane: dict) -> None:
    directory = lane_dir(lane)
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / "lane.json.tmp"
    staging.write_text(json.dumps(lane, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, directory / "lane.json")


def update_processes(lane: dict, name: str, record: Optional[dict]) -> None:
    """Set or remove one process record, merging with the latest saved lane."""
    with registry_lock():
        fresh = load_lane(lane["id"]) or lane
        processes = fresh.setdefault("processes", {})
        if record is None:
            processes.pop(name, None)
        else:
            processes[name] = record
        save_lane(fresh)
    lane["processes"] = fresh["processes"]


def remove_lane_dir(lane_id: str) -> None:
    target = lanes_dir() / lane_id
    if not LANE_ID.fullmatch(lane_id) or target.parent != lanes_dir():
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


def lane_id_for(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def session_for(root: Path, lane_id: str) -> str:
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
    return f"lane-{slug or 'root'}-{lane_id[:6]}"


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
    """PIDs listening on the port, or None when lsof is unavailable."""
    if shutil.which("lsof") is None:
        return None
    probe = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, check=False,
    )
    return sorted({int(token) for token in probe.stdout.split() if token.isdigit()})


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
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def record_state(record: dict) -> str:
    """Return running, stopped, or foreign.

    A process group ID cannot be reused while any member of the group is alive,
    so a live group is still ours unless its leader PID now belongs to a
    process with a different start time.
    """
    if not group_alive(record["pgid"]):
        return "stopped"
    start = record.get("start")
    if start and pid_alive(record["pid"]) and ps_field(record["pid"], "lstart") != start:
        return "foreign"
    return "running"


def lane_has_live_process(lane: dict) -> bool:
    return any(record_state(record) == "running" for record in lane.get("processes", {}).values())


def terminate_group(pgid: int) -> bool:
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
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
    """Whether a listener on the port belongs to the process group; None if unknown."""
    pids = listener_pids(port)
    if not pids:
        return None
    return any(ps_field(pid, "pgid") == str(pgid) for pid in pids)


def tail(path: str, lines: int = 30) -> str:
    try:
        data = Path(path).read_bytes()[-12000:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


# Lanes ----------------------------------------------------------------------


def claim_is_active(lane: dict) -> bool:
    return Path(lane["root"]).exists() or lane_has_live_process(lane)


def pick_slot(lane_id: str, taken: Set[int]) -> int:
    start = int(lane_id, 16) % SLOTS
    for offset in range(SLOTS):
        slot = (start + offset) % SLOTS
        if slot in taken:
            continue
        app_port, cdp_port = slot_ports(slot)
        if port_free(app_port) and port_free(cdp_port):
            return slot
    fail(f"no free lane among {SLOTS} slots; run `lane.py list --prune` or stop idle lanes")


def claim_lane(root: Path, move: bool = False) -> dict:
    """Return this worktree's lane, creating it on first use.

    An existing lane keeps its slot even when its ports are busy, because the
    usual owner is this agent's own server. Use move=True to pick a new slot.
    """
    lane_id = lane_id_for(root)
    with registry_lock():
        lanes = load_all_lanes()
        taken = {
            lane["slot"] for other_id, lane in lanes.items()
            if other_id != lane_id and claim_is_active(lane)
        }
        mine = lanes.get(lane_id)
        if mine is not None and not move and 0 <= mine["slot"] < SLOTS and mine["slot"] not in taken:
            app_port, cdp_port = slot_ports(mine["slot"])
            if (mine["app_port"], mine["cdp_port"]) != (app_port, cdp_port):
                mine["app_port"], mine["cdp_port"] = app_port, cdp_port
                save_lane(mine)
            return mine
        if mine is not None and lane_has_live_process(mine):
            fail("this lane still has running processes; run `lane.py stop` before moving it")
        if mine is not None:
            taken.add(mine["slot"])
        slot = pick_slot(lane_id, taken)
        app_port, cdp_port = slot_ports(slot)
        lane = {
            "id": lane_id,
            "root": str(root),
            "slot": slot,
            "app_port": app_port,
            "cdp_port": cdp_port,
            "session": session_for(root, lane_id),
            "created_at": now_iso(),
            "processes": {},
        }
        save_lane(lane)
        return lane


def lane_values(lane: dict) -> Dict[str, str]:
    directory = lane_dir(lane)
    return {
        "LANE_ROOT": lane["root"],
        "LANE_DIR": str(directory),
        "APP_PORT": str(lane["app_port"]),
        "CDP_PORT": str(lane["cdp_port"]),
        "APP_URL": f"http://localhost:{lane['app_port']}",
        "AGENT_BROWSER_SESSION": lane["session"],
        "ELECTRON_USER_DATA_DIR": str(directory / "electron-user-data"),
    }


def launch(lane: dict, name: str, argv: List[str], display: str) -> Tuple[subprocess.Popen, dict]:
    directory = lane_dir(lane)
    (directory / "electron-user-data").mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{name}.log"
    env = dict(os.environ)
    env.update(lane_values(lane))
    env["PORT"] = str(lane["app_port"])
    with open(log_path, "ab") as log:
        log.write(f"\n--- {now_iso()} {name}: {display}\n".encode("utf-8"))
        log.flush()
        process = subprocess.Popen(
            argv, cwd=os.getcwd(), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    start = ps_field(process.pid, "lstart")
    for _ in range(5):
        if start or process.poll() is not None:
            break
        time.sleep(0.1)
        start = ps_field(process.pid, "lstart")
    record = {
        "pid": process.pid,
        "pgid": process.pid,
        "start": start,
        "cmd": display,
        "cwd": os.getcwd(),
        "log": str(log_path),
        "started_at": now_iso(),
    }
    update_processes(lane, name, record)
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


def stop_process(lane: dict, name: str) -> Tuple[bool, str]:
    record = lane.get("processes", {}).get(name)
    if record is None:
        return True, f"{name}: not started by this lane"
    state = record_state(record)
    if state == "foreign":
        update_processes(lane, name, None)
        return True, f"{name}: PID {record['pid']} now belongs to another process; left it running"
    if state == "stopped":
        update_processes(lane, name, None)
        return True, f"{name}: already stopped"
    if not terminate_group(record["pgid"]):
        return False, f"{name}: process group {record['pgid']} did not exit"
    update_processes(lane, name, None)
    return True, f"{name}: stopped PID {record['pid']}"


def reject_busy(port: int, role: str) -> NoReturn:
    fail(
        f"{role} port {port} is in use{describe_listeners(port)}. Do not kill it. If it is your own "
        "server started outside lane.py, stop it or keep using it; otherwise run `lane.py env --move`.",
        code=2,
    )


def failed_start(lane: dict, name: str, record: dict, reason: str) -> NoReturn:
    output = tail(record["log"])
    stop_process(lane, name)
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


def close_agent_browser(session: str) -> Optional[str]:
    binary = shutil.which("agent-browser")
    if binary is None:
        return None
    try:
        listing = subprocess.run(
            [binary, "session", "list", "--json"], capture_output=True, text=True, timeout=20, check=False,
        )
        sessions = json.loads(listing.stdout or "{}").get("data", {}).get("sessions", [])
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return f"agent-browser: could not list sessions ({error})"
    names = {entry if isinstance(entry, str) else entry.get("name") for entry in sessions}
    if session not in names:
        return "agent-browser: no open session"
    try:
        closing = subprocess.run(
            [binary, "--session", session, "close"], capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"agent-browser: close failed ({error})"
    if closing.returncode != 0:
        return f"agent-browser: close exited {closing.returncode}: {closing.stderr.strip()[:200]}"
    return f"agent-browser: closed session {session}"


# Commands -------------------------------------------------------------------


def cmd_env(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    lane = claim_lane(root, move=args.move)
    values = lane_values(lane)
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for key, value in values.items():
            print(f"export {key}={shlex.quote(value)}")
    if not lane_has_live_process(lane):
        for role, port in (("app", lane["app_port"]), ("CDP", lane["cdp_port"])):
            if port_listening(port):
                print(
                    f"lane: note: {role} port {port} is already in use{describe_listeners(port)}. "
                    "If that is not your own server, run `lane.py env --move`.",
                    file=sys.stderr,
                )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    lane = claim_lane(resolve_root(args.root))
    port = {"app": lane["app_port"], "cdp": lane["cdp_port"], "none": None}[args.wait]
    if args.wait == "cdp":
        def ready() -> bool:
            return cdp_version(port) is not None
    else:
        def ready() -> bool:
            return port is None or port_listening(port)

    existing = lane.get("processes", {}).get("serve")
    if existing is not None and record_state(existing) == "running":
        if not args.restart:
            if not wait_ready(None, existing, ready, args.timeout):
                fail(f"serve is running as PID {existing['pid']} but not ready; log {existing['log']}")
            print(f"serve: already running and ready, PID {existing['pid']}; log {existing['log']}")
            return 0
        stopped, message = stop_process(lane, "serve")
        print(message)
        if not stopped:
            return 1
    if port is not None and not port_free(port):
        reject_busy(port, args.wait)

    process, record = launch(lane, "serve", ["/bin/sh", "-c", args.cmd], args.cmd)
    if port is None:
        print(f"serve: started PID {record['pid']}; log {record['log']}")
        return 0
    if not wait_ready(process, record, ready, args.timeout):
        if record_state(record) == "running":
            failed_start(lane, "serve", record, f"was not ready on port {port} after {args.timeout:.0f}s")
        failed_start(lane, "serve", record, "exited before it was ready")
    if owned_by_group(port, record["pgid"]) is False:
        failed_start(
            lane, "serve", record,
            f"did not bind port {port}; it is held{describe_listeners(port)}. "
            "The command may have moved to another port",
        )
    target = lane_values(lane)["APP_URL"] if args.wait == "app" else f"CDP port {port}"
    print(f"serve: ready at {target}, PID {record['pid']}; log {record['log']}")
    return 0


def cmd_chrome(args: argparse.Namespace) -> int:
    lane = claim_lane(resolve_root(args.root))
    port = lane["cdp_port"]
    existing = lane.get("processes", {}).get("chrome")
    if existing is not None and record_state(existing) == "running":
        info = cdp_version(port)
        if info is not None:
            print(f"chrome: already running on CDP port {port} ({info.get('Browser', '?')})")
            print(f"next: agent-browser connect {port}")
            return 0
        stopped, message = stop_process(lane, "chrome")
        print(message)
        if not stopped:
            return 1
    if not port_free(port):
        reject_busy(port, "CDP")
    binary = find_chrome()
    if binary is None:
        fail("no Chrome or Chromium found; set AGENT_BROWSER_EXECUTABLE_PATH or CHROME_PATH")

    argv = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={lane_dir(lane) / 'chrome-profile'}",
        "--no-first-run",
        "--no-default-browser-check",
        # Fewer background processes per lane; the same flags Puppeteer uses by default.
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

    process, record = launch(lane, "chrome", argv, shlex.join(argv))
    if not wait_ready(process, record, lambda: cdp_version(port) is not None, args.timeout):
        failed_start(lane, "chrome", record, f"did not open CDP port {port} within {args.timeout:.0f}s")
    if owned_by_group(port, record["pgid"]) is False:
        failed_start(lane, "chrome", record, f"CDP port {port} is held{describe_listeners(port)}")
    info = cdp_version(port) or {}
    print(f"chrome: ready on CDP port {port} ({info.get('Browser', '?')}), PID {record['pid']}")
    print(f"next: agent-browser connect {port}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    lane = load_lane(lane_id_for(root))
    if lane is None:
        print(f"no lane for {root}")
        return 0
    ok = True
    closed = close_agent_browser(lane["session"])
    if closed:
        print(closed)
    for name in list(lane.get("processes", {})):
        stopped, message = stop_process(lane, name)
        ok = ok and stopped
        print(message)
    if args.release:
        if not ok:
            fail("not releasing the lane while a process is still running")
        with registry_lock():
            remove_lane_dir(lane["id"])
        print(f"released lane {lane['session']} and deleted {lane_dir(lane)}")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    lane = load_lane(lane_id_for(root))
    if lane is None:
        print(f"no lane for {root}; claim one with: eval \"$(python3 {Path(__file__).resolve()} env)\"")
        return 1
    print(f"lane     {lane['session']}  slot {lane['slot']}")
    print(f"root     {lane['root']}")
    app_state = "listening" if port_listening(lane["app_port"]) else "free"
    print(f"app      {lane_values(lane)['APP_URL']}  {app_state}{describe_listeners(lane['app_port'])}")
    info = cdp_version(lane["cdp_port"])
    cdp_state = f"CDP up ({info.get('Browser', '?')})" if info else "no CDP endpoint"
    print(f"cdp      {lane['cdp_port']}  {cdp_state}")
    processes = lane.get("processes", {})
    if not processes:
        print("process  none")
    for name, record in processes.items():
        print(f"process  {name}: {record_state(record)}, PID {record['pid']}, log {record['log']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    lanes = load_all_lanes()
    if args.prune:
        with registry_lock():
            for lane_id, lane in list(lanes.items()):
                if not Path(lane["root"]).exists() and not lane_has_live_process(lane):
                    remove_lane_dir(lane_id)
                    print(f"pruned {lane['session']} ({lane['root']})")
                    del lanes[lane_id]
    if not lanes:
        print("no lanes")
        return 0
    for lane in sorted(lanes.values(), key=lambda item: item["slot"]):
        running = [name for name, record in lane.get("processes", {}).items() if record_state(record) == "running"]
        missing = "" if Path(lane["root"]).exists() else "  (worktree missing)"
        print(
            f"{lane['app_port']:>5} {lane['cdp_port']:>5}  {lane['session']:<36} "
            f"{','.join(running) or '-':<14} {lane['root']}{missing}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    validate_config()
    parser = argparse.ArgumentParser(prog="lane.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="worktree path (default: the Git worktree of the current directory)")
    commands = parser.add_subparsers(dest="command", required=True)

    env = commands.add_parser("env", help="claim this worktree's lane and print shell exports")
    env.add_argument("--json", action="store_true", help="print JSON instead of export lines")
    env.add_argument("--move", action="store_true", help="move the lane to a new slot with free ports")
    env.set_defaults(handler=cmd_env)

    serve = commands.add_parser("serve", help="start the dev server or Electron app in the background")
    serve.add_argument("--cmd", required=True, help="shell command; runs with PORT=$APP_PORT and lane variables")
    serve.add_argument("--wait", choices=("app", "cdp", "none"), default="app",
                       help="readiness check: app port (default), CDP endpoint (Electron), or none")
    serve.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for readiness")
    serve.add_argument("--restart", action="store_true", help="stop the running server first")
    serve.set_defaults(handler=cmd_serve)

    chrome = commands.add_parser("chrome", help="start a dedicated Chrome on this lane's CDP port")
    chrome.add_argument("--headed", action="store_true", help="show the browser window")
    chrome.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the CDP endpoint")
    chrome.set_defaults(handler=cmd_chrome)

    stop = commands.add_parser("stop", help="close the session and stop processes this lane started")
    stop.add_argument("--release", action="store_true", help="also drop the port claim and delete lane files")
    stop.set_defaults(handler=cmd_stop)

    status = commands.add_parser("status", help="show this worktree's lane")
    status.set_defaults(handler=cmd_status)

    listing = commands.add_parser("list", help="show every lane on this machine")
    listing.add_argument("--prune", action="store_true", help="drop lanes whose worktree no longer exists")
    listing.set_defaults(handler=cmd_list)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
