"""What each Other-software row IS and who uses it (2026-10-31, CONTRACT.md "Other software").

Each row gets two short lines, filled cheapest source first; a later layer only fills
what the earlier ones left empty:

  1. the collectors themselves: registry descriptions (npm, PyPI, crates.io,
     extensions.gnome.org, Docker Hub) and, for Home Assistant, the entity's kind
     (core / os / supervisor / add-on / integration / firmware) -> `what`, `used_by`;
  2. STATIC below (things known about this box) plus a grep of a few local files
     (quadlets, compose files, projects' package.json / scripts, user units) -> `used_by`;
  3. `claude -p` once per name+kind (the house pattern: the Claude Code CLI on the
     subscription, no API key), cached in know-cache.json. Lines written that way are
     listed in the row's `ai`, and the UI tags them "AI".

Nothing here may hold up collection: layer 3 runs after state.json is written, with a
hard budget, and a timeout or a refusal just leaves the line empty (retried after
FAIL_RETRY_S).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import STATE_DIR, write_json_atomic

HOME = Path.home()
CACHE_PATH = STATE_DIR / "know-cache.json"


def cache_path() -> Path:
    return CACHE_PATH
MODEL = os.environ.get("RIVET_KNOW_MODEL", "haiku")
AI_TIMEOUT_S = 60
AI_BUDGET_S = 150
AI_PARALLEL = 4
FAIL_RETRY_S = 6 * 3600       # a refused / timed-out lookup is tried again after this
_LOCK = threading.Lock()

# (source, key) -> (what, used_by). key: the package / crate / tool name, the image repo
# without registry or tag ("couchdb", "temporalio/auto-setup"), the HA title, the
# extension uuid. None = "let a later layer fill it". Only generic entries ship here;
# entries about one particular box go in the local overrides (see load_local below).
GENERIC_STATIC: dict[tuple[str, str], tuple[str | None, str | None]] = {
    ("npm", "pnpm"): ("JavaScript package manager, a faster npm.", None),   # used_by: pnpm_usage()
    ("cargo", "tauri-driver"): ("WebDriver server for Tauri apps.", None),
    ("podman", "couchdb"): ("Apache CouchDB, a JSON document database.", None),
    ("podman", "postgres"): ("PostgreSQL database server.", None),
    ("podman", "redis"): ("Redis, an in-memory key-value store.", None),
    ("podman", "ghost"): ("Ghost, the blogging platform.", None),
    ("podman", "temporalio/auto-setup"): ("Temporal workflow server.", None),
    ("ha", "Home Assistant Core"): ("Home Assistant itself.", None),
    ("ha", "Home Assistant Operating System"): ("HA OS, the Linux system the Home Assistant VM boots.", None),
    ("ha", "Home Assistant Supervisor"): ("Runs the add-ons, backups and updates on the Home Assistant VM.", None),
    ("ha", "Matter Server"): ("The Matter controller Home Assistant uses to talk to Matter and Thread devices.", None),
    ("ha", "Terminal & SSH"): ("SSH server and web terminal for the Home Assistant VM.", None),
    ("ha", "Samba share"): ("Samba file shares for the Home Assistant config, media and backup folders.", None),
    ("ha", "Mosquitto broker"): ("MQTT broker that MQTT devices and bridges talk through.", None),
    ("ha", "Piper"): ("Local text-to-speech for Home Assistant voice.", None),
    ("ha", "Whisper"): ("Local speech-to-text for Home Assistant voice.", None),
    ("ha", "Alexa Media Player"): ("Controls Amazon Echo devices from Home Assistant (announce, play, routines).", None),
    ("ha", "HACS"): ("Home Assistant Community Store: installs and updates community integrations.", None),
}
STATIC: dict[tuple[str, str], tuple[str | None, str | None]] = dict(GENERIC_STATIC)

# container / unit name prefix -> the app it belongs to (local overrides only)
APP_NAMES: dict[str, str] = {}


# 2026-11-01: containers that hold data
# never get an Update button. By image repo (image_key) or by the app their container belongs
# to (APP_NAMES prefix of the container / unit name).
STATEFUL_IMAGES = {"couchdb", "postgres", "redis", "valkey", "mariadb", "mysql", "mongo",
                   "temporalio/auto-setup", "temporalio/server", "ghost"}
STATEFUL_APPS: set[str] = set()          # local overrides only
# app -> (title, url) of its runbook, the "Open runbook" button (local overrides only)
RUNBOOKS: dict[str, tuple[str, str]] = {}

# ---- local overrides (git-ignored) ----------------------------------------------------------
# What this particular box runs (its own tools, container apps, runbook links) lives in
# backend/know.local.json, which is git-ignored, or in the file $RIVET_KNOW_LOCAL names:
#   {"static":        [["npm", "some-tool", "What it is.", "Who uses it" | null], ...],
#    "app_names":     {"<container/unit prefix>": "<app name>", ...},
#    "stateful_apps": ["<prefix>", ...],
#    "runbooks":      {"<prefix>": ["<title>", "<url>"], ...}}
LOCAL_PATH = Path(os.environ.get("RIVET_KNOW_LOCAL") or Path(__file__).with_name("know.local.json"))


def load_local(path=None) -> bool:
    """Reset to the shipped entries, then lay the local overrides over them (in place, so a
    module holding `from know import STATIC` sees the result). False when there is no file."""
    STATIC.clear()
    STATIC.update(GENERIC_STATIC)
    APP_NAMES.clear()
    STATEFUL_APPS.clear()
    RUNBOOKS.clear()
    try:
        data = json.loads(Path(path or LOCAL_PATH).read_text())
    except (OSError, ValueError):
        return False
    for row in data.get("static") or []:
        src, key, what, used = (list(row) + [None] * 4)[:4]
        STATIC[(src, key)] = (what, used)
    APP_NAMES.update(data.get("app_names") or {})
    STATEFUL_APPS.update(data.get("stateful_apps") or [])
    RUNBOOKS.update({k: tuple(v) for k, v in (data.get("runbooks") or {}).items()})
    return True


load_local()
MANUAL_NOTE = "Manual: holds data, upgrade by hand"
# image repo -> its release notes (the container row's link; else the registry's tags page)
RELEASE_NOTES = {
    "couchdb": "https://docs.couchdb.org/en/stable/whatsnew/index.html",
    "postgres": "https://www.postgresql.org/docs/release/",
    "redis": "https://github.com/redis/redis/releases",
    "temporalio/auto-setup": "https://github.com/temporalio/temporal/releases",
    "ghost": "https://github.com/TryGhost/Ghost/releases",
}


def container_app(names, units=()) -> str | None:
    for n in list(names or []) + [str(u).removesuffix(".service") for u in units or []]:
        for app in APP_NAMES:
            if str(n) == app or str(n).startswith(app + "-"):
                return app
    return None


def container_policy(image: str, names, units=()) -> dict:
    """{stateful, app, runbook, runbook_title} for one image and the containers running it."""
    app = container_app(names, units)
    stateful = image_key(image) in STATEFUL_IMAGES or app in STATEFUL_APPS
    title, url = RUNBOOKS.get(app, (None, None)) if stateful else (None, None)
    return {"stateful": stateful, "app": app, "runbook": url, "runbook_title": title}


def tags_page(image: str) -> str | None:
    """The registry's tags page for an image, when there is a well-known one."""
    s = str(image).split("@", 1)[0]
    reg = s.split("/")[0] if "/" in s and ("." in s.split("/")[0] or ":" in s.split("/")[0]) else "docker.io"
    key = image_key(image)
    if reg in ("docker.io", "index.docker.io"):
        return f"https://hub.docker.com/_/{key}/tags" if "/" not in key else f"https://hub.docker.com/r/{key}/tags"
    if reg == "ghcr.io" and "/" in key:
        return f"https://github.com/{key.split('/')[0]}/{key.split('/')[1]}/pkgs/container/{key.split('/')[-1]}"
    if reg == "quay.io":
        return f"https://quay.io/repository/{key}?tab=tags"
    return None


def container_link(image: str) -> str | None:
    return RELEASE_NOTES.get(image_key(image)) or tags_page(image)


def tilde(p) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def first_sentence(text, limit: int = 160) -> str | None:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return None
    m = re.match(r"^(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    if len(t) > limit:
        t = t[:limit - 1].rsplit(" ", 1)[0] + "…"
    if t[-1] not in ".!?…":
        t += "."
    return t[0].upper() + t[1:]


def image_key(image: str) -> str:
    """docker.io/library/postgres:17-alpine -> postgres; ghcr.io/a/b:1 -> a/b."""
    s = str(image).split("@", 1)[0]
    parts = s.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if parts and parts[0] == "library":
        parts = parts[1:]
    s = "/".join(parts)
    return s.rsplit(":", 1)[0] if ":" in s.split("/")[-1] else s


def key_of(it: dict) -> str:
    if it["source"] == "podman":
        return image_key(it["name"])
    if it["source"] == "gnome-ext":
        return it["id"].split(":", 1)[1]
    return str(it["name"])


# --------------------------------------------------------------------------- local usage
def _projects(home: Path) -> list[Path]:
    root = home / "projects"
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name != "rivet"
                      and not p.name.startswith("."))
    except OSError:
        return []


def _read(p: Path, limit: int = 1 << 20) -> str:
    try:
        if p.stat().st_size > limit:
            return ""
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _head(p: Path, n: int = 4096) -> str:
    try:
        with open(p, "rb") as fh:
            return fh.read(n).decode("utf-8", "replace")
    except OSError:
        return ""


def pname(p: Path) -> str:
    return p.name[:1].upper() + p.name[1:]


def project_label(p: Path) -> str:
    """~/projects/notes -> "Notes (~/projects/notes)"."""
    return f"{pname(p)} ({tilde(p)})"


def pnpm_usage(home: Path | None = None) -> tuple[list[Path], dict]:
    """Projects with a pnpm-lock.yaml, and per project what pins the pnpm version:
    {path: {"pin": "9.15.4" | None, "lockfile": "9.0" | None, "scripts": ["9"]}}."""
    out, facts = [], {}
    for p in _projects(home or HOME):
        lock = p / "pnpm-lock.yaml"
        if not lock.is_file():
            continue
        out.append(p)
        pin = None
        try:
            pm = json.loads(_read(p / "package.json") or "{}").get("packageManager") or ""
            m = re.match(r"pnpm@(\d[\w.+-]*)", pm)
            pin = m.group(1) if m else None
        except ValueError:
            pass
        lv = re.search(r"^lockfileVersion:\s*'?([\d.]+)", _head(lock), re.M)
        scripts = set()
        for s in list((p / "scripts").glob("*.sh"))[:40] if (p / "scripts").is_dir() else []:
            scripts.update(re.findall(r"pnpm@(\d+)", _read(s, 200_000)))
        facts[p] = {"pin": pin, "lockfile": lv.group(1) if lv else None, "scripts": sorted(scripts)}
    return out, facts


def pnpm_warning(installed, latest, facts: dict) -> str | None:
    """The major-bump warning on the global pnpm row (2026-10-31: pnpm 9 -> 12)."""
    from elsewhere import vtuple
    a, b = vtuple(installed), vtuple(latest)
    if not facts or not a or not b or a[0] == b[0]:
        return None
    bits = []
    for p, f in facts.items():
        name = pname(p)
        if f["pin"]:
            bits.append(f"{name} pins {f['pin'].split('.')[0]}.x (packageManager), so the global "
                        f"version barely matters there.")
        else:
            why = [f"lockfile format {f['lockfile']}"] if f["lockfile"] else []
            if f["scripts"]:
                why.append("its scripts install pnpm@" + "/".join(f["scripts"]))
            bits.append(f"{name} does not pin pnpm ({'; '.join(why) or 'no packageManager field'}), "
                        f"so 'pnpm install' there would run {b[0]}.x. Build {name} after, or stay on {a[0]}.")
    return f"Major jump {a[0]} → {b[0]}. " + " ".join(bits)


def _mention_files(home: Path) -> list[tuple[str, Path]]:
    files = []
    for p in _projects(home):
        for f in ("package.json", "Makefile", "justfile", "README.md", "CLAUDE.md"):
            if (p / f).is_file():
                files.append((project_label(p), p / f))
        if (p / "scripts").is_dir():
            files += [(project_label(p), s) for s in sorted((p / "scripts").glob("*.sh"))[:40]]
    units = home / ".config/systemd/user"
    if units.is_dir():
        files += [(u.name, u) for u in sorted(units.glob("*.service"))]
    return files[:600]


def mentions(name: str, home: Path | None = None, files=None) -> list[str]:
    """Who mentions `name` (word match): user units first, then projects. Cheap grep."""
    short = name.split("/")[-1]
    pats = {re.compile(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])") for n in {name, short} if len(n) >= 3}
    hits_units, hits_proj = [], []
    for label, f in files if files is not None else _mention_files(home or HOME):
        txt = _read(f, 400_000)
        if txt and any(p.search(txt) for p in pats):
            (hits_units if f.suffix == ".service" else hits_proj).append(label)
    # a unit named after the thing first (verdaccio-registry.service), shortest name first
    hits_units.sort(key=lambda u: (short not in u, len(u), u))
    seen, out = set(), []
    for h in hits_units + hits_proj:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def compose_refs(image: str, home: Path | None = None) -> list[Path]:
    """Compose files under ~/projects/*/ that reference this image (any registry prefix)."""
    want = image_key(image)
    tag = image.split("@", 1)[0].rsplit(":", 1)[1] if ":" in image.split("/")[-1] else "latest"
    out = []
    for p in _projects(home or HOME):
        for f in sorted(p.glob("*compose*.y*ml")):
            for m in re.finditer(r"^\s*image:\s*['\"]?([^\s'\"#]+)", _read(f), re.M):
                ref = m.group(1)
                rtag = ref.rsplit(":", 1)[1] if ":" in ref.split("/")[-1] else "latest"
                if image_key(ref) == want and rtag == tag:
                    out.append(f)
                    break
    return out


def app_for(units: list[str], containers: list[str]) -> str | None:
    for n in list(units) + list(containers):
        head = n.split("-", 1)[0].split(".", 1)[0]
        if head in APP_NAMES:
            return APP_NAMES[head]
    return None


# --------------------------------------------------------------------------- enrich
def enrich(items: list, *, home: Path | None = None, cache: dict | None = None) -> list:
    """Fill `what` / `used_by` (layers 1-2 + cached AI answers) in place. Offline."""
    home = home or HOME
    cache = load_cache() if cache is None else cache
    files = None
    pnpm = None
    for it in items:
        it.setdefault("what", None)
        it.setdefault("used_by", None)
        it.setdefault("ai", [])
        it.setdefault("warning", None)
        if it["id"].endswith(":_source"):
            continue
        src, key = it["source"], key_of(it)
        what, used = STATIC.get((src, key), (None, None))
        if what:
            it["what"] = what
        if src == "npm" and key == "pnpm":
            if pnpm is None:
                pnpm = pnpm_usage(home)
            projs, facts = pnpm
            if projs and not it["used_by"]:
                it["used_by"] = "Builds " + ", ".join(project_label(p) for p in projs)
            if it["status"] == "ok":
                it["warning"] = pnpm_warning(it["installed"], it["latest"], facts) or it["warning"]
        if src == "podman":
            units, conts = it.get("units") or [], it.get("containers") or []
            app = used or app_for(units, conts)
            refs = list(dict.fromkeys(tilde(f.parent) for f in compose_refs(it["name"], home)))
            where = [*units] + [f"compose in {r}" for r in refs if not units]
            if app or where:
                it["used_by"] = it["used_by"] or (f"{app or 'Containers'}" + (f" ({', '.join(where)})" if where else ""))
        elif used and not it["used_by"]:
            it["used_by"] = used
        if src in ("npm", "pipx", "uv", "cargo") and not it["used_by"]:
            if files is None:
                files = _mention_files(home)
            hits = mentions(key, home, files)
            if hits:
                it["used_by"] = (", ".join(hits) if len(hits) <= 3 else
                                 f"{hits[0]}, {hits[1]} and {len(hits) - 2} more")
        apply_cached(it, cache)
    return items


# --------------------------------------------------------------------------- AI (claude -p)
KIND_DESC = {
    "npm": "a globally installed npm package", "pipx": "a Python CLI installed with pipx",
    "uv": "a Python CLI installed with uv tool", "cargo": "a Rust program installed with cargo install",
    "podman": "a container image run with podman", "gnome-ext": "a GNOME Shell extension",
    "appimage": "an AppImage desktop app",
    "ha:addon": "a Home Assistant add-on (app)", "ha:integration": "a Home Assistant custom integration from HACS",
    "ha:firmware": "device firmware reported by Home Assistant", "ha": "a Home Assistant update",
}


def cache_key(it: dict) -> str:
    kind = it["source"] + (f":{it['kind']}" if it["source"] == "ha" and it.get("kind") else "")
    return f"{kind}:{key_of(it)}"


def load_cache() -> dict:
    try:
        d = json.loads(cache_path().read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_cache(cache: dict) -> None:
    with _LOCK:
        write_json_atomic(cache_path(), cache)


def apply_cached(it: dict, cache: dict) -> bool:
    hit = cache.get(cache_key(it)) or {}
    changed = False
    for field, ck in (("what", "what"), ("used_by", "used_for")):
        if not it.get(field) and hit.get(ck):
            it[field] = hit[ck]
            it["ai"] = sorted(set(it.get("ai") or []) | {field})
            changed = True
    return changed


def needs_ai(it: dict, cache: dict, now: float | None = None) -> bool:
    if it["id"].endswith(":_source") or it["status"] not in ("ok", "cant_check", "updated"):
        return False
    if it.get("what") and it.get("used_by"):
        return False
    hit = cache.get(cache_key(it))
    if not hit:
        return True
    if hit.get("what") or hit.get("used_for") or hit.get("unknown"):
        return False                    # answered once (or "never heard of it"): never again
    return (now or time.time()) - float(hit.get("failed_at") or 0) > FAIL_RETRY_S


def prompt_for(it: dict) -> str:
    kind = KIND_DESC.get(f"ha:{it.get('kind')}" if it["source"] == "ha" else it["source"]) or KIND_DESC["ha"]
    return (f'Describe the software "{key_of(it)}" ({kind}). Reply with ONE JSON object and nothing '
            'else: {"what": one plain sentence, at most 20 words, saying what it is; "used_for": a '
            'short phrase, at most 12 words, saying what it is typically used for on a Fedora Linux '
            'workstation or a home-lab Home Assistant setup}. No marketing words. If you do not '
            'recognise it, answer {"what": null, "used_for": null}.')


def claude_bin() -> str | None:
    for c in (os.environ.get("RIVET_CLAUDE_BIN"), shutil.which("claude"),
              str(HOME / ".local/bin/claude"), "/usr/local/bin/claude", "/usr/bin/claude"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def ask_claude(prompt: str, timeout: float = AI_TIMEOUT_S) -> dict:
    """One `claude -p` call -> {"what", "used_for"}. Raises on any failure."""
    exe = claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found")
    cp = subprocess.run([exe, "-p", "--model", MODEL, "--output-format", "json",
                         "--no-session-persistence", "--permission-prompts", "none",
                         "--setting-sources", "", "--tools", "", "--max-turns", "2"],
                        input=prompt, capture_output=True, text=True, timeout=timeout,
                        cwd=str(STATE_DIR) if STATE_DIR.is_dir() else None)
    outer = json.loads((cp.stdout or "").strip() or "{}")
    if outer.get("is_error") or cp.returncode != 0:
        raise RuntimeError(f"claude: {str(outer.get('result') or cp.stderr)[:160]}")
    text = str(outer.get("result") or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("claude: no JSON in the answer")
    d = json.loads(m.group(0))
    return {"what": first_sentence(d.get("what"), 180) if d.get("what") else None,
            "used_for": (re.sub(r"\s+", " ", str(d["used_for"])).strip().rstrip(".")[:120]
                         if d.get("used_for") else None)}


def fill_ai(items: list, *, ask=ask_claude, budget: float = AI_BUDGET_S,
            cache: dict | None = None, log=lambda m: None) -> int:
    """Ask claude -p about every row still missing a line (once per name+kind, cached).
    Returns how many rows changed. Never raises; failures leave the lines empty."""
    cache = load_cache() if cache is None else cache
    todo: dict[str, dict] = {}
    for it in items:
        if needs_ai(it, cache):
            todo.setdefault(cache_key(it), it)
    if todo:
        t0 = time.monotonic()
        results: dict = {}

        def one(k):
            if time.monotonic() - t0 > budget:
                return
            try:
                results[k] = ask(prompt_for(todo[k]), timeout=min(AI_TIMEOUT_S, max(5, budget - (time.monotonic() - t0))))
            except Exception as exc:  # noqa: BLE001 - a failure only leaves the line empty
                results[k] = exc

        with ThreadPoolExecutor(max_workers=AI_PARALLEL) as ex:
            list(ex.map(one, list(todo)))
        now = time.time()
        for k, r in results.items():
            if isinstance(r, Exception):
                cache[k] = {"failed_at": now, "error": str(r)[:200]}
            elif r.get("what") or r.get("used_for"):
                cache[k] = dict(r, at=now, model=MODEL)
            else:
                cache[k] = {"unknown": True, "at": now, "model": MODEL}
        ok = sum(1 for r in results.values() if not isinstance(r, Exception))
        log(f"know: claude -p answered {ok}/{len(todo)} ({time.monotonic() - t0:.1f}s)")
        try:
            save_cache(cache)
        except OSError as exc:
            log(f"know: cache not saved: {exc}")
    return sum(1 for it in items if apply_cached(it, cache))
