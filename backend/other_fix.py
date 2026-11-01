"""Fix it on a failed Other-software row (2026-11-01, CONTRACT.md "Fix it on a failed row").

After "Alexa Media Player: Home Assistant stopped without installing it" (2026-11-01), the
idea was to let Claude Code retry what it needs instead of a manual fix. So a failed row offers
**Fix it**, which runs ONE bounded diagnosis through `claude -p` (the subscription CLI, model
opus, never an API key, never --bare) with:

  * the row (name, versions, kind, entity), the apply.log excerpt of its last run, the last
    200 relevant Home Assistant log lines and the live facts (entity, HACS restart repairs,
    the version HA has LOADED), all in the prompt;
  * a tool budget of exact commands and nothing else (Bash allowlist, no wildcards, every
    other tool denied by the CLI itself):
        rivet-apply --fix-tool state   ID   read the live state again
        rivet-apply --fix-tool logs    ID   the relevant log lines again
        rivet-apply --fix-tool retry   ID   the same update once more, via rivet-apply --other-update
        rivet-apply --fix-tool restart ID   HA core restart via rivet-apply --ha-restart, which
                                              refuses unless a restart is pending  (HA integrations)
        rivet-apply --fix-tool reload  ID   reload that one integration's config entries (HA integrations)
    retry / restart / reload run at most once per Fix it run (marker files in the session
    dir). Nothing can uninstall or change anything else.

It answers {fixed, result, cause, actions}; `result` is the one line the row shows ("Fixed:
…" / "Not fixed: …"). Rivet then checks Home Assistant itself and never reports "Fixed"
for a row that is still not at its version. Every event lands in apply.log.

`rivet-apply --other-fix ID --dry-run` builds the same prompt and budget from a canned log
(backend/fixtures/fix-alexa.json, the 2026-11-01 story), asserts them, prints them, and
calls neither claude nor Home Assistant. `RIVET_FIX_CANNED=<file>` makes a real run use
canned evidence and canned tool answers (app QA, with a fake claude via RIVET_CLAUDE_BIN).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import elsewhere as E
import elsewhere_apply as EA
from common import LOG_DIR, STATE_PATH, now_iso

STEP = "fix"
MODEL = os.environ.get("RIVET_FIX_MODEL", "opus")
FALLBACK = [m for m in os.environ.get("RIVET_FIX_FALLBACK_MODELS", "sonnet").split(",") if m.strip()]
TIMEOUT_S = int(os.environ.get("RIVET_FIX_TIMEOUT", "1800"))     # a 20 min HA retry fits
MAX_TURNS = "14"
HA_LOG_LINES = 200
APPLY_LINES = 120
HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "fix-alexa.json"
SAFE_ID = re.compile(r"^[a-z-]+:[@A-Za-z0-9][@A-Za-z0-9._/:+-]{0,200}$")
FAILED = ("failed", "fix_failed")     # update failures (a failed Remove keeps its HA link)
MUTATING = ("retry", "restart", "reload")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

FIX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fixed", "result", "cause", "actions"],
    "properties": {
        "fixed": {"type": "boolean", "description": "true only when the live state shows the target version installed (and, for an integration, loaded)."},
        "result": {"type": "string", "description": "ONE plain sentence, max 160 characters, starting 'Fixed: ' or 'Not fixed: ': the cause and what you did."},
        "cause": {"type": "string", "description": "The cause in a few words, e.g. 'needed a Home Assistant restart', 'GitHub rate limit'."},
        "actions": {"type": "array", "items": {"type": "string"}, "description": "The tool commands you ran, in order (verb only, e.g. 'retry')."},
    },
}

FIX_SYSTEM = """You are Rivet's "Fix it" for ONE failed update on the user's Fedora PC (Rivet is their update app; Home Assistant runs on another machine on their network). You get the facts in the prompt and a few exact commands. Nothing else is allowed and the CLI denies anything else automatically: do not try other commands, do not add pipes, redirects, flags or `2>&1`, do not try to work around a denial.

Rules:
- Read the evidence first; most of the time it already answers the question. Use at most 6 commands.
- `retry` re-runs the same update ONCE (the tool refuses a second time). Retrying makes no sense for a rate limit or a missing token; say so instead.
- `restart` restarts Home Assistant core. Use it ONLY when the state or logs show a restart is pending: a HACS `restart_required` repair, "Restart of Home Assistant required", `restart_pending: true`, or a loaded version older than the downloaded one. The tool itself refuses otherwise. It takes one to three minutes; pass timeout 600000.
- `reload` reloads that one integration's config entries; only for an integration that is loaded at the right version but misbehaving.
- Never uninstall, remove, downgrade or change anything else, never suggest the user edit files.
- Common causes: HACS downloaded the new version but Home Assistant needs a restart to load it; Home Assistant restarted in the middle of an install; HACS rate limited by GitHub (say when to try again, from the reset time in the log, local time); the newest version moved while installing; Home Assistant unreachable.
- Done = the live state (run `state` after a change) shows the target version installed and, for an integration, loaded. Only then fixed=true.

Answer with the JSON object only. `result` is the single line shown in the row, e.g. "Fixed: HACS had downloaded 5.19.0 but Home Assistant needed a restart; restarted, now v5.19.0" or "Not fixed: GitHub rate limit, try after 3:40 PM"."""


class Refused(Exception):
    pass


# --------------------------------------------------------------------------- the tool budget
def tool_cmd() -> str:
    """How Claude invokes the tools: the installed rivet-apply when it is this file's sibling."""
    env = os.environ.get("RIVET_FIX_TOOL_CMD")
    if env:
        return env
    inst = Path.home() / ".local/bin/rivet-apply"
    try:
        if inst.resolve() == (HERE / "rivet_apply.py"):
            return str(inst)
    except OSError:
        pass
    return str(HERE / "rivet_apply.py")


def verbs_for(it: dict) -> list[str]:
    if it.get("source") == "ha" and it.get("kind") == "integration":
        return ["state", "logs", "retry", "restart", "reload"]
    return ["state", "logs", "retry"]


def budget(it: dict) -> tuple[list[str], list[str]]:
    """(the exact commands, the --allowedTools rules for them). Exact match: no `:*`."""
    base = tool_cmd()
    cmds = [f"{base} --fix-tool {v} {it['id']}" for v in verbs_for(it)]
    return cmds, [f"Bash({c})" for c in cmds]


def refusal(it: dict | None, item_id: str) -> str | None:
    if not SAFE_ID.match(item_id or ""):
        return f"refusing an odd row id: {item_id!r}"
    if not it:
        return f"{item_id} is not in the current list (check again first)."
    if (it.get("last_run") or {}).get("result") not in FAILED:
        return f"{it.get('name') or item_id} has no failed run to fix."
    return None


# --------------------------------------------------------------------------- evidence
def apply_excerpt(item_id: str, path: Path | None = None, limit: int = APPLY_LINES) -> list[str]:
    """The apply.log lines of this row's LAST run: back from its last `done` to the previous `done`."""
    path = path or (LOG_DIR / "apply.log")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 3 * 2 ** 20))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    parsed = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            ev = None
        parsed.append((ln, ev if isinstance(ev, dict) else None))
    end = next((i for i in range(len(parsed) - 1, -1, -1)
                if (parsed[i][1] or {}).get("step") == "done"
                and ((parsed[i][1] or {}).get("other") or {}).get("id") == item_id), None)
    if end is None:
        return []
    start = end
    while start > 0 and (parsed[start - 1][1] or {}).get("step") != "done":
        start -= 1
    return [ln[:600] for ln, _ in parsed[start:end + 1]][-limit:]


def _terms(it: dict, domain: str | None) -> list[str]:
    out = {"hacs", "restart"}
    if domain:
        out.add(domain.lower())
    if it.get("entity_id"):
        out.add(str(it["entity_id"]).lower())
    name = str(it.get("name") or "").lower()
    if name:
        out.add(name)
    return sorted(out)


def ha_log_lines(ctx: E.Ctx, base: str, auth: dict, it: dict, domain: str | None,
                 n: int = HA_LOG_LINES) -> list[str]:
    """The last `n` relevant lines of HA core's log since its last start (Supervisor
    `core/logs/latest`; /api/error_log is gone in 2026.x)."""
    terms = _terms(it, domain)
    try:
        text = ctx.http_text(f"{base}/api/hassio/core/logs/latest", headers=auth, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return [f"(Home Assistant's log could not be read: {E._why(exc)})"]
    keep = [ANSI.sub("", ln).rstrip() for ln in text.splitlines()]
    keep = [ln for ln in keep if ln and any(t in ln.lower() for t in terms)]
    return [ln[:400] for ln in keep[-n:]]


def live_facts(ctx: E.Ctx, it: dict) -> dict:
    """What is true right now, read-only."""
    if it.get("source") != "ha":
        return {"installed_now": EA.installed_now(it), "row_latest": it.get("latest")}
    base, token, auth = EA.ha_conn(ctx)
    out: dict = {"entity_id": it.get("entity_id"), "target": it.get("latest")}
    try:
        st = ctx.http_json(f"{base}/api/states/{it['entity_id']}", headers=auth, timeout=10) or {}
        a = st.get("attributes") or {}
        out["entity"] = {"state": st.get("state"), **{k: a.get(k) for k in (
            "installed_version", "latest_version", "in_progress", "update_percentage", "skipped_version",
            "supported_features")},
            "release_summary": re.sub(r"<[^>]+>", "", str(a.get("release_summary") or ""))[:300] or None}
    except Exception as exc:  # noqa: BLE001
        out["entity"] = f"unreadable: {E._why(exc)}"
    try:
        cfg = ctx.http_json(f"{base}/api/config", headers=auth, timeout=10) or {}
        out["home_assistant"] = {"version": cfg.get("version"), "state": cfg.get("state"),
                                 "safe_mode": cfg.get("safe_mode"), "recovery_mode": cfg.get("recovery_mode")}
    except Exception as exc:  # noqa: BLE001
        out["home_assistant"] = f"unreachable: {E._why(exc)}"
    try:
        ws = ctx.ha_ws(base, token)
        try:
            issues = EA.restart_issues(ws)
            dom = EA.row_domain(it, issues)
            out["domain"] = dom
            out["restart_required_repairs"] = issues
            if it.get("kind") == "integration":
                out["loaded_version"] = EA.loaded_version(ws, dom)
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001
        out["repairs"] = f"unreadable: {E._why(exc)}"
    return out


def gather(it: dict, *, ctx: E.Ctx | None = None) -> dict:
    canned = canned_doc()
    if canned is not None:
        return {"apply_log": canned.get("apply_log") or [], "ha_log": canned.get("ha_log") or [],
                "live": canned.get("live") or {}, "canned": True}
    ctx = ctx or E.Ctx(env=os.environ)
    ev = {"apply_log": apply_excerpt(it["id"]), "ha_log": [], "live": {}, "canned": False}
    try:
        ev["live"] = live_facts(ctx, it)
    except EA.Refused as exc:
        ev["live"] = {"error": str(exc)}
    if it.get("source") == "ha":
        try:
            base, _t, auth = EA.ha_conn(ctx)
            ev["ha_log"] = ha_log_lines(ctx, base, auth, it, (ev["live"] or {}).get("domain"))
        except EA.Refused as exc:
            ev["ha_log"] = [f"({exc})"]
    return ev


def canned_doc() -> dict | None:
    p = os.environ.get("RIVET_FIX_CANNED")
    if not p:
        return None
    return json.loads(Path(p).read_text())


# --------------------------------------------------------------------------- payload
ROW_KEYS = ("id", "source", "name", "kind", "installed", "latest", "severity", "entity_id", "status",
            "restart_pending", "where", "command", "last_run")


def build_payload(it: dict, ev: dict) -> dict:
    """{prompt, cmds, allowed, verbs}: everything claude gets. Unit-tested and --dry-run-asserted."""
    cmds, allowed = budget(it)
    row = {k: it.get(k) for k in ROW_KEYS if it.get(k) is not None}
    tools = "\n".join(f"  {c}" + {"state": "   # live state again (read-only)",
                                   "logs": "    # the relevant log lines again (read-only)",
                                   "retry": "   # the same update ONCE more, through rivet-apply",
                                   "restart": " # restart Home Assistant core; refuses unless a restart is pending",
                                   "reload": "  # reload this one integration's config entries"}[c.split(" --fix-tool ")[1].split()[0]]
                      for c in cmds)
    ha_log = ev.get("ha_log") or []
    prompt = (
        f"Rivet could not update {it.get('name')} ({it.get('installed')} -> {it.get('latest')}). "
        "Find out why, fix it with the commands below if you can, and report.\n\n"
        f"## The row\n{json.dumps(row, indent=1)}\n\n"
        f"## What Rivet did (apply.log, the last run of this row)\n"
        + ("\n".join(ev.get("apply_log") or []) or "(nothing logged)") + "\n\n"
        + (f"## Home Assistant log, the last {len(ha_log)} relevant lines (since its last start)\n"
           + ("\n".join(ha_log) or "(no matching lines)") + "\n\n" if it.get("source") == "ha" else "")
        + f"## Live state right now\n{json.dumps(ev.get('live') or {}, indent=1)[:4000]}\n\n"
        f"## Your commands (run EXACTLY as written; nothing else is allowed)\n{tools}\n"
    )
    return {"prompt": prompt, "cmds": cmds, "allowed": allowed, "verbs": verbs_for(it)}


GUARD = HERE / "fix_guard.py"


def guard_settings() -> str:
    """Inline settings for the CLI: a PreToolUse hook on every tool (fix_guard.py). Claude
    Code auto-approves read-only shell commands (cat, ls, grep ...) whatever the allowlist,
    so the hook is what actually holds the budget (verified 2026-11-01)."""
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"{sys.executable} {GUARD}"}]}]}})


def claude_kw(payload: dict) -> dict:
    return {"schema": FIX_SCHEMA, "system": FIX_SYSTEM, "tool_names": "Bash", "allowed": payload["allowed"],
            "max_turns": MAX_TURNS, "extra": ["--strict-mcp-config", "--settings", guard_settings()],
            "fallback": FALLBACK}


def session_env(session: str) -> dict:
    # the CLI's Bash tool times a command out at 2 min by default; a retry or a restart needs longer
    return {"RIVET_FIX_SESSION": session, "BASH_DEFAULT_TIMEOUT_MS": "900000",
            "BASH_MAX_TIMEOUT_MS": "1500000",
            **({"RIVET_FIX_CANNED": os.environ["RIVET_FIX_CANNED"]} if os.environ.get("RIVET_FIX_CANNED") else {}),
            **({"RIVET_STATE_DIR": os.environ["RIVET_STATE_DIR"]} if os.environ.get("RIVET_STATE_DIR") else {})}


def dry_checks(it: dict, ev: dict, payload: dict, argv: list[str], env: dict) -> list[tuple[bool, str]]:
    """What --dry-run asserts (and the unit tests with it)."""
    p, out = payload["prompt"], []
    chk = lambda ok, what: out.append((bool(ok), what))  # noqa: E731
    want = ["state", "logs", "retry", "restart", "reload"] if (it.get("source") == "ha" and it.get("kind") == "integration") \
        else ["state", "logs", "retry"]
    chk(payload["verbs"] == want, f"tool budget is {'/'.join(want)}")
    chk(payload["allowed"] == [f"Bash({tool_cmd()} --fix-tool {v} {it['id']})" for v in want],
        "each tool is one exact command for this row")
    chk(not any("*" in a for a in payload["allowed"]), "no wildcard in the allowlist")
    chk(argv[argv.index("--tools") + 1] == "Bash", "Bash is the only tool (no Read/Edit/Write/WebFetch)")
    chk(argv[argv.index("--allowedTools") + 1] == ",".join(payload["allowed"]), "--allowedTools is exactly that budget")
    chk(argv[argv.index("--model") + 1] == "opus", "model opus")
    chk("--bare" not in argv, "never --bare (it skips the subscription login)")
    chk(argv[argv.index("--permission-prompts") + 1] == "none", "anything else is denied, not asked")
    chk("--strict-mcp-config" in argv and argv[argv.index("--setting-sources") + 1] == "", "no MCP servers, no settings files")
    try:
        hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]
        guarded = hook["matcher"] == "*" and str(GUARD) in hook["hooks"][0]["command"]
    except Exception:  # noqa: BLE001
        guarded = False
    chk(guarded, "a PreToolUse hook (fix_guard.py) denies every other command, read-only ones too")
    import fix_guard
    chk(fix_guard.decide({"tool_name": "Bash", "tool_input": {"command": "cat ~/.config/secrets/sa-token"}}, payload["cmds"])
        and fix_guard.decide({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, payload["cmds"])
        and fix_guard.decide({"tool_name": "Bash", "tool_input": {"command": payload["cmds"][0] + " && touch /tmp/x"}}, payload["cmds"])
        and not fix_guard.decide({"tool_name": "Bash", "tool_input": {"command": payload["cmds"][0]}}, payload["cmds"])
        and not fix_guard.decide({"tool_name": "StructuredOutput", "tool_input": {}}, payload["cmds"]),
        "the guard allows a budget command and the JSON answer, denies cat, Read and a chained command")
    chk(not any(k in env for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
        "no API key in claude's environment (subscription only)")
    for k in ("name", "installed", "latest"):
        chk(str(it.get(k)) in p, f"the prompt names the row's {k} ({it.get(k)})")
    if it.get("kind"):
        chk(f'"kind": "{it["kind"]}"' in p, f"the prompt has the kind ({it['kind']})")
    al = ev.get("apply_log") or []
    chk(al and all(line in p for line in al), f"the prompt carries the apply.log excerpt ({len(al)} lines)")
    if it.get("source") == "ha":
        hl = ev.get("ha_log") or []
        chk(0 < len(hl) <= HA_LOG_LINES and all(line in p for line in hl), f"the prompt carries {len(hl)} HA log lines (<= {HA_LOG_LINES})")
    chk(all(c in p for c in payload["cmds"]), "the prompt lists the exact commands")
    return out


# --------------------------------------------------------------------------- run
def run(em, item_id: str, state: dict, *, dry: bool = False, ctx: E.Ctx | None = None) -> int:
    import rivet_doctor as D
    items = {i.get("id"): i for i in ((state.get("elsewhere") or {}).get("items") or [])}
    it = items.get(item_id)
    if dry:
        if os.environ.get("RIVET_FIX_CANNED") is None:
            os.environ["RIVET_FIX_CANNED"] = str(FIXTURE)
        row = canned_doc().get("row") or {}
        if row.get("id") == item_id:
            it = row                      # the canned story, whatever the live row says now
    why = refusal(it, item_id)
    if why:
        em.done("fail", reboot_required=False, kmod_verified=None, summary=why, cause=why,
                other={"id": item_id, "result": "refused", "item": None})
        return 2
    em.emit(STEP, "start", 0, f"Gathering what happened to {it['name']}…", None)
    ev = gather(it, ctx=ctx)
    payload = build_payload(it, ev)
    em.emit(STEP, "progress", 15, f"Evidence: {len(ev['apply_log'])} apply.log lines, {len(ev['ha_log'])} "
            f"Home Assistant log lines{' (canned)' if ev.get('canned') else ''}.",
            "tools: " + " | ".join(payload["cmds"]))
    session = tempfile.mkdtemp(prefix="rivet-fix-")
    try:
        Path(session, "meta.json").write_text(json.dumps({"id": item_id, "verbs": payload["verbs"],
                                                          "cmds": payload["cmds"]}))
        env = session_env(session)
        if dry:
            argv = D.claude_argv(payload["prompt"], MODEL, **claude_kw(payload))
            checks = dry_checks(it, ev, payload, argv, dict(D.clean_env(), **env))
            for ok, what in checks:
                em.emit(STEP, "progress", None, ("ok: " if ok else "FAILED: ") + what, None)
            bad = [w for ok, w in checks if not ok]
            em.emit(STEP, "progress", 90, "prompt:", payload["prompt"][:6000])
            summary = (f"Dry run: {len(checks)} checks passed; claude was not called." if not bad
                       else f"Dry run: {len(bad)} of {len(checks)} checks FAILED: {'; '.join(bad)}")
            em.emit(STEP, "ok" if not bad else "fail", 100, summary, None)
            em.done("ok" if not bad else "fail", reboot_required=False, kmod_verified=None, summary=summary,
                    other={"id": item_id, "result": "dry_run", "item": None,
                           "fix": {"dry_run": True, "checks": len(checks), "failed": bad, "allowed_tools": payload["allowed"],
                                   "verbs": payload["verbs"], "model": MODEL, "prompt_chars": len(payload["prompt"])}})
            return 0 if not bad else 1
        try:
            ans = D.run_claude(em, payload["prompt"], MODEL, tools=True, timeout=TIMEOUT_S, step=STEP,
                               env=env, **claude_kw(payload))
        except Exception as exc:  # noqa: BLE001 - no usage left, CLI missing, timed out
            ran = sorted(p.stem for p in Path(session).glob("*.done"))
            msg = (f"Not fixed: {D.plain_claude_error(exc)}" +
                   (f"; it had run {', '.join(ran)}, see apply.log." if ran else "; nothing was changed."))
            return _finish(em, item_id, it, fixed=False, result=msg, fix={"error": str(exc)[:300], "ran": ran})
        ran = sorted(p.stem for p in Path(session).glob("*.done"))
    finally:
        shutil.rmtree(session, ignore_errors=True)
    fixed, result = bool(ans.get("fixed")), str(ans.get("result") or "").strip()
    if not re.match(r"^(Fixed|Not fixed):", result):
        result = ("Fixed: " if fixed else "Not fixed: ") + (result or ans.get("cause") or "no reason given")
    verified = None
    if fixed and not ev.get("canned"):
        verified, why_not = verify(it, ctx=ctx)
        if not verified:
            fixed, result = False, f"Not fixed: {why_not}"
    meta = ans.get("_meta") or {}
    return _finish(em, item_id, it, fixed=fixed, result=result[:240],
                   fix={"fixed": fixed, "result": result[:240], "claude_result": ans.get("result"),
                        "cause": ans.get("cause"), "actions": ans.get("actions") or [], "ran": ran,
                        "verified": verified, "model": meta.get("model"), "checks": meta.get("checks"),
                        "denied": meta.get("denied")})


def verify(it: dict, *, ctx: E.Ctx | None = None) -> tuple[bool, str]:
    """Rivet's own look after Claude says "fixed"."""
    target = it.get("latest")
    try:
        live = live_facts(ctx or E.Ctx(env=os.environ), it)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not confirm it ({E._why(exc)})"
    if it.get("source") != "ha":
        now = live.get("installed_now")
        return (bool(now) and not E.is_newer(now, target)), f"it still reports {now}, wanted {target}"
    ent = live.get("entity") if isinstance(live.get("entity"), dict) else {}
    have = ent.get("installed_version")
    if not have or E.is_newer(have, target):
        return False, f"Home Assistant still reports {have or 'nothing'}, wanted {target}"
    if it.get("kind") == "integration":
        loaded = live.get("loaded_version")
        if any(i.get("name") == it.get("name") for i in live.get("restart_required_repairs") or []) \
                or (loaded and EA._strip_v(loaded) != EA._strip_v(have)):
            return False, f"{have} is downloaded but Home Assistant still runs {loaded or 'the old one'} (restart pending)"
    return True, ""


def _finish(em, item_id: str, it: dict, *, fixed: bool, result: str, fix: dict) -> int:
    try:
        cur = EA.find_item(json.loads(STATE_PATH.read_text()), item_id)
    except Exception:  # noqa: BLE001
        cur = it
    row = dict(cur, last_run={"at": now_iso(), "result": "fixed" if fixed else "fix_failed", "note": result})
    if fixed:
        row["restart_pending"] = False
    EA.patch_state(row)
    em.emit(STEP, "ok" if fixed else "fail", 100, result, None)
    em.done("ok" if fixed else "fail", reboot_required=False, kmod_verified=None, summary=result,
            cause=None if fixed else result,
            other={"id": item_id, "result": "fixed" if fixed else "not_fixed", "item": row, "fix": fix})
    return 0 if fixed else 1


# --------------------------------------------------------------------------- the tools themselves
def tool_main(verb: str, item_id: str) -> int:
    """`rivet-apply --fix-tool VERB ID`: plain text for Claude. Only inside a Fix it run."""
    session = os.environ.get("RIVET_FIX_SESSION") or ""
    try:
        meta = json.loads(Path(session, "meta.json").read_text())
    except Exception:  # noqa: BLE001
        print("Refused: this only works inside a Rivet Fix it run.")
        return 2
    if meta.get("id") != item_id or verb not in (meta.get("verbs") or []):
        print(f"Refused: {verb} {item_id} is not in this Fix it run's tool budget.")
        return 2
    marker = Path(session, f"{verb}.done")
    if verb in MUTATING:
        if marker.exists():
            print(f"Refused: {verb} already ran once in this Fix it run; it does not run twice.")
            return 3
        marker.write_text(now_iso())
    canned = canned_doc()
    if canned is not None:
        tools = canned.get("tools") or {}
        if verb == "state" and Path(session, "restart.done").exists() and tools.get("state_after_restart"):
            verb = "state_after_restart"
        print(tools.get(verb) or f"(canned: no answer for {verb})")
        return 0
    try:
        it = EA.find_item(json.loads(STATE_PATH.read_text()), item_id)
    except Exception as exc:  # noqa: BLE001
        print(f"Refused: {exc}")
        return 2
    ctx = E.Ctx(env=os.environ)
    if verb == "state":
        print(json.dumps(live_facts(ctx, it), indent=1))
        return 0
    if verb == "logs":
        if it.get("source") == "ha":
            base, _t, auth = EA.ha_conn(ctx)
            issues = []
            print("\n".join(ha_log_lines(ctx, base, auth, it, EA.row_domain(it, issues))) or "(no matching lines)")
        else:
            print("\n".join(apply_excerpt(item_id)) or "(nothing in apply.log)")
        return 0
    if verb in ("retry", "restart"):
        argv = [sys.executable, str(HERE / "rivet_apply.py")] + (
            ["--other-update", item_id] if verb == "retry" else ["--ha-restart"])
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=1500, stdin=subprocess.DEVNULL)
        msgs, done = [], {}
        for ln in cp.stdout.splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("step") == "done":
                done = e
            elif e.get("msg"):
                msgs.append(e["msg"])
        print("\n".join(msgs[-15:]))
        o = done.get("other") or {}
        print(f"RESULT: {done.get('status')} ({o.get('result')}): {done.get('summary')}"
              + (" [restart_pending]" if o.get("restart_pending") else ""))
        return 0 if done.get("status") == "ok" else 1
    if verb == "reload":
        return _reload(ctx, it)
    print(f"Refused: unknown tool {verb}.")
    return 2


def _reload(ctx: E.Ctx, it: dict) -> int:
    if it.get("source") != "ha" or it.get("kind") != "integration":
        print("Refused: reload is only for a Home Assistant integration.")
        return 2
    base, token, auth = EA.ha_conn(ctx)
    ws = ctx.ha_ws(base, token)
    try:
        dom = EA.row_domain(it, EA.restart_issues(ws))
    finally:
        ws.close()
    if not dom:
        print(f"Refused: Rivet does not know which integration {it.get('name')} is.")
        return 2
    entries = ctx.http_json(f"{base}/api/config/config_entries/entry?domain={dom}", headers=auth, timeout=15) or []
    if not entries:
        print(f"No config entries for {dom}.")
        return 1
    rc = 0
    for e in entries:
        eid = str(e.get("entry_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", eid):
            continue
        try:
            r = ctx.http_json(f"{base}/api/config/config_entries/entry/{eid}/reload", headers=auth, data={}, timeout=60)
            print(f"reloaded {dom} entry {e.get('title') or eid}: {json.dumps(r)[:200]}")
        except Exception as exc:  # noqa: BLE001
            rc = 1
            print(f"reload of {dom} entry {e.get('title') or eid} failed: {E._why(exc)}")
    return rc


__all__ = ["run", "tool_main", "build_payload", "budget", "verbs_for", "refusal", "apply_excerpt",
           "dry_checks", "FIX_SCHEMA"]
