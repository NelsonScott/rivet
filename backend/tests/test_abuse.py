#!/usr/bin/env python3
"""The 2026-11-01 abuse pass before the public release: every scenario a stranger's machine
(or a bad day on this one) can throw at the backend, simulated through the fake layers.

    python3 backend/tests/test_abuse.py

Nothing real is asked. Home Assistant is a socket on 127.0.0.1 that refuses or never answers
(the real urllib path), claude is app/qa/fake-claude.py in one of its failure modes, dnf is
backend/fixtures/fake_dnf.py, flatpak / systemctl are scripts on a scratch PATH, and every
state file lives in a scratch dir. docs/QA-MATRIX.md lists what each test stands for.
"""
import importlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "fixtures"))
SCRATCH = Path(tempfile.mkdtemp(prefix="rivet-abuse-"))
os.environ.setdefault("RIVET_STATE_DIR", str(SCRATCH / "state"))
os.environ.pop("RIVET_FIX_CANNED", None)

from fake_dnf import FakeWorld  # noqa: E402

FAKE_BIN = FakeWorld.enable_fake_akmods()     # sudo / pkexec / akmods on PATH are fakes from here on
for _tool in ("sudo", "pkexec", "akmods"):
    assert Path(shutil.which(_tool) or "/").parent == FAKE_BIN, f"real {_tool} on PATH"

import common  # noqa: E402
import elsewhere as E  # noqa: E402
import elsewhere_apply as EA  # noqa: E402
import know as K  # noqa: E402

K.HOME = SCRATCH / "home"
K.CACHE_PATH = SCRATCH / "state" / "know-cache.json"
FAKE_CLAUDE = REPO / "app" / "qa" / "fake-claude.py"
FIX_FIXTURE = BACKEND / "fixtures" / "fix-alexa.json"
AID = "ha:update.alexa_media_player_update"


def exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Em:
    def __init__(self):
        self.events = []

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        self.events.append(dict(step=step, status=status, pct=pct, msg=msg, log=log, **extra))

    def done(self, status, *a, **kw):
        if a:
            kw["summary"] = a[0]
        self.events.append(dict(step="done", status=status, **kw))

    @property
    def last(self):
        return self.events[-1]


def write_state(items, path=None):
    p = path or EA.STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": 1, "groups": [], "elsewhere": {
        "counts": {}, "sources": [{"id": "ha", "stale": len(items)}], "items": items}}))


def state_row(rid):
    return next(i for i in json.loads(EA.STATE_PATH.read_text())["elsewhere"]["items"] if i["id"] == rid)


def ha_row(name="Matter Server", eid="update.matter_server_update", kind="addon", **kw):
    it = E.decide_action(E.row("ha", name, "8.1.0", "8.1.2", kind=kind, installable=True, backup=False,
                               entity_id=eid, key=eid))
    it.update(kw)
    return it


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nobody listens there now: connection refused
    return port


class SilentServer:
    """Accepts connections and never answers (a hung Home Assistant)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)

    def close(self):
        for c in self.conns:
            c.close()
        self.sock.close()


class NoTools(E.Ctx):
    """A machine with none of the tools: which() finds nothing."""

    def which(self, name, *fallbacks):
        return None


# ============================================================ 1. Home Assistant
class NoHomeAssistant(unittest.TestCase):
    def test_not_set_up_at_all_shows_nothing(self):
        # no HA_TOKEN, no HA_URL, no ha.env: absent, no row, no hint about the author's files
        r = E.collect_ha(NoTools(env={}))
        self.assertEqual(r["status"], "absent")
        reg = [("ha", "Home Assistant", E.collect_ha, 5, 0)]
        orig = E.Ctx.which
        E.Ctx.which = lambda self, name, *f: None
        try:
            doc = E.collect(registry=reg, env={})
        finally:
            E.Ctx.which = orig
        self.assertEqual(doc["items"], [])
        self.assertEqual([s["status"] for s in doc["sources"]], ["absent"])
        self.assertEqual(doc["counts"], {"stale": 0, "failed": 0, "cant_check": 0})

    def test_the_default_url_is_not_a_lan_ip(self):
        self.assertEqual(E.HA_URL_DEFAULT, "http://homeassistant.local:8123")
        self.assertEqual(E.ha_base(E.Ctx(env={"HA_URL": "http://ha.lan:8123/"})), "http://ha.lan:8123")


class HomeAssistantUnreachable(unittest.TestCase):
    def collect(self, url, timeout=4):
        reg = [("ha", "Home Assistant", E.collect_ha, timeout, 0)]
        t0 = time.monotonic()
        doc = E.collect(registry=reg, env={"HA_TOKEN": "t", "HA_URL": url})
        return doc, time.monotonic() - t0

    def test_connection_refused_is_one_failed_row_fast(self):
        url = f"http://127.0.0.1:{closed_port()}"
        doc, dt = self.collect(url)
        self.assertLess(dt, 3, "a refused connection must not wait out the deadline")
        (row,) = doc["items"]
        self.assertEqual((row["id"], row["status"]), ("ha:_source", "failed"))
        self.assertEqual(row["hint"], f"Could not reach Home Assistant at {url} (connection refused).")
        self.assertEqual(doc["counts"]["failed"], 1)

    def test_a_name_that_does_not_resolve_says_so(self):
        doc, _ = self.collect("http://rivet-no-such-host.invalid:8123")
        self.assertIn("(the name does not resolve)", doc["items"][0]["hint"])

    def test_timeout_is_a_failed_row_at_the_deadline(self):
        srv = SilentServer()
        try:
            doc, dt = self.collect(f"http://127.0.0.1:{srv.port}", timeout=2)
        finally:
            srv.close()
        self.assertLess(dt, 5)
        self.assertEqual(doc["items"][0]["status"], "failed")
        self.assertIn("timed out", doc["items"][0]["hint"])


class SeqCtx(E.Ctx):
    """POST answers, GETs follow a script: a list of values / exceptions (the last repeats)."""

    def __init__(self, gets, post=None):
        super().__init__(deadline=time.monotonic() + 30, env={"HA_TOKEN": "t", "HA_URL": "http://ha.test"})
        self.gets, self.post, self.calls = list(gets), post, []

    def http_json(self, url, *, headers=None, timeout=None, data=None):
        self.calls.append(("POST" if data is not None else "GET", url))
        if data is not None:
            if isinstance(self.post, Exception):
                raise self.post
            return self.post or []
        v = self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        if isinstance(v, Exception):
            raise v
        return v


REFUSED = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class HomeAssistantGoesAway(unittest.TestCase):
    def test_gone_before_the_install_fails_at_once_and_offers_fix_it(self):
        it = ha_row()
        write_state([it])
        em = Em()
        t0 = time.monotonic()
        rc = EA.run_ha(em, it, ctx=SeqCtx([REFUSED], post=REFUSED), sleep=lambda s: None)
        self.assertLess(time.monotonic() - t0, 2, "used to poll for 20 minutes")
        self.assertEqual(rc, 1)
        other = em.last["other"]
        self.assertEqual((other["result"], other["unreachable"]), ("failed", True))
        self.assertIn("could not reach Home Assistant at http://ha.test (connection refused)", em.last["summary"])
        self.assertIn("nothing was installed", em.last["summary"])
        # the row ends failed, which is what puts Fix it on it (app.js ewFixable)
        self.assertEqual(state_row(it["id"])["last_run"]["result"], "failed")

    def test_gone_in_the_middle_of_an_install_fails_after_a_grace_period(self):
        it = ha_row()
        write_state([it])
        busy = {"state": "on", "attributes": {"installed_version": "8.1.0", "latest_version": "8.1.2", "in_progress": True}}
        ctx = SeqCtx([busy, busy, REFUSED], post=[])
        orig = EA.HA_GONE_S
        EA.HA_GONE_S = 0.3
        try:
            em = Em()
            rc = EA.run_ha(em, it, ctx=ctx, sleep=lambda s: time.sleep(0.05))
        finally:
            EA.HA_GONE_S = orig
        self.assertEqual(rc, 1)
        self.assertTrue(em.last["other"]["unreachable"])
        self.assertIn("Home Assistant stopped answering while installing (connection refused", em.last["summary"])
        self.assertIn("Waiting for Home Assistant…", [e.get("msg") for e in em.events])

    def test_a_slow_answer_is_not_unreachable(self):
        self.assertFalse(E.unreachable(urllib.error.URLError(TimeoutError("timed out"))))
        self.assertTrue(E.unreachable(urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))))
        self.assertTrue(E.unreachable(REFUSED))

    def test_no_token_refuses_without_sending_anything(self):
        it = ha_row()
        orig = E.Ctx.which
        E.Ctx.which = lambda self, name, *f: None
        try:
            with self.assertRaises(EA.Refused) as cm:
                EA.ha_conn(E.Ctx(env={}))
        finally:
            E.Ctx.which = orig
        self.assertIn("HA_TOKEN", str(cm.exception))
        self.assertNotIn("agent.env.tpl", str(cm.exception))
        del it


# ============================================================ 2. claude missing / broken
class ClaudeBroken(unittest.TestCase):
    """Fix it with no claude, a logged-out claude, garbage, a hang, an exhausted budget:
    the row gets ONE plain line, apply.log has the whole run."""

    def failed_alexa(self):
        it = E.decide_action(E.row("ha", "Alexa Media Player", "v5.15.7", "v5.18.0", kind="integration",
                                   installable=True, backup=False, entity_id="update.alexa_media_player_update",
                                   key="update.alexa_media_player_update"))
        it["last_run"] = {"at": None, "result": "failed", "note": "Home Assistant stopped without installing it"}
        return it

    def fix(self, **env):
        write_state([self.failed_alexa()])
        home = Path(tempfile.mkdtemp(prefix="rivet-nohome-"))
        e = dict(os.environ, RIVET_STATE_DIR=str(EA.STATE_PATH.parent), RIVET_FIX_CANNED=str(FIX_FIXTURE),
                 HOME=str(home), PATH=f"{FAKE_BIN}:/usr/bin:/bin")
        mode = env.pop("FAKE_CLAUDE_MODE", None)
        mf = EA.STATE_PATH.parent / "fake-claude-mode"
        mf.write_text(mode) if mode else mf.unlink(missing_ok=True)
        self.addCleanup(mf.unlink, missing_ok=True)
        e.update(env)
        log = EA.STATE_PATH.parent / "logs" / "apply.log"
        before = log.read_text() if log.exists() else ""
        t0 = time.monotonic()
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_apply.py"), "--other-fix", AID],
                            capture_output=True, text=True, timeout=120, env=e)
        evs = [json.loads(x) for x in cp.stdout.splitlines() if x.startswith("{")]
        new_log = (log.read_text() if log.exists() else "")[len(before):]
        return cp, evs, new_log, time.monotonic() - t0

    def check_plain(self, cp, evs, log, want):
        self.assertEqual(cp.returncode, 1, cp.stdout[-1500:] + cp.stderr[-800:])
        done = evs[-1]
        self.assertEqual((done["step"], done["other"]["result"]), ("done", "not_fixed"))
        self.assertTrue(done["summary"].startswith("Not fixed: " + want), done["summary"])
        self.assertNotIn("\n", done["summary"])
        self.assertLess(len(done["summary"]), 240)
        self.assertEqual(state_row(AID)["last_run"]["result"], "fix_failed")
        self.assertEqual(state_row(AID)["last_run"]["note"], done["summary"])
        self.assertIn('"step": "fix"', log, "the run is in apply.log")
        self.assertIn(done["summary"], log)

    def test_not_installed(self):
        cp, evs, log, _ = self.fix(RIVET_CLAUDE_BIN=str(SCRATCH / "no-such-claude"))
        self.check_plain(cp, evs, log, "Claude Code is not installed")
        self.assertTrue(evs[-1]["summary"].endswith("nothing was changed."))

    def test_not_logged_in(self):
        cp, evs, log, _ = self.fix(RIVET_CLAUDE_BIN=str(FAKE_CLAUDE), FAKE_CLAUDE_MODE="logged-out")
        self.check_plain(cp, evs, log, "Claude Code is not logged in")

    def test_garbage(self):
        cp, evs, log, _ = self.fix(RIVET_CLAUDE_BIN=str(FAKE_CLAUDE), FAKE_CLAUDE_MODE="garbage")
        self.check_plain(cp, evs, log, "Claude's answer could not be read")
        self.assertIn("lorem ipsum", log, "the garbage itself is in the log")

    def test_timeout(self):
        cp, evs, log, dt = self.fix(RIVET_CLAUDE_BIN=str(FAKE_CLAUDE), FAKE_CLAUDE_MODE="hang", RIVET_FIX_TIMEOUT="3")
        self.check_plain(cp, evs, log, "Claude did not finish within 3 s and was stopped")
        self.assertLess(dt, 30)

    def test_budget_exhausted(self):
        cp, evs, log, _ = self.fix(RIVET_CLAUDE_BIN=str(FAKE_CLAUDE), FAKE_CLAUDE_MODE="max-turns")
        self.check_plain(cp, evs, log, "Claude used its whole budget of steps without an answer")

    def test_out_of_usage(self):
        cp, evs, log, _ = self.fix(RIVET_CLAUDE_BIN=str(FAKE_CLAUDE), FAKE_CLAUDE_MODE="usage")
        self.check_plain(cp, evs, log, "no Claude usage is left right now")


# ============================================================ 3. dnf
STATE = json.loads((BACKEND / "fixtures" / "state-selfupdate.json").read_text())
ITEMS = STATE["groups"][0]["items"]


def run_apply(world, ids, state=STATE, streamed=None):
    import rivet_apply
    A = importlib.reload(rivet_apply)
    world.install(A)
    # fwupdmgr (and anything else streamed) never reaches the real tool from a test
    A.stream_cmd = lambda cmd, em, step, **kw: (streamed if streamed is not None else []).append(cmd) or 0
    A.AKMOD_POLL_S = 0
    buf = io.StringIO()
    em = A.Emitter(buf)
    rc = A.run_real(em, ids, resolve_only=False, state=state)
    em.logfh.close()
    evs = [json.loads(x) for x in buf.getvalue().splitlines()]
    return rc, evs, evs[-1], A


class Dnf(unittest.TestCase):
    def test_a_transaction_that_dies_midway_names_what_is_missing(self):
        names = [i["name"] for i in ITEMS
                 if not i["name"].startswith(("kernel", "akmod", "kmod", "dnf5", "libdnf5", "polkit", "python3-libdnf5"))]
        w = FakeWorld.from_items(ITEMS)
        w.raise_tx = {0: "Transaction failed: rpm transaction aborted (signal 9)"}
        w.drop = set(names[len(names) // 2:])
        rc, evs, done, _ = run_apply(w, [i["id"] for i in ITEMS])
        self.assertEqual((rc, done["status"]), (1, "fail"))
        v = done["results"]["dnf"]
        self.assertGreater(v["installed"], 0)
        self.assertLess(v["installed"], v["planned"])
        # a failed main transaction also means the updater's own transaction never ran
        updater = {"dnf5", "dnf5daemon-server", "libdnf5", "polkit"}
        self.assertTrue(set(v["missing"]) & w.drop and set(v["missing"]) <= w.drop | updater, (v["missing"], w.drop))
        self.assertIn("Not installed:", done["summary"])
        self.assertEqual(done["cause"], "dnf stopped partway: some packages are installed and some are not.")

    def test_a_kernel_says_reboot_required(self):
        kern = [i for i in ITEMS if i["name"] == "kernel"]
        w = FakeWorld.from_items(kern)
        rc, evs, done, _ = run_apply(w, [i["id"] for i in kern])
        self.assertEqual((rc, done["status"], done["reboot_required"]), (0, "ok", True))
        self.assertIn("A reboot is needed to finish.", done["summary"])

    def test_a_conflict_holds_everything_back_and_says_which(self):
        import fake_dnf
        w = FakeWorld.from_items(ITEMS)
        conflict = ("package mesa-va-drivers-26.1.4-1.fc44.x86_64 from updates conflicts with "
                    "mesa-va-drivers-freeworld provided by mesa-va-drivers-freeworld-26.0.2-1.fc44.x86_64 from @System")
        orig = (fake_dnf.FakeDnf.problems, fake_dnf.FakeDnf.resolve)
        fake_dnf.FakeDnf.problems = lambda self: [conflict]
        fake_dnf.FakeDnf.resolve = lambda self: (self._entries(), 2)
        try:
            rc, evs, done, _ = run_apply(w, [i["id"] for i in ITEMS])
        finally:
            fake_dnf.FakeDnf.problems, fake_dnf.FakeDnf.resolve = orig
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(w.transactions, [], "no transaction may start")
        self.assertTrue(done["cause"].startswith("dnf found a dependency conflict around mesa-va-drivers"), done["cause"])
        self.assertIn(conflict[:60], json.dumps(evs), "the solver's own words are in the log")

    def test_cancelled_password_prompt_installs_nothing_and_says_so(self):
        w = FakeWorld.from_items(ITEMS)
        w.raise_tx = {0: "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Not authorized"}
        w.drop = {i["name"] for i in ITEMS}
        # a Flatpak and a firmware update ride along: after a cancelled prompt neither may run
        # (e2e abuse-noclaude caught "nothing was installed" next to "firmware staged")
        fp = {"id": "flatpak:app/org.example.Editor/x86_64/stable", "source": "flatpak", "name": "org.example.Editor",
              "display_name": "Editor", "ref": "app/org.example.Editor/x86_64/stable", "scope": "user", "from": "1", "to": "2"}
        fw = {"id": "fwupd:dev1", "source": "fwupd", "name": "dev1", "display_name": "UEFI dbx", "ref": "dev1", "from": "1", "to": "2"}
        state = json.loads(json.dumps(STATE))
        state["groups"] += [{"id": "flatpak", "items": [fp]}, {"id": "fwupd", "items": [fw]}]
        streamed = []
        rc, evs, done, A = run_apply(w, [i["id"] for i in ITEMS] + [fp["id"], fw["id"]], state=state, streamed=streamed)
        self.assertEqual(w.flatpak_cmds, [], "no flatpak command after the refusal")
        self.assertFalse([c for c in streamed if "fwupdmgr" in c[0]], "no fwupdmgr after the refusal")
        self.assertEqual([e["msg"] for e in evs if e["status"] == "skip" and e["step"] in ("flatpak", "fwupd")],
                         ["Editor skipped: the password prompt was cancelled, so Rivet stopped there.",
                          "UEFI dbx skipped: the password prompt was cancelled, so Rivet stopped there."])
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(done["cause"], "The administrator password prompt was cancelled or denied, so nothing was installed.")
        self.assertEqual(done["results"]["dnf"]["installed"], 0)
        self.assertFalse(done["reboot_required"], "nothing landed, so no 'restart to finish'")
        self.assertIsNot(done["kmod_verified"], False, "no akmod verdict for a run that installed nothing")
        self.assertFalse([e for e in evs if e["step"] == "akmod" and e["status"] == "start"], "no akmod wait")
        self.assertEqual(len(w.transactions), 1, "the updater transaction never starts after a refusal")
        self.assertEqual(w.calls("pkexec") + w.calls("sudo"), [], "nothing else asked for root")


def collect_fixture(fixture: dict) -> tuple[subprocess.CompletedProcess, dict, float]:
    sd = Path(tempfile.mkdtemp(prefix="rivet-collect-"))
    fx = sd / "fixture.json"
    fx.write_text(json.dumps(fixture))
    t0 = time.monotonic()
    cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--fixture", str(fx), "--no-llm", "--quiet"],
                        capture_output=True, text=True, timeout=120,
                        env=dict(os.environ, RIVET_STATE_DIR=str(sd), RIVET_NO_NOTIFY="1", RIVET_NO_ELSEWHERE="1"))
    dt = time.monotonic() - t0
    return cp, json.loads((sd / "state.json").read_text()), dt


def dnf_item(n: int) -> dict:
    return {"id": f"dnf:pkg{n:03d}", "source": "dnf", "name": f"pkg{n:03d}", "display_name": f"pkg{n:03d}",
            "from": "1.0-1.fc44", "to": "1.1-1.fc44", "size_bytes": 1000 + n,
            "ref": f"pkg{n:03d}-1.1-1.fc44.x86_64", "advisory": None, "notes_excerpt": "", "notes_url": None}


class AiOptIn(unittest.TestCase):
    """Claude Code is opt-in (2026-11-01): with settings ai_triage unset or false, a collect run
    never starts `claude`, even when it is installed; with it on, triage asks it."""

    def run_collect(self, settings):
        sd = Path(tempfile.mkdtemp(prefix="rivet-ai-"))
        (sd / "cfg").mkdir()
        if settings is not None:
            (sd / "cfg" / "settings.json").write_text(json.dumps(settings))
        marker = sd / "claude-called"
        fake = sd / "claude"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\necho '{{}}'\n")
        fake.chmod(0o755)
        fx = sd / "fixture.json"
        fx.write_text(json.dumps({"dnf": [dnf_item(1)], "flatpak": [], "fwupd": []}))
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--fixture", str(fx), "--quiet"],
                            capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, RIVET_STATE_DIR=str(sd / "state"), RIVET_CONFIG_DIR=str(sd / "cfg"),
                                     RIVET_CLAUDE_BIN=str(fake), RIVET_NO_NOTIFY="1", RIVET_NO_ELSEWHERE="1"))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        st = json.loads((sd / "state" / "state.json").read_text())
        self.assertTrue(st["groups"][0]["items"][0]["reasoning"], "the deterministic reason is always there")
        return marker.exists()

    def test_off_by_default(self):
        self.assertFalse(self.run_collect(None), "no settings file: claude must not run")
        self.assertFalse(self.run_collect({"ai_triage": False}), "ai_triage false: claude must not run")
        self.assertFalse(self.run_collect({"ai_triage": "yes"}), "only a real true opts in")

    def test_on_when_opted_in(self):
        self.assertTrue(self.run_collect({"ai_triage": True}), "ai_triage true: triage asks claude")


class UpdateDays(unittest.TestCase):
    def test_a_zero_update_day(self):
        cp, st, _ = collect_fixture({"dnf": [], "flatpak": [], "fwupd": []})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual((st["status"], st["summary"]["total"]), ("ok", 0))
        self.assertEqual([g["items"] for g in st["groups"] if g["id"] != "held"], [[], [], []])

    def test_a_300_update_day(self):
        cp, st, dt = collect_fixture({"dnf": [dnf_item(n) for n in range(300)], "flatpak": [], "fwupd": []})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(st["summary"]["total"], 300)
        self.assertLess(dt, 20, "the rules + state write for 300 items")


# ============================================================ 4. Flatpak + health checks
FAKE_FLATPAK = r'''#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
scope = "user" if "--user" in a else "system"
mode = os.environ.get("FAKE_FP", "broken-remote")
if a[0] == "remote-ls":
    remote = a[-1] if not a[-1].startswith("-") else None
    if scope == "system":
        sys.exit(0)
    if remote is None or remote == "broken":
        print("error: Unable to load summary from remote broken: Could not resolve hostname", file=sys.stderr)
        sys.exit(1)
    print("app/org.example.Editor/x86_64/stable\tflathub\t2.1 MB\tEditor\t3.2.0")
elif a[0] == "remotes":
    print("flathub\nbroken" if scope == "user" else "")
elif a[0] == "list":
    print("org.example.Editor/x86_64/stable\tEditor\t3.1.0\tabc123" if scope == "user" else "")
elif a[0] in ("mask", "ps"):
    pass
elif a[0] == "update":
    print("Updating org.gnome.Platform//48")
    print("error: org.gnome.Platform//48 is in use by running apps (org.example.Editor); close them first", file=sys.stderr)
    sys.exit(1)
else:
    sys.exit("fake flatpak refuses " + " ".join(a))
'''


class Flatpak(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp(prefix="rivet-fpbin-"))
        exe(self.bin / "flatpak", FAKE_FLATPAK)
        self.path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.path}"
        ops = self.bin / "ops.json"
        ops.write_text('{"ops": []}')
        os.environ["RIVET_FLATPAK_DRYRUN_FIXTURE"] = str(ops)

    def tearDown(self):
        os.environ["PATH"] = self.path
        os.environ.pop("RIVET_FLATPAK_DRYRUN_FIXTURE", None)

    def test_one_broken_remote_no_longer_hides_the_others(self):
        import sources
        items = sources.collect_flatpak()
        self.assertEqual([i["id"] for i in items], ["flatpak:app/org.example.Editor/x86_64/stable"])
        self.assertEqual(sources.REMOTE_ERRORS, [{"scope": "user", "remote": "broken",
                                                  "error": "error: Unable to load summary from remote broken: Could not resolve hostname"}])

    def test_a_runtime_in_use_fails_with_what_to_close(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)          # the dnf tests patch run_flatpak on the module
        it = {"id": "flatpak:runtime/org.gnome.Platform/x86_64/48", "source": "flatpak", "name": "org.gnome.Platform",
              "display_name": "GNOME Platform", "ref": "runtime/org.gnome.Platform/x86_64/48", "scope": "user",
              "from": "48.1", "to": "48.2"}
        buf = io.StringIO()
        em = A.Emitter(buf)
        res = A.apply_flatpak(em, [it])
        em.logfh.close()
        evs = [json.loads(x) for x in buf.getvalue().splitlines()]
        self.assertEqual((res["failed"], res["failed_names"]), (1, ["GNOME Platform"]))
        fail = [e for e in evs if e["step"] == "flatpak" and e["status"] == "fail"][-1]
        self.assertIn("in use by a running app; close GNOME Platform", fail["msg"])
        self.assertIn("is in use by running apps", fail["error"])


FAKE_SYSTEMCTL = (BACKEND / "fixtures" / "fake_systemctl.py").read_text()


class HealthChecks(unittest.TestCase):
    def world(self, units, by_app=False):
        w = Path(tempfile.mkdtemp(prefix="rivet-health-"))
        (w / "run" / "doc").mkdir(parents=True)
        if by_app:
            (w / "run" / "doc" / "by-app").mkdir()
        (w / "units.json").write_text(json.dumps(units))
        sc = exe(w / "bin" / "systemctl", FAKE_SYSTEMCTL)
        self.env = {"RIVET_RUN_DIR": str(w / "run"), "RIVET_SYSTEMCTL": str(sc), "FAKE_WORLD": str(w)}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self.restore)
        return w

    def restore(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_stale_portal_is_found_and_fixed_with_one_user_restart(self):
        import health as H
        w = self.world({"xdg-document-portal.service": {"since": time.time() - 3600}}, by_app=False)
        doc = H.run_checks()
        self.assertEqual([c["id"] for c in doc["checks"]], ["doc-portal"])
        self.assertIn(f"{w}/run/doc/by-app is missing", doc["checks"][0]["detail"])
        em = Em()
        self.assertEqual(H.fix(em, "doc-portal"), 0)
        self.assertEqual(em.last["status"], "ok")
        self.assertEqual(em.last["health"]["checks"], [])
        self.assertIn("restart xdg-document-portal.service", (w / "calls").read_text())
        self.assertEqual(json.loads(H.HEALTH_PATH.read_text())["checks"], [])

    def test_a_portal_that_is_not_running_is_fine(self):
        import health as H
        self.world({}, by_app=False)
        self.assertEqual(H.run_checks()["checks"], [])

    def test_a_healthy_portal_is_fine(self):
        import health as H
        self.world({"xdg-document-portal.service": {"since": time.time()}}, by_app=True)
        self.assertEqual(H.run_checks()["checks"], [])

    def test_a_fix_that_does_not_help_says_so(self):
        import health as H
        self.world({"xdg-document-portal.service": {"since": time.time()}}, by_app=False)
        os.environ["FAKE_RESTART"] = "fail"
        self.addCleanup(os.environ.pop, "FAKE_RESTART", None)
        orig = H.time.sleep
        H.time.sleep = lambda s: None
        try:
            em = Em()
            rc = H.fix(em, "doc-portal")
        finally:
            H.time.sleep = orig
        self.assertEqual(rc, 1)
        self.assertTrue(em.last["summary"].startswith("Restart the document portal did not help (systemctl exited 1"))
        self.assertEqual([c["id"] for c in em.last["health"]["checks"]], ["doc-portal"])

    def test_only_known_fixes_run(self):
        import health as H
        self.world({})
        em = Em()
        self.assertEqual(H.fix(em, "rm -rf /"), 2)
        self.assertFalse((Path(self.env["FAKE_WORLD"]) / "calls").exists() and
                         "rm" in (Path(self.env["FAKE_WORLD"]) / "calls").read_text())

    def test_the_doctor_cli_writes_health_json(self):
        self.world({"xdg-document-portal.service": {"since": time.time()}}, by_app=False)
        sd = Path(tempfile.mkdtemp(prefix="rivet-hstate-"))
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_doctor.py"), "health"], capture_output=True,
                            text=True, timeout=60, env=dict(os.environ, RIVET_STATE_DIR=str(sd)))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        done = json.loads(cp.stdout.splitlines()[-1])
        self.assertEqual([c["id"] for c in done["health"]["checks"]], ["doc-portal"])
        self.assertEqual(json.loads((sd / "health.json").read_text())["checks"][0]["id"], "doc-portal")


# ============================================================ 5. a machine without the tools
class NoToolsAnywhere(unittest.TestCase):
    def test_every_collector_is_absent_and_lists_nothing(self):
        empty = Path(tempfile.mkdtemp(prefix="rivet-emptyhome-"))
        orig = (E.Ctx.which, E.HOME)
        E.Ctx.which = lambda self, name, *f: None
        E.HOME = empty
        try:
            doc = E.collect(env={"PATH": str(empty)}, containers=True)
        finally:
            E.Ctx.which, E.HOME = orig
        self.assertEqual(doc["items"], [])
        self.assertEqual({s["id"]: s["status"] for s in doc["sources"]},
                         {sid: "absent" for sid, *_ in E.REGISTRY})
        self.assertEqual(doc["counts"], {"stale": 0, "failed": 0, "cant_check": 0})


# Names of the author's own machine, tools and projects (2026-11-01): whole words, any case.
# Public tool names live here; private project names come from an untracked, gitignored
# box_names.local.txt next to this file (one name per line), so the list itself stays private.
_LOCAL_NAMES = Path(__file__).with_name("box_names.local.txt")
_NAMES = ["openclaw", "vicinae", "upkeep", "fable", "tailscale", "howdy"]
if _LOCAL_NAMES.is_file():
    _NAMES += [ln.strip() for ln in _LOCAL_NAMES.read_text().splitlines()
               if ln.strip() and not ln.lstrip().startswith("#")]
BOX_NAMES = r"\b(?:" + "|".join(map(re.escape, _NAMES)) + r")\b"
RESIDUE = re.compile(r"/home/scottnelson|scottnelson\.|192\.168\.\d+\.\d+|100\.\d+\.\d+\.\d+|notion\.(so|com)|"
                     r"agent\.env\.tpl|secrets-load|1Password|\bScott\b|monitor\.local|" + BOX_NAMES, re.I)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in src.splitlines())


class PersonalResidue(unittest.TestCase):
    """What a stranger sees must not carry this box's names, paths or addresses (2026-11-01).
    Comments are exempt (the audit of CLAUDE.md / comments is a separate pass)."""

    def hits(self, text):
        return sorted({m.group(0) for m in RESIDUE.finditer(text)})

    def test_ui_copy(self):
        ui = REPO / "app" / "ui"
        self.assertEqual(self.hits(_strip_js_comments((ui / "app.js").read_text())), [])
        self.assertEqual(self.hits((ui / "index.html").read_text()), [])

    def test_sample_data(self):
        self.assertEqual(self.hits((REPO / "app" / "ui" / "sample-state.json").read_text()), [])

    def test_desktop_entry_has_no_home_path(self):
        self.assertNotIn("/home/", (REPO / "app" / "xyz.scottnelson.Rivet.desktop").read_text())

    def test_whole_tracked_tree_comments_included(self):
        """The public tree as a whole (2026-11-01 privacy pass): no home path, LAN or tailnet
        address, Notion link, secrets tooling, first name or name from the author's own box
        anywhere, comments included. LICENSE, README and CHANGELOG may say anything (the author's
        name, the story); packaging metadata carries the author's name for attribution only;
        this file holds the patterns."""
        try:
            out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True,
                                 text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        if out.returncode != 0:
            self.skipTest("not a git checkout")
        tree = re.compile(r"/home/scottnelson|192\.168\.\d+\.\d+|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+"
                          r"|notion\.(so|com)|agent\.env\.tpl|1Password|monitor\.local|RTX ?5090"
                          r"|face unlock|pam_howdy|" + BOX_NAMES, re.I)
        author = re.compile(r"\bScott\b")
        skip = {"LICENSE", "README.md", "backend/tests/test_abuse.py"}
        attribution = {"packaging/rpm/rivet-updater.spec", "data/xyz.scottnelson.Rivet.metainfo.xml"}
        bad = {}
        for rel in filter(None, out.stdout.split("\0")):
            if rel in skip or re.fullmatch(r"(?:.*/)?CHANGELOG[^/]*", rel) or not (REPO / rel).is_file():
                continue
            try:
                text = (REPO / rel).read_text()
            except (UnicodeDecodeError, OSError):
                continue                       # binary (icons, fonts)
            found = sorted({m.group(0) for m in tree.finditer(text)} |
                           (set() if rel in attribution else {m.group(0) for m in author.finditer(text)}))
            if found:
                bad[rel] = found
        self.assertEqual(bad, {})

    def test_box_names_pattern(self):
        rx = re.compile(BOX_NAMES, re.I)
        for hit in ("OPENCLAW", "vicinae.service", "Upkeep", "fable", "howdy", "tailscale up"):
            self.assertTrue(rx.search(hit), hit)
        for miss in ("fables", "upkeeping", "tailscaled", "showdy", "resource"):
            self.assertFalse(rx.search(miss), miss)

    def test_hints_the_collectors_can_show(self):
        src = (BACKEND / "elsewhere.py").read_text()
        strings = re.findall(r'"(?:[^"\\]|\\.)*"', re.sub(r"#.*", "", src))
        bad = [s for s in strings if re.search(r"agent\.env\.tpl|192\.168\.", s)]
        self.assertEqual(bad, [])


# ============================================================ 6. privilege
class Privilege(unittest.TestCase):
    """Root only where it must be: dnf through dnf5daemon's own polkit action, and pkexec /
    sudo -n for exactly three things (akmods retry, a Fix it root step, a held rollback)."""

    def test_the_only_root_call_sites(self):
        sites = {}
        for p in sorted(BACKEND.glob("*.py")):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\broot_prefix\(\)|\[\s*\"pkexec\"|\[\s*\"sudo\"", code) and "def root_prefix" not in code:
                    sites.setdefault(p.name, 0)
                    sites[p.name] += 1
        self.assertEqual(set(sites), {"common.py", "rivet_apply.py", "rivet_doctor.py", "held_ops.py"}, sites)

    def test_other_software_health_and_collect_never_escalate(self):
        for name in ("elsewhere.py", "elsewhere_apply.py", "other_fix.py", "health.py", "rivet_collect.py",
                     "sources.py", "know.py", "triage.py"):
            code = "\n".join(ln.split("#", 1)[0] for ln in (BACKEND / name).read_text().splitlines())
            self.assertNotRegex(code, r"\bpkexec\b|\bsudo\b|root_prefix", name)

    def test_dnf_asks_through_the_daemon_without_config_overrides(self):
        src = (BACKEND / "dnf5daemon.py").read_text()
        # a session config override would be a second, stranger polkit action
        self.assertNotIn("config_overrides", src)
        self.assertNotIn('"config"', src)


class Packaging(unittest.TestCase):
    """The from-source install and the RPM ship the same files, and need no image tools (2026-11-01)."""
    SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

    def test_the_pre_rendered_icon_set_is_complete(self):
        import struct
        for s in self.SIZES:
            f = REPO / "app" / "icons" / "hicolor" / f"{s}x{s}" / "apps" / "xyz.scottnelson.Rivet.png"
            head = f.read_bytes()[:24]
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", f)
            self.assertEqual(struct.unpack(">II", head[16:24]), (s, s), f)

    def test_no_install_step_renders_images(self):
        for rel in ("install.sh", "app/install.sh"):
            code = [ln for ln in (REPO / rel).read_text().splitlines() if not ln.lstrip().startswith("#")]
            self.assertFalse([ln for ln in code if re.search(r"\b(magick|convert)\b", ln)], rel)
        spec = (REPO / "packaging" / "rpm" / "rivet-updater.spec").read_text()
        self.assertNotIn("ImageMagick", spec)
        self.assertNotRegex(spec, r"(?m)^\s*magick ")

    def test_every_repo_path_the_spec_names_exists(self):
        spec = (REPO / "packaging" / "rpm" / "rivet-updater.spec").read_text()
        spec = spec.replace("%{app_id}", "xyz.scottnelson.Rivet").replace("%{ext_uuid}", "rivet@scottnelson.xyz")
        body = spec.split("%install", 1)[1].split("%check", 1)[0] + spec.split("%files", 1)[1].split("%changelog", 1)[0]
        names = set(re.findall(r"(?<![\w/}%-])((?:app|backend|data|shell-ext|systemd)/[\w./@{},*${}-]+|LICENSE|README\.md|CONTRACT\.md)", body))
        self.assertTrue(names)
        for n in sorted(names):
            n = re.sub(r"\$\{s\}", "48", n).rstrip("\\")
            m = re.search(r"\{([^}]*,[^}]*)\}", n)
            for alt in (m.group(1).split(",") if m else [None]):
                path = n if alt is None else n[:m.start()] + alt + n[m.end():]
                self.assertTrue(list(REPO.glob(path)), f"the spec names {path}, which is not in the tree")


if __name__ == "__main__":
    unittest.main()
