#!/usr/bin/env python3
"""Tests for splitscreen.py. Run from the repository root: python3 -m unittest discover -s tests"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "splitscreen" / "scripts" / "splitscreen.py"
sys.path.insert(0, str(SCRIPT.parent))
import splitscreen as screen_module  # noqa: E402


def listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # PermissionError: macOS reports it for a group of exited, unreaped members.
        pass


def group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_group_exit(pgid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while group_is_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not group_is_alive(pgid)


class ScreenTest(unittest.TestCase):
    slots = 4

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="splitscreen-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app_base = random.randrange(20000, 29000, 10)
        self.cdp_base = self.app_base + 6000
        self.env = dict(os.environ)
        self.env.update({
            "SPLITSCREEN_HOME": str(self.tmp / "state"),
            "SPLITSCREEN_APP_BASE": str(self.app_base),
            "SPLITSCREEN_CDP_BASE": str(self.cdp_base),
            "SPLITSCREEN_SLOTS": str(self.slots),
        })
        self.env.pop("AGENT_BROWSER_SESSION", None)
        # A bare listening socket stands in for a dev server. python -m http.server resolves
        # the host name before it listens, which stalls for over 30 seconds on some CI hosts.
        self.listener = self.tmp / "listen.py"
        self.listener.write_text(
            "import os, socket, time\n"
            "server = socket.socket()\n"
            "server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "server.bind(('127.0.0.1', int(os.environ['APP_PORT'])))\n"
            "server.listen()\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        self.serve_cmd = f'exec "{sys.executable}" "{self.listener}"'

    def run_cli(self, *args: str, cwd: Path, expect: int = 0, timeout: float = 90,
                 env: Optional[dict] = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=cwd, env=env or self.env,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != expect:
            self.fail(f"splitscreen.py {' '.join(args)} exited {result.returncode}, expected {expect}:\n"
                      f"{result.stdout}\n{result.stderr}")
        return result

    def make_root(self, name: str) -> Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        return root

    def claim(self, root: Path) -> dict:
        values = json.loads(self.run_cli("env", "--json", cwd=root).stdout)
        self.addCleanup(self.run_cli, "stop", cwd=root, expect=0)
        return values

    def screen_file(self, root: Path) -> Path:
        screen_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        return self.tmp / "state" / "screens" / screen_id / "screen.json"

    def test_env_is_stable_and_shell_safe(self) -> None:
        root = self.make_root("agent one")
        first = self.claim(root)
        second = json.loads(self.run_cli("env", "--json", cwd=root).stdout)
        self.assertEqual(first, second)
        app_port, cdp_port = int(first["APP_PORT"]), int(first["CDP_PORT"])
        self.assertEqual(app_port - self.app_base, cdp_port - self.cdp_base)
        self.assertEqual(first["APP_URL"], f"http://localhost:{app_port}")
        self.assertTrue(first["AGENT_BROWSER_SESSION"].startswith("screen-agent-one-"))
        exports = self.run_cli("env", cwd=root).stdout
        shell = subprocess.run(
            ["/bin/sh", "-c", exports + '\nprintf "%s|%s" "$SCREEN_ROOT" "$APP_PORT"'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(shell.stdout, f"{root}|{app_port}")

    def test_worktrees_get_distinct_slots_until_full(self) -> None:
        ports = {self.claim(self.make_root(f"wt{index}"))["APP_PORT"] for index in range(self.slots)}
        self.assertEqual(len(ports), self.slots)
        extra = self.make_root("one-too-many")
        result = self.run_cli("env", cwd=extra, expect=1)
        self.assertIn("no free screen", result.stderr)

    def test_new_screen_skips_busy_ports(self) -> None:
        root = self.make_root("busy")
        start = int(hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12], 16) % self.slots
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", self.app_base + start))
        blocker.listen()
        values = self.claim(root)
        self.assertNotEqual(int(values["APP_PORT"]), self.app_base + start)

    def test_git_subdirectory_and_worktree_resolution(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        repo = self.make_root("repo")
        git = ["git", "-c", "user.name=Screen Test", "-c", "user.email=screen@example.invalid"]
        subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
        (repo / "sub").mkdir()
        (repo / "sub" / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run([*git, "add", "."], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
        worktree = self.tmp / "repo-agent-b"
        subprocess.run([*git, "worktree", "add", "-q", str(worktree)], cwd=repo, check=True)
        at_root = self.claim(repo)
        in_subdir = json.loads(self.run_cli("env", "--json", cwd=repo / "sub").stdout)
        in_worktree = self.claim(worktree.resolve())
        self.assertEqual(at_root, in_subdir)
        self.assertEqual(in_worktree["SCREEN_ROOT"], str(worktree.resolve()))
        self.assertNotEqual(at_root["APP_PORT"], in_worktree["APP_PORT"])

    def test_serve_status_reuse_and_stop(self) -> None:
        root = self.make_root("server")
        values = self.claim(root)
        port = int(values["APP_PORT"])
        command = self.serve_cmd
        started = self.run_cli("serve", "--cmd", command, "--timeout", "30", cwd=root)
        self.assertIn(f"ready at http://localhost:{port}", started.stdout)
        self.assertTrue(listening(port))
        pid = json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"]["serve"]["pid"]
        self.addCleanup(kill_group, pid)
        again = self.run_cli("serve", "--cmd", command, cwd=root)
        self.assertIn("already running and ready", again.stdout)
        status = self.run_cli("status", cwd=root)
        self.assertIn("serve: running", status.stdout)
        stopped = self.run_cli("stop", cwd=root)
        self.assertIn(f"serve: stopped PID {pid}", stopped.stdout)
        self.assertFalse(listening(port))
        self.assertEqual(json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"], {})

    def test_serve_refuses_port_held_by_another_process(self) -> None:
        root = self.make_root("taken")
        port = int(self.claim(root)["APP_PORT"])
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        result = self.run_cli("serve", "--cmd", "sleep 30", cwd=root, expect=2)
        self.assertIn("is in use", result.stderr)
        self.assertEqual(json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"], {})

    @unittest.skipIf(shutil.which("lsof") is None, "lsof not installed")
    def test_serve_fails_when_listener_is_outside_the_screen(self) -> None:
        root = self.make_root("escaped")
        port = int(self.claim(root)["APP_PORT"])
        detach = self.tmp / "detach.py"
        detach.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(self.listener)!r}], start_new_session=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        result = self.run_cli("serve", "--cmd", f'"{sys.executable}" "{detach}"', "--timeout", "20",
                              cwd=root, expect=1)
        for pid in screen_module.listener_pids(port) or []:
            kill_group(pid)
        self.assertIn(f"did not bind port {port}", result.stderr)
        self.assertEqual(json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"], {})

    def test_stop_leaves_a_reused_pid_alone(self) -> None:
        root = self.make_root("reused")
        self.claim(root)
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(kill_group, bystander.pid)
        screen_path = self.screen_file(root)
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        screen["processes"]["serve"] = {
            "pid": bystander.pid, "pgid": bystander.pid, "start": "Mon Jan  1 00:00:00 2001",
            "cmd": "old server", "cwd": str(root), "log": str(screen_path.parent / "serve.log"),
            "started_at": "2001-01-01T00:00:00+00:00",
        }
        screen_path.write_text(json.dumps(screen), encoding="utf-8")
        result = self.run_cli("stop", cwd=root)
        self.assertIn("belongs to another process", result.stdout)
        self.assertIsNone(bystander.poll())

    def test_release_and_prune(self) -> None:
        kept = self.make_root("kept")
        self.claim(kept)
        self.run_cli("stop", "--release", cwd=kept)
        self.assertFalse(self.screen_file(kept).parent.exists())
        gone = self.make_root("gone")
        self.claim(gone)
        shutil.rmtree(gone)
        gone.mkdir()  # recreated so the cleanup stop call has a working directory
        screen_path = self.screen_file(gone)
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        screen["root"] = str(self.tmp / "deleted-worktree")
        screen_path.write_text(json.dumps(screen), encoding="utf-8")
        listing = self.run_cli("list", "--prune", cwd=self.tmp)
        self.assertIn("pruned", listing.stdout)
        self.assertFalse(screen_path.parent.exists())

    def write_record(self, root: Path, name: str, record: dict) -> None:
        screen_path = self.screen_file(root)
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        screen["processes"][name] = {
            "cmd": "test", "cwd": str(root), "log": str(screen_path.parent / f"{name}.log"),
            "started_at": "2001-01-01T00:00:00+00:00", **record,
        }
        screen_path.write_text(json.dumps(screen), encoding="utf-8")

    def test_stop_needs_force_when_the_group_leader_is_gone(self) -> None:
        root = self.make_root("orphans")
        self.claim(root)
        leader = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & exit 0"], start_new_session=True)
        leader.wait()
        self.addCleanup(kill_group, leader.pid)
        self.write_record(root, "serve", {"pid": leader.pid, "pgid": leader.pid, "start": "Mon Jan  1 00:00:00 2001"})
        refused = self.run_cli("stop", cwd=root, expect=1)
        self.assertIn("Ownership cannot be verified", refused.stdout)
        self.assertTrue(group_is_alive(leader.pid))
        self.run_cli("stop", "--force", cwd=root)
        self.assertTrue(wait_for_group_exit(leader.pid))

    def test_record_without_a_start_time_is_not_trusted(self) -> None:
        root = self.make_root("no-start")
        self.claim(root)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(kill_group, sleeper.pid)
        self.write_record(root, "serve", {"pid": sleeper.pid, "pgid": sleeper.pid, "start": None})
        self.run_cli("stop", cwd=root, expect=1)
        self.assertIsNone(sleeper.poll())

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_failed_state_write_stops_the_new_process(self) -> None:
        root = self.make_root("read-only-state")
        self.claim(root)
        directory = self.screen_file(root).parent
        (directory / "serve.log").touch()
        (directory / "electron-user-data").mkdir(exist_ok=True)
        (directory / "launch.lock").touch()
        directory.chmod(0o500)
        self.addCleanup(directory.chmod, 0o700)
        marker = f"{random.randrange(10 ** 6)}.25"
        result = self.run_cli("serve", "--wait", "none", "--cmd", f"exec sleep {marker}", cwd=root, expect=1)
        self.assertIn("could not record serve", result.stderr)
        leftover = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
        self.assertEqual(leftover.stdout.strip(), "")

    def test_concurrent_serve_starts_one_server(self) -> None:
        root = self.make_root("race")
        port = int(self.claim(root)["APP_PORT"])
        command = self.serve_cmd
        runs = [
            subprocess.Popen([sys.executable, str(SCRIPT), "serve", "--cmd", command, "--timeout", "30"],
                             cwd=root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outputs = [run.communicate(timeout=60) for run in runs]
        record = json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"]["serve"]
        self.addCleanup(kill_group, record["pid"])
        self.assertEqual([run.returncode for run in runs], [0, 0], outputs)
        stdout = "".join(out for out, _ in outputs)
        self.assertEqual(stdout.count("serve: ready at"), 1, stdout)
        self.assertEqual(stdout.count("already running and ready"), 1, stdout)
        self.assertTrue(listening(port))

    def test_owner_check_requires_every_listener(self) -> None:
        with mock.patch.object(screen_module, "listener_pids", return_value=[11, 12]), \
                mock.patch.object(screen_module, "ps_field", side_effect=lambda pid, field: {11: "500", 12: "999"}[pid]):
            self.assertFalse(screen_module.owned_by_group(4000, 500))
        with mock.patch.object(screen_module, "listener_pids", return_value=[11, 12]), \
                mock.patch.object(screen_module, "ps_field", return_value="500"):
            self.assertTrue(screen_module.owned_by_group(4000, 500))
        with mock.patch.object(screen_module, "listener_pids", return_value=[]), \
                mock.patch.object(screen_module, "port_listening", return_value=True):
            self.assertFalse(screen_module.owned_by_group(4000, 500))
        with mock.patch.object(screen_module, "listener_pids", return_value=None):
            self.assertIsNone(screen_module.owned_by_group(4000, 500))

    @unittest.skipIf(screen_module.listener_pids(1) is None, "no lsof or ss to list listeners")
    def test_running_server_must_still_own_its_port(self) -> None:
        root = self.make_root("stolen-port")
        port = int(self.claim(root)["APP_PORT"])
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(kill_group, sleeper.pid)
        self.write_record(root, "serve", {
            "pid": sleeper.pid, "pgid": sleeper.pid, "start": screen_module.ps_field(sleeper.pid, "lstart"),
        })
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        result = self.run_cli("serve", "--cmd", "sleep 30", "--timeout", "5", cwd=root, expect=1)
        self.assertIn(f"but port {port} is held", result.stderr)
        self.assertIsNone(sleeper.poll())
        self.run_cli("stop", cwd=root)

    def test_running_screen_keeps_its_ports_when_bases_change(self) -> None:
        root = self.make_root("rebased")
        before = self.claim(root)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(kill_group, sleeper.pid)
        self.write_record(root, "serve", {
            "pid": sleeper.pid, "pgid": sleeper.pid, "start": screen_module.ps_field(sleeper.pid, "lstart"),
        })
        rebased = dict(self.env, SPLITSCREEN_APP_BASE=str(self.app_base + 100))
        running = self.run_cli("env", "--json", cwd=root, env=rebased)
        self.assertEqual(json.loads(running.stdout)["APP_PORT"], before["APP_PORT"])
        self.assertIn("keeping ports", running.stderr)
        self.run_cli("stop", cwd=root)
        idle = json.loads(self.run_cli("env", "--json", cwd=root, env=rebased).stdout)
        self.assertEqual(int(idle["APP_PORT"]), int(before["APP_PORT"]) + 100)

    def test_failed_session_close_fails_stop_and_blocks_release(self) -> None:
        root = self.make_root("stuck-session")
        session = self.claim(root)["AGENT_BROWSER_SESSION"]
        bin_dir = self.tmp / "fake-bin"
        bin_dir.mkdir()
        fake = bin_dir / "agent-browser"
        fake.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = session ]; then echo '{{\"data\":{{\"sessions\":[\"{session}\"]}}}}'; exit 0; fi\n"
            "echo 'daemon did not answer' >&2\nexit 3\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        broken = dict(self.env, PATH=f"{bin_dir}{os.pathsep}{self.env['PATH']}")
        result = self.run_cli("stop", cwd=root, env=broken, expect=1)
        self.assertIn("close exited 3", result.stdout)
        blocked = self.run_cli("stop", "--release", cwd=root, env=broken, expect=1)
        self.assertIn("not releasing", blocked.stderr)
        self.assertTrue(self.screen_file(root).exists())

    @unittest.skipIf(screen_module.find_chrome() is None, "Chrome not installed")
    def test_dedicated_chrome_exposes_cdp_and_stops(self) -> None:
        root = self.make_root("browser")
        port = int(self.claim(root)["CDP_PORT"])
        started = self.run_cli("chrome", cwd=root)
        self.assertIn(f"ready on CDP port {port}", started.stdout)
        self.assertIsNotNone(screen_module.cdp_version(port))
        pid = json.loads(self.screen_file(root).read_text(encoding="utf-8"))["processes"]["chrome"]["pid"]
        self.addCleanup(kill_group, pid)
        self.run_cli("stop", cwd=root)
        deadline = time.monotonic() + 5
        while listening(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(listening(port))


if __name__ == "__main__":
    unittest.main()
