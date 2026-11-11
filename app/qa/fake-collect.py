#!/usr/bin/env python3
"""Stand-in for rivet-collect: after a pause, write sample-state.json as the new state.

RIVET_FAKE_SCENARIO=held: the `held` group comes from the REAL collector run against the fake
flatpak world (backend/fixtures/fake_held.py), so the row follows what try / keep / rollback
did. Once 3.16.30 has been marked bad, "Flathub" publishes 3.16.31.
RIVET_FAKE_SCENARIO=release (2026-11-03): the sample plus the newer-Fedora row, built by the
REAL backend/release.py + rules from a canned check (Fedora 45 out, one repo without it).
"""
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
time.sleep(1.5)
doc = json.load(open(os.path.join(here, "..", "ui", "sample-state.json")))
doc["status"] = "ok"
p = os.path.join(os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet"), "state.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(os.path.join(os.path.dirname(p), "fake-collect.calls"), "a") as _fh:   # qa-e2e "steam" counts these
    _fh.write(" ".join(sys.argv) + "\n")

if os.environ.get("RIVET_FAKE_SCENARIO") == "release":
    sys.path.insert(0, os.path.join(here, "..", "..", "backend"))
    import release, rules, triage
    row = release.item(release.check(fixture={"version": 45, "current": 44, "checked_repos": 14,
                                              "missing_repos": ["copr:copr.fedorainfracloud.org:someone:tool"]}))
    rules.apply_rules([row], steam_running=False)
    d = triage.deterministic(row)
    row["verdict"], row["reasoning"] = d["verdict"], d["reasoning"]
    dnf = next(g for g in doc["groups"] if g["id"] == "dnf")
    dnf["items"].append(row)
    doc["summary"]["total"] += 1
    doc["flags"]["reboot_required"] = True
    doc["host"]["os"] = "Fedora Linux 44 (Workstation Edition)"

if os.environ.get("RIVET_FAKE_SCENARIO") in ("hacore", "hacore-none"):
    # 2026-11-10: the Home Assistant core row investigated by the REAL backend/ha_core.py: canned
    # release notes and a fake Home Assistant from backend/fixtures/fake_ha.py on 127.0.0.1, and
    # qa/fake-claude.py as claude (core-touches: MQTT is in use -> red; core-none: nothing in use is
    # touched -> a yellow monthly release). Nothing leaves this machine.
    backend = os.path.join(here, "..", "..", "backend")
    sys.path[:0] = [backend, os.path.join(backend, "fixtures")]
    none = os.environ["RIVET_FAKE_SCENARIO"] == "hacore-none"
    os.environ.update(RIVET_CLAUDE_BIN=os.path.join(here, "fake-claude.py"),
                      FAKE_CLAUDE_MODE="core-none" if none else "core-touches")
    import fake_ha
    srv = fake_ha.FakeHA(components=fake_ha.COMPONENTS_NONE if none else None).start()
    import elsewhere, ha_core
    ha_core.GH_RELEASES, ha_core.SITE_OVERRIDE = srv.url + "/gh/", srv.url
    items = doc["elsewhere"]["items"]
    row = next(i for i in items if i["id"] == "ha:update.home_assistant_core_update")
    for k in ("core_check", "risk"):
        row.pop(k, None)
    row["ai"] = []
    ha_core.investigate([row], use_ai=True, ctx=elsewhere.Ctx(env=srv.env()),
                        log=lambda m: print(m, file=sys.stderr))
    elsewhere.decide_action(row)
    srv.stop()

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
