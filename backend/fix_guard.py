#!/usr/bin/env python3
"""PreToolUse hook for Fix it's `claude -p` (other_fix.py), passed with `--settings`.

2026-11-01: an --allowedTools list alone is not a budget. Claude Code auto-approves
read-only commands (cat, ls, grep, head, sed -n ...) in every permission mode, so a
Fix it run could have read any file of the user's (verified with a fake secret). This hook
runs before EVERY tool call and denies anything that is not, character for character, one
of the commands in the run's session (RIVET_FIX_SESSION/meta.json "cmds")."""
import json
import os
import sys
from pathlib import Path


# The CLI delivers a --json-schema answer through this tool; it is the run's verdict, not an action.
ANSWER_TOOL = "StructuredOutput"


def decide(ev: dict, cmds: list[str]) -> str | None:
    """None = allow; otherwise the reason it is denied."""
    if ev.get("tool_name") == ANSWER_TOOL:
        return None
    if ev.get("tool_name") != "Bash":
        return f"{ev.get('tool_name')} is not part of this Fix it run."
    cmd = str((ev.get("tool_input") or {}).get("command") or "").strip()
    if cmd in cmds:
        return None
    return "Not in this Fix it run's tool budget; only these commands run: " + " | ".join(cmds)


def main() -> int:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        ev = {}
    try:
        cmds = json.loads(Path(os.environ["RIVET_FIX_SESSION"], "meta.json").read_text()).get("cmds") or []
    except Exception:  # noqa: BLE001 - no session: nothing is allowed
        cmds = []
    why = decide(ev, cmds)
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
