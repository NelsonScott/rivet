#!/usr/bin/env python3
"""2026-11-01: Home Assistant installs judged against their target, the HA core restart,
Fix it (other_fix.py: payload, tool budget, guard, dry run, a run with a fake claude), and
the show_containers setting.

    python3 backend/tests/test_other_fix.py

Nothing real is asked: Home Assistant is a fake ctx + fake websocket, claude is
app/qa/fake-claude.py or a stub, state.json and apply.log live in a scratch dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
SCRATCH = Path(tempfile.mkdtemp(prefix="rivet-fix-test-"))
os.environ.setdefault("RIVET_STATE_DIR", str(SCRATCH / "state"))
os.environ.pop("RIVET_FIX_CANNED", None)

import common  # noqa: E402
import elsewhere as E  # noqa: E402
import elsewhere_apply as EA  # noqa: E402
import fix_guard  # noqa: E402
import other_fix as OF  # noqa: E402

FAKE_CLAUDE = BACKEND.parent / "app" / "qa" / "fake-claude.py"
FIXTURE = BACKEND / "fixtures" / "fix-alexa.json"
AID = "ha:update.alexa_media_player_update"


class FakeEm:
    def __init__(self):
        self.events = []

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        self.events.append(dict(step=step, status=status, pct=pct, msg=msg, log=log, **extra))

    def done(self, status, **kw):
        self.events.append(dict(step="done", status=status, **kw))

    @property
    def last(self):
        return self.events[-1]


def alexa(**kw):
    it = E.decide_action(E.row("ha", "Alexa Media Player", "v5.15.7", "v5.18.0", kind="integration", installable=True,
                               backup=False, entity_id="update.alexa_media_player_update",
                               key="update.alexa_media_player_update"))
    it.update(kw)
    return it


def write_state(items):
    EA.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EA.STATE_PATH.write_text(json.dumps({"schema": 1, "groups": [], "elsewhere": {
        "counts": {}, "sources": [{"id": "ha", "stale": len(items)}], "items": items}}))


def state_row(rid):
    return next(i for i in json.loads(EA.STATE_PATH.read_text())["elsewhere"]["items"] if i["id"] == rid)


class SeqCtx(E.Ctx):
    """http_json by URL suffix: a list is answered in turn (the last one repeats); an
    Exception is raised. Every call is recorded."""

    def __init__(self, answers, ws=None):
        super().__init__(deadline=time.monotonic() + 30, env={"HA_TOKEN": "t"})
        self.answers, self.calls, self.ws = answers, [], ws if isinstance(ws, list) else [ws]

    def http_json(self, url, *, headers=None, timeout=None, data=None):
        self.calls.append((url.replace(E.HA_URL_DEFAULT, ""), data))
        for key, val in self.answers.items():
            if url.endswith(key) or (key.endswith("*") and key[:-1] in url):
                if isinstance(val, list):
                    v = val.pop(0) if len(val) > 1 else val[0]
                else:
                    v = val
                if isinstance(v, Exception):
                    raise v
                return v
        raise AssertionError(f"unexpected HTTP {url}")

    def ha_ws(self, base, token, timeout=15):
        return self.ws.pop(0) if len(self.ws) > 1 else self.ws[0]

    def urls(self):
        return [u for u, _ in self.calls]


class FakeWsR:
    def __init__(self, issues=(), versions=None):
        self.issues, self.versions, self.closed = list(issues), versions or {}, False

    def call(self, msg, timeout=None):
        if msg["type"] == "repairs/list_issues":
            return {"issues": self.issues}
        if msg["type"] == "manifest/get":
            return {"version": self.versions.get(msg["integration"])}
        raise AssertionError(msg)

    def close(self):
        self.closed = True


ISSUE = {"domain": "hacs", "translation_key": "restart_required", "issue_domain": "alexa_media",
         "issue_id": "restart_required_139664351_tags/v5.18.0", "ignored": False,
         "translation_placeholders": {"name": "Alexa Media Player"}}


def ent(installed, latest, **a):
    return {"state": "on" if installed != latest else "off",
            "attributes": dict({"installed_version": installed, "latest_version": latest, "in_progress": False,
                                "supported_features": 23}, **a)}


class HaInstallTarget(unittest.TestCase):
    """rivet-apply --other-update on an HA row, judged against the row's target."""

    def test_the_alexa_regression_latest_moved_while_installing(self):
        # 2026-11-01: HACS downloaded v5.18.0 in 2 s, then knew v5.19.0; the old check said "failed"
        it = alexa()
        write_state([it])
        ctx = SeqCtx({"/api/states/update.alexa_media_player_update": [
            ent("v5.15.7", "v5.18.0"),
            ent("v5.18.0", "v5.19.0", release_summary="<ha-alert alert-type='error'>Restart of Home Assistant required</ha-alert>")],
            "/api/services/update/install": []})
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=ctx, sleep=lambda s: None), 0)
        self.assertEqual(ctx.calls[1], ("/api/services/update/install",
                                        {"entity_id": "update.alexa_media_player_update", "version": "v5.18.0"}),
                         "the confirmed version is pinned (SPECIFIC_VERSION supported)")
        self.assertEqual((em.last["status"], em.last["other"]["result"]), ("ok", "installed"))
        self.assertTrue(em.last["other"]["restart_pending"])
        self.assertIn("v5.18.0 is downloaded; Home Assistant restarts to load it", em.last["summary"])
        self.assertIn("v5.19.0 is out too", em.last["summary"])
        row = state_row(AID)
        self.assertEqual((row["status"], row["installed"], row["restart_pending"]), ("updated", "v5.18.0", True))

    def test_already_downloaded_installs_nothing(self):
        it = alexa()
        write_state([it])
        ctx = SeqCtx({"/api/states/update.alexa_media_player_update":
                      ent("v5.18.0", "v5.19.0", release_summary="Restart of Home Assistant required")})
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=ctx, sleep=lambda s: None), 0)
        self.assertNotIn("/api/services/update/install", ctx.urls(), "nothing to install: it is already there")
        self.assertTrue(em.last["other"]["restart_pending"])
        self.assertIn("was already downloaded; Home Assistant needs a restart", em.last["summary"])

    def test_an_add_on_needs_no_restart(self):
        it = E.decide_action(E.row("ha", "Samba share", "0.30.4", "0.30.5", kind="addon", installable=True,
                                   entity_id="update.samba_share_update"))
        write_state([it])
        ctx = SeqCtx({"/api/states/update.samba_share_update": [ent("0.30.4", "0.30.5", supported_features=9),
                                                              ent("0.30.5", "0.30.5")],
                      "/api/services/update/install": []})
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=ctx, sleep=lambda s: None), 0)
        self.assertFalse(em.last["other"]["restart_pending"])
        self.assertNotIn("version", ctx.calls[1][1], "no SPECIFIC_VERSION feature: no version sent")

    def test_a_real_failure_says_what_it_wanted(self):
        it = alexa()
        write_state([it])
        ctx = SeqCtx({"/api/states/update.alexa_media_player_update": ent("v5.15.7", "v5.18.0"),
                      "/api/services/update/install": []})
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=ctx, sleep=lambda s: None), 1)
        self.assertIn("still v5.15.7, wanted v5.18.0", em.last["cause"])
        self.assertEqual(state_row(AID)["last_run"]["result"], "failed")


class HaRestart(unittest.TestCase):
    """rivet-apply --ha-restart: only when something waits for it, after the config check."""

    def setUp(self):
        self.row = alexa(status="updated", installed="v5.18.0", restart_pending=True)
        write_state([self.row])

    def answers(self, check=None, config=None):
        return {"/api/config/core/check_config": check or {"result": "valid", "errors": None},
                "/api/services/homeassistant/restart": OSError("connection reset"),
                "/api/config": config or [{"state": "RUNNING"}, OSError("refused"), {"state": "NOT_RUNNING"},
                                          {"state": "RUNNING"}],
                "/api/states/update.alexa_media_player_update": ent("v5.18.0", "v5.19.0")}

    def test_refuses_when_nothing_waits_for_a_restart(self):
        write_state([alexa()])          # no restart_pending, no repair
        ctx = SeqCtx(self.answers(), ws=FakeWsR())
        em = FakeEm()
        self.assertEqual(EA.run_ha_restart(em, json.loads(EA.STATE_PATH.read_text()), ctx=ctx, sleep=lambda s: None), 2)
        self.assertEqual(ctx.calls, [], "nothing was sent to Home Assistant")
        self.assertEqual(em.last["other"]["result"], "refused")

    def test_restarts_once_and_confirms_what_it_loaded(self):
        before = FakeWsR([ISSUE], {"alexa_media": "5.15.7"})
        after = FakeWsR([], {"alexa_media": "5.18.0"})
        ctx = SeqCtx(self.answers(), ws=[before, after])
        em = FakeEm()
        rc = EA.run_ha_restart(em, json.loads(EA.STATE_PATH.read_text()), ctx=ctx, sleep=lambda s: None)
        self.assertEqual(rc, 0, em.events)
        self.assertEqual(ctx.urls().count("/api/services/homeassistant/restart"), 1)
        self.assertLess(ctx.urls().index("/api/config/core/check_config"), ctx.urls().index("/api/services/homeassistant/restart"))
        self.assertEqual((em.last["other"]["id"], em.last["other"]["result"]), ("ha:_restart", "restarted"))
        self.assertIn("Now running Alexa Media Player 5.18.0", em.last["summary"])
        row = state_row(AID)
        self.assertEqual((row["restart_pending"], row["loaded"], row["status"]), (False, "5.18.0", "updated"))
        self.assertTrue(before.closed and after.closed)

    def test_a_broken_config_is_never_restarted(self):
        ctx = SeqCtx(self.answers(check={"result": "invalid", "errors": "Invalid config for 'automation'"}),
                     ws=FakeWsR([ISSUE], {"alexa_media": "5.15.7"}))
        em = FakeEm()
        self.assertEqual(EA.run_ha_restart(em, json.loads(EA.STATE_PATH.read_text()), ctx=ctx, sleep=lambda s: None), 1)
        self.assertNotIn("/api/services/homeassistant/restart", ctx.urls())
        self.assertIn("does not check out", em.last["cause"])

    def test_still_the_old_version_after_the_restart_is_a_failure(self):
        ctx = SeqCtx(self.answers(), ws=[FakeWsR([ISSUE], {"alexa_media": "5.15.7"}),
                                         FakeWsR([ISSUE], {"alexa_media": "5.15.7"})])
        em = FakeEm()
        self.assertEqual(EA.run_ha_restart(em, json.loads(EA.STATE_PATH.read_text()), ctx=ctx, sleep=lambda s: None), 1)
        self.assertIn("did not load Alexa Media Player (runs 5.15.7", em.last["cause"])
        self.assertTrue(state_row(AID)["restart_pending"])


def failed_alexa():
    return alexa(last_run={"at": "2026-11-01T13:10:14-05:00", "result": "failed",
                           "note": "Home Assistant stopped without installing it (still v5.18.0)"})


class FixPayload(unittest.TestCase):
    """The Fix it payload builder and its tool budget."""

    def test_budget_per_kind_is_exact_commands_for_this_row(self):
        it = failed_alexa()
        cmds, allowed = OF.budget(it)
        self.assertEqual(OF.verbs_for(it), ["state", "logs", "retry", "restart", "reload"])
        self.assertEqual(allowed, [f"Bash({OF.tool_cmd()} --fix-tool {v} {AID})" for v in OF.verbs_for(it)])
        self.assertFalse(any("*" in a for a in allowed))
        addon = E.decide_action(E.row("ha", "Samba share", "1", "2", kind="addon", installable=True, entity_id="update.t"))
        npm = E.decide_action(E.row("npm", "pnpm", "9.0.0", "9.0.1", command="npm i -g pnpm@latest"))
        self.assertEqual(OF.verbs_for(addon), ["state", "logs", "retry"], "no HA restart or reload for an add-on")
        self.assertEqual(OF.verbs_for(npm), ["state", "logs", "retry"])

    def test_refusals(self):
        self.assertIn("has no failed run", OF.refusal(alexa(), AID))
        self.assertIn("not in the current list", OF.refusal(None, AID))
        self.assertIn("odd row id", OF.refusal(failed_alexa(), "ha:x; rm -rf ~"))
        removal = alexa(last_run={"result": "remove_failed"})
        self.assertIn("has no failed run", OF.refusal(removal, AID), "a failed Remove keeps its own fallback")
        self.assertIsNone(OF.refusal(failed_alexa(), AID))

    def test_apply_excerpt_is_the_last_run_of_that_row_only(self):
        log = SCRATCH / "apply-excerpt.log"
        ev = lambda **k: json.dumps(dict({"ts": "t", "pct": None, "log": None}, **k))  # noqa: E731
        log.write_text("\n".join([
            ev(step="other", status="start", msg="first try"),
            ev(step="done", status="fail", msg="old", other={"id": AID}),
            ev(step="other", status="start", msg="Matter"),
            ev(step="done", status="ok", msg="Matter removed", other={"id": "ha:update.matter_server_update"}),
            ev(step="other", status="start", msg="second try"),
            ev(step="other", status="fail", msg="stopped without installing it"),
            ev(step="done", status="fail", msg="Alexa failed", other={"id": AID}),
            ev(step="other", status="start", msg="ruff, later"),
        ]) + "\n")
        got = OF.apply_excerpt(AID, log)
        self.assertEqual([json.loads(x)["msg"] for x in got], ["second try", "stopped without installing it", "Alexa failed"])
        self.assertEqual(OF.apply_excerpt("npm:nope", log), [])

    def test_ha_log_is_the_last_200_relevant_lines(self):
        lines = []
        for i in range(500):
            lines.append(f"2026-11-01 13:{i // 60:02d}:{i % 60:02d}.000 WARNING [custom_components.alexa_media] \x1b[33mpoll {i}\x1b[0m")
            lines.append(f"2026-11-01 13:00:00.000 ERROR [homeassistant.components.command_line] thermal {i}")

        class LogCtx(E.Ctx):
            def http_text(self, url, *, headers=None, timeout=None, limit=0):
                assert url.endswith("/api/hassio/core/logs/latest"), url
                return "\n".join(lines)
        got = OF.ha_log_lines(LogCtx(), E.HA_URL_DEFAULT, {}, failed_alexa(), "alexa_media")
        self.assertEqual(len(got), 200)
        self.assertTrue(got[-1].endswith("poll 499") and "\x1b" not in got[-1], "ANSI stripped, newest last")
        self.assertFalse(any("thermal" in x for x in got), "unrelated lines are left out")

    def test_build_payload_carries_everything_the_dry_run_asserts(self):
        doc = json.loads(FIXTURE.read_text())
        it = doc["row"]
        ev = {"apply_log": doc["apply_log"], "ha_log": doc["ha_log"], "live": doc["live"]}
        payload = OF.build_payload(it, ev)
        import rivet_doctor as D
        argv = D.claude_argv(payload["prompt"], OF.MODEL, **OF.claude_kw(payload))
        checks = OF.dry_checks(it, ev, payload, argv, dict(D.clean_env(), **OF.session_env("/tmp/x")))
        self.assertEqual([w for ok, w in checks if not ok], [])
        self.assertGreaterEqual(len(checks), 19)
        self.assertIn("Restart of Home Assistant required", payload["prompt"])
        self.assertIn('"loaded_version": "5.15.7"', payload["prompt"])
        self.assertNotIn("HA_TOKEN", json.dumps(OF.session_env("/tmp/x")), "the token never reaches claude's env")

    def test_the_guard_denies_everything_outside_the_budget(self):
        cmds = OF.budget(failed_alexa())[0]
        self.assertIsNone(fix_guard.decide({"tool_name": "Bash", "tool_input": {"command": cmds[2]}}, cmds))
        for bad in ({"tool_name": "Bash", "tool_input": {"command": "cat ~/.config/secrets/sa-token"}},
                    {"tool_name": "Bash", "tool_input": {"command": cmds[2] + " ; reboot"}},
                    {"tool_name": "Bash", "tool_input": {"command": cmds[2].replace("retry", "remove")}},
                    {"tool_name": "Read", "tool_input": {"file_path": "/etc/shadow"}},
                    {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}):
            self.assertTrue(fix_guard.decide(bad, cmds), bad)
        self.assertIsNone(fix_guard.decide({"tool_name": "StructuredOutput", "tool_input": {"fixed": True}}, cmds),
                          "the --json-schema answer goes through (found by a real run, 2026-11-01)")
        # the hook script itself, as the CLI calls it
        sess = Path(tempfile.mkdtemp(prefix="rivet-fix-sess-", dir=SCRATCH))
        (sess / "meta.json").write_text(json.dumps({"cmds": cmds}))
        run = lambda ev: subprocess.run([sys.executable, str(BACKEND / "fix_guard.py")], input=json.dumps(ev),  # noqa: E731
                                        capture_output=True, text=True, env=dict(os.environ, RIVET_FIX_SESSION=str(sess)))
        self.assertEqual(run({"tool_name": "Bash", "tool_input": {"command": cmds[0]}}).stdout.strip(), "")
        out = json.loads(run({"tool_name": "Bash", "tool_input": {"command": "ls ~"}}).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class FixRuns(unittest.TestCase):
    def env(self, **kw):
        e = dict(os.environ, RIVET_STATE_DIR=str(EA.STATE_PATH.parent), **kw)
        e.pop("RIVET_FIX_CANNED", None) if "RIVET_FIX_CANNED" not in kw else None
        return e

    def apply(self, *args, **env):
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_apply.py"), *args], capture_output=True, text=True,
                            timeout=120, env=self.env(**env))
        evs = []
        for x in cp.stdout.splitlines():
            try:
                evs.append(json.loads(x))
            except ValueError:
                pass                     # --fix-tool prints plain text for claude
        return cp, [e for e in evs if isinstance(e, dict)]

    def test_dry_run_asserts_and_never_calls_claude(self):
        marker = SCRATCH / "claude-was-called"
        trap = SCRATCH / "trap-claude.py"
        trap.write_text(f"#!/usr/bin/env python3\nopen({str(marker)!r}, 'w').write('x')\n")
        trap.chmod(0o755)
        write_state([])
        cp, evs = self.apply("--other-fix", AID, "--dry-run", RIVET_CLAUDE_BIN=str(trap))
        self.assertEqual(cp.returncode, 0, cp.stdout[-2000:] + cp.stderr)
        self.assertRegex(evs[-1]["summary"], r"^Dry run: \d+ checks passed; claude was not called\.$")
        self.assertEqual(evs[-1]["other"]["fix"]["verbs"], ["state", "logs", "retry", "restart", "reload"])
        self.assertFalse(marker.exists(), "claude was not run")

    def test_a_run_with_a_fake_claude_lands_its_line_in_the_row(self):
        write_state([failed_alexa()])
        cp, evs = self.apply("--other-fix", AID, RIVET_FIX_CANNED=str(FIXTURE), RIVET_CLAUDE_BIN=str(FAKE_CLAUDE))
        self.assertEqual(cp.returncode, 0, cp.stdout[-3000:] + cp.stderr)
        self.assertEqual((evs[-1]["status"], evs[-1]["other"]["result"]), ("ok", "fixed"))
        self.assertTrue(evs[-1]["summary"].startswith("Fixed: HACS had downloaded v5.18.0"))
        self.assertIn("Checking: Fix it: restart", [e.get("msg") for e in evs])
        row = state_row(AID)
        self.assertEqual(row["last_run"]["result"], "fixed")
        argv = json.loads((EA.STATE_PATH.parent / "fake-claude-argv.json").read_text())
        self.assertEqual(argv["problems"], [])
        log = (EA.STATE_PATH.parent / "logs" / "apply.log").read_text()
        self.assertIn('"step": "fix"', log, "the whole run is in apply.log")

    def test_refuses_a_row_that_did_not_fail(self):
        write_state([alexa()])
        cp, evs = self.apply("--other-fix", AID, RIVET_CLAUDE_BIN=str(FAKE_CLAUDE))
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(evs[-1]["other"]["result"], "refused")

    def test_claude_saying_fixed_is_checked_against_home_assistant(self):
        import rivet_doctor as D
        write_state([failed_alexa()])
        orig = (OF.gather, D.run_claude, OF.verify)
        OF.gather = lambda it, ctx=None: {"apply_log": ["x"], "ha_log": ["y"], "live": {}, "canned": False}
        D.run_claude = lambda *a, **k: {"fixed": True, "result": "Fixed: restarted", "cause": "c", "actions": ["restart"]}
        OF.verify = lambda it, ctx=None: (False, "v5.18.0 is downloaded but Home Assistant still runs 5.15.7 (restart pending)")
        try:
            em = FakeEm()
            rc = OF.run(em, AID, json.loads(EA.STATE_PATH.read_text()))
        finally:
            OF.gather, D.run_claude, OF.verify = orig
        self.assertEqual(rc, 1)
        self.assertEqual(em.last["summary"], "Not fixed: v5.18.0 is downloaded but Home Assistant still runs 5.15.7 (restart pending)")
        self.assertEqual(state_row(AID)["last_run"]["result"], "fix_failed")

    def test_verify_reads_the_loaded_version(self):
        it = failed_alexa()
        live = {"entity": {"installed_version": "v5.18.0"}, "loaded_version": "5.15.7", "restart_required_repairs": []}
        orig = OF.live_facts
        OF.live_facts = lambda ctx, it: live
        try:
            self.assertEqual(OF.verify(it, ctx=object())[0], False)
            live["loaded_version"] = "5.18.0"
            self.assertEqual(OF.verify(it, ctx=object()), (True, ""))
        finally:
            OF.live_facts = orig

    def test_tools_only_inside_a_session_and_mutating_ones_once(self):
        cp, _ = self.apply("--fix-tool", "retry", AID)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("only works inside a Rivet Fix it run", cp.stdout)
        sess = Path(tempfile.mkdtemp(prefix="rivet-fix-sess-", dir=SCRATCH))
        (sess / "meta.json").write_text(json.dumps({"id": AID, "verbs": ["state", "logs", "retry"]}))
        env = dict(RIVET_FIX_SESSION=str(sess), RIVET_FIX_CANNED=str(FIXTURE))
        cp, _ = self.apply("--fix-tool", "restart", AID, **env)
        self.assertEqual(cp.returncode, 2, "restart is not in an add-on's budget")
        cp, _ = self.apply("--fix-tool", "retry", "ha:update.other", **env)
        self.assertEqual(cp.returncode, 2, "another row's id is refused")
        first, _ = self.apply("--fix-tool", "retry", AID, **env)
        again, _ = self.apply("--fix-tool", "retry", AID, **env)
        self.assertEqual((first.returncode, again.returncode), (0, 3))
        self.assertIn("already ran once", again.stdout)
        self.assertEqual(self.apply("--fix-tool", "state", AID, **env)[0].returncode, 0, "read-only tools may repeat")
        self.assertIn('"loaded_version": "5.15.7"', self.apply("--fix-tool", "state", AID, **env)[0].stdout)


class Containers(unittest.TestCase):
    """show_containers (2026-11-01): off by default; off = the podman collector does not run."""

    def test_setting_default_off_and_read_from_settings_json(self):
        self.assertFalse(common.DEFAULT_SETTINGS["show_containers"])
        orig = common.SETTINGS_PATH
        try:
            common.SETTINGS_PATH = SCRATCH / "settings-off.json"
            self.assertFalse(E.show_containers())
            common.SETTINGS_PATH.write_text('{"show_containers": true}')
            self.assertTrue(E.show_containers())
            common.SETTINGS_PATH.write_text('{"show_containers": "yes"}')
            self.assertFalse(E.show_containers(), "only a real true counts")
        finally:
            common.SETTINGS_PATH = orig

    def test_off_means_the_podman_collector_never_runs(self):
        ran = []
        reg = [("npm", "npm globals", lambda c: (ran.append("npm"), E.result(checked=1))[1], 2, 0),
               ("podman", "Containers", lambda c: (ran.append("podman"), E.result(checked=3, items=[
                   E.row("podman", "docker.io/library/caddy:2", "2.10.0", "2.10.2")]))[1], 2, 0)]
        doc = E.collect(registry=reg, containers=False)
        self.assertEqual(ran, ["npm"])
        self.assertEqual([s["id"] for s in doc["sources"]], ["npm"])
        ran.clear()
        doc = E.collect(registry=reg, containers=True)
        self.assertEqual(sorted(ran), ["npm", "podman"])
        self.assertEqual([i["source"] for i in doc["items"]], ["podman"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
