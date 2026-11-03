#!/usr/bin/env python3
"""rivet-collect killed mid-check must not leave the `checking` stub on disk.

    python3 backend/tests/test_collect_interrupt.py

2026-10-04: a SIGTERM (systemd TimeoutStartSec, logout) never reached the collector's
`except`, so state.json stayed `status: "checking"` until the next hourly run. Runs the
REAL CLI against fake binaries on PATH, with a `dnf5` that never returns; nothing on the
real system is asked.
"""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

FAKE_EMPTY = "#!/bin/sh\nexit 0\n"
# `exec` keeps the pid, so the test can check the child really went down with the collector
FAKE_HANG = "#!/bin/sh\necho $$ > \"$FAKE_PIDFILE\"\nexec sleep 60\n"
PREV = {
    "schema": 1, "generated_at": "2026-10-04T10:00:00-04:00", "checked_at": "2026-10-04T10:00:00-04:00",
    "status": "ok", "error": None,
    "host": {"os": "Fedora 44", "kernel_running": "7.2.6-200.fc44.x86_64", "secure_boot": True},
    "flags": {"steam_running": False, "kernel_pending": False, "akmod_nvidia_pending": False,
              "reboot_required": False, "blocked_count": 0, "security_count": 0,
              "held_count": 0, "held_candidates": 0},
    "summary": {"total": 1, "total_bytes": 5},
    "groups": [{"id": "dnf", "title": "System", "subtitle": "dnf",
                "items": [{"id": "dnf:curl", "source": "dnf", "name": "curl", "display_name": "curl"}]},
               {"id": "flatpak", "title": "Apps & runtimes", "subtitle": "Flatpak", "items": []},
               {"id": "fwupd", "title": "Firmware", "subtitle": "fwupd", "items": []},
               {"id": "held", "title": "Held back", "subtitle": "pinned", "items": []}],
}


def wait_for(pred, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class InterruptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rivet-int-"))
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, body in (("flatpak", FAKE_EMPTY), ("rpm", FAKE_EMPTY), ("fwupdmgr", FAKE_EMPTY),
                           ("mokutil", FAKE_EMPTY), ("dnf5", FAKE_HANG)):
            f = bindir / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        (self.state_dir / "state.json").write_text(json.dumps(PREV))
        (self.tmp / "ops.json").write_text('{"ops": []}')
        self.pidfile = self.tmp / "dnf5.pid"
        self.env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin", FAKE_PIDFILE=str(self.pidfile),
                        RIVET_STATE_DIR=str(self.state_dir), RIVET_CONFIG_DIR=str(self.tmp / "cfg"),
                        RIVET_FLATPAK_DRYRUN_FIXTURE=str(self.tmp / "ops.json"), RIVET_NO_NOTIFY="1",
                        RIVET_NO_ELSEWHERE="1", RIVET_NO_RELEASE="1")

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def start(self):
        return subprocess.Popen([sys.executable, str(BACKEND / "rivet_collect.py"), "--no-llm", "--quiet"],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def check_interrupted(self, proc, sig, expect_rc):
        wait_for(lambda: self.state()["status"] == "checking", what="the checking stub")
        wait_for(self.pidfile.exists, what="the fake dnf5 to start")
        proc.send_signal(sig)
        _out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, expect_rc, err)
        st = self.state()
        self.assertEqual((st["status"], st["error"]), ("error", "interrupted"))
        self.assertEqual(st["groups"], PREV["groups"])            # carried like the stub does
        self.assertEqual(st["checked_at"], PREV["checked_at"])
        self.assertEqual(st["summary"], PREV["summary"])
        self.assertIn("interrupted", (self.state_dir / "logs" / "collect.log").read_text())
        # no half-written temp file left behind
        self.assertEqual([p.name for p in self.state_dir.iterdir() if p.name.startswith(".state.json.")], [])
        # the hung child went down with us
        pid = int(self.pidfile.read_text().strip())

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        wait_for(gone, timeout=5, what="the fake dnf5 to be killed")

    def test_sigterm_marks_the_state_interrupted_and_exits_143(self):
        self.check_interrupted(self.start(), signal.SIGTERM, 143)

    def test_sigint_marks_the_state_interrupted_and_exits_130(self):
        self.check_interrupted(self.start(), signal.SIGINT, 130)


if __name__ == "__main__":
    unittest.main(verbosity=2)
