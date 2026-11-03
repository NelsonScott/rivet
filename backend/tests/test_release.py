#!/usr/bin/env python3
"""A newer Fedora release (backend/release.py, 2026-11-03): the hourly check, the row, the
repo probe, the download step of rivet-apply, and the restart into the installer.

    python3 -m unittest tests.test_release

Nothing here reaches the network, dnf, polkit or the real /etc/yum.repos.d: the release
list and the HTTP probe are fakes, repos live in a temp dir, dnf5 is backend/fixtures/
fake_dnf.py, and `dnf5 system-upgrade` is a scripted LineStream that records its argv.
"""
from __future__ import annotations

import http.server
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "fixtures"))

import notify  # noqa: E402
import release  # noqa: E402
import rules  # noqa: E402
import triage  # noqa: E402
from fake_dnf import FakeWorld  # noqa: E402

STATE = json.loads((BACKEND / "fixtures" / "state-selfupdate.json").read_text())
HOWDY = "copr:copr.fedorainfracloud.org:scottnelson:howdy"
# the shape of https://fedoraproject.org/releases.json: one entry per image
RELEASES = [
    {"version": "45 Beta", "arch": "x86_64", "variant": "Workstation"},
    {"version": "45", "arch": "x86_64", "variant": "Workstation"},
    {"version": "45", "arch": "aarch64", "variant": "Spins"},
    {"version": "44", "arch": "x86_64", "variant": "Workstation"},
    {"version": "43", "arch": "x86_64", "variant": "Labs"},
    {"version": "rawhide", "arch": "x86_64", "variant": "Everything"},
]


def rel(**kw) -> dict:
    base = {"version": 45, "current": 44, "repos": [], "missing_repos": [], "checked_repos": 0,
            "downloaded": False, "notes_url": release.NOTES_URL.format(v=45)}
    base.update(kw)
    return base


class WhatIsOut(unittest.TestCase):
    def test_the_newest_numbered_release_wins_never_a_beta(self):
        self.assertEqual(release.newest_release(RELEASES), 45)
        self.assertEqual(release.newest_release([{"version": "46 Beta"}, {"version": "45"}]), 45)
        self.assertIsNone(release.newest_release([]))
        self.assertIsNone(release.newest_release({"not": "a list"}))
        self.assertIsNone(release.newest_release([{"version": "rawhide"}, {}, None]))

    def test_up_to_date_is_none_and_newer_is_the_facts(self):
        orig = (release.fetch_json, release.current_version, release.offline_staged)
        release.fetch_json = lambda url, timeout=0: RELEASES
        release.offline_staged = lambda v, offline_dir=None: False
        try:
            release.current_version = lambda: 45
            self.assertIsNone(release.check(probe=False))
            release.current_version = lambda: 44
            r = release.check(probe=False)
        finally:
            release.fetch_json, release.current_version, release.offline_staged = orig
        self.assertEqual((r["version"], r["current"], r["checked_repos"], r["downloaded"]), (45, 44, 0, False))
        self.assertEqual(r["notes_url"], "https://docs.fedoraproject.org/en-US/fedora/f45/release-notes/")

    def test_no_network_or_no_fedora_is_quietly_none(self):
        orig = (release.fetch_json, release.current_version)
        try:
            def boom(url, timeout=0):
                raise OSError("no route to host")
            release.fetch_json = boom
            release.current_version = lambda: 44
            self.assertIsNone(release.check(probe=False))
            release.fetch_json = lambda url, timeout=0: RELEASES
            release.current_version = lambda: None
            self.assertIsNone(release.check(probe=False))
        finally:
            release.fetch_json, release.current_version = orig

    def test_version_id_is_read_from_os_release(self):
        tmp = Path(tempfile.mkdtemp(prefix="rivet-osr-"))
        (tmp / "os-release").write_text('NAME="Fedora Linux"\nVERSION_ID=44\nPRETTY_NAME="Fedora Linux 44"\n')
        orig = release.OS_RELEASE
        try:
            release.OS_RELEASE = tmp / "os-release"
            self.assertEqual(release.current_version(), 44)
            (tmp / "os-release").write_text('VERSION_ID="sid"\n')
            self.assertIsNone(release.current_version())
            release.OS_RELEASE = tmp / "missing"
            self.assertIsNone(release.current_version())
        finally:
            release.OS_RELEASE = orig

    def test_a_fixture_fills_in_the_defaults(self):
        r = release.check(fixture={"version": "45"})
        self.assertEqual((r["version"], r["current"], r["missing_repos"], r["downloaded"]), (45, 44, [], False))
        self.assertIsNone(release.check(fixture={"version": "soon"}))


class Repos(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rivet-repos-"))
        (self.dir / "_copr:good.repo").write_text(
            "[copr:copr.fedorainfracloud.org:someone:good]\nname=Copr repo for good\nenabled=1\n"
            "baseurl=https://download.copr.fedorainfracloud.org/results/someone/good/fedora-$releasever-$basearch/\n")
        (self.dir / "_copr:gone.repo").write_text(
            f"[{HOWDY}]\nname=Copr repo for howdy\nenabled=1\n"
            "baseurl=https://download.copr.fedorainfracloud.org/results/scottnelson/howdy/fedora-$releasever-$basearch/\n")
        (self.dir / "fedora.repo").write_text(
            "[fedora]\nname=Fedora $releasever - $basearch\nenabled=1\n"
            "#baseurl=http://download.example/pub/fedora/linux/releases/$releasever/Everything/$basearch/os/\n"
            "metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch\n\n"
            "[fedora-debuginfo]\nname=debug\nenabled=0\n"
            "metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-debug-$releasever&arch=$basearch\n")
        (self.dir / "vendor.repo").write_text(
            "[google-chrome]\nname=google-chrome\nenabled=1\n"
            "baseurl=https://dl.google.com/linux/chrome/rpm/stable/x86_64\n")
        (self.dir / "flaky.repo").write_text(
            "[flaky]\nname=Flaky\nenabled=1\nmirrorlist=https://flaky.example/mirrors?release=$releasever\n")
        (self.dir / "broken.repo").write_text("this is not an ini file\n[unterminated\n")

    def test_only_enabled_repos_that_follow_releasever_are_probed(self):
        repos = release.enabled_release_repos(self.dir)
        self.assertEqual(sorted(r["id"] for r in repos),
                         sorted(["copr:copr.fedorainfracloud.org:someone:good", HOWDY, "fedora", "flaky"]))
        kinds = {r["id"]: r["kind"] for r in repos}
        self.assertEqual((kinds["fedora"], kinds["flaky"], kinds[HOWDY]), ("metalink", "mirrorlist", "baseurl"))

    def test_probe_urls(self):
        repos = {r["id"]: r for r in release.enabled_release_repos(self.dir)}
        self.assertEqual(release.probe_url(repos[HOWDY], 45, "x86_64"),
                         "https://download.copr.fedorainfracloud.org/results/scottnelson/howdy/fedora-45-x86_64/repodata/repomd.xml")
        self.assertEqual(release.probe_url(repos["fedora"], 45, "x86_64"),
                         "https://mirrors.fedoraproject.org/metalink?repo=fedora-45&arch=x86_64")

    def test_statuses_ok_missing_unknown(self):
        def status(url):
            return 200 if "good" in url or "fedoraproject" in url else 404 if "howdy" in url else None
        out = release.probe_repos(45, repos_dir=self.dir, arch="x86_64", status=status)
        by = {r["id"]: r["status"] for r in out}
        self.assertEqual(by, {"copr:copr.fedorainfracloud.org:someone:good": "ok", HOWDY: "missing",
                              "fedora": "ok", "flaky": "unknown"})
        self.assertEqual([r["id"] for r in out if r["status"] == "missing"], [HOWDY])

    def test_no_repos_means_no_probe(self):
        self.assertEqual(release.probe_repos(45, repos_dir=self.dir / "nothing", status=lambda u: 200), [])

    def test_http_status_follows_redirects_and_reads_404(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/moved":
                    self.send_response(301)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                self.send_response(200 if self.path == "/ok" else 404)
                self.end_headers()
                self.wfile.write(b"x")

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            self.assertEqual(release.http_status(base + "/ok", timeout=5), 200)
            self.assertEqual(release.http_status(base + "/moved", timeout=5), 200)
            self.assertEqual(release.http_status(base + "/gone", timeout=5), 404)
        finally:
            srv.shutdown()
        self.assertIsNone(release.http_status("http://127.0.0.1:1/nothing", timeout=2))


class Downloaded(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rivet-offline-"))

    def test_no_transaction_means_not_downloaded(self):
        self.assertFalse(release.offline_staged(45, self.dir))
        self.assertFalse(release.offline_staged(45, self.dir / "missing"))

    def test_a_transaction_for_this_release_counts(self):
        (self.dir / "transaction.json").write_text('{"releasever": "45", "cmd_line": "dnf5 system-upgrade download"}')
        self.assertTrue(release.offline_staged(45, self.dir))
        self.assertFalse(release.offline_staged(46, self.dir))

    def test_a_transaction_that_names_no_release_counts_too(self):
        (self.dir / "transaction.json").write_text('{"cmd_line": "dnf5 offline upgrade"}')
        self.assertTrue(release.offline_staged(45, self.dir))


class Row(unittest.TestCase):
    def test_yellow_reboot_badge_and_never_ticked_by_default(self):
        it = release.item(rel())
        flags = rules.apply_rules([it], steam_running=False)
        self.assertEqual((it["risk"], it["badge"], it["requires_reboot"], it["selected_default"], it["blocked_by"]),
                         ("yellow", "reboot", True, False, None))
        self.assertEqual((it["id"], it["source"], it["display_name"], it["from"], it["to"], it["op"]),
                         ("release:45", "dnf", "Fedora 45", "44", "45", "release"))
        self.assertTrue(flags["reboot_required"])
        self.assertFalse(flags["kernel_pending"] or flags["akmod_nvidia_pending"])
        self.assertEqual(flags["security_count"], 0)

    def test_the_words(self):
        line, why = release.verdict(rel())
        self.assertEqual(line, "Fedora 45 is out. Finish the other updates first, then upgrade when you have an hour.")
        self.assertIn("20 to 60 minutes", why)
        line, why = release.verdict(rel(missing_repos=[HOWDY], checked_repos=14,
                                       repos=[{"id": "flaky", "status": "unknown"}]))
        self.assertIn("1 of your repos has no Fedora 45 builds yet (scottnelson/howdy)", line)
        self.assertIn("1 of 14 enabled repos has no Fedora 45 packages yet (scottnelson/howdy)", why)
        self.assertIn("1 repo could not be checked (flaky)", why)
        line, _ = release.verdict(rel(downloaded=True))
        self.assertEqual(line, "Fedora 45 is downloaded. Restart to install it when you have an hour.")
        for n in ("Tom", "Dick", "Harry", "Sue"):
            pass
        line, _ = release.verdict(rel(missing_repos=["a", "b", "c", "d"], checked_repos=9))
        self.assertIn("4 of your repos have no Fedora 45 builds yet (a, b, c, …)", line)

    def test_deterministic_triage_uses_those_words_and_the_model_never_sees_it(self):
        it = release.item(rel(missing_repos=[HOWDY], checked_repos=14))
        rules.apply_rules([it], steam_running=False)
        d = triage.deterministic(it)
        self.assertTrue(d["verdict"].startswith("Fedora 45 is out. 1 of your repos"))
        self.assertIsNone(d["risk_raise"])
        self.assertTrue(release.is_release(it))
        self.assertFalse(release.is_release({"op": "install"}) or release.is_release(None))

    def test_notification_says_it(self):
        it = release.item(rel())
        kern = dict(STATE["groups"][0]["items"][2])
        items = [kern, it]
        flags = rules.apply_rules(items, steam_running=False)
        title, body = notify.build_body(items, flags)
        self.assertEqual(title, "Rivet: 2 updates")
        self.assertIn("Fedora 45 is out", body)


class Collector(unittest.TestCase):
    def run_collect(self, fixture: dict) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="rivet-rel-"))
        (tmp / "fx.json").write_text(json.dumps(fixture))
        env = dict(os.environ, RIVET_STATE_DIR=str(tmp / "state"), RIVET_CONFIG_DIR=str(tmp / "cfg"),
                   RIVET_NO_NOTIFY="1", RIVET_NO_HEALTH="1")
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--no-llm", "--quiet",
                             "--fixture", str(tmp / "fx.json")], env=env, capture_output=True,
                            text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads((tmp / "state" / "state.json").read_text())

    def test_the_row_is_the_last_dnf_item_counted_and_kept_whole(self):
        dnf = [dict(i) for i in STATE["groups"][0]["items"][:3]]
        doc = self.run_collect({"dnf": dnf, "release": {"version": 45, "current": 44,
                                                        "missing_repos": [HOWDY], "checked_repos": 14}})
        items = doc["groups"][0]["items"]
        self.assertEqual(items[-1]["id"], "release:45")
        self.assertEqual(len(items), 4)
        row = items[-1]
        self.assertEqual((row["op"], row["risk"], row["badge"], row["selected_default"]), ("release", "yellow", "reboot", False))
        self.assertEqual(row["release"]["missing_repos"], [HOWDY])
        self.assertIn("scottnelson/howdy", row["verdict"])
        self.assertEqual(row["notes_url"], "https://docs.fedoraproject.org/en-US/fedora/f45/release-notes/")
        self.assertEqual(doc["summary"]["total"], 4)
        self.assertTrue(doc["flags"]["reboot_required"])

    def test_without_a_release_nothing_changes(self):
        dnf = [dict(i) for i in STATE["groups"][0]["items"][:2]]
        doc = self.run_collect({"dnf": dnf})
        self.assertEqual([i["id"] for i in doc["groups"][0]["items"]], [i["id"] for i in dnf])
        self.assertFalse(any(i.get("op") == "release" for i in doc["groups"][0]["items"]))


class FakeStream:
    """A scripted `dnf5 system-upgrade`: records argv, plays lines, exits with rc."""
    calls: list[list[str]] = []
    lines_out: list[str] = []
    rc = 0
    timed_out = False

    def __init__(self, cmd, timeout):
        FakeStream.calls.append(list(cmd))
        self.returncode = None
        self.timed_out = False

    def lines(self):
        for ln in FakeStream.lines_out:
            yield ln
        self.returncode = FakeStream.rc
        self.timed_out = FakeStream.timed_out


DOWNLOAD_LINES = [
    "Updating and loading repositories:",
    "Repositories loaded.",
    "Total size of inbound packages is 3.1 GiB. Need to download 3.1 GiB.",
    "[   1/1834] glibc-0:2.43-1.fc45.x86_64  100% | 12.4 MiB/s | 2.3 MiB | 00m00s",
    "[ 900/1834] kernel-core-0:7.3.2-300.fc45.x86_64  100% | 30.1 MiB/s | 61.0 MiB | 00m02s\r",
    "[1834/1834] zlib-ng-compat-0:2.3.1-1.fc45.x86_64  100% | 1.1 MiB/s | 0.1 MiB | 00m00s",
    "Download complete! Use `dnf5 system-upgrade reboot` to start the upgrade.",
]


def state_with_release(**kw) -> tuple[dict, dict]:
    st = json.loads(json.dumps(STATE))
    it = release.item(rel(**kw))
    rules.apply_rules([it], steam_running=False)
    st["groups"][0]["items"].append(it)
    return st, it


def run_apply(world, ids, state, *, prefix=("pkexec",), lines=DOWNLOAD_LINES, rc=0, timed_out=False,
              resolve_only=False):
    import rivet_apply
    A = importlib.reload(rivet_apply)
    world.install(A)
    A.stream_cmd = lambda cmd, em, step, **kw: 0
    A.AKMOD_POLL_S = 0
    A.akmod_installed = lambda: True
    A.root_prefix = lambda: list(prefix)
    FakeStream.calls, FakeStream.lines_out, FakeStream.rc, FakeStream.timed_out = [], list(lines), rc, timed_out
    A.LineStream = FakeStream
    buf = io.StringIO()
    em = A.Emitter(buf)
    code = A.run_real(em, ids, resolve_only=resolve_only, state=state)
    em.logfh.close()
    evs = [json.loads(x) for x in buf.getvalue().splitlines()]
    return code, evs, evs[-1], A


class Apply(unittest.TestCase):
    def test_runs_last_with_the_missing_repos_disabled_and_done_carries_it(self):
        st, it = state_with_release(missing_repos=[HOWDY], checked_repos=14)
        curl = next(i for i in st["groups"][0]["items"] if i["name"] == "curl")
        w = FakeWorld.from_items([curl])
        rc, evs, done, _ = run_apply(w, [curl["id"], it["id"]], st)
        self.assertEqual((rc, done["status"], done["reboot_required"]), (0, "ok", True))
        steps = [e["step"] for e in evs]
        self.assertLess(steps.index("install"), steps.index("release"), "the release runs after the rpm transaction")
        self.assertEqual(FakeStream.calls, [["pkexec", "dnf5", "system-upgrade", "download", "--releasever=45", "-y",
                                             f"--disable-repo={HOWDY}"]])
        rel_evs = [e for e in evs if e["step"] == "release"]
        self.assertEqual(rel_evs[0]["status"], "start")
        self.assertIn("Leaving out 1 repo with no Fedora 45 build yet: " + HOWDY, rel_evs[1]["msg"])
        self.assertIn("Asking for permission", rel_evs[2]["msg"])
        self.assertTrue(any(e["msg"] == "Fedora 45 is 3.1 GiB to download." for e in rel_evs))
        pcts = [e["pct"] for e in rel_evs if e["pct"] is not None]
        self.assertEqual(pcts, sorted(pcts), "progress never goes backwards")
        self.assertIn(49, pcts)
        self.assertEqual(rel_evs[-1]["status"], "ok")
        self.assertEqual(rel_evs[-1]["msg"], "Fedora 45 is downloaded (3.1 GiB). Restart to install it.")
        self.assertEqual(done["release"], {"version": 45, "downloaded": True, "status": "ok", "size": "3.1 GiB", "nvidia": True})
        self.assertEqual(done["results"]["release"]["packages"], 1834)
        self.assertEqual(done["results"]["release"]["disabled_repos"], [HOWDY])
        self.assertEqual(done["summary"], "Applied 1 package(s), Fedora 45 downloaded. Restart to install Fedora 45.")
        self.assertEqual(done["results"]["dnf"]["installed"], 1)

    def test_alone_in_the_plan_there_is_no_rpm_transaction(self):
        st, it = state_with_release()
        w = FakeWorld.from_items([])
        rc, evs, done, _ = run_apply(w, [it["id"]], st, prefix=())
        self.assertEqual((rc, done["status"]), (0, "ok"))
        self.assertNotIn("install", [e["step"] for e in evs])
        self.assertEqual(w.transactions, [])
        self.assertEqual(FakeStream.calls, [["dnf5", "system-upgrade", "download", "--releasever=45", "-y"]])
        self.assertFalse(any("Asking for permission" in (e.get("msg") or "") for e in evs), "already root: no prompt line")
        self.assertEqual(done["summary"], "Applied Fedora 45 downloaded. Restart to install Fedora 45.")

    def test_skipped_when_the_rpm_transaction_failed(self):
        st, it = state_with_release()
        curl = next(i for i in st["groups"][0]["items"] if i["name"] == "curl")
        w = FakeWorld.from_items([curl])
        w.fail_tx = {0}
        w.drop = {"curl"}
        w.journal = {"self_restart": False, "error": "ERROR [rpm] unpacking of archive failed: No space left on device"}
        rc, evs, done, _ = run_apply(w, [curl["id"], it["id"]], st)
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(FakeStream.calls, [], "no download on top of a failed update")
        skip = next(e for e in evs if e["step"] == "release")
        self.assertEqual(skip["status"], "skip")
        self.assertIn("the updates before it did not all go through", skip["msg"])
        self.assertEqual(done["release"]["downloaded"], False)
        self.assertFalse(done["reboot_required"], "nothing landed, nothing to restart for")

    def test_a_dismissed_prompt_is_named_and_is_a_failure(self):
        st, it = state_with_release()
        w = FakeWorld.from_items([])
        rc, evs, done, _ = run_apply(w, [it["id"]], st, lines=[], rc=126)
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(done["cause"], "Fedora 45 was not downloaded: the permission prompt was dismissed, so nothing ran.")
        self.assertEqual(done["release"]["downloaded"], False)
        self.assertFalse(done["reboot_required"])

    def test_allowerasing_is_explained_in_plain_words(self):
        st, it = state_with_release()
        w = FakeWorld.from_items([])
        lines = ["Updating and loading repositories:",
                 "Problem: package howdy-3.0.0-1.fc44.x86_64 requires python3.14, but none of the providers can be installed",
                 "  - conflicting requests",
                 "You can try to add to command line:",
                 "  --allowerasing to allow removing of installed packages to resolve problems"]
        rc, evs, done, _ = run_apply(w, [it["id"]], st, lines=lines, rc=1)
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertIn("would have to be removed (dnf says --allowerasing)", done["cause"])
        fail = next(e for e in evs if e["step"] == "release" and e["status"] == "fail")
        self.assertTrue(fail["error"].startswith("Problem: package howdy"))

    def test_a_timeout_is_said(self):
        st, it = state_with_release()
        w = FakeWorld.from_items([])
        rc, evs, done, _ = run_apply(w, [it["id"]], st, lines=["[  10/1834] x"], rc=124, timed_out=True)
        self.assertEqual(done["status"], "fail")
        self.assertIn("timed out after 4 h", done["cause"])

    def test_resolve_only_downloads_nothing(self):
        st, it = state_with_release()
        w = FakeWorld.from_items([])
        rc, evs, done, _ = run_apply(w, [it["id"]], st, resolve_only=True)
        self.assertEqual(FakeStream.calls, [])
        self.assertEqual(next(e for e in evs if e["step"] == "release")["status"], "skip")
        self.assertEqual(done["status"], "ok")

    def test_dry_run_plays_the_release_too(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        st, it = state_with_release()
        A.load_state = lambda: st
        A.akmod_installed = lambda: False
        buf = io.StringIO()
        em = A.Emitter(buf)
        rc = A.run_dry(em, [it["id"]], fast=True)
        em.logfh.close()
        evs = [json.loads(x) for x in buf.getvalue().splitlines()]
        self.assertEqual(rc, 0)
        self.assertEqual([e["status"] for e in evs if e["step"] == "release"][-1], "ok")
        self.assertEqual(evs[-1]["release"]["version"], 45)
        self.assertIn("Restart to install Fedora 45.", evs[-1]["summary"])


class ReleaseReboot(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rivet-offline-"))
        import rivet_apply
        self.A = importlib.reload(rivet_apply)
        release.OFFLINE_DIR = self.dir
        self.A.root_prefix = lambda: ["pkexec"]
        self.calls = []

        def stream(cmd, em, step, *, timeout):
            self.calls.append(list(cmd))
            return (self.rc, self.last)
        self.A.stream_lines = stream
        self.rc, self.last = 0, ""

    def tearDown(self):
        release.OFFLINE_DIR = Path(os.environ.get("RIVET_OFFLINE_DIR", "/usr/lib/sysimage/libdnf5/offline"))

    def run_it(self, argv):
        buf = io.StringIO()
        orig = self.A.Emitter.__init__

        def init(em, stream=sys.stdout):
            orig(em, buf)
        self.A.Emitter.__init__ = init
        try:
            rc = self.A.main(argv)
        finally:
            self.A.Emitter.__init__ = orig
        evs = [json.loads(x) for x in buf.getvalue().splitlines()]
        return rc, evs, evs[-1]

    def test_refuses_without_a_download_and_runs_nothing(self):
        rc, evs, done = self.run_it(["--release-reboot", "45"])
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertIn("Fedora 45 is not downloaded", done["cause"])
        self.assertEqual(self.calls, [])
        self.assertEqual(done["release"], {"version": 45, "downloaded": False, "rebooting": False})

    def test_runs_dnf5_system_upgrade_reboot_as_root(self):
        (self.dir / "transaction.json").write_text('{"releasever": "45"}')
        rc, evs, done = self.run_it(["--release-reboot", "45"])
        self.assertEqual((rc, done["status"]), (0, "ok"))
        self.assertEqual(self.calls, [["pkexec", "dnf5", "system-upgrade", "reboot", "-y"]])
        self.assertEqual(done["release"], {"version": 45, "downloaded": True, "rebooting": True})
        self.assertTrue(any("Asking for permission" in (e.get("msg") or "") for e in evs))

    def test_a_dismissed_prompt_leaves_the_machine_up(self):
        (self.dir / "transaction.json").write_text('{"releasever": "45"}')
        self.rc = 126
        rc, evs, done = self.run_it(["--release-reboot", "45"])
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(done["cause"], "Not restarted: the permission prompt was dismissed, so nothing ran.")
        self.assertEqual(done["release"]["rebooting"], False)

    def test_a_download_for_another_release_does_not_count(self):
        (self.dir / "transaction.json").write_text('{"releasever": "44"}')
        rc, evs, done = self.run_it(["--release-reboot", "45"])
        self.assertEqual(done["status"], "fail")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
