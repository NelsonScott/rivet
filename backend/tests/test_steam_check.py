#!/usr/bin/env python3
"""The Steam-only re-check (backend/steam_check.py, 2026-11-10).

    python3 -m unittest tests.test_steam_check

The oracle is the real collector: `rivet-collect --fixture` run once with Steam running and once
without. Re-checking the first document with a probe that says "gone" must give the second one
(groups and flags), and the other way round, without touching checked_at. Nothing here runs the
real `flatpak`: the probe is a function, and the CLI test puts a fake `flatpak` first on PATH.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="rivet-steam-test-"))
os.environ.setdefault("RIVET_STATE_DIR", str(SCRATCH / "state"))   # common.log's collect.log
sys.path.insert(0, str(BACKEND))

import steam_check as SC  # noqa: E402
import triage  # noqa: E402

RUNTIME = "flatpak:runtime/org.freedesktop.Platform/x86_64/25.08"
GL = "flatpak:runtime/org.freedesktop.Platform.GL.default/x86_64/25.08"
NV_INSTALL = "flatpak:runtime/org.freedesktop.Platform.GL.nvidia-615-71-09/x86_64/1.4"
APP = "flatpak:app/md.obsidian.Obsidian/x86_64/stable"


def fixture(steam: bool) -> dict:
    fx = json.loads((BACKEND / "fixtures" / "flatpak-steam.json").read_text())
    fx["steam_running"] = steam
    # a runtime being ADDED (the NVIDIA GL extension) is never blocked by a running Steam
    fx["flatpak"].append({
        "id": NV_INSTALL, "source": "flatpak", "op": "install", "scope": "user", "origin": "flathub",
        "name": "org.freedesktop.Platform.GL.nvidia-615-71-09",
        "display_name": "NVIDIA GL 615.71.09", "from": None, "to": "1.4", "size_bytes": 1,
        "ref": "runtime/org.freedesktop.Platform.GL.nvidia-615-71-09/x86_64/1.4",
        "notes_excerpt": "", "notes_url": None, "advisory": None})
    return fx


_COLLECTED: dict[bool, dict] = {}


def collected(steam: bool) -> dict:
    """state.json as the real rivet-collect writes it for this fixture (cached per steam value)."""
    if steam not in _COLLECTED:
        tmp = Path(tempfile.mkdtemp(dir=SCRATCH))
        fx = tmp / "fixture.json"
        fx.write_text(json.dumps(fixture(steam)))
        env = dict(os.environ, RIVET_STATE_DIR=str(tmp / "state"), RIVET_CONFIG_DIR=str(tmp / "cfg"),
                   RIVET_NO_NOTIFY="1", RIVET_NO_HEALTH="1", RIVET_NO_RELEASE="1", RIVET_NO_ELSEWHERE="1")
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--fixture", str(fx),
                             "--no-llm", "--quiet"], env=env, capture_output=True, text=True, timeout=60)
        assert cp.returncode == 0, cp.stderr
        _COLLECTED[steam] = json.loads((tmp / "state" / "state.json").read_text())
    return copy.deepcopy(_COLLECTED[steam])


def items(doc: dict) -> dict:
    return {i["id"]: i for g in doc["groups"] for i in g["items"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=SCRATCH))
        self.path = self.dir / "state.json"

    def put(self, doc: dict) -> None:
        self.path.write_text(json.dumps(doc, indent=2))

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def recheck(self, running, **kw):
        kw.setdefault("settings", {})
        return SC.recheck(path=self.path, probe_fn=lambda: running, **kw)


class BothDirections(Base):
    def assert_same_as_full_collect(self, got: dict, want: dict) -> None:
        self.assertEqual(got["groups"], want["groups"], "rows differ from a full collect")
        self.assertEqual(got["flags"], want["flags"], "flags differ from a full collect")
        self.assertEqual(got["summary"], want["summary"])

    def test_steam_quit_unblocks_exactly_like_a_full_collect(self):
        before = collected(True)
        self.assertTrue(before["flags"]["steam_running"])
        self.assertEqual(before["flags"]["blocked_count"], 4)     # 4 runtimes, not the app, not the install
        self.put(before)
        res = self.recheck(False)
        self.assertEqual(res, {"steam_running": False, "changed": True, "wrote": True, "skipped": None,
                               "waited_ms": 0})
        got = self.read()
        self.assert_same_as_full_collect(got, collected(False))
        rt = items(got)[RUNTIME]
        self.assertEqual((rt["blocked_by"], rt["badge"], rt["risk"], rt["selected_default"]),
                         (None, None, "yellow", True))
        self.assertNotIn("Held back", rt["verdict"])
        self.assertEqual(got["checked_at"], before["checked_at"], "checked_at is the last FULL check")
        self.assertEqual(got["generated_at"], before["generated_at"])
        self.assertIn("steam_checked_at", got)

    def test_steam_started_blocks_exactly_like_a_full_collect(self):
        before = collected(False)
        self.put(before)
        res = self.recheck(True)
        self.assertTrue(res["wrote"])
        got = self.read()
        self.assert_same_as_full_collect(got, collected(True))
        for rid in (RUNTIME, GL):
            it = items(got)[rid]
            self.assertEqual((it["blocked_by"], it["badge"], it["risk"], it["selected_default"]),
                             ("steam_running", "blocked", "red", False))
            self.assertIn("Held back", it["verdict"])
        self.assertEqual(items(got)[NV_INSTALL]["blocked_by"], None)
        self.assertEqual(items(got)[APP]["blocked_by"], None)
        self.assertEqual(got["checked_at"], before["checked_at"])

    def test_round_trip_comes_back_to_the_same_document(self):
        before = collected(True)
        self.put(before)
        self.recheck(False)
        self.recheck(True)
        got = self.read()
        self.assertEqual(got["groups"], before["groups"])
        self.assertEqual(got["flags"], before["flags"])


class NothingToDo(Base):
    def test_no_change_means_no_write(self):
        for steam in (True, False):
            self.put(collected(steam))
            raw, st = self.path.read_bytes(), os.stat(self.path)
            res = self.recheck(steam)
            self.assertEqual((res["changed"], res["wrote"], res["skipped"]), (False, False, None))
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertEqual(os.stat(self.path).st_ino, st.st_ino, "not even a same-content rewrite")

    def test_a_running_collector_is_left_alone(self):
        doc = collected(True)
        doc["status"] = "checking"
        self.put(doc)
        raw = self.path.read_bytes()
        res = self.recheck(False)
        self.assertEqual((res["skipped"], res["wrote"]), ("checking", False))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_a_failed_probe_changes_nothing(self):
        self.put(collected(True))
        raw = self.path.read_bytes()
        res = self.recheck(None)
        self.assertEqual((res["skipped"], res["wrote"]), ("probe-failed", False))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_no_state_yet(self):
        res = self.recheck(False)
        self.assertEqual((res["skipped"], res["wrote"]), ("no-state", False))
        self.assertFalse(self.path.exists())

    def test_a_write_by_someone_else_in_between_wins(self):
        """The collector (or an Other software patch) writing between our read and our rename:
        their document stands, ours is dropped."""
        self.put(collected(True))
        theirs = dict(collected(True), checked_at="2099-01-01T00:00:00+00:00")
        real = SC.rederive

        def racing(doc, steam, **kw):
            (self.dir / "theirs.json").write_text(json.dumps(theirs))
            os.replace(self.dir / "theirs.json", self.path)
            return real(doc, steam, **kw)

        with mock.patch.object(SC, "rederive", racing):
            res = self.recheck(False)
        self.assertEqual((res["skipped"], res["wrote"]), ("raced", False))
        self.assertEqual(self.read()["checked_at"], "2099-01-01T00:00:00+00:00")


class KeepsWhatTheModelSaid(Base):
    def test_a_raised_colour_on_another_row_survives(self):
        doc = collected(True)
        vim = items(doc)["dnf:vim-common"]
        self.assertEqual(vim["risk"], "green")
        vim["risk"] = "red"                       # the model raised it in the last full check
        self.put(doc)
        self.recheck(False)
        self.assertEqual(items(self.read())["dnf:vim-common"]["risk"], "red")

    def test_ai_on_takes_the_cached_verdict_and_raise_for_an_unblocked_row(self):
        doc = collected(True)
        self.put(doc)
        rt = items(doc)[RUNTIME]
        cache = {triage._cache_key(rt): {"verdict": "Mesa and codecs refresh.", "reasoning": "Because.",
                                         "risk_raise": "red"}}
        self.recheck(False, settings={"ai_triage": True}, cache=cache)
        got = items(self.read())
        self.assertEqual((got[RUNTIME]["verdict"], got[RUNTIME]["risk"], got[RUNTIME]["badge"]),
                         ("Mesa and codecs refresh.", "red", None))
        self.assertTrue(got[RUNTIME]["selected_default"])
        # no cache entry: the collector's fallback, the deterministic line
        self.assertEqual(got[GL]["verdict"], triage.deterministic(dict(got[GL]))["verdict"])
        self.assertEqual(got[GL]["risk"], "yellow")

    def test_ai_off_ignores_the_cache(self):
        doc = collected(True)
        self.put(doc)
        rt = items(doc)[RUNTIME]
        cache = {triage._cache_key(rt): {"verdict": "x", "reasoning": "", "risk_raise": "red"}}
        self.recheck(False, settings={"ai_triage": False}, cache=cache)
        self.assertEqual(items(self.read())[RUNTIME]["risk"], "yellow")

    def test_held_rows_are_not_touched(self):
        doc = collected(True)
        held = {"id": "flatpak:runtime/org.freedesktop.Platform.VulkanLayer.gamescope/x86_64/25.08",
                "source": "flatpak", "name": "org.freedesktop.Platform.VulkanLayer.gamescope",
                "ref": "runtime/org.freedesktop.Platform.VulkanLayer.gamescope/x86_64/25.08",
                "held": True, "hold_state": "pinned", "risk": "green", "badge": None, "blocked_by": None,
                "selected_default": False, "verdict": "Pinned."}
        next(g for g in doc["groups"] if g["id"] == "held")["items"].append(dict(held))
        self.put(doc)
        self.recheck(False)
        self.recheck(True)
        self.assertEqual(next(g for g in self.read()["groups"] if g["id"] == "held")["items"], [held])


class WaitGone(unittest.TestCase):
    def clock(self):
        t = [0.0]
        return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))

    def test_stops_polling_as_soon_as_steam_is_gone(self):
        clock, sleep = self.clock()
        seq = iter([True, True, True, False])
        calls = []
        running, ms = SC.wait_gone(5, probe_fn=lambda: calls.append(1) or next(seq), sleep=sleep, clock=clock)
        self.assertEqual((running, len(calls)), (False, 4))
        self.assertLess(ms, 1000)

    def test_gives_up_after_the_cap_and_says_still_running(self):
        clock, sleep = self.clock()
        running, ms = SC.wait_gone(5, probe_fn=lambda: True, sleep=sleep, clock=clock)
        self.assertTrue(running)
        self.assertGreaterEqual(ms, 5000)
        self.assertLess(ms, 5500)

    def test_recheck_with_a_stubborn_steam_writes_nothing(self):
        d = Path(tempfile.mkdtemp(dir=SCRATCH))
        p = d / "state.json"
        p.write_text(json.dumps(collected(True)))
        raw = p.read_bytes()
        clock, sleep = self.clock()
        res = SC.recheck(path=p, wait=5, probe_fn=lambda: True, settings={}, sleep=sleep, clock=clock)
        self.assertEqual((res["steam_running"], res["wrote"]), (True, False))
        self.assertGreaterEqual(res["waited_ms"], 5000)
        self.assertEqual(p.read_bytes(), raw)


class Cli(unittest.TestCase):
    """The way the app runs it: a subprocess, a fake `flatpak` first on PATH, one JSON line out."""

    def run_cli(self, ps_lines: str, *args) -> tuple[dict, Path]:
        d = Path(tempfile.mkdtemp(dir=SCRATCH))
        (d / "bin").mkdir()
        fake = d / "bin" / "flatpak"
        fake.write_text("#!/bin/sh\n[ \"$1\" = ps ] || exit 3\nprintf '%s' \"$FAKE_PS\"\n")
        fake.chmod(0o755)
        (d / "state").mkdir()
        (d / "state" / "state.json").write_text(json.dumps(collected(True)))
        env = dict(os.environ, PATH=f"{d / 'bin'}:/usr/bin:/bin", FAKE_PS=ps_lines,
                   RIVET_STATE_DIR=str(d / "state"), RIVET_CONFIG_DIR=str(d / "cfg"))
        cp = subprocess.run([sys.executable, str(BACKEND / "steam_check.py"), *args], env=env,
                            capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        lines = cp.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, cp.stdout)
        return json.loads(lines[0]), d / "state" / "state.json"

    def test_steam_gone(self):
        res, p = self.run_cli("org.gnome.Calculator\n", "--wait-gone", "5")
        self.assertEqual((res["steam_running"], res["wrote"]), (False, True))
        self.assertLess(res["waited_ms"], 1000)
        self.assertFalse(json.loads(p.read_text())["flags"]["steam_running"])

    def test_steam_still_there(self):
        res, p = self.run_cli("com.valvesoftware.Steam\n")
        self.assertEqual((res["steam_running"], res["changed"], res["wrote"]), (True, False, False))


if __name__ == "__main__":
    unittest.main()
