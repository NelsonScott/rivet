"""Fake flatpak / pgrep / journalctl / sudo world for held-back refs (tests and app QA).

    w = World()                       # scratch dir, gamescope 3.16.28 masked, 3.16.30 on the remote
    w.collect(); w.apply("--try-held", REF)
    World(root=<dir>, state_dir=<dir>)  # re-attach to an existing world (app/qa/fake-apply.py)

Runs the REAL CLIs as subprocesses with the fakes first on PATH. The fake flatpak keeps the two
behaviours that matter: `update` skips masked refs, and `update --commit=` on a system install is
refused without root. Nothing here can reach the real flatpak: World asserts that `flatpak`
resolves to the fake before it runs anything.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

REF = "runtime/org.freedesktop.Platform.VulkanLayer.gamescope/x86_64/25.08"
BARE = REF.split("/", 1)[1]
MASK = "org.freedesktop.Platform.VulkanLayer.gamescope//25.08"
GOOD = "be130657227c538a7c5726d7dcd87fa78d309d2247a1558306b9ca0b129a970e"
C29 = "29" * 32
C30 = "30" * 32
C31 = "31" * 32
OBSIDIAN = "app/md.obsidian.Obsidian/x86_64/stable"

FAKE_FLATPAK = r'''#!/usr/bin/env python3
import fnmatch, json, os, sys
P = os.environ["FAKE_WORLD"]
w = json.load(open(P))
a = sys.argv[1:]
# append-only command log: the collector calls flatpak from several threads at once, so
# read-only commands must never rewrite world.json
with open(P + ".cmds", "a") as fh:
    fh.write(json.dumps({"argv": a, "root": os.environ.get("FAKE_ROOT") == "1"}) + "\n")
scope = "user" if "--user" in a else "system"
pos = [x for x in a[1:] if not x.startswith("-")]
def save():
    json.dump(w, open(P + ".tmp%d" % os.getpid(), "w")); os.replace(P + ".tmp%d" % os.getpid(), P)
def parts(r):
    p = r.split("/")
    if p[0] in ("app", "runtime"): p = p[1:]
    return (p + ["", "", ""])[:3]
def masked(ref):
    return any(all(fnmatch.fnmatchcase(v, m or "*") for v, m in zip(parts(ref), parts(mk)))
               for mk in w["masks"].get(scope, []))
inst = w["installed"].get(scope, {})
if a[0] == "list":
    for bare, i in inst.items():
        print("\t".join([bare, i["version"], i["commit"][:12], i["kind"] if i["kind"] == "runtime" else "current", i["name"]]))
elif a[0] == "remote-ls":
    for ref, r in w["remote"].items():
        i = inst.get(ref.split("/", 1)[1])
        if i and i["commit"] != r["commit"]:
            print("\t".join([ref, "flathub", "36.0 MB", i["name"], r["version"]]))
elif a[0] == "mask":
    ms = w["masks"].setdefault(scope, [])
    if not pos:
        print("\n".join("  " + m for m in ms) if ms else "No masked patterns")
    elif w.get("mask_rc"):
        sys.exit(w["mask_rc"])
    elif "--remove" in a:
        if pos[0] in ms: ms.remove(pos[0])
        save()
    else:
        if pos[0] not in ms: ms.append(pos[0])
        save()
elif a[0] == "info":
    i = inst.get(pos[0].split("/", 1)[1]) if pos else None
    if not i: sys.exit(1)
    print("      Version: %s\n       Commit: %s" % (i["version"], i["commit"]))
elif a[0] == "update":
    ref = pos[0]
    commit = next((x.split("=", 1)[1] for x in a if x.startswith("--commit=")), None)
    if w.get("update_rc"):
        print("error: simulated failure"); sys.exit(w["update_rc"])
    if commit:
        if scope == "system" and os.environ.get("FAKE_ROOT") != "1":
            print("error: Can't update to a specific commit without root permissions"); sys.exit(1)
        inst[ref.split("/", 1)[1]].update(commit=commit, version=w["commits"][commit])
    elif masked(ref):
        print("Nothing to do.")
    else:
        r = w["remote"][ref]
        inst[ref.split("/", 1)[1]].update(commit=r["commit"], version=r["version"])
        print("Updating… 100%")
    save()
elif a[0] == "ps":
    print("com.valvesoftware.Steam" if w.get("steam") else "")
elif a[0] == "remote-info":
    sys.exit(1)
else:
    sys.exit("fake flatpak: refusing " + " ".join(a))
'''
FAKE_SUDO = r'''#!/usr/bin/env python3
import json, os, sys
w = json.load(open(os.environ["FAKE_WORLD"]))
a = [x for x in sys.argv[1:] if x != "-n"]
if os.path.basename(sys.argv[0]) == "sudo" and not w.get("sudo_ok", True):
    sys.exit(1)
if a == ["true"]:
    sys.exit(0)
os.environ["FAKE_ROOT"] = "1"
os.execvp(a[0], a)
'''
FAKE_PGREP = r'''#!/usr/bin/env python3
import json, os, sys, time
w = json.load(open(os.environ["FAKE_WORLD"]))
t = time.time() - w.get("t0", 0)
sys.exit(0 if any(s <= t < e for s, e in w.get("procs", {}).get(sys.argv[-1], [])) else 1)
'''
FAKE_JOURNALCTL = r'''#!/usr/bin/env python3
import json, os, sys, time
w = json.load(open(os.environ["FAKE_WORLD"]))
start = time.time()
for delay, line in w.get("journal", []):
    time.sleep(max(0, delay - (time.time() - start)))
    print(line, flush=True)
time.sleep(60)
'''
FAKE_EMPTY = "#!/bin/sh\nexit 0\n"


# An example hold: gamescope pinned at a good build after the next one broke a game.
EXAMPLE_HOLD = {
    "scope": "system",
    "mask": MASK,
    "display_name": "gamescope",
    "reason": "3.16.29 broke game launches ('Failed to get Xwayland server id'); 3.16.28 works.",
    "held_since": "2026-09-21",
    "good": {"version": "3.16.28", "commit": GOOD},
    "bad_versions": ["3.16.29"],
    "verify": {"type": "steam-game", "appid": 480, "label": "the game",
               "process": "Game.exe", "alive_seconds": 75,
               "fail_patterns": ["Failed to get Xwayland server id",
                                 "Primary child shut down", "gamescope.*SIGABRT"]},
    "history": [{"date": "2026-09-21", "event": "held", "note": "rolled back 3.16.29 -> 3.16.28 by hand"}],
}


def hold_entry(**over):
    h = json.loads(json.dumps(EXAMPLE_HOLD))
    h["verify"].update(alive_seconds=1.0, quick_exit_seconds=0.6)
    h.update(over)
    return h


class World:
    def __init__(self, *, registry=True, masks=(MASK,), remote_version="3.16.30", remote_commit=C30,
                 root=None, state_dir=None, verify=None):
        self.tmp = Path(root) if root else Path(tempfile.mkdtemp(prefix="rivet-holds-"))
        self.bin = self.tmp / "bin"
        fresh = not (self.tmp / "world.json").exists()
        self.bin.mkdir(parents=True, exist_ok=True)
        for name, body in (("flatpak", FAKE_FLATPAK), ("sudo", FAKE_SUDO), ("pkexec", FAKE_SUDO),
                           ("pgrep", FAKE_PGREP), ("journalctl", FAKE_JOURNALCTL),
                           ("rpm", "#!/bin/sh\nexit 1\n"), ("dnf5", FAKE_EMPTY),
                           ("fwupdmgr", FAKE_EMPTY), ("mokutil", FAKE_EMPTY)):
            f = self.bin / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.path = self.tmp / "world.json"
        self.state_dir = Path(state_dir) if state_dir else self.tmp / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.tmp / "ops.json").write_text('{"ops": []}')
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", FAKE_WORLD=str(self.path),
                        RIVET_STATE_DIR=str(self.state_dir), RIVET_CONFIG_DIR=str(self.tmp / "cfg"),
                        RIVET_FLATPAK_DRYRUN_FIXTURE=str(self.tmp / "ops.json"),
                        RIVET_NO_NOTIFY="1", RIVET_HELD_POLL_S="0.05", RIVET_NO_ELSEWHERE="1")
        assert shutil.which("flatpak", path=self.env["PATH"]) == str(self.bin / "flatpak")
        if not fresh:
            return
        self.write({
            "installed": {"system": {
                BARE: {"version": "3.16.28", "commit": GOOD, "kind": "runtime", "name": "gamescope"},
                OBSIDIAN.split("/", 1)[1]: {"version": "1.9.14", "commit": "aa" * 32, "kind": "app",
                                            "name": "Obsidian"}}},
            "remote": {REF: {"version": remote_version, "commit": remote_commit},
                       OBSIDIAN: {"version": "1.9.15", "commit": "bb" * 32}},
            "commits": {GOOD: "3.16.28", C29: "3.16.29", C30: "3.16.30", C31: "3.16.31"},
            "masks": {"system": list(masks)}, "steam": False, "procs": {}, "journal": []})
        if registry:
            self.save_holds({"version": 1, "holds": {REF: hold_entry(**({"verify": verify} if verify else {}))}})

    # -- files
    def read(self):
        return json.loads(self.path.read_text())

    def write(self, w):
        tmp = self.path.with_suffix(".tmp-test")
        tmp.write_text(json.dumps(w))
        os.replace(tmp, self.path)

    def set(self, **kw):
        w = self.read()
        w.update(kw)
        self.write(w)

    def holds(self):
        return json.loads((self.state_dir / "holds.json").read_text())

    def save_holds(self, doc):
        (self.state_dir / "holds.json").write_text(json.dumps(doc))

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def masks(self):
        return self.read()["masks"]["system"]

    def installed(self):
        return self.read()["installed"]["system"][BARE]

    def flatpak_cmds(self, verb):
        log = Path(str(self.path) + ".cmds")
        cmds = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return [c for c in cmds if c["argv"][0] == verb]

    # -- CLIs
    def collect(self):
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--no-llm", "--quiet"],
                            env=self.env, capture_output=True, text=True, timeout=120)
        assert cp.returncode == 0, cp.stderr
        return self.state()

    def apply(self, *args):
        self.set(t0=time.time())
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_apply.py"), *args],
                            env=self.env, capture_output=True, text=True, timeout=60)
        evs = [json.loads(l) for l in cp.stdout.splitlines() if l.strip()]
        assert evs and evs[-1]["step"] == "done", cp.stdout + cp.stderr
        return cp.returncode, evs, evs[-1]
