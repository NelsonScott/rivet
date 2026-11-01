"""Health checks: small, deterministic, read-only looks at things on this box that make an app
look broken when it is not, each with ONE fixed, user-level fix (no Claude, no root).

    rivet-doctor health               run the checks, write health.json, print it
    rivet-doctor health --fix ID      run that check's fix, check again, write health.json

Added 2026-11-01 (abuse pass before the public release):

* `doc-portal`: xdg-document-portal says it is active but `/run/user/<uid>/doc/by-app` is gone
  (its FUSE mount went stale after a crash). Every Flatpak started in that state dies in bwrap
  ("Can't find source path /run/user/<uid>/doc/by-app/<app>") and launchers swallow the error,
  so the app "does nothing". Fix: `systemctl --user restart xdg-document-portal.service`.

`health.json` (state dir) is its own file, never part of state.json, so a check can never race
the collector's state.json write: {"checked_at", "checks": [{id, title, detail, fix_label}]}
lists only the PROBLEMS found (empty = all fine). The fix argv comes from FIXES below, never
from a file or the UI. Overrides for tests / app QA: RIVET_RUN_DIR (default /run/user/<uid>),
RIVET_SYSTEMCTL (default systemctl).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from common import STATE_DIR, now_iso, run, write_json_atomic

HEALTH_PATH = STATE_DIR / "health.json"
CHECK_TIMEOUT_S = 5.0          # per check; a hung FUSE stat must not hold up a collect run

PORTAL_UNIT = "xdg-document-portal.service"
FIXES = {
    "doc-portal": ["--user", "restart", PORTAL_UNIT],
}


def run_dir() -> Path:
    return Path(os.environ.get("RIVET_RUN_DIR") or f"/run/user/{os.getuid()}")


def systemctl() -> str:
    return os.environ.get("RIVET_SYSTEMCTL") or "systemctl"


def _sc(*args: str, timeout: int = 5):
    return run([systemctl(), *args], timeout=timeout)


def unit_active(unit: str) -> bool:
    try:
        cp = _sc("--user", "is-active", unit)
    except Exception:  # noqa: BLE001 - no systemctl, no user manager: nothing to say
        return False
    return cp.returncode == 0 and cp.stdout.strip() == "active"


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:                # ENOTCONN on a dead FUSE mount
        return False


def check_doc_portal() -> dict | None:
    by_app = run_dir() / "doc" / "by-app"
    if _is_dir(by_app) or not unit_active(PORTAL_UNIT):
        # not running is fine: D-Bus activation starts a fresh one with its mount
        return None
    return {
        "id": "doc-portal",
        "title": "Flatpak apps may not start",
        "detail": (f"The document portal (xdg-document-portal) is running but {by_app} is missing, so a "
                   "Flatpak app started now fails without a word (bwrap: “Can't find source path”). "
                   "Restarting the portal fixes it; nothing else is touched."),
        "fix_label": "Restart the document portal",
    }


CHECKS = [("doc-portal", check_doc_portal)]


def run_checks(only: list[str] | None = None) -> dict:
    """Every check on its own daemon thread with a deadline; a check that hangs or raises
    reports nothing (it is a hint, never a reason to fail a run)."""
    found: dict[str, dict] = {}

    def one(cid, fn):
        try:
            r = fn()
        except Exception:  # noqa: BLE001
            r = None
        if r:
            found[cid] = r

    threads = []
    for cid, fn in CHECKS:
        if only and cid not in only:
            continue
        t = threading.Thread(target=one, args=(cid, fn), name=f"health-{cid}", daemon=True)
        t.start()
        threads.append(t)
    end = time.monotonic() + CHECK_TIMEOUT_S
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    return {"checked_at": now_iso(), "checks": [found[c] for c, _ in CHECKS if c in found]}


def save(doc: dict) -> None:
    try:
        write_json_atomic(HEALTH_PATH, doc)
    except OSError:
        pass


def fix(em, cid: str) -> int:
    """Run the fixed fix for one check, then check everything again."""
    if cid not in FIXES:
        msg = f"No fix called {cid!r}."
        em.done("fail", msg, cause=msg, health=None)
        return 2
    before = dict(CHECKS)[cid]()
    if before is None:
        doc = run_checks()
        save(doc)
        msg = "Nothing to fix: the check passes now."
        em.emit("health", "ok", 100, msg, None)
        em.done("ok", msg, health=doc, fixed=cid)
        return 0
    argv = [systemctl(), *FIXES[cid]]
    em.emit("health", "start", 0, f"{before['fix_label']}…", "$ systemctl " + " ".join(FIXES[cid]))
    try:
        cp = run(argv, timeout=30)
        rc, out = cp.returncode, (cp.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        rc, out = 127, str(exc)
    if out:
        em.emit("health", "progress", 50, None, out[-400:])
    still = before
    for _ in range(10):                      # the unit needs a moment to mount / come back
        still = dict(CHECKS)[cid]()
        if still is None:
            break
        time.sleep(0.5)
    doc = run_checks()
    save(doc)
    if rc == 0 and still is None:
        msg = {"doc-portal": "The document portal was restarted; Flatpak apps start again."}[cid]
        em.emit("health", "ok", 100, msg, None)
        em.done("ok", msg, health=doc, fixed=cid)
        return 0
    why = (f"systemctl exited {rc}" + (f": {out.splitlines()[-1][:160]}" if out else "")) if rc else \
        "the check still fails after the restart"
    msg = f"{before['fix_label']} did not help ({why})."
    em.emit("health", "fail", 100, msg, None)
    em.done("fail", msg, cause=msg, health=doc, fixed=None)
    return 1
