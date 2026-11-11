#!/usr/bin/env python3
"""Home Assistant core, investigated and installed with care (2026-11-10, CONTRACT.md).

Nothing real is asked: Home Assistant, GitHub's releases API and the release posts are
backend/fixtures/fake_ha.py on 127.0.0.1 (canned notes from backend/fixtures/ha_core/), and
claude is app/qa/fake-claude.py (modes core-touches / core-none / core-stray / garbage) or a
missing binary.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "fixtures"))
os.environ.setdefault("RIVET_STATE_DIR", tempfile.mkdtemp(prefix="rivet-hacore-"))

import elsewhere as E  # noqa: E402
import elsewhere_apply as EA  # noqa: E402
import fake_ha  # noqa: E402
import ha_core as H  # noqa: E402
import other_fix  # noqa: E402

FAKE_CLAUDE = str(REPO / "app" / "qa" / "fake-claude.py")
CORE = "ha:update.home_assistant_core_update"


class Em:
    def __init__(self):
        self.events = []

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        self.events.append(dict(step=step, status=status, pct=pct, msg=msg, log=log, **extra))

    def done(self, status, *a, **kw):
        self.events.append(dict(step="done", status=status, **kw))

    @property
    def last(self):
        return self.events[-1]

    def msgs(self):
        return [e.get("msg") or "" for e in self.events]


def core_row(installed="2026.10.4", latest="2026.11.1", **kw):
    it = E.row("ha", "Home Assistant Core", installed, latest, kind="core", installable=True, backup=True,
               entity_id="update.home_assistant_core_update", key="update.home_assistant_core_update", **kw)
    return E.decide_action(it)


class Env:
    """Set env vars for one test, restore afterwards."""

    def __init__(self, tc, **kv):
        old = {k: os.environ.get(k) for k in kv}
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        tc.addCleanup(restore)


class WithFakeHA(unittest.TestCase):
    story = "ok"
    components = None

    def setUp(self):
        self.srv = fake_ha.FakeHA(self.story, components=self.components).start()
        self.addCleanup(self.srv.stop)
        old = (H.GH_RELEASES, H.SITE_OVERRIDE)
        H.GH_RELEASES, H.SITE_OVERRIDE = self.srv.url + "/gh/", self.srv.url
        self.addCleanup(lambda: (setattr(H, "GH_RELEASES", old[0]), setattr(H, "SITE_OVERRIDE", old[1])))
        try:
            H.cache_path().unlink()
        except OSError:
            pass
        self.ctx = E.Ctx(env=self.srv.env())


# ------------------------------------------------------------------------- deterministic rules
class Step(unittest.TestCase):
    def test_patch_inside_a_month_monthly_otherwise(self):
        self.assertEqual(H.step("2026.11.1", "2026.11.2"), "patch")
        self.assertEqual(H.step("2026.11.0", "2026.11.3"), "patch")
        self.assertEqual(H.step("2026.11", "2026.12"), "monthly")
        self.assertEqual(H.step("2026.10.4", "2026.11.1"), "monthly")
        self.assertEqual(H.step("2026.12.3", "2027.1.0"), "monthly")       # a year rollover is not "major"
        self.assertEqual(H.step("dev", "2026.11.1"), "monthly")            # unreadable: the careful answer
        self.assertEqual((H.severity_of("patch"), H.severity_of("monthly")), ("patch", "minor"))

    def test_which_releases_are_read(self):
        self.assertEqual(H.months_between("2026.9.3", "2026.11.2"), ["2026.10", "2026.11"])
        self.assertEqual(H.months_between("2026.11.1", "2026.11.2"), [])
        self.assertEqual(H.months_between("2026.11.1", "2027.2.0"), ["2026.12", "2027.1", "2027.2"])
        self.assertEqual(H.patch_tags("2026.11.1", "2026.11.3"), ["2026.11.2", "2026.11.3"])
        self.assertEqual(H.patch_tags("2026.10.4", "2026.11.2"), ["2026.11.1", "2026.11.2"])
        self.assertEqual(H.patch_tags("2026.10.4", "2026.11.0"), [])


class Rate(unittest.TestCase):
    def test_monthly_is_never_below_yellow(self):
        self.assertEqual(H.rate("monthly", notes_ok=True, model_ok=True), "yellow")
        self.assertEqual(H.rate("patch", notes_ok=True, model_ok=True), "green")

    def test_not_fully_checked_is_yellow_at_best(self):
        for kw in ({"notes_ok": False, "model_ok": True}, {"notes_ok": True, "model_ok": False},
                   {"notes_ok": True, "model_ok": True, "partial": True},
                   {"notes_ok": True, "model_ok": True, "unsure": True}):
            self.assertEqual(H.rate("patch", **kw), "yellow", kw)

    def test_a_touch_is_red_whatever_else(self):
        self.assertEqual(H.rate("patch", notes_ok=True, model_ok=True, touches=True), "red")
        self.assertEqual(H.rate("monthly", notes_ok=False, model_ok=False, touches=True), "red")


NOTES = {"ok": True, "error": None, "partial": False, "months_total": 1, "releases": [
    {"version": "2026.11", "url": "https://example.invalid/post", "breaking": [
        {"title": "MQTT", "domains": ["mqtt"], "text": "The discovery_prefix option in YAML is removed."},
        {"title": "Shelly", "domains": ["shelly"], "text": "An attribute was removed."},
        {"title": "Targeting labels", "domains": [], "text": "Labels now target config entities."}]}]}
USED = ["automation", "mqtt", "sensor", "sun", "template"]
NOT_MQTT = ["automation", "sensor", "sun", "template"]


def answer(**kw):
    return lambda prompt: json.dumps(dict({"touches": [], "untouched": 3, "verdict": "none_touch", "summary": "s"}, **kw))


class ModelMayOnlyRaise(unittest.TestCase):
    def test_none_touch_cannot_lower_what_the_notes_name(self):
        c = H.judge("2026.10.4", "2026.11.1", NOTES, USED, use_ai=True, ask=answer())
        self.assertEqual((c["status"], c["risk"]), ("checked", "red"))
        self.assertEqual([t["domain"] for t in c["touches"]], ["mqtt"])
        self.assertEqual(c["touches"][0]["by"], "notes")
        self.assertTrue(c["line"].startswith("Touches MQTT, which you use: the discovery_prefix option"), c["line"])

    def test_a_monthly_release_none_touching_stays_yellow(self):
        c = H.judge("2026.10.4", "2026.11.1", NOTES, NOT_MQTT, use_ai=True, ask=answer())
        self.assertEqual((c["status"], c["risk"], c["ai"]), ("checked", "yellow", True))
        self.assertEqual(c["line"], "3 breaking changes, none touch an integration you use.")

    def test_the_model_raises_a_patch_release_to_red(self):
        notes = dict(NOTES, releases=[dict(NOTES["releases"][0], version="2026.11.2")])
        c = H.judge("2026.11.1", "2026.11.2", notes, NOT_MQTT, use_ai=True, ask=answer(
            verdict="touches", touches=[{"change": 3, "integration": "automation", "what_changes": "Labels now target config entities",
                                         "what_to_do": "Check your labels."}]))
        self.assertEqual((c["step"], c["risk"]), ("patch", "red"))
        self.assertEqual(c["line"], "Touches Automation, which you use: labels now target config entities; check your config first.")

    def test_none_touch_that_names_one_is_a_touch(self):
        v = H.validate({"verdict": "none_touch", "touches": [{"change": 1, "integration": "mqtt"}], "untouched": 2},
                       [{"n": 1}, {"n": 2}, {"n": 3}], USED)
        self.assertEqual(v["verdict"], "touches")

    def test_a_touch_on_an_integration_not_in_use_is_dropped_and_not_trusted(self):
        notes = dict(NOTES, releases=[dict(NOTES["releases"][0], version="2026.11.2")])
        c = H.judge("2026.11.1", "2026.11.2", notes, NOT_MQTT, use_ai=True, ask=answer(
            verdict="touches", touches=[{"change": 1, "integration": "zwave_js", "what_changes": "x", "what_to_do": "y"}]))
        self.assertEqual((c["touches"], c["verdict"], c["risk"]), ([], "unsure", "yellow"))   # a patch, but not green

    def test_bad_json_missing_claude_and_ai_off_say_so(self):
        for ask, why in ((lambda p: "lorem ipsum", "Claude could not judge them"),
                         (lambda p: (_ for _ in ()).throw(RuntimeError("claude CLI not found")), "claude CLI not found"),
                         (lambda p: json.dumps({"verdict": "maybe"}), "verdict 'maybe'")):
            c = H.judge("2026.11.1", "2026.11.2", dict(NOTES, releases=[dict(NOTES["releases"][0], version="2026.11.2")]),
                        NOT_MQTT, use_ai=True, ask=ask)
            self.assertEqual((c["status"], c["risk"], c["line"]), ("unchecked", "yellow", H.UNCHECKED_LINE))
            self.assertIn(why, c["reason"])
        c = H.judge("2026.11.1", "2026.11.2", NOTES, NOT_MQTT, use_ai=False, ask=lambda p: self.fail("no claude when AI is off"))
        self.assertEqual((c["status"], c["risk"]), ("unchecked", "yellow"))
        self.assertIn("AI is off", c["reason"])

    def test_unreadable_notes_are_never_safe(self):
        c = H.judge("2026.11.1", "2026.11.2", {"ok": False, "error": "could not read the notes of 2026.11.2 (HTTP 500)",
                                               "releases": []}, USED, use_ai=True, ask=answer())
        self.assertEqual((c["status"], c["risk"], c["line"], c["breaking"]), ("unchecked", "yellow", H.UNCHECKED_LINE, None))
        self.assertIn("HTTP 500", c["reason"])

    def test_nothing_listed_is_said_plainly(self):
        notes = {"ok": True, "releases": [{"version": "2026.11.2", "url": "u", "breaking": []}]}
        c = H.judge("2026.11.1", "2026.11.2", notes, USED, use_ai=True, ask=lambda p: self.fail("nothing to ask"))
        self.assertEqual((c["status"], c["risk"], c["line"]), ("no_breaking", "green", "No breaking changes listed for 2026.11.2."))


class Parse(unittest.TestCase):
    def test_the_release_post(self):
        p = H.parse_post((BACKEND / "fixtures" / "ha_core" / "blog-202611.html").read_text())
        self.assertTrue(p["found"])
        self.assertEqual([(c["title"], c["domains"]) for c in p["breaking"]],
                         [("MQTT", ["mqtt"]), ("Shelly", ["shelly"]), ("HERE Travel Time", ["here_travel_time"]),
                          ("Targeting labels in automations and scripts", [])])
        self.assertTrue(p["breaking"][0]["text"].startswith("The discovery_prefix option in YAML is removed;"))
        self.assertNotIn("zha", json.dumps(p), "only the backward-incompatible section counts")

    def test_a_post_whose_section_cannot_be_parsed_is_never_safe(self):
        page = "<h2>Backward-incompatible changes</h2><div class='new-layout'><span>MQTT</span> changed.</div><h2>All changes</h2>"
        rel = lambda url: (url, json.dumps({"body": "https://www.home-assistant.io/blog/2026/11/04/release-202611/"})  # noqa: E731
                           if "/gh/" in url else page)
        old = H.GH_RELEASES
        H.GH_RELEASES = "http://x/gh/"
        try:
            n = H.fetch_notes("2026.10.4", "2026.11.0", get=rel)
        finally:
            H.GH_RELEASES = old
        self.assertFalse(n["ok"])
        self.assertIn("could not read the changes", n["error"])

    def test_farewell_counts_and_a_missing_section_is_not_found(self):
        p = H.parse_post((BACKEND / "fixtures" / "ha_core" / "blog-202610.html").read_text())
        self.assertEqual([c["title"] for c in p["breaking"]][-1], "Removed: Acme Cloud")
        self.assertFalse(H.parse_post("<h2>New things</h2><p>All good.</p>")["found"])

    def test_a_github_release_body(self):
        g = H.parse_gh_body("\n".join(fake_ha.PATCH_NOTES["2026.11.2"]))
        self.assertEqual([(c["domains"], c["title"]) for c in g["breaking"]],
                         [(["sonos"], "Remove the deprecated `household` option of Sonos")])
        self.assertEqual(g["changes"], 2)
        self.assertEqual(H.parse_gh_body("https://www.home-assistant.io/blog/2026/11/04/release-202611/")["post"],
                         "https://www.home-assistant.io/blog/2026/11/04/release-202611/")


# ------------------------------------------------------------------------- notes over HTTP
class Fetch(WithFakeHA):
    def test_a_skipped_month_is_read_too(self):
        n = H.fetch_notes("2026.9.3", "2026.11.2")
        self.assertTrue(n["ok"], n["error"])
        self.assertEqual([(r["version"], len(r["breaking"])) for r in n["releases"]],
                         [("2026.10", 3), ("2026.11", 4), ("2026.11.1", 0), ("2026.11.2", 1)])

    def test_a_release_without_notes_is_could_not_read(self):
        n = H.fetch_notes("2026.11.1", "2026.12.0")
        self.assertFalse(n["ok"])
        self.assertIn("2026.12.0 (not published yet)", n["error"])

    def test_a_long_skip_is_partial(self):
        old = H.MAX_MONTHS
        H.MAX_MONTHS = 1
        try:
            n = H.fetch_notes("2026.9.3", "2026.11.1")
        finally:
            H.MAX_MONTHS = old
        self.assertTrue(n["ok"] and n["partial"])
        self.assertEqual([r["version"] for r in n["releases"]], ["2026.11", "2026.11.1"])
        c = H.judge("2026.9.3", "2026.11.1", n, fake_ha.COMPONENTS_NONE, use_ai=True, ask=answer(untouched=4))
        self.assertEqual(c["risk"], "yellow")

    def test_nothing_reachable_is_could_not_read(self):
        self.srv.stop()
        n = H.fetch_notes("2026.10.4", "2026.11.1", budget=5)
        self.assertFalse(n["ok"])
        self.assertIn("could not read the notes of 2026.11.0", n["error"])


# ------------------------------------------------------------------------- the collect layer
class Investigate(WithFakeHA):
    def run_it(self, mode, *, use_ai=True, row=None, ask=None, now=None, claude=FAKE_CLAUDE):
        Env(self, RIVET_CLAUDE_BIN=claude, FAKE_CLAUDE_MODE=mode)
        it = row or core_row()
        n = H.investigate([it], use_ai=use_ai, ctx=self.ctx, ask=ask, now=now)
        return it, n

    def test_touches_through_the_real_cli_call(self):
        it, n = self.run_it("core-touches")
        c = it["core_check"]
        self.assertEqual((n, c["status"], c["risk"], it["risk"], it["severity"]), (1, "checked", "red", "red", "minor"))
        self.assertTrue(c["ai"] and it["ai"] == ["core_check"])
        self.assertEqual(c["line"], "Touches MQTT and Automation, which you use: the discovery_prefix option in YAML is "
                                    "removed; check your config first.")
        self.assertEqual((c["breaking"], c["untouched"], c["in_use"]), (4, 2, 15))
        self.assertEqual(c["notes_url"], "https://www.home-assistant.io/blog/2026/11/04/release-202611/")
        self.assertTrue(it["confirm"] and it["action"] == "ha_install")
        self.assertEqual(self.srv.calls("POST"), [], "collecting only reads (no POST to Home Assistant)")

    def test_none_touch(self):
        self.srv.components = list(fake_ha.COMPONENTS_NONE)
        it, _ = self.run_it("core-none")
        c = it["core_check"]
        self.assertEqual((c["status"], c["risk"], c["line"]),
                         ("checked", "yellow", "4 breaking changes, none touch an integration you use."))

    def test_garbage_and_missing_claude_are_could_not_check(self):
        for mode, claude in (("garbage", FAKE_CLAUDE), ("core-touches", "/nonexistent/claude")):
            self.srv.components = list(fake_ha.COMPONENTS_NONE)
            try:
                H.cache_path().unlink()
            except OSError:
                pass
            it, _ = self.run_it(mode, claude=claude)
            c = it["core_check"]
            self.assertEqual((c["status"], c["risk"], c["line"]), ("unchecked", "yellow", H.UNCHECKED_LINE), mode)

    def test_the_cache_runs_it_once_per_release(self):
        asked = []

        def ask(p):
            asked.append(p)
            return H.ask_claude(p)
        self.run_it("core-touches", ask=ask)
        gh = len(self.srv.calls("GET"))
        it, n = self.run_it("core-touches", ask=ask)
        self.assertEqual((len(asked), n, it["core_check"]["risk"]), (1, 0, "red"))
        self.assertEqual(len(self.srv.calls("GET")), gh, "nothing fetched again")
        fresh = core_row()                          # what the next collect_ha writes: the cached answer
        H.prepare(fresh)
        self.assertEqual(fresh["core_check"]["line"], it["core_check"]["line"])
        other = core_row(latest="2026.11.2")        # a new target is a new question
        H.prepare(other)
        self.assertEqual(other["core_check"]["status"], "pending")

    def test_a_failure_is_retried_later_not_every_hour(self):
        asked = []
        bad = lambda p: asked.append(1) or "not json"           # noqa: E731
        self.run_it("x", ask=bad)
        self.run_it("x", ask=bad)
        self.assertEqual(len(asked), 1, "within FAIL_RETRY_S the failure is reused")
        it, _ = self.run_it("x", ask=bad, now=time.time() + H.FAIL_RETRY_S + 5)
        self.assertEqual(len(asked), 2)
        self.assertEqual(it["core_check"]["status"], "unchecked")

    def test_ai_off_then_on(self):
        it, _ = self.run_it("core-touches", use_ai=False)
        c = it["core_check"]
        self.assertEqual((c["status"], c["risk"], c["ai"]), ("unchecked", "red", False))     # the notes name MQTT
        self.assertEqual(c["line"], "Touches MQTT, which you use (its release notes name it). Read them before installing.")
        it, n = self.run_it("core-touches", use_ai=True)
        self.assertEqual((n, it["core_check"]["status"]), (1, "checked"), "turning AI on asks at once")

    def test_collect_ha_marks_core_installable_and_deterministic(self):
        r = E.collect_ha(self.ctx)
        (it,) = r["items"]
        E.decide_action(it)
        self.assertEqual((it["kind"], it["action"], it["confirm"], it["severity"]), ("core", "ha_install", True, "minor"))
        self.assertEqual(it["core_check"]["status"], "pending")
        self.assertEqual(it["core_check"]["risk"], "yellow")
        self.assertEqual(it["link"], "https://www.home-assistant.io/latest-release-notes/")
        self.assertEqual(it["ha_link"], self.srv.url + "/config/updates")


# ------------------------------------------------------------------------- the install
class Install(WithFakeHA):
    def setUp(self):
        super().setUp()
        for k, v in (("HA_POLL_S", 0.05), ("HA_CORE_TIMEOUT_S", 3), ("HA_CORE_IDLE_S", 0.6)):
            old = getattr(EA, k)
            setattr(EA, k, v)
            self.addCleanup(setattr, EA, k, old)
        self.it = core_row()
        EA.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EA.STATE_PATH.write_text(json.dumps({"schema": 1, "groups": [], "elsewhere": {
            "counts": {}, "sources": [{"id": "ha", "stale": 1}], "items": [self.it]}}))

    def install(self, confirmed=True):
        em = Em()
        rc = EA.run_core(em, self.it, ctx=self.ctx, confirmed=confirmed)
        return rc, em

    def row(self):
        return next(i for i in json.loads(EA.STATE_PATH.read_text())["elsewhere"]["items"] if i["id"] == CORE)


class InstallOk(Install):
    def test_goes_away_and_comes_back_on_the_new_version(self):
        rc, em = self.install()
        self.assertEqual(rc, 0, em.last)
        self.assertEqual(em.last["other"]["result"], "installed")
        self.assertEqual(em.last["summary"], "Home Assistant is now 2026.11.1 and running.")
        self.assertIn("Home Assistant is restarting, this is expected. Rivet waits for it (up to 3 s)…", em.msgs())
        self.assertEqual(self.srv.calls("POST", "/api/services/update/install")[0][2],
                         {"entity_id": "update.home_assistant_core_update", "version": "2026.11.1", "backup": True})
        posts = [r[1] for r in self.srv.calls("POST")]
        self.assertEqual(posts, ["/api/config/core/check_config", "/api/services/update/install"], "check_config first")
        self.assertEqual((self.row()["status"], self.row()["installed"]), ("updated", "2026.11.1"))
        self.assertFalse(EA.core_lock_path().exists(), "the lock goes with the run")

    def test_refused_without_the_confirmation(self):
        em = Em()
        rc = EA.run(em, CORE, json.loads(EA.STATE_PATH.read_text()))
        self.assertEqual((rc, em.last["other"]["result"]), (2, "refused"))
        self.assertIn("only after you confirm it", em.last["summary"])
        self.assertEqual(self.srv.calls("POST"), [])

    def test_no_other_ha_row_while_core_runs(self):
        EA.core_lock_path().write_text(f"{os.getppid()} now\n")      # a live process that is not us
        self.addCleanup(lambda: EA.core_lock_path().unlink(missing_ok=True))
        addon = E.decide_action(E.row("ha", "Samba share", "1.0.0", "1.0.1", kind="addon", installable=True,
                                      entity_id="update.samba_share_update", key="update.samba_share_update"))
        em = Em()
        rc = EA.run(em, addon["id"], {"elsewhere": {"items": [addon, self.it]}})
        self.assertEqual(rc, 2)
        self.assertIn("Home Assistant core is being updated right now", em.last["summary"])
        em = Em()
        self.assertEqual(EA.run_ha_restart(em, {"elsewhere": {"items": []}}), 2)
        em = Em()
        self.assertEqual(EA.run(em, CORE, {"elsewhere": {"items": [self.it]}}, confirmed=True), 2)
        self.assertEqual(self.srv.calls("POST"), [])
        EA.core_lock_path().write_text("999999999 stale\n")          # a dead pid does not block
        self.assertIsNone(EA.core_running())


class InstallRollback(Install):
    story = "rollback"

    def test_back_on_the_old_version_is_a_failure_fix_it_can_take(self):
        rc, em = self.install()
        self.assertEqual(rc, 1)
        self.assertIn("came back on 2026.10.4, not 2026.11.1", em.last["summary"])
        self.assertIn("rolled back", em.last["summary"])
        self.assertEqual(self.row()["last_run"]["result"], "failed")          # what puts Fix it on the row
        self.assertEqual(self.row()["status"], "ok")
        self.assertEqual(other_fix.verbs_for(self.row()), ["state", "logs"])   # Fix it reads, never re-installs


class InstallNever(Install):
    story = "never"

    def test_never_back_fails_at_the_cap(self):
        t0 = time.monotonic()
        rc, em = self.install()
        self.assertEqual(rc, 1)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("has not come back after 3 s", em.last["summary"])
        self.assertTrue(em.last["other"]["unreachable"])


class InstallInvalid(Install):
    story = "invalid"

    def test_an_invalid_config_installs_nothing(self):
        rc, em = self.install()
        self.assertEqual(rc, 1)
        self.assertIn("configuration does not check out", em.last["summary"])
        self.assertIn("nothing was installed", em.last["summary"])
        self.assertEqual(self.srv.calls("POST", "/api/services/update/install"), [])


class InstallNoBackup(Install):
    story = "nobackup"

    def test_no_backup_feature_says_so_and_still_installs(self):
        rc, em = self.install()
        self.assertEqual(rc, 0)
        self.assertTrue(any("cannot take a backup" in m and "as you confirmed" in m for m in em.msgs()))
        self.assertNotIn("backup", self.srv.calls("POST", "/api/services/update/install")[0][2])


class InstallIdle(Install):
    story = "idle"

    def test_nothing_happens_is_a_failure(self):
        rc, em = self.install()
        self.assertEqual(rc, 1)
        self.assertIn("never started installing it (still 2026.10.4", em.last["summary"])


# ------------------------------------------------------------------------- buttons
class Actions(unittest.TestCase):
    def test_core_installs_os_supervisor_firmware_stay_open(self):
        core = core_row()
        self.assertEqual((core["action"], core["confirm"]), ("ha_install", True))
        for kind in ("os", "supervisor", "firmware"):
            it = E.decide_action(E.row("ha", kind, "1.0", "1.1", kind=kind, installable=True, link="http://ha/config/updates"))
            self.assertEqual(it["action"], "open", kind)
        old = E.decide_action(E.row("ha", "Home Assistant Core", "2026.10.4", "2026.11.1", kind="core",
                                    link="http://ha/config/updates"))
        self.assertEqual(old["action"], "open", "a core entity without the INSTALL feature stays Open")

    def test_the_os_refusal_is_unchanged(self):
        os_row = E.row("ha", "HA OS", "18.3", "18.4", kind="os", action="ha_install", entity_id="update.home_assistant_operating_system_update")
        with self.assertRaises(EA.Refused) as cm:
            EA.run_ha(Em(), os_row)
        self.assertIn("updated by hand", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
