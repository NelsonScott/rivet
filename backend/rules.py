"""Deterministic risk rules — applied before the triage model sees anything.

CONTRACT.md, "Rules the backend applies":
  red    : advisory severity critical/important, or blocked_by set
  yellow : requires_reboot, or name matches kernel*/akmod-*/kmod-*/*nvidia*/mesa*/
           systemd*/glibc, or a Flatpak runtime
  green  : otherwise (the model may raise a colour, never lower it)
"""
from __future__ import annotations

import re

RISK_ORDER = {"green": 0, "yellow": 1, "red": 2}

# --- yellow-by-name (dnf) ---
YELLOW_NAME_RE = re.compile(
    r"""^(
          kernel.*
        | akmod-.*
        | kmod-.*
        | .*nvidia.*
        | mesa.*
        | systemd.*
        | glibc
    )$""",
    re.X | re.I,
)

# --- Flatpak runtime refs (yellow, and Steam-blockable) ---
FLATPAK_RUNTIME_RE = re.compile(
    r"""(
          ^org\.freedesktop\.Platform.*
        | ^org\.gnome\.Platform.*
        | .*\.GL\..*
        | .*\.GL32\..*
        | .*\.Compat\..*
    )""",
    re.X,
)

# Packages whose replacement only takes effect after a reboot.
REBOOT_NAME_RE = re.compile(
    r"^(kernel.*|akmod-.*|kmod-.*|xorg-x11-drv-nvidia.*|nvidia-.*|systemd|systemd-libs|systemd-udev|glibc|dbus|dbus-broker)$",
    re.I,
)

SEVERE = {"critical", "important"}

STEAM_REF = "com.valvesoftware.Steam"


def flatpak_id(ref_or_id: str) -> str:
    """'runtime/org.freedesktop.Platform/x86_64/25.08' -> 'org.freedesktop.Platform'."""
    s = (ref_or_id or "").strip()
    if s.startswith(("runtime/", "app/")):
        s = s.split("/", 1)[1]
    return s.split("/", 1)[0]


def is_flatpak_runtime(ref_or_id: str) -> bool:
    return bool(FLATPAK_RUNTIME_RE.search(flatpak_id(ref_or_id)))


def severity_norm(sev) -> str | None:
    """dnf5 reports 'None' (a string) for 'no severity'."""
    if not sev:
        return None
    s = str(sev).strip().lower()
    return None if s in ("none", "null", "unspecified", "") else s


def requires_reboot(item: dict) -> bool:
    if item["source"] == "dnf":
        return bool(REBOOT_NAME_RE.match(item["name"]))
    if item["source"] == "fwupd":
        return bool(item.get("_needs_reboot"))
    return False


def base_risk(item: dict) -> str:
    """Deterministic colour. Assumes blocked_by/requires_reboot already set."""
    adv = item.get("advisory") or {}
    if severity_norm(adv.get("severity")) in SEVERE:
        return "red"
    if item.get("blocked_by"):
        return "red"
    if item.get("requires_reboot"):
        return "yellow"
    if item["source"] == "dnf" and YELLOW_NAME_RE.match(item["name"]):
        return "yellow"
    if item["source"] == "flatpak" and is_flatpak_runtime(item.get("ref") or item["name"]):
        return "yellow"
    return "green"


def is_security(item: dict) -> bool:
    adv = item.get("advisory") or {}
    return adv.get("type") == "security" or severity_norm(adv.get("severity")) in SEVERE


def badge(item: dict, *, akmod_pair: bool) -> str | None:
    """One badge per item; most-actionable wins.

    'blocked' first (nothing can happen), then the contract's explicit
    kernel+akmod pairing (rebuild/reboot is the actionable fact for those),
    then 'security', then a plain reboot requirement.
    """
    if item.get("blocked_by"):
        return "blocked"
    name = item["name"]
    if akmod_pair and item["source"] == "dnf":
        if re.match(r"^(akmod|kmod)-", name, re.I):
            return "rebuild"
        if name == "kernel" or name.startswith("kernel-"):
            return "reboot"
    if is_security(item):
        return "security"
    if item.get("requires_reboot"):
        return "reboot"
    return None


def raise_risk(current: str, proposed: str | None) -> str:
    """The model may raise a colour, never lower it."""
    if proposed not in RISK_ORDER:
        return current
    return proposed if RISK_ORDER[proposed] > RISK_ORDER[current] else current


def apply_rules(items: list[dict], *, steam_running: bool) -> dict:
    """Mutate items in place with blocked_by/requires_reboot/risk/badge; return flags."""
    for it in items:
        if (
            it["source"] == "flatpak"
            and steam_running
            and is_flatpak_runtime(it.get("ref") or it["name"])
            # adding a runtime nobody has loaded yet cannot disturb a running Steam
            and it.get("op") != "install"
        ):
            it["blocked_by"] = "steam_running"
        else:
            it.setdefault("blocked_by", None)
        it["requires_reboot"] = requires_reboot(it)

    dnf_names = {i["name"] for i in items if i["source"] == "dnf"}
    kernel_pending = any(n == "kernel" or n.startswith("kernel-") for n in dnf_names)
    akmod_nvidia_pending = any(
        re.match(r"^(akmod|kmod)-nvidia", n, re.I) for n in dnf_names
    )
    akmod_pair = kernel_pending and akmod_nvidia_pending

    for it in items:
        it["risk"] = base_risk(it)
        it["badge"] = badge(it, akmod_pair=akmod_pair)
        # kernel + akmod-nvidia pending together -> both are at least yellow
        if akmod_pair and it["source"] == "dnf" and re.match(
            r"^(kernel.*|akmod-.*|kmod-.*)$", it["name"], re.I
        ):
            it["requires_reboot"] = True
            it["risk"] = raise_risk(it["risk"], "yellow")
        it["selected_default"] = not it.get("blocked_by")

    return {
        "steam_running": steam_running,
        "kernel_pending": kernel_pending,
        "akmod_nvidia_pending": akmod_nvidia_pending,
        "reboot_required": any(i.get("requires_reboot") for i in items),
        "blocked_count": sum(1 for i in items if i.get("blocked_by")),
        "security_count": sum(1 for i in items if is_security(i)),
    }
