#!/usr/bin/env python3
"""rivet-collect vs. Flatpak work that `remote-ls --updates` cannot see.

    python3 backend/tests/test_collect_flatpak.py

2026-09-21: dnf moved the NVIDIA driver 610.57.04 -> 615.71.09, reboot, Flatpak then
wanted org.freedesktop.Platform.GL.nvidia-615-71-09 (an INSTALL, not an update).
Rivet said "all up to date", GNOME Software listed four runtimes, a game would not
start. Everything here runs against fake `flatpak`, `rpm`, `dnf5`, `fwupdmgr` on PATH
and a canned libflatpak dry run: the real system is never asked, nothing is installed.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
GL = "runtime/org.freedesktop.Platform.GL.nvidia-615-71-09/x86_64/1.4"
GL32 = "runtime/org.freedesktop.Platform.GL32.nvidia-615-71-09/x86_64/1.4"

FAKE_FLATPAK = r'''#!/usr/bin/env python3
import json, os, sys
w = json.load(open(os.environ["FAKE_WORLD"]))
a = sys.argv[1:]
scope = "user" if "--user" in a else "system"
if a[0] == "list":
    print("\n".join(w["installed"].get(scope, [])))
elif a[0] == "remote-ls":
    print("\n".join(w.get("updates", {}).get(scope, [])))
elif a[0] == "remote-info":
    sys.exit(0 if a[-1] in w["flathub"] else 1)
elif a[0] == "ps":
    print("com.valvesoftware.Steam" if w.get("steam") else "")
else:
    sys.exit("fake flatpak: refusing " + " ".join(a))   # install/update/uninstall: never
'''
FAKE_RPM = r'''#!/usr/bin/env python3
import json, os, sys
w = json.load(open(os.environ["FAKE_WORLD"]))
v = w.get("rpm", {}).get(sys.argv[-1])
print(v if v else "package %s is not installed" % sys.argv[-1])
sys.exit(0 if v else 1)
'''
FAKE_EMPTY = "#!/bin/sh\nexit 0\n"
OLD = ["org.freedesktop.Platform.GL.nvidia-610-57-04/x86_64/1.4",
       "org.freedesktop.Platform.GL32.nvidia-610-57-04/x86_64/1.4",
       "org.freedesktop.Platform/x86_64/25.08", "com.valvesoftware.Steam/x86_64/stable"]
NEW = [GL.split("/", 1)[1], GL32.split("/", 1)[1]]


def collect(world: dict, ops: list[dict]) -> tuple[dict, str]:
    tmp = Path(tempfile.mkdtemp(prefix="rivet-fp-"))
    bindir = tmp / "bin"
    bindir.mkdir()
    for name, body in (("flatpak", FAKE_FLATPAK), ("rpm", FAKE_RPM), ("dnf5", FAKE_EMPTY),
                       ("fwupdmgr", FAKE_EMPTY), ("mokutil", FAKE_EMPTY)):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    (tmp / "world.json").write_text(json.dumps(world))
    (tmp / "ops.json").write_text(json.dumps({"ops": ops}))
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin", FAKE_WORLD=str(tmp / "world.json"),
               RIVET_FLATPAK_DRYRUN_FIXTURE=str(tmp / "ops.json"),
               RIVET_STATE_DIR=str(tmp / "state"), RIVET_CONFIG_DIR=str(tmp / "config"),
               RIVET_NO_NOTIFY="1", RIVET_NO_ELSEWHERE="1")
    cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--no-llm", "--quiet"],
                        env=env, capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    state = json.loads((tmp / "state" / "state.json").read_text())
    return state, (tmp / "state" / "logs" / "collect.log").read_text()


def fp_items(state):
    return next(g for g in state["groups"] if g["id"] == "flatpak")["items"]


def op(kind, ref, size=0):
    return {"scope": "system", "op": kind, "ref": ref, "remote": "flathub",
            "download_bytes": size, "installed_bytes": 0}


class CollectFlatpakTest(unittest.TestCase):
    def test_after_the_reboot_flatpak_wants_the_new_runtime(self):
        """This morning exactly: remote-ls empty, libflatpak wants 2 installs + 2 removals."""
        world = {"installed": {"system": OLD}, "flathub": [GL, GL32], "steam": True,
                 "rpm": {"xorg-x11-drv-nvidia": "615.71.09"}}
        ops = [op("install", GL, 343997577), op("install", GL32, 244844418),
               op("uninstall", "runtime/org.freedesktop.Platform.GL.nvidia-610-57-04/x86_64/1.4"),
               op("uninstall", "runtime/org.freedesktop.Platform.GL32.nvidia-610-57-04/x86_64/1.4")]
        state, _ = collect(world, ops)
        items = fp_items(state)
        self.assertEqual(sorted(i["ref"] for i in items), [GL, GL32])     # no dupes, no removals
        self.assertEqual(state["summary"]["total"], 2)
        gl = next(i for i in items if i["ref"] == GL)
        self.assertEqual((gl["op"], gl["scope"], gl["origin"]), ("install", "system", "flathub"))
        self.assertEqual((gl["from"], gl["to"], gl["size_bytes"]), ("610.57.04", "615.71.09", 343997577))
        self.assertEqual(gl["display_name"], "NVIDIA driver 615.71.09 for Flatpak apps")
        self.assertIn("(32-bit)", next(i for i in items if i["ref"] == GL32)["display_name"])
        # Steam is running; adding a runtime must not be held back by that
        self.assertIsNone(gl["blocked_by"])
        self.assertTrue(gl["selected_default"])

    def test_before_the_reboot_only_the_rpm_knows(self):
        """Driver rpm already 615, old module still loaded: libflatpak sees nothing to do."""
        world = {"installed": {"system": OLD}, "flathub": [GL, GL32],
                 "rpm": {"xorg-x11-drv-nvidia": "615.71.09"}}
        state, _ = collect(world, [])
        self.assertEqual(sorted(i["ref"] for i in fp_items(state)), [GL, GL32])

    def test_not_on_flathub_yet_is_logged_not_listed(self):
        world = {"installed": {"system": OLD}, "flathub": [],
                 "rpm": {"xorg-x11-drv-nvidia": "615.71.09"}}
        state, log = collect(world, [])
        self.assertEqual(fp_items(state), [])
        self.assertIn("is not on Flathub yet", log)

    def test_matching_runtime_present_means_nothing_pending(self):
        world = {"installed": {"system": OLD + NEW}, "flathub": [GL, GL32],
                 "rpm": {"xorg-x11-drv-nvidia": "615.71.09"}}
        state, _ = collect(world, [op("uninstall", "runtime/" + OLD[0])])
        self.assertEqual(state["summary"]["total"], 0)

    def test_no_nvidia_runtime_installed_means_none_is_pushed(self):
        world = {"installed": {"system": ["org.freedesktop.Platform/x86_64/25.08"]},
                 "flathub": [GL, GL32], "rpm": {"xorg-x11-drv-nvidia": "615.71.09"}}
        state, _ = collect(world, [])
        self.assertEqual(fp_items(state), [])

    def test_gl_only_installation_does_not_get_gl32(self):
        world = {"installed": {"user": [OLD[0]]}, "flathub": [GL, GL32],
                 "rpm": {"akmod-nvidia": "615.71.09"}}
        state, _ = collect(world, [])
        items = fp_items(state)
        self.assertEqual([(i["ref"], i["scope"]) for i in items], [(GL, "user")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
