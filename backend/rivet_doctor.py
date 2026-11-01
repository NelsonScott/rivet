#!/usr/bin/env python3
"""rivet-doctor -- the "Fix it" button.

    rivet-doctor diagnose [--log PATH] [--model opus] [--fake SCENARIO]
    rivet-doctor run --fix PATH [--fake SCENARIO]
    rivet-doctor evidence            (print the evidence bundle and exit)

`diagnose` gathers read-only evidence (the last rivet-apply run, state.json,
kernels on disk, NVIDIA module state, akmods cache, journal, dnf) and hands it to
the Claude Code CLI (`claude -p --model opus`) with a READ-ONLY tool allowlist so
it can dig further on its own. Claude answers in a fixed JSON shape (headline,
what happened, is a reboot safe, an optional fix as shell commands, how to verify).
Nothing is changed by `diagnose`: every tool call outside the allowlist is denied by
the CLI itself (`--permission-prompts none`, verified 2026-09-22).

`run --fix PATH` executes the commands of a saved diagnosis one by one through
`bash -o pipefail -c`, streaming their output, then runs the verify commands.
Root work runs through one polkit prompt; reboots are refused
outright -- the app owns restarts. A denylist stops the obviously destructive.

Events are JSON lines on stdout in the rivet-apply envelope:
  {"ts","step":"doctor|fix|verify|done","status":"start|progress|ok|fail","pct","msg","log"}
`diagnose` ends with {"step":"done","status":"ok","diagnosis":{...},"fix_path":...};
`run` ends with {"step":"done","status":"ok|fail","summary":...,"results":{...}}.
Everything is appended to ~/.local/state/rivet/logs/doctor.log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LOG_DIR, STATE_DIR, STATE_PATH, LineStream, emit_line, now_iso, open_log, root_prefix, root_refusal  # noqa: E402

DOCTOR_DIR = STATE_DIR / "doctor"
DOCTOR_LOG = LOG_DIR / "doctor.log"
APPLY_LOG = LOG_DIR / "apply.log"
LAST_PATH = DOCTOR_DIR / "last.json"

CLAUDE_TIMEOUT_S = int(os.environ.get("RIVET_DOCTOR_TIMEOUT", "420"))   # investigation pass
FALLBACK_TIMEOUT_S = int(os.environ.get("RIVET_DOCTOR_FALLBACK_TIMEOUT", "240"))  # evidence-only pass
CLAUDE_MAX_TURNS = "30"
# Default model first, then down the ladder. `--fallback-model` only covers
# "overloaded or not available": an exhausted per-model allowance comes back as a
# hard rejection ("You're out of usage credits"), so the ladder is walked here, each
# model in its own process (2026-09-22, the night one model's weekly limit ran out).
FALLBACK_MODELS = [m for m in os.environ.get("RIVET_DOCTOR_FALLBACK_MODELS", "sonnet").split(",") if m.strip()]

OUT_OF_USAGE = re.compile(r"out of usage|usage limit|rate.?limit|quota|credits", re.I)


class OutOfUsage(RuntimeError):
    """This model has nothing left; a different one may still answer."""
CMD_TIMEOUT_S = 1800          # one fix command (a kernel install + akmods build fits)

CLAUDE_CANDIDATES = (
    os.environ.get("RIVET_CLAUDE_BIN"),
    str(Path.home() / ".local/bin/claude"),
    "/usr/local/bin/claude",
    "/usr/bin/claude",
)

# Read-only investigation. Anything else the CLI denies on its own.
ALLOWED_TOOLS = [
    "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)", "Bash(sed -n:*)",
    "Bash(ls:*)", "Bash(stat:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(sort:*)", "Bash(uniq:*)",
    "Bash(cut:*)", "Bash(tr:*)", "Bash(date:*)", "Bash(echo:*)", "Bash(true)",
    "Bash(uname:*)", "Bash(hostnamectl:*)", "Bash(df:*)", "Bash(free:*)", "Bash(uptime:*)",
    "Bash(rpm -q:*)", "Bash(rpm -qa:*)", "Bash(rpm -qi:*)", "Bash(rpm -ql:*)", "Bash(rpm -V:*)",
    "Bash(rpm --query:*)", "Bash(rpm -qf:*)",
    "Bash(dnf5 check-upgrade:*)", "Bash(dnf5 repoquery:*)", "Bash(dnf5 history:*)",
    "Bash(dnf5 check:*)", "Bash(dnf5 repolist:*)", "Bash(dnf5 list:*)", "Bash(dnf5 info:*)",
    "Bash(dnf5 updateinfo:*)", "Bash(dnf5 --version)", "Bash(dnf5 -q check-upgrade:*)",
    "Bash(dnf check-upgrade:*)", "Bash(dnf repoquery:*)", "Bash(dnf history:*)", "Bash(dnf list:*)",
    "Bash(journalctl:*)", "Bash(systemctl status:*)", "Bash(systemctl list-units:*)",
    "Bash(systemctl is-active:*)", "Bash(systemctl is-enabled:*)", "Bash(systemctl show:*)",
    "Bash(systemctl cat:*)", "Bash(systemctl --user status:*)", "Bash(systemctl --user list-units:*)",
    "Bash(systemctl --user list-timers:*)", "Bash(systemctl list-timers:*)",
    "Bash(modinfo:*)", "Bash(lsmod:*)", "Bash(nvidia-smi:*)", "Bash(mokutil --sb-state)",
    "Bash(mokutil --list-enrolled:*)", "Bash(bootctl list:*)", "Bash(bootctl status:*)",
    "Bash(grubby --info:*)", "Bash(grubby --default-kernel)", "Bash(akmods --help)",
    "Bash(flatpak list:*)", "Bash(flatpak remote-ls:*)", "Bash(flatpak info:*)",
    "Bash(flatpak mask)", "Bash(flatpak mask --system)", "Bash(flatpak history:*)",
    "Bash(fwupdmgr get-devices:*)", "Bash(fwupdmgr get-updates:*)", "Bash(fwupdmgr get-history:*)",
    "Bash(pgrep:*)", "Bash(ps:*)", "Bash(id)", "Bash(whoami)", "Bash(sudo -n true)",
    "Bash(python3 -c:*)", "Bash(python3 -m json.tool:*)", "Bash(jq:*)",
    "Read", "Grep", "Glob",
]

DENYLIST = re.compile(
    # a reboot/shutdown as a COMMAND (line start or after ; && || | sudo), not as a word
    r"(^|[;&|]\s*)(sudo\s+(-\S+\s+)*)?(reboot|shutdown|poweroff|halt|telinit|kexec)\b"
    r"|systemctl\s+(--\S+\s+)*(reboot|poweroff|halt|kexec|suspend|hibernate)\b"
    r"|\bmkfs\b|\bdd\s+if=|\bwipefs\b|\bfdisk\b|\bparted\b"
    r"|rm\s+(-\S*r\S*\s+)+(/|/\*|/boot|/usr|/etc|/var|/home|~|\$HOME)(\s|$)"
    r"|:\(\)\s*\{|>\s*/dev/(sd|nvme|mem)|chmod\s+(-R\s+)?777\s+/(\s|$)",
    re.I,
)

DIAG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "what_happened", "severity", "reboot", "fix", "after_fix",
                 "confidence", "evidence"],
    "properties": {
        "headline": {"type": "string", "description": "One plain-English line, max 90 characters, what is actually wrong (or that nothing is)."},
        "what_happened": {"type": "string", "description": "2-4 plain sentences for a person who does not read logs. Name the packages involved. No jargon dumps."},
        "severity": {"type": "string", "enum": ["fine", "attention", "broken"]},
        "reboot": {
            "type": "object", "additionalProperties": False, "required": ["safe", "why"],
            "properties": {"safe": {"type": "string", "enum": ["yes", "no", "unknown"]},
                           "why": {"type": "string"}},
        },
        "fix": {
            "type": "object", "additionalProperties": False,
            "required": ["available", "title", "explanation", "commands", "needs_root", "risk", "reversible"],
            "properties": {
                "available": {"type": "boolean"},
                "title": {"type": "string", "description": "Max 40 characters, imperative, e.g. 'Refresh dnf and install the kernel'."},
                "explanation": {"type": "string", "description": "What the commands do and why, 1-3 sentences."},
                "commands": {"type": "array", "items": {"type": "string"}},
                "needs_root": {"type": "boolean"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "reversible": {"type": "string", "description": "How to undo it, or 'not needed' / 'not reversible'."},
            },
        },
        "after_fix": {
            "type": "object", "additionalProperties": False, "required": ["verify_commands", "expect"],
            "properties": {"verify_commands": {"type": "array", "items": {"type": "string"}},
                           "expect": {"type": "string"}},
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "3-8 short facts, each with the command that showed it."},
    },
}

SYSTEM_PROMPT = """You are the "Fix it" button of Rivet, the user's Fedora update app on their GNOME PC (often with an NVIDIA GPU on akmod-nvidia, Secure Boot and Steam via Flatpak; check, do not assume). A run of rivet-apply just ended badly and the user pressed the button. Find out what actually happened and what, if anything, to do about it.

How Rivet works (facts, not guesses):
- rivet-collect (hourly systemd --user timer) writes ~/.local/state/rivet/state.json from `dnf5 check-upgrade` (user cache in ~/.cache/libdnf5), Flatpak and fwupd.
- rivet-apply installs RPMs through dnf5daemon-server over D-Bus (org.rpm.dnf.v0). The daemon has its OWN metadata cache in /var/cache/dnf5daemon-server/, separate from the user's and from /var/cache/libdnf5 (the root dnf5 CLI). It logs JSON events to ~/.local/state/rivet/logs/apply.log; step "download"/"install" is the RPM transaction, "akmod" waits up to 600 s for akmods to build kmod-nvidia for the target kernel, "verify" runs modinfo. A kernel or akmod-nvidia update needs a reboot; Rivet refuses to offer one while kmod_verified is false.
- NVIDIA: akmod-nvidia from RPM Fusion builds kmod-nvidia-<kernel> into /var/cache/akmods/nvidia/ (logs there too) and installs it; modules live in /lib/modules/<kernel>/extra/nvidia/. Secure Boot signing uses the akmods MOK. Flatpak Steam also needs org.freedesktop.Platform.GL.nvidia-<driver> matching the LOADED driver.
- The running kernel keeps its modules; a NEW kernel only becomes the default at reboot.

Known failure signatures (check these first; the evidence bundle usually shows them outright):
- Stale daemon metadata: apply.log says "0 package(s) in the transaction" / "Nothing left to do" right after "Preparing N package(s)", the target version is absent from `rpm -q` and /lib/modules, and the dnf5daemon-server updates cache revision is older than the user's. Nothing was installed; the running kernel is untouched. Fix = `sudo dnf5 -y --refresh upgrade <the planned packages>` (plus `sudo akmods --kernels <new kernel>` when a kernel is involved).
- Real akmods build failure: a `*.failed.log` for the new kernel in /var/cache/akmods/nvidia; the kernel IS installed but has no extra/nvidia modules. Reboot unsafe. Fix usually = kernel-devel matching the kernel + `sudo akmods --force --kernels <kver>`.
- Module built but unsigned / MOK not enrolled under Secure Boot: modinfo shows no signer. Needs the user at the MOK prompt; no silent fix.
- dnf5daemon restarted mid-transaction (dnf history "Error", every package present): cosmetic, nothing to redo.
- Verify raced depmod (rivet-apply before 2026-10-12): `akmod ok` and `verify ... modinfo FAILED` land within the same second or two, /var/cache/akmods/nvidia/.last.log says "Successful" one or two seconds LATER, and `modinfo -k <kver> nvidia` passes now. Nothing is broken: severity fine, reboot safe yes, fix.available=false; Rivet re-runs its own check when you say so.
- modinfo by NAME fails while /lib/modules/<kver>/extra/nvidia/nvidia.ko.xz exists and `modinfo <that path>` works: modules.dep for <kver> is stale (depmod never ran). Fix = `sudo depmod -a <kver>`.
- Flatpak GL.nvidia-<driver> runtime missing after a driver change: Steam (Flatpak) loses the GPU after reboot; fix = `flatpak install -y --system org.freedesktop.Platform.GL.nvidia-<ver>` (dashes for dots).

Budget: you have roughly 8 tool calls and four minutes. Start from the evidence bundle, which already holds the usual answers; use tools only to close a real gap (e.g. confirm a version, read one failed log). Do not audit Rivet's source code, systemd units or boot loader unless the evidence points there. As soon as the story is consistent, stop investigating and answer. Compound commands (`a; b`, `a && b`, pipes into tools that are not allowlisted) are denied: one simple command per call.

Investigate with the tools you have. Only read-only commands are allowed (rpm -q, dnf5 check-upgrade/repoquery/history/list, journalctl, systemctl status, ls/cat/tail/grep, modinfo, lsmod, nvidia-smi, mokutil --sb-state, flatpak list, df ...) plus Read/Grep/Glob; anything that changes state is denied automatically -- do not try to work around that, and never propose a workaround for it. Do not spend turns on things the evidence already answers. Compare what rivet-apply BELIEVED with what the rpmdb and /lib/modules SAY; disagreements are usually the story.

Then answer in the required JSON shape. Rules for the answer:
- Plain English for the user, who knows Linux but was not watching. Say which package/kernel/driver, and whether anything is actually broken right now.
- reboot.safe = "yes" only when the kernel that would boot has a verified NVIDIA module (or no NVIDIA change is involved). "no" when a reboot would land on a kernel without the driver. Say why in one sentence.
- fix.commands: a short list of non-interactive shell commands, in order, each one complete on its own (they run one by one through `bash -o pipefail -c`, not as a script, with no prompt to answer: use -y / --noninteractive / --assumeyes). Prefix root work with `sudo` (Rivet strips it and runs those steps as root through ONE polkit prompt that the user approves; never assume root is free). Prefer the smallest fix that leaves the system consistent: e.g. refresh metadata and install what was meant to be installed, rebuild/verify the module, fix a duplicate package. NEVER include reboot/shutdown/poweroff (the app restarts the machine itself, after verifying), never remove the running kernel, never `rm -rf` outside caches, never disable Secure Boot, never `dnf history undo` a kernel. If the right thing is "nothing, just check again", set fix.available=false and say so.
- after_fix.verify_commands: read-only commands that exit 0 only when the fix worked (rpm -q of the exact NEVRA, modinfo -k <kernel> nvidia, ...). Test the OUTCOME only: the package in the rpmdb, the module on disk, the file present. Never test for the absence of logs, caches or marker files: tools leave those behind (akmods keeps its .failed.log after a successful --force rebuild).
- evidence: the facts you relied on, each with the command that produced it.
- Deliver the answer by calling StructuredOutput exactly once, with one complete JSON object as its only argument and nothing else: no prose around it, no partial or repeated calls.
"""


# ---------------------------------------------------------------- events

class Emitter:
    """One JSON object per line, flushed at once; mirrored to doctor.log (rotated at 2 MB).

    A closed app window breaks stdout; common.emit_line notes it once and the run goes
    on with every event, `done` included, in doctor.log."""

    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self.stdout_gone = False
        DOCTOR_DIR.mkdir(parents=True, exist_ok=True)
        self.logfh = open_log(DOCTOR_LOG.name)

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        ev = {"ts": now_iso(), "step": step, "status": status, "pct": pct, "msg": msg, "log": log}
        ev.update(extra)
        emit_line(self, json.dumps(ev))

    def done(self, status, summary, **extra):
        self.emit("done", status, 100, summary, None, summary=summary,
                  log_path=str(DOCTOR_LOG), **extra)


# ---------------------------------------------------------------- evidence

def sh(cmd: str, timeout: int = 60, limit: int = 6000) -> str:
    """Run one read-only shell line; the output (or the error) as text, truncated."""
    try:
        cp = subprocess.run(["bash", "-o", "pipefail", "-c", cmd], capture_output=True,
                            text=True, timeout=timeout, env=clean_env(), stdin=subprocess.DEVNULL)
        out = cp.stdout
        if cp.returncode != 0:
            out += f"\n[exit {cp.returncode}] {cp.stderr.strip()[:400]}"
    except subprocess.TimeoutExpired:
        out = f"[timed out after {timeout}s]"
    except Exception as exc:
        out = f"[failed: {exc}]"
    out = out.strip() or "(no output)"
    if len(out) > limit:
        out = out[:limit // 2] + "\n… [truncated] …\n" + out[-limit // 2:]
    return out


def clean_env() -> dict:
    home = os.path.expanduser("~")
    path = f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    if os.environ.get("RIVET_FAKE_BIN"):            # tests: fake sudo/pkexec first
        path = os.environ["RIVET_FAKE_BIN"] + ":" + path
    return {
        "HOME": home,
        "PATH": path,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        "USER": os.environ.get("USER", os.path.basename(home)),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
    }


def last_apply_run(log_path: Path, keep_progress: int = 12) -> str:
    """The last rivet-apply run from apply.log: every non-progress event, plus a few
    progress lines (600 lines of 'Building… (NNNs)' say nothing new)."""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except Exception as exc:
        return f"(no apply log: {exc})"
    start = 0
    for i, line in enumerate(lines):
        if '"step": "download"' in line and '"status": "start"' in line:
            start = i
    run = lines[start:]
    is_progress = []
    for line in run:
        try:
            ev = json.loads(line)
            is_progress.append(ev.get("status") == "progress" and ev.get("step") != "done")
        except ValueError:
            is_progress.append(False)
    prog_idx = [i for i, p in enumerate(is_progress) if p]
    keep = set(prog_idx[:6] + prog_idx[-keep_progress:]) if len(prog_idx) > keep_progress + 6 else set(prog_idx)
    out, omitted = [], 0
    for i, line in enumerate(run):
        if is_progress[i] and i not in keep:
            omitted += 1
            continue
        if omitted:
            out.append(f"… {omitted} progress lines omitted …")
            omitted = 0
        out.append(line)
    text = "\n".join(out)
    if len(text) > 14000:
        text = text[:5000] + "\n… [middle truncated] …\n" + text[-9000:]
    return text or "(empty)"


def state_summary() -> str:
    try:
        s = json.loads(STATE_PATH.read_text())
    except Exception as exc:
        return f"(no state.json: {exc})"
    items = []
    for g in s.get("groups", []):
        for it in g.get("items", []):
            items.append(f"{it.get('id')}: {it.get('from')} -> {it.get('to')}"
                         + (f" [{it.get('badge')}]" if it.get("badge") else ""))
    head = {k: s.get(k) for k in ("status", "checked_at", "error", "flags", "summary")}
    return json.dumps(head, indent=1) + "\npending items:\n" + ("\n".join(items[:80]) or "(none)")


def _health_lines() -> str:
    try:
        import health as H
        checks = H.run_checks()["checks"]
    except Exception as exc:  # noqa: BLE001
        return f"(could not run: {exc})"
    return "\n".join(f"PROBLEM {c['id']}: {c['detail']}" for c in checks) or "all fine"


def gather_evidence(log_path: Path, em: Emitter | None = None) -> str:
    running = sh("uname -r")
    sections = [
        ("Last rivet-apply run (JSON events, apply.log)", lambda: last_apply_run(log_path)),
        ("state.json (what the checker believes is pending)", state_summary),
        ("Host", lambda: sh("uname -r; grep PRETTY_NAME /etc/os-release; date; uptime; mokutil --sb-state 2>&1")),
        ("Kernels installed (rpm)", lambda: sh("rpm -q kernel kernel-core kernel-modules kernel-devel 2>&1")),
        ("Kernels on disk and their NVIDIA modules",
         lambda: sh("for k in /lib/modules/*; do printf '%s: ' \"$k\"; ls \"$k\"/extra/nvidia/ 2>/dev/null | tr '\\n' ' '; echo; done")),
        ("NVIDIA packages", lambda: sh("rpm -qa 'kmod-nvidia*' 'akmod-nvidia*' 'xorg-x11-drv-nvidia*' 'nvidia-*' | sort")),
        (f"Loaded driver (running kernel {running})",
         lambda: sh("modinfo -k $(uname -r) nvidia 2>&1 | grep -E '^(version|filename|sig_key|signer)' ; lsmod | grep -E '^nvidia ' ; nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1")),
        ("akmods cache (/var/cache/akmods/nvidia)", lambda: sh("ls -la --time-style=+%F_%T /var/cache/akmods/nvidia/ 2>&1 | tail -12; echo '--- last log tail ---'; tail -15 /var/cache/akmods/nvidia/.last.log 2>&1")),
        ("akmods / dnf5daemon journal, last 6 h, errors and warnings only",
         lambda: sh("journalctl --no-pager -o short-iso --since '-6h' -u akmods -u dnf5daemon-server -u akmods-shutdown 2>&1 | grep -iE 'error|fail|warn|cannot|unable|nothing to do|timed out|denied' | grep -v 'DEBUG' | tail -30")),
        ("dnf5 (as the user, its own metadata cache)", lambda: sh("dnf5 -q --assumeno check-upgrade 'kernel*' 'akmod-nvidia' 'xorg-x11-drv-nvidia' 2>&1 | head -30; echo '--- history ---'; dnf5 history list 2>&1 | head -6", timeout=90)),
        ("dnf5daemon-server metadata cache age vs the user's",
         lambda: sh("for d in /var/cache/dnf5daemon-server/updates-* ~/.cache/libdnf5/updates-* /var/cache/libdnf5/updates-*; do [ -f \"$d/repodata/repomd.xml\" ] && printf '%s  %s  revision=%s\\n' \"$(stat -c %y \"$d/repodata/repomd.xml\" | cut -c1-19)\" \"$d\" \"$(grep -o '<revision>[0-9]*' \"$d/repodata/repomd.xml\" | tr -dc 0-9)\"; done")),
        ("Disk", lambda: sh("df -h / /boot /boot/efi /var 2>&1 | sort -u")),
        ("Flatpak NVIDIA runtimes", lambda: sh("flatpak list --runtime --columns=ref 2>&1 | grep -i nvidia")),
        ("Rivet health checks (document portal, launcher icons)", _health_lines),
    ]
    parts = []
    for i, (title, fn) in enumerate(sections):
        if em:
            em.emit("doctor", "progress", int(5 + 25 * i / len(sections)), f"Collecting evidence: {title.split(' (')[0].lower()}…", None)
        parts.append(f"### {title}\n{fn()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- claude

def claude_bin() -> str:
    """$RIVET_CLAUDE_BIN first (tests point it at a fake), then the known places."""
    for cand in CLAUDE_CANDIDATES:
        if cand and os.path.exists(cand):
            return cand
    found = shutil.which("claude")
    if found:
        return found
    raise RuntimeError("the Claude Code CLI ('claude') was not found on this system")


def build_prompt(evidence: str) -> str:
    return (
        "Rivet's last run ended with an error or an unverified NVIDIA module. Below is the "
        "evidence gathered just now (read-only). Investigate further with your tools where the "
        "evidence leaves a question open, then produce the diagnosis.\n\n" + evidence
    )


def claude_argv(prompt: str, model: str, *, tools: bool = True, schema: dict | None = None,
                system: str | None = None, tool_names: str = "Bash,Read,Grep,Glob",
                allowed: list[str] | None = None, max_turns: str | None = None,
                extra: list[str] | None = None, fallback: list[str] | None = None) -> list[str]:
    """The `claude -p` command line: subscription CLI, never --bare (it skips the login),
    no settings files, no permission prompts (anything outside `allowed` is denied)."""
    argv = [
        claude_bin(), "-p", "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(schema or DIAG_SCHEMA),
        "--permission-prompts", "none",
        "--setting-sources", "",
        "--no-session-persistence",
        "--append-system-prompt", system or SYSTEM_PROMPT,
    ] + list(extra or [])
    fb = FALLBACK_MODELS if fallback is None else fallback
    if fb:
        argv += ["--fallback-model", ",".join(fb)]
    if tools:
        argv += ["--tools", tool_names, "--allowedTools", ",".join(allowed or ALLOWED_TOOLS),
                 "--max-turns", max_turns or CLAUDE_MAX_TURNS, prompt]
    else:
        argv += ["--tools", "", "--max-turns", "3",
                 prompt + "\n\nYou have NO tools on this pass: answer now from the evidence above."]
    return argv


def run_claude(em: Emitter, prompt: str, model: str, *, tools: bool = True,
               timeout: int | None = None, step: str = "doctor", env: dict | None = None,
               **argv_kw) -> dict:
    """Stream the CLI, narrate its tool calls as progress, return structured_output.

    tools=False is the fallback pass: no tools at all, answer from the evidence bundle
    (one turn, fast) -- used when the investigation pass runs out of time or turns.
    `step` and the claude_argv() keywords let Other software's Fix it (other_fix.py) reuse
    this with its own schema and a much smaller tool budget.
    """
    timeout = timeout or (CLAUDE_TIMEOUT_S if tools else FALLBACK_TIMEOUT_S)
    argv = claude_argv(prompt, model, tools=tools, **argv_kw)
    if tools:
        em.emit(step, "progress", 30, f"Asking Claude ({model}) to look into it…",
                f"$ claude -p --model {model} ({'read-only tools' if step == 'doctor' else 'Fix it tools only'})")
    else:
        em.emit(step, "progress", 92, f"Asking Claude ({model}) for its verdict from the evidence alone…",
                f"$ claude -p --model {model} (no tools)")
    # stderr is merged into stdout: its lines are not JSON and show up as log lines, and
    # nothing is left unread in a second pipe. The deadline holds while the CLI is silent.
    try:
        ls = LineStream(argv, timeout=timeout, env=dict(clean_env(), **(env or {})), cwd=str(STATE_DIR))
    except OSError as exc:
        raise RuntimeError(f"cannot run the Claude CLI: {exc}") from None
    result: dict | None = None
    used: list[str] = []
    checks = 0
    denied = 0
    noise: list[str] = []            # non-JSON lines (the CLI's stderr lands here)
    pending_calls: dict[str, str] = {}
    if True:
        for raw in ls.lines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                noise.append(raw)
                em.emit(step, "progress", None, None, raw[:300])
                continue
            t = ev.get("type")
            if t == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use" and c.get("name") == "StructuredOutput":
                        pending_calls[c.get("id", "")] = "__answer__"     # the --json-schema answer, not a check
                    elif c.get("type") == "tool_use":
                        checks += 1
                        name, inp = c.get("name"), c.get("input") or {}
                        if name == "Bash":
                            what = inp.get("description") or inp.get("command") or ""
                            log = "$ " + str(inp.get("command", ""))[:400]
                        elif name == "Read":
                            what = "Reading " + tilde(inp.get("file_path", ""))
                            log = "read " + str(inp.get("file_path", ""))
                        else:
                            what = f"{name}: {inp.get('pattern') or inp.get('path') or ''}"
                            log = f"{name.lower()} {json.dumps(inp)[:200]}"
                        pending_calls[c.get("id", "")] = str(inp.get("command") or inp.get("file_path") or name)
                        em.emit(step, "progress", min(90, 30 + checks * 3), f"Checking: {what}"[:160], log)
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        em.emit(step, "progress", None, None, "claude: " + c["text"].strip()[:400])
            elif t == "user":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") != "tool_result":
                        continue
                    body = c.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        x.get("text", "") for x in (body or []) if isinstance(x, dict))
                    cmd = pending_calls.pop(c.get("tool_use_id", ""), "")
                    if cmd == "__answer__":
                        continue
                    if c.get("is_error") and "denied" in text.lower():
                        denied += 1
                        em.emit(step, "progress", None, None, f"(not allowed, read-only mode: {cmd[:160]})")
                    else:
                        first = text.strip().splitlines()[:3]
                        if first:
                            em.emit(step, "progress", None, None, "  " + " ⏎ ".join(x[:120] for x in first))
            elif t == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                if info.get("status") not in (None, "allowed", "allowed_warning"):
                    em.emit(step, "progress", None,
                            f"{model} is rate limited ({info.get('status')})…", None)
            elif t == "system" and ev.get("subtype") == "model_fallback":
                used.append(str(ev.get("to_model") or ev.get("model") or ""))
                em.emit(step, "progress", None,
                        f"{model} was not available; switched to {used[-1] or 'another model'}.", None)
            elif t == "result":
                result = ev
    if ls.timed_out:
        raise RuntimeError(f"claude did not finish within {timeout}s (killed)")
    stderr = "\n".join(noise[-20:]).strip()
    if result is None:
        if OUT_OF_USAGE.search(stderr):
            raise OutOfUsage(f"{model} has no usage left ({stderr.strip()[:120]})")
        raise RuntimeError(f"claude produced no result (exit {ls.returncode}): {stderr[:300]}")
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        detail = str(result.get("result") or result.get("api_error_status") or "")[:300]
        sub = result.get("subtype") or "error"
        if OUT_OF_USAGE.search(detail):
            raise OutOfUsage(f"{model} has no usage left ({detail[:120]})")
        raise RuntimeError(f"claude {sub}: {detail}")
    diag = result.get("structured_output")
    if not isinstance(diag, dict):
        # older CLI: the JSON is the result text
        try:
            diag = json.loads(result.get("result") or "")
        except ValueError:
            raise RuntimeError("claude answered without the structured diagnosis")
    diag["_meta"] = {
        "model": (list(result.get("modelUsage") or {}) or used or [model])[-1],
        "asked_for": model, "turns": result.get("num_turns"), "checks": checks, "denied": denied,
        "tools": tools,
        "duration_ms": result.get("duration_ms"), "cost_usd": result.get("total_cost_usd"),
        "session_id": result.get("session_id"),
    }
    return diag


NOT_LOGGED_IN = re.compile(r"/login|not logged in|invalid api key|authentication_error|oauth token|"
                           r"please log ?in|credentials", re.I)


def plain_claude_error(exc: BaseException) -> str:
    """One plain clause for a Claude CLI failure, for the row / the doctor screen (2026-11-01
    abuse pass): not installed, not logged in, out of usage, timed out, out of turns, or an
    answer that could not be read. The raw text stays in the log."""
    t = str(exc)
    if t.startswith("no model has usage left"):
        return t.rstrip(".")                   # already plain (ask()'s own sentence)
    if "was not found on this system" in t or "cannot run the Claude CLI" in t:
        return "Claude Code is not installed (install it and log in to use Fix it)"
    if NOT_LOGGED_IN.search(t):
        return "Claude Code is not logged in (run `claude` once in a terminal and log in)"
    if OUT_OF_USAGE.search(t) or "no model has usage left" in t:
        return "no Claude usage is left right now (it resets on its own)"
    m = re.search(r"did not finish within (\d+)s", t)
    if m:
        secs = int(m.group(1))
        return "Claude did not finish within %s and was stopped" % (
            f"{secs // 60} minutes" if secs >= 120 else f"{secs} s")
    if "error_max_turns" in t:
        return "Claude used its whole budget of steps without an answer"
    if "error_max_budget" in t:
        return "Claude hit its spending cap without an answer"
    if "produced no result" in t or "without the structured" in t:
        return "Claude's answer could not be read"
    return t[:160]


def ask(em: Emitter, prompt: str, model: str) -> dict:
    """Investigate, then fall back: no tools, then the next model down the ladder.

    Two independent reasons a pass fails, handled differently:
    * out of time / turns while digging -> the same model answers from the evidence alone
      (2026-09-22: 57 read-only checks in 10 minutes and no verdict);
    * out of usage for that model -> start over with the next model, tools and all.
    """
    ladder = [model] + [m for m in FALLBACK_MODELS if m != model]
    spent: list[str] = []
    for i, m in enumerate(ladder):
        if i:
            em.emit("doctor", "progress", 30, f"Asking {m} instead…",
                    f"{spent[-1]} had nothing left")
        try:
            try:
                diag = run_claude(em, prompt, m, tools=True)
            except OutOfUsage:
                raise
            except RuntimeError as exc:
                em.emit("doctor", "progress", 91,
                        "That took too long; asking for a verdict from the evidence alone…",
                        f"investigation pass gave up: {exc}")
                diag = run_claude(em, prompt, m, tools=False)
                diag["_meta"]["fallback"] = str(exc)[:200]
            if i:
                diag["_meta"]["ladder"] = spent + [m]
            return normalize(diag)
        except OutOfUsage as exc:
            spent.append(m)
            em.emit("doctor", "progress", None, None, str(exc))
            if i == len(ladder) - 1:
                raise RuntimeError(
                    "no model has usage left right now (tried %s). Your Claude limit resets on its "
                    "own; the evidence is in the log and nothing was changed." % ", ".join(spent)
                ) from None
    raise RuntimeError("no model to ask")          # unreachable


def tilde(p: str) -> str:
    home = os.path.expanduser("~")
    return p.replace(home, "~", 1) if p.startswith(home) else p


def normalize(diag: dict) -> dict:
    """Defensive shaping: the schema is enforced by the CLI, but keep the UI safe anyway."""
    d = dict(diag)
    d.setdefault("headline", "Diagnosis")
    d.setdefault("what_happened", "")
    d["severity"] = d.get("severity") if d.get("severity") in ("fine", "attention", "broken") else "attention"
    rb = d.get("reboot") if isinstance(d.get("reboot"), dict) else {}
    d["reboot"] = {"safe": rb.get("safe") if rb.get("safe") in ("yes", "no", "unknown") else "unknown",
                   "why": str(rb.get("why") or "")}
    fx = d.get("fix") if isinstance(d.get("fix"), dict) else {}
    cmds = [str(c).strip() for c in (fx.get("commands") or []) if str(c).strip()]
    d["fix"] = {
        "available": bool(fx.get("available")) and bool(cmds),
        "title": str(fx.get("title") or "Apply the fix")[:60],
        "explanation": str(fx.get("explanation") or ""),
        "commands": cmds,
        "needs_root": bool(fx.get("needs_root")) or any(c.startswith("sudo ") for c in cmds),
        "risk": fx.get("risk") if fx.get("risk") in ("low", "medium", "high") else "medium",
        "reversible": str(fx.get("reversible") or ""),
    }
    blocked = [c for c in cmds if DENYLIST.search(c)]
    if blocked:
        d["fix"]["available"] = False
        d["fix"]["blocked"] = blocked
        d["fix"]["explanation"] = ("Rivet refused this fix because it contains a command it never runs "
                                   f"({blocked[0][:60]}). " + d["fix"]["explanation"])
    af = d.get("after_fix") if isinstance(d.get("after_fix"), dict) else {}
    d["after_fix"] = {"verify_commands": [str(c) for c in (af.get("verify_commands") or []) if str(c).strip()],
                      "expect": str(af.get("expect") or "")}
    d["confidence"] = d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "medium"
    d["evidence"] = [str(e) for e in (d.get("evidence") or [])][:12]
    return d


# ---------------------------------------------------------------- diagnose

def diagnose(em: Emitter, *, log_path: Path, model: str) -> int:
    em.emit("doctor", "start", 0, "Looking into what happened…", None)
    try:
        evidence = gather_evidence(log_path, em)
        prompt = build_prompt(evidence)
        diag = ask(em, prompt, model)
    except Exception as exc:
        msg = f"Could not get a diagnosis: {plain_claude_error(exc)}."
        em.emit("doctor", "fail", None, msg, None)
        em.done("fail", msg, cause=msg, diagnosis=None, fix_path=None)
        return 1
    diag["created_at"] = now_iso()
    diag["apply_log"] = str(log_path)
    path = DOCTOR_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(diag, indent=1))
    try:
        LAST_PATH.write_text(json.dumps(diag, indent=1))
    except Exception:
        pass
    meta = diag.get("_meta") or {}
    em.emit("doctor", "ok", 100, diag["headline"],
            f"{meta.get('checks', 0)} checks, {meta.get('turns', '?')} turns, "
            f"{(meta.get('duration_ms') or 0) / 1000:.0f}s")
    em.done("ok", diag["headline"], diagnosis=diag, fix_path=str(path))
    return 0


# ---------------------------------------------------------------- run the fix

def stream_lines(em: Emitter, step: str, cmd: str | list[str], timeout: int = CMD_TIMEOUT_S,
                 on_line=None) -> int:
    """One fix/verify command through bash (or a ready argv), output streamed; killed and
    reaped at `timeout` even when it is silent (a polkit dialog nobody answers, a hung dnf).
    `on_line(line)` may claim a line (return True) so it is handled instead of logged."""
    argv = ["bash", "-o", "pipefail", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    ls = LineStream(argv, timeout=timeout, env=dict(clean_env(), SUDO_PROMPT=""))
    n = 0
    for line in ls.lines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if on_line is not None and on_line(line):
            continue
        n += 1
        if n <= 400 or n % 25 == 0:
            em.emit(step, "progress", None, None, line[:400])
    if ls.timed_out:
        em.emit(step, "progress", None, f"Timed out after {timeout}s; the command was killed.", None)
        return 124
    return ls.returncode if ls.returncode is not None else 1


ROOT_TOOL_RE = re.compile(r"^(dnf5?|rpm -[iUe]|akmods|depmod|flatpak (update|install|uninstall|mask) --system"
                          r"|systemctl (restart|start|stop|enable|disable)|dracut|grubby|kernel-install)\b")


def split_root(cmd: str, needs_root: bool) -> tuple[str, bool]:
    """(command without its sudo prefix, must it run as root?). A root tool named without
    sudo counts as root when the diagnosis says needs_root."""
    c = cmd.strip()
    m = re.match(r"^sudo\s+(?:-\S+\s+)*", c)
    if m:
        return c[m.end():], True
    return c, bool(needs_root and ROOT_TOOL_RE.match(c))


STEP_MARK = "__RIVET_STEP__"
FAIL_MARK = "__RIVET_FAIL__"


def batch_script(steps: list[tuple[int, str]]) -> str:
    """One bash script for consecutive steps: a marker line before each, the first failure
    reports its step number and exit code, then stops."""
    out = ["set -o pipefail"]
    for i, cmd in steps:
        out.append(f"echo '{STEP_MARK} {i}'")
        out.append(f"{{ {cmd} ; }} || {{ rc=$?; echo \"{FAIL_MARK} {i} $rc\"; exit $rc; }}")
    return "\n".join(out) + "\n"


def run_fix(em: Emitter, fix_path: Path) -> int:
    try:
        diag = json.loads(fix_path.read_text())
    except Exception as exc:
        msg = f"Cannot read the diagnosis file: {exc}"
        em.emit("fix", "fail", None, msg, None)
        em.done("fail", msg, cause=msg)
        return 1
    fix = diag.get("fix") or {}
    cmds = [str(c) for c in fix.get("commands") or []]
    if not fix.get("available") or not cmds:
        msg = "This diagnosis has no fix to run."
        em.emit("fix", "fail", None, msg, None)
        em.done("fail", msg, cause=msg)
        return 1
    blocked = [c for c in cmds if DENYLIST.search(c)]
    if blocked:
        msg = f"Refusing to run: {blocked[0][:80]}"
        em.emit("fix", "fail", None, msg, None)
        em.done("fail", msg, cause="The fix contains a command Rivet never runs (reboot, wipe, ...).")
        return 1
    title = fix.get("title") or "the fix"
    em.emit("fix", "start", 0, f"Running: {title}", None)

    # Consecutive steps with the same privilege run as ONE bash script, so a fix with
    # several root commands asks for permission once (polkit remembers nothing between
    # pkexec calls). Marker lines turn into the per-step progress events.
    plain = [split_root(c, bool(fix.get("needs_root"))) for c in cmds]
    batches: list[tuple[bool, list[tuple[int, str]]]] = []
    for i, (c, root) in enumerate(plain):
        if batches and batches[-1][0] == root:
            batches[-1][1].append((i, c))
        else:
            batches.append((root, [(i, c)]))
    prefix = root_prefix() if any(root for root, _ in batches) else []
    n = len(cmds)
    ran = 0
    failed: tuple[int, int] | None = None     # (step index, rc)
    seen_step = -1

    def on_line(line: str) -> bool:
        nonlocal ran, failed, seen_step
        if line.startswith(STEP_MARK):
            i = int(line.split()[1])
            if seen_step >= 0:
                ran += 1
            seen_step = i
            em.emit("fix", "progress", int(100 * i / n), f"Step {i + 1} of {n}: {cmds[i][:120]}", "$ " + plain[i][0])
            return True
        if line.startswith(FAIL_MARK):
            _, i, rc = line.split()[:3]
            failed = (int(i), int(rc))
            return True
        return False

    for root, steps in batches:
        argv = (prefix if root else []) + ["bash", "-c", batch_script(steps)]
        if root and prefix == ["pkexec"]:
            em.emit("fix", "progress", None, f"Asking for permission to run step {steps[0][0] + 1} as root (face or password)…", None)
        seen_step = -1
        rc = stream_lines(em, "fix", argv, timeout=CMD_TIMEOUT_S * len(steps), on_line=on_line)
        if rc == 0 and seen_step >= 0:
            ran += 1
            continue
        if failed is None:
            # nothing in the script said so: the batch itself never ran (polkit said no, timeout)
            refused = root_refusal(prefix, rc) if root else None
            step_i = steps[0][0]
            msg = (f"Step {step_i + 1} did not run: {refused}." if refused
                   else f"Step {step_i + 1} failed (exit {rc}): {cmds[step_i][:100]}.")
            em.emit("fix", "fail", None, msg, None)
            em.done("fail", msg, cause=msg, results={"ran": ran, "planned": n, "failed_step": step_i + 1,
                                                     **({"refused": refused} if refused else {})})
            return 1
        step_i, step_rc = failed
        msg = f"Step {step_i + 1} failed (exit {step_rc}): {cmds[step_i][:100]}."
        em.emit("fix", "fail", None, msg, None)
        em.done("fail", msg, cause=msg, results={"ran": ran, "planned": n, "failed_step": step_i + 1})
        return 1
    em.emit("fix", "ok", 100, f"{ran} of {len(cmds)} step(s) ran without errors.", None)

    checks = [str(c) for c in (diag.get("after_fix") or {}).get("verify_commands") or []
              if not DENYLIST.search(str(c))]
    expect = (diag.get("after_fix") or {}).get("expect") or "All checks passed."
    if checks:
        # Every check runs; one wrong check must not hide that the others passed. 2026-10-04:
        # akmods --force had installed the module (rpm -q, modinfo ok) and the only failing
        # check was `test ! -e <old .failed.log>`, a file akmods never deletes -- reported as
        # "the fix did not work" and the user ran the whole thing again.
        em.emit("verify", "start", 0, "Checking that it worked…", None)
        failed: list[tuple[str, int]] = []
        for i, cmd in enumerate(checks):
            em.emit("verify", "progress", int(100 * i / len(checks)), cmd[:120], "$ " + cmd)
            rc = stream_lines(em, "verify", cmd, timeout=300)
            if rc != 0:
                failed.append((cmd, rc))
        passed = len(checks) - len(failed)
        if failed and passed == 0:
            msg = f"The fix ran, but the check failed: {failed[0][0][:100]} (exit {failed[0][1]})."
            em.emit("verify", "fail", None, msg, None)
            em.done("fail", msg, cause=msg,
                    results={"ran": ran, "planned": len(cmds), "verify_failed": [c for c, _ in failed],
                             "verify_passed": passed, "verify_total": len(checks)})
            return 1
        if failed:
            what = "; ".join(f"{c[:80]} (exit {rc})" for c, rc in failed)
            msg = (f"The fix ran and {passed} of {len(checks)} checks passed. Did not pass: {what}. "
                   "Likely the check, not the fix: look at the details before running it again.")
            em.emit("verify", "warn", 100, msg, None)
            em.done("warn", f"Fixed, with a note: {title}.", cause=msg,
                    results={"ran": ran, "planned": len(cmds), "verify_failed": [c for c, _ in failed],
                             "verify_passed": passed, "verify_total": len(checks)})
            return 0
        em.emit("verify", "ok", 100, expect, None)
    summary = f"Fixed: {title}."
    em.done("ok", summary, results={"ran": ran, "planned": len(cmds)})
    return 0


# ---------------------------------------------------------------- fake (QA / demo)

FAKE_DIAG = {
    "headline": "The new kernel was never installed, so nothing is broken",
    "what_happened": "Rivet asked dnf to install kernel 7.2.6, but dnf's own package index was three hours older than the checker's and did not know that version yet, so it installed nothing and said 'nothing to do'. Rivet then waited ten minutes for an NVIDIA module build for a kernel that is not on the disk. The running kernel 7.2.5 still has its NVIDIA module, exactly as before.",
    "severity": "attention",
    "reboot": {"safe": "yes", "why": "Nothing changed: 7.2.5 boots with its verified NVIDIA module, and 7.2.6 is not installed."},
    "fix": {"available": True, "title": "Refresh dnf and install the kernel",
            "explanation": "Refresh dnf's package index, install the 12 packages Rivet meant to install, then build the NVIDIA module for the new kernel and confirm it loads.",
            "commands": ["sudo dnf5 -y --refresh upgrade kernel kernel-core kernel-modules kernel-modules-core kernel-modules-extra kernel-devel kernel-devel-matched kernel-tools kernel-tools-libs python3-perf selinux-policy selinux-policy-targeted",
                         "sudo akmods --kernels 7.2.6-200.fc44.x86_64"],
            "needs_root": True, "risk": "low", "reversible": "The old kernel stays installed and selectable at boot."},
    "after_fix": {"verify_commands": ["rpm -q kernel-core-7.2.6-200.fc44.x86_64", "modinfo -k 7.2.6-200.fc44.x86_64 nvidia"],
                  "expect": "kernel-core 7.2.6 is installed and its NVIDIA module loads."},
    "confidence": "high",
    "evidence": ["apply.log: '0 package(s) in the transaction. Nothing left to do.' right after 'Preparing 12 package(s)'",
                 "rpm -q kernel-core: 7.1.10, 7.2.4, 7.2.5 -- no 7.2.6",
                 "ls /lib/modules: no 7.2.6 directory; 7.2.5/extra/nvidia has all 5 modules",
                 "dnf5daemon-server cache revision 1789950156 (20:01) vs user cache 1790036627 (23:05)",
                 "dnf5 check-upgrade (user): kernel 7.2.6-200.fc44 offered from updates"],
}


def fake_diagnose(em: Emitter, scenario: str) -> int:
    pace = float(os.environ.get("RIVET_FAKE_PACE", "0.25"))
    em.emit("doctor", "start", 0, "Looking into what happened…", None)
    for pct, msg, log in [
        (8, "Collecting evidence: last rivet-apply run…", None),
        (20, "Collecting evidence: kernels on disk…", None),
        (30, "Asking Claude (opus) to look into it…", "$ claude -p --model opus (read-only tools)"),
        (36, "Checking: Which kernels does rpm know about?", "$ rpm -q kernel-core"),
        (None, None, "  kernel-core-7.2.5-200.fc44.x86_64"),
        (42, "Checking: NVIDIA modules per kernel", "$ ls /lib/modules/*/extra/nvidia/"),
        (48, "Checking: dnf5daemon cache age", "$ stat -c %y /var/cache/dnf5daemon-server/updates-*/repodata/repomd.xml"),
        (None, None, "(not allowed, read-only mode: sudo dnf5 makecache)"),
        (60, "Checking: what dnf offers now", "$ dnf5 -q check-upgrade 'kernel*'"),
        (None, None, "claude: The kernel never landed; dnf's daemon-side metadata was stale."),
    ]:
        em.emit("doctor", "progress", pct, msg, log)
        time.sleep(pace)
    if scenario == "nodiag":
        msg = "Could not get a diagnosis: claude cli rate_limited"
        em.emit("doctor", "fail", None, msg, None)
        em.done("fail", msg, cause=msg, diagnosis=None, fix_path=None)
        return 1
    diag = normalize(json.loads(json.dumps(FAKE_DIAG)))
    if scenario == "nofix":
        diag["fix"]["available"] = False
        diag["fix"]["explanation"] = "Nothing to fix: just check again and run the update."
        diag["fix"]["commands"] = []
    diag["_meta"] = {"model": "fake", "turns": 9, "checks": 4, "denied": 1, "duration_ms": 4200}
    diag["created_at"] = now_iso()
    DOCTOR_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCTOR_DIR / f"fake-{scenario}.json"
    path.write_text(json.dumps(diag, indent=1))
    em.emit("doctor", "ok", 100, diag["headline"], "4 checks, 9 turns, 4s")
    em.done("ok", diag["headline"], diagnosis=diag, fix_path=str(path))
    return 0


def fake_run(em: Emitter, scenario: str, fix_path: Path) -> int:
    pace = float(os.environ.get("RIVET_FAKE_PACE", "0.25"))
    em.emit("fix", "start", 0, "Running: Refresh dnf and install the kernel", None)
    for step, status, pct, msg, log in [
        ("fix", "progress", None, "Asking for permission to run step 1 as root (face or password)…", None),
        ("fix", "progress", 0, "Step 1 of 2: sudo dnf5 -y --refresh upgrade kernel …", "$ dnf5 -y --refresh upgrade kernel kernel-core …"),
        ("fix", "progress", None, None, "Updating and loading repositories: done"),
        ("fix", "progress", None, None, "Upgrading kernel-core-0:7.2.6-200.fc44.x86_64"),
        ("fix", "progress", None, None, "Complete!"),
        ("fix", "progress", 50, "Step 2 of 2: sudo akmods --kernels 7.2.6-200.fc44.x86_64", "$ akmods --kernels 7.2.6-200.fc44.x86_64"),
        ("fix", "progress", None, None, "Checking kmods exist for 7.2.6-200.fc44.x86_64"),
        ("fix", "progress", None, None, "Building and installing nvidia-kmod  [  OK  ]"),
    ]:
        em.emit(step, status, pct, msg, log)
        time.sleep(pace)
    if scenario == "runfail":
        msg = "Step 2 failed (exit 1): sudo akmods --kernels 7.2.6-200.fc44.x86_64."
        em.emit("fix", "fail", None, msg, None)
        em.done("fail", msg, cause=msg, results={"ran": 1, "planned": 2, "failed_step": 2})
        return 1
    em.emit("fix", "ok", 100, "2 of 2 step(s) ran without errors.", None)
    em.emit("verify", "start", 0, "Checking that it worked…", None)
    em.emit("verify", "progress", 0, "rpm -q kernel-core-7.2.6-200.fc44.x86_64", "$ rpm -q kernel-core-7.2.6-200.fc44.x86_64")
    em.emit("verify", "progress", None, None, "kernel-core-7.2.6-200.fc44.x86_64")
    em.emit("verify", "progress", 50, "modinfo -k 7.2.6-200.fc44.x86_64 nvidia", "$ modinfo -k 7.2.6-200.fc44.x86_64 nvidia")
    em.emit("verify", "ok", 100, "kernel-core 7.2.6 is installed and its NVIDIA module loads.", None)
    em.done("ok", "Fixed: Refresh dnf and install the kernel.", results={"ran": 2, "planned": 2})
    return 0


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rivet-doctor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose")
    d.add_argument("--log", default=str(APPLY_LOG))
    d.add_argument("--model", default=os.environ.get("RIVET_DOCTOR_MODEL", "opus"))
    d.add_argument("--fake", default=None, help="QA/demo: ok | nofix | nodiag (no Claude call)")
    r = sub.add_parser("run")
    r.add_argument("--fix", required=True)
    r.add_argument("--fake", default=None, help="QA/demo: ok | runfail (touches nothing)")
    sub.add_parser("evidence")
    h = sub.add_parser("health", help="deterministic checks (document portal, launcher icons); see health.py")
    h.add_argument("--fix", default=None, metavar="ID", help="run that check's fixed user-level fix")
    h.add_argument("--fake", default=None, help="demo: report all fine / a fix that changes nothing")
    a = ap.parse_args(argv)

    if a.cmd == "evidence":
        print(gather_evidence(APPLY_LOG))
        return 0
    em = Emitter()
    if a.cmd == "health":
        import health as H
        if a.fake:
            doc = {"checked_at": now_iso(), "checks": [], "fake": True}
            msg = "Demo: nothing was restarted." if a.fix else "Demo: no checks were run."
            em.done("ok", msg, health=doc, fixed=a.fix)
            return 0
        if a.fix:
            return H.fix(em, a.fix)
        doc = H.run_checks()
        H.save(doc)
        em.done("ok", f"{len(doc['checks'])} problem(s) found.", health=doc)
        return 0
    if a.cmd == "diagnose":
        if a.fake:
            return fake_diagnose(em, a.fake)
        return diagnose(em, log_path=Path(a.log), model=a.model)
    if a.fake:
        return fake_run(em, a.fake, Path(a.fix))
    return run_fix(em, Path(a.fix))


if __name__ == "__main__":
    sys.exit(main())
