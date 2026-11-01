#!/usr/bin/env python3
"""Held-back Flatpak refs: registry, collector group, try / verify / keep / roll back.

    python3 backend/tests/test_holds.py

Everything runs the REAL CLIs (rivet_collect.py, rivet_apply.py) as subprocesses against a
fake `flatpak`, `pgrep`, `journalctl`, `sudo`, `pkexec` on PATH and a scratch state dir. The
real system is never asked and nothing is installed, masked or launched. The fake flatpak keeps
the two behaviours that matter: `update` skips masked refs, and `update --commit=` is refused
without root (which is why the rollback goes through `sudo -n` / pkexec).
"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

sys.path.insert(0, str(BACKEND / "fixtures"))
from fake_held import C29, C30, GOOD, MASK, OBSIDIAN, REF, World, hold_entry  # noqa: E402


def held_items(state):
    return next(g for g in state["groups"] if g["id"] == "held")["items"]


class RegistryTest(unittest.TestCase):
    def test_version_compare(self):
        import holds
        self.assertEqual(holds.vercmp("3.16.9", "3.16.28"), -1)
        self.assertEqual(holds.vercmp("3.16.28", "3.16.30"), -1)
        self.assertEqual(holds.vercmp("3.16.30", "3.16.29"), 1)
        self.assertEqual(holds.vercmp("3.16.29", "3.16.29"), 0)
        self.assertEqual(holds.max_version(["3.16.9", "3.16.29", "3.16.3"]), "3.16.29")

    def test_classification(self):
        import holds
        h = hold_entry()
        self.assertEqual(holds.classify(h, "3.16.28", "3.16.29"), "known_bad")
        self.assertEqual(holds.classify(h, "3.16.28", "3.16.30"), "candidate")
        self.assertEqual(holds.classify(h, "3.16.28", "3.17.0"), "candidate")
        h2 = hold_entry(bad_versions=["3.16.29", "3.16.31"])
        self.assertEqual(holds.classify(h2, "3.16.28", "3.16.30"), "known_bad")   # <= max bad
        self.assertEqual(holds.classify(h2, "3.16.28", "3.16.32"), "candidate")
        self.assertEqual(holds.classify(hold_entry(bad_versions=["3.16.100"]), "3.16.28", "3.16.30"),
                         "known_bad")                                             # numeric, not text
        self.assertEqual(holds.classify(None, "3.16.28", "3.16.30"), "pinned")
        self.assertEqual(holds.classify(h, "3.16.28", None), "pinned")

    def test_a_mask_alone_never_writes_a_registry(self):
        # 2026-11-01: no built-in seed; a mask with no note shows as "pinned" and writes nothing
        w = World(registry=False, remote_version="3.16.29", remote_commit=C29)
        (it,) = held_items(w.collect())
        self.assertEqual(it["hold_state"], "pinned")
        self.assertFalse((w.state_dir / "holds.json").exists())

    def test_no_mask_means_no_seed_and_existing_registry_is_left_alone(self):
        w = World(registry=False, masks=())
        w.collect()
        self.assertFalse((w.state_dir / "holds.json").exists())
        w2 = World()
        before = w2.holds()
        w2.collect()
        self.assertEqual(w2.holds(), before)

    def test_mask_without_registry_entry_shows_as_pinned(self):
        w = World(registry=False, masks=("md.obsidian.Obsidian",))
        (it,) = held_items(w.collect())
        self.assertEqual((it["ref"], it["hold_state"]), (OBSIDIAN, "pinned"))
        self.assertEqual(it["hold_reason"], "Pinned with flatpak mask (no note)")
        self.assertIsNone(it["held_since"])


class CollectorTest(unittest.TestCase):
    def test_held_items_are_shown_but_never_counted_selected_or_planned(self):
        import rivet_apply
        w = World()
        state = w.collect()
        (it,) = held_items(state)
        self.assertEqual((it["id"], it["hold_state"], it["held"]), (f"flatpak:{REF}", "candidate", True))
        self.assertEqual((it["from"], it["to"], it["display_name"]), ("3.16.28", "3.16.30", "gamescope"))
        self.assertFalse(it["selected_default"])
        self.assertGreater(it["size_bytes"], 0)
        self.assertNotEqual(it["risk"], "red")                 # no red dot in the top bar
        fp = next(g for g in state["groups"] if g["id"] == "flatpak")["items"]
        self.assertEqual([i["ref"] for i in fp], [OBSIDIAN])
        self.assertEqual(state["summary"]["total"], 1)
        self.assertEqual(state["summary"]["total_bytes"], fp[0]["size_bytes"])
        self.assertEqual((state["flags"]["held_count"], state["flags"]["held_candidates"]), (1, 1))
        last = json.loads((w.state_dir / "last-pending.json").read_text())
        self.assertEqual(last["ids"], [f"flatpak:{OBSIDIAN}"])  # the notification set
        found, missing = rivet_apply.resolve_plan([f"flatpak:{REF}", f"flatpak:{OBSIDIAN}"], state)
        self.assertEqual(([i["ref"] for i in found], missing), ([OBSIDIAN], [f"flatpak:{REF}"]))

    def test_known_bad_build_is_flagged_not_offered(self):
        w = World(remote_version="3.16.29", remote_commit=C29)
        state = w.collect()
        (it,) = held_items(state)
        self.assertEqual(it["hold_state"], "known_bad")
        self.assertEqual((state["flags"]["held_count"], state["flags"]["held_candidates"]), (1, 0))
        self.assertIn("staying on 3.16.28", it["verdict"])

    def test_candidate_notification_text(self):
        import holds
        title, body = holds.candidate_notification(
            {"display_name": "gamescope", "to": "3.16.30", "from": "3.16.28", "good_version": "3.16.28"})
        self.assertEqual(body, "gamescope 3.16.30 is out; the pinned 3.16.28 still works. "
                               "Try it from Rivet when you have a minute.")

    def test_stale_trial_is_surfaced_never_rolled_back_silently(self):
        w = World()
        w.collect()
        self.assertEqual(w.apply("--try-held", REF)[0], 0)
        doc = w.holds()
        old = (dt.datetime.now().astimezone() - dt.timedelta(hours=30)).replace(microsecond=0)
        doc["holds"][REF]["pending_trial"]["started_at"] = old.isoformat()
        w.save_holds(doc)
        (it,) = held_items(w.collect())                       # unmasked now: only the registry knows
        self.assertEqual(it["hold_state"], "trial")
        self.assertTrue(it["trial"]["stale"])
        self.assertEqual((it["trial"]["from_version"], it["trial"]["to_version"]), ("3.16.28", "3.16.30"))
        self.assertIn("never confirmed", it["verdict"])
        self.assertEqual(w.installed()["version"], "3.16.30")  # nothing was rolled back
        self.assertIn("pending_trial", w.holds()["holds"][REF])


class TrialTest(unittest.TestCase):
    def start(self, **kw):
        w = World(**kw)
        w.collect()
        return w

    def test_try_then_game_stays_up_keeps_it_and_removes_the_pin(self):
        w = self.start()
        rc, evs, done = w.apply("--try-held", REF)
        self.assertEqual((rc, done["held"]["result"]), (0, "awaiting_verify"))
        self.assertEqual(evs[-2]["step"], "trial")
        self.assertEqual(evs[-2]["status"], "awaiting_verify")
        self.assertIn("Now launch the game", done["summary"])
        self.assertEqual(w.masks(), [])
        self.assertEqual(w.installed()["version"], "3.16.30")
        trial = w.holds()["holds"][REF]["pending_trial"]
        self.assertEqual((trial["from_commit"], trial["from_version"], trial["to_version"], trial["stage"]),
                         (GOOD, "3.16.28", "3.16.30", "awaiting_verify"))

        w.set(procs={"Game.exe": [[0.3, 99]]})
        rc, evs, done = w.apply("--verify-held", REF, "--timeout", "5")
        self.assertEqual((rc, done["held"]["result"]), (0, "kept"))
        self.assertEqual(done["summary"], "gamescope 3.16.30 kept. Pin removed.")
        msgs = [e["msg"] for e in evs]
        self.assertIn("Waiting for you to launch the game…", msgs)
        self.assertTrue(any(m.startswith("The game is running") for m in msgs))
        self.assertEqual(w.masks(), [])
        doc = w.holds()
        self.assertEqual(doc["holds"], {})
        rel = doc["released"][0]
        self.assertEqual((rel["ref"], rel["good"]), (REF, {"version": "3.16.30", "commit": C30}))
        self.assertEqual(rel["history"][-1]["event"], "released")
        self.assertEqual(held_items(w.collect()), [])          # no mask, no hold: nothing held

    def test_fail_pattern_rolls_back_repins_and_remembers_the_bad_version(self):
        w = self.start()
        w.apply("--try-held", REF)
        w.set(procs={"Game.exe": [[0.2, 0.5]]},
              journal=[[0.4, "[Gamescope WSI] Failed to get Xwayland server id. Failing swapchain creation."]])
        rc, evs, done = w.apply("--verify-held", REF, "--timeout", "5")
        self.assertEqual((rc, done["held"]["result"]), (0, "rolled_back"))
        self.assertEqual(done["summary"],
                         "Rolled back to 3.16.28 and pinned again. 3.16.30 is marked bad.")
        self.assertEqual(w.installed(), dict(w.installed(), version="3.16.28", commit=GOOD))
        self.assertEqual(w.masks(), [MASK])
        hold = w.holds()["holds"][REF]
        self.assertEqual(hold["bad_versions"], ["3.16.29", "3.16.30"])
        self.assertNotIn("pending_trial", hold)
        self.assertEqual(hold["history"][-1]["event"], "rolled-back")
        back = [c for c in w.flatpak_cmds("update") if f"--commit={GOOD}" in c["argv"]]
        self.assertTrue(back and back[0]["root"])              # the helper refuses --commit
        (it,) = held_items(w.collect())
        self.assertEqual(it["hold_state"], "known_bad")        # 3.16.30 is never offered again

    def test_two_quick_exits_or_a_gamescope_coredump_count_as_failure(self):
        w = self.start()
        w.apply("--try-held", REF)
        w.set(procs={"Game.exe": [[0.2, 0.4], [0.8, 1.0]]})
        rc, _evs, done = w.apply("--verify-held", REF, "--timeout", "5")
        self.assertEqual(done["held"]["result"], "rolled_back")
        self.assertIn("twice", done["cause"])

        w2 = self.start()
        w2.apply("--try-held", REF)
        w2.set(journal=[[0.2, "Process 4242 (gamescope-wl) of user 1000 dumped core."]])
        _rc, _evs, done = w2.apply("--verify-held", REF, "--timeout", "5")
        self.assertEqual(done["held"]["result"], "rolled_back")
        self.assertEqual(w2.masks(), [MASK])

    def test_try_is_refused_while_the_game_or_steam_is_running(self):
        for world_kw, expect in (({"procs": {"Game.exe": [[0, 99]]}}, "Close the game first."),
                                 ({"procs": {"gamescope": [[0, 99]]}}, "Close the game first."),
                                 ({"steam": True}, "Quit Steam first")):
            w = self.start()
            w.set(**world_kw)
            rc, _evs, done = w.apply("--try-held", REF)
            self.assertEqual((rc, done["status"], done["held"]["result"]), (1, "fail", "refused"))
            self.assertIn(expect, done["summary"])
            self.assertEqual(w.masks(), [MASK])
            self.assertEqual(w.flatpak_cmds("update"), [])
            self.assertNotIn("pending_trial", w.holds()["holds"][REF])

    def test_known_bad_needs_force(self):
        w = self.start(remote_version="3.16.29", remote_commit=C29)
        rc, _evs, done = w.apply("--try-held", REF)
        self.assertEqual((rc, done["held"]["result"]), (1, "refused"))
        self.assertIn("marked bad", done["summary"])
        self.assertEqual((w.masks(), w.flatpak_cmds("update")), ([MASK], []))
        rc, _evs, done = w.apply("--try-held", REF, "--force")
        self.assertEqual((rc, done["held"]["result"]), (0, "awaiting_verify"))

    def test_update_failure_puts_the_pin_back(self):
        w = self.start()
        w.set(update_rc=1)
        rc, evs, done = w.apply("--try-held", REF)
        self.assertEqual((rc, done["held"]["result"]), (1, "failed"))
        self.assertIn("Still pinned at 3.16.28", done["summary"])
        self.assertEqual(w.masks(), [MASK])
        self.assertEqual(w.installed()["commit"], GOOD)
        hold = w.holds()["holds"][REF]
        self.assertNotIn("pending_trial", hold)
        self.assertEqual(hold["bad_versions"], ["3.16.29"])     # a failed download is not a bad build
        self.assertTrue(any(e["step"] == "trial" and e["status"] == "fail" for e in evs))

    def test_no_launch_is_inconclusive_and_keeps_the_trial(self):
        w = self.start()
        w.apply("--try-held", REF)
        rc, _evs, done = w.apply("--verify-held", REF, "--timeout", "0.5")
        self.assertEqual((rc, done["status"], done["held"]["result"]), (0, "ok", "inconclusive"))
        self.assertIn("nothing was rolled back", done["summary"])
        self.assertEqual(w.installed()["version"], "3.16.30")
        self.assertEqual(w.masks(), [])
        self.assertIn("pending_trial", w.holds()["holds"][REF])

    def test_rollback_after_a_restart_uses_the_trial_on_disk(self):
        w = self.start()
        w.apply("--try-held", REF)
        # "reboot": a new process, nothing in memory, state.json gone, no passwordless sudo
        (w.state_dir / "state.json").unlink()
        w.set(sudo_ok=False)
        rc, _evs, done = w.apply("--rollback-held", REF)
        self.assertEqual((rc, done["held"]["result"]), (0, "rolled_back"))
        self.assertEqual((w.installed()["commit"], w.masks()), (GOOD, [MASK]))
        self.assertIn("3.16.30", w.holds()["holds"][REF]["bad_versions"])
        back = [c for c in w.flatpak_cmds("update") if f"--commit={GOOD}" in c["argv"]]
        self.assertTrue(back[0]["root"])                        # went through pkexec
        rc, _evs, done = w.apply("--rollback-held", REF)        # second click: nothing to do
        self.assertEqual((rc, done["held"]["result"]), (1, "refused"))

    def test_manual_keep_wins_without_the_watcher(self):
        w = self.start()
        w.apply("--try-held", REF)
        rc, _evs, done = w.apply("--keep-held", REF)
        self.assertEqual((rc, done["held"]["result"], w.masks()), (0, "kept", []))
        self.assertIn("by the user", w.holds()["released"][0]["history"][-1]["note"])

    def test_failed_rollback_keeps_the_trial_open_and_does_not_pin_the_bad_build(self):
        w = self.start()
        w.apply("--try-held", REF)
        w.set(update_rc=1)
        rc, _evs, done = w.apply("--rollback-held", REF)
        self.assertEqual((rc, done["held"]["result"]), (1, "failed"))
        self.assertEqual(w.masks(), [])                         # never pin 3.16.30 in place
        self.assertIn("pending_trial", w.holds()["holds"][REF])


class CorruptRegistryTest(unittest.TestCase):
    """2026-10-04: holds.json that does not parse used to come back as an empty registry, and
    the next --try-held overwrote it with a bare entry, losing gamescope's history and
    bad_versions. Now: pins still show, a .bad-<ts> copy is kept, nothing is ever written."""

    def test_unreadable_registry_is_shown_quarantined_and_never_overwritten(self):
        w = World()
        reg = w.state_dir / "holds.json"
        reg.write_text("{not json")
        items = held_items(w.collect())
        self.assertEqual([i["ref"] for i in items], [REF])
        self.assertEqual(items[0]["hold_state"], "pinned")       # no notes to classify with
        self.assertEqual(reg.read_text(), "{not json")
        bads = list(w.state_dir.glob("holds.json.bad-*"))
        self.assertEqual(len(bads), 1)
        self.assertEqual(bads[0].read_text(), "{not json")
        w.collect()                                               # same content: no second copy
        self.assertEqual(len(list(w.state_dir.glob("holds.json.bad-*"))), 1)
        self.assertIn("not valid JSON", (w.state_dir / "logs" / "collect.log").read_text())

        rc, evs, done = w.apply("--try-held", REF)
        self.assertEqual((rc, done["status"]), (1, "fail"))
        self.assertEqual((done["held"]["action"], done["held"]["result"]), ("try", "failed"))
        self.assertIn("not valid JSON", done["summary"])
        self.assertEqual(reg.read_text(), "{not json")           # untouched
        self.assertIn(MASK, w.masks())                            # the pin never came off
        self.assertEqual(w.flatpak_cmds("update"), [])

    def test_save_refuses_only_an_unreadable_file(self):
        import holds
        import common
        w = World(registry=False)
        common.STATE_DIR = w.state_dir
        holds.save({"version": 1, "holds": {}})                   # missing: fine
        holds.save({"version": 1, "holds": {}})                   # valid: fine
        (w.state_dir / "holds.json").write_text("[1, 2]")          # parses, but not an object
        with self.assertRaises(holds.RegistryUnreadable):
            holds.save({"version": 1, "holds": {}})
        self.assertEqual((w.state_dir / "holds.json").read_text(), "[1, 2]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
