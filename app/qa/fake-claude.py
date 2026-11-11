#!/usr/bin/env python3
"""Stands in for `claude -p` in app QA (Fix it, 2026-11-01). Never calls a model.

Checks the command line the way the real one must look (opus, stream-json, no --bare, the
Fix it budget as exact --allowedTools, the guard hook in --settings), records it in
$RIVET_STATE_DIR/fake-claude-argv.json for qa-e2e.sh, then streams what a real run looks
like: read the state, restart Home Assistant (the canned log says a restart is pending),
read it again, and answer with the schema's JSON."""
import json
import os
import sys
import time

# 2026-11-01 abuse pass: FAKE_CLAUDE_MODE makes it fail the ways the real CLI does
# (logged-out | garbage | hang | max-turns | usage); unset = the happy Fix it story below.
# The Fix it run hands claude a clean environment, so the mode also comes from a file in the
# state dir (RIVET_STATE_DIR is part of the Fix it session env).
_mf = os.path.join(os.environ.get("RIVET_STATE_DIR") or "/nonexistent", "fake-claude-mode")
MODE = os.environ.get("FAKE_CLAUDE_MODE") or (open(_mf).read().strip() if os.path.exists(_mf) else None)
if MODE == "logged-out":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Invalid API key \u00b7 Please run /login"}), flush=True)
    sys.exit(1)
if MODE == "garbage":
    print("lorem ipsum <html>502 Bad Gateway</html>", flush=True)
    print("{not json either", flush=True)
    sys.exit(0)
if MODE == "hang":
    time.sleep(120)
    sys.exit(0)
if MODE == "max-turns":
    print(json.dumps({"type": "system", "subtype": "init", "model": "opus"}), flush=True)
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 14}), flush=True)
    sys.exit(0)
if MODE == "usage":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "You're out of usage credits for opus. Your limit resets at 6pm."}), flush=True)
    sys.exit(0)
a = sys.argv[1:]
val = lambda f: a[a.index(f) + 1] if f in a else None  # noqa: E731

# 2026-11-10: the Home Assistant core check (backend/ha_core.py through triage.ask_locked): one
# locked-down question on stdin, answered in the CLI's json envelope. Modes: core-touches (the
# default: every breaking change whose notes link an integration in use, plus the labels change
# for "automation" when it is in use), core-none (none touch), core-unsure, core-stray (names an
# integration that is not in use); garbage / logged-out above apply here too.
if val("--tools") == "" and "--output-format" in a:
    text = sys.stdin.read()
    sd = os.environ.get("RIVET_STATE_DIR")
    if sd and os.path.isdir(sd):
        with open(os.path.join(sd, "fake-claude-core.json"), "w") as fh:
            json.dump({"argv": a, "stdin_chars": len(text), "mentions_switch": os.environ.get("CLAUDE_CODE_DISABLE_FILE_MENTIONS"),
                       "ha_token": bool(os.environ.get("HA_TOKEN")), "cwd": os.getcwd()}, fh)
    if not text.startswith("HA_CORE_CHECK"):
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"}))
        sys.exit(0)
    data = json.loads(text.split("\nDATA:\n", 1)[1])
    use, changes = set(data["in_use"]), data["breaking_changes"]
    mode = MODE or "core-touches"
    touches = []
    if mode == "core-touches":
        for c in changes:
            for d in c["domains"]:
                if d in use:
                    first = c["text"].split("\n")[0].split(";")[0].rstrip(".")
                    touches.append({"change": c["n"], "integration": d, "what_changes": first,
                                    "what_to_do": "Move the setting before you update."})
            if not c["domains"] and "label" in c["title"].lower() and "automation" in use:
                touches.append({"change": c["n"], "integration": "automation",
                                "what_changes": "labelled config entities now follow label-targeted actions",
                                "what_to_do": "Check which entities carry your labels."})
    elif mode == "core-stray":
        touches = [{"change": 1, "integration": "not_in_use_anywhere", "what_changes": "x", "what_to_do": "y"}]
    verdict = {"core-none": "none_touch", "core-unsure": "unsure"}.get(mode, "touches" if touches else "none_touch")
    ans = {"touches": touches, "untouched": len(changes) - len({t["change"] for t in touches}), "verdict": verdict,
           "summary": "Canned answer from qa/fake-claude.py (%s)." % mode}
    time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "0")))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                      "result": json.dumps(ans), "total_cost_usd": 0}))
    sys.exit(0)
problems = []
if val("--model") != "opus":
    problems.append("model is %r, not opus" % val("--model"))
if "--bare" in a:
    problems.append("--bare given")
if val("--tools") != "Bash":
    problems.append("tools %r" % val("--tools"))
allowed = (val("--allowedTools") or "").split(",")
if not allowed or any("*" in x or not x.startswith("Bash(") for x in allowed):
    problems.append("allowlist %r" % allowed)
if "fix_guard.py" not in (val("--settings") or ""):
    problems.append("no guard hook")
for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
    if os.environ.get(k):
        problems.append("%s in the environment" % k)
cmds = [x[len("Bash("):-1] for x in allowed]
sd = os.environ.get("RIVET_STATE_DIR")
if sd:
    with open(os.path.join(sd, "fake-claude-argv.json"), "w") as fh:
        json.dump({"argv": a[:-1], "allowed": allowed, "problems": problems,
                   "session": bool(os.environ.get("RIVET_FIX_SESSION"))}, fh)


def out(o):
    print(json.dumps(o), flush=True)
    time.sleep(0.6)


out({"type": "system", "subtype": "init", "model": "opus"})
if problems:
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "; ".join(problems)})
    sys.exit(0)
pick = lambda verb: next(c for c in cmds if " --fix-tool %s " % verb in c)  # noqa: E731
for n, (verb, text, said) in enumerate([
        ("state", "installed v5.18.0, loaded 5.15.7, HACS restart_required repair", "The log and state agree: v5.18.0 is downloaded, 5.15.7 still loaded."),
        ("restart", "RESULT: ok (restarted): Restarted Home Assistant. Now running Alexa Media Player 5.18.0.", "A restart is pending, so restarting Home Assistant."),
        ("state", "installed v5.18.0, loaded 5.18.0, no repairs", "Loaded now.")]):
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": said},
         {"type": "tool_use", "id": "t%d" % n, "name": "Bash", "input": {"command": pick(verb), "description": "Fix it: %s" % verb}}]}})
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % n, "content": text}]}})
ans = {"fixed": True, "cause": "needed a Home Assistant restart", "actions": ["state", "restart", "state"],
       "result": "Fixed: HACS had downloaded v5.18.0 but Home Assistant needed a restart; restarted, now v5.18.0"}
out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 2400,
     "result": json.dumps(ans), "structured_output": ans, "modelUsage": {"claude-opus-fake": {}}, "total_cost_usd": 0})
