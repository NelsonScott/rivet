#!/usr/bin/env python3
"""steam_check: the Steam-only re-check (2026-11-10).

    python3 backend/steam_check.py [--wait-gone SECONDS]

Whether Steam runs decides one flag (`flags.steam_running`, the red banner) and which Flatpak
runtime rows are `blocked_by: "steam_running"`. Only the hourly collector used to compute it, so
a Steam quit or started since then showed stale for up to an hour, and Quit Steam re-ran the
whole collector (dnf, Flatpak, every Other software source: 21 s) to learn one fact.

This asks `flatpak ps` (milliseconds) and re-derives only what Steam decides, through the same
rules.apply_rules the collector uses: on copies of the rows, so a colour the model raised on an
unrelated row stays. A row whose block changed takes the copy's badge / risk / selected_default,
plus the verdict the collector would have given it (the triage cache when AI triage is on, else
triage.deterministic; never a Claude call). The flags `steam_running` and `blocked_count` follow.

Writes state.json (tmp + fsync + rename) only when something changed, and never touches
`checked_at` or `generated_at` (that stays the last full check; `steam_checked_at` says when this
ran). Does nothing while a collector's `checking` stub is on disk, and gives up when state.json
changed between its read and its write.

`--wait-gone S` (after the app's `flatpak kill`): poll until Steam has left `flatpak ps`, at most
S seconds, then re-derive with whatever the last probe said.

Prints one JSON line: {"steam_running": bool|null, "changed": bool, "wrote": bool,
"skipped": null|"no-state"|"checking"|"probe-failed"|"raced", "waited_ms": int}. Exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules  # noqa: E402
import triage as triage_mod  # noqa: E402
from common import STATE_PATH, load_settings, log, now_iso, run, write_json_atomic  # noqa: E402

# the groups rivet-collect builds from apply_rules' items (held rows never went through it)
RULE_GROUPS = ("dnf", "flatpak", "fwupd")
POLL_S = 0.2


def probe() -> bool | None:
    """True / False from `flatpak ps`; None when it could not be asked (then nothing changes:
    the collector reads a failed probe as "not running", which would unblock every runtime)."""
    cp = run(["flatpak", "ps", "--columns=application"], timeout=10)
    if cp.returncode != 0:
        return None
    return rules.STEAM_REF in cp.stdout


def wait_gone(seconds: float, *, probe_fn=probe, sleep=time.sleep, clock=time.monotonic):
    """Poll until Steam is gone or `seconds` passed. Returns (last probe, waited ms)."""
    t0 = clock()
    while True:
        running = probe_fn()
        if running is not True or clock() - t0 >= seconds:
            return running, int((clock() - t0) * 1000)
        sleep(POLL_S)


def _verdict(item: dict, *, use_ai: bool, cache: dict) -> dict:
    """What rivet-collect would put on the row: the cached model verdict when AI triage is on
    (triage.triage reads the same cache first), else the deterministic one."""
    if use_ai:
        hit = cache.get(triage_mod._cache_key(item))
        if isinstance(hit, dict) and hit.get("verdict"):
            return {"verdict": hit["verdict"], "reasoning": hit.get("reasoning", ""),
                    "risk_raise": hit.get("risk_raise")}
    return triage_mod.deterministic(item)


def rederive(doc: dict, steam: bool, *, use_ai: bool = False, cache: dict | None = None) -> bool:
    """Bring `doc` in line with `steam` in place; True when anything changed."""
    cache = cache or {}
    items = [it for g in doc.get("groups") or [] if g.get("id") in RULE_GROUPS
             for it in g.get("items") or [] if isinstance(it, dict) and not it.get("held")]
    # apply_rules only ever SETS blocked_by (setdefault otherwise), so the copies start unblocked;
    # it also resets every colour, which is why only the rows whose block changed are copied back
    copies = [dict(it, blocked_by=None) for it in items]
    new_flags = rules.apply_rules(copies, steam_running=steam)
    changed = False
    for it, c in zip(items, copies):
        if (it.get("blocked_by") or None) == (c.get("blocked_by") or None):
            continue
        v = _verdict(c, use_ai=use_ai, cache=cache)
        it.update(blocked_by=c["blocked_by"], badge=c["badge"], selected_default=c["selected_default"],
                  risk=rules.raise_risk(c["risk"], v.get("risk_raise")),
                  verdict=v["verdict"], reasoning=v.get("reasoning", ""))
        changed = True
    flags = doc.setdefault("flags", {})
    for k in ("steam_running", "blocked_count"):
        if flags.get(k) != new_flags[k]:
            flags[k] = new_flags[k]
            changed = True
    return changed


def recheck(*, path: Path | None = None, wait: float = 0, probe_fn=probe, settings: dict | None = None,
            cache: dict | None = None, sleep=time.sleep, clock=time.monotonic) -> dict:
    path = Path(path or STATE_PATH)
    out = {"steam_running": None, "changed": False, "wrote": False, "skipped": None, "waited_ms": 0}
    if wait > 0:
        running, out["waited_ms"] = wait_gone(wait, probe_fn=probe_fn, sleep=sleep, clock=clock)
    else:
        running = probe_fn()
    out["steam_running"] = running
    if running is None:
        out["skipped"] = "probe-failed"
        log("steam check: flatpak ps failed; state.json left alone", quiet=True)
        return out
    # read AFTER the probe/wait, so the read-to-rename window stays a few milliseconds
    try:
        st = os.stat(path)
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        out["skipped"] = "no-state"
        return out
    if not isinstance(doc, dict) or doc.get("status") == "checking":
        out["skipped"] = "checking" if isinstance(doc, dict) else "no-state"
        return out          # a collector is running: it writes the truth itself
    s = settings if settings is not None else load_settings()
    use_ai = s.get("ai_triage") is True
    if not rederive(doc, running, use_ai=use_ai,
                    cache=cache if cache is not None else (triage_mod.load_cache() if use_ai else {})):
        return out
    out["changed"] = True
    doc["steam_checked_at"] = now_iso()
    try:
        now = os.stat(path)
    except OSError:
        now = None
    if now is None or (now.st_ino, now.st_mtime_ns, now.st_size) != (st.st_ino, st.st_mtime_ns, st.st_size):
        out["skipped"] = "raced"      # someone wrote state.json meanwhile; theirs stands
        log("steam check: state.json changed under it; not written", quiet=True)
        return out
    write_json_atomic(path, doc)
    out["wrote"] = True
    log(f"steam check: Steam {'running' if running else 'not running'}; "
        f"blocked_count={doc['flags']['blocked_count']} (state.json updated, checked_at kept)", quiet=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="steam_check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait-gone", type=float, default=0, metavar="SECONDS",
                    help="poll until Steam has left `flatpak ps`, at most this long")
    args = ap.parse_args(argv)
    try:
        res = recheck(wait=max(0.0, min(args.wait_gone, 30.0)))
    except Exception as exc:  # noqa: BLE001 - the app reads one line, whatever happened
        res = {"steam_running": None, "changed": False, "wrote": False,
               "skipped": f"error: {type(exc).__name__}: {exc}"[:200], "waited_ms": 0}
    print(json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
