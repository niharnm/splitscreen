#!/usr/bin/env python3
"""Tests for lane.py. Run: python3 -m unittest discover -s <skill-dir>/scripts -p 'test_*.py'"""

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

SCRIPT = Path(__file__).resolve().parent / "lane.py"
sys.path.insert(0, str(SCRIPT.parent))
import lane as lane_module  # noqa: E402


def listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class LaneTest(unittest.TestCase):
    slots = 4

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lane-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app_base = random.randrange(20000, 29000, 10)
        self.cdp_base = self.app_base + 6000
        self.env = dict(os.environ)
        self.env.update({
            "PARALLEL_APP_TESTING_HOME": str(self.tmp / "state"),
            "PARALLEL_APP_TESTING_APP_BASE": str(self.app_base),
            "PARALLEL_APP_TESTING_CDP_BASE": str(self.cdp_base),
            "PARALLEL_APP_TESTING_SLOTS": str(self.slots),
        })
        self.env.pop("AGENT_BROWSER_SESSION", None)

    def run_lane(self, *args: str, cwd: Path, expect: int = 0, timeout: float = 90) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=cwd, env=self.env,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != expect:
            self.fail(f"lane.py {' '.join(args)} exited {result.returncode}, expected {expect}:\n"
                      f"{result.stdout}\n{result.stderr}")
        return result

    def make_root(self, name: str) -> Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        return root

    def claim(self, root: Path) -> dict:
        values = json.loads(self.run_lane("env", "--json", cwd=root).stdout)
        self.addCleanup(self.run_lane, "stop", cwd=root, expect=0)
        return values

    def lane_file(self, root: Path) -> Path:
        lane_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        return self.tmp / "state" / "lanes" / lane_id / "lane.json"

    def test_env_is_stable_and_shell_safe(self) -> None:
        root = self.make_root("agent one")
        first = self.claim(root)
        second = json.loads(self.run_lane("env", "--json", cwd=root).stdout)
        self.assertEqual(first, second)
        app_port, cdp_port = int(first["APP_PORT"]), int(first["CDP_PORT"])
        self.assertEqual(app_port - self.app_base, cdp_port - self.cdp_base)
        self.assertEqual(first["APP_URL"], f"http://localhost:{app_port}")
        self.assertTrue(first["AGENT_BROWSER_SESSION"].startswith("lane-agent-one-"))
        exports = self.run_lane("env", cwd=root).stdout
        shell = subprocess.run(
            ["/bin/sh", "-c", exports + '\nprintf "%s|%s" "$LANE_ROOT" "$APP_PORT"'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(shell.stdout, f"{root}|{app_port}")

    def test_worktrees_get_distinct_slots_until_full(self) -> None:
        ports = {self.claim(self.make_root(f"wt{index}"))["APP_PORT"] for index in range(self.slots)}
        self.assertEqual(len(ports), self.slots)
        extra = self.make_root("one-too-many")
        result = self.run_lane("env", cwd=extra, expect=1)
        self.assertIn("no free lane", result.stderr)

    def test_new_lane_skips_busy_ports(self) -> None:
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
        git = ["git", "-c", "user.name=Lane Test", "-c", "user.email=lane@example.invalid"]
        subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
        (repo / "sub").mkdir()
        (repo / "sub" / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run([*git, "add", "."], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
        worktree = self.tmp / "repo-agent-b"
        subprocess.run([*git, "worktree", "add", "-q", str(worktree)], cwd=repo, check=True)
        at_root = self.claim(repo)
        in_subdir = json.loads(self.run_lane("env", "--json", cwd=repo / "sub").stdout)
        in_worktree = self.claim(worktree.resolve())
        self.assertEqual(at_root, in_subdir)
        self.assertEqual(in_worktree["LANE_ROOT"], str(worktree.resolve()))
        self.assertNotEqual(at_root["APP_PORT"], in_worktree["APP_PORT"])

    def test_serve_status_reuse_and_stop(self) -> None:
        root = self.make_root("server")
        values = self.claim(root)
        port = int(values["APP_PORT"])
        command = f'exec "{sys.executable}" -m http.server "$APP_PORT" --bind 127.0.0.1'
        started = self.run_lane("serve", "--cmd", command, "--timeout", "30", cwd=root)
        self.assertIn(f"ready at http://localhost:{port}", started.stdout)
        self.assertTrue(listening(port))
        pid = json.loads(self.lane_file(root).read_text(encoding="utf-8"))["processes"]["serve"]["pid"]
        self.addCleanup(kill_group, pid)
        again = self.run_lane("serve", "--cmd", command, cwd=root)
        self.assertIn("already running and ready", again.stdout)
        status = self.run_lane("status", cwd=root)
        self.assertIn("serve: running", status.stdout)
        stopped = self.run_lane("stop", cwd=root)
        self.assertIn(f"serve: stopped PID {pid}", stopped.stdout)
        self.assertFalse(listening(port))
        self.assertEqual(json.loads(self.lane_file(root).read_text(encoding="utf-8"))["processes"], {})

    def test_serve_refuses_port_held_by_another_process(self) -> None:
        root = self.make_root("taken")
        port = int(self.claim(root)["APP_PORT"])
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        result = self.run_lane("serve", "--cmd", "sleep 30", cwd=root, expect=2)
        self.assertIn("is in use", result.stderr)
        self.assertEqual(json.loads(self.lane_file(root).read_text(encoding="utf-8"))["processes"], {})

    @unittest.skipIf(shutil.which("lsof") is None, "lsof not installed")
    def test_serve_fails_when_listener_is_outside_the_lane(self) -> None:
        root = self.make_root("escaped")
        port = int(self.claim(root)["APP_PORT"])
        detach = (
            "import os, subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-m', 'http.server', os.environ['APP_PORT'], '--bind', '127.0.0.1'], "
            "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(60)"
        )
        result = self.run_lane("serve", "--cmd", f'"{sys.executable}" -c "{detach}"', "--timeout", "20",
                               cwd=root, expect=1)
        for pid in lane_module.listener_pids(port) or []:
            kill_group(pid)
        self.assertIn(f"did not bind port {port}", result.stderr)
        self.assertEqual(json.loads(self.lane_file(root).read_text(encoding="utf-8"))["processes"], {})

    def test_stop_leaves_a_reused_pid_alone(self) -> None:
        root = self.make_root("reused")
        self.claim(root)
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(kill_group, bystander.pid)
        lane_path = self.lane_file(root)
        lane = json.loads(lane_path.read_text(encoding="utf-8"))
        lane["processes"]["serve"] = {
            "pid": bystander.pid, "pgid": bystander.pid, "start": "Mon Jan  1 00:00:00 2001",
            "cmd": "old server", "cwd": str(root), "log": str(lane_path.parent / "serve.log"),
            "started_at": "2001-01-01T00:00:00+00:00",
        }
        lane_path.write_text(json.dumps(lane), encoding="utf-8")
        result = self.run_lane("stop", cwd=root)
        self.assertIn("belongs to another process", result.stdout)
        self.assertIsNone(bystander.poll())

    def test_release_and_prune(self) -> None:
        kept = self.make_root("kept")
        self.claim(kept)
        self.run_lane("stop", "--release", cwd=kept)
        self.assertFalse(self.lane_file(kept).parent.exists())
        gone = self.make_root("gone")
        self.claim(gone)
        shutil.rmtree(gone)
        gone.mkdir()  # recreated so the cleanup stop call has a working directory
        lane_path = self.lane_file(gone)
        lane = json.loads(lane_path.read_text(encoding="utf-8"))
        lane["root"] = str(self.tmp / "deleted-worktree")
        lane_path.write_text(json.dumps(lane), encoding="utf-8")
        listing = self.run_lane("list", "--prune", cwd=self.tmp)
        self.assertIn("pruned", listing.stdout)
        self.assertFalse(lane_path.parent.exists())

    @unittest.skipIf(lane_module.find_chrome() is None, "Chrome not installed")
    def test_dedicated_chrome_exposes_cdp_and_stops(self) -> None:
        root = self.make_root("browser")
        port = int(self.claim(root)["CDP_PORT"])
        started = self.run_lane("chrome", cwd=root)
        self.assertIn(f"ready on CDP port {port}", started.stdout)
        self.assertIsNotNone(lane_module.cdp_version(port))
        pid = json.loads(self.lane_file(root).read_text(encoding="utf-8"))["processes"]["chrome"]["pid"]
        self.addCleanup(kill_group, pid)
        self.run_lane("stop", cwd=root)
        deadline = time.monotonic() + 5
        while listening(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(listening(port))


if __name__ == "__main__":
    unittest.main()
