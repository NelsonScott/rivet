#!/usr/bin/env python3
"""Stand-in for rivet-apply in app QA. Touches no package manager, never reboots.

RIVET_FAKE_SCENARIO picks the story:
  (unset)/ok  a short hand-written contract stream, exit 0. Includes a blank line and a
              non-JSON line, the two things that broke the reader on 2026-09-18.
  warn        the 2026-09-21 incident, produced by the REAL backend/rivet_apply.run_real on
              top of a scripted dnf + rpmdb (backend/fixtures/fake_dnf.py): the updater
              transaction "fails" with signal 15, every package is installed -> done/warn.
  fail        same machinery, but five packages never land (disk full) -> done/fail with names.
  crash       a few events, then exit 3 with NO done event -> the app must synthesize one.
  cancel      (2026-11-01) the REAL run_real; the polkit prompt for the rpm transaction is cancelled
              (D-Bus AccessDenied), nothing installs -> done/fail "the password prompt was cancelled".
  abuse       (2026-11-01) --other-update: the first Home Assistant row installs, every later HA
              row fails "could not reach Home Assistant" with unreachable: true (HA went away).
  held        --try-held / --verify-held / --keep-held / --rollback-held: the REAL
              backend/rivet_apply.py, exec'd with the fake flatpak / pgrep / journalctl / sudo
              world of backend/fixtures/fake_held.py first on PATH. The first build tried
              (3.16.30) dies with the Xwayland-id journal line -> automatic rollback; the next
              one (3.16.31) stays up -> kept.
  (any)       --other-update ID: a canned Other-software run for that row, done ok; the
              nginx image row always fails (2026-10-31 b; couchdb until 2026-11-01, when
              containers that hold data lost their Update button).
  (any)       --other-remove ID: the REAL refusal rule (elsewhere_apply.remove_refusal), then a
              canned uninstall. The FIRST remove in a state dir fails ("2 Matter entities now",
              the live re-check saying no); the next one succeeds and drops the row from
              state.json with the real patch_state (2026-11-01). Nothing reaches HA.
  (any)       --other-update of a HACS integration ends `restart_pending` (2026-11-01), so the
              bridge follows with --ha-restart: a canned restart that clears it in the scratch
              state.json. Scenario other-fix: Alexa Media Player fails the way it did on
              2026-11-01, and --other-fix runs the REAL other_fix.run on canned evidence with
              qa/fake-claude.py standing in for claude.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def held_world():
    backend = os.path.join(HERE, "..", "..", "backend")
    sys.path.insert(0, os.path.join(backend, "fixtures"))
    from fake_held import World, hold_entry
    state_dir = os.environ["RIVET_STATE_DIR"]
    verify = dict(hold_entry()["verify"], alive_seconds=6, quick_exit_seconds=1)
    return World(root=os.path.join(state_dir, "fake-world"), state_dir=state_dir, verify=verify), backend


if any(a.endswith("-held") for a in sys.argv[1:]):
    w, backend = held_world()
    if "--verify-held" in sys.argv:
        from fake_held import REF
        trying = w.holds()["holds"][REF]["pending_trial"]["to_version"]
        if trying == "3.16.30":     # the bad one: the game starts, gamescope gives up, it exits
            w.set(procs={"Game.exe": [[2.0, 4.0]]}, t0=time.time(), journal=[
                [3.0, "[Gamescope WSI] Atom of T was wrong type. Expected XCB_ATOM_CARDINAL"],
                [4.2, "[Gamescope WSI] Failed to get Xwayland server id. Failing swapchain creation."]])
        else:
            w.set(procs={"Game.exe": [[2.0, 999]]}, journal=[], t0=time.time())
    env = dict(w.env, RIVET_HELD_POLL_S="0.25")
    os.execve(sys.executable, [sys.executable, os.path.join(backend, "rivet_apply.py")] + sys.argv[1:], env)

if "--other-remove" in sys.argv:
    sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
    import elsewhere_apply as EA
    rid = sys.argv[sys.argv.index("--other-remove") + 1]
    sd = os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet")
    path = os.path.join(sd, "state.json")
    doc = json.load(open(path))
    it = next((i for i in (doc.get("elsewhere") or {}).get("items", []) if i["id"] == rid), None)
    out = lambda e: print(json.dumps(dict({"ts": None, "pct": None, "msg": None, "log": None}, **e)), flush=True)
    why = "%s is not in the current list (check again first)." % rid if it is None else EA.remove_refusal(it)
    if why:
        out({"step": "done", "status": "fail", "summary": why, "cause": why,
             "other": {"id": rid, "result": "refused", "item": None}})
        sys.exit(2)
    slug, name = it["addon_slug"], it["name"]
    out({"step": "other", "status": "start", "pct": 0, "msg": "Checking that nothing in Home Assistant uses %s\u2026" % name,
         "log": "POST /api/template integration_entities('matter') | count"})
    time.sleep(0.5)
    marker = os.path.join(sd, "fake-remove-tried")
    if not os.path.exists(marker):
        open(marker, "w").close()
        why = "the Matter integration has 2 entities now, so something uses it; nothing was removed"
        out({"step": "other", "status": "fail", "pct": 100, "msg": why})
        out({"step": "done", "status": "fail", "summary": "%s was not removed: %s." % (name, why), "cause": why,
             "other": {"id": rid, "result": "failed", "item": dict(it, last_run={"result": "remove_failed", "note": why}),
                       "link": it.get("addon_link")}})
        sys.exit(1)
    for m, lg in (("0 Matter entities. Asking the Supervisor about %s\u2026" % name, None),
                  ("Uninstalling %s (started, %s)\u2026" % (name, it.get("installed")),
                   'supervisor/api POST /addons/%s/uninstall {"remove_config": false}' % slug),
                  ("Waiting for the Supervisor\u2026", None)):
        time.sleep(0.5)
        out({"step": "other", "status": "progress", "msg": m, "log": lg})
    msg = "%s was uninstalled from Home Assistant." % name
    out({"step": "other", "status": "ok", "pct": 100, "msg": msg})
    EA.patch_state(it, __import__("pathlib").Path(path), remove=True)
    out({"step": "done", "status": "ok", "summary": msg, "other": {"id": rid, "result": "removed", "item": None, "removed": True}})
    sys.exit(0)

if "--other-fix" in sys.argv:
    # Fix it (2026-11-01): the REAL backend (other_fix.run) with the canned 2026-11-01 Alexa
    # evidence (backend/fixtures/fix-alexa.json: no Home Assistant call) and a fake claude
    # (qa/fake-claude.py: no model call) that "reads the state, restarts, reads it again".
    backend = os.path.join(HERE, "..", "..", "backend")
    env = dict(os.environ, RIVET_FIX_CANNED=os.path.join(backend, "fixtures", "fix-alexa.json"),
               RIVET_CLAUDE_BIN=os.path.join(HERE, "fake-claude.py"))
    os.execve(sys.executable, [sys.executable, os.path.join(backend, "rivet_apply.py")] + sys.argv[1:], env)

if "--ha-restart" in sys.argv:
    # the HA restart after an integration install: canned, clears restart_pending in the scratch state
    sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
    import elsewhere_apply as EA
    sd = os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet")
    path = __import__("pathlib").Path(sd, "state.json")
    out = lambda e: print(json.dumps(dict({"ts": None, "pct": None, "msg": None, "log": None}, **e)), flush=True)
    doc = json.loads(path.read_text()) if path.exists() else {}
    rows = [i for i in (doc.get("elsewhere") or {}).get("items", []) if i.get("restart_pending")]
    if not rows:
        msg = "Home Assistant does not say it needs a restart, so Rivet did not restart it"
        out({"step": "done", "status": "fail", "summary": msg + ".", "cause": msg,
             "other": {"id": "ha:_restart", "result": "refused", "item": None, "items": []}})
        sys.exit(2)
    names = ", ".join(r["name"] for r in rows)
    for pct, m, lg in ((0, "Checking Home Assistant's configuration before restarting\u2026", "POST /api/config/core/check_config"),
                       (15, "Restarting Home Assistant to load %s\u2026" % names, "POST /api/services/homeassistant/restart"),
                       (40, "Home Assistant is restarting\u2026", None),
                       (85, "Home Assistant is back (31 s). Checking what it loaded\u2026", None)):
        time.sleep(0.7)
        out({"step": "other", "status": "progress" if pct else "start", "pct": pct, "msg": m, "log": lg})
    done = []
    for r in rows:
        new = dict(r, restart_pending=False, loaded=str(r.get("installed") or "").lstrip("v"),
                   last_run={"result": "installed", "note": "Home Assistant restarted; it now runs %s." % r.get("installed")})
        EA.patch_state(new, path)
        done.append(new)
    msg = "Restarted Home Assistant. Now running %s." % ", ".join("%s %s" % (r["name"], r["loaded"]) for r in done)
    out({"step": "other", "status": "ok", "pct": 100, "msg": msg})
    out({"step": "done", "status": "ok", "summary": msg, "other": {"id": "ha:_restart", "result": "restarted", "item": None, "items": done}})
    sys.exit(0)

if "--other-update" in sys.argv:
    # Other software's Update / Install in HA (2026-10-31 b): a canned stream for the row,
    # nothing runs. The nginx image row always fails (the driver checks the red path).
    rid = sys.argv[sys.argv.index("--other-update") + 1]
    sd = os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet")
    try:
        doc = json.load(open(os.path.join(sd, "state.json")))
    except Exception:
        doc = json.load(open(os.path.join(HERE, "..", "ui", "sample-state.json")))
    it = next((i for i in (doc.get("elsewhere") or {}).get("items", []) if i["id"] == rid), None)
    out = lambda e: print(json.dumps(dict({"ts": None, "pct": None, "msg": None, "log": None}, **e)), flush=True)
    if it is None:
        out({"step": "done", "status": "fail", "summary": "%s is not in the current list." % rid,
             "other": {"id": rid, "result": "refused", "item": None}})
        sys.exit(2)
    ha = it["source"] == "ha"
    out({"step": "other", "status": "start", "pct": 0, "msg": "Updating %s\u2026" % it["name"],
         "log": ("POST /api/services/update/install {\"entity_id\": \"%s\"}" % it.get("entity_id")) if ha
         else "$ " + (it.get("command") or "")})
    for line in (["Installing %s\u2026 35%%" % it["name"], "Installing %s\u2026 80%%" % it["name"]] if ha else
                 ["Trying to pull %s..." % it["name"], "Error: initializing source docker://%s: pinging container registry registry-1.docker.io: Get \"https://registry-1.docker.io/v2/\": dial tcp: lookup registry-1.docker.io: no such host" % it["name"]]
                 if "nginx" in rid else ["changed 1 package in 1s", "1 package is looking for funding"]):
        time.sleep(0.4)
        out({"step": "other", "status": "progress", "msg": line[:200], "log": line})
    time.sleep(0.4)
    if os.environ.get("RIVET_FAKE_SCENARIO") == "abuse" and ha:
        # 2026-11-01 abuse pass: Home Assistant goes away after the first HA row of a run
        marker = os.path.join(sd, "fake-ha-gone")
        if os.path.exists(marker):
            sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
            import elsewhere_apply as EA
            why = ("could not reach Home Assistant at http://homeassistant.local:8123 (connection refused); "
                   "nothing was installed")
            row = dict(it, last_run={"at": None, "result": "failed", "note": why})
            EA.patch_state(row, __import__("pathlib").Path(sd, "state.json"))
            out({"step": "other", "status": "fail", "pct": 100, "msg": why})
            out({"step": "done", "status": "fail", "summary": "%s: %s." % (it["name"], why), "cause": why,
                 "other": {"id": rid, "result": "failed", "item": row, "unreachable": True}})
            sys.exit(1)
        open(marker, "w").close()
    if os.environ.get("RIVET_FAKE_SCENARIO") == "other-fix" and "alexa_media_player" in rid:
        # the 2026-11-01 failure as it was, patched into the scratch state.json (Fix it reads it)
        sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
        import elsewhere_apply as EA
        why = "Home Assistant stopped without installing it (still v5.18.0); see Settings \u203a System \u203a Logs"
        row = dict(it, last_run={"at": None, "result": "failed", "note": why})
        EA.patch_state(row, __import__("pathlib").Path(sd, "state.json"))
        out({"step": "other", "status": "fail", "pct": 100, "msg": why})
        out({"step": "done", "status": "fail", "summary": "%s: %s." % (it["name"], why), "cause": why,
             "other": {"id": rid, "result": "failed", "item": row}})
        sys.exit(1)
    if "nginx" in rid:
        why = "podman exited 125: Error: initializing source docker://%s: no such host" % it["name"]
        out({"step": "other", "status": "fail", "pct": 100, "msg": why})
        out({"step": "done", "status": "fail", "summary": "%s was not updated (%s)." % (it["name"], why), "cause": why,
             "other": {"id": rid, "result": "failed", "item": dict(it, last_run={"result": "failed", "note": why})}})
        sys.exit(1)
    row = dict(it, status="updated", installed=it.get("latest"), action=None, confirm=False)
    pending = ha and it.get("kind") == "integration"      # HACS: loads only after a core restart
    msg = ("%s %s is downloaded; Home Assistant restarts to load it." if pending else "%s is now %s.") % (it["name"], it.get("latest"))
    if pending:
        row["restart_pending"] = True
    p = __import__("pathlib").Path(sd, "state.json")
    if p.exists():                       # like the real rivet-apply: the row is patched on disk
        sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
        import elsewhere_apply as EA
        EA.patch_state(row, p)
    out({"step": "other", "status": "ok", "pct": 100, "msg": msg})
    out({"step": "done", "status": "ok", "summary": msg,
         "other": {"id": rid, "result": "installed" if ha else "updated", "item": row, "restart_pending": pending}})
    sys.exit(0)

if "--verify-kmod" in sys.argv:
    # the app's re-check after a "nothing to run" diagnosis (2026-10-12): module fine
    kver = "7.2.9-201.fc44.x86_64"
    for e in [
        {"step": "verify", "status": "start", "pct": 0, "msg": "Verifying the NVIDIA module for %s\u2026" % kver, "log": None},
        {"step": "verify", "status": "progress", "pct": 50, "msg": "kmod-nvidia-%s: present" % kver, "log": None},
        {"step": "verify", "status": "progress", "pct": 90, "msg": "modinfo -k %s nvidia: ok" % kver, "log": None},
        {"step": "verify", "status": "ok", "pct": 100, "msg": "NVIDIA module verified for the new kernel.", "log": None},
        {"step": "done", "status": "ok", "pct": 100, "msg": None, "log": None, "reboot_required": True,
         "kmod_verified": True, "kver": kver,
         "summary": "The NVIDIA module is verified for %s. A reboot is needed to finish." % kver},
    ]:
        print(json.dumps(e), flush=True)
        time.sleep(0.1)
    sys.exit(0)

plan = json.load(open(sys.argv[sys.argv.index("--plan") + 1]))
scenario = os.environ.get("RIVET_FAKE_SCENARIO", "ok")
n = len(plan["items"])


def simple():
    evs = [
        {"step": "download", "status": "start", "pct": 0, "msg": "resolving", "log": "Resolving %d updates" % n},
        {"step": "download", "status": "ok", "pct": 100, "msg": "done", "log": "GPG ok"},
        {"step": "install", "status": "start", "pct": 0, "msg": "transaction", "log": "Running transaction"},
        "stray tool output, not json",
        "",
    ]
    if scenario == "crash":
        evs += [{"step": "install", "status": "progress", "pct": 41, "msg": "Upgrade mesa-dri-drivers-0:26.1.4-1.fc44.x86_64",
                 "log": "mesa-dri-drivers-0:26.1.4-1.fc44.x86_64"}]
    elif scenario == "kmodfail":
        # 2026-10-12 20:03: everything installed, verify raced depmod -> done fail, kmod_verified false
        kver = "7.2.9-201.fc44.x86_64"
        cause = ("The NVIDIA kernel module for the new kernel could not be verified \u2014 do not reboot "
                 "until it is.")
        evs += [
            {"step": "install", "status": "ok", "pct": 100, "msg": "RPM transaction complete.", "log": None,
             "verify": {"planned": n, "installed": n, "missing": [], "leftover": []}},
            {"step": "akmod", "status": "start", "pct": 0, "msg": "Waiting for the NVIDIA module to build for %s\u2026" % kver, "log": None},
            {"step": "akmod", "status": "ok", "pct": 100, "msg": "kmod-nvidia-%s is installed." % kver, "log": None},
            {"step": "verify", "status": "start", "pct": 0, "msg": "Verifying the NVIDIA module for %s\u2026" % kver, "log": None},
            {"step": "verify", "status": "progress", "pct": 50, "msg": "kmod-nvidia-%s: present" % kver, "log": None},
            {"step": "verify", "status": "progress", "pct": 90, "msg": "modinfo -k %s nvidia: FAILED" % kver, "log": None},
            {"step": "verify", "status": "fail", "pct": 100, "msg": "NVIDIA module NOT verified \u2014 do not reboot yet.", "log": None},
            {"step": "done", "status": "fail", "pct": 100, "log": None, "reboot_required": True, "kmod_verified": False,
             "msg": "Finished with errors: %d package(s). %s" % (n, cause),
             "summary": "Finished with errors: %d package(s). %s A reboot is needed to finish." % (n, cause),
             "cause": cause, "error": None, "warnings": [],
             "results": {"dnf": {"planned": n, "installed": n, "missing": [], "leftover": []}}},
        ]
    else:
        evs += [
            {"step": "install", "status": "ok", "pct": 100, "msg": "RPM transaction complete.", "log": None},
            {"step": "done", "status": "ok", "pct": 100, "msg": "Applied %d package(s)." % n, "log": None,
             "reboot_required": False, "kmod_verified": None, "summary": "Applied %d package(s)." % n},
        ]
    for e in evs:
        print(json.dumps(e) if isinstance(e, dict) else e, flush=True)
        time.sleep(0.15)
    return 3 if scenario == "crash" else 1 if scenario == "kmodfail" else 0


def real_backend():
    backend = os.path.join(HERE, "..", "..", "backend")
    sys.path.insert(0, backend)
    sys.path.insert(0, os.path.join(backend, "fixtures"))
    import rivet_apply as A
    from fake_dnf import FakeWorld

    state = json.load(open(os.path.join(HERE, "..", "ui", "sample-state.json")))
    extra = json.load(open(os.path.join(backend, "fixtures", "state-selfupdate.json")))
    have = {i["id"] for g in state["groups"] for i in g["items"]}
    dnf = next(g for g in state["groups"] if g["id"] == "dnf")
    ids = list(plan["items"])
    for it in extra["groups"][0]["items"]:          # the updater rides along, as on 09-21
        if it["name"] in ("dnf5daemon-server", "libdnf5", "systemd") and it["id"] not in have:
            dnf["items"].append(it)
            ids.append(it["id"])
    for g in state["groups"]:
        for it in g["items"]:
            it["_scope"] = "user"
            it["blocked_by"] = None

    items = [i for i in dnf["items"] if i["id"] in ids]
    world = FakeWorld.from_items(items)
    world.pace = 0.04
    world.install(A)
    A.AKMOD_POLL_S = 0
    A.stream_cmd = lambda cmd, em, step, **kw: 0          # no flatpak, no fwupdmgr
    if scenario == "cancel":
        # 2026-11-01 abuse pass: the polkit dialog for the rpm transaction is cancelled
        world.raise_tx = {0: "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Not authorized"}
        world.drop = {i["name"] for i in items}
        world.journal = {"self_restart": False, "error": None}
    elif scenario == "warn":
        world.fail_tx = {1}
        world.journal = {"self_restart": True, "error":
                         "WARNING [rpm] %transfiletriggerpostun(systemd-259.9-1.fc44.x86_64) "
                         "scriptlet failed, signal 15"}
    else:
        world.fail_tx = {0}
        world.drop = {i["name"] for i in items if i["name"].startswith("gstreamer1")}
        world.journal = {"self_restart": False, "error":
                         "ERROR [rpm] unpacking of archive failed on file "
                         "/usr/lib64/libgstreamer-1.0.so.0.2807.0: cpio: write failed - "
                         "No space left on device"}
    return A.run_real(A.Emitter(), ids, resolve_only=False, state=state)


sys.exit(real_backend() if scenario in ("warn", "fail", "cancel") else simple())
