"""Triage: batched LLM verdicts for pending updates.

Interface (CONTRACT.md): triage(items) -> {id: {verdict, reasoning, risk_raise}}
where risk_raise is null | "yellow" | "red". The caller may only RAISE a colour.

Provider: `claude -p --model sonnet --output-format json` (the Claude Code CLI on
this box, which draws on the user's Claude subscription). No paid API key and no
network credential is read -- there is deliberately no OpenRouter/Anthropic
HTTP path, so triage costs nothing per run beyond the subscription.

Every item is sent in ONE call. Items that share an advisory (or a package
family at the same target version) are collapsed into a single cluster so the
prompt stays small; the verdict fans back out to each member id.

Verdicts are cached by (id, from, to) in ~/.local/state/rivet/triage-cache.json,
so a re-run with no new packages costs nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import TRIAGE_CACHE_PATH, log, write_json_atomic
from rules import RISK_ORDER

CLAUDE_TIMEOUT = 110      # per chunk
CHUNK_CLUSTERS = 12       # clusters per claude call
MAX_PARALLEL = 4          # concurrent claude calls

# systemd --user services get a minimal PATH that does NOT include ~/.local/bin,
# where the Claude Code CLI lives, so resolve the binary explicitly.
CLAUDE_CANDIDATES = (
    os.environ.get("RIVET_CLAUDE_BIN"),
    str(Path.home() / ".local/bin/claude"),
    "/usr/local/bin/claude",
    "/usr/bin/claude",
)


def claude_bin() -> str:
    """Absolute path to the Claude Code CLI. RIVET_CLAUDE_BIN, when set, decides (QA fakes)."""
    forced = os.environ.get("RIVET_CLAUDE_BIN")
    if forced:
        if os.access(forced, os.X_OK):
            return forced
        raise RuntimeError(f"RIVET_CLAUDE_BIN={forced} is not executable")
    found = shutil.which("claude")
    if found:
        return found
    for cand in CLAUDE_CANDIDATES:
        if cand and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError("the Claude Code CLI ('claude') was not found on this system")

SYSTEM_PROMPT = """You triage pending Linux package updates for one Fedora GNOME \
workstation (it may run the proprietary akmod-nvidia driver, Secure Boot and Steam as a \
Flatpak; the facts given with each cluster say which). The owner is a competent developer who wants to know \
which updates deserve attention, not a lecture.

For each cluster you are given, return:
  verdict   - ONE short sentence (<= 140 chars), plain and concrete, saying what this
              update does and whether it matters to this machine.
  reasoning - 2 to 4 sentences explaining the risk colour, citing the supplied release
              note / advisory text. Mention the driver rebuild or reboot when relevant.
  risk_raise - null, "yellow", or "red". The caller has already assigned a colour
              deterministically (given to you as current_risk). Use risk_raise ONLY to
              argue the colour should be HIGHER than current_risk; you can never lower
              it. Raise to "red" only for a genuine exploited/remote-code-execution class
              security fix or a real risk of an unbootable / display-less system. Raise to
              "yellow" for anything touching the graphics stack, kernel modules, boot,
              or a compositor restart. Otherwise return null.

Reply with STRICT JSON only, no markdown fence, no prose:
{"results":[{"key":"<cluster key verbatim>","verdict":"...","reasoning":"...","risk_raise":null}]}
Return exactly one entry per cluster key you were given."""


# ------------------------------------------------------------------ cache


def _cache_key(item: dict) -> str:
    return f"{item['id']}|{item.get('from')}|{item.get('to')}"


def load_cache() -> dict:
    try:
        d = json.loads(TRIAGE_CACHE_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    # keep the cache from growing forever
    if len(cache) > 4000:
        cache = dict(list(cache.items())[-2000:])
    try:
        write_json_atomic(TRIAGE_CACHE_PATH, cache)
    except Exception as exc:
        log(f"triage cache write failed: {exc}")


# ------------------------------------------------------------------ clustering


def cluster_key(item: dict) -> str:
    adv = item.get("advisory") or {}
    if adv.get("id"):
        return f"adv:{adv['id']}"
    family = re.split(r"[-_]", item["name"])[0].lower()
    return f"{item['source']}:{family}:{item.get('to')}"


def build_clusters(items: list[dict]) -> dict[str, list[dict]]:
    clusters: dict[str, list[dict]] = {}
    for it in items:
        clusters.setdefault(cluster_key(it), []).append(it)
    return clusters


def _cluster_payload(key: str, members: list[dict]) -> dict:
    rep = max(members, key=lambda m: m.get("size_bytes") or 0)
    adv = rep.get("advisory") or {}
    names = [m["name"] for m in members]
    p = {
        "key": key,
        "source": rep["source"],
        "package": rep["display_name"] if rep["source"] != "dnf" else rep["name"],
        "from": rep.get("from"),
        "to": rep.get("to"),
        "current_risk": rep.get("risk"),
        "requires_reboot": bool(rep.get("requires_reboot")),
    }
    if len(members) > 1:
        p["also_in_cluster"] = names[:12] if len(names) <= 12 else names[:12] + ["…"]
        p["cluster_size"] = len(members)
    if adv:
        p["advisory"] = {
            "id": adv.get("id"),
            "type": adv.get("type"),
            "severity": adv.get("severity"),
        }
    if rep.get("blocked_by"):
        p["blocked_by"] = rep["blocked_by"]
    if rep.get("notes_excerpt"):
        p["release_note"] = rep["notes_excerpt"][:600]
    return p


# ------------------------------------------------------------------ providers


def _ask_claude_cli(payload: list[dict]) -> str:
    """Ask the Claude Code CLI. Returns the assistant's text.

    The CLI exits 0 even for API failures (rate limits, quota), reporting them
    inside the JSON envelope as is_error/api_error_status, so both the exit
    code and the envelope have to be checked.
    """
    prompt = SYSTEM_PROMPT + "\n\nCLUSTERS:\n" + json.dumps({"clusters": payload})
    cp = subprocess.run(
        # Log/package text is partly third-party; triage needs no tools, so allow none.
        [claude_bin(), "-p", "--model", "sonnet", "--output-format", "json", "--no-session-persistence",
         "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=tempfile.gettempdir(),
    )
    raw = (cp.stdout or "").strip()
    if not raw:
        raise RuntimeError(
            f"claude cli rc={cp.returncode}, no stdout: {(cp.stderr or '').strip()[:200]}"
        )
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        if cp.returncode != 0:
            raise RuntimeError(f"claude cli rc={cp.returncode}: {raw[:200]}") from None
        return raw  # not the envelope we expected; let the caller try to parse it
    if outer.get("is_error") or outer.get("subtype") == "error":
        status = outer.get("api_error_status") or outer.get("terminal_reason") or "error"
        raise RuntimeError(f"claude cli {status}: {str(outer.get('result'))[:200]}")
    if cp.returncode != 0:
        raise RuntimeError(f"claude cli rc={cp.returncode}: {str(outer.get('result'))[:200]}")
    text = outer.get("result") or outer.get("text")
    if not text:
        raise RuntimeError("claude cli returned an envelope with no result text")
    return text


# ------------------------------------------------------------------ parsing


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object in model reply")


def _validate(raw: dict, wanted: set[str]) -> dict[str, dict]:
    """Keep only well-formed entries for keys we asked about."""
    results = raw.get("results")
    if isinstance(raw, dict) and results is None:
        # tolerate a bare {key: {...}} mapping
        results = [dict(v, key=k) for k, v in raw.items() if isinstance(v, dict)]
    out: dict[str, dict] = {}
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key not in wanted:
            continue
        verdict = str(entry.get("verdict") or "").strip()
        reasoning = str(entry.get("reasoning") or "").strip()
        if not verdict:
            continue
        rr = entry.get("risk_raise")
        if isinstance(rr, str):
            rr = rr.strip().lower()
        if rr not in RISK_ORDER:
            rr = None
        out[key] = {
            "verdict": verdict[:300],
            "reasoning": reasoning[:1200],
            "risk_raise": rr,
        }
    return out


# ------------------------------------------------------------------ fallback


def deterministic(item: dict) -> dict:
    adv = item.get("advisory") or {}
    sev = adv.get("severity")
    typ = adv.get("type")
    if item.get("op") == "release":
        # the newer-Fedora row (release.py): its own words, never the model's
        import release
        v, r = release.verdict(item.get("release") or {"version": item.get("to")})
        return {"verdict": v, "reasoning": r, "risk_raise": None, "_offline": True}
    if item.get("blocked_by") == "steam_running":
        v = "Held back: a running Steam is using this runtime."
        r = ("This Flatpak runtime is in use by the running Steam sandbox, so replacing it now "
             "would swap libraries out from under a live app. Quit Steam and re-run to apply it.")
    elif typ == "security":
        v = f"Security update ({sev or 'unrated'}) — apply it."
        r = (f"Advisory {adv.get('id')} is a security update rated {sev or 'unrated'}. "
             "The release note is included verbatim below; applying it closes the listed issues.")
    elif item.get("requires_reboot"):
        v = "Takes effect after a reboot."
        r = ("This package is only live once the machine restarts, so the running system keeps "
             "the old code until then. Nothing breaks by staging it now.")
    else:
        v = f"Routine {typ or 'package'} update."
        r = ("No advisory severity and nothing in the boot, kernel-module or graphics path, so "
             "this is a normal version bump.")
    return {"verdict": v, "reasoning": r, "risk_raise": None, "_offline": True}


# ------------------------------------------------------------------ entry point


def triage(items: list[dict], *, quiet: bool = False, force: bool = False) -> dict[str, dict]:
    """Return {item_id: {verdict, reasoning, risk_raise}} for every item."""
    if not items:
        return {}

    cache = load_cache()
    out: dict[str, dict] = {}
    todo: list[dict] = []
    for it in items:
        hit = None if force else cache.get(_cache_key(it))
        if isinstance(hit, dict) and hit.get("verdict"):
            out[it["id"]] = {
                "verdict": hit["verdict"],
                "reasoning": hit.get("reasoning", ""),
                "risk_raise": hit.get("risk_raise"),
            }
        else:
            todo.append(it)

    if not todo:
        log(f"triage: {len(items)} items all cached", quiet=quiet)
        return out

    clusters = build_clusters(todo)
    payload = [_cluster_payload(k, v) for k, v in clusters.items()]
    log(f"triage: {len(todo)} uncached items -> {len(payload)} clusters", quiet=quiet)

    # One giant call for every cluster times out once the backlog is large (232 items -> 76
    # clusters never finished inside 110 s, so nothing was ever cached and every run retried the
    # whole thing). Ask in small chunks, a few in parallel, and keep whatever comes back: each
    # answered chunk is cached, so a slow or failed chunk only costs that chunk next time.
    parsed: dict[str, dict] = {}
    chunks = [payload[i:i + CHUNK_CLUSTERS] for i in range(0, len(payload), CHUNK_CLUSTERS)]

    def _run(chunk: list[dict]) -> dict[str, dict]:
        return _validate(_extract_json(_ask_claude_cli(chunk)), {c["key"] for c in chunk})

    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = [ex.submit(_run, ch) for ch in chunks]
        for fut in as_completed(futs):
            try:
                parsed.update(fut.result())
            except (OSError, subprocess.SubprocessError, ValueError, KeyError,
                    RuntimeError, json.JSONDecodeError) as exc:
                failed += 1
                log(f"triage: claude-cli/sonnet chunk failed: {type(exc).__name__}: {str(exc)[:180]}", quiet=quiet)
    log(f"triage: claude-cli/sonnet answered {len(parsed)}/{len(payload)} clusters "
        f"in {len(chunks)} chunks ({failed} failed)", quiet=quiet)

    # fan cluster verdicts out to members; fall back per item
    for ckey, members in clusters.items():
        got = parsed.get(ckey)
        for it in members:
            res = dict(got) if got else deterministic(it)
            res.setdefault("risk_raise", None)
            out[it["id"]] = {
                "verdict": res["verdict"],
                "reasoning": res.get("reasoning", ""),
                "risk_raise": res.get("risk_raise"),
            }
            if got:  # only cache real model answers
                cache[_cache_key(it)] = out[it["id"]]

    save_cache(cache)
    return out
