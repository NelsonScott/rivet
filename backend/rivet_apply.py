#!/usr/bin/env python3
"""rivet-apply — apply a selected set of updates, streaming JSON-line events.

    rivet-apply --plan plan.json          # real run (polkit prompts once)
    rivet-apply --dry-run                 # fake but realistic event stream
    rivet-apply --plan plan.json --resolve-only   # compute, change nothing
    rivet-apply --try-held REF [--force] | --verify-held REF [--timeout 240]
                 | --keep-held REF | --rollback-held REF      # held-back refs, see held_ops.py
    rivet-apply --other-update ID         # one "Other software" row (npm/pipx/uv/cargo/
                                           # podman pull/HA add-on), see elsewhere_apply.py
    rivet-apply --other-remove ID         # uninstall one UNUSED Home Assistant add-on row
    rivet-apply --ha-restart              # restart HA core, only when an integration waits for it
    rivet-apply --other-fix ID [--dry-run]  # Fix it on a failed row (claude -p, bounded tools)
    rivet-apply --release-reboot VERSION   # restart into the downloaded release upgrade (see below)

plan.json: {"items": ["dnf:kernel", "flatpak:runtime/org.gnome.Platform/x86_64/50", ...]}

One event per line on stdout:
  {"ts","step":"download|install|akmod|verify|flatpak|fwupd|done",
   "status":"start|progress|ok|fail|skip","pct":0-100|null,"msg":str,"log":str|null}
and finally
  {"step":"done","status":"ok|warn|fail","reboot_required":bool,
   "kmod_verified":bool|null,"summary":str,
   "cause":str|null,"error":str|null,"warnings":[str],"results":{...},"log_path":str}

A dnf transaction that reports an error is never believed on its word: the rpmdb
is checked against the plan first (see verify_installed). 2026-09-21: systemd's
file trigger restarted dnf5daemon-server at the end of a 458-element transaction
that upgraded dnf5daemon-server itself; dnf recorded "Error", every package was
in fact installed, and Rivet said the update had failed.

RPMs go through dnf5daemon on the system bus (org.rpm.dnf.v0); Flatpaks through
`flatpak update -y --noninteractive`; firmware through
`fwupdmgr update -y --no-reboot-check`. This tool NEVER reboots, with ONE exception the
user asks for by name: `--release-reboot` runs `dnf5 system-upgrade reboot`, which is how
Fedora's own tooling restarts into a downloaded release upgrade (2026-11-03).

A newer Fedora in the plan (`release:45`, the collector's release.py row) is step `release`,
AFTER everything else: `dnf5 system-upgrade download --releasever=45 -y` as root (pkexec),
streamed, with `--disable-repo` for every repo the collector found has no build for the new
release yet. It only downloads; done then carries `release: {version, downloaded, ...}` and
the app offers "Restart now", which is `--release-reboot`. If the rpm transaction before it
failed, the release step is skipped: never an upgrade on top of a half-applied update.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import LOG_DIR, STATE_PATH, LineStream, emit_line, now_iso, open_log, root_prefix, root_refusal  # noqa: E402
import flatpak_nvidia  # noqa: E402

AKMOD_CACHE = Path(os.environ.get("RIVET_AKMOD_CACHE", "/var/cache/akmods/nvidia"))
AKMOD_TIMEOUT_S = 600
AKMOD_POLL_S = 10
# rpm lists kmod-nvidia-<kver> as soon as its header is in the rpmdb, a few seconds BEFORE
# the package's %post has run depmod; `modinfo -k <kver> nvidia` resolves the NAME through
# modules.dep, so in that gap it fails although the module is on disk (2026-10-12).
KMOD_SETTLE_S = 30
KMOD_SETTLE_POLL_S = 2
# 2026-10-04: akmods finished building kmod-nvidia for the new kernel while the deferred
# updater transaction held the rpm lock, could not install the RPM it had just built, and
# left a .failed.log that made the wait below report a "build failure". akmods skips a
# kernel with a .failed.log, hence --force. Run once, as root (common.root_prefix: sudo -n
# when passwordless, else pkexec = the polkit dialog), 15 min cap.
AKMOD_LOCK_LOST = "Could not install newly built RPMs"
AKMOD_RETRY_CMD = ["akmods", "--force", "--kernels"]
AKMOD_RETRY_TIMEOUT_S = 900


# ---------------------------------------------------------------- events


class Emitter:
    """One JSON object per line, flushed immediately so the UI streams it.

    Mirrored to apply.log (rotated at 2 MB, see common.open_log). When the app window is
    closed mid-run stdout becomes a broken pipe; common.emit_line notes that once and the
    run carries on (akmod wait, verify, flatpak, fwupd) with every event, `done` included,
    landing in apply.log. `stdout_gone` says whether that happened."""

    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self.stdout_gone = False
        self.logfh = open_log("apply.log")

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        ev = {
            "ts": now_iso(),
            "step": step,
            "status": status,
            "pct": pct,
            "msg": msg,
            "log": log,
        }
        ev.update(extra)
        emit_line(self, json.dumps(ev))

    def done(self, status, *, reboot_required, kmod_verified, summary, **extra):
        """status: "ok" | "warn" | "fail" (a bool is accepted: True=ok, False=fail)."""
        if isinstance(status, bool):
            status = "ok" if status else "fail"
        ev = {
            "ts": now_iso(),
            "step": "done",
            "status": status,
            "pct": 100,
            "msg": summary,
            "log": None,
            "reboot_required": bool(reboot_required),
            "kmod_verified": kmod_verified,
            "summary": summary,
            "cause": None,
            "error": None,
            "warnings": [],
            "results": {},
            "log_path": str(LOG_DIR / "apply.log"),
        }
        ev.update(extra)
        emit_line(self, json.dumps(ev))


# ---------------------------------------------------------------- plan


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def resolve_plan(plan_ids: list[str], state: dict) -> tuple[list[dict], list[str]]:
    """Map plan ids onto the items recorded in state.json."""
    # Held-back (masked) refs are never part of a normal plan, whatever the caller sends.
    index = {i["id"]: i for g in state.get("groups", []) if g.get("id") != "held"
             for i in g.get("items", []) if not i.get("held")}
    found, missing = [], []
    for pid in plan_ids:
        it = index.get(pid)
        if it:
            found.append(it)
        else:
            missing.append(pid)
    return found, missing


def target_kernel(items: list[dict], state: dict) -> str | None:
    """The kernel release the module must be built for, e.g. 7.2.5-200.fc44.x86_64."""
    for it in items:
        if it["source"] == "dnf" and it["name"] in ("kernel", "kernel-core"):
            evr = (it.get("to") or "").split(":")[-1]
            arch = (state.get("host", {}).get("kernel_running") or "x86_64").split(".")[-1]
            m = re.match(r"^(.*)\.([^.]+)$", it.get("ref") or "")
            if m:
                arch = m.group(2)
            return f"{evr}.{arch}" if evr else None
    return None


# ---------------------------------------------------------------- streaming helpers


def stream_cmd(cmd: list[str], em: Emitter, step: str, *, timeout=7200) -> int:
    """Run cmd, forwarding each output line as a progress event.

    The deadline holds during silence too (common.LineStream): a flatpak or fwupdmgr
    stuck on a polkit prompt is killed and reaped at `timeout`, rc 124."""
    em.emit(step, "progress", None, " ".join(cmd[:3]) + " …", " ".join(cmd))
    rc, last = stream_lines(cmd, em, step, timeout=timeout)
    if rc == 127 and last.startswith("cannot run"):
        em.emit(step, "fail", None, last, None)
    elif rc == 124:
        em.emit(step, "fail", None,
                f"{cmd[0]} timed out after {timeout}s and was killed (it may have been "
                "waiting on a prompt).", None)
    return rc


def stream_lines(cmd: list[str], em: Emitter, step: str, *, timeout: float) -> tuple[int, str]:
    """Run cmd under LineStream, one progress event per output line.

    Returns (rc, last non-empty line). rc 124 = killed at the deadline, 127 = could not
    start (the "line" then says why). Emits no verdict: the caller owns ok/fail."""
    try:
        ls = LineStream(cmd, timeout=timeout)
    except OSError as exc:
        return 127, f"cannot run {cmd[0]}: {exc}"
    last = ""
    for raw in ls.lines():
        # flatpak/fwupd redraw progress with \r
        for chunk in re.split(r"[\r\n]", raw):
            line = chunk.strip()
            if not line:
                continue
            last = line
            m = re.search(r"(\d{1,3})\s?%", line)
            pct = min(100, int(m.group(1))) if m else None
            em.emit(step, "progress", pct, line[:200], line[:500])
    if ls.timed_out:
        return 124, last
    return (ls.returncode if ls.returncode is not None else 1), last


# ---------------------------------------------------------------- akmod


def norm_evr(evr: str | None) -> str:
    """'7.2.5-200.fc44' and '0:7.2.5-200.fc44' are the same EVR; rpm prints either."""
    evr = (evr or "").strip()
    if not evr:
        return ""
    return evr if re.match(r"^\d+:", evr) else f"0:{evr}"


def evr_ge(have: str, want: str) -> bool:
    """have >= want by rpm's own ordering; plain equality if python3-rpm is missing."""
    have, want = norm_evr(have), norm_evr(want)
    if have == want:
        return True
    try:
        import rpm  # python3-rpm ships with Fedora's dnf stack
    except Exception:
        return False

    def split(evr):
        e, _, vr = evr.partition(":")
        v, _, r = vr.rpartition("-")
        return (e, v, r)
    try:
        return rpm.labelCompare(split(have), split(want)) >= 0
    except Exception:
        return False


def kmod_pkg_installed(kver: str, evr: str | None = None) -> bool:
    """kmod-nvidia-<kver> is installed (and, when evr is given, at that driver EVR)."""
    cp = subprocess.run(
        ["rpm", "-q", "--qf", "%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n", f"kmod-nvidia-{kver}"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        return False
    if not evr:
        return True
    return norm_evr(evr) in {norm_evr(x) for x in cp.stdout.split()}


def modinfo_ok(kver: str) -> bool:
    cp = subprocess.run(
        ["modinfo", "-k", kver, "nvidia"], capture_output=True, text=True
    )
    return cp.returncode == 0


def kmod_module_file(kver: str) -> Path | None:
    """The nvidia.ko* that kmod-nvidia-<kver> put on disk (per rpm -ql, no index needed)."""
    cp = subprocess.run(["rpm", "-ql", f"kmod-nvidia-{kver}"], capture_output=True, text=True)
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        if re.search(r"/nvidia\.ko(\.[a-z0-9]+)?$", line) and Path(line).is_file():
            return Path(line)
    return None


def modinfo_path_ok(path: Path) -> bool:
    """modinfo on the FILE: works without modules.dep, so it tells the index from the module."""
    return subprocess.run(["modinfo", str(path)], capture_output=True).returncode == 0


def wait_for_modinfo(em: Emitter, kver: str) -> bool:
    """Give depmod its moment after the rpmdb shows the kmod (2026-10-12: verify ran in
    that two-second gap and declared the module missing; akmods said "Successful" 2 s later)."""
    if modinfo_ok(kver):
        return True
    em.emit("akmod", "progress", 97,
            f"kmod-nvidia-{kver} is in the rpmdb; waiting for depmod to index it…", None)
    deadline = time.monotonic() + KMOD_SETTLE_S
    while time.monotonic() < deadline:
        time.sleep(KMOD_SETTLE_POLL_S)
        if modinfo_ok(kver):
            return True
    em.emit("akmod", "progress", 99,
            f"modinfo still cannot resolve nvidia for {kver} after {KMOD_SETTLE_S}s; "
            "the verify step will say why.", None)
    return False


def akmod_installed() -> bool:
    return subprocess.run(["rpm", "-q", "akmod-nvidia"], capture_output=True).returncode == 0


def newest_installed_kernel() -> str | None:
    """The kernel the next boot uses when the plan carries no kernel of its own."""
    cp = subprocess.run(
        ["rpm", "-q", "--last", "kernel-core"],
        capture_output=True, text=True,
    )
    for line in cp.stdout.splitlines():        # newest first; --last ignores --qf
        if line.startswith("kernel-core-"):
            return line.split()[0][len("kernel-core-"):]
    return None


def akmod_lock_lost(failed_log: Path, built: list[Path]) -> bool:
    """The .failed.log is not a build failure but a lost rpm lock: akmods had the RPM built
    and could not install it (2026-10-04, the updater transaction was running)."""
    if not built:
        return False
    try:
        size = failed_log.stat().st_size
        with failed_log.open("rb") as fh:
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    return AKMOD_LOCK_LOST in tail


def retry_akmods(em: Emitter, kver: str) -> tuple[bool, str]:
    """`akmods --force --kernels <kver>` once, as root. (ok, what it said)."""
    prefix = root_prefix()
    cmd = prefix + AKMOD_RETRY_CMD + [kver]
    if prefix == ["pkexec"]:
        em.emit("akmod", "progress", None, "Asking for permission to rebuild the module (face or password)…", None)
    em.emit("akmod", "progress", None, "$ " + " ".join(cmd), " ".join(cmd))
    rc, last = stream_lines(cmd, em, "akmod", timeout=AKMOD_RETRY_TIMEOUT_S)
    if rc == 124:
        return False, f"timed out after {AKMOD_RETRY_TIMEOUT_S}s and was killed"
    if rc == 127 and not prefix:
        return False, last
    refused = root_refusal(prefix, rc, last) if rc != 0 else None
    if refused:
        return False, refused
    if rc != 0:
        return False, f"exited {rc}" + (f": {last[:200]}" if last else "")
    return True, "exited 0"


def wait_for_akmod(em: Emitter, kver: str, evr: str | None = None) -> bool:
    """Poll the akmods cache and the rpmdb for up to 10 minutes.

    A .failed.log for kver ends the wait: as a failure, unless it says akmods built the
    module and merely lost the rpm lock installing it, in which case akmods is re-run
    once with --force (it skips a kernel that has a .failed.log) and the rpmdb decides."""
    em.emit("akmod", "start", 0, f"Waiting for the NVIDIA module to build for {kver}…", None)
    deadline = time.monotonic() + AKMOD_TIMEOUT_S
    while time.monotonic() < deadline:
        if kmod_pkg_installed(kver, evr):
            wait_for_modinfo(em, kver)          # verify decides; this only avoids the race
            em.emit("akmod", "ok", 100, f"kmod-nvidia-{kver} is installed.", None)
            return True
        built = sorted(AKMOD_CACHE.glob(f"kmod-nvidia-{kver}-*.rpm")) if AKMOD_CACHE.is_dir() else []
        failed = sorted(AKMOD_CACHE.glob(f"*for-{kver}.failed.log")) if AKMOD_CACHE.is_dir() else []
        if failed and akmod_lock_lost(failed[-1], built):
            em.emit("akmod", "progress", None,
                    f"akmods built kmod-nvidia-{kver} but could not install it: another "
                    "transaction held the rpm lock. Installing the built module with "
                    f"`{' '.join(AKMOD_RETRY_CMD)} {kver}`…", str(failed[-1]))
            ok, said = retry_akmods(em, kver)
            if ok and kmod_pkg_installed(kver, evr):
                wait_for_modinfo(em, kver)
                em.emit("akmod", "ok", 100,
                        f"kmod-nvidia-{kver} is installed (akmods needed a retry: it had built "
                        "the module but lost the rpm lock installing it).",
                        str(built[-1]), retried=True)
                return True
            if ok:
                said = f"exited 0 but kmod-nvidia-{kver} is still not installed"
            em.emit("akmod", "fail", None,
                    f"akmods reported a build failure for {kver}. It had built the module but "
                    "lost the rpm lock installing it; the retry "
                    f"`{' '.join(AKMOD_RETRY_CMD)} {kver}` {said}.", str(failed[-1]),
                    retried=True)
            return False
        if failed:
            em.emit("akmod", "fail", None,
                    f"akmods reported a build failure for {kver}.", str(failed[-1]))
            return False
        elapsed = int(AKMOD_TIMEOUT_S - (deadline - time.monotonic()))
        pct = min(95, int(100 * elapsed / AKMOD_TIMEOUT_S))
        em.emit("akmod", "progress", pct,
                f"Building the NVIDIA module for {kver} ({elapsed}s)…",
                str(built[-1]) if built else None)
        time.sleep(AKMOD_POLL_S)
    em.emit("akmod", "fail", None, f"akmods did not finish within {AKMOD_TIMEOUT_S}s.", None)
    return False


def verify_kmod(em: Emitter, kver: str, evr: str | None = None) -> tuple[bool, str | None]:
    """(verified, why not). A failure names the exact gap: package, file, or depmod index."""
    em.emit("verify", "start", 0, f"Verifying the NVIDIA module for {kver}…", None)
    pkg = kmod_pkg_installed(kver, evr)
    em.emit("verify", "progress", 50,
            f"kmod-nvidia-{kver}: {'present' if pkg else 'MISSING'}", None)
    mi = modinfo_ok(kver)
    why = None
    if not pkg:
        why = (f"kmod-nvidia-{kver}" + (f" at driver {evr}" if evr else "")
               + " is not in the rpmdb: akmods has not installed a module for this kernel.")
    elif not mi:
        ko = kmod_module_file(kver)
        if ko is not None and modinfo_path_ok(ko):
            why = (f"{ko} is on disk and reads fine by path, but modules.dep for {kver} does not "
                   f"list it: depmod has not run for this kernel. `sudo depmod -a {kver}` fixes it.")
        elif ko is None:
            why = f"kmod-nvidia-{kver} is in the rpmdb but its nvidia.ko is not on disk."
        else:
            why = f"{ko} is on disk but modinfo cannot read it (corrupt or unsigned build?)."
    em.emit("verify", "progress", 90,
            f"modinfo -k {kver} nvidia: {'ok' if mi else 'FAILED'}", why)
    ok = pkg and mi
    em.emit("verify", "ok" if ok else "fail", 100,
            "NVIDIA module verified for the new kernel."
            if ok else "NVIDIA module NOT verified — do not reboot yet.", None,
            **({"cause": why} if why else {}))
    return ok, why


def run_verify_kmod(em: Emitter, kver: str | None) -> int:
    """`rivet-apply --verify-kmod [KVER]`: the module check on its own, for the app's
    "the doctor says nothing is broken, check again" path (2026-10-12). Nothing is changed."""
    if not akmod_installed():
        em.done("ok", reboot_required=False, kmod_verified=None,
                summary="akmod-nvidia is not installed: there is no NVIDIA module to verify.")
        return 0
    kver = kver or newest_installed_kernel()
    if not kver:
        msg = "No installed kernel-core found in the rpmdb."
        em.done("fail", reboot_required=False, kmod_verified=None, summary=msg, cause=msg)
        return 1
    reboot = kver != os.uname().release
    ok, why = verify_kmod(em, kver)
    if ok:
        summary = f"The NVIDIA module is verified for {kver}." + (
            " A reboot is needed to finish." if reboot else " That is the running kernel.")
        em.done("ok", reboot_required=reboot, kmod_verified=True, summary=summary, kver=kver)
        return 0
    summary = f"The NVIDIA module for {kver} could not be verified — do not reboot until it is."
    em.done("fail", reboot_required=reboot, kmod_verified=False, summary=summary,
            cause=why or summary, kver=kver)
    return 1


# ---------------------------------------------------------------- rpmdb truth


def rpmdb_snapshot() -> dict[str, set[tuple[str, str]]]:
    """name -> {(E:V-R, arch)} for everything installed. One rpm call, ~0.4 s, no root.

    Done in memory rather than `rpm -q name-E:V-R.arch` because rpm does not match
    a label spelled with an explicit 0 epoch ("fzf-0:0.74.3-1.fc44.x86_64 is not
    installed"), which is exactly how dnf5daemon spells every NEVRA.
    """
    cp = subprocess.run(
        ["rpm", "-qa", "--qf", "%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{ARCH}\n"],
        capture_output=True, text=True, timeout=120,
    )
    snap: dict[str, set[tuple[str, str]]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            snap.setdefault(parts[0], set()).add((parts[1], parts[2]))
    return snap


INBOUND = {"install", "upgrade", "downgrade", "reinstall"}
OUTBOUND = {"remove", "replaced"}


def tx_pkg(entry) -> dict | None:
    """One Goal.resolve() element -> {name, evr, arch, nevra, action}."""
    try:
        pkg = dict(entry[-1])
        action = str(entry[1]).lower()
    except Exception:
        return None
    if not pkg.get("name"):
        return None
    evr = f"{pkg.get('epoch') or 0}:{pkg.get('version')}-{pkg.get('release')}"
    return {"name": pkg["name"], "evr": norm_evr(evr), "arch": pkg.get("arch") or "",
            "nevra": pkg.get("full_nevra") or pkg["name"], "action": action}


def verify_installed(items: list[dict], pkgs: list[dict], snap: dict) -> dict:
    """Did the transaction really land? Compares the rpmdb with the plan.

    items: the planned dnf items (name + `to` EVR from state.json)
    pkgs:  every element dnf resolved (tx_pkg dicts), so dependencies count too
    A planned name is installed when its resolved target (or, failing that, its
    `to` EVR) is in the rpmdb -- any one of several installed versions will do,
    so kernel* with three versions side by side is fine. Removed and replaced
    packages must be GONE, unless the same NEVRA is also inbound (reinstall);
    a survivor there is the classic half-finished-transaction duplicate.
    """
    inbound = [p for p in pkgs if p["action"] in INBOUND]
    outbound = [p for p in pkgs if p["action"] in OUTBOUND]
    in_keys = {(p["name"], p["evr"], p["arch"]) for p in inbound}

    def present(p):
        return (p["evr"], p["arch"]) in snap.get(p["name"], set())

    tx_missing = [p["name"] for p in inbound if not present(p)]
    by_name: dict[str, list[dict]] = {}
    for p in inbound:
        by_name.setdefault(p["name"], []).append(p)

    def is_leftover(p):
        if not present(p) or (p["name"], p["evr"], p["arch"]) in in_keys:
            return False
        if p["name"] in by_name:
            # old AND new both installed = a duplicate. Old without new is merely
            # "not upgraded", which the missing list already says.
            return all(present(n) for n in by_name[p["name"]])
        return not tx_missing       # obsoleted/removed: only damning if the rest landed

    leftover = [p["nevra"] for p in outbound if is_leftover(p)]
    planned_missing = []
    for it in items:
        name = it["name"]
        have = {evr for evr, _arch in snap.get(name, set())}
        if name in by_name:
            ok = all(present(p) for p in by_name[name])
        else:
            # >= rather than ==: a build newer than the one state.json saw may
            # have landed between collect and apply (Sunshine did, 2026-09-21).
            ok = any(evr_ge(h, it.get("to")) for h in have)
        if not ok:
            planned_missing.append(name)

    missing = list(dict.fromkeys(planned_missing + tx_missing))
    return {
        "planned": len(items),
        "installed": len(items) - len(planned_missing),
        "missing": missing,
        "leftover": leftover,
        "tx_total": len(inbound),
        "tx_installed": len(inbound) - len(tx_missing),
    }


def dnf_check() -> list[str] | None:
    """`dnf5 check` as the user: read-only, no root, ~10 s. None = could not run."""
    try:
        cp = subprocess.run(["dnf5", "check"], capture_output=True, text=True, timeout=180)
    except Exception:
        return None
    lines = [l.strip() for l in (cp.stdout + cp.stderr).splitlines() if l.strip()]
    if cp.returncode == 0:
        return []
    return lines or [f"dnf5 check exited {cp.returncode}"]


# What the daemon logs at WARNING for every kernel removal; never the cause.
JOURNAL_NOISE = re.compile(r"remove failed: No such file or directory|non-critical error")
JOURNAL_LEVEL = re.compile(r"\] (WARNING|ERROR|CRITICAL) ")
JOURNAL_TIERS = [
    re.compile(r"scriptlet failed|transaction lock|Transaction failed|unpacking of archive failed"
               r"|No space left|conflicts with|Failed to (download|obtain)", re.I),
    re.compile(r"\] (ERROR|CRITICAL) "),
    re.compile(r"\] WARNING "),
]


def journal_scan(since_epoch: float) -> dict:
    """Fallback error source: what dnf5daemon-server itself logged since `since`.

    The daemon writes its own log lines to stdout, so journald files every one of
    them at priority info and `-p warning..err` matches nothing (checked against
    the 2026-09-21 run). Match on the level word inside the message instead.
    A user in `wheel` reads the system journal without privilege.
    """
    out = {"error": None, "self_restart": False}
    try:
        cp = subprocess.run(
            ["journalctl", "-u", "dnf5daemon-server", "--since", f"@{int(since_epoch)}",
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return out
    lines = cp.stdout.splitlines()
    out["self_restart"] = any("Stopping dnf5daemon-server.service" in l for l in lines)
    for tier in JOURNAL_TIERS:
        for l in lines:
            if not JOURNAL_LEVEL.search(l) or JOURNAL_NOISE.search(l):
                continue
            if tier.search(l):
                out["error"] = re.sub(r"^\S+ \[\d+\] ", "", l).strip()[:300]
                return out
    return out


# ---------------------------------------------------------------- real apply

# Packages whose upgrade makes systemd restart the very service that is running
# the transaction (dnf5daemon-server: %systemd_postun_with_restart + systemd's
# %transfiletriggerpostun "restart marked units"), or that the transaction's
# D-Bus/polkit plumbing depends on. The dnf5 stack is version-locked, so it moves
# as one family. These go LAST, in a small transaction of their own, where an
# interrupted cleanup scriptlet costs nothing.
UPDATER_RE = re.compile(r"^(dnf5|libdnf5|python3-libdnf5)|^(dbus-broker|polkit|polkit-libs)$")


def is_updater_pkg(name: str) -> bool:
    return bool(UPDATER_RE.search(name or ""))


def split_updater(names: list[str]) -> tuple[list[str], list[str]]:
    """(main, deferred). No split when there is nothing else to install."""
    deferred = [n for n in names if is_updater_pkg(n)]
    main = [n for n in names if not is_updater_pkg(n)]
    if not main or not deferred:
        return names, []
    return main, deferred


def make_daemon(on_event):
    """Seam for the tests (backend/tests, app/qa/fake-apply.py)."""
    from dnf5daemon import DnfDaemon
    return DnfDaemon(on_event=on_event)


def not_offered(items: list[dict], pkgs: list[dict], snap: dict) -> list[str]:
    """Planned names dnf neither put in the transaction nor already has installed at
    (or above) the target: the daemon's metadata does not know that build."""
    known = {p["name"] for p in pkgs}      # inbound, or replaced/obsoleted by something inbound
    out = []
    for it in items:
        if it["name"] in known:
            continue
        have = {evr for evr, _arch in snap.get(it["name"], set())}
        if not any(evr_ge(h, it.get("to")) for h in have):
            out.append(it["name"])
    return out


def stale_message(items: list[dict], names: list[str]) -> str:
    to = {i["name"]: i.get("to") for i in items}
    shown = ", ".join(f"{n}-{to[n]}" if to.get(n) else n for n in names[:4])
    more = f" and {len(names) - 4} more" if len(names) > 4 else ""
    return (f"dnf does not offer {shown}{more} yet: its package metadata is behind the "
            "checker's.")


def run_transaction(emit, items: list[dict], *, phase: str, resolve_only: bool,
                    refresh: bool = True) -> dict:
    """One dnf5daemon session: expire-cache -> upgrade(specs) -> resolve -> do_transaction.

    Returns {"ok", "stage": open|resolve|transaction|done, "pkgs", "errors", "started",
    "stale": [names dnf never offered]}.
    Never emits install ok/fail itself: apply_dnf decides that after verifying.

    Stale metadata (2026-09-22): the daemon's own cache was hours behind the
    collector's, so upgrade(kernel...) resolved to an EMPTY transaction that the old
    code took for success. Now the cache is expired first, and a planned package that
    is neither in the transaction nor already installed at its target means "dnf does
    not see it": refreshed and retried once, then a failure that names it.
    """
    specs = sorted({i["name"] for i in items})
    for attempt in (1, 2):
        res = _run_transaction_once(emit, items, specs, phase=phase, resolve_only=resolve_only,
                                    refresh=refresh or attempt == 2)
        if not res.get("stale") or attempt == 2:
            return res
        emit("download", "progress", None,
             stale_message(items, res["stale"]) + " Refreshing and trying again…",
             "stale: " + " ".join(res["stale"]))
    return res


def _run_transaction_once(emit, items: list[dict], specs: list[str], *, phase: str,
                          resolve_only: bool, refresh: bool) -> dict:
    res = {"ok": False, "stage": "open", "pkgs": [], "errors": [], "stale": [],
           "started": time.time(), "phase": phase}
    try:
        daemon = make_daemon(emit)
    except Exception as exc:
        res["errors"].append(f"cannot reach dnf5daemon: {exc}")
        return res
    try:
        daemon.open()
        if refresh:
            emit("download", "progress", None,
                 "Refreshing package metadata… (a minute or two on a cold cache)", None)
            try:
                daemon.expire_cache()
            except Exception as exc:     # the wait/verify below still catch a stale view
                emit("download", "progress", None,
                     f"Could not refresh the metadata cache ({exc}); using what dnf has.", None)
        daemon.upgrade(specs)
        res["stage"] = "resolve"
        tx, result = daemon.resolve()
        res["pkgs"] = [p for p in (tx_pkg(e) for e in tx) if p]
        inbound = [p for p in res["pkgs"] if p["action"] in INBOUND]
        probs = daemon.problems()
        if result > 1 or (probs and result != 0):
            for p in probs:
                emit("install", "progress", None, p[:200], p)
            res["errors"] += probs or [f"dnf could not resolve the transaction (code {result})"]
            return res
        for p in probs:
            emit("install", "progress", None, f"note: {p[:180]}", p)
        res["stale"] = not_offered(items, res["pkgs"], rpmdb_snapshot())
        if res["stale"]:
            res["errors"].append(stale_message(items, res["stale"]))
            return res
        if phase == "main":
            pulled = sorted({p["name"] for p in inbound if is_updater_pkg(p["name"])})
            if pulled:
                emit("download", "progress", None,
                     "Could not hold back " + ", ".join(pulled[:4])
                     + ": another package needs it now. If dnf reports a late error, "
                       "Rivet checks the installed versions before believing it.", None)
        emit("download", "progress", 0, f"{len(inbound)} package(s) in the transaction.", None)
        if not inbound and not [p for p in res["pkgs"] if p["action"] == "remove"]:
            emit("install", "progress", 100,
                 "Nothing left to do: every planned package is already installed.", None)
            res["ok"], res["stage"] = True, "done"
            return res
        if resolve_only:
            res["ok"], res["stage"] = True, "done"
            return res
        res["stage"] = "transaction"
        res["started"] = time.time()
        res["ok"] = bool(daemon.do_transaction(
            comment="Rivet" if phase == "main" else "Rivet (updater packages)"))
        if res["ok"]:
            res["stage"] = "done"
        return res
    except Exception as exc:  # DnfError, or anything the bus throws
        res["errors"].append(str(exc))
        return res
    finally:
        for e in getattr(daemon, "errors", []):
            if e not in res["errors"]:
                res["errors"].append(e)
        try:
            daemon.close()
        except Exception:
            pass


def plain_cause(error: str | None, v: dict, stale: list[str] | None = None) -> str:
    e = (error or "").lower()
    if stale:
        return (f"dnf does not offer {', '.join(stale[:4])} yet (its package metadata is behind "
                "the checker's), so that part was skipped.")
    if re.search(r"not authori[sz]ed|authorization|authentication|dismissed", e):
        return "The administrator password prompt was cancelled or denied, so nothing was installed."
    if re.search(r"conflicts with|conflicting requests|nothing provides|cannot install both|broken dependenc", e):
        # 2026-11-01 abuse pass: a held-back package used to read "dnf could not run the transaction"
        m = re.search(r"package (\S+?)-\d[^\s]* (?:from \S+ )?(?:conflicts|requires)|nothing provides \S+ needed by (\S+?)-\d", error or "")
        who = next((g for g in (m.groups() if m else ()) if g), None)
        return (f"dnf found a dependency conflict{f' around {who}' if who else ''}, so nothing was installed. "
                "Untick it and update the rest; a conflict like this usually clears when the repositories catch up.")
    if "transaction lock" in e:
        return "Another package manager was holding the RPM lock, so the transaction could not run."
    if "no space left" in e:
        return "The disk filled up during the transaction."
    if re.search(r"download|obtain|curl|mirror", e):
        return "Some packages could not be downloaded."
    if v.get("leftover") and not v.get("missing"):
        return ("The new packages are installed but old versions were left behind; "
                "run `sudo dnf5 remove --duplicates`.")
    if v.get("installed") == 0:
        return "dnf could not run the transaction, so no package was installed."
    return "dnf stopped partway: some packages are installed and some are not."


KMOD_TRIGGER_RE = re.compile(r"^(akmod|kmod)-nvidia$", re.I)


def kmod_touched(items: list[dict], missing: list[str]) -> bool:
    """A kernel or NVIDIA driver in `items` really landed (per the rpmdb's `missing`),
    so akmods is building a module the next boot depends on."""
    return any(i["name"] not in missing for i in items
               if i["name"] in ("kernel", "kernel-core") or KMOD_TRIGGER_RE.match(i["name"]))


def apply_dnf(em: Emitter, items: list[dict], *, resolve_only: bool,
              reboot: bool = False, kver: str | None = None,
              kmod_evr: str | None = None) -> dict:
    """All RPM work. Returns
    {"status": ok|warn|fail|skip, "warning", "error", "cause", "verify": {...},
     "akmod_ok": True|False|None}.

    kver / kmod_evr (from kmod_plan): when the main transaction installed a kernel or
    the NVIDIA driver and an updater transaction follows, the akmod wait happens IN
    BETWEEN, so akmods never tries to install its freshly built kmod while the updater
    transaction holds the rpm lock (2026-10-04). `akmod_ok` is that wait's verdict, None
    when no wait happened here; the caller must not wait (or retry) a second time.
    """
    names = sorted({i["name"] for i in items})
    main, deferred = split_updater(names)
    phases = [("main", main)] + ([("updater", deferred)] if deferred else [])
    split = len(phases) > 1
    em.emit("download", "start", 0, f"Preparing {len(names)} package(s)…", " ".join(names))
    if split:
        em.emit("download", "progress", None,
                f"Holding back {len(deferred)} updater package(s) "
                f"({', '.join(deferred[:3])}{'…' if len(deferred) > 3 else ''}) for a second, "
                "final transaction, so the updater cannot restart itself mid-run.",
                "deferred: " + " ".join(deferred))

    def emitter_for(phase):
        if not split:
            return em.emit

        def emit(step, status, pct=None, msg="", log=None, **extra):
            # One install step as far as the UI is concerned: the main transaction
            # fills 0-90 %, the updater transaction 90-100 %, tagged with `phase`.
            if phase == "updater":
                downloading = step == "download"
                step = "install"
                if status in ("start", "fail"):     # apply_dnf emits the one verdict
                    status = "progress"
                msg = f"Updater packages: {msg}" if msg else msg
                pct = None if pct is None else 90 if downloading else 90 + int(pct / 10)
            elif step == "install" and pct is not None:
                pct = int(pct * 0.9)
            em.emit(step, status, pct, msg, log, phase=phase, **extra)
        return emit

    results = []
    akmod_ok = None
    for idx, (phase, specs) in enumerate(phases):
        if phase == "updater":
            em.emit("install", "progress", 90,
                    f"Now the updater's own {len(specs)} package(s)…", " ".join(specs),
                    phase="updater")
        phase_items = [i for i in items if i["name"] in specs]
        r = run_transaction(emitter_for(phase), phase_items,
                            phase=phase, resolve_only=resolve_only)
        results.append(r)
        v = None
        if not r["ok"]:
            if r["stage"] != "transaction":
                break       # never started: a second polkit prompt would only confuse
            snap = rpmdb_snapshot()
            v = verify_installed(phase_items, r["pkgs"], snap)
            if v["missing"] or v["leftover"]:
                break       # really broken: do not pile a second transaction on top
        if phase == "main" and idx + 1 < len(phases) and kver and not resolve_only:
            # The updater transaction comes next. akmods, kicked off by the kernel/driver
            # install, will want the rpm lock to install its kmod: let it finish first.
            # (Only reached when the main transaction ran and nothing planned is missing.)
            v = v or verify_installed(phase_items, r["pkgs"], rpmdb_snapshot())
            if kmod_touched(phase_items, v["missing"]):
                em.emit("install", "progress", 90,
                        "Letting akmods finish before the updater's own transaction, so the "
                        "two never fight over the rpm lock.",
                        f"akmod wait for {kver} between the main and updater transactions",
                        phase="main")
                akmod_ok = wait_for_akmod(em, kver, kmod_evr)

    all_pkgs = [p for r in results for p in r["pkgs"]]
    n = len(items)
    if resolve_only:
        ok = all(r["ok"] for r in results)
        em.emit("install", "skip" if ok else "fail", 100,
                "--resolve-only: transaction computed, nothing installed." if ok
                else "dnf could not resolve the transaction.",
                None if ok else "; ".join(e for r in results for e in r["errors"])[:500])
        return {"status": "skip" if ok else "fail", "warning": None, "cause": None,
                "error": None, "verify": {"planned": n, "installed": 0, "missing": [],
                                          "leftover": []}, "akmod_ok": None}

    # ---- dnf's word, ok or not, is checked against the rpmdb before anyone hears it.
    # (2026-09-22: an empty transaction was reported as "all 12 installed".)
    dnf_ok = all(r["ok"] for r in results) and len(results) == len(phases)
    if dnf_ok:
        v = verify_installed(items, all_pkgs, rpmdb_snapshot())
        if not v["missing"] and not v["leftover"]:
            nothing = not any(p["action"] in INBOUND for p in all_pkgs)
            msg = ("Nothing to install: all packages were already at their target version."
                   if nothing else "RPM transaction complete.")
            em.emit("install", "ok", 100, msg, None, verify=v)
            return {"status": "ok", "warning": None, "error": None, "cause": None, "verify": v,
                    "akmod_ok": akmod_ok}
        em.emit("install", "progress", 99,
                "dnf reported success but the rpmdb disagrees — checking…", None)
    else:
        em.emit("install", "progress", 99,
                "dnf reported an error — checking what is actually installed…", None)
        v = verify_installed(items, all_pkgs, rpmdb_snapshot())
    failed = [r for r in results if not r["ok"]]
    stale = [nm for r in results for nm in r.get("stale", [])]
    error = next((e for r in failed for e in r["errors"]), None)
    if dnf_ok and not error:
        error = (f"dnf said the transaction succeeded but {', '.join(v['missing'][:4])} "
                 "is not installed at the planned version.")
    scan = {"error": None, "self_restart": False}
    for r in failed:
        s = journal_scan(r["started"])
        scan["self_restart"] = scan["self_restart"] or s["self_restart"]
        scan["error"] = scan["error"] or s["error"]
    error = error or scan["error"]
    if scan["error"] and error != scan["error"] and re.search(r"restarted during", error or ""):
        error = f"{scan['error']} ({error})"

    if not v["missing"] and not v["leftover"] and not stale:
        check = dnf_check()
        updater_in_tx = any(p["name"] == "dnf5daemon-server" and p["action"] in INBOUND
                            for p in all_pkgs)
        self_restart = updater_in_tx and (
            scan["self_restart"] or re.search(r"signal 15|restarted during", error or ""))
        if self_restart:
            warning = ("Packages installed. A cleanup script was interrupted because the "
                       "updater restarted itself (dnf5daemon-server was part of this update). "
                       "Nothing to redo" + ("; reboot as planned." if reboot else "."))
        else:
            warning = (f"Packages installed: all {n} verified at their target version. dnf "
                       "reported an error in a post-install script"
                       + (f" ({error})" if error else "") + ". Nothing to redo"
                       + ("; reboot as planned." if reboot else "."))
        if check:
            warning = (f"Packages installed: all {n} verified at their target version, but "
                       f"`dnf check` reports {len(check)} problem(s): {check[0][:160]}")
        v["dnf_check"] = check
        em.emit("install", "ok", 100, warning, error, warning=warning, error=error, verify=v)
        return {"status": "warn", "warning": warning, "error": error,
                "cause": warning, "verify": v, "akmod_ok": akmod_ok}

    miss = v["missing"]
    msg = f"{v['installed']} of {n} package(s) installed."
    if miss:
        msg += " Not installed: " + ", ".join(miss[:8]) + (
            f" and {len(miss) - 8} more." if len(miss) > 8 else ".")
    if v["leftover"]:
        msg += (f" {len(v['leftover'])} old version(s) left behind: "
                + ", ".join(v["leftover"][:8]) + ".")
    if error:
        msg += f" First error: {error}"
    cause = (stale_message(items, stale) + " Check again in a while; nothing was changed."
             if stale and v["installed"] == 0 else plain_cause(error, v, stale=stale))
    em.emit("install", "fail", 100, msg, error, error=error, verify=v, cause=cause)
    return {"status": "fail", "warning": None, "error": error, "cause": cause,
            "verify": v, "msg": msg, "akmod_ok": akmod_ok}


# Seams the tests replace (fixtures/fake_dnf.py): nothing below may reach the real
# flatpak from a test run.
def nv_missing(version: str) -> list[dict]:
    return flatpak_nvidia.missing(version)


def nv_remote_has(scope: str, ref: str) -> bool:
    return flatpak_nvidia.remote_has(scope, ref)


def run_flatpak(cmd: list[str], em: Emitter) -> int:
    return stream_cmd(cmd, em, "flatpak")


def flatpak_scope(it: dict) -> str:
    scope = it.get("scope") or it.get("_scope")
    if scope in ("user", "system"):
        return f"--{scope}"
    # older state.json: no scope recorded, so ask flatpak itself
    probe = subprocess.run(["flatpak", "info", "--user", it["ref"]],
                           capture_output=True, text=True)
    return "--user" if probe.returncode == 0 else "--system"


def new_nvidia_version(dnf_items: list[dict], verify: dict | None) -> str | None:
    """The driver version this plan moves the host to, or None when it does not."""
    gone = set((verify or {}).get("missing") or [])
    for it in dnf_items:
        if it["name"] in flatpak_nvidia.DRIVER_PKGS and it["name"] not in gone:
            old, new = flatpak_nvidia.version_of(it.get("from")), flatpak_nvidia.version_of(it.get("to"))
            if new and new != old:
                return new
    return None


def apply_nvidia_runtime(em: Emitter, version: str, res: dict, *, skip_refs: set[str],
                         resolve_only: bool = False) -> list[str]:
    """Install the Flatpak NVIDIA runtime for the driver this run just installed.

    `flatpak update` cannot do it: Flatpak follows the LOADED kernel module, which is
    still the old one until the reboot -- and after the reboot Steam's games would have
    no GPU (2026-09-21). Old runtimes stay; `flatpak uninstall --unused` owns those.
    Returns warnings (a runtime Flathub does not have yet, or a failed install).
    """
    warnings: list[str] = []
    gaps = [g for g in nv_missing(version) if g["ref"] not in skip_refs]
    if not gaps:
        return warnings
    title = "Installing matching NVIDIA runtime for Flatpak apps (Steam)"
    em.emit("flatpak", "start", 0, f"{title}: driver {version}…", None)
    for n, gap in enumerate(gaps):
        name = flatpak_nvidia.display_name(gap["kind"], version)
        if resolve_only:
            em.emit("flatpak", "skip", None, f"--resolve-only: would install {name}.", gap["ref"])
            continue
        if not nv_remote_has(gap["scope"], gap["ref"]):
            w = (f"Flathub does not have the NVIDIA {version} runtime yet ({gap['kind']}), so "
                 "Flatpak apps such as Steam games will start without the GPU after the "
                 "reboot. Check for updates again later; Rivet will list it.")
            warnings.append(w)
            res["skipped"] += 1
            em.emit("flatpak", "skip", None, w, gap["ref"], warning=w)
            continue
        em.emit("flatpak", "progress", int(100 * n / len(gaps)), f"{title}: {name}", gap["ref"])
        rc = run_flatpak(["flatpak", "install", "-y", "--noninteractive", f"--{gap['scope']}",
                          flatpak_nvidia.REMOTE, gap["ref"]], em)
        if rc == 0:
            res["ok"] += 1
            em.emit("flatpak", "ok", 100, f"{name} installed.", None)
        else:
            w = (f"{name} could not be installed (flatpak rc={rc}); Flatpak apps may start "
                 "without the GPU after the reboot. Rivet will list it as pending.")
            warnings.append(w)
            res["failed"] += 1
            res["failed_names"].append(name)
            em.emit("flatpak", "fail", None, w, None, error=w)
    return warnings


def apply_flatpak(em: Emitter, items: list[dict], res: dict | None = None) -> dict:
    res = res if res is not None else {"ok": 0, "failed": 0, "skipped": 0, "failed_names": []}
    for it in items:
        if it.get("blocked_by"):
            res["skipped"] += 1
            em.emit("flatpak", "skip", None,
                    f"{it['display_name']} skipped — blocked by {it['blocked_by']}.", None)
            continue
        scope = flatpak_scope(it)
        install = it.get("op") == "install"
        verb = "Installing" if install else "Updating"
        em.emit("flatpak", "start", 0, f"{verb} {it['display_name']}…", it["ref"])
        if install:
            # a related ref Flatpak wants but does not have: `update <ref>` would fail
            cmd = ["flatpak", "install", "-y", "--noninteractive", scope,
                   it.get("origin") or "flathub", it["ref"]]
        else:
            cmd = ["flatpak", "update", "-y", "--noninteractive", scope, it["ref"]]
        tap = _ErrTap(em)
        rc = run_flatpak(cmd, tap)
        if rc == 0:
            res["ok"] += 1
            em.emit("flatpak", "ok", 100,
                    f"{it['display_name']} {'installed' if install else 'updated'}.", None)
        else:
            res["failed"] += 1
            res["failed_names"].append(it["display_name"])
            why = flatpak_reason(tap.err, it)
            if why:
                res.setdefault("reasons", {})[it["display_name"]] = why
            em.emit("flatpak", "fail", None,
                    f"{it['display_name']} failed (rc={rc})" + (f": {why}" if why else "."), None,
                    **({"error": tap.err} if tap.err else {}))
    return res


class _ErrTap:
    """Passes every event on and remembers flatpak's last `error:` line (2026-11-01)."""

    def __init__(self, em):
        self.em, self.err = em, None

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        for t in (log, msg):
            t = str(t or "").strip()
            if re.match(r"(?i)^error:", t):
                self.err = t[:300]
                break
        return self.em.emit(step, status, pct, msg, log, **extra)

    def __getattr__(self, name):
        return getattr(self.em, name)


def flatpak_reason(err: str | None, it: dict) -> str | None:
    """flatpak's error line -> one plain clause, or None."""
    if not err:
        return None
    e = err.lower()
    if re.search(r"\bis (currently )?running\b|\bin use\b|running instances?|close (it|them|the app)", e):
        return (f"it is in use by a running app; close {it['display_name']} (and any app that uses it) "
                "and update again")
    if re.search(r"unable to load summary|could not resolve|couldn.t resolve|network|timed out|remote", e):
        return "its remote could not be reached; nothing changed"
    if "no space" in e:
        return "the disk is full"
    return err.split(":", 1)[-1].strip()[:160] or None


def apply_fwupd(em: Emitter, items: list[dict]) -> dict:
    res = {"ok": 0, "failed": 0, "failed_names": []}
    for it in items:
        em.emit("fwupd", "start", 0, f"Updating {it['display_name']}…", it["ref"])
        rc = stream_cmd(
            ["fwupdmgr", "update", "-y", "--no-reboot-check", it["ref"]],
            em, "fwupd",
        )
        if rc == 0:
            res["ok"] += 1
            em.emit("fwupd", "ok", 100,
                    f"{it['display_name']} staged; it applies on the next boot.", None)
        else:
            res["failed"] += 1
            res["failed_names"].append(it["display_name"])
            em.emit("fwupd", "fail", None, f"{it['display_name']} failed (rc={rc}).", None)
    return res


def kmod_plan(dnf_items: list[dict], state: dict) -> tuple[str | None, str | None]:
    """(kernel release to verify, driver EVR the kmod must carry | None).

    Needed whenever the plan touches a kernel OR the NVIDIA akmod -- either one
    makes akmods build a module the next boot depends on. (Before 2026-09-21 it
    took BOTH in the plan, and a dnf error skipped the check entirely.)
    """
    has_kernel = any(i["name"] in ("kernel", "kernel-core") for i in dnf_items)
    akmod = next((i for i in dnf_items
                  if re.match(r"^(akmod|kmod)-nvidia$", i["name"], re.I)), None)
    if not has_kernel and not akmod:
        return None, None
    if not akmod and not akmod_installed():
        return None, None
    kver = target_kernel(dnf_items, state) if has_kernel else newest_installed_kernel()
    return kver, (akmod.get("to") if akmod else None)


# ---------------------------------------------------------------- release upgrade (2026-11-03)

RELEASE_TIMEOUT_S = 4 * 3600          # a few GB on a slow mirror
# dnf5's download counter: "[ 12/1834] libfoo-1.2.x86_64  100% | 2.3 MiB/s | ..."
RELEASE_COUNT_RE = re.compile(r"^\[\s*(\d+)/(\d+)\]")
RELEASE_SIZE_RE = re.compile(r"Total size of inbound packages is ([\d.,]+\s?[KMGT]i?B)", re.I)
RELEASE_ERASE_HINT = re.compile(r"--allowerasing|conflict|problem", re.I)


def release_cmd(version: int, missing_repos: list[str]) -> list[str]:
    cmd = ["dnf5", "system-upgrade", "download", f"--releasever={version}", "-y"]
    cmd += [f"--disable-repo={r}" for r in missing_repos]
    return cmd


def apply_release(em: Emitter, item: dict, *, resolve_only: bool) -> dict:
    """Download the newer Fedora (step `release`), never install it: that is the reboot's job.

    Returns {version, downloaded, status: ok|fail|skip, disabled_repos, size, packages,
    nvidia, cause}. Root through common.root_prefix() (pkexec: the desktop's polkit dialog,
    "Asking for permission…" first)."""
    rel = item.get("release") or {}
    try:
        version = int(rel.get("version") or item.get("to"))
    except (TypeError, ValueError):
        em.emit("release", "fail", None, f"{item.get('id')}: no release version in state.json.", None)
        return {"version": None, "downloaded": False, "status": "fail", "disabled_repos": [],
                "size": None, "packages": None, "nvidia": akmod_installed(),
                "cause": "The release row in state.json names no version; check for updates again."}
    missing = [r for r in (rel.get("missing_repos") or []) if re.match(r"^[\w.:@+-]+$", str(r))]
    res = {"version": version, "downloaded": False, "status": "ok", "disabled_repos": missing,
           "size": None, "packages": None, "nvidia": akmod_installed(), "cause": None}
    if resolve_only:
        em.emit("release", "skip", None, f"--resolve-only: Fedora {version} not downloaded.", None)
        res["status"] = "skip"
        return res
    em.emit("release", "start", 0, f"Downloading Fedora {version}…", None)
    if missing:
        em.emit("release", "progress", None,
                f"Leaving out {len(missing)} repo{'s' if len(missing) > 1 else ''} with no Fedora "
                f"{version} build yet: " + ", ".join(missing), None)
    prefix = root_prefix()
    cmd = prefix + release_cmd(version, missing)
    if prefix[:1] == ["pkexec"]:
        em.emit("release", "progress", None, "Asking for permission…", None)
    em.emit("release", "progress", None, "dnf5 system-upgrade download …", " ".join(cmd))
    try:
        ls = LineStream(cmd, timeout=RELEASE_TIMEOUT_S)
    except OSError as exc:
        res.update(status="fail", cause=f"dnf5 could not be started: {exc}")
        em.emit("release", "fail", None, res["cause"], None)
        return res
    last, errors = "", []
    for raw in ls.lines():
        for chunk in re.split(r"[\r\n]", raw):
            line = chunk.strip()
            if not line:
                continue
            last = line
            pct = None
            m = RELEASE_COUNT_RE.match(line)
            if m and int(m.group(2)):
                res["packages"] = int(m.group(2))
                pct = min(99, int(100 * int(m.group(1)) / int(m.group(2))))
            m = RELEASE_SIZE_RE.search(line)
            if m:
                res["size"] = m.group(1)
                em.emit("release", "progress", None, f"Fedora {version} is {m.group(1)} to download.", line[:500])
                continue
            if re.match(r"^(Error|Problem|Failed|Cannot)", line, re.I) or "error:" in line.lower():
                errors.append(line)
            em.emit("release", "progress", pct, line[:200], line[:500])
    rc = 124 if ls.timed_out else (ls.returncode if ls.returncode is not None else 1)
    if rc == 0:
        res["downloaded"] = True
        size = f" ({res['size']})" if res["size"] else ""
        em.emit("release", "ok", 100, f"Fedora {version} is downloaded{size}. Restart to install it.", None)
        return res
    refusal = root_refusal(prefix, rc, last)
    if refusal:
        cause = f"Fedora {version} was not downloaded: {refusal}."
    elif rc == 124:
        cause = f"The Fedora {version} download timed out after {RELEASE_TIMEOUT_S // 3600} h and was stopped."
    elif any(RELEASE_ERASE_HINT.search(e) for e in errors) or RELEASE_ERASE_HINT.search(last):
        cause = (f"dnf could not work out the Fedora {version} upgrade: some installed package has no "
                 f"Fedora {version} build and would have to be removed (dnf says --allowerasing). "
                 "Fix it can show which one.")
    else:
        first = errors[0] if errors else last
        cause = f"dnf5 system-upgrade download exited {rc}" + (f": {first[:200]}" if first else ".")
    res.update(status="fail", cause=cause)
    em.emit("release", "fail", None, cause, None, error=(errors[0] if errors else last) or None)
    return res


def run_release_reboot(em: Emitter, version: int) -> int:
    """`dnf5 system-upgrade reboot`: the one command here that restarts the machine.

    Only with a downloaded transaction on disk (release.offline_staged), only as root through
    the polkit prompt, and only because the user clicked Restart now on the result screen."""
    import release
    if not release.offline_staged(version):
        msg = (f"Nothing to restart into: Fedora {version} is not downloaded (dnf5 has no offline "
               "transaction). Run the upgrade again.")
        em.done("fail", reboot_required=False, kmod_verified=None, summary=msg, cause=msg,
                release={"version": version, "downloaded": False, "rebooting": False})
        return 1
    prefix = root_prefix()
    cmd = prefix + ["dnf5", "system-upgrade", "reboot", "-y"]
    if prefix[:1] == ["pkexec"]:
        em.emit("release", "progress", None, "Asking for permission…", None)
    em.emit("release", "start", None, f"Restarting into the Fedora {version} installer…", " ".join(cmd))
    rc, last = stream_lines(cmd, em, "release", timeout=300)
    refusal = root_refusal(prefix, rc, last)
    if rc == 0:
        # the machine is going down; the app may never read this line
        em.done("ok", reboot_required=True, kmod_verified=None,
                summary=f"Restarting now to install Fedora {version}.",
                release={"version": version, "downloaded": True, "rebooting": True})
        return 0
    cause = (f"Not restarted: {refusal}." if refusal
             else f"dnf5 system-upgrade reboot exited {rc}" + (f": {last[:200]}" if last else "."))
    em.done("fail", reboot_required=True, kmod_verified=None, summary=cause, cause=cause,
            release={"version": version, "downloaded": True, "rebooting": False})
    return 1


def run_real(em: Emitter, plan_ids: list[str], *, resolve_only: bool,
             state: dict | None = None) -> int:
    state = load_state() if state is None else state
    items, missing = resolve_plan(plan_ids, state)
    for pid in missing:
        em.emit("install", "skip", None, f"{pid} is not in state.json — skipped.", None)
    if not items:
        em.done("fail", reboot_required=False, kmod_verified=None,
                summary="Nothing to do: no plan item matched state.json.",
                cause="None of the selected updates is in the current update list; check again.")
        return 1

    rel_items = [i for i in items if i.get("op") == "release"]      # the newer-Fedora row
    dnf_items = [i for i in items if i["source"] == "dnf" and i.get("op") != "release"]
    fp_items = [i for i in items if i["source"] == "flatpak"]
    fw_items = [i for i in items if i["source"] == "fwupd"]
    reboot = any(i.get("requires_reboot") for i in items)

    ok = True
    warnings: list[str] = []
    causes: list[str] = []
    error = None
    results: dict = {}
    kmod_verified = None
    kver, kmod_evr = kmod_plan(dnf_items, state)

    if dnf_items:
        d = apply_dnf(em, dnf_items, resolve_only=resolve_only, reboot=reboot,
                      kver=kver, kmod_evr=kmod_evr)
        results["dnf"] = d["verify"]
        error = d.get("error")
        if d["status"] == "fail":
            ok = False
            causes.append(d["cause"])
        elif d["status"] == "warn":
            warnings.append(d["warning"])
        if kver and resolve_only:
            em.emit("akmod", "skip", None,
                    "--resolve-only: skipping the akmod wait and verify.", None)
        elif kver:
            # Runs on dnf's word being ok OR fail: what matters is whether the new
            # kernel / driver is on disk, because that decides if a reboot is safe.
            # verify.missing is real whatever dnf said (always verified since 2026-09-22),
            # so "landed" means the new kernel / driver is actually in the rpmdb now.
            touched = kmod_touched(dnf_items, d["verify"]["missing"])
            if touched:
                # apply_dnf may have waited already (between its two transactions):
                # its verdict stands, so a lost-lock retry never runs twice.
                waited = d.get("akmod_ok")
                if waited is None:
                    waited = wait_for_akmod(em, kver, kmod_evr)
                why = None
                if waited:
                    kmod_verified, why = verify_kmod(em, kver, kmod_evr)
                else:
                    kmod_verified = False
                if not kmod_verified:
                    ok = False
                    causes.append("The NVIDIA kernel module for the new kernel could not be "
                                  "verified — do not reboot until it is."
                                  + (f" {why}" if why else ""))
            else:
                em.emit("akmod", "skip", None,
                        "The new kernel/driver was not installed, so there is no module to "
                        "build; the current kernel and its NVIDIA module are untouched.", None)

    # 2026-11-01 abuse pass: the rpm transaction's password prompt was cancelled or denied. The
    # person said no, and the cause says "nothing was installed", so stop here: no Flatpak, no
    # firmware (fwupd and system Flatpaks would put up another prompt right away).
    if causes and causes[0].startswith("The administrator password prompt was cancelled") and (fp_items or fw_items):
        for it in fp_items + fw_items:
            em.emit(it["source"], "skip", None,
                    f"{it.get('display_name') or it['name']} skipped: the password prompt was cancelled, "
                    "so Rivet stopped there.", None)
        results["stopped_after_refusal"] = len(fp_items) + len(fw_items)
        fp_items, fw_items = [], []
    fp_res = {"ok": 0, "failed": 0, "skipped": 0, "failed_names": []}
    nv_version = new_nvidia_version(dnf_items, results.get("dnf"))
    if nv_version:
        # A driver upgrade drags its Flatpak runtime along, in this run, selected or not.
        # A miss here is a warning, never a failure: the host itself is fine.
        warnings += apply_nvidia_runtime(em, nv_version, fp_res, resolve_only=resolve_only,
                                         skip_refs={i["ref"] for i in fp_items})
    if fp_items:
        before = len(fp_res["failed_names"])
        apply_flatpak(em, fp_items, fp_res)
        if fp_res["failed_names"][before:]:
            ok = False
            names = fp_res["failed_names"][before:]
            whys = [fp_res.get("reasons", {}).get(n) for n in names]
            causes.append("Flatpak could not update " + ", ".join(names[:4])
                          + (f": {whys[0]}." if len(names) == 1 and whys[0] else "."))
    if fp_items or fp_res["ok"] or fp_res["skipped"] or fp_res["failed_names"]:
        results["flatpak"] = fp_res
    if fw_items:
        results["fwupd"] = apply_fwupd(em, fw_items)
        if results["fwupd"]["failed"]:
            ok = False
            causes.append("fwupd could not stage "
                          + ", ".join(results["fwupd"]["failed_names"][:4]) + ".")
    release_res = None
    if rel_items:
        # last of all, and never on top of a failed rpm transaction
        v = rel_items[0].get("release", {}).get("version") or rel_items[0].get("to")
        dnf_failed = bool(dnf_items) and (results.get("dnf") or {}).get("installed", 0) < len(dnf_items)
        if not ok or dnf_failed:
            em.emit("release", "skip", None,
                    f"Fedora {v} was not downloaded: the updates before it did not all go through. "
                    "Fix those first, then run the upgrade on its own.", None)
            release_res = {"version": v, "downloaded": False, "status": "skip", "disabled_repos": [],
                           "size": None, "packages": None, "nvidia": akmod_installed(),
                           "cause": "the updates before it did not all go through"}
        else:
            release_res = apply_release(em, rel_items[0], resolve_only=resolve_only)
            if release_res["status"] == "fail":
                ok = False
                causes.append(release_res["cause"])
        results["release"] = release_res

    parts = []
    if dnf_items:
        v = results["dnf"]
        parts.append(f"{len(dnf_items)} package(s)"
                     if resolve_only or v["installed"] == v["planned"]
                     else f"{v['installed']} of {v['planned']} package(s)")
    if "flatpak" in results:
        parts.append(f"{results['flatpak']['ok']} Flatpak(s)")
    if fw_items:
        parts.append(f"{results['fwupd']['ok']} firmware update(s)")
    if release_res and release_res["downloaded"]:
        parts.append(f"Fedora {release_res['version']} downloaded")
    # 2026-11-01 abuse pass: "a reboot is needed" only for what actually landed (a cancelled
    # password prompt planned a kernel, installed nothing, and still said "restart to finish")
    if reboot and not resolve_only:
        gone = set((results.get("dnf") or {}).get("missing") or [])
        reboot = any(i.get("requires_reboot") and (
            (i.get("op") == "release" and release_res is not None and release_res["downloaded"])
            or (i["source"] == "dnf" and i.get("op") != "release" and i["name"] not in gone)
            or (i["source"] == "fwupd" and (results.get("fwupd") or {}).get("ok"))
            or i["source"] == "flatpak") for i in items)
    status = "fail" if not ok else "warn" if warnings else "ok"
    if resolve_only:
        summary = "Resolved (nothing installed): " + ", ".join(parts) + "."
    elif status == "fail":
        summary = "Finished with errors: " + ", ".join(parts) + ". " + " ".join(causes)
        v = results.get("dnf") or {}
        if v.get("missing"):
            summary += " Not installed: " + ", ".join(v["missing"][:8]) + (
                f" and {len(v['missing']) - 8} more." if len(v["missing"]) > 8 else ".")
        if error:
            summary += f" First error: {error.rstrip('.')}."
    else:
        summary = "Applied " + ", ".join(parts) + "."
        if warnings:
            summary += " " + " ".join(warnings)
    if release_res and release_res["downloaded"] and status != "fail":
        summary += f" Restart to install Fedora {release_res['version']}."
    elif reboot and status != "warn":
        summary += " A reboot is needed to finish."
    extra = {}
    if release_res is not None:
        extra["release"] = {k: release_res[k] for k in ("version", "downloaded", "status", "size", "nvidia")}
    em.done(status, reboot_required=reboot, kmod_verified=kmod_verified, summary=summary,
            cause=(causes[0] if causes else warnings[0] if warnings else None),
            error=error, warnings=warnings, results=results, **extra)
    return 0 if ok else 1


# ---------------------------------------------------------------- dry run


def run_dry(em: Emitter, plan_ids: list[str], *, fast: bool = False) -> int:
    """A realistic fake stream for the UI engineer. Touches nothing."""
    pause = (lambda s: None) if fast else time.sleep
    state = load_state()
    items, _ = resolve_plan(plan_ids, state) if plan_ids else ([], [])
    if not items:
        items = [
            {"id": "dnf:kernel", "source": "dnf", "name": "kernel",
             "display_name": "kernel", "to": "7.2.5-200.fc44",
             "ref": "kernel-7.2.5-200.fc44.x86_64", "requires_reboot": True,
             "size_bytes": 148_000_000, "blocked_by": None},
            {"id": "dnf:akmod-nvidia", "source": "dnf", "name": "akmod-nvidia",
             "display_name": "akmod-nvidia", "to": "3:615.71.09-3.fc44",
             "ref": "akmod-nvidia-615.71.09-3.fc44.x86_64", "requires_reboot": True,
             "size_bytes": 46_101, "blocked_by": None},
            {"id": "dnf:curl", "source": "dnf", "name": "curl",
             "display_name": "curl", "to": "8.18.0-10.fc44",
             "ref": "curl-8.18.0-10.fc44.x86_64", "requires_reboot": False,
             "size_bytes": 245_961, "blocked_by": None},
            {"id": "flatpak:runtime/org.freedesktop.Platform/x86_64/25.08",
             "source": "flatpak", "name": "org.freedesktop.Platform",
             "display_name": "Freedesktop Platform", "to": "25.08.18",
             "ref": "runtime/org.freedesktop.Platform/x86_64/25.08",
             "requires_reboot": False, "size_bytes": 259_300_000,
             "blocked_by": "steam_running"},
            {"id": "flatpak:app/md.obsidian.Obsidian/x86_64/stable",
             "source": "flatpak", "name": "md.obsidian.Obsidian",
             "display_name": "Obsidian", "to": "1.9.15",
             "ref": "app/md.obsidian.Obsidian/x86_64/stable",
             "requires_reboot": False, "size_bytes": 118_400_000, "blocked_by": None},
            {"id": "fwupd:362301da643102b9f38477387e2193e57abaa590",
             "source": "fwupd", "name": "UEFI dbx", "display_name": "Secure Boot dbx",
             "to": "20260707", "ref": "362301da643102b9f38477387e2193e57abaa590",
             "requires_reboot": True, "size_bytes": 17_256, "blocked_by": None},
        ]

    rel_items = [i for i in items if i.get("op") == "release"]
    dnf_items = [i for i in items if i["source"] == "dnf" and i.get("op") != "release"]
    fp_items = [i for i in items if i["source"] == "flatpak"]
    fw_items = [i for i in items if i["source"] == "fwupd"]
    kver = "7.2.5-200.fc44.x86_64"
    needs_kmod = any(re.match(r"^(akmod|kmod)-nvidia", i["name"], re.I) for i in dnf_items)

    if dnf_items:
        total_mb = sum(i.get("size_bytes") or 0 for i in dnf_items) / 1e6
        em.emit("download", "start", 0,
                f"Preparing {len(dnf_items)} package(s), {total_mb:.0f} MB…",
                " ".join(i["name"] for i in dnf_items))
        for pct in (0, 8, 19, 31, 44, 58, 67, 79, 88, 96, 100):
            em.emit("download", "progress", pct, f"Downloading packages… {pct}%", None)
            pause(0.12)
        em.emit("download", "ok", 100, "All packages downloaded.", None)

        em.emit("install", "start", 0, f"Installing {len(dnf_items)} packages…", None)
        for n, it in enumerate(dnf_items, 1):
            pct = int(100 * n / max(1, len(dnf_items)))
            em.emit("install", "progress", pct, f"Upgrading {it['ref']}", it["ref"])
            pause(0.18)
        em.emit("install", "progress", 100, "Verifying… 100%", None)
        em.emit("install", "ok", 100, "RPM transaction complete.", None)

    kmod_verified = None
    if needs_kmod:
        em.emit("akmod", "start", 0,
                f"Waiting for the NVIDIA module to build for {kver}…", None)
        for pct, secs in ((12, 30), (34, 80), (58, 140), (81, 200)):
            em.emit("akmod", "progress", pct,
                    f"Building the NVIDIA module for {kver} ({secs}s)…",
                    f"/var/cache/akmods/nvidia/615.71.09-3-for-{kver}.log")
            pause(0.25)
        em.emit("akmod", "ok", 100, f"kmod-nvidia-{kver} is installed.", None)

        em.emit("verify", "start", 0, f"Verifying the NVIDIA module for {kver}…", None)
        em.emit("verify", "progress", 50, f"kmod-nvidia-{kver}: present", None)
        pause(0.15)
        em.emit("verify", "progress", 90, f"modinfo -k {kver} nvidia: ok", None)
        em.emit("verify", "ok", 100, "NVIDIA module verified for the new kernel.", None)
        kmod_verified = True

    for it in fp_items:
        if it.get("blocked_by"):
            em.emit("flatpak", "skip", None,
                    f"{it['display_name']} skipped — blocked by {it['blocked_by']}.", None)
            continue
        em.emit("flatpak", "start", 0, f"Updating {it['display_name']}…", it["ref"])
        for pct in (14, 46, 73, 100):
            em.emit("flatpak", "progress", pct,
                    f"Updating {it['display_name']}… {pct}%", None)
            pause(0.12)
        em.emit("flatpak", "ok", 100, f"{it['display_name']} updated.", None)

    for it in fw_items:
        em.emit("fwupd", "start", 0, f"Updating {it['display_name']}…", it["ref"])
        em.emit("fwupd", "progress", 40, "Writing firmware…", None)
        pause(0.15)
        em.emit("fwupd", "progress", 100, "Scheduled for the next boot.", None)
        em.emit("fwupd", "ok", 100,
                f"{it['display_name']} staged; it applies on the next boot.", None)

    release = None
    if rel_items:
        # the newer Fedora, last: a canned system-upgrade download (2026-11-03)
        v = (rel_items[0].get("release") or {}).get("version") or rel_items[0].get("to") or 45
        em.emit("release", "start", 0, f"Downloading Fedora {v}…", None)
        em.emit("release", "progress", None, "dnf5 system-upgrade download …",
                f"pkexec dnf5 system-upgrade download --releasever={v} -y")
        pause(0.2)
        em.emit("release", "progress", None, f"Fedora {v} is 3.1 GiB to download.",
                "Total size of inbound packages is 3.1 GiB. Need to download 3.1 GiB.")
        for n, name in ((120, "glibc-2.43-1.fc45.x86_64"), (900, "kernel-core-7.3.2-300.fc45.x86_64"),
                        (1834, "zlib-ng-compat-2.3.1-1.fc45.x86_64")):
            pause(0.2)
            em.emit("release", "progress", min(99, 100 * n // 1834), f"[{n:5d}/1834] {name}",
                    f"[{n:5d}/1834] {name}  100% | 12.4 MiB/s | 1.2 MiB | 00m00s")
        em.emit("release", "ok", 100, f"Fedora {v} is downloaded (3.1 GiB). Restart to install it.", None)
        release = {"version": v, "downloaded": True, "status": "ok", "size": "3.1 GiB", "nvidia": needs_kmod or akmod_installed()}

    reboot = any(i.get("requires_reboot") for i in items)
    parts = []
    if dnf_items:
        parts.append(f"{len(dnf_items)} package(s)")
    live_fp = [i for i in fp_items if not i.get("blocked_by")]
    if live_fp:
        parts.append(f"{len(live_fp)} Flatpak(s)")
    if fw_items:
        parts.append(f"{len(fw_items)} firmware update(s)")
    if release:
        parts.append(f"Fedora {release['version']} downloaded")
    summary = "Applied " + ", ".join(parts) + "."
    skipped = [i for i in fp_items if i.get("blocked_by")]
    if skipped:
        summary += f" {len(skipped)} skipped while Steam is running."
    if release:
        summary += f" Restart to install Fedora {release['version']}."
    elif reboot:
        summary += " A reboot is needed to finish."
    results = {}
    if release:
        results["release"] = release
    if dnf_items:
        results["dnf"] = {"planned": len(dnf_items), "installed": len(dnf_items),
                          "missing": [], "leftover": []}
    if fp_items:
        results["flatpak"] = {"ok": len(live_fp), "failed": 0, "skipped": len(skipped),
                              "failed_names": []}
    if fw_items:
        results["fwupd"] = {"ok": len(fw_items), "failed": 0, "failed_names": []}
    em.done("ok", reboot_required=reboot, kmod_verified=kmod_verified, summary=summary,
            results=results, **({"release": release} if release else {}))
    return 0


# ---------------------------------------------------------------- main
RESTART_TAG = "ha:_restart"     # elsewhere_apply.RESTART_ID, without importing it here


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rivet-apply", description=__doc__)
    ap.add_argument("--plan", metavar="FILE", help="plan.json with {'items': [ids]}")
    ap.add_argument("--dry-run", action="store_true",
                    help="emit a realistic fake event stream; change nothing")
    ap.add_argument("--fast", action="store_true",
                    help="with --dry-run, skip the pacing sleeps")
    ap.add_argument("--resolve-only", action="store_true",
                    help="resolve the dnf transaction and stop (installs nothing)")
    ap.add_argument("--try-held", metavar="REF", help="unpin a held-back ref and update it (trial)")
    ap.add_argument("--verify-held", metavar="REF",
                    help="watch for the game to be launched (never launches it); keep or roll back")
    ap.add_argument("--keep-held", metavar="REF", help="the trial works: release the hold")
    ap.add_argument("--rollback-held", metavar="REF",
                    help="the trial is broken: previous commit, re-pin, mark the version bad")
    ap.add_argument("--force", action="store_true", help="with --try-held: try a known-bad build")
    ap.add_argument("--timeout", type=float, default=240,
                    help="with --verify-held: seconds to wait for a launch (default 240)")
    ap.add_argument("--verify-kmod", nargs="?", const="", metavar="KVER",
                    help="only re-run the NVIDIA module check for KVER (default: the newest "
                         "installed kernel); changes nothing")
    ap.add_argument("--other-update", metavar="ID",
                    help="update one Other-software row of state.json (user level, no root)")
    ap.add_argument("--other-remove", metavar="ID",
                    help="uninstall one UNUSED Home Assistant add-on row of state.json")
    ap.add_argument("--ha-restart", nargs="?", const=RESTART_TAG, metavar="TAG",
                    help="restart Home Assistant core, only when an installed integration waits for "
                         "it (HACS restart_required); TAG is the bridge's row tag and is ignored")
    ap.add_argument("--other-fix", metavar="ID",
                    help="Fix it on a failed Other-software row: a bounded claude -p diagnosis "
                         "(with --dry-run: canned evidence, prompt + tool budget asserted, no claude)")
    ap.add_argument("--release-reboot", type=int, metavar="VERSION",
                    help="restart into the downloaded Fedora VERSION upgrade (dnf5 system-upgrade "
                         "reboot, as root); the ONE command here that reboots, on the user's click")
    ap.add_argument("--fix-tool", nargs=2, metavar=("VERB", "ID"),
                    help="one tool of a Fix it run (state|logs|retry|restart|reload); only inside it")
    args = ap.parse_args(argv)

    if args.fix_tool:
        import other_fix
        return other_fix.tool_main(args.fix_tool[0], args.fix_tool[1])

    em = Emitter()
    if args.other_update:
        import elsewhere_apply
        return elsewhere_apply.run(em, args.other_update, load_state())
    if args.other_remove:
        import elsewhere_apply
        return elsewhere_apply.run(em, args.other_remove, load_state(), remove=True)
    if args.ha_restart:
        import elsewhere_apply
        return elsewhere_apply.run_ha_restart(em, load_state())
    if args.other_fix:
        import other_fix
        return other_fix.run(em, args.other_fix, load_state(), dry=args.dry_run)
    if args.verify_kmod is not None:
        return run_verify_kmod(em, args.verify_kmod or None)
    if args.release_reboot is not None:
        return run_release_reboot(em, args.release_reboot)
    if args.try_held or args.verify_held or args.keep_held or args.rollback_held:
        import held_ops
        import holds
        action, ref = next((a, r) for a, r in (("try", args.try_held), ("verify", args.verify_held),
                                                ("keep", args.keep_held), ("rollback", args.rollback_held)) if r)
        try:
            if args.try_held:
                return held_ops.try_held(em, args.try_held, force=args.force, state=load_state())
            if args.verify_held:
                return held_ops.verify_held(em, args.verify_held, timeout=args.timeout)
            if args.keep_held:
                return held_ops.keep_held(em, args.keep_held)
            return held_ops.rollback_held(em, args.rollback_held)
        except holds.RegistryUnreadable as exc:
            # holds.json is damaged: nothing was changed, and it must not be overwritten
            msg = str(exc)
            em.done("fail", reboot_required=False, kmod_verified=None, summary=msg, cause=msg,
                    held={"ref": ref, "action": action, "result": "failed"})
            return 1
    plan_ids: list[str] = []
    if args.plan:
        try:
            doc = json.loads(Path(args.plan).read_text())
            plan_ids = list(doc.get("items") or [])
        except Exception as exc:
            em.done(False, reboot_required=False, kmod_verified=None,
                    summary=f"Cannot read plan {args.plan}: {exc}")
            return 2

    if args.dry_run:
        return run_dry(em, plan_ids, fast=args.fast)
    if not plan_ids:
        em.done(False, reboot_required=False, kmod_verified=None,
                summary="--plan with a non-empty items list is required (or use --dry-run).")
        return 2
    return run_real(em, plan_ids, resolve_only=args.resolve_only)


if __name__ == "__main__":
    sys.exit(main())
