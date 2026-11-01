"""Hold registry: Flatpak refs pinned with `flatpak mask`, why, and what to do about it.

    ~/.local/state/rivet/holds.json
    {"version": 1, "holds": {"<full ref>": {scope, mask, display_name, reason, held_since,
        good: {version, commit}, bad_versions: [..], verify: {...}, history: [...],
        pending_trial: {from_commit, from_version, to_version, started_at, stage} | absent,
        notified_candidate: "<version>" | absent}},
     "released": [ ...holds whose trial was kept... ]}

2026-09-21: a gamescope build broke game launches; the previous one was rolled back by hand and
masked. A mask hides the ref from `flatpak update` forever, so without this file nobody would
ever learn that a fixed build shipped. The collector shows held refs in their own group, classifies the
available build (known_bad / candidate), and rivet-apply can try a candidate safely
(held_ops.py). A mask with no entry here still shows up, as "pinned" with no note.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import functools
import json
import re
import shutil
from pathlib import Path

import common
from common import now_iso, write_json_atomic

VERSION = 1
STALE_TRIAL_S = 24 * 3600
NO_NOTE = "Pinned with flatpak mask (no note)"

def path() -> Path:
    return common.STATE_DIR / "holds.json"


class RegistryUnreadable(RuntimeError):
    """holds.json exists but is not valid JSON. It is never overwritten: it holds the hold
    history and bad_versions, and a bare entry written over it would lose them."""


def _parse(raw: str) -> dict:
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


def _quarantine(p: Path, raw: str) -> str:
    """Keep a copy of an unreadable holds.json as holds.json.bad-<ts>, once per content."""
    for old in sorted(p.parent.glob(p.name + ".bad-*")):
        try:
            if old.read_text() == raw:
                return str(old)
        except OSError:
            pass
    bad = p.with_name(f"{p.name}.bad-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(p, bad)
    except OSError as exc:
        return f"(copy failed: {exc})"
    return str(bad)


def load() -> dict:
    """The registry; empty when the file is missing OR unreadable.

    Unreadable (2026-10-04): the pins are still shown (as "pinned", no note), a copy of the
    bad file is kept next to it, and save() refuses to touch it until it is fixed."""
    p = path()
    try:
        raw = p.read_text()
    except OSError:
        doc = {}
    else:
        try:
            doc = _parse(raw)
        except ValueError as exc:
            bad = _quarantine(p, raw)
            common.log(f"holds: {p} is not valid JSON ({exc}); copy kept at {bad}. Pins are shown "
                       "without their notes and nothing will be written to the registry until "
                       "the file is fixed or deleted.")
            doc = {}
    doc.setdefault("version", VERSION)
    if not isinstance(doc.get("holds"), dict):
        doc["holds"] = {}
    return doc


def save(doc: dict) -> None:
    """Atomic write; refuses when the file on disk exists but cannot be parsed."""
    p = path()
    if p.exists():
        try:
            _parse(p.read_text())
        except (OSError, ValueError) as exc:
            raise RegistryUnreadable(
                f"{p} is damaged ({exc}) and is not valid JSON, so nothing was changed: "
                f"overwriting it would lose the hold history. A copy is kept as "
                f"{p.name}.bad-<timestamp> in the same folder; fix or delete the file, then try again."
            ) from None
    write_json_atomic(p, doc)


def today() -> str:
    return _dt.date.today().isoformat()


def add_history(hold: dict, event: str, note: str = "", **extra) -> None:
    hold.setdefault("history", []).append({"date": today(), "event": event, "note": note, **extra})


# ---------------------------------------------------------------- versions


def _vkey(v: str) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"[._\-+:~]", v or "") if x != ""]


def vercmp(a: str, b: str) -> int:
    """3.16.9 < 3.16.28 < 3.16.30. Numeric fields compare as numbers."""
    ka, kb = _vkey(a), _vkey(b)
    for x, y in zip(ka, kb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return 1 if x > y else -1
        if isinstance(x, int) != isinstance(y, int):
            return 1 if isinstance(x, int) else -1      # 3.16.1 > 3.16.rc1
        return 1 if x > y else -1
    return (len(ka) > len(kb)) - (len(ka) < len(kb))


def max_version(versions: list[str]) -> str | None:
    vs = [v for v in versions if v]
    return max(vs, key=functools.cmp_to_key(vercmp)) if vs else None


def classify(hold: dict | None, installed: str, available: str | None) -> str:
    """known_bad | candidate | pinned | trial  (see CONTRACT.md, group `held`)."""
    if hold is None:
        return "pinned"
    if hold.get("pending_trial"):
        return "trial"
    if not available or not _vkey(available) or available == installed:
        return "pinned"                      # nothing newer on the remote (or no version string)
    bad = list(hold.get("bad_versions") or [])
    if available in bad:
        return "known_bad"
    worst = max_version(bad)
    if worst is not None and vercmp(available, worst) <= 0:
        return "known_bad"
    good = (hold.get("good") or {}).get("version")
    if good and vercmp(available, good) <= 0:
        return "pinned"                      # the remote went backwards; nothing to try
    return "candidate"


# ---------------------------------------------------------------- masks


def _ref_parts(ref: str) -> list[str]:
    parts = ref.split("/")
    if parts and parts[0] in ("app", "runtime"):
        parts = parts[1:]
    return (parts + ["", "", ""])[:3]


def mask_matches(ref: str, mask: str) -> bool:
    """Flatpak mask patterns are partial refs with globs: `id//branch`, `id`, `runtime/id/arch/branch`."""
    return all(fnmatch.fnmatchcase(v, pat or "*")
               for v, pat in zip(_ref_parts(ref), _ref_parts(mask)))


def is_masked(ref: str, masks: list[str]) -> bool:
    return any(mask_matches(ref, m) for m in masks)


def find(doc: dict, ref: str) -> tuple[str, dict] | tuple[None, None]:
    """Registry entry for a ref, tolerant of a missing app/ or runtime/ prefix."""
    holds = doc.get("holds") or {}
    if ref in holds:
        return ref, holds[ref]
    bare = "/".join(_ref_parts(ref))
    for key, hold in holds.items():
        if "/".join(_ref_parts(key)) == bare:
            return key, hold
    return None, None


def mask_for(ref: str) -> str:
    rid, _arch, branch = _ref_parts(ref)
    return f"{rid}//{branch}"


def trial_age_s(trial: dict) -> float:
    try:
        started = _dt.datetime.fromisoformat(trial.get("started_at"))
        return (_dt.datetime.now().astimezone() - started).total_seconds()
    except Exception:
        return float("inf")                  # unreadable date: treat as stale, never hide it


def trial_is_stale(trial: dict) -> bool:
    return trial_age_s(trial) > STALE_TRIAL_S


# ---------------------------------------------------------------- collector view


def held_item(ref: str, scope: str, hold: dict | None, *, installed: str, available: str | None,
              display_name: str, size_bytes: int = 0, origin: str = "flathub") -> dict:
    """One state.json item for the `held` group. Never selected, never counted."""
    from rules import flatpak_id

    state = classify(hold, installed, available)
    hold = hold or {}
    good = (hold.get("good") or {}).get("version") or installed
    verify = hold.get("verify") or {}
    name = hold.get("display_name") or display_name or flatpak_id(ref)
    trial = hold.get("pending_trial")
    to = available or installed
    if state == "known_bad":
        verdict = f"{to} is known bad; staying on {good}."
    elif state == "candidate":
        verdict = f"New build {to} is available; the pinned {good} still works. Try it when you have a minute."
    elif state == "trial":
        verdict = (f"Trial of {trial.get('to_version') or 'a new build'} was never confirmed. "
                   "Keep it or roll back.")
    else:
        verdict = f"Pinned at {installed}."
    item = {
        "id": f"flatpak:{ref}",
        "source": "flatpak",
        "name": flatpak_id(ref),
        "display_name": name,
        "from": installed,
        "to": to,
        "size_bytes": int(size_bytes or 0),
        "ref": ref,
        # never red: the top-bar badge shows a red dot for any red item, and a pin that is
        # doing its job is not an alarm
        "risk": "yellow" if state in ("candidate", "trial") else "green",
        "badge": None,
        "verdict": verdict,
        "reasoning": hold.get("reason") or NO_NOTE,
        "notes_excerpt": "",
        "notes_url": None,
        "advisory": None,
        "requires_reboot": False,
        "blocked_by": None,
        "selected_default": False,
        "scope": scope,
        "origin": origin,
        "held": True,
        "hold_state": state,
        "hold_reason": hold.get("reason") or NO_NOTE,
        "held_since": hold.get("held_since"),
        "good_version": good,
        "bad_versions": list(hold.get("bad_versions") or []),
        "verify_label": verify.get("label"),
    }
    if trial:
        item["trial"] = {
            "from_version": trial.get("from_version"),
            "to_version": trial.get("to_version"),
            "started_at": trial.get("started_at"),
            "stage": trial.get("stage"),
            "stale": trial_is_stale(trial),
        }
    return item


def candidate_notification(item: dict) -> tuple[str, str]:
    name, to, good = item["display_name"], item["to"], item.get("good_version") or item["from"]
    return (f"Rivet: {name} {to} is out",
            f"{name} {to} is out; the pinned {good} still works. "
            "Try it from Rivet when you have a minute.")


def mark_notified(ref: str, version: str) -> None:
    doc = load()
    key, hold = find(doc, ref)
    if hold is not None:
        hold["notified_candidate"] = version
        try:
            save(doc)
        except RegistryUnreadable as exc:        # a notify is not worth failing the run over
            common.log(f"holds: {exc}")


__all__ = ["load", "save", "classify", "vercmp", "held_item", "is_masked",
           "mask_matches", "find", "mask_for", "now_iso", "RegistryUnreadable"]
