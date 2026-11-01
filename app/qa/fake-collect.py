#!/usr/bin/env python3
"""Stand-in for rivet-collect: after a pause, write sample-state.json as the new state.

RIVET_FAKE_SCENARIO=held: the `held` group comes from the REAL collector run against the fake
flatpak world (backend/fixtures/fake_held.py), so the row follows what try / keep / rollback
did. Once 3.16.30 has been marked bad, "Flathub" publishes 3.16.31.
"""
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
time.sleep(1.5)
doc = json.load(open(os.path.join(here, "..", "ui", "sample-state.json")))
doc["status"] = "ok"
p = os.path.join(os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet"), "state.json")
os.makedirs(os.path.dirname(p), exist_ok=True)

if os.environ.get("RIVET_FAKE_SCENARIO") == "held":
    backend = os.path.join(here, "..", "..", "backend")
    sys.path.insert(0, os.path.join(backend, "fixtures"))
    from fake_held import C31, REF, World, hold_entry
    state_dir = os.path.dirname(p)
    verify = dict(hold_entry()["verify"], alive_seconds=6, quick_exit_seconds=1)
    w = World(root=os.path.join(state_dir, "fake-world"), state_dir=state_dir, verify=verify)
    hold = w.holds()["holds"].get(REF) or {}
    if "3.16.30" in (hold.get("bad_versions") or []):
        world = w.read()
        world["remote"][REF] = {"version": "3.16.31", "commit": C31}
        w.write(world)
    # the real rivet_collect.py with the fake tools on PATH, in a scratch state dir so its
    # own state.json never reaches the app (only the merged document below does)
    import shutil
    scratch = os.path.join(state_dir, "collect-scratch")
    os.makedirs(scratch, exist_ok=True)
    shutil.copy(os.path.join(state_dir, "holds.json"), scratch)
    cp = subprocess.run([sys.executable, os.path.join(backend, "rivet_collect.py"), "--no-llm", "--quiet"],
                        env=dict(w.env, RIVET_STATE_DIR=scratch), capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    real = json.load(open(os.path.join(scratch, "state.json")))
    held = next(g for g in real["groups"] if g["id"] == "held")
    mock = next(g for g in doc["groups"] if g["id"] == "held")
    mock["items"] = held["items"] + [i for i in mock["items"] if i["hold_state"] == "known_bad"]
    doc["flags"].update(held_count=len(mock["items"]),
                        held_candidates=sum(1 for i in mock["items"] if i["hold_state"] == "candidate"))

tmp = p + ".tmp"
json.dump(doc, open(tmp, "w")); os.replace(tmp, p)
