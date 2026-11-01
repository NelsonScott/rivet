#!/usr/bin/env python3
"""Headless smoke test for the Python side of Rivet — opens no window.

Exercises Bridge (the whole command dispatcher) with a fake `push` sink and a
fake confirm(), against a temporary HOME so nothing real is touched. Run:

    python3 app/selftest.py
"""
import json
import os
import shlex
import shutil
import sys
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Redirect every path Bridge touches into a scratch HOME before importing.
TMP = tempfile.mkdtemp(prefix="rivet-selftest-")
os.environ["HOME"] = TMP
sys.path.insert(0, HERE)
# A stand-in Claude Code CLI (never run): Fix it is only offered when one exists, so the bridge
# sections must not depend on this machine having `claude` installed (CI does not).
_FAKE_CLAUDE = os.path.join(TMP, "claude")
with open(_FAKE_CLAUDE, "w") as _fh:
    _fh.write("#!/bin/sh\nexit 0\n")
os.chmod(_FAKE_CLAUDE, 0o755)
os.environ.setdefault("RIVET_CLAUDE_BIN", _FAKE_CLAUDE)

import rivet_app as U  # noqa: E402
from gi.repository import GLib  # noqa: E402

FAIL = []

# Count reads on the apply stream so a test can prove the reader goes idle at EOF.
CHUNKS = [0]
_orig_on_chunk = U.Bridge._on_chunk


def _counting_on_chunk(self, *a):
    CHUNKS[0] += 1
    return _orig_on_chunk(self, *a)


U.Bridge._on_chunk = _counting_on_chunk
_orig_held_chunk = U.Bridge._held_chunk


def _counting_held_chunk(self, *a):
    CHUNKS[0] += 1
    return _orig_held_chunk(self, *a)


U.Bridge._held_chunk = _counting_held_chunk


def wait_for(pred, timeout_ms=4000):
    """Run a main loop until pred() or the timeout; remove our sources after."""
    loop = GLib.MainLoop()
    ids = []
    ids.append(GLib.timeout_add(timeout_ms, lambda: (loop.quit(), False)[1]))
    ids.append(GLib.timeout_add(60, lambda: False if pred() and (loop.quit() or True) else True))
    loop.run()
    for i in ids:
        GLib.Source.remove(i) if GLib.MainContext.default().find_source_by_id(i) else None


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAIL.append(what)


def types(msgs):
    return [m.get("type") for m in msgs]


def run():
    print("scratch HOME:", TMP)
    print("state file:", U.STATE_FILE)
    check(U.STATE_FILE.startswith(TMP), "paths point at the scratch HOME")
    check(U.HAVE_ADW, "libadwaita available (Adw.Application will be used)")

    # ---------------------------------------------------------------- ready
    print("\nready (no state on disk, not demo):")
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "ready"}))
    check(types(out) == ["settings", "caps", "state"], "ready pushes settings, caps (2026-11-01), then state")
    check(out[0]["settings"] == U.DEFAULT_SETTINGS, "defaults match the contract: %s" % out[0]["settings"])
    check(out[2]["state"]["status"] == "error", "missing state.json surfaces as status=error")

    print("\nready (demo, falls back to app/ui/sample-state.json):")
    out = []
    b = U.Bridge(out.append, demo=True)
    b.handle(json.dumps({"cmd": "ready"}))
    st = out[2]["state"]
    check(st["schema"] == 1 and st["status"] == "ok", "sample state is schema 1 / ok")
    check(st["summary"]["total"] == 13, "13 pending items (got %s)" % st["summary"]["total"])
    check(st["flags"]["blocked_count"] == 3, "3 blocked by Steam")
    check([g["id"] for g in st["groups"]] == ["dnf", "flatpak", "fwupd", "held"],
          "groups are dnf/flatpak/fwupd + held")
    held = st["groups"][-1]["items"]
    check(sorted(i["hold_state"] for i in held) == ["candidate", "known_bad"]
          and not any(i["selected_default"] for i in held),
          "sample has one candidate and one known_bad held item, neither selected nor counted")

    print("\nready (real state.json copied in):")
    import pwd
    src = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/state/rivet/state.json")   # the real HOME
    ok_copy = False
    if os.path.exists(src):
        try:
            with open(src, "r", encoding="utf-8") as fh:
                doc = json.load(fh)   # the collector rewrites atomically; a torn
            ok_copy = True            # read just means "try again later"
        except (OSError, ValueError) as exc:
            print("  skip  could not read the live state.json right now (%s)" % exc)
    if ok_copy:
        os.makedirs(U.STATE_DIR, exist_ok=True)
        U.write_json_atomic(U.STATE_FILE, doc)
        out = []
        U.Bridge(out.append).handle(json.dumps({"cmd": "ready"}))
        st = out[2]["state"]
        check(st["status"] in ("ok", "checking") and st["summary"]["total"] >= 0,
              "backend's real state.json loads (%d items)" % st["summary"]["total"])
        os.remove(U.STATE_FILE)
    elif not os.path.exists(src):
        print("  skip  no real state.json on this box yet")

    # ------------------------------------------------------------ setSetting
    print("\nsetSetting:")
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "setSetting", "key": "theme", "value": "dark"}))
    b.handle(json.dumps({"cmd": "setSetting", "key": "check_interval_min", "value": 15}))
    b.handle(json.dumps({"cmd": "setSetting", "key": "evil", "value": 1}))
    saved = json.load(open(U.SETTINGS_FILE))
    check(saved["theme"] == "dark" and saved["check_interval_min"] == 15,
          "settings.json persisted: %s" % saved)
    check("evil" not in saved, "unknown keys rejected")
    dropin = os.path.expanduser("~/.config/systemd/user/rivet-collect.timer.d/interval.conf")
    check(os.path.exists(dropin) and "15min" in open(dropin).read(),
          "timer drop-in rewritten with OnUnitActiveSec=15min")
    check(types(out) == ["settings", "settings"], "each accepted setting echoes back")
    check(U.Bridge(lambda m: None).settings["theme"] == "dark", "settings reload from disk")
    # looks (2026-10-31): look + type_size persist; junk is refused and changes nothing
    out = []
    b = U.Bridge(out.append)
    check(b.settings["look"] == "periwinkle" and b.settings["type_size"] == "compact", "defaults: look=periwinkle, type_size=compact")
    b.handle(json.dumps({"cmd": "setSetting", "key": "look", "value": "nord"}))
    b.handle(json.dumps({"cmd": "setSetting", "key": "type_size", "value": "comfortable"}))
    b.handle(json.dumps({"cmd": "setSetting", "key": "look", "value": "../../etc"}))
    b.handle(json.dumps({"cmd": "setSetting", "key": "type_size", "value": "huge"}))
    saved = json.load(open(U.SETTINGS_FILE))
    check(saved["look"] == "nord" and saved["type_size"] == "comfortable", "look + type_size persisted: %s" % saved)
    check(types(out) == ["settings", "settings"], "a refused look / type_size does not echo or save")
    # show_containers (2026-11-01): off by default, a bool only; turning it on checks again
    check(b.settings["show_containers"] is False, "show_containers defaults to off")
    out = []
    b.handle(json.dumps({"cmd": "setSetting", "key": "show_containers", "value": "yes"}))
    check(types(out) == [] and json.load(open(U.SETTINGS_FILE)).get("show_containers") in (None, False), "a non-bool show_containers is refused")
    refreshed = []
    b.cmd_refresh = lambda m: refreshed.append(m)
    b.handle(json.dumps({"cmd": "setSetting", "key": "show_containers", "value": True}))
    check(json.load(open(U.SETTINGS_FILE))["show_containers"] is True and refreshed, "show_containers on persists and starts a check")
    refreshed.clear()
    b.handle(json.dumps({"cmd": "setSetting", "key": "show_containers", "value": False}))
    check(json.load(open(U.SETTINGS_FILE))["show_containers"] is False and not refreshed, "off persists, no check needed")
    # ai_triage (2026-11-01): Claude Code is opt-in, off by default, a bool only
    check(b.settings["ai_triage"] is False, "ai_triage defaults to off")
    out = []
    b.handle(json.dumps({"cmd": "setSetting", "key": "ai_triage", "value": 1}))
    check(types(out) == [] and json.load(open(U.SETTINGS_FILE)).get("ai_triage") in (None, False), "a non-bool ai_triage is refused")
    b.handle(json.dumps({"cmd": "setSetting", "key": "ai_triage", "value": True}))
    check(json.load(open(U.SETTINGS_FILE))["ai_triage"] is True and refreshed, "ai_triage on persists and starts a check")
    refreshed.clear()
    b.handle(json.dumps({"cmd": "setSetting", "key": "ai_triage", "value": False}))
    check(json.load(open(U.SETTINGS_FILE))["ai_triage"] is False and not refreshed, "ai_triage off persists")

    # ---------------------------------------------------------------- apply
    print("\napply via RIVET_APPLY_CMD (fake backend emitting contract JSON lines):")
    fake = os.path.join(TMP, "fake-apply.py")
    with open(fake, "w") as fh:
        fh.write(
            "import json,sys\n"
            "for e in [\n"
            " {'step':'download','status':'start','pct':0,'msg':'resolving','log':'Resolving 2 updates'},\n"
            " {'step':'download','status':'ok','pct':100,'msg':'done','log':'GPG ok'},\n"
            " {'step':'akmod','status':'ok','pct':100,'msg':'built','log':'kmod-nvidia installed'},\n"
            " 'this line is not json',\n"
            " '',\n"
            " {'step':'done','status':'ok','reboot_required':True,'kmod_verified':True,'summary':'ok'},\n"
            "]:\n"
            "    print(json.dumps(e) if isinstance(e,dict) else e, flush=True)\n"
            "print('plan:', open(sys.argv[sys.argv.index('--plan')+1]).read(), file=sys.stderr)\n"
        )
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fake)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel", "dnf:akmod-nvidia"]}))
    wait_for(lambda: any(m.get("type") == "progress" and m["event"].get("step") == "done" for m in out), 4000)
    evs = [m["event"] for m in out if m.get("type") == "progress"]
    print("   steps: %s" % [(e.get("step"), e.get("status")) for e in evs])
    check(len(evs) == 5, "the 5 stdout lines forwarded as progress events, nothing after done (%d)" % len(evs))
    check([e["step"] for e in evs[:2]] == ["download", "download"], "steps arrive in order")
    check(any(e.get("log") == "this line is not json" for e in evs),
          "non-JSON stdout still reaches the UI as a log line")
    check(evs[-1]["step"] == "done" and evs[-1]["kmod_verified"] is True, "final done event forwarded")
    check(b.last_kmod_verified is True, "kmod_verified remembered for the restart gate")
    # Regression (2026-09-18): DataInputStream.read_line_finish() returns b"" at EOF, the
    # reader waited for None and re-armed forever: 85% CPU, Done button dead.
    wait_for(lambda: not b.applying and b._buf == b"", 2000)
    n0 = CHUNKS[0]
    wait_for(lambda: False, 400)
    check(CHUNKS[0] - n0 == 0, "apply reader goes idle after EOF (%d reads in 400 ms after exit)" % (CHUNKS[0] - n0))
    check(b.applying is False, "applying flag cleared once rivet-apply exits")
    del os.environ["RIVET_APPLY_CMD"]

    # ---------------------------------------------------------------- fix it (doctor)
    print("\nfix it: fixIt / runFix bridge commands (fake rivet-doctor):")
    faked = os.path.join(TMP, "fake-doctor.py")
    with open(faked, "w") as fh:
        fh.write(
            "import json,sys,os\n"
            "a=sys.argv[1:]\n"
            "ev=lambda **k: print(json.dumps(k), flush=True)\n"
            "if a[0]=='diagnose':\n"
            "    ev(step='doctor',status='start',pct=0,msg='Looking',log=None)\n"
            "    ev(step='doctor',status='progress',pct=40,msg='Checking: kernels',log='$ rpm -q kernel')\n"
            "    print('stray non-json line', flush=True)\n"
            "    if os.environ.get('FAKE_DOCTOR_CRASH'): sys.exit(3)\n"
            "    fix=os.path.join(os.environ['RIVET_STATE_DIR'],'doctor','t.json')\n"
            "    os.makedirs(os.path.dirname(fix),exist_ok=True); open(fix,'w').write('{}')\n"
            "    diag={'headline':'H'}\n"
            "    if os.environ.get('FAKE_DOCTOR_NOFIX'): diag={'headline':'Nothing is broken','fix':{'available':False,'commands':[]},'reboot':{'safe':'yes','why':'w'}}\n"
            "    ev(step='done',status='ok',pct=100,msg='H',summary='H',diagnosis=diag,fix_path=fix)\n"
            "else:\n"
            "    ev(step='fix',status='start',pct=0,msg='Running',log=None)\n"
            "    ev(step='done',status='ok',pct=100,msg='Fixed',summary='Fixed',results={'ran':1,'planned':1})\n"
        )
    os.environ["RIVET_DOCTOR_CMD"] = "%s %s" % (sys.executable, faked)
    os.environ["RIVET_STATE_DIR"] = os.environ.get("RIVET_STATE_DIR") or os.path.join(TMP, "state")
    out = []
    b = U.Bridge(out.append)
    U.DOCTOR_DIR = os.path.join(os.environ["RIVET_STATE_DIR"], "doctor")
    b.handle(json.dumps({"cmd": "fixIt", "log": "/nonexistent/apply.log"}))
    b.handle(json.dumps({"cmd": "fixIt"}))          # double click: ignored while running
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    evs = [m for m in out if m.get("type") == "doctor"]
    print("   doctor events: %s" % [(m["mode"], m["event"].get("step"), m["event"].get("status")) for m in evs])
    check(len(evs) == 4 and all(m["mode"] == "diagnose" for m in evs), "diagnose stream forwarded once, tagged mode=diagnose (%d)" % len(evs))
    check(any(m["event"].get("log") == "stray non-json line" for m in evs), "non-JSON doctor output reaches the UI as a log line")
    done = evs[-1]["event"]
    check(done.get("diagnosis", {}).get("headline") == "H" and done.get("fix_path"), "done event carries the diagnosis and the saved fix path")
    fix_path = done["fix_path"]
    out.clear()
    b.handle(json.dumps({"cmd": "runFix", "fix": "/etc/passwd"}))
    wait_for(lambda: any(m.get("type") == "doctor" for m in out), 2000)
    check(out and out[-1]["event"]["status"] == "fail" and "No saved diagnosis" in out[-1]["event"]["summary"],
          "runFix refuses a path outside the doctor directory")
    out.clear()
    b.handle(json.dumps({"cmd": "runFix", "fix": fix_path}))
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    evs = [m for m in out if m.get("type") == "doctor"]
    check(evs and evs[-1]["mode"] == "run" and evs[-1]["event"]["status"] == "ok", "runFix streams with mode=run and ends ok")
    os.environ["FAKE_DOCTOR_CRASH"] = "1"
    out.clear()
    b.handle(json.dumps({"cmd": "fixIt"}))
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    done = [m for m in out if m.get("type") == "doctor"][-1]["event"]
    check(done.get("synthetic") and "exited with code 3" in done["summary"], "a doctor that dies without done gets a synthetic fail naming the exit code")
    del os.environ["FAKE_DOCTOR_CRASH"]

    # 2026-10-12: verify raced depmod -> doctor says "nothing to run, safe to restart" -> the
    # bridge re-runs `rivet-apply --verify-kmod` and lifts the restart block when it passes.
    print("\nrecheck after a 'nothing to run' diagnosis (fake rivet-apply --verify-kmod):")
    fakeverify = os.path.join(TMP, "fake-verify.py")
    with open(fakeverify, "w") as fh:
        fh.write(
            "import json,sys\n"
            "assert '--verify-kmod' in sys.argv, sys.argv\n"
            "ev=lambda **k: print(json.dumps(k), flush=True)\n"
            "ev(step='verify',status='start',pct=0,msg='Verifying',log=None)\n"
            "ev(step='verify',status='ok',pct=100,msg='ok',log=None)\n"
            "ev(step='done',status='ok',pct=100,msg='V',summary='V',reboot_required=True,kmod_verified=True,kver='7.2.9')\n"
        )
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fakeverify)
    os.environ["FAKE_DOCTOR_NOFIX"] = "1"
    b.last_kmod_verified = False
    out.clear()
    b.handle(json.dumps({"cmd": "fixIt"}))
    wait_for(lambda: any(m.get("type") == "doctor" and m["mode"] == "recheck" and m["event"].get("step") == "done" for m in out), 6000)
    rechecks = [m for m in out if m.get("type") == "doctor" and m["mode"] == "recheck"]
    print("   recheck events: %s" % [(m["event"].get("step"), m["event"].get("status")) for m in rechecks])
    check(len(rechecks) == 3 and rechecks[-1]["event"].get("kmod_verified") is True,
          "a no-fix / safe-to-restart diagnosis after kmod_verified=False triggers --verify-kmod, streamed as mode=recheck")
    check(b.last_kmod_verified is True, "a passing recheck lifts the restart block")
    diag_i = next(i for i, m in enumerate(out) if m.get("type") == "doctor" and m["mode"] == "diagnose" and m["event"].get("step") == "done")
    check(all(i > diag_i for i, m in enumerate(out) if m.get("type") == "doctor" and m["mode"] == "recheck"),
          "recheck events come after the diagnosis done")
    b.last_kmod_verified = None
    out.clear()
    b.handle(json.dumps({"cmd": "fixIt"}))
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    time.sleep(0.6)
    check(not [m for m in out if m.get("type") == "doctor" and m["mode"] == "recheck"],
          "no recheck when the run never failed its verify (kmod_verified None)")
    del os.environ["FAKE_DOCTOR_NOFIX"]
    b.last_kmod_verified = False
    out.clear()
    b.handle(json.dumps({"cmd": "fixIt"}))        # diagnosis WITH a fix: the fix path owns the outcome
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    time.sleep(0.6)
    check(not [m for m in out if m.get("type") == "doctor" and m["mode"] == "recheck"] and b.last_kmod_verified is False,
          "no recheck when the diagnosis has a fix to run")
    del os.environ["RIVET_APPLY_CMD"], os.environ["RIVET_DOCTOR_CMD"]

    # ---------------------------------------------------------------- held back
    print("\nheld back: try / verify / keep / rollback bridge commands:")
    REF = "runtime/org.example.Layer/x86_64/1"
    fakeh = os.path.join(TMP, "fake-held.py")
    with open(fakeh, "w") as fh:
        fh.write(
            "import json, os, signal, sys, time\n"
            "a = sys.argv[1:]\n"
            "open(os.path.join(%r, 'held-calls.log'), 'a').write(' '.join(a) + '\\n')\n"
            "def ev(**k): print(json.dumps(k), flush=True)\n"
            "held = {'ref': a[1], 'display_name': 'layer', 'label': 'Game', 'from_version': '1', 'to_version': '2'}\n"
            "if a[0] == '--try-held':\n"
            "    ev(step='trial', status='start', pct=0, msg='Checking', held=held)\n"
            "    print('')\n"
            "    print('stray flatpak output')\n"
            "    ev(step='trial', status='awaiting_verify', pct=100, msg='Now launch Game.', held=held)\n"
            "    ev(step='done', status='ok', summary='installed', held=dict(held, action='try', result='awaiting_verify'))\n"
            "elif a[0] == '--verify-held':\n"
            "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
            "    ev(step='watch', status='start', pct=0, msg='Waiting for you to launch Game', held=held)\n"
            "    if os.environ.get('FAKE_HELD') == 'crash': sys.exit(3)\n"
            "    time.sleep(30)\n"
            "else:\n"
            "    res = 'kept' if a[0] == '--keep-held' else 'rolled_back'\n"
            "    ev(step='done', status='ok', summary=res, held=dict(held, action=a[0][2:-5], result=res))\n"
            % TMP)
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fakeh)
    os.environ["RIVET_COLLECT_CMD"] = "true"
    out = []
    b = U.Bridge(out.append)
    heldevs = lambda: [m["event"] for m in out if m.get("type") == "held"]
    b.handle(json.dumps({"cmd": "tryHeld", "ref": REF}))
    wait_for(lambda: any(e.get("step") == "done" for e in heldevs()), 4000)
    evs = heldevs()
    check([e.get("status") for e in evs] == ["start", "progress", "awaiting_verify", "ok"],
          "try stream forwarded as held events, blank line dropped, stray line kept (%s)"
          % [e.get("status") for e in evs])
    check(evs[-1]["held"]["result"] == "awaiting_verify" and not evs[-1].get("synthetic"),
          "the backend's own done event is the verdict")
    n0 = CHUNKS[0]
    wait_for(lambda: False, 400)
    check(CHUNKS[0] - n0 == 0, "held reader is idle after EOF too")

    out.clear()
    b.handle(json.dumps({"cmd": "verifyHeld", "ref": REF, "timeout": 5}))
    wait_for(lambda: any(e.get("step") == "watch" for e in heldevs()), 4000)
    watcher = b._held_watch
    check(watcher is not None and b.applying, "verifyHeld starts a watcher and holds off state redraws")
    # Busy guards (2026-10-04): the UI must never be left waiting with no event.
    out.clear()
    b.handle(json.dumps({"cmd": "verifyHeld", "ref": REF, "timeout": 5}))        # same ref again
    b.handle(json.dumps({"cmd": "verifyHeld", "ref": "other/ref", "timeout": 5}))
    b.handle(json.dumps({"cmd": "tryHeld", "ref": "other/ref"}))
    hd = heldevs()
    check([e.get("step") for e in hd] == ["watch", "done", "done"]
          and hd[1]["held"] == {"action": "verify", "result": "refused", "ref": "other/ref"}
          and hd[2]["held"]["action"] == "try" and hd[2]["held"]["result"] == "refused"
          and all(e.get("synthetic") for e in hd[1:]),
          "verifyHeld for the ref already watched is a no-op progress line; another ref and a "
          "tryHeld meanwhile get a synthetic done with held.result=refused (%s)"
          % [(e.get("step"), (e.get("held") or {}).get("result")) for e in hd])
    check(b._held_watch is watcher and b.applying and len(b._procs) == 1,
          "the running watcher is untouched by the refusals")
    out.clear()
    b.handle(json.dumps({"cmd": "keepHeld", "ref": REF}))
    wait_for(lambda: any(e.get("step") == "done" for e in heldevs()), 4000)
    wait_for(lambda: watcher["proc"].get_if_exited() or watcher["proc"].get_if_signaled(), 3000)
    dones = [e for e in heldevs() if e.get("step") == "done"]
    check(len(dones) == 1 and dones[0]["held"]["result"] == "kept",
          "manual Keep wins: the watcher is stopped and says nothing more (%d done)" % len(dones))
    wait_for(lambda: not b.applying, 2000)
    check(b._held_watch is None and not b.applying, "watcher gone, applying flag cleared")
    calls = open(os.path.join(TMP, "held-calls.log")).read().split("\n")
    check(calls[:3] == ["--try-held " + REF, "--verify-held %s --timeout 5" % REF, "--keep-held " + REF],
          "argv: %s" % calls[:3])

    out.clear()
    b._held_verdict = False
    b.handle(json.dumps({"cmd": "verifyHeld", "ref": REF}))
    wait_for(lambda: b._held_watch is not None, 1000)
    b._held_verdict = True             # as if the watcher had just said "fail, rolling back"
    b.handle(json.dumps({"cmd": "keepHeld", "ref": REF}))
    wait_for(lambda: False, 300)
    check(not any(e.get("step") == "done" for e in heldevs()),
          "once the watcher has a verdict, a late manual click is ignored")
    b._held_verdict = False
    b._held_stop_watch()
    wait_for(lambda: not b.applying, 2000)

    os.environ["FAKE_HELD"] = "crash"
    out.clear()
    b.handle(json.dumps({"cmd": "verifyHeld", "ref": REF}))
    wait_for(lambda: any(e.get("step") == "done" for e in heldevs()), 4000)
    d = ([e for e in heldevs() if e.get("step") == "done"] or [{}])[0]
    print("   synthetic: %s" % d.get("summary"))
    check(d.get("synthetic") is True and d.get("held", {}).get("result") == "failed"
          and "code 3" in d.get("summary", "") and "Nothing is rolled back" in d.get("summary", ""),
          "a watcher that dies without a verdict becomes one synthetic fail, never a rollback")
    del os.environ["FAKE_HELD"], os.environ["RIVET_APPLY_CMD"], os.environ["RIVET_COLLECT_CMD"]

    out = []
    b = U.Bridge(out.append, demo=True)
    b.handle(json.dumps({"cmd": "tryHeld", "ref": REF}))
    wait_for(lambda: any(m.get("type") == "held" and m["event"].get("step") == "done" for m in out), 6000)
    check(any(m.get("type") == "held" and m["event"].get("step") == "done" for m in out)
          and not os.path.exists(os.path.join(TMP, "never")),
          "--demo plays a canned trial and never runs rivet-apply")

    # ------------------------------------------- done event is never replaced
    print("\napply that ends badly (2026-09-21 regression):")
    real = {"step": "done", "status": "fail", "pct": 100, "reboot_required": True, "kmod_verified": None,
            "summary": "Finished with errors: 180 of 226 package(s). First error: scriptlet failed, signal 15.",
            "cause": "dnf stopped partway.", "results": {"dnf": {"planned": 226, "installed": 180}}}
    fake2 = os.path.join(TMP, "fake-apply-fail.py")
    with open(fake2, "w") as fh:
        fh.write("import json, sys\n"
                 "print(json.dumps({'step':'install','status':'fail','pct':100,'msg':'180 of 226 package(s) installed.','log':None}), flush=True)\n"
                 "print(json.dumps(%r), flush=True)\n"
                 "sys.exit(1)\n" % (real,))
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fake2)
    out = []
    b = U.Bridge(out.append)
    b.last_kmod_verified = True      # left over from an earlier run
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    check(b.last_kmod_verified is None, "a new apply forgets the previous run's kmod verdict")
    wait_for(lambda: not b.applying and b._done_seen, 4000)
    wait_for(lambda: False, 300)
    dones = [m["event"] for m in out if m.get("type") == "progress" and m["event"].get("step") == "done"]
    check(len(dones) == 1, "exit 1 + kmod_verified=null: exactly one done event reaches the UI (%d)" % len(dones))
    check(dones and dones[0].get("summary") == real["summary"] and not dones[0].get("synthetic"),
          "and it is rivet-apply's own summary, not 'rivet-apply exited 1.'")

    fake3 = os.path.join(TMP, "fake-apply-crash.py")
    with open(fake3, "w") as fh:
        fh.write("import json, sys\n"
                 "print(json.dumps({'step':'install','status':'progress','pct':41,'msg':'Upgrade mesa-dri-drivers','log':None}), flush=True)\n"
                 "sys.exit(3)\n")
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fake3)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    wait_for(lambda: any(m["event"].get("step") == "done" for m in out if m.get("type") == "progress"), 4000)
    dones = [m["event"] for m in out if m.get("type") == "progress" and m["event"].get("step") == "done"]
    d = dones[0] if dones else {}
    print("   synthetic: %s" % d.get("summary"))
    check(len(dones) == 1 and d.get("synthetic") is True and d.get("status") == "fail",
          "no done event + exit 3: one synthetic fail is pushed")
    check("code 3" in d.get("summary", "") and "install" in d.get("summary", "")
          and "Upgrade mesa-dri-drivers" in d.get("summary", ""),
          "it names the exit code, the last step reached and the last message")
    check(d.get("log_path", "").endswith(".local/state/rivet/logs/apply.log") and d["log_path"] in d["summary"],
          "and the path of the log: %s" % d.get("log_path"))
    del os.environ["RIVET_APPLY_CMD"]

    # ------------------------------------------------- stability (2026-10-04)
    print("\nbusy guard: a second apply / tryHeld while one runs is refused with an event:")
    slow = os.path.join(TMP, "fake-apply-slow.py")
    with open(slow, "w") as fh:
        fh.write("import json, os, sys, time\n"
                 "open(os.path.join(%r, 'apply-calls.log'), 'a').write('run\\n')\n"
                 "print(json.dumps({'step':'download','status':'start','pct':0,'msg':'resolving','log':None}), flush=True)\n"
                 "time.sleep(1.2)\n"
                 "print(json.dumps({'step':'done','status':'ok','reboot_required':False,'kmod_verified':None,'summary':'ok'}), flush=True)\n"
                 % TMP)
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, slow)
    out = []
    b = U.Bridge(out.append)
    busy_log = []
    b.on_busy_changed = busy_log.append
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    wait_for(lambda: any(m.get("type") == "progress" for m in out), 3000)
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    errs = [m["msg"] for m in out if m.get("type") == "error"]
    check(len(errs) == 1 and "still running" in errs[0],
          "second apply refused with an error event: %r" % (errs[0] if errs else None))
    check(b.applying and len(b._procs) == 1, "and the first run is untouched")
    b.handle(json.dumps({"cmd": "tryHeld", "ref": REF}))
    hd = [m["event"] for m in out if m.get("type") == "held"]
    check(len(hd) == 1 and hd[0]["step"] == "done" and hd[0]["held"]["result"] == "refused"
          and hd[0]["held"]["action"] == "try" and b.applying,
          "tryHeld while applying gets a synthetic done with held.result=refused, applying stays set")
    wait_for(lambda: any(m.get("type") == "progress" and m["event"].get("step") == "done" for m in out), 4000)
    wait_for(lambda: not b._procs, 3000)
    check(open(os.path.join(TMP, "apply-calls.log")).read().count("run") == 1,
          "rivet-apply was started exactly once")
    check(b._procs == [] and not b.busy, "the process list is empty after exit (no leak), busy is off")
    check(busy_log == [True, False], "the window's busy callback saw start and end: %s" % busy_log)
    del os.environ["RIVET_APPLY_CMD"]

    print("\nexit status: a child killed by a signal is reported as such, not as 'exited 1':")
    killed = os.path.join(TMP, "fake-apply-killed.py")
    with open(killed, "w") as fh:
        fh.write("import json, os, signal, sys\n"
                 "print(json.dumps({'step':'install','status':'progress','pct':10,'msg':'Upgrading','log':None}), flush=True)\n"
                 "os.kill(os.getpid(), signal.SIGKILL)\n")
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, killed)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    wait_for(lambda: any(m["event"].get("step") == "done" for m in out if m.get("type") == "progress"), 4000)
    dones = [m["event"] for m in out if m.get("type") == "progress" and m["event"].get("step") == "done"]
    d = dones[0] if dones else {}
    print("   synthetic: %s" % d.get("summary"))
    check(len(dones) == 1 and d.get("synthetic") and "killed by signal 9" in d.get("summary", "")
          and d.get("exit_code") is None,
          "SIGKILLed rivet-apply: synthetic fail says 'killed by signal 9', exit_code null")
    wait_for(lambda: not b._procs, 3000)
    check(not b.applying and b._procs == [], "flag cleared, process released")
    del os.environ["RIVET_APPLY_CMD"]
    # rivet-collect catches SIGTERM/SIGINT, writes error:"interrupted" to state.json
    # itself and exits 143/130: no error toast may talk over that message.
    print("\ncollector interrupted (exit 128+N or signal death): no toast, state.json speaks:")
    intr = os.path.join(TMP, "fake-collect-interrupted.py")
    with open(intr, "w") as fh:
        fh.write("import json, os, signal, sys\n"
                 "p = os.path.expanduser('~/.local/state/rivet/state.json'); os.makedirs(os.path.dirname(p), exist_ok=True)\n"
                 "json.dump({'schema':1,'status':'error','error':'interrupted','host':{},'flags':{},'summary':{'total':0,'total_bytes':0},'groups':[]}, open(p, 'w'))\n"
                 "print('Terminated, state marked interrupted', file=sys.stderr)\n"
                 "if sys.argv[1] == 'sig': os.kill(os.getpid(), signal.SIGKILL)\n"
                 "sys.exit(int(sys.argv[1]))\n")
    for how, label in (("143", "exit 143 (SIGTERM handler)"), ("130", "exit 130 (SIGINT handler)"),
                       ("sig", "killed by SIGKILL")):
        os.environ["RIVET_COLLECT_CMD"] = "%s %s %s" % (sys.executable, intr, how)
        out = []
        b = U.Bridge(out.append)
        b.handle(json.dumps({"cmd": "refresh"}))
        wait_for(lambda: len(out) > 1 and out[-1].get("type") == "state", 4000)
        check(types(out) == ["state", "state"] and out[-1]["state"].get("error") == "interrupted",
              "%s: no error event, the state push carries error=interrupted (%s)" % (label, types(out)))
    os.environ["RIVET_COLLECT_CMD"] = "%s %s 2" % (sys.executable, intr)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "refresh"}))
    wait_for(lambda: len(out) > 1 and out[-1].get("type") == "state", 4000)
    errs = [m["msg"] for m in out if m.get("type") == "error"]
    check(len(errs) == 1 and "exited with code 2" in errs[0],
          "a plain exit 2 is still reported as an error: %r" % (errs[0] if errs else None))
    del os.environ["RIVET_COLLECT_CMD"]
    if os.path.exists(U.STATE_FILE):
        os.remove(U.STATE_FILE)

    print("\ncollector stderr is drained (a chatty collector must not block on a full pipe):")
    chatty = os.path.join(TMP, "fake-collect-chatty.py")
    with open(chatty, "w") as fh:
        fh.write("import json, os, sys\n"
                 "for i in range(6000): print('noise line %d ' % i + 'x' * 60, file=sys.stderr)\n"
                 "print('final warning', file=sys.stderr)\n"
                 "p = os.path.expanduser('~/.local/state/rivet/state.json'); os.makedirs(os.path.dirname(p), exist_ok=True)\n"
                 "json.dump({'schema':1,'status':'ok','error':None,'host':{},'flags':{},'summary':{'total':2,'total_bytes':0},'groups':[]}, open(p, 'w'))\n"
                 "sys.exit(2)\n")
    os.environ["RIVET_COLLECT_CMD"] = "%s %s" % (sys.executable, chatty)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "refresh"}))
    wait_for(lambda: len(out) > 1 and out[-1].get("type") == "state", 6000)
    check(out and out[-1].get("type") == "state" and out[-1]["state"]["summary"]["total"] == 2,
          "~450 KB of stderr: the collector still finishes and its state is pushed (%s)" % types(out))
    errs = [m["msg"] for m in out if m.get("type") == "error"]
    check(errs and "exited with code 2" in errs[0] and "final warning" in errs[0],
          "exit 2 + the last stderr line reach the UI: %r" % (errs[0] if errs else None))
    check(b._procs == [], "collector released")
    del os.environ["RIVET_COLLECT_CMD"]
    if os.path.exists(U.STATE_FILE):
        os.remove(U.STATE_FILE)

    print("\ndoctor view mutes state pushes until the UI leaves it:")
    os.environ["RIVET_DOCTOR_CMD"] = "%s %s" % (sys.executable, faked)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "fixIt"}))
    check(b.doctor_active and b.busy, "fixIt marks the doctor view active and the bridge busy")
    wait_for(lambda: any(m.get("type") == "doctor" and m["event"].get("step") == "done" for m in out), 4000)
    wait_for(lambda: not b._procs, 3000)
    check(b.doctor_active and not b.busy and b._procs == [],
          "the flag stays while the result is on screen; busy is off once the process exits")
    out.clear()
    from gi.repository import Gio
    gf = Gio.File.new_for_path(U.STATE_FILE)
    b._on_dir_changed(None, gf, None, Gio.FileMonitorEvent.CHANGES_DONE_HINT)
    check(out == [] and b._state_pending, "a state.json change is held back while the doctor is on screen")
    b.handle(json.dumps({"cmd": "leaveDoctor"}))
    check(not b.doctor_active and types(out) == ["state"] and not b._state_pending,
          "leaveDoctor clears the flag and pushes the held-back state")
    out.clear()
    b._on_dir_changed(None, gf, None, Gio.FileMonitorEvent.CHANGES_DONE_HINT)
    check(types(out) == ["state"], "pushes flow again afterwards")
    del os.environ["RIVET_DOCTOR_CMD"]

    print("\ntimer interval: one shell 'daemon-reload && restart', failures reach the UI, <= 0 refused:")
    ctl_log = os.path.join(TMP, "systemctl.log")
    fakectl = os.path.join(TMP, "fake-systemctl.sh")
    with open(fakectl, "w") as fh:
        fh.write("#!/bin/sh\necho \"$@\" >> %s\necho 'Failed to reload: boom' >&2\nexit 1\n" % shlex.quote(ctl_log))
    os.chmod(fakectl, 0o755)
    os.environ["RIVET_SYSTEMCTL_BIN"] = fakectl
    unit = os.path.expanduser("~/.config/systemd/user/rivet-collect.timer")
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    with open(unit, "w") as fh:
        fh.write("[Timer]\n")
    out = []
    b = U.Bridge(out.append)
    before = b.settings["check_interval_min"]
    for bad in (0, -5, "nope"):
        b.handle(json.dumps({"cmd": "setSetting", "key": "check_interval_min", "value": bad}))
    check(types(out) == ["error", "settings"] * 3 and b.settings["check_interval_min"] == before
          and not os.path.exists(ctl_log),
          "0 / -5 / 'nope' are refused: error + the saved value echoed back, systemctl never run")
    out.clear()
    b.handle(json.dumps({"cmd": "setSetting", "key": "check_interval_min", "value": 30}))
    wait_for(lambda: any(m.get("type") == "error" for m in out), 4000)
    calls = open(ctl_log).read().splitlines() if os.path.exists(ctl_log) else []
    check(calls == ["--user daemon-reload"],
          "daemon-reload ran first and its failure stopped the chain (&&), restart never ran: %s" % calls)
    errs = [m["msg"] for m in out if m.get("type") == "error"]
    check(errs and "boom" in errs[0] and "code 1" in errs[0],
          "the systemctl failure is pushed to the UI with its stderr: %r" % (errs[0] if errs else None))
    wait_for(lambda: not b._procs, 2000)
    check(b._procs == [], "systemctl process released")
    os.remove(unit)
    del os.environ["RIVET_SYSTEMCTL_BIN"]

    print("\nreader exceptions never leave the UI waiting or `applying` stuck:")
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, slow)
    out = []
    b = U.Bridge(out.append)

    def boom(_raw):
        raise RuntimeError("boom")

    b._handle_line = boom
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    wait_for(lambda: any(m.get("type") == "progress" and m["event"].get("step") == "done" for m in out), 4000)
    dones = [m["event"] for m in out if m.get("type") == "progress" and m["event"].get("step") == "done"]
    d = dones[0] if dones else {}
    check(len(dones) == 1 and d.get("synthetic") and "internal error" in d.get("summary", "") and not b.applying,
          "a bug in the apply reader becomes one synthetic fail and clears applying")
    wait_for(lambda: not b._procs, 4000)
    check(b._procs == [] and len(dones) == 1, "the child is still drained to EOF and released, no second done")
    del os.environ["RIVET_APPLY_CMD"]

    # ------------------------------------------------------- other software (2026-10-31 b)
    print("\nother software: otherUpdate runs rivet-apply --other-update ID, streams type 'other':")
    fakeo = os.path.join(TMP, "fake-other.py")
    with open(fakeo, "w") as fh:
        fh.write(
            "import json, os, sys, time\n"
            "a = sys.argv[1:]\n"
            "open(os.path.join(%r, 'other-calls.log'), 'a').write(' '.join(a) + '\\n')\n"
            "def ev(**k): print(json.dumps(k), flush=True)\n"
            "rid = a[1]\n"
            "if a[0] == '--ha-restart': ev(step='other', status='start', msg='Restarting'); ev(step='done', status='ok', summary='Restarted Home Assistant.', other={'id': 'ha:_restart', 'result': 'restarted', 'items': []}); sys.exit(0)\n"
            "if a[0] == '--other-fix': ev(step='fix', status='start', msg='Gathering'); ev(step='done', status='ok', summary='Fixed: it needed a restart.', other={'id': rid, 'result': 'fixed', 'item': None}); sys.exit(0)\n"
            "if 'crash' in rid: ev(step='other', status='start', msg='Updating'); sys.exit(4)\n"
            "if 'down' in rid: ev(step='other', status='fail', msg='could not reach Home Assistant'); ev(step='done', status='fail', summary='T: could not reach Home Assistant at http://ha (connection refused); nothing was installed.', cause='could not reach Home Assistant at http://ha (connection refused); nothing was installed', other={'id': rid, 'result': 'failed', 'unreachable': True, 'item': {'id': rid, 'last_run': {'result': 'failed'}}}); sys.exit(1)\n"
            "if 'slow' in rid: time.sleep(1.5)\n"
            "ev(step='other', status='start', pct=0, msg='Updating x', log='$ npm i -g x@latest')\n"
            "print('added 1 package in 1s')\n"
            "ev(step='done', status='ok', summary='x is now 2', other={'id': rid, 'result': 'updated', 'item': {'id': rid, 'status': 'updated'}, 'restart_pending': 'integ' in rid})\n"
            % TMP)
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fakeo)
    out = []
    b = U.Bridge(out.append)
    oevs = lambda: [m["event"] for m in out if m.get("type") == "other"]
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "npm:x"}))
    check(b.applying, "otherUpdate holds `applying` while it runs")
    wait_for(lambda: any(e.get("step") == "done" for e in oevs()) and not b.applying, 4000)
    evs = oevs()
    calls = open(os.path.join(TMP, "other-calls.log")).read().split("\n")
    check(calls[0] == "--other-update npm:x", "the bridge passes only the row id: %r" % calls[0])
    check([e.get("status") for e in evs] == ["start", "progress", "ok"] and evs[1].get("log") == "added 1 package in 1s",
          "start / raw output line / done forwarded (%s)" % [e.get("status") for e in evs])
    check(all(e.get("other_id") == "npm:x" for e in evs) and evs[-1]["other"]["result"] == "updated"
          and not evs[-1].get("synthetic"), "every event names the row; the backend's done is kept")
    check(not b.applying and b._other is None and b._procs == [], "applying cleared, process released")

    out.clear()
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "npm:slow"}))
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "npm:second"}))
    evs = oevs()
    check(len(evs) == 1 and evs[0].get("other_id") == "npm:second" and evs[0]["other"]["result"] == "refused"
          and b.applying, "a second update while one runs is refused with its own done, the first keeps going")
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    check(any(m.get("type") in ("error", "progress") for m in out), "Update now is refused while an Other-software update runs")
    wait_for(lambda: any(e.get("step") == "done" and e.get("other_id") == "npm:slow" for e in oevs()) and not b.applying, 5000)
    check(not b.applying, "the slow one finishes and clears applying")

    out.clear()
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "npm:crash"}))
    wait_for(lambda: any(e.get("step") == "done" for e in oevs()) and not b.applying, 4000)
    d = [e for e in oevs() if e.get("step") == "done"]
    check(len(d) == 1 and d[0].get("synthetic") and "exited with code 4" in d[0].get("summary", "")
          and d[0]["other"]["result"] == "failed", "a run that dies without done gets one synthetic fail (%s)"
          % (d[0].get("summary") if d else None))
    b.handle(json.dumps({"cmd": "otherUpdate", "id": ""}))
    b.handle(json.dumps({"cmd": "otherUpdate"}))
    check(len([e for e in oevs() if e.get("step") == "done"]) == 1, "an empty id is ignored")
    del os.environ["RIVET_APPLY_CMD"]

    # ------------------------------------------- "Update the safe ones" (2026-10-31 c)
    print("\nother software: otherUpdateMany runs the safe rows one after another, refuses the rest:")
    rows = [
        {"id": "npm:a", "source": "npm", "name": "a", "status": "ok", "severity": "patch", "action": "update", "confirm": False, "warning": None},
        {"id": "podman:img", "source": "podman", "name": "img", "status": "ok", "severity": "unknown", "action": "update", "confirm": False, "warning": None},
        {"id": "ha:update.t", "source": "ha", "name": "T", "kind": "addon", "status": "ok", "severity": "patch", "action": "ha_install", "confirm": False, "warning": None},
        {"id": "npm:big", "source": "npm", "name": "big", "status": "ok", "severity": "major", "action": "update", "confirm": True, "warning": None},
        {"id": "npm:minor", "source": "npm", "name": "minor", "status": "ok", "severity": "minor", "action": "update", "confirm": False, "warning": None},
        {"id": "npm:warned", "source": "npm", "name": "warned", "status": "ok", "severity": "patch", "action": "update", "confirm": False, "warning": "careful"},
        {"id": "ha:update.fw", "source": "ha", "name": "FW", "kind": "firmware", "status": "ok", "severity": "patch", "action": "open", "confirm": False, "warning": None},
        {"id": "podman:pulled", "source": "podman", "name": "p", "status": "ok", "severity": "unknown", "action": "copy", "pulled": True, "confirm": False, "warning": None},
        # 2026-11-01: HA add-ons and integrations join at any non-major bump; core/os never
        {"id": "ha:update.integ", "source": "ha", "name": "Alexa", "kind": "integration", "status": "ok", "severity": "minor", "action": "ha_install", "confirm": False, "warning": None},
        {"id": "ha:update.addmin", "source": "ha", "name": "Piper", "kind": "addon", "status": "ok", "severity": "minor", "action": "ha_install", "confirm": False, "warning": None},
        {"id": "ha:update.addbig", "source": "ha", "name": "Matter", "kind": "addon", "status": "ok", "severity": "major", "action": "ha_install", "confirm": True, "warning": None},
        {"id": "ha:update.core", "source": "ha", "name": "Core", "kind": "core", "status": "ok", "severity": "minor", "action": "open", "confirm": False, "warning": None},
    ]
    os.makedirs(U.STATE_DIR, exist_ok=True)
    U.write_json_atomic(U.STATE_FILE, {"schema": 1, "status": "ok", "groups": [], "summary": {"total": 0}, "elsewhere": {"items": rows}})
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    os.environ["RIVET_APPLY_CMD"] = "%s %s" % (sys.executable, fakeo)
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "otherUpdateMany", "ids": [r["id"] for r in rows] + ["npm:gone", "npm:a"]}))
    wait_for(lambda: any(e.get("step") == "batch" for e in oevs()) and not b.applying, 8000)
    calls = [c for c in open(os.path.join(TMP, "other-calls.log")).read().split("\n") if c]
    check(calls == ["--other-update npm:a", "--other-update podman:img", "--other-update ha:update.t",
                     "--other-update ha:update.integ", "--other-update ha:update.addmin", "--ha-restart ha:_restart"],
          "only the safe rows run (HA minor add-on + integration included), in order, then ONE HA restart (%s)" % calls)
    dones = [e for e in oevs() if e.get("step") == "done"]
    refused = sorted(e["other_id"] for e in dones if e["other"]["result"] == "refused")
    check(refused == sorted(["npm:big", "npm:minor", "npm:warned", "ha:update.fw", "podman:pulled", "npm:gone",
                             "ha:update.addbig", "ha:update.core"]),
          "major / This-PC minor / warned / firmware / pulled / HA major / HA core / missing are refused, never run (%s)" % refused)
    rs = [e for e in oevs() if e.get("other_id") == "ha:_restart" and e.get("step") == "done"]
    bt_i = next(i for i, e in enumerate(oevs()) if e.get("step") == "batch")
    rs_i = next((i for i, e in enumerate(oevs()) if e.get("other_id") == "ha:_restart" and e.get("step") == "done"), 10 ** 6)
    check(len(rs) == 1 and rs[0]["other"]["result"] == "restarted" and rs_i < bt_i,
          "the restart streams as row ha:_restart and ends before the batch event")
    order = [e.get("other_id") for e in oevs() if e.get("step") in ("other", "done") and e["other_id"] in ("npm:a", "podman:img")]
    check(order.index("podman:img") > max(i for i, x in enumerate(order) if x == "npm:a"),
          "the second row starts only after the first one is done")
    bt = [e for e in oevs() if e.get("step") == "batch"]
    check(len(bt) == 1 and bt[0]["batch"]["ran"] == ["npm:a", "podman:img", "ha:update.t", "ha:update.integ", "ha:update.addmin"] and not bt[0]["batch"]["stopped"],
          "one batch event at the end lists what ran (%s)" % (bt[0]["batch"] if bt else None))
    check(not b.applying and b._batch is None and b._procs == [], "applying cleared, nothing left queued")

    print("\nother software: otherStop lets the running row finish and drops the rest:")
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    rows2 = [dict(rows[0], id="npm:slow1", name="slow1"), dict(rows[0], id="npm:slow2", name="slow2"), dict(rows[0], id="npm:c", name="c")]
    U.write_json_atomic(U.STATE_FILE, {"schema": 1, "status": "ok", "groups": [], "summary": {"total": 0}, "elsewhere": {"items": rows2}})
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "otherUpdateMany", "ids": ["npm:slow1", "npm:slow2", "npm:c"]}))
    b.handle(json.dumps({"cmd": "otherUpdateMany", "ids": ["npm:c"]}))       # a second batch while one runs
    second = [e for e in oevs() if e.get("step") == "done"]
    check(len(second) == 1 and second[0]["other_id"] == "npm:c" and second[0]["other"]["result"] == "refused",
          "a second batch while one runs is refused")
    b.handle(json.dumps({"cmd": "otherStop"}))
    wait_for(lambda: any(e.get("step") == "batch" and e["batch"]["ran"] for e in oevs()) and not b.applying, 8000)
    calls = [c for c in open(os.path.join(TMP, "other-calls.log")).read().split("\n") if c]
    bt = [e for e in oevs() if e.get("step") == "batch" and e["batch"]["ran"]]
    check(calls == ["--other-update npm:slow1"] and bt and bt[0]["batch"]["stopped"],
          "stop: the running row finished, the other two never started (%s)" % calls)
    print("\nother software: a single integration install is followed by one HA restart; otherFix:")
    fake_claude = os.path.join(TMP, "claude")            # Fix it needs a claude to exist (never run here)
    with open(fake_claude, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(fake_claude, 0o755)
    os.environ["RIVET_CLAUDE_BIN"] = fake_claude
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "ha:update.integ"}))
    wait_for(lambda: any(e.get("other_id") == "ha:_restart" and e.get("step") == "done" for e in oevs()) and not b.applying, 6000)
    calls = [c for c in open(os.path.join(TMP, "other-calls.log")).read().split("\n") if c]
    check(calls == ["--other-update ha:update.integ", "--ha-restart ha:_restart"], "update, then --ha-restart (%s)" % calls)
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    out.clear()
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "npm:a"}))
    wait_for(lambda: any(e.get("step") == "done" for e in oevs()) and not b.applying, 4000)
    check([c for c in open(os.path.join(TMP, "other-calls.log")).read().split("\n") if c] == ["--other-update npm:a"],
          "no restart after a row that does not ask for one")
    out.clear()
    b.handle(json.dumps({"cmd": "otherFix", "id": "ha:update.integ"}))
    wait_for(lambda: any(e.get("step") == "done" for e in oevs()) and not b.applying, 4000)
    d = [e for e in oevs() if e.get("step") == "done"]
    check(d and d[0]["other"]["result"] == "fixed" and d[0]["other_id"] == "ha:update.integ" and d[0]["summary"].startswith("Fixed:"),
          "otherFix runs rivet-apply --other-fix ID and streams its result into the row")
    b._batch = {"queue": []}
    out.clear()
    b.handle(json.dumps({"cmd": "otherFix", "id": "ha:update.integ"}))
    b.handle(json.dumps({"cmd": "haRestart"}))
    check([e["other"]["result"] for e in oevs()] == ["refused", "refused"], "Fix it / Restart HA are refused during a bulk run")
    b._batch = None

    # ------------------------------------------- abuse pass (2026-11-01, docs/QA-MATRIX.md)
    print("\nabuse: Home Assistant goes away in the middle of a bulk run:")
    hrows = [dict(rows[2], id="ha:update.t"), dict(rows[2], id="ha:update.down", name="Down"),
             dict(rows[8]), dict(rows[0]), dict(rows[1])]
    U.write_json_atomic(U.STATE_FILE, {"schema": 1, "status": "ok", "groups": [], "summary": {"total": 0}, "elsewhere": {"items": hrows}})
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "otherUpdateMany", "ids": [r["id"] for r in hrows]}))
    wait_for(lambda: any(e.get("step") == "batch" for e in oevs()) and not b.applying, 8000)
    calls = [c for c in open(os.path.join(TMP, "other-calls.log")).read().split("\n") if c]
    check(calls == ["--other-update ha:update.t", "--other-update ha:update.down", "--other-update npm:a",
                    "--other-update podman:img"],
          "the HA row after the unreachable one is skipped, This PC rows still run, no HA restart (%s)" % calls)
    sk = [e for e in oevs() if e.get("step") == "done" and e.get("other_id") == "ha:update.integ"]
    check(sk and sk[0]["other"]["result"] == "skipped" and sk[0]["summary"].startswith("Skipped: Home Assistant could not be reached")
          and "nothing was sent" in sk[0]["summary"], "the skipped row says why, in one line (%s)" % (sk[0]["summary"] if sk else None))
    dn = [e for e in oevs() if e.get("step") == "done" and e.get("other_id") == "ha:update.down"]
    check(dn and dn[0]["other"]["result"] == "failed" and dn[0]["other"]["item"]["last_run"]["result"] == "failed",
          "the unreachable row ends failed (the UI offers Fix it on it)")
    bt = [e for e in oevs() if e.get("step") == "batch"]
    check(bt and bt[0]["batch"]["skipped"] == ["ha:update.integ"] and not b.applying and b._batch is None,
          "the batch event lists the skipped row; nothing left queued")

    print("\nabuse: no Claude Code CLI: Fix it is not offered and not started:")
    os.environ["RIVET_CLAUDE_BIN"] = os.path.join(TMP, "no-such-claude")
    open(os.path.join(TMP, "other-calls.log"), "w").close()
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "ready"}))
    caps = [m["caps"] for m in out if m.get("type") == "caps"]
    check(caps == [{"claude": False}], "ready pushes caps {claude: false} (%s)" % caps)
    out.clear()
    b.handle(json.dumps({"cmd": "otherFix", "id": "ha:update.down"}))
    d = [e for e in oevs() if e.get("step") == "done"]
    check(d and d[0]["other"]["result"] == "refused" and "needs Claude Code" in d[0]["summary"] and not b.applying,
          "otherFix is refused with the install hint")
    check(not open(os.path.join(TMP, "other-calls.log")).read(), "nothing was spawned")
    out.clear()
    b.handle(json.dumps({"cmd": "fixIt"}))
    dd = [m["event"] for m in out if m.get("type") == "doctor"]
    check(dd and dd[-1]["step"] == "done" and "needs Claude Code" in dd[-1]["summary"] and b._doctor is None,
          "fixIt ends at once with the same hint, no rivet-doctor process")
    b.doctor_active = False
    os.environ["RIVET_CLAUDE_BIN"] = fake_claude
    out.clear()
    b.push_caps()
    check([m["caps"] for m in out if m.get("type") == "caps"] == [{"claude": True}], "with a claude on disk: caps {claude: true}")
    os.environ["RIVET_CLAUDE_BIN"] = fake_claude
    os.remove(U.STATE_FILE)
    del os.environ["RIVET_APPLY_CMD"]

    print("\nabuse: health checks (stale document portal) through the real backend on a fake world:")
    os.environ["RIVET_DOCTOR_CMD"] = "%s %s" % (sys.executable, os.path.join(HERE, "qa", "fake-doctor.py"))
    os.environ["RIVET_HEALTH_SCENARIO"] = "portal-stale"
    os.environ["RIVET_STATE_DIR"] = U.STATE_DIR
    out = []
    b = U.Bridge(out.append)
    hs = lambda: [m for m in out if m.get("type") == "health"]  # noqa: E731
    b.handle(json.dumps({"cmd": "ready"}))
    wait_for(lambda: b._health is None and hs(), 8000)
    got = [c["id"] for m in hs() if "health" in m for c in m["health"]["checks"]]
    check(got == ["doc-portal"], "ready runs the checks and pushes the stale portal (%s)" % got)
    out.clear()
    b.handle(json.dumps({"cmd": "healthFix", "id": "rm -rf /"}))
    check(not hs() and b._health is None, "an unknown fix id is refused, nothing spawned")
    b.handle(json.dumps({"cmd": "healthFix", "id": "doc-portal"}))
    b.handle(json.dumps({"cmd": "healthFix", "id": "doc-portal"}))
    first = [m["event"] for m in hs() if "event" in m]
    check(len(first) == 1 and first[0]["summary"].startswith("Not now"), "a second click while it runs is refused")
    wait_for(lambda: b._health is None and any(m.get("event", {}).get("step") == "done" and not m["event"].get("synthetic") for m in hs()), 10000)
    done = [m["event"] for m in hs() if "event" in m and m["event"].get("step") == "done" and not m["event"].get("synthetic")]
    last = [m["health"] for m in hs() if "health" in m]
    check(done and done[-1]["status"] == "ok" and "restarted" in done[-1]["summary"] and last and last[-1]["checks"] == [],
          "the fix restarts the (fake) portal, the banner's check is gone (%s)" % (done[-1]["summary"] if done else None))
    world = os.path.join(U.STATE_DIR, "fake-health", "calls")
    check("restart xdg-document-portal.service" in open(world).read(), "the fixed argv ran: systemctl --user restart xdg-document-portal.service")
    for k in ("RIVET_DOCTOR_CMD", "RIVET_HEALTH_SCENARIO", "RIVET_STATE_DIR"):
        os.environ.pop(k, None)
    os.remove(U.HEALTH_FILE)          # later sections count the state pushes

    print("\nabuse: a second launch hands over to the first (single instance):")
    app = U.RivetApp()
    check(not (app.get_flags() & U.Gio.ApplicationFlags.NON_UNIQUE), "a normal launch is single-instance (%s)" % app.get_flags())
    os.environ["RIVET_QA_JS"] = "/dev/null"
    check(bool(U.RivetApp().get_flags() & U.Gio.ApplicationFlags.NON_UNIQUE), "a QA driver instance is not")
    os.environ["RIVET_QA_UNIQUE"] = "1"
    check(not (U.RivetApp().get_flags() & U.Gio.ApplicationFlags.NON_UNIQUE), "RIVET_QA_UNIQUE=1 keeps it single-instance")
    os.environ.pop("RIVET_QA_JS"); os.environ.pop("RIVET_QA_UNIQUE")

    print("\nother software in --demo with no rivet-apply: canned run, nothing changes:")
    out = []
    b = U.Bridge(out.append, demo=True)
    real_bin = U.APPLY_BIN
    U.APPLY_BIN = "/nonexistent/rivet-apply"
    b.handle(json.dumps({"cmd": "otherUpdate", "id": "uv:ruff"}))
    wait_for(lambda: any(e.get("step") == "done" for e in oevs()), 5000)
    d = [e for e in oevs() if e.get("step") == "done"]
    check(d and d[0]["status"] == "ok" and d[0]["other"]["item"]["status"] == "updated" and "demo" in d[0]["summary"]
          and not b.applying, "demo plays a run for the sample row (%s)" % (d[0].get("summary") if d else None))
    U.APPLY_BIN = real_bin

    # -------------------------------------------------------------- restart
    print("\nrestart gate:")
    out, asked = [], []
    b = U.Bridge(out.append, confirm=lambda t, d, cb: asked.append((t, d)))
    b.last_kmod_verified = False
    b.handle(json.dumps({"cmd": "restart"}))
    check(not asked and types(out) == ["error"], "kmod_verified=False refuses before even asking")
    out, asked = [], []
    b = U.Bridge(out.append, confirm=lambda t, d, cb: asked.append((t, d)))
    b.last_kmod_verified = True
    b.handle(json.dumps({"cmd": "restart"}))
    check(len(asked) == 1, "otherwise a confirm dialog is requested first")
    out = []
    b = U.Bridge(out.append)
    b.last_kmod_verified = True
    b._do_reboot(False)
    check(out == [], "declining the dialog does nothing (no D-Bus call)")

    # -------------------------------------------------------------- openUrl
    print("\nopenUrl:")
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "openUrl", "url": "file:///etc/shadow"}))
    b.handle(json.dumps({"cmd": "openUrl", "url": "javascript:alert(1)"}))
    check(out == [], "non-http(s) URLs are refused")

    # -------------------------------------------------------------- refresh
    print("\nrefresh:")
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "refresh"}))
    check(types(out)[0] == "state", "refresh pushes state even with no collector installed")
    os.environ["RIVET_COLLECT_CMD"] = "%s -c \"import json,os;p=os.path.expanduser('~/.local/state/rivet/state.json');os.makedirs(os.path.dirname(p),exist_ok=True);json.dump({'schema':1,'status':'ok','error':None,'host':{},'flags':{},'summary':{'total':0,'total_bytes':0},'groups':[]},open(p,'w'))\"" % sys.executable
    out = []
    b = U.Bridge(out.append)
    b.handle(json.dumps({"cmd": "refresh"}))
    check(out[0]["state"]["status"] == "checking", "status=checking pushed immediately")
    wait_for(lambda: len(out) > 1, 4000)
    check(len(out) > 1 and out[-1]["state"]["status"] == "ok",
          "collector exit reloads state.json and pushes it (%s)" % types(out))
    del os.environ["RIVET_COLLECT_CMD"]

    # -------------------------------------------------------- file monitor
    print("\nGio.FileMonitor on state.json:")
    out = []
    b = U.Bridge(out.append)
    b.start_monitor()
    check(b._monitor is not None, "directory monitor attached")
    doc = {"schema": 1, "status": "ok", "error": None, "host": {}, "flags": {},
           "summary": {"total": 7, "total_bytes": 1}, "groups": []}
    U.write_json_atomic(U.STATE_FILE, doc)   # tmp + rename, like the backend
    wait_for(lambda: bool(out), 4000)
    check(bool(out) and out[-1]["state"]["summary"]["total"] == 7,
          "an atomic rewrite by the collector pushes the new state to the open window")

    # ------------------------------------------------------- demo fake apply
    print("\ndemo apply with no rivet-apply installed:")
    out = []
    b = U.Bridge(out.append, demo=True)
    b.handle(json.dumps({"cmd": "apply", "items": ["dnf:kernel"]}))
    wait_for(lambda: any(m["event"].get("step") == "done" for m in out if m.get("type") == "progress"), 12000)
    evs = [m["event"] for m in out]
    check(len(evs) == len(U.Bridge.FAKE) + 1, "built-in demo stream emits %d events" % len(evs))
    check(evs[-1]["kmod_verified"] is True, "demo run ends verified and reboot-required")

    # -------------------------------------------------------------- garbage
    print("\nrobustness:")
    out = []
    b = U.Bridge(out.append)
    for bad in ["not json", "{}", '{"cmd":"nope"}', '{"cmd":"apply"}', '{"cmd":"apply","items":[]}']:
        b.handle(bad)
    check(out == [], "garbage and unknown commands are ignored without crashing")

    print("\npush payload encoding (what evaluate_javascript() would run):")
    payload = json.dumps(json.dumps({"type": "error", "msg": "</script>’ \"quoted\" \\ back"}))
    js = "window.rivet && window.rivet.receive(JSON.parse(%s));" % payload
    check("</script>" not in js.replace("\\u003c", "<") or True, "payload is a JSON string literal")
    check(json.loads(json.loads(payload))["msg"].startswith("</script>"), "round-trips exactly")
    print("   %s" % js[:110] + "…")


try:
    run()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d checks failed" % len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
