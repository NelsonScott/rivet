#!/usr/bin/env python3
"""rivet-apply against a scripted dnf + rpmdb (backend/fixtures/fake_dnf.py).

    python3 backend/tests/test_apply.py

Covers the 2026-09-21 failure: dnf5daemon-server upgraded inside its own
transaction, systemd restarted it, dnf said "Error", every package was installed.
"""
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.environ["RIVET_STATE_DIR"] = tempfile.mkdtemp(prefix="rivet-test-")   # apply.log goes here
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "fixtures"))

from fake_dnf import FakeWorld  # noqa: E402

# the root-prefixed `akmods --force ...` (the lost-lock retry) must hit the fakes, never the system.
FAKE_BIN = FakeWorld.enable_fake_akmods()
for _tool in ("sudo", "pkexec", "akmods"):
    assert Path(shutil.which(_tool) or "/").parent == FAKE_BIN, f"real {_tool} on PATH"

STATE = json.loads((BACKEND / "fixtures" / "state-selfupdate.json").read_text())
ITEMS = STATE["groups"][0]["items"]
ALL_IDS = [i["id"] for i in ITEMS]
KVER = "7.2.5-200.fc44.x86_64"          # the fixture's target kernel
SIG15 = "WARNING [rpm] %transfiletriggerpostun(systemd-259.9-1.fc44.x86_64) scriptlet failed, signal 15"


def run(world, ids=None, state=STATE):
    import rivet_apply
    A = importlib.reload(rivet_apply)
    world.install(A)
    A.AKMOD_POLL_S = 0
    buf = io.StringIO()
    em = A.Emitter(buf)
    rc = A.run_real(em, ids or ALL_IDS, resolve_only=False, state=state)
    em.logfh.close()
    evs = [json.loads(l) for l in buf.getvalue().splitlines()]
    return rc, evs, evs[-1]


def install_final(evs):
    return [e for e in evs if e["step"] == "install" and e["status"] in ("ok", "fail")][-1]


class SplitTest(unittest.TestCase):
    def test_updater_goes_last_in_its_own_transaction(self):
        w = FakeWorld.from_items(ITEMS)
        rc, evs, done = run(w)
        self.assertEqual(len(w.transactions), 2)
        main, updater = w.transactions
        self.assertEqual(sorted(updater), ["dnf5", "dnf5daemon-server", "libdnf5", "polkit"])
        self.assertIn("systemd", main)
        self.assertIn("kernel", main)
        self.assertFalse(set(main) & set(updater))
        self.assertEqual((rc, done["status"]), (0, "ok"))
        self.assertEqual(done["results"]["dnf"]["installed"], len(ITEMS))

    def test_one_install_step_with_monotonic_progress(self):
        w = FakeWorld.from_items(ITEMS)
        _, evs, _ = run(w)
        inst = [e for e in evs if e["step"] == "install"]
        self.assertEqual(sum(e["status"] == "start" for e in inst), 1)
        self.assertEqual(sum(e["status"] in ("ok", "fail") for e in inst), 1)
        pcts = [e["pct"] for e in inst if e["pct"] is not None]
        self.assertEqual(pcts, sorted(pcts))
        first_updater = next(i for i, e in enumerate(evs) if e.get("phase") == "updater")
        self.assertFalse([e for e in evs[first_updater:] if e["step"] == "download"],
                         "the updater phase must not reopen the download step")

    def test_no_split_without_updater_packages_or_when_alone(self):
        w = FakeWorld.from_items(ITEMS)
        run(w, ["dnf:curl", "dnf:tzdata"])
        self.assertEqual(w.transactions, [["curl", "tzdata"]])
        w = FakeWorld.from_items(ITEMS)
        run(w, ["dnf:dnf5daemon-server", "dnf:libdnf5"])
        self.assertEqual(w.transactions, [["dnf5daemon-server", "libdnf5"]])


class VerifyTest(unittest.TestCase):
    def test_error_but_everything_installed_is_ok_with_warning(self):
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {1}                                   # the updater transaction "fails"
        w.journal = {"error": SIG15, "self_restart": True}
        rc, evs, done = run(w)
        fin = install_final(evs)
        self.assertEqual(fin["status"], "ok")
        self.assertIn("updater restarted itself", fin["warning"])
        self.assertEqual(fin["error"], SIG15)
        self.assertEqual((rc, done["status"]), (0, "warn"))
        self.assertEqual(done["warnings"], [fin["warning"]])
        self.assertIn("signal 15", done["error"])
        self.assertEqual(done["results"]["dnf"]["installed"], len(ITEMS))
        self.assertIs(done["kmod_verified"], True, "kmod check must run despite the dnf error")

    def test_the_real_incident_single_transaction(self):
        """Exactly 2026-09-21: updater pulled into the main transaction, tx 'fails'."""
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {0}
        w.journal = {"error": SIG15, "self_restart": True}
        rc, evs, done = run(w, ["dnf:dnf5daemon-server", "dnf:libdnf5"])
        self.assertEqual((rc, done["status"]), (0, "warn"))
        self.assertIn("Nothing to redo", done["summary"])

    def test_error_without_self_restart_names_the_error(self):
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {0}
        w.script_errors = {0: ["scriptlet failed for tzdata-0:2026b-1.fc44.noarch (type 3, rc=1)"]}
        rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])
        self.assertEqual(done["status"], "warn")
        self.assertIn("scriptlet failed for tzdata", done["warnings"][0])
        self.assertNotIn("restarted itself", done["warnings"][0])

    def test_error_and_missing_packages_is_fail_with_names(self):
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {0}
        w.drop = {"curl", "kernel", "kernel-core"}
        w.journal = {"error": "ERROR [rpm] unpacking of archive failed on file /usr/bin/curl: cpio: write",
                     "self_restart": False}
        rc, evs, done = run(w)
        fin = install_final(evs)
        self.assertEqual((rc, done["status"], fin["status"]), (1, "fail", "fail"))
        self.assertIn(f"{len(ITEMS) - 3 - 4} of {len(ITEMS)} package(s) installed", fin["msg"])
        for name in ("curl", "kernel", "kernel-core"):
            self.assertIn(name, fin["msg"])
            self.assertIn(name, done["summary"])
        self.assertIn("unpacking of archive failed", fin["msg"])
        self.assertIn("unpacking of archive failed", done["summary"])
        self.assertEqual(done["error"], w.journal["error"])
        self.assertEqual(len(w.transactions), 1, "no updater transaction on top of a broken one")
        self.assertTrue(done["cause"])

    def test_more_than_eight_missing_is_truncated(self):
        w = FakeWorld.from_items(ITEMS)
        w.raise_tx = {0: "GDBus.Error: Not authorized"}
        w.drop = {i["name"] for i in ITEMS}
        rc, evs, done = run(w)
        fin = install_final(evs)
        self.assertIn(f"0 of {len(ITEMS)} package(s) installed", fin["msg"])
        self.assertIn("more.", fin["msg"])
        self.assertIn("Not authorized", done["error"])
        self.assertIn("password prompt", done["cause"])
        self.assertIsNone(done["kmod_verified"], "nothing installed -> nothing to build")
        self.assertTrue(any(e["step"] == "akmod" and e["status"] == "skip" for e in evs))

    def test_kmod_verified_when_kernel_landed_but_others_did_not(self):
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {0}
        w.drop = {"curl"}
        rc, evs, done = run(w)
        self.assertEqual(done["status"], "fail")
        self.assertIs(done["kmod_verified"], True)

    def test_leftover_duplicate_is_a_failure(self):
        w = FakeWorld.from_items(ITEMS)
        w.fail_tx = {0}
        rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])     # lands cleanly...
        w2 = FakeWorld.from_items(ITEMS)
        w2.fail_tx = {0}
        import fake_dnf
        orig = fake_dnf.FakeDnf.do_transaction

        def dup(self, **kw):
            ok = orig(self, **kw)
            self.w.installed["curl"].add(("0:8.18.0-9.fc44", "x86_64"))   # old one survives
            return ok
        fake_dnf.FakeDnf.do_transaction = dup
        try:
            rc, evs, done = run(w2, ["dnf:curl", "dnf:tzdata"])
        finally:
            fake_dnf.FakeDnf.do_transaction = orig
        self.assertEqual(done["status"], "fail")
        self.assertIn("left behind", install_final(evs)["msg"])

    def test_epoch_zero_and_newer_than_planned(self):
        import rivet_apply as A
        self.assertEqual(A.norm_evr("7.2.5-200.fc44"), A.norm_evr("0:7.2.5-200.fc44"))
        snap = {"kernel-core": {("0:7.1.10-200.fc44", "x86_64"), ("0:7.2.5-200.fc44", "x86_64")},
                "Sunshine": {("0:2026.921.13701-1.fc44", "x86_64")},
                "akmod-nvidia": {("3:615.71.09-3.fc44", "x86_64")}}
        items = [{"name": "kernel-core", "to": "7.2.5-200.fc44"},
                 {"name": "Sunshine", "to": "2026.919.234445-1.fc44"},
                 {"name": "akmod-nvidia", "to": "3:615.71.09-3.fc44"},
                 {"name": "gone", "to": "1-1"}]
        v = A.verify_installed(items, [], snap)
        self.assertEqual(v["missing"], ["gone"])
        removed = [{"name": "old", "evr": "0:1-1", "arch": "noarch", "nevra": "old-0:1-1.noarch",
                    "action": "remove"}]
        self.assertEqual(A.verify_installed([], removed, {"old": {("0:1-1", "noarch")}})["leftover"],
                         ["old-0:1-1.noarch"])
        self.assertEqual(A.verify_installed([], removed, {})["leftover"], [])


class StaleMetadataTest(unittest.TestCase):
    """2026-09-22: the daemon's metadata cache was hours behind the collector's. It did
    not know kernel-7.2.6, resolved the plan to NOTHING, apply called that 12 of 12
    installed and then waited 600 s for an NVIDIA module for a kernel that never landed."""

    KERNEL = ["dnf:kernel", "dnf:kernel-core"]

    def test_never_offered_is_a_named_failure_with_no_akmod_wait(self):
        w = FakeWorld.from_items(ITEMS)
        w.stale, w.stale_expires = {"kernel", "kernel-core"}, 0     # no refresh helps
        rc, evs, done = run(w, self.KERNEL)
        fin = install_final(evs)
        self.assertEqual((rc, done["status"], fin["status"]), (1, "fail", "fail"))
        self.assertEqual(w.transactions, [], "nothing to install -> no transaction")
        self.assertEqual(w.expires, 2, "expired once up front, once more for the retry")
        self.assertIn("0 of 2 package(s) installed", fin["msg"])
        self.assertIn("kernel-core", fin["msg"])
        self.assertIn("does not offer kernel", done["cause"])
        self.assertIn("kernel-core-7.2.5-200.fc44", done["summary"])   # the build it lacks
        self.assertNotIn("NVIDIA kernel module", done["summary"])
        self.assertIsNone(done["kmod_verified"])
        self.assertFalse([e for e in evs if e["step"] == "akmod" and e["status"] != "skip"])
        self.assertEqual(done["results"]["dnf"]["missing"], ["kernel", "kernel-core"])

    def test_refresh_and_retry_then_it_installs(self):
        w = FakeWorld.from_items(ITEMS)
        w.stale, w.stale_expires = {"kernel", "kernel-core"}, 2      # visible after 2nd expire
        rc, evs, done = run(w, self.KERNEL)
        self.assertEqual((rc, done["status"]), (0, "ok"))
        self.assertEqual(len(w.transactions), 1)
        self.assertEqual(w.expires, 2)
        retry = [e for e in evs if "Refreshing and trying again" in (e["msg"] or "")]
        self.assertEqual(len(retry), 1)
        self.assertIn("kernel-core-7.2.5-200.fc44", retry[0]["msg"])
        self.assertEqual(done["results"]["dnf"]["installed"], 2)
        self.assertIs(done["kmod_verified"], True)

    def test_fresh_cache_needs_no_retry(self):
        w = FakeWorld.from_items(ITEMS)
        w.stale = {"kernel"}                       # stale_expires=1: the up-front expire fixes it
        rc, evs, done = run(w, self.KERNEL)
        self.assertEqual((rc, done["status"], w.expires), (0, "ok", 1))
        self.assertFalse([e for e in evs if "trying again" in (e["msg"] or "")])
        self.assertTrue([e for e in evs if "Refreshing package metadata" in (e["msg"] or "")])

    def test_already_at_target_is_ok_not_fail(self):
        w = FakeWorld.from_items(ITEMS)
        for name in ("curl", "tzdata"):
            w.installed[name] = {w.available[name]}
        rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])
        fin = install_final(evs)
        self.assertEqual((rc, done["status"], fin["status"]), (0, "ok", "ok"))
        self.assertIn("already", fin["msg"])
        self.assertEqual(w.transactions, [])
        self.assertEqual(done["results"]["dnf"], {"planned": 2, "installed": 2, "missing": [],
                                                  "leftover": [], "tx_total": 0, "tx_installed": 0})

    def test_success_is_verified_against_the_rpmdb(self):
        w = FakeWorld.from_items(ITEMS)
        import fake_dnf
        orig = fake_dnf.FakeDnf.do_transaction

        def liar(self, **kw):       # dnf says ok, but curl silently did not land
            ok = orig(self, **kw)
            self.w.installed["curl"] = {("0:8.18.0-9.fc44", "x86_64")}
            return ok
        fake_dnf.FakeDnf.do_transaction = liar
        try:
            rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])
        finally:
            fake_dnf.FakeDnf.do_transaction = orig
        fin = install_final(evs)
        self.assertEqual((rc, done["status"], fin["status"]), (1, "fail", "fail"))
        self.assertIn("1 of 2 package(s) installed", fin["msg"])
        self.assertIn("curl", fin["msg"])
        self.assertIn("said the transaction succeeded", done["error"])
        self.assertEqual(done["results"]["dnf"]["missing"], ["curl"])

    def test_partial_stale_installs_the_rest_and_says_so(self):
        w = FakeWorld.from_items(ITEMS)
        w.stale, w.stale_expires = {"tzdata"}, 0
        rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual(w.transactions, [], "stale plan never starts a transaction")
        self.assertIn("tzdata", done["summary"])
        self.assertIn("does not offer", done["cause"])

    def test_expire_denied_still_detects_staleness(self):
        w = FakeWorld.from_items(ITEMS)
        w.expire_ok = False
        w.stale, w.stale_expires = {"kernel"}, 0
        rc, evs, done = run(w, self.KERNEL)
        self.assertEqual(done["status"], "fail")
        self.assertIn("does not offer kernel", done["cause"])
        self.assertIsNone(done["kmod_verified"])


GL = "runtime/org.freedesktop.Platform.GL.nvidia-615-71-09/x86_64/1.4"
GL32 = "runtime/org.freedesktop.Platform.GL32.nvidia-615-71-09/x86_64/1.4"
GAPS = [{"scope": "system", "kind": "GL", "ref": GL, "have": ["610.57.04"]},
        {"scope": "system", "kind": "GL32", "ref": GL32, "have": ["610.57.04"]}]


class NvidiaFlatpakRuntimeTest(unittest.TestCase):
    """2026-09-21: driver 610 -> 615 by dnf, reboot, Flatpak Steam had only the 610 runtime."""

    def test_driver_upgrade_installs_matching_runtime_same_run(self):
        w = FakeWorld.from_items(ITEMS)
        w.flatpak_missing = GAPS
        rc, evs, done = run(w, ["dnf:akmod-nvidia"])
        self.assertEqual(w.flatpak_cmds, [
            ["flatpak", "install", "-y", "--noninteractive", "--system", "flathub", GL],
            ["flatpak", "install", "-y", "--noninteractive", "--system", "flathub", GL32]])
        fp = [e for e in evs if e["step"] == "flatpak"]
        self.assertIn("Installing matching NVIDIA runtime for Flatpak apps (Steam)", fp[0]["msg"])
        self.assertIn("615.71.09", fp[0]["msg"])
        self.assertEqual((rc, done["status"]), (0, "ok"))
        self.assertEqual(done["results"]["flatpak"]["ok"], 2)
        # the dnf step must be over before Flatpak starts
        steps = [e["step"] for e in evs]
        self.assertGreater(steps.index("flatpak"), max(i for i, x in enumerate(steps) if x == "install"))

    def test_not_on_flathub_yet_is_a_warning_not_a_failure(self):
        w = FakeWorld.from_items(ITEMS)
        w.flatpak_missing, w.flathub_has = GAPS, False
        rc, evs, done = run(w, ["dnf:akmod-nvidia"])
        self.assertEqual(w.flatpak_cmds, [])
        self.assertEqual((rc, done["status"]), (0, "warn"))
        self.assertIn("Flathub does not have the NVIDIA 615.71.09 runtime yet", done["warnings"][0])
        skip = [e for e in evs if e["step"] == "flatpak" and e["status"] == "skip"]
        self.assertEqual(len(skip), 2)
        self.assertTrue(all(e.get("warning") for e in skip))

    def test_install_failure_warns_and_names_it(self):
        w = FakeWorld.from_items(ITEMS)
        w.flatpak_missing, w.flatpak_rc = GAPS[:1], 1
        rc, evs, done = run(w, ["dnf:akmod-nvidia"])
        self.assertEqual((rc, done["status"]), (0, "warn"))
        self.assertEqual(done["results"]["flatpak"]["failed_names"],
                         ["NVIDIA driver 615.71.09 for Flatpak apps"])

    def test_no_driver_change_no_flatpak_step(self):
        w = FakeWorld.from_items(ITEMS)
        w.flatpak_missing = GAPS          # even with a gap: not this run's business
        rc, evs, done = run(w, ["dnf:curl", "dnf:tzdata"])
        self.assertEqual(w.flatpak_cmds, [])
        self.assertNotIn("flatpak", [e["step"] for e in evs])
        self.assertNotIn("flatpak", done["results"])

    def test_driver_that_did_not_land_installs_nothing(self):
        w = FakeWorld.from_items(ITEMS)
        w.flatpak_missing = GAPS
        w.fail_tx, w.drop = {0, 1}, {"akmod-nvidia"}
        rc, evs, done = run(w, ["dnf:akmod-nvidia"])
        self.assertEqual(done["status"], "fail")
        self.assertEqual(w.flatpak_cmds, [])

    def test_collector_install_item_uses_flatpak_install(self):
        """The self-heal path: rivet-collect listed the missing runtime as op=install."""
        state = {"groups": [{"id": "flatpak", "items": [
            {"id": "flatpak:" + GL, "source": "flatpak", "name": "org.freedesktop.Platform.GL.nvidia-615-71-09",
             "display_name": "NVIDIA driver 615.71.09 for Flatpak apps", "ref": GL, "op": "install",
             "scope": "system", "origin": "flathub", "blocked_by": None, "requires_reboot": False},
            {"id": "flatpak:app/md.obsidian.Obsidian/x86_64/stable", "source": "flatpak",
             "name": "md.obsidian.Obsidian", "display_name": "Obsidian", "scope": "system",
             "ref": "app/md.obsidian.Obsidian/x86_64/stable", "blocked_by": None}]}]}
        w = FakeWorld()
        rc, evs, done = run(w, [i["id"] for i in state["groups"][0]["items"]], state=state)
        self.assertEqual(w.flatpak_cmds, [
            ["flatpak", "install", "-y", "--noninteractive", "--system", "flathub", GL],
            ["flatpak", "update", "-y", "--noninteractive", "--system",
             "app/md.obsidian.Obsidian/x86_64/stable"]])
        self.assertEqual((rc, done["status"], done["results"]["flatpak"]["ok"]), (0, "ok", 2))

    def test_version_helpers(self):
        import flatpak_nvidia as N
        self.assertEqual(N.version_of("3:615.71.09-3.fc44"), "615.71.09")
        self.assertIsNone(N.version_of("(not installed)"))
        self.assertEqual(N.make_ref("GL32", "615.71.09"), GL32)
        self.assertEqual(N.parse_ref(GL.split("/", 1)[1])["tag"], "615-71-09")
        self.assertIsNone(N.parse_ref("runtime/org.freedesktop.Platform.VAAPI.nvidia/x86_64/25.08"))


class AkmodLockTest(unittest.TestCase):
    """2026-10-04 01:32: kernel 7.2.8 + the dnf5daemon family in one plan. akmods (async,
    root) finished building kmod-nvidia-7.2.8 exactly while the deferred updater transaction
    held the rpm lock: "can't create transaction lock", "Could not install newly built RPMs",
    a .failed.log next to the built RPM, and rivet-apply called it a build failure."""

    RETRY = f"--force --kernels {KVER}"

    def setUp(self):
        for var in ("FAKE_AKMODS_MODE", "FAKE_SUDO_MODE", "FAKE_PKEXEC_MODE"):
            os.environ.pop(var, None)

    tearDown = setUp

    @staticmethod
    def first(evs, step, status=None, text=None):
        for i, e in enumerate(evs):
            if e["step"] == step and (status is None or e["status"] == status) \
                    and (text is None or text in (e["msg"] or "")):
                return i
        raise AssertionError(f"no {step}/{status} event with {text!r}")

    def test_akmod_wait_sits_between_the_main_and_updater_transactions(self):
        w = FakeWorld.from_items(ITEMS)
        w.akmod_build_polls = 3                  # akmods "builds" in the background
        rc, evs, done = run(w)
        self.assertEqual(len(w.transactions), 2)
        self.assertEqual(w.kmod_at_tx, [False, True],
                         "the updater transaction must start only once the kmod is installed")
        why = self.first(evs, "install", "progress", "Letting akmods finish")
        start = self.first(evs, "akmod", "start")
        ok = self.first(evs, "akmod", "ok")
        updater = self.first(evs, "install", "progress", "Now the updater's own")
        self.assertLess(why, start)
        self.assertLess(start, ok)
        self.assertLess(ok, updater, "akmod ok must come before the updater transaction")
        self.assertEqual(sum(e["step"] == "akmod" and e["status"] == "start" for e in evs), 1,
                         "the caller must not wait a second time")
        self.assertEqual(w.calls("akmods"), [], "no retry when the build simply finishes")
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))
        # the one-install-step contract still holds
        inst = [e for e in evs if e["step"] == "install"]
        self.assertEqual(sum(e["status"] in ("ok", "fail") for e in inst), 1)
        pcts = [e["pct"] for e in inst if e["pct"] is not None]
        self.assertEqual(pcts, sorted(pcts))

    def test_no_wait_between_phases_without_a_kernel(self):
        w = FakeWorld.from_items(ITEMS)
        w.akmod_build_polls = 3
        rc, evs, done = run(w, ["dnf:curl", "dnf:dnf5daemon-server"])
        self.assertEqual(len(w.transactions), 2)
        self.assertFalse([e for e in evs if e["step"] == "akmod"])
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", None))

    def test_lost_rpm_lock_is_repaired_with_one_forced_akmods_run(self):
        w = FakeWorld.from_items(ITEMS)
        log = w.seed_akmod_failure(KVER, lock_lost=True)
        rc, evs, done = run(w)
        self.assertEqual(w.calls("akmods"), [self.RETRY], "exactly one akmods --force")
        self.assertEqual(w.calls("sudo"), ["-n true", f"-n akmods {self.RETRY}"], "sudo -n probed, then used")
        self.assertEqual(w.calls("pkexec"), [])
        self.assertTrue(w.kmod_marker(KVER).exists(), "the fake akmods installed the kmod")
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))
        why = evs[self.first(evs, "akmod", "progress", "could not install it")]
        self.assertIn("rpm lock", why["msg"])
        self.assertIn(f"akmods --force --kernels {KVER}", why["msg"])
        self.assertEqual(why["log"], str(log))
        ok = evs[self.first(evs, "akmod", "ok")]
        self.assertIn("needed a retry", ok["msg"])
        self.assertIs(ok.get("retried"), True)
        self.assertEqual(sum(e["step"] == "akmod" and e["status"] == "fail" for e in evs), 0)
        self.assertEqual(w.kmod_at_tx, [False, True])
        self.assertNotIn("do not reboot", done["summary"])

    def test_lost_rpm_lock_without_an_updater_phase_heals_in_the_final_wait(self):
        w = FakeWorld.from_items(ITEMS)
        w.seed_akmod_failure(KVER, lock_lost=True)
        rc, evs, done = run(w, ["dnf:kernel", "dnf:kernel-core", "dnf:akmod-nvidia"])
        self.assertEqual(len(w.transactions), 1)
        self.assertEqual(w.calls("akmods"), [self.RETRY])
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))

    def test_failed_retry_is_a_failure_that_names_the_retry(self):
        os.environ["FAKE_AKMODS_MODE"] = "fail"
        w = FakeWorld.from_items(ITEMS)
        w.seed_akmod_failure(KVER, lock_lost=True)
        rc, evs, done = run(w)
        self.assertEqual(w.calls("akmods"), [self.RETRY], "retried once, never twice")
        self.assertEqual(len(w.transactions), 2, "a kmod failure does not stop the updater tx")
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (1, "fail", False))
        fail = evs[self.first(evs, "akmod", "fail")]
        self.assertIn(f"akmods reported a build failure for {KVER}", fail["msg"])
        self.assertIn(f"akmods --force --kernels {KVER}", fail["msg"])
        self.assertIn("exited 2", fail["msg"])
        self.assertIn("Building rpms failed", fail["msg"])
        self.assertIs(fail.get("retried"), True)
        self.assertEqual(sum(e["step"] == "akmod" and e["status"] == "start" for e in evs), 1)
        self.assertIn("do not reboot", done["summary"])

    def test_no_passwordless_sudo_means_pkexec_asks_and_the_retry_still_runs(self):
        # 2026-10-07: no NOPASSWD rule any more. `sudo -n true` fails, so the retry goes
        # through pkexec (the polkit dialog) and succeeds.
        os.environ["FAKE_SUDO_MODE"] = "refuse"
        w = FakeWorld.from_items(ITEMS)
        w.seed_akmod_failure(KVER, lock_lost=True)
        rc, evs, done = run(w)
        self.assertEqual(w.calls("sudo"), ["-n true"], "probed once, never used for the command")
        self.assertEqual(w.calls("pkexec"), [f"akmods {self.RETRY}"])
        self.assertEqual(w.calls("akmods"), [self.RETRY])
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))
        asked = evs[self.first(evs, "akmod", "progress", "permission")]
        self.assertIn("face or password", asked["msg"])

    def test_dismissed_permission_prompt_is_a_failure_that_says_so(self):
        os.environ["FAKE_SUDO_MODE"] = "refuse"
        os.environ["FAKE_PKEXEC_MODE"] = "dismiss"
        try:
            w = FakeWorld.from_items(ITEMS)
            w.seed_akmod_failure(KVER, lock_lost=True)
            rc, evs, done = run(w)
        finally:
            del os.environ["FAKE_PKEXEC_MODE"]
        self.assertEqual(w.calls("akmods"), [])
        self.assertEqual(len(w.calls("pkexec")), 1)
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (1, "fail", False))
        fail = evs[self.first(evs, "akmod", "fail")]
        self.assertIn("permission prompt was dismissed", fail["msg"])

    def test_denied_permission_is_a_failure_that_says_so(self):
        os.environ["FAKE_SUDO_MODE"] = "refuse"
        os.environ["FAKE_PKEXEC_MODE"] = "deny"
        try:
            w = FakeWorld.from_items(ITEMS)
            w.seed_akmod_failure(KVER, lock_lost=True)
            rc, evs, done = run(w)
        finally:
            del os.environ["FAKE_PKEXEC_MODE"]
        self.assertEqual(w.calls("akmods"), [])
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (1, "fail", False))
        fail = evs[self.first(evs, "akmod", "fail")]
        self.assertIn("permission was refused", fail["msg"])
        self.assertIn("Not authorized", fail["msg"])

    def test_real_build_failure_fails_at_once_with_no_retry(self):
        w = FakeWorld.from_items(ITEMS)
        log = w.seed_akmod_failure(KVER, lock_lost=False)        # no RPM, a real error
        rc, evs, done = run(w)
        self.assertEqual(w.calls("akmods"), [])
        self.assertEqual(w.calls("sudo"), [])
        self.assertEqual(w.calls("pkexec"), [])
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (1, "fail", False))
        fail = evs[self.first(evs, "akmod", "fail")]
        self.assertEqual(fail["msg"], f"akmods reported a build failure for {KVER}.")
        self.assertEqual(fail["log"], str(log))
        self.assertNotIn("retried", fail)
        self.assertEqual(sum(e["step"] == "akmod" and e["status"] == "start" for e in evs), 1)
        self.assertIn("do not reboot", done["summary"])

    def test_lock_lost_text_without_a_built_rpm_is_still_a_failure(self):
        w = FakeWorld.from_items(ITEMS)
        w.seed_akmod_failure(KVER, lock_lost=True)
        for rpm in w.akmod_cache.glob("kmod-nvidia-*.rpm"):
            rpm.unlink()
        rc, evs, done = run(w)
        self.assertEqual(w.calls("akmods"), [])
        self.assertEqual(done["status"], "fail")


class DepmodRaceTest(unittest.TestCase):
    """2026-10-12: rpm listed kmod-nvidia-7.2.9 two seconds before its %post ran depmod;
    verify's `modinfo -k <kver> nvidia` (resolved through modules.dep) failed, the run was
    reported as 'NVIDIA module NOT verified', and the doctor then found nothing wrong."""

    def _world(self, misses):
        """modinfo-by-name fails for the first `misses` calls after the kmod is in the rpmdb."""
        w = FakeWorld.from_items(ITEMS)
        w.akmod_build_polls = 2
        calls = {"n": 0}

        def modinfo_ok(kver):
            if not w.kmod_marker(kver).exists():
                return False
            calls["n"] += 1
            return calls["n"] > misses
        return w, modinfo_ok, calls

    def _run(self, w, modinfo_ok, settle_s=90):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        w.install(A)
        A.modinfo_ok = modinfo_ok
        A.AKMOD_POLL_S = 0
        A.KMOD_SETTLE_POLL_S = 0
        A.KMOD_SETTLE_S = settle_s
        buf = io.StringIO()
        em = A.Emitter(buf)
        rc = A.run_real(em, ALL_IDS, resolve_only=False, state=STATE)
        em.logfh.close()
        evs = [json.loads(l) for l in buf.getvalue().splitlines()]
        return rc, evs, evs[-1]

    def test_verify_waits_for_depmod_instead_of_failing(self):
        w, modinfo_ok, calls = self._world(misses=3)
        rc, evs, done = self._run(w, modinfo_ok)
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))
        self.assertTrue(any(e["step"] == "akmod" and "waiting for depmod" in (e["msg"] or "") for e in evs))
        ok_i = next(i for i, e in enumerate(evs) if e["step"] == "akmod" and e["status"] == "ok")
        wait_i = next(i for i, e in enumerate(evs) if "waiting for depmod" in (e["msg"] or ""))
        self.assertLess(wait_i, ok_i, "akmod ok comes only after modinfo resolves")
        self.assertFalse([e for e in evs if e["step"] == "verify" and e["status"] == "fail"])
        self.assertNotIn("do not reboot", done["summary"])

    def test_modinfo_that_never_resolves_fails_and_names_depmod(self):
        w, modinfo_ok, _ = self._world(misses=10**9)
        import rivet_apply
        A = importlib.reload(rivet_apply)
        w.install(A)
        ko = w.akmod_cache / "nvidia.ko.xz"
        ko.write_bytes(b"ko")
        A.kmod_module_file = lambda kver: ko
        A.modinfo_path_ok = lambda path: True
        A.modinfo_ok = modinfo_ok
        A.AKMOD_POLL_S = 0
        A.KMOD_SETTLE_POLL_S = 0
        A.KMOD_SETTLE_S = 0.01
        buf = io.StringIO()
        em = A.Emitter(buf)
        rc = A.run_real(em, ALL_IDS, resolve_only=False, state=STATE)
        em.logfh.close()
        evs = [json.loads(l) for l in buf.getvalue().splitlines()]
        done = evs[-1]
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (1, "fail", False))
        fail = [e for e in evs if e["step"] == "verify" and e["status"] == "fail"][0]
        self.assertIn("depmod has not run", fail["cause"])
        self.assertIn(f"sudo depmod -a {KVER}", fail["cause"])
        self.assertIn("depmod has not run", done["cause"])

    def test_missing_module_file_is_named(self):
        w, modinfo_ok, _ = self._world(misses=10**9)
        import rivet_apply
        A = importlib.reload(rivet_apply)
        w.install(A)
        A.kmod_module_file = lambda kver: None
        A.modinfo_ok = modinfo_ok
        A.AKMOD_POLL_S = 0
        A.KMOD_SETTLE_POLL_S = 0
        A.KMOD_SETTLE_S = 0.01
        buf = io.StringIO()
        em = A.Emitter(buf)
        A.run_real(em, ALL_IDS, resolve_only=False, state=STATE)
        em.logfh.close()
        done = json.loads(buf.getvalue().splitlines()[-1])
        self.assertEqual(done["kmod_verified"], False)
        self.assertIn("not on disk", done["cause"])

    def test_verify_kmod_cli_reports_the_newest_kernel(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        w = FakeWorld.from_items(ITEMS)
        w.install(A)
        w.kmod_marker("7.2.5-200.fc44.x86_64").touch()
        buf = io.StringIO()
        em = A.Emitter(buf)
        rc = A.run_verify_kmod(em, None)
        em.logfh.close()
        evs = [json.loads(l) for l in buf.getvalue().splitlines()]
        done = evs[-1]
        self.assertEqual((rc, done["status"], done["kmod_verified"]), (0, "ok", True))
        self.assertEqual(done["kver"], "7.2.5-200.fc44.x86_64")
        self.assertTrue(done["reboot_required"], "the fixture kernel is not the running one")
        self.assertEqual([e["step"] for e in evs][:1], ["verify"])
        # and the flag parses with and without a kernel
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["--verify-kmod"]), 0)
            self.assertEqual(A.main(["--verify-kmod", "7.2.5-200.fc44.x86_64"]), 0)

    def test_verify_kmod_cli_without_akmod_is_a_no_op(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        A.akmod_installed = lambda: False
        buf = io.StringIO()
        em = A.Emitter(buf)
        self.assertEqual(A.run_verify_kmod(em, None), 0)
        em.logfh.close()
        done = json.loads(buf.getvalue().splitlines()[-1])
        self.assertEqual((done["status"], done["kmod_verified"]), ("ok", None))


class StdoutGoneTest(unittest.TestCase):
    """2026-10-04: the app window was closed mid-apply; stdout became a broken pipe, emit()
    raised, and the akmod wait / verify / flatpak / fwupd steps died with a traceback and no
    `done` in apply.log. Python ignores SIGPIPE, so the write raises; it must be swallowed."""

    def test_broken_pipe_does_not_stop_the_run(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        w = FakeWorld.from_items(ITEMS)
        w.install(A)
        A.AKMOD_POLL_S = 0
        import common   # where Emitter really logs (another test module may have changed the env since)
        log_path = common.LOG_DIR / "apply.log"
        before = len(log_path.read_text().splitlines()) if log_path.exists() else 0
        r, wfd = os.pipe()
        os.close(r)                                   # nobody will ever read: EPIPE on write
        stream = os.fdopen(wfd, "w")
        em = A.Emitter(stream)
        rc = A.run_real(em, ALL_IDS, resolve_only=False, state=STATE)
        em.logfh.close()
        stream.close()                                # fd was pointed at /dev/null: no error here
        self.assertEqual(rc, 0)
        self.assertTrue(em.stdout_gone)
        evs = [json.loads(l) for l in log_path.read_text().splitlines()[before:]]
        self.assertEqual((evs[-1]["step"], evs[-1]["status"]), ("done", "ok"))
        self.assertEqual(evs[-1]["results"]["dnf"]["installed"], len(ITEMS))
        self.assertTrue(any(e["step"] == "log" and "stdout closed" in e["msg"] for e in evs))
        self.assertIn("install", {e["step"] for e in evs})
        self.assertIn("akmod", {e["step"] for e in evs})

    def test_stringio_stream_is_untouched(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        buf = io.StringIO()
        em = A.Emitter(buf)
        em.emit("download", "start", 0, "x", None)
        em.logfh.close()
        self.assertFalse(em.stdout_gone)
        self.assertEqual(json.loads(buf.getvalue())["step"], "download")


class StreamTimeoutTest(unittest.TestCase):
    """A child that goes silent (flatpak waiting on polkit) must be killed at the deadline,
    not when its next line arrives; and reaped, never left as a zombie."""

    def test_silent_child_is_killed_at_the_deadline(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        buf = io.StringIO()
        em = A.Emitter(buf)
        t0 = time.monotonic()
        rc = A.stream_cmd(["sh", "-c", "echo 'Downloading 10%'; exec sleep 30"], em, "flatpak", timeout=1)
        em.logfh.close()
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - t0, 6)
        evs = [json.loads(l) for l in buf.getvalue().splitlines()]
        self.assertTrue(any(e["status"] == "progress" and e["pct"] == 10 for e in evs))
        self.assertTrue(any(e["status"] == "fail" and "timed out" in e["msg"] for e in evs))

    def test_normal_exit_code_is_returned(self):
        import rivet_apply
        A = importlib.reload(rivet_apply)
        em = A.Emitter(io.StringIO())
        self.assertEqual(A.stream_cmd(["sh", "-c", "echo a; echo b >&2; exit 3"], em, "fwupd", timeout=5), 3)
        em.logfh.close()


class LogRotationTest(unittest.TestCase):
    def test_apply_log_rotates_at_2mb(self):
        import common
        import rivet_apply
        A = importlib.reload(rivet_apply)
        log_path = common.LOG_DIR / "apply.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("x" * (common.LOG_MAX_BYTES + 10))
        em = A.Emitter(io.StringIO())
        em.emit("download", "start", 0, "after rotation", None)
        em.logfh.close()
        self.assertTrue((common.LOG_DIR / "apply.log.1").exists())
        self.assertEqual((common.LOG_DIR / "apply.log.1").stat().st_size, common.LOG_MAX_BYTES + 10)
        self.assertLess(log_path.stat().st_size, 1000)
        self.assertIn("after rotation", log_path.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
