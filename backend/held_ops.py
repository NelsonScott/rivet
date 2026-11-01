"""Try / verify / keep / roll back a held (masked) Flatpak ref. Driven by rivet-apply:

    rivet-apply --try-held <ref> [--force]      unpin, update, leave a pending trial on disk
    rivet-apply --verify-held <ref> [--timeout 240]   watch (never launch) the game; then keep or roll back
    rivet-apply --keep-held <ref>               the new build works: pin stays off, hold released
    rivet-apply --rollback-held <ref>           back to the recorded commit, re-pin, remember the bad version

The trial lives in holds.json (`pending_trial`), written BEFORE the mask comes off, so a crash
or reboot at any point leaves something Keep / Roll back can act on. Nothing here ever starts
Steam or a game, and nothing rolls back without either a failed verification or the user's click.

Privilege: plain `flatpak` as the user goes through flatpak-system-helper + polkit, like every
other Flatpak step in rivet-apply. One thing the helper refuses outright is `update --commit=`
("Can't update to a specific commit without root permissions"), so the rollback runs through
`sudo -n` when that works without a password (never interactive) and `pkexec` otherwise. When
`sudo -n` is available the mask changes use it too, so an unattended rollback needs no prompt.
"""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

import holds
from common import LineStream, now_iso, run

STEAM_APP = "com.valvesoftware.Steam"
COREDUMP_RE = re.compile(r"Process \d+ \((gamescope[^)]*)\).*dumped core", re.I)
QUICK_EXIT_S = 20
POLL_S = float(os.environ.get("RIVET_HELD_POLL_S", "1.0"))


# ---------------------------------------------------------------- system seams
# Everything that touches the machine is a module-level function resolved through PATH, so the
# tests put fake flatpak / pgrep / journalctl / sudo in front and run the real CLI.


def root_prefix() -> list[str] | None:
    if os.geteuid() == 0:
        return []
    cp = run(["sudo", "-n", "true"], timeout=10)
    return ["sudo", "-n"] if cp.returncode == 0 else None


def flatpak_info(scope: str, ref: str) -> dict:
    cp = run(["flatpak", "info", f"--{scope}", ref], timeout=60)
    out = {"version": "", "commit": "", "ok": cp.returncode == 0}
    for line in cp.stdout.splitlines():
        k, _, v = line.strip().partition(":")
        if k == "Version":
            out["version"] = v.strip()
        elif k == "Commit":
            out["commit"] = v.strip()
    return out


def list_masks(scope: str) -> list[str]:
    cp = run(["flatpak", "mask", f"--{scope}"], timeout=30)
    if cp.returncode != 0:
        return []
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip() and "No masked" not in ln]


def steam_running() -> bool:
    return STEAM_APP in run(["flatpak", "ps", "--columns=application"], timeout=30).stdout


def proc_alive(name: str) -> bool:
    """pgrep -x matches the 15-character comm, which is what a Proton game shows up as."""
    return bool(name) and run(["pgrep", "-x", name[:15]], timeout=10).returncode == 0


def stream(cmd: list[str], em, step: str, held: dict, *, timeout: int = 3600) -> int:
    em.emit(step, "progress", None, " ".join(c for c in cmd[:4] if not c.startswith("-")) + " …",
            " ".join(cmd), held=held)
    try:
        ls = LineStream(cmd, timeout=timeout)       # deadline holds while the child is silent
    except OSError as exc:
        em.emit(step, "progress", None, f"cannot run {cmd[0]}: {exc}", None, held=held)
        return 127
    for raw in ls.lines():
        for chunk in re.split(r"[\r\n]", raw):
            line = chunk.strip()
            if line:
                m = re.search(r"(\d{1,3})\s?%", line)
                em.emit(step, "progress", min(100, int(m.group(1))) if m else None,
                        line[:200], line[:500], held=held)
    if ls.timed_out:
        em.emit(step, "progress", None, f"{cmd[0]} timed out after {timeout}s and was killed.",
                None, held=held)
        return 124
    return ls.returncode if ls.returncode is not None else 1


def mask_cmd(scope: str, mask: str, *, remove: bool) -> list[str]:
    cmd = ["flatpak", "mask", f"--{scope}"] + (["--remove"] if remove else []) + [mask]
    if scope == "system":
        cmd = (root_prefix() or []) + cmd
    return cmd


def update_cmd(scope: str, ref: str, commit: str | None = None) -> list[str]:
    cmd = ["flatpak", "update", f"--{scope}", "-y", "--noninteractive"]
    if commit:
        cmd.append(f"--commit={commit}")
        if scope == "system":
            prefix = root_prefix()
            cmd = (["pkexec"] if prefix is None else prefix) + cmd
    return cmd + [ref]


# ---------------------------------------------------------------- shared


def _held(ref: str, hold: dict, **extra) -> dict:
    trial = hold.get("pending_trial") or {}
    out = {
        "ref": ref,
        "display_name": hold.get("display_name") or ref.split("/")[1],
        "label": (hold.get("verify") or {}).get("label"),
        "from_version": trial.get("from_version") or (hold.get("good") or {}).get("version"),
        "to_version": trial.get("to_version"),
    }
    out.update(extra)
    return out


def _finish(em, ok: bool, action: str, result: str, summary: str, held: dict, cause=None) -> int:
    held = dict(held, action=action, result=result)
    em.done("ok" if ok else "fail", reboot_required=False, kmod_verified=None,
            summary=summary, cause=cause, held=held)
    return 0 if ok else 1


def _load(ref: str):
    doc = holds.load()
    key, hold = holds.find(doc, ref)
    return doc, key, hold


def _state_item(ref: str, state: dict) -> dict | None:
    for g in state.get("groups", []):
        if g.get("id") == "held":
            for it in g.get("items", []):
                if it.get("ref") == ref:
                    return it
    return None


def _ensure_masked(em, step, scope, mask, held) -> bool:
    if mask in list_masks(scope):
        return True
    rc = stream(mask_cmd(scope, mask, remove=False), em, step, held)
    return rc == 0 and mask in list_masks(scope)


def busy_reason(hold: dict) -> str | None:
    """Why a trial cannot start right now, in the user's words; None when it can."""
    v = hold.get("verify") or {}
    if v.get("type") != "steam-game":
        return None
    label = v.get("label") or "the game"
    if proc_alive(v.get("process") or "") or proc_alive("gamescope") or proc_alive("gamescope-wl"):
        return f"Close {label} first."
    if steam_running():
        return ("Quit Steam first: the new build only loads when Steam starts, so a test now "
                "would still be running the old one.")
    return None


# ---------------------------------------------------------------- try


def try_held(em, ref: str, *, force: bool, state: dict) -> int:
    doc, key, hold = _load(ref)
    item = _state_item(ref, state) or {}
    if hold is None:
        # a bare `flatpak mask` with no note: adopt it so the trial has somewhere to live
        scope = item.get("scope") or "system"
        masks = [m for m in list_masks(scope) if holds.mask_matches(ref, m)]
        if not masks:
            return _finish(em, False, "try", "refused", f"{ref} is not held back.",
                           {"ref": ref}, cause="There is no pin on this ref.")
        key = ref
        hold = doc["holds"][key] = {
            "scope": scope, "mask": masks[0], "display_name": item.get("display_name") or "",
            "reason": holds.NO_NOTE, "held_since": None, "good": {}, "bad_versions": [],
            "verify": None, "history": []}
    ref, scope, mask = key, hold.get("scope") or "system", hold.get("mask") or holds.mask_for(key)
    name = hold.get("display_name") or ref
    held = _held(ref, hold)

    if hold.get("pending_trial"):
        t = hold["pending_trial"]
        return _finish(em, False, "try", "refused",
                       f"A trial of {name} {t.get('to_version') or ''} is already waiting for a "
                       "verdict. Keep it or roll it back first.", held)

    em.emit("trial", "start", 0, f"Checking {name}…", ref, held=held)
    info = flatpak_info(scope, ref)
    if not info["ok"] or not info["commit"]:
        return _finish(em, False, "try", "refused", f"Cannot read the installed {name}.", held,
                       cause="`flatpak info` did not report a commit, so there would be nothing "
                             "to roll back to.")
    to_version = item.get("to") if item.get("to") != item.get("from") else None
    hold_state = holds.classify(hold, info["version"], to_version)
    held.update(from_version=info["version"], to_version=to_version)
    if hold_state == "known_bad" and not force:
        return _finish(em, False, "try", "refused",
                       f"{name} {to_version} is marked bad; staying on {info['version']}.", held,
                       cause="Use “Try anyway” if you really want to test it again.")
    if not to_version and not force:
        return _finish(em, False, "try", "refused",
                       f"There is no newer build of {name} to try.", held)
    why = busy_reason(hold)
    if why:
        return _finish(em, False, "try", "refused", why, held, cause=why)

    hold["pending_trial"] = {
        "from_commit": info["commit"], "from_version": info["version"],
        "to_version": to_version, "started_at": now_iso(), "stage": "updating"}
    holds.save(doc)

    def abort(msg: str, cause: str) -> int:
        remasked = _ensure_masked(em, "trial", scope, mask, held)
        doc2, _k, h2 = _load(ref)
        h2.pop("pending_trial", None)
        holds.add_history(h2, "trial-failed", cause)
        holds.save(doc2)
        tail = (f" Still pinned at {info['version']}." if remasked else
                f" WARNING: could not put the pin back; run `flatpak mask --{scope} '{mask}'`.")
        em.emit("trial", "fail", None, msg + tail, None, held=held)
        return _finish(em, False, "try", "failed", msg + tail, held, cause=cause)

    em.emit("trial", "progress", 10, f"Removing the pin on {name}…", mask, held=held)
    rc = stream(mask_cmd(scope, mask, remove=True), em, "trial", held)
    if rc != 0 or mask in list_masks(scope):
        return abort(f"Could not remove the pin on {name} (rc={rc}).",
                     "The pin could not be removed, so nothing was updated.")

    em.emit("trial", "progress", 30, f"Updating {name} to {to_version or 'the newest build'}…",
            ref, held=held)
    rc = stream(update_cmd(scope, ref), em, "trial", held)
    after = flatpak_info(scope, ref)
    if rc != 0:
        if after["commit"] and after["commit"] != info["commit"]:
            # flatpak complained but the new build is deployed: that is a trial, not a failure
            em.emit("trial", "progress", None,
                    f"flatpak exited {rc}, but {after['version']} is installed; carrying on.",
                    None, held=held)
        else:
            return abort(f"The update of {name} failed (flatpak rc={rc}).",
                         "Flatpak could not install the new build.")
    elif after["commit"] == info["commit"]:
        return abort(f"Flatpak had nothing newer for {name}.",
                     "The update changed nothing, so there is nothing to test.")

    doc, _k, hold = _load(ref)
    hold["pending_trial"].update(stage="awaiting_verify", to_version=after["version"] or to_version,
                                 to_commit=after["commit"])
    holds.add_history(hold, "trial-started",
                      f"{info['version']} -> {hold['pending_trial']['to_version']}")
    holds.save(doc)
    held = _held(ref, hold)
    label = held["label"]
    msg = (f"{name} {held['to_version']} is installed. Now launch {label}. I'm watching."
           if label else f"{name} {held['to_version']} is installed. Test it, then keep it or roll back.")
    em.emit("trial", "awaiting_verify", 100, msg, None, held=held)
    return _finish(em, True, "try", "awaiting_verify", msg, held)


# ---------------------------------------------------------------- keep / rollback


def keep_held(em, ref: str, *, auto: bool = False) -> int:
    doc, key, hold = _load(ref)
    if hold is None or not hold.get("pending_trial"):
        return _finish(em, False, "keep", "refused", "There is no trial to keep.", {"ref": ref})
    ref, scope, mask = key, hold.get("scope") or "system", hold.get("mask") or holds.mask_for(key)
    trial = hold["pending_trial"]
    held = _held(ref, hold)
    name = held["display_name"]
    em.emit("keep", "start", 0, f"Keeping {name} {trial.get('to_version')}…", ref, held=held)
    pin_note = "Pin removed."
    if mask in list_masks(scope):          # crash between "pending" and "unmask": finish the job
        rc = stream(mask_cmd(scope, mask, remove=True), em, "keep", held)
        if rc != 0 or mask in list_masks(scope):
            pin_note = f"The pin is still there; remove it with `flatpak mask --{scope} --remove '{mask}'`."
    info = flatpak_info(scope, ref)
    hold["good"] = {"version": info["version"] or trial.get("to_version"),
                    "commit": info["commit"] or trial.get("to_commit")}
    hold.pop("pending_trial", None)
    hold["released_at"] = holds.today()
    holds.add_history(hold, "released",
                      f"{trial.get('from_version')} -> {hold['good']['version']} verified "
                      + ("by the watcher" if auto else "by the user"))
    doc.setdefault("released", []).append(dict(hold, ref=ref))
    del doc["holds"][ref]
    holds.save(doc)
    summary = f"{name} {hold['good']['version']} kept. {pin_note}"
    em.emit("keep", "ok", 100, summary, None, held=held)
    return _finish(em, True, "keep", "kept", summary, held)


def rollback_held(em, ref: str, *, reason: str = "", auto: bool = False) -> int:
    doc, key, hold = _load(ref)
    if hold is None or not hold.get("pending_trial"):
        return _finish(em, False, "rollback", "refused", "There is no trial to roll back.",
                       {"ref": ref})
    ref, scope, mask = key, hold.get("scope") or "system", hold.get("mask") or holds.mask_for(key)
    trial = hold["pending_trial"]
    held = _held(ref, hold)
    name, back_to = held["display_name"], trial.get("from_version") or "the previous build"
    em.emit("rollback", "start", 0, f"Rolling {name} back to {back_to}…", ref, held=held)

    now = flatpak_info(scope, ref)
    tried = trial.get("to_version") or now["version"]
    if now["commit"] != trial["from_commit"]:
        rc = stream(update_cmd(scope, ref, trial["from_commit"]), em, "rollback", held)
        now = flatpak_info(scope, ref)
        if now["commit"] != trial["from_commit"]:
            manual = " ".join(update_cmd(scope, ref, trial["from_commit"]))
            msg = (f"Could not roll {name} back (flatpak rc={rc}); it is still on "
                   f"{now['version'] or tried}. The trial is still open, so you can retry. By hand: {manual}")
            em.emit("rollback", "fail", None, msg, None, held=held)
            return _finish(em, False, "rollback", "failed", msg, held,
                           cause="Flatpak could not deploy the previous commit.")
    em.emit("rollback", "progress", 70, f"Pinning {name} at {back_to} again…", mask, held=held)
    pinned = _ensure_masked(em, "rollback", scope, mask, held)

    if tried and tried != trial.get("from_version") and tried not in (hold.get("bad_versions") or []):
        hold.setdefault("bad_versions", []).append(tried)
    hold.pop("pending_trial", None)
    holds.add_history(hold, "rolled-back",
                      f"{tried} failed ({reason or ('watcher' if auto else 'the user said it is broken')}); "
                      f"back on {back_to}")
    holds.save(doc)
    summary = (f"Rolled back to {back_to} and pinned again. {tried} is marked bad."
               if pinned else
               f"Rolled back to {back_to}, and {tried} is marked bad, but the pin could not be "
               f"put back: run `flatpak mask --{scope} '{mask}'`.")
    if steam_running():
        summary += " Restart Steam before you play, so it picks the old build up."
    em.emit("rollback", "ok" if pinned else "fail", 100, summary, None, held=held)
    return _finish(em, pinned, "rollback", "rolled_back", summary, held,
                   cause=reason or None)


# ---------------------------------------------------------------- verify (passive)


def _follow_journal(q: "queue.Queue[str]") -> subprocess.Popen | None:
    try:
        proc = subprocess.Popen(
            ["journalctl", "-f", "--since", "now", "-o", "cat", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            text=True, bufsize=1)
    except OSError:
        return None

    def pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()
    return proc


def watch(em, hold: dict, held: dict, timeout: float) -> tuple[str, str]:
    """('ok'|'fail'|'inconclusive', reason). Watches; never launches anything."""
    v = hold.get("verify") or {}
    label, process = v.get("label") or "the game", v.get("process") or ""
    Label = label[:1].upper() + label[1:]         # at the start of a sentence
    need = float(v.get("alive_seconds") or 75)
    quick = float(v.get("quick_exit_seconds") or QUICK_EXIT_S)
    patterns = [re.compile(p, re.I) for p in (v.get("fail_patterns") or [])]
    q: "queue.Queue[str]" = queue.Queue()
    follower = _follow_journal(q)
    if follower is None:
        em.emit("watch", "progress", None, "journalctl is unavailable; watching the process only.",
                None, held=held)
    em.emit("watch", "start", 0, f"Waiting for you to launch {label}…", None, held=held)
    deadline = time.monotonic() + timeout
    run_start = None
    quick_exits = 0
    last_tick = -1
    try:
        while True:
            while True:
                try:
                    line = q.get_nowait()
                except queue.Empty:
                    break
                m = COREDUMP_RE.search(line)
                if m:
                    return "fail", f"{m.group(1)} crashed (core dump)"
                for p in patterns:
                    if p.search(line):
                        return "fail", f"the journal says: {line.strip()[:160]}"
            now = time.monotonic()
            alive = proc_alive(process)
            if alive and run_start is None:
                run_start = now
                em.emit("watch", "progress", 1, f"{Label} is running…", None, held=held)
            elif alive:
                secs = now - run_start
                if secs >= need:
                    return "ok", f"{Label} stayed up for {int(need)} s"
                if int(secs) // 5 != last_tick:
                    last_tick = int(secs) // 5
                    em.emit("watch", "progress", min(99, int(100 * secs / need)),
                            f"{Label} is running, {int(secs)} s…", None, held=held)
            elif run_start is not None:
                lived = now - run_start
                run_start, last_tick = None, -1
                if lived < quick:
                    quick_exits += 1
                    if quick_exits >= 2:
                        return "fail", f"{Label} quit within {int(quick)} s of starting, twice"
                    em.emit("watch", "progress", None,
                            f"{Label} closed after {int(lived)} s. Launch it once more…", None,
                            held=held)
                else:
                    em.emit("watch", "progress", None,
                            f"{Label} closed after {int(lived)} s, before I could be sure. "
                            "Launch it again, or use the buttons.", None, held=held)
                deadline = max(deadline, now + min(timeout, 120))
            elif now > deadline:
                return "inconclusive", f"{Label} was not launched within {int(timeout)} s"
            time.sleep(POLL_S)
    finally:
        if follower is not None:
            follower.terminate()
            try:
                follower.wait(timeout=3)
            except Exception:
                follower.kill()


def verify_held(em, ref: str, *, timeout: float) -> int:
    doc, key, hold = _load(ref)
    if hold is None or not hold.get("pending_trial"):
        return _finish(em, False, "verify", "refused", "There is no trial to verify.", {"ref": ref})
    ref = key
    held = _held(ref, hold)
    name, to = held["display_name"], held["to_version"]
    v = hold.get("verify") or {}
    if v.get("type") != "steam-game" or not v.get("process"):
        msg = (f"No automatic check is set up for {name}. Test it yourself, then press "
               "“It works, keep it” or “It's broken, roll back”.")
        em.emit("watch", "skip", None, msg, None, held=held)
        return _finish(em, True, "verify", "inconclusive", msg, held)

    # The app stops the watcher with SIGTERM when the user presses Keep / Roll back.
    # Exit through `finally` so the journalctl follower does not outlive us.
    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(143))
    verdict, reason = watch(em, hold, held, timeout)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)     # a verdict is being acted on: finish it
    if verdict == "ok":
        em.emit("watch", "ok", 100, f"{reason}. Keeping {name} {to}.", None, held=held,
                verdict="ok")
        return keep_held(em, ref, auto=True)
    if verdict == "fail":
        em.emit("watch", "fail", None, f"{name} {to} failed: {reason}. Rolling back…", None,
                held=held, verdict="fail", reason=reason)
        return rollback_held(em, ref, reason=reason, auto=True)
    msg = (f"{reason[0].upper() + reason[1:]}. {name} {to} stays installed and the trial stays open: "
           "nothing was rolled back. Launch it later and check again, or use Keep / Roll back.")
    em.emit("watch", "skip", None, msg, None, held=held, verdict="inconclusive")
    return _finish(em, True, "verify", "inconclusive", msg, held)
