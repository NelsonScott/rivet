"""A scripted stand-in for dnf5daemon + the rpmdb, for tests and UI QA.

    world = FakeWorld.from_items(dnf_items)       # installed = `from`, available = `to`
    world.install(rivet_apply)                    # patches make_daemon, rpmdb_snapshot, ...
    world.fail_tx = {0}                            # transaction #0 reports an error
    world.drop = {"curl"}                          # ...and these never land

Nothing here talks to D-Bus, rpm, journalctl or polkit. Shared by
backend/tests/test_apply.py and app/qa/fake-apply.py (warn / fail scenarios).
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from pathlib import Path

# Fake `sudo`, `pkexec` and `akmods` for the 2026-10-04 lost-rpm-lock retry. All live in a
# bin dir that FakeWorld.enable_fake_akmods() PREPENDS to PATH, so the root-prefixed
# `akmods --force ...` from rivet_apply never reaches the real ones. State goes through files in the fake
# akmods cache (RIVET_AKMOD_CACHE), the only channel a subprocess has to the world.
FAKE_SUDO = r"""#!/bin/sh
# fake sudo / pkexec (tests): never escalates. Records the call under its own name, then
# runs the command as-is. FAKE_SUDO_MODE=refuse -> "a password is required" (exit 1);
# FAKE_PKEXEC_MODE=dismiss -> exit 126 (dialog closed), deny -> exit 127 (not authorized).
me=$(basename "$0")
cache="${RIVET_AKMOD_CACHE:?fake $me needs RIVET_AKMOD_CACHE}"
printf '%s\n' "$*" >> "$cache/$me.calls"
if [ "$me" = sudo ] && [ "${FAKE_SUDO_MODE:-ok}" = "refuse" ]; then
    echo "sudo: a password is required" >&2
    exit 1
fi
if [ "$me" = pkexec ]; then
    case "${FAKE_PKEXEC_MODE:-ok}" in
        dismiss) exit 126 ;;
        deny) echo "Error executing command as another user: Not authorized" >&2; exit 127 ;;
    esac
fi
while [ $# -gt 0 ]; do
    case "$1" in
        -n|-E|-k|-H) shift ;;
        -u) shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done
exec "$@"
"""

FAKE_AKMODS = r"""#!/bin/sh
# fake akmods (tests): records argv; "installs" the kmod (a marker file) or fails.
cache="${RIVET_AKMOD_CACHE:?fake akmods needs RIVET_AKMOD_CACHE}"
printf '%s\n' "$*" >> "$cache/akmods.calls"
kver=""
while [ $# -gt 0 ]; do
    case "$1" in --kernels) kver="$2"; shift ;; esac
    shift
done
echo "Checking kmods exist for $kver"
case "${FAKE_AKMODS_MODE:-ok}" in
    ok)
        echo "Building and installing nvidia-kmod"
        rm -f "$cache"/*-for-"$kver".failed.log
        : > "$cache/installed-kmod-nvidia-$kver"
        exit 0 ;;
    *)
        echo "Building rpms failed; see $cache/fake-for-$kver.failed.log for details"
        exit 2 ;;
esac
"""


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _split(evr: str):
    epoch, _, vr = evr.partition(":") if ":" in evr else ("0", "", evr)
    ver, _, rel = vr.rpartition("-")
    return epoch, ver, rel


def _norm(evr: str) -> str:
    return evr if re.match(r"^\d+:", evr) else "0:" + evr


class FakeWorld:
    def __init__(self):
        self.installed: dict[str, set[tuple[str, str]]] = {}
        self.available: dict[str, tuple[str, str]] = {}
        self.transactions: list[list[str]] = []   # specs of every do_transaction, in order
        self.fail_tx: set[int] = set()            # indices that report failure
        self.raise_tx: dict[int, str] = {}        # index -> D-Bus error text
        self.drop: set[str] = set()               # names a failing transaction does not install
        self.script_errors: dict[int, list[str]] = {}
        self.journal = {"error": None, "self_restart": False}
        self.check: list[str] = []
        self.pace = 0.0
        # Stale metadata (2026-09-22): names the daemon does not offer until its cache
        # has been expired `stale_expires` times (0 = never, like a build the mirrors
        # do not have yet). `expires` counts expire_cache() calls.
        self.stale: set[str] = set()
        self.stale_expires = 1
        self.expires = 0
        self.expire_ok = True
        # Flatpak side (2026-09-21): the NVIDIA runtimes an installation lacks, whether
        # Flathub has them, and every flatpak command apply tried to run.
        self.flatpak_missing: list[dict] = []
        self.flathub_has = True
        self.flatpak_rc = 0
        self.flatpak_cmds: list[list[str]] = []
        # akmods side (2026-10-04): a private cache dir stands in for
        # /var/cache/akmods/nvidia. `akmod_build_polls` = None: kmod-nvidia-<kver> counts
        # as installed at once (the old default). N: it lands on the N-th
        # kmod_pkg_installed() poll, as if akmods were building in the background; a huge
        # N = never on its own, only the fake `akmods` (marker file) can install it.
        self.akmod_cache = Path(tempfile.mkdtemp(prefix="rivet-akmods-"))
        self.akmod_build_polls: int | None = None
        self.akmod_polls = 0
        self.kmod_at_tx: list[bool] = []       # was the kmod installed when each tx started?

    @classmethod
    def from_items(cls, items):
        w = cls()
        for it in items:
            arch = (it.get("ref") or "x.x86_64").rsplit(".", 1)[-1]
            w.installed.setdefault(it["name"], set()).add((_norm(it["from"]), arch))
            w.available[it["name"]] = (_norm(it["to"]), arch)
        return w

    # -- what rivet_apply sees
    def snapshot(self):
        return {k: set(v) for k, v in self.installed.items()}

    def install(self, mod):
        mod.make_daemon = lambda on_event: FakeDnf(self, on_event)
        mod.rpmdb_snapshot = self.snapshot
        mod.journal_scan = lambda since: dict(self.journal)
        mod.dnf_check = lambda: list(self.check)
        mod.akmod_installed = lambda: "akmod-nvidia" in self.installed
        mod.kmod_pkg_installed = self.kmod_pkg_installed
        mod.modinfo_ok = lambda kver: True
        mod.newest_installed_kernel = lambda: "7.2.5-200.fc44.x86_64"
        mod.nv_missing = lambda version: [dict(g) for g in self.flatpak_missing]
        mod.nv_remote_has = lambda scope, ref: self.flathub_has
        mod.run_flatpak = self._run_flatpak
        mod.AKMOD_CACHE = self.akmod_cache                 # never the real cache dir
        os.environ["RIVET_AKMOD_CACHE"] = str(self.akmod_cache)   # the fake binaries' view

    def _run_flatpak(self, cmd, em):
        self.flatpak_cmds.append(list(cmd))
        return self.flatpak_rc

    # -- akmods
    def kmod_marker(self, kver) -> Path:
        return self.akmod_cache / f"installed-kmod-nvidia-{kver}"

    def kmod_pkg_installed(self, kver, evr=None) -> bool:
        if self.kmod_marker(kver).exists():
            return True
        if self.akmod_build_polls is None:
            return True
        self.akmod_polls += 1
        if self.akmod_polls >= self.akmod_build_polls:
            self.kmod_marker(kver).touch()
            return True
        return False

    def seed_akmod_failure(self, kver, evr="615.71.09-3", *, lock_lost: bool, tail=None):
        """What /var/cache/akmods/nvidia looked like on 2026-10-04: a .failed.log for kver
        and, when the lock was lost, the RPM akmods had built just before."""
        self.akmod_build_polls = 10**9                     # never lands on its own
        log = self.akmod_cache / f"{evr}-for-{kver}.failed.log"
        lines = ["2026/10/04 01:40:12 akmods: Building RPM using the command",
                 f"'/sbin/akmodsbuild --target x86_64 --kernels {kver} /usr/src/akmods/nvidia-kmod.latest'"]
        if tail is None:
            tail = ("error: can't create transaction lock on /usr/lib/sysimage/rpm/.rpm.lock "
                    "(Resource temporarily unavailable)\n"
                    "2026/10/04 01:44:03 akmods: Could not install newly built RPMs. "
                    "You can find them and the logfile(s) in /var/cache/akmods/nvidia/"
                    if lock_lost else
                    "2026/10/04 01:44:03 akmods: Building rpms failed; see "
                    f"/var/cache/akmods/nvidia/{evr}-for-{kver}.failed.log for details")
        log.write_text("\n".join(lines) + "\n" + tail + "\n")
        if lock_lost:
            (self.akmod_cache / f"kmod-nvidia-{kver}-{evr}.fc44.x86_64.rpm").write_bytes(b"rpm")
        return log

    def calls(self, name) -> list[str]:
        p = self.akmod_cache / f"{name}.calls"
        return p.read_text().splitlines() if p.exists() else []

    @staticmethod
    def enable_fake_akmods() -> Path:
        """Put a fake `sudo` + `pkexec` + `akmods` first on PATH for this process. Idempotent."""
        bin_dir = Path(tempfile.mkdtemp(prefix="rivet-fakebin-"))
        _write_exe(bin_dir / "sudo", FAKE_SUDO)
        _write_exe(bin_dir / "pkexec", FAKE_SUDO)
        _write_exe(bin_dir / "akmods", FAKE_AKMODS)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        return bin_dir


class FakeDnf:
    def __init__(self, world: FakeWorld, on_event):
        self.w, self.on_event = world, on_event
        self.errors: list[str] = []
        self.specs: list[str] = []

    def open(self):
        return "/fake/session"

    def close(self):
        pass

    def upgrade(self, specs):
        self.specs = list(specs)

    def expire_cache(self):
        self.w.expires += 1
        if not self.w.expire_ok:
            self.errors.append("could not expire the metadata cache: denied")
        return self.w.expire_ok

    def _offered(self, name):
        if name in self.w.stale:
            return self.w.stale_expires and self.w.expires >= self.w.stale_expires
        return True

    def _entries(self):
        out = []
        for name in self.specs:
            if name not in self.w.available or not self._offered(name):
                continue
            evr, arch = self.w.available[name]
            have = self.w.installed.get(name, set())
            if (evr, arch) in have:
                continue
            e, v, r = _split(evr)
            pkg = {"name": name, "epoch": e, "version": v, "release": r, "arch": arch,
                   "full_nevra": f"{name}-{e}:{v}-{r}.{arch}"}
            installonly = name.startswith("kernel")
            out.append(("Package", "Install" if installonly else "Upgrade", "User", {}, pkg))
            if not installonly:
                for (oevr, oarch) in sorted(have):
                    oe, ov, orl = _split(oevr)
                    out.append(("Package", "Replaced", "User", {}, {
                        "name": name, "epoch": oe, "version": ov, "release": orl, "arch": oarch,
                        "full_nevra": f"{name}-{oe}:{ov}-{orl}.{oarch}"}))
        return out

    def resolve(self):
        return self._entries(), 0

    def problems(self):
        return []

    def do_transaction(self, *, comment="Rivet", timeout_s=7200):
        idx = len(self.w.transactions)
        self.w.transactions.append(list(self.specs))
        self.w.kmod_at_tx.append(any(self.w.akmod_cache.glob("installed-kmod-nvidia-*")))
        entries = [e for e in self._entries() if e[1] in ("Install", "Upgrade")]
        failing = idx in self.w.fail_tx or idx in self.w.raise_tx
        self.on_event("download", "progress", 100, "Downloading packages… 100%", None)
        self.on_event("install", "start", 0, f"Installing {len(entries)} packages…", None)
        for n, e in enumerate(entries, 1):
            pkg = e[4]
            name = pkg["name"]
            if failing and name in self.w.drop:
                continue
            evr = f"{pkg['epoch']}:{pkg['version']}-{pkg['release']}"
            if e[1] == "Upgrade":
                self.w.installed[name] = set()
            self.w.installed.setdefault(name, set()).add((evr, pkg["arch"]))
            self.on_event("install", "progress", int(100 * n / len(entries)),
                          pkg["full_nevra"], pkg["full_nevra"])
            if self.w.pace:
                time.sleep(self.w.pace)
        for msg in self.w.script_errors.get(idx, []):
            self.errors.append(msg)
            self.on_event("install", "progress", None, msg, msg)
        if idx in self.w.raise_tx:
            self.errors.append(self.w.raise_tx[idx])
            raise RuntimeError(self.w.raise_tx[idx])
        return not failing
