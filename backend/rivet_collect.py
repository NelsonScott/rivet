#!/usr/bin/env python3
"""rivet-collect — gather pending updates, triage them, write state.json.

    rivet-collect [--no-llm] [--quiet] [--notify] [--force-triage] [--fixture FILE]
                   [--no-elsewhere]

Notifications are OPT-IN: nothing is shown unless --notify is passed (the
systemd unit passes it) AND the pending set changed AND settings.notify is
true. RIVET_NO_NOTIFY=1 in the environment mutes it unconditionally, which is
what QA runs use so a test can never pop a window over a full-screen game.

Writes ~/.local/state/rivet/state.json atomically: first a stub with
status "checking", then the full document. Exit 0 even when nothing is pending.

SIGTERM / SIGINT / SIGHUP mid-run (systemd TimeoutStartSec, logout, Ctrl-C) write
`status: "error", error: "interrupted"` over the stub, carrying the previous groups,
and exit 128+signal (143 for SIGTERM). Every write of state.json is tmp + fsync +
rename, so a reader never sees a half-written document.

Elsewhere (2026-10-31, read-only): npm/pipx/uv/cargo/Home Assistant/podman/GNOME
extension/AppImage staleness runs on its own thread from the start (10 s per source),
lands as the top-level `elsewhere` object and never touches the groups, the counts or
the notification. If it is somehow still running when dnf/Flatpak/fwupd are done, the
document is written with the previous `elsewhere` first and again once it finishes.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import elsewhere as elsewhere_mod  # noqa: E402
import holds  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
import sources  # noqa: E402
import triage as triage_mod  # noqa: E402
from common import (  # noqa: E402
    LAST_SET_PATH,
    SCHEMA,
    STATE_PATH,
    ensure_dirs,
    kill_children,
    load_settings,
    log,
    now_iso,
    run,
    write_json_atomic,
)

GROUPS = [
    ("dnf", "System", "dnf"),
    ("flatpak", "Apps & runtimes", "Flatpak"),
    ("fwupd", "Firmware", "fwupd"),
]

ITEM_KEYS = [
    "id", "source", "name", "display_name", "from", "to", "size_bytes", "ref",
    "risk", "badge", "verdict", "reasoning", "notes_excerpt", "notes_url",
    "advisory", "requires_reboot", "blocked_by", "selected_default",
]


def host_facts() -> dict:
    os_name = "Fedora"
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
                break
    except Exception:
        pass
    sb = None
    cp = run(["mokutil", "--sb-state"], timeout=15)
    if cp.returncode == 0 and cp.stdout.strip():
        sb = "enabled" in cp.stdout.lower()
    if sb is None:
        try:
            for p in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
                sb = p.read_bytes()[-1] == 1
                break
        except Exception:
            pass
    return {
        "os": os_name,
        "kernel_running": platform.release(),
        "secure_boot": sb,
    }


def carry_doc(prev: dict | None, *, status: str, error: str | None, host: dict) -> dict:
    """A document that carries the previous groups/flags/summary (the app keeps its list
    on screen) under a new status: the `checking` stub, or an `error` on failure."""
    base = prev or {}
    doc = {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "checked_at": base.get("checked_at"),
        "status": status,
        "error": error,
        "host": host,
        "flags": base.get("flags")
        or {
            "steam_running": False, "kernel_pending": False,
            "akmod_nvidia_pending": False, "reboot_required": False,
            "blocked_count": 0, "security_count": 0,
        },
        "summary": base.get("summary") or {"total": 0, "total_bytes": 0},
        "groups": base.get("groups") or [
            {"id": g, "title": t, "subtitle": s, "items": []} for g, t, s in GROUPS
        ],
    }
    if base.get("elsewhere"):
        doc["elsewhere"] = base["elsewhere"]
    return doc


def write_checking(prev: dict | None, host: dict | None = None) -> None:
    """Stub document so the app/extension can show a spinner immediately."""
    write_json_atomic(STATE_PATH, carry_doc(prev, status="checking", error=None,
                                            host=host or host_facts()))


class Interrupt:
    """SIGTERM / SIGINT / SIGHUP while the `checking` stub is on disk.

    A signal never reaches the `except` in main(), so until 2026-10-04 the stub stayed on
    disk forever after a systemd timeout or a logout. The handler takes the children down
    (a hand-run collector must not orphan a dnf5), writes `status: "error", error:
    "interrupted"` atomically with the previous groups, and leaves through os._exit with
    128+signal (143 for SIGTERM): the worker threads may be blocked in a child wait, and a
    SystemExit would hang on the executor's shutdown. Disarmed once the final document
    is on disk, so a late signal can no longer replace a good result."""

    def __init__(self, prev: dict | None, host: dict, *, quiet: bool):
        self.prev, self.host, self.quiet = prev, host, quiet
        self.armed = True

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self)

    def __call__(self, signum, _frame) -> None:
        name = signal.Signals(signum).name
        try:
            kill_children()
        except Exception:
            pass
        if self.armed:
            try:
                write_json_atomic(STATE_PATH, carry_doc(self.prev, status="error",
                                                        error="interrupted", host=self.host))
            except Exception:
                pass
        log(f"collect interrupted by {name}: state.json set to status=error, error=interrupted",
            quiet=self.quiet)
        os._exit(128 + signum)


def read_prev() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return None


def load_fixture(path: str) -> dict:
    """QA hook: inject raw source output so the rules can be exercised
    deterministically even when the real box has nothing pending."""
    return json.loads(Path(path).read_text())


def collect_all(fixture: dict | None, *, quiet: bool):
    if fixture is not None:
        return (
            fixture.get("dnf", []),
            fixture.get("flatpak", []),
            fixture.get("fwupd", []),
            bool(fixture.get("steam_running", False)),
        )
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_dnf = ex.submit(sources.collect_dnf)
        f_fp = ex.submit(sources.collect_flatpak)
        f_fw = ex.submit(sources.collect_fwupd)
        f_st = ex.submit(sources.steam_running)
        return f_dnf.result(), f_fp.result(), f_fw.result(), f_st.result()


# Optional item keys (contract amendment 2026-09-21): written only when set.
OPTIONAL_ITEM_KEYS = ["op", "scope", "origin"]
# Group `held` (contract amendment 2026-09-21b): refs pinned with `flatpak mask`.
HELD_GROUP = ("held", "Held back", "pinned")
HELD_ITEM_KEYS = ["held", "hold_state", "hold_reason", "held_since", "good_version",
                  "bad_versions", "verify_label", "trial"]


def strip_private(item: dict) -> dict:
    out = {k: item.get(k) for k in ITEM_KEYS}
    for k in OPTIONAL_ITEM_KEYS:
        if item.get(k) is not None:
            out[k] = item[k]
    if item.get("held"):
        for k in HELD_ITEM_KEYS:
            if k in item:
                out[k] = item[k]
        out["selected_default"] = False
    out["size_bytes"] = int(out.get("size_bytes") or 0)
    out["notes_excerpt"] = (out.get("notes_excerpt") or "")[:600]
    return out


class ElsewhereRun:
    """The Elsewhere collectors on a background thread, started before dnf so a slow
    registry never holds up (or is held up by) the package check."""

    # the longest collector deadline + slack: the most a final join can wait
    JOIN_S = max(r[3] for r in elsewhere_mod.REGISTRY) + 5

    def __init__(self, prev: dict | None, *, quiet: bool):
        self.prev = (prev or {}).get("elsewhere")
        self.quiet = quiet
        self.doc: dict | None = None
        self.thread = threading.Thread(target=self._run, name="elsewhere", daemon=True)

    def _run(self) -> None:
        try:
            self.doc = elsewhere_mod.collect(self.prev)
            c = self.doc["counts"]
            log(f"elsewhere: stale={c['stale']} failed={c['failed']} cant_check={c['cant_check']} "
                f"({self.doc['duration_ms']} ms; "
                + ", ".join(f"{s['id']}={s['status']}" for s in self.doc["sources"]) + ")",
                quiet=self.quiet)
        except Exception as exc:  # noqa: BLE001 - never take the dnf result down with it
            log(f"elsewhere failed: {type(exc).__name__}: {exc}", quiet=False)

    def start(self) -> "ElsewhereRun":
        self.thread.start()
        return self

    def ready(self) -> bool:
        return not self.thread.is_alive()

    def wait(self) -> dict | None:
        self.thread.join(self.JOIN_S)
        return self.doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rivet-collect", description=__doc__)
    ap.add_argument("--no-llm", action="store_true", help="skip the triage model entirely")
    ap.add_argument("--quiet", action="store_true", help="no stderr chatter (for systemd)")
    ap.add_argument("--force-triage", action="store_true", help="ignore the verdict cache")
    ap.add_argument("--fixture", metavar="FILE", help="QA: read raw source data from FILE")
    ap.add_argument(
        "--notify",
        action="store_true",
        help="allow a desktop notification when the pending set changed (opt-in; "
             "the systemd unit passes this). RIVET_NO_NOTIFY=1 overrides it.",
    )
    ap.add_argument("--no-elsewhere", action="store_true",
                    help="skip the read-only Elsewhere sources (also RIVET_NO_ELSEWHERE=1)")
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    ensure_dirs()
    prev = read_prev()
    settings = load_settings()
    # Claude Code runs only when the user opted in (settings ai_triage, default off)
    use_ai = settings.get("ai_triage") is True and not args.no_llm
    host = host_facts()
    guard = Interrupt(prev, host, quiet=args.quiet)
    guard.install()

    try:
        write_checking(prev, host)
        fixture = load_fixture(args.fixture) if args.fixture else None
        # Elsewhere: never with a fixture (QA must not touch the network; a fixture may
        # carry a ready-made "elsewhere" object instead), never when switched off.
        ew = None
        if (fixture is None and not args.no_elsewhere
                and os.environ.get("RIVET_NO_ELSEWHERE") != "1"):
            ew = ElsewhereRun(prev, quiet=args.quiet).start()
        dnf_i, fp_i, fw_i, steam = collect_all(fixture, quiet=args.quiet)
        # Held-back refs are shown, never offered: not counted, not selected, not triaged,
        # not part of the pending set that drives the notification.
        held = [i for i in fp_i if i.get("held")]
        fp_i = [i for i in fp_i if not i.get("held")]
        items = dnf_i + fp_i + fw_i
        log(
            f"collected dnf={len(dnf_i)} flatpak={len(fp_i)} fwupd={len(fw_i)} held={len(held)} "
            f"steam_running={steam} ({time.monotonic()-t0:.1f}s)",
            quiet=args.quiet,
        )

        flags = rules.apply_rules(items, steam_running=steam)
        flags["held_count"] = len(held)
        flags["held_candidates"] = sum(1 for i in held if i.get("hold_state") == "candidate")

        if not use_ai:
            for it in items:
                d = triage_mod.deterministic(it)
                it["verdict"], it["reasoning"] = d["verdict"], d["reasoning"]
        else:
            verdicts = triage_mod.triage(items, quiet=args.quiet, force=args.force_triage)
            for it in items:
                v = verdicts.get(it["id"]) or triage_mod.deterministic(it)
                it["verdict"] = v["verdict"]
                it["reasoning"] = v.get("reasoning", "")
                # the model may raise a colour, never lower it
                it["risk"] = rules.raise_risk(it["risk"], v.get("risk_raise"))
            # a raise can create new reds; keep derived counts honest
            flags["blocked_count"] = sum(1 for i in items if i.get("blocked_by"))

        groups = [
            {
                "id": gid,
                "title": title,
                "subtitle": sub,
                "items": [strip_private(i) for i in items if i["source"] == gid],
            }
            for gid, title, sub in GROUPS
        ]
        groups.append({"id": HELD_GROUP[0], "title": HELD_GROUP[1], "subtitle": HELD_GROUP[2],
                       "items": [strip_private(i) for i in held]})

        if fixture is None and sources.REMOTE_ERRORS:
            # 2026-11-01: a Flatpak remote that could not be read is said, never "up to date"
            flags["flatpak_remote_errors"] = list(sources.REMOTE_ERRORS)
        elif fixture is not None and fixture.get("flatpak_remote_errors"):
            flags["flatpak_remote_errors"] = fixture["flatpak_remote_errors"]
        doc = {
            "schema": SCHEMA,
            "generated_at": now_iso(),
            "checked_at": now_iso(),
            "status": "ok",
            "error": None,
            "host": host,
            "flags": flags,
            "summary": {
                "total": len(items),
                "total_bytes": sum(int(i.get("size_bytes") or 0) for i in items),
            },
            "groups": groups,
        }
        # Elsewhere is additive and top-level: not a group, not counted, not notified.
        late = False
        if fixture is not None and isinstance(fixture.get("elsewhere"), dict):
            doc["elsewhere"] = fixture["elsewhere"]
        elif ew is not None and ew.ready() and ew.doc:
            doc["elsewhere"] = ew.doc
        elif ew is not None:
            late = True
            if (prev or {}).get("elsewhere"):
                doc["elsewhere"] = prev["elsewhere"]      # last run's, until this one lands
        write_json_atomic(STATE_PATH, doc)
        guard.armed = False          # the result is on disk; a late signal must not replace it
        # Health checks (2026-11-01, backend/health.py): a stat and a systemctl call each, their
        # own file (health.json), never a reason to fail the run; not with a fixture (QA).
        if fixture is None and os.environ.get("RIVET_NO_HEALTH") != "1":
            try:
                import health
                hdoc = health.run_checks()
                health.save(hdoc)
                if hdoc["checks"]:
                    log("health: " + ", ".join(c["id"] for c in hdoc["checks"]), quiet=args.quiet)
            except Exception as exc:  # noqa: BLE001
                log(f"health: {type(exc).__name__}: {exc}", quiet=args.quiet)
        if late and ew.wait():
            doc["elsewhere"] = ew.doc
            doc["generated_at"] = now_iso()
            write_json_atomic(STATE_PATH, doc)
            log("elsewhere finished after the main check; state.json written again", quiet=args.quiet)
        # Other software: the "what it is / used by" lines nothing else could fill come from
        # `claude -p`, once per name (know-cache.json), AFTER the result is on disk, with a
        # hard budget. A failure leaves the line empty; it never fails the run.
        if ew is not None and isinstance(doc.get("elsewhere"), dict) and use_ai:
            try:
                import know
                # stay inside the unit's TimeoutStartSec (1015 s): the AI pass gets what is left
                budget = min(know.AI_BUDGET_S, 950 - (time.monotonic() - t0))
                if budget >= 15 and know.fill_ai(doc["elsewhere"].get("items") or [], budget=budget,
                                                 log=lambda m: log(m, quiet=args.quiet)):
                    doc["generated_at"] = now_iso()
                    write_json_atomic(STATE_PATH, doc)
            except Exception as exc:  # noqa: BLE001
                log(f"know: {type(exc).__name__}: {exc}", quiet=args.quiet)

        # notify only when the pending SET changed (compare item ids)
        ids = sorted(i["id"] for i in items)
        try:
            last = json.loads(LAST_SET_PATH.read_text()).get("ids")
        except Exception:
            last = None
        changed = last is None or sorted(last) != ids
        write_json_atomic(LAST_SET_PATH, {"ids": ids, "at": now_iso()})

        muted = os.environ.get("RIVET_NO_NOTIFY") == "1"
        may_notify = args.notify and not muted and settings.get("notify", True)
        # A newer build of something pinned: say so once per version, whatever else changed.
        for h in held:
            if h.get("hold_state") != "candidate":
                continue
            _key, hold = holds.find(holds.load(), h["ref"])
            if hold is None or hold.get("notified_candidate") == h["to"]:
                continue
            title, body = holds.candidate_notification(h)
            if may_notify and notify.send(title, body, quiet=args.quiet):
                holds.mark_notified(h["ref"], h["to"])
                log(f"notified: {title} — {body}", quiet=args.quiet)
            else:
                log(f"held candidate {h['display_name']} {h['to']}: not notified "
                    f"(--notify={args.notify}, muted={muted})", quiet=args.quiet)

        if changed and items and may_notify:
            title, body = notify.build_body(items, flags)
            ok = notify.send(title, body, quiet=args.quiet)
            log(f"notified: {title} — {body} (ok={ok})", quiet=args.quiet)
        else:
            log(
                f"no notification (changed={changed}, items={len(items)}, "
                f"--notify={args.notify}, muted={muted}, "
                f"settings.notify={settings.get('notify', True)})",
                quiet=args.quiet,
            )

        log(
            f"done: {len(items)} pending, status=ok in {time.monotonic()-t0:.1f}s",
            quiet=args.quiet,
        )
        return 0

    except Exception as exc:  # never leave a half-written / stale-checking state
        import traceback

        log("collect failed:\n" + traceback.format_exc(), quiet=False)
        doc = carry_doc(prev, status="error", error=f"{type(exc).__name__}: {exc}"[:500], host=host)
        try:
            write_json_atomic(STATE_PATH, doc)
            guard.armed = False
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
