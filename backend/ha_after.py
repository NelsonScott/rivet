"""After a Home Assistant core install: does everything that worked before still work?

rivet-apply takes a snapshot before it asks Home Assistant to install (every entity's state,
every integration's load state), lets Home Assistant settle once it is back, takes another and
compares. Read-only: three GETs, never a service call. The answer joins the run's summary and
the row's note; it never turns a finished install into a failure, because the new version IS
running either way. Added 2026-11-15: "it said a bunch of breaking changes, but is everything
we rely on working?" was a question the owner had to ask by hand.
"""
from __future__ import annotations

import os
import time

SETTLE_S = float(os.environ.get("RIVET_HA_SETTLE_S", "90"))        # integrations keep loading after RUNNING
SETTLE_POLL_S = float(os.environ.get("RIVET_HA_SETTLE_POLL_S", "10"))
DEAD = ("unavailable", "unknown")
BAD_ENTRY = ("setup_error", "setup_retry", "migration_error", "failed_unload")
SHOWN = 4


def snapshot(ctx, base: str, auth: dict) -> dict | None:
    """{states: {entity_id: state}, entries: {"domain|title": [state, reason]}} or None."""
    try:
        states = ctx.http_json(f"{base}/api/states", headers=auth, timeout=20) or []
        entries = ctx.http_json(f"{base}/api/config/config_entries/entry", headers=auth, timeout=20) or []
    except Exception:  # noqa: BLE001 - best effort: no snapshot means no comparison, never a failed install
        return None
    return {"states": {str(s.get("entity_id")): str(s.get("state")) for s in states if isinstance(s, dict)},
            "entries": {f"{e.get('domain')}|{e.get('title') or ''}": [str(e.get("state")), e.get("reason")]
                        for e in entries if isinstance(e, dict) and not e.get("disabled_by")}}


def compare(before: dict, after: dict) -> dict:
    sb, sa, eb, ea = before["states"], after["states"], before["entries"], after["entries"]
    dead = sorted(e for e, v in sa.items() if v in DEAD and e in sb and sb[e] not in DEAD)
    gone = sorted(e for e in sb if e not in sa and sb[e] not in DEAD)
    failed = sorted([k.split("|")[0], v[0], str(v[1] or "")[:120]] for k, v in ea.items()
                    if v[0] in BAD_ENTRY and (eb.get(k) or [None])[0] not in BAD_ENTRY)
    doms_b, doms_a = {k.split("|")[0] for k in eb}, {k.split("|")[0] for k in ea}
    return {"entities": len(sb), "integrations": len(eb), "dead": dead, "gone": gone, "failed": failed,
            "integrations_gone": sorted(doms_b - doms_a), "integrations_new": sorted(doms_a - doms_b)}


def problems(d: dict) -> int:
    return len(d["dead"]) + len(d["gone"]) + len(d["failed"])


def _some(names: list[str]) -> str:
    return ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN} more" if len(names) > SHOWN else "")


def line(d: dict | None) -> str:
    """One sentence for the summary and the row."""
    if d is None:
        return "Rivet could not compare before and after (Home Assistant did not hand over its entity list)."
    moved = ""
    if d["integrations_gone"] or d["integrations_new"]:
        moved = (" Integrations changed by the update: "
                 + "; ".join(p for p in (("gone " + _some(d["integrations_gone"])) if d["integrations_gone"] else "",
                                         ("new " + _some(d["integrations_new"])) if d["integrations_new"] else "") if p) + ".")
    if not problems(d):
        return (f"Checked after the restart: all {d['entities']} entities and {d['integrations']} integrations "
                f"that worked before still do.{moved}")
    parts = []
    if d["failed"]:
        parts.append("did not load: " + _some([f"{dom} ({why or st})" for dom, st, why in d["failed"]]))
    if d["dead"]:
        parts.append(f"{len(d['dead'])} now unavailable: " + _some(d["dead"]))
    if d["gone"]:
        parts.append(f"{len(d['gone'])} no longer there: " + _some(d["gone"]))
    return ("Checked after the restart, and something that worked before does not now; " + "; ".join(parts)
            + f". The other {max(0, d['entities'] - len(d['dead']) - len(d['gone']))} entities are fine.{moved}")


def settle_and_compare(ctx, base: str, auth: dict, before: dict | None, *, sleep=time.sleep, emit=None) -> dict | None:
    """Wait for integrations to finish loading (the count of problems stops falling, or SETTLE_S is
    up), then the comparison. None when either snapshot is missing."""
    if before is None:
        return None
    if emit:
        emit("Home Assistant is back. Checking that what worked before still works…")
    t0, best, same = time.monotonic(), None, 0
    while True:
        now = snapshot(ctx, base, auth)
        d = compare(before, now) if now else None
        if d is not None:
            if not problems(d):
                return d
            same = same + 1 if best is not None and problems(d) >= problems(best) else 0
            best = d if best is None or problems(d) <= problems(best) else best
        if time.monotonic() - t0 >= SETTLE_S or (same >= 2 and time.monotonic() - t0 >= SETTLE_S / 2):
            return best
        sleep(SETTLE_POLL_S)
