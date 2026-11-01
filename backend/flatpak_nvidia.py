"""The NVIDIA driver runtime Flatpak apps need, and whether it is there.

A Flatpak app cannot use the host's NVIDIA userspace; it gets its own copy from
`org.freedesktop.Platform.GL.nvidia-<ver>` (and `GL32...` for 32-bit, i.e. Steam),
and the version must equal the host driver's EXACTLY. Flatpak picks the version from
the kernel module that is loaded right now (/sys/module/nvidia/version), so:

  * a run that upgrades akmod-nvidia / xorg-x11-drv-nvidia cannot get the new runtime
    from `flatpak update` -- the old module is still loaded, Flatpak sees nothing to do;
  * after the reboot the new module is loaded, the matching runtime is missing, and
    Flatpak apps fall back to no GPU (2026-09-21: Steam up, a game would not start).

So rivet-apply installs the matching runtime in the same run as the driver, and
rivet-collect lists it as pending whenever it is missing, so a miss heals itself.
Only installations that already carry some `GL.nvidia-*` runtime are touched, and only
the kinds (GL, GL32) they already have.
"""
from __future__ import annotations

import re
import subprocess

# dnf packages whose version IS the driver version
DRIVER_PKGS = ("xorg-x11-drv-nvidia", "xorg-x11-drv-nvidia-cuda-libs", "akmod-nvidia")

_REF_RE = re.compile(
    r"^(?:runtime/)?org\.freedesktop\.Platform\.(GL32|GL)\.nvidia-(\d+(?:-\d+)*)/([^/]+)/([^/]+)$"
)
DEFAULT_BRANCH = "1.4"
REMOTE = "flathub"


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def version_of(evr: str | None) -> str | None:
    """'3:615.71.09-3.fc44' -> '615.71.09'."""
    s = (evr or "").strip().split(":")[-1].split("-")[0]
    return s if re.match(r"^\d+(\.\d+)+$", s) else None


def tag(version: str) -> str:
    """'615.71.09' -> '615-71-09' (how the Flatpak ref spells it)."""
    return version.replace(".", "-")


def untag(t: str) -> str:
    return t.replace("-", ".")


def parse_ref(ref: str) -> dict | None:
    m = _REF_RE.match(ref or "")
    if not m:
        return None
    return {"kind": m.group(1), "tag": m.group(2), "arch": m.group(3), "branch": m.group(4)}


def make_ref(kind: str, version: str, arch: str = "x86_64", branch: str = DEFAULT_BRANCH) -> str:
    return f"runtime/org.freedesktop.Platform.{kind}.nvidia-{tag(version)}/{arch}/{branch}"


def host_driver_version() -> str | None:
    """The driver the host will run after its next boot: the installed rpm, else the
    loaded module. The rpm comes first on purpose -- between a driver upgrade and the
    reboot it is the rpm that says what is coming."""
    for pkg in ("xorg-x11-drv-nvidia", "akmod-nvidia"):
        cp = _run(["rpm", "-q", "--qf", "%{VERSION}\n", pkg], timeout=20)
        if cp.returncode == 0:
            v = version_of(cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "")
            if v:
                return v
    try:
        with open("/sys/module/nvidia/version") as fh:
            return version_of(fh.read())
    except OSError:
        return None


def installed(scope: str) -> list[dict]:
    """Every GL/GL32 nvidia runtime in one installation ('user' | 'system')."""
    cp = _run(["flatpak", "list", f"--{scope}", "--runtime", "--all", "--columns=ref"])
    out = []
    for line in cp.stdout.splitlines():
        p = parse_ref(line.strip())
        if p:
            out.append(p)
    return out


def missing(version: str | None) -> list[dict]:
    """[{scope, kind, ref, have:[versions]}] for each installation + kind that has an
    nvidia runtime, but not the one for `version`."""
    if not version:
        return []
    gaps = []
    for scope in ("user", "system"):
        have = installed(scope)
        for kind in ("GL", "GL32"):
            mine = [h for h in have if h["kind"] == kind]
            if not mine or any(h["tag"] == tag(version) for h in mine):
                continue
            newest = mine[-1]
            gaps.append({
                "scope": scope,
                "kind": kind,
                "ref": make_ref(kind, version, newest["arch"], newest["branch"]),
                "have": sorted({untag(h["tag"]) for h in mine}),
            })
    return gaps


def remote_has(scope: str, ref: str, remote: str = REMOTE) -> bool:
    """Flathub publishes a driver a little after NVIDIA/RPM Fusion do; ask first."""
    return _run(["flatpak", "remote-info", f"--{scope}", remote, ref], timeout=90).returncode == 0


def display_name(kind: str, version: str) -> str:
    return f"NVIDIA driver {version} for Flatpak apps" + (" (32-bit)" if kind == "GL32" else "")


def notes(version: str, have: list[str]) -> str:
    old = f" Installed now: {', '.join(have)}." if have else ""
    return (f"The host NVIDIA driver is {version}. Flatpak apps (Steam and its games) need the "
            f"Flatpak runtime of exactly that version, or they start without the GPU.{old} "
            "This installs the matching one; nothing is removed.")
