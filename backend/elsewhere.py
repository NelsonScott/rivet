"""Other software: what else on this box is stale, outside dnf and Flatpak.

Added 2026-10-31 as "Elsewhere", shown as "Other software" since the same day; see
CONTRACT.md "Other software" (the state.json key stays `elsewhere`). One small collector
per source, each returning the same row shape; a registry runs them concurrently, each
with its own deadline, and `collect()` folds them into the `elsewhere` object:

    row = {id, source, name, installed, latest, severity, checked_at, status, link, hint,
           command, what, used_by, ai, warning, where, action, confirm, + per-source facts}
    severity: major | minor | patch | unknown      status: ok | cant_check | failed | updated
                                                           | rebuilt (podman: same version, new digest)
    action:   update | ha_install | open | copy | runbook | null

Collecting never installs, upgrades or pulls anything; the Update / Install in HA buttons
go through `rivet-apply --other-update ID`, Remove (an unused HA add-on) through
`rivet-apply --other-remove ID` (elsewhere_apply.py). A collector that breaks
or runs out of time is reported as `failed`, never as "up to date" (the PolyGet rule).
"""
from __future__ import annotations

import errno
import json
import os
import re
import shlex
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import CONFIG_DIR, now_iso, run
from know import MANUAL_NOTE, container_link, container_policy, image_key, tilde

HOME = Path.home()
UA = "rivet-elsewhere/1 (Fedora workstation update checker; read-only)"
DEFAULT_TIMEOUT = 10.0        # seconds per collector (CONTRACT.md)
# Home Assistant's own default hostname (mDNS); HA_URL overrides it. HA_URL and HA_TOKEN come
# from the environment, else from ~/.config/rivet/ha.env (NAME=value lines). No HA_TOKEN anywhere
# = Home Assistant is not set up for Rivet: the source is `absent` and shows nothing (2026-11-01).
HA_URL_DEFAULT = "http://homeassistant.local:8123"
HA_ENV_FILE = CONFIG_DIR / "ha.env"
HA_KEYS = ("HA_URL", "HA_TOKEN")
SEVERITY_RANK = {"major": 0, "minor": 1, "patch": 2, "unknown": 3}


# --------------------------------------------------------------------------- versions
_LEAD = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def vtuple(v) -> tuple | None:
    """'v1.34.0' -> (1, 34, 0); '2026.10.8' -> (2026, 10, 8); 'abc' -> None."""
    m = _LEAD.match(str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _pad(t: tuple, n: int) -> tuple:
    return t + (0,) * (n - len(t))


def is_newer(installed, latest) -> bool:
    """latest > installed. Unparsable versions: newer when the strings differ."""
    a, b = vtuple(installed), vtuple(latest)
    if a is None or b is None:
        return bool(latest) and str(latest).strip() != str(installed or "").strip()
    n = max(len(a), len(b))
    return _pad(b, n) > _pad(a, n)


def severity(installed, latest) -> str:
    """Positional bump: first differing component 0 = major, 1 = minor, later = patch.
    Single-number versions (extensions.gnome.org uploads) and anything unparsable are
    `unknown`: a counter says nothing about how big the change is. CalVer is read
    positionally too (2026.10.3 -> 2026.10.5 is a patch, 2026.10 -> 2026.11 a minor)."""
    a, b = vtuple(installed), vtuple(latest)
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return "unknown"
    n = max(len(a), len(b), 3)
    a, b = _pad(a, n), _pad(b, n)
    for i in range(n):
        if a[i] != b[i]:
            return "major" if i == 0 else "minor" if i == 1 else "patch"
    return "unknown"


# --------------------------------------------------------------------------- context
class Ctx:
    """What a collector may touch: subprocesses, HTTP, the environment. Tests swap it."""

    def __init__(self, *, deadline: float | None = None, env: dict | None = None,
                 ha_env_file: Path | None | bool = True):
        self.deadline = deadline
        self.env = os.environ if env is None else env
        self.desc: dict = {}          # name -> registry description, filled by the lookups
        # ~/.config/rivet/ha.env is read only for the real environment: a test's env dict
        # never picks up the user's file unless the test names one.
        if ha_env_file is True:
            ha_env_file = HA_ENV_FILE if self.env is os.environ else None
        self.ha_env_file = ha_env_file or None

    def ha_setting(self, name: str) -> str | None:
        """HA_URL / HA_TOKEN: the environment first, then the ha.env file."""
        val = self.env.get(name)
        if val:
            return val
        return read_env_file(self.ha_env_file).get(name) if self.ha_env_file else None

    def left(self, cap: float = DEFAULT_TIMEOUT) -> float:
        if self.deadline is None:
            return cap
        return max(0.5, min(cap, self.deadline - time.monotonic()))

    def which(self, name: str, *fallbacks: str) -> str | None:
        p = shutil.which(name)
        if p:
            return p
        for f in fallbacks:
            f = os.path.expanduser(f)
            if os.path.isfile(f) and os.access(f, os.X_OK):
                return f
        return None

    def run(self, cmd: list[str], timeout: float | None = None, env: dict | None = None):
        return run(cmd, timeout=max(1, int(timeout if timeout is not None else self.left())), env=env)

    def http_json(self, url: str, *, headers: dict | None = None, timeout: float | None = None,
                  data=None):
        """GET url (POST when `data` is given, as JSON) -> parsed JSON.
        Raises HttpError(status) / OSError on failure."""
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": UA, "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout if timeout is not None else self.left()) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, url) from None

    def http_text(self, url: str, *, headers: dict | None = None, timeout: float | None = None,
                  limit: int = 8 * 2 ** 20) -> str:
        """GET url -> text (at most `limit` bytes, the TAIL kept: logs grow at the end).
        Raises HttpError(status) / OSError on failure."""
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/plain", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout if timeout is not None else self.left()) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, url) from None
        return body[-limit:].decode("utf-8", "replace")

    def read_bytes(self, path: Path, limit: int) -> bytes:
        with open(path, "rb") as fh:
            return fh.read(limit)

    def ha_ws(self, base: str, token: str, timeout: float = 15) -> "HaWs":
        """An authenticated Home Assistant websocket (Remove only; tests swap it)."""
        return HaWs(base, token, timeout=timeout)


class HttpError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {urllib.parse.urlsplit(url).netloc}")
        self.status = status


class HaWsError(Exception):
    """A websocket command HA (or the Supervisor behind it) answered with success: false."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class HaWs:
    """Home Assistant's websocket API, for the Supervisor calls the Remove button needs.

    2026-11-01: the REST proxy `/api/hassio/addons/<slug>/...` answers 401 to a long-lived
    token for everything but a few log/changelog paths (HA closed it in 2023), so add-on
    info and uninstall go through `supervisor/api` on the websocket, exactly as HA's own
    frontend does. Needs an admin user's token (the HA_TOKEN one is)."""

    def __init__(self, base: str, token: str, *, timeout: float = 15):
        from websockets.sync.client import connect   # python3-websockets (Fedora RPM)
        url = re.sub(r"^http", "ws", base.rstrip("/")) + "/api/websocket"
        self.timeout = timeout
        self.ws = connect(url, open_timeout=timeout, close_timeout=2, max_size=16 * 2 ** 20,
                          user_agent_header=UA)
        self.n = 0
        try:
            json.loads(self.ws.recv(timeout))                    # auth_required
            self.ws.send(json.dumps({"type": "auth", "access_token": token}))
            r = json.loads(self.ws.recv(timeout))
        except Exception:
            self.close()
            raise
        if r.get("type") != "auth_ok":
            self.close()
            raise HaWsError("Home Assistant refused the token on its websocket; check HA_TOKEN", "auth")

    def call(self, msg: dict, timeout: float | None = None):
        """Send one command, wait for its result (other messages are skipped) -> `result`."""
        self.n += 1
        self.ws.send(json.dumps(dict(msg, id=self.n)))
        end = time.monotonic() + (timeout or self.timeout)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out")
            r = json.loads(self.ws.recv(left))
            if r.get("id") != self.n or r.get("type") != "result":
                continue
            if not r.get("success"):
                err = r.get("error") or {}
                raise HaWsError(str(err.get("message") or "Home Assistant said no"), err.get("code"))
            return r.get("result")

    def supervisor(self, endpoint: str, method: str = "get", data: dict | None = None,
                   timeout: float = 30):
        """`supervisor/api`: the Supervisor's own REST API, proxied by HA core for an admin."""
        msg = {"type": "supervisor/api", "endpoint": endpoint, "method": method, "timeout": timeout}
        if data is not None:
            msg["data"] = data
        return self.call(msg, timeout=timeout + 10)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


_GONE_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ECONNRESET}


def unreachable(exc: BaseException) -> bool:
    """Nobody is there (connection refused, no route, the name does not resolve), as opposed
    to a slow answer: a timeout may still be the server working on it (2026-11-01 abuse pass)."""
    r = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(r, (socket.gaierror, ConnectionRefusedError, ConnectionResetError)):
        return True
    return isinstance(r, OSError) and getattr(r, "errno", None) in _GONE_ERRNOS


def _why(exc: BaseException) -> str:
    if isinstance(exc, HttpError):
        return str(exc)
    r = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(r, socket.gaierror):
        return "the name does not resolve"
    if isinstance(r, ConnectionRefusedError) or getattr(r, "errno", None) == errno.ECONNREFUSED:
        return "connection refused"
    if isinstance(r, OSError) and getattr(r, "errno", None) in _GONE_ERRNOS:
        return (getattr(r, "strerror", None) or str(r)).lower()
    if isinstance(exc, urllib.error.URLError):
        return "timed out" if "timed out" in str(r) else str(r)
    if isinstance(exc, TimeoutError) or "timed out" in str(exc):
        return "timed out"
    return f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------------- rows
def row(source: str, name: str, installed, latest, *, link=None, hint=None, command=None,
        status: str = "ok", sev: str | None = None, key: str | None = None, what=None,
        used_by=None, where=None, **facts) -> dict:
    return {
        "id": f"{source}:{key or name}",
        "source": source,
        "name": name,
        "installed": None if installed is None else str(installed),
        "latest": None if latest is None else str(latest),
        "severity": sev or (severity(installed, latest) if status == "ok" else "unknown"),
        "checked_at": now_iso(),
        "status": status,
        "link": link,
        "hint": hint,
        "command": command,
        "what": what,                 # one sentence: what it is (know.py fills the gaps)
        "used_by": used_by,           # short phrase: who uses it / where it lives
        "ai": [],                     # which of what / used_by claude -p wrote
        "warning": None,
        "where": where,
        "action": None,               # set by decide_action() in collect()
        "confirm": False,
        **facts,
    }


def result(status="ok", *, checked=0, items=None, error=None, hint=None) -> dict:
    return {"status": status, "checked": checked, "items": items or [], "error": error, "hint": hint}


def _lookup_all(names, fn, ctx: Ctx, workers: int = 8) -> dict:
    """{name: latest | Exception} for fn(name) run in parallel within the deadline."""
    out: dict = {}
    if not names:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, len(names))) as ex:
        futs = {n: ex.submit(fn, n) for n in names}
        for n, f in futs.items():
            try:
                out[n] = f.result(timeout=ctx.left())
            except Exception as exc:  # noqa: BLE001 - every failure becomes a row
                out[n] = exc
    return out


def _registry_rows(source: str, installed: dict, latest: dict, *, title: str, host: str,
                   command, link, desc: dict | None = None) -> dict:
    """Shared tail of the pipx / uv / cargo collectors: compare, build rows, and fail
    the source as a whole when no lookup at all came back."""
    items, errors = [], []
    for name, ver in sorted(installed.items()):
        got = latest.get(name)
        if isinstance(got, Exception) or got is None:
            errors.append((name, got))
            continue
        if is_newer(ver, got):
            items.append(row(source, name, ver, got, command=command(name), link=link(name),
                             what=_desc(desc, name)))
    if errors and len(errors) == len(installed):
        why = _why(errors[0][1]) if isinstance(errors[0][1], Exception) else "no version returned"
        return result("failed", checked=len(installed),
                      error=f"Could not reach {host} ({why}). Not known to be current.")
    for name, exc in errors:
        why = _why(exc) if isinstance(exc, Exception) else "no version returned"
        items.append(row(source, name, installed[name], None, status="failed",
                         hint=f"{host} lookup failed ({why}).", link=link(name)))
    return result(checked=len(installed), items=items)


# --------------------------------------------------------------------------- npm
def _desc(desc: dict | None, name: str) -> str | None:
    from know import first_sentence
    return first_sentence((desc or {}).get(name)) if desc else None


def parse_npm_outdated(text: str) -> dict:
    """`npm outdated -g --json` -> {name: {current, latest}}; raises on an npm error doc."""
    doc = json.loads(text or "{}")
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        raise RuntimeError(doc["error"].get("summary") or doc["error"].get("code") or "npm error")
    return {k: v for k, v in (doc or {}).items() if isinstance(v, dict)}


NVM_NODE = HOME / ".nvm/versions/node"
NPM_PREFIXES = ("~/.npm-global", "~/.npm-canvas-new")
NODE_BUNDLED = {"npm", "corepack"}        # ship inside an nvm Node: updated with Node itself


def npm_bin(home: Path | None = None, env=None) -> str | None:
    """The npm to drive: $RIVET_NPM_BIN, else the newest nvm Node's npm (the `npm` /
    `node` shell functions are broken in agent shells, and /usr/bin/npm is Node 22),
    else whatever is on PATH."""
    home = home or HOME              # looked up at call time, so a test can point HOME elsewhere
    env = os.environ if env is None else env
    if env.get("RIVET_NPM_BIN"):
        return env["RIVET_NPM_BIN"]
    best = None
    for d in (home / ".nvm/versions/node").glob("v*"):
        if (d / "bin/npm").is_file() and (best is None or (vtuple(d.name) or ()) > (vtuple(best.name) or ())):
            best = d
    if best:
        return str(best / "bin/npm")
    return shutil.which("npm") or ("/usr/bin/npm" if os.path.exists("/usr/bin/npm") else None)


def node_env(npm: str) -> dict:
    """PATH with npm's own bin dir first, so `#!/usr/bin/env node` finds the matching Node;
    npm's update / funding notices off (they are noise in a streamed log)."""
    return {"PATH": os.path.dirname(npm) + os.pathsep + os.environ.get("PATH", "/usr/bin"),
            "NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_FUND": "false"}


def npm_prefixes(ctx: Ctx, npm: str, home: Path | None = None) -> list[dict]:
    """Every global prefix with a lib/node_modules: `npm prefix -g`, ~/.npm-global,
    ~/.npm-canvas-new, $RIVET_NPM_PREFIXES (colon list) and each nvm Node dir
    (driven by that Node's own npm). Deduplicated by real path."""
    home = home or HOME
    cands = []
    cp = ctx.run([npm, "prefix", "-g"], timeout=ctx.left(4), env=node_env(npm))
    if cp.returncode == 0 and cp.stdout.strip():
        cands.append((cp.stdout.strip().splitlines()[-1], npm))
    extra = [p for p in str(ctx.env.get("RIVET_NPM_PREFIXES") or "").split(":") if p]
    for p in list(NPM_PREFIXES) + extra:
        cands.append((p.replace("~", str(home), 1) if p.startswith("~") else p, npm))
    for d in sorted((home / ".nvm/versions/node").glob("v*")):
        own = d / "bin/npm"
        cands.append((str(d), str(own) if own.is_file() else npm))
    out, seen = [], set()
    for p, n in cands:
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        if real in seen or not os.path.isdir(os.path.join(real, "lib", "node_modules")):
            continue
        seen.add(real)
        nvm = real.startswith(str((home / ".nvm/versions/node").resolve()) + os.sep) if (home / ".nvm").exists() else False
        out.append({"path": real, "label": tilde(real), "npm": n, "nvm": nvm})
    return out


def npm_latest_desc(ctx: Ctx, name: str) -> str | None:
    try:
        return (ctx.http_json(f"https://registry.npmjs.org/{name.replace('/', '%2F')}/latest",
                              timeout=ctx.left(4)) or {}).get("description")
    except Exception:  # noqa: BLE001 - the description is cosmetic
        return None


def collect_npm(ctx: Ctx, home: Path | None = None) -> dict:
    home = home or HOME
    npm = npm_bin(home, ctx.env) if ctx.which("npm", "/usr/bin/npm") or (home / ".nvm").exists() else None
    if not npm:
        return result("absent")
    prefixes = npm_prefixes(ctx, npm, home)
    if not prefixes:
        return result("absent")

    def scan(pfx: dict):
        env = node_env(pfx["npm"])
        cp = ctx.run([pfx["npm"], "outdated", "-g", "--json", "--prefix", pfx["path"]], env=env)
        if cp.returncode not in (0, 1) or not cp.stdout.strip().startswith("{"):   # rc 1 = outdated
            raise RuntimeError(_cp_error("npm outdated", cp))
        outdated = parse_npm_outdated(cp.stdout)
        ls = ctx.run([pfx["npm"], "ls", "-g", "--depth=0", "--json", "--prefix", pfx["path"]],
                     timeout=ctx.left(5), env=env)
        try:
            names = list(json.loads(ls.stdout).get("dependencies") or {})
        except Exception:  # noqa: BLE001 - the count is cosmetic
            names = list(outdated)
        return outdated, names

    got = _lookup_all([p["path"] for p in prefixes], lambda path: scan(next(p for p in prefixes if p["path"] == path)), ctx)
    errors = [(p, got[p["path"]]) for p in prefixes if isinstance(got.get(p["path"]), Exception)]
    if len(errors) == len(prefixes):
        return result("failed", error=f"npm outdated: {_why(errors[0][1])}"[:200])
    rows, checked, bundled, names_seen = [], 0, 0, {}
    for p in prefixes:
        r = got.get(p["path"])
        if isinstance(r, Exception) or r is None:
            continue
        outdated, names = r
        checked += len(set(names) | set(outdated))
        for name, info in sorted(outdated.items()):
            cur, lat = info.get("current"), info.get("latest")
            # an install from a newer dist-tag than `latest` (2026.10.8 installed vs latest
            # 2026.9.2) is not stale, and the suggested command would be a downgrade
            if not cur or not lat or not is_newer(cur, lat):
                continue
            if p["nvm"] and name in NODE_BUNDLED:
                bundled += 1
                continue
            names_seen[name] = names_seen.get(name, 0) + 1
            rows.append((p, name, cur, lat))
    descs = _lookup_all(sorted({n for _p, n, _c, _l in rows}), lambda n: npm_latest_desc(ctx, n), ctx)
    items = []
    default = prefixes[0]["path"]
    for p, name, cur, lat in rows:
        dup = names_seen[name] > 1 and p["path"] != default
        n = npm if not p["nvm"] else p["npm"]
        cmd = f"{tilde(n)} i -g --prefix {p['label']} {name}@latest"
        d = descs.get(name)
        items.append(row("npm", name, cur, lat, key=f"{name}@{p['label']}" if dup else name,
                         command=cmd, link=f"https://www.npmjs.com/package/{name}",
                         what=_desc({name: d}, name) if isinstance(d, str) else None,
                         where=p["label"], prefix=p["path"], npm=n))
    hints = []
    if errors:
        hints.append("could not scan " + ", ".join(p["label"] for p, _e in errors))
    if bundled:
        hints.append(f"{bundled} package(s) bundled with an nvm Node (npm, corepack) left to Node updates")
    res = result(checked=checked, items=items, hint="; ".join(hints) or None)
    res["prefixes"] = [p["label"] for p in prefixes]
    return res


def _cp_error(what: str, cp) -> str:
    if cp.returncode == 124:
        return f"{what} timed out"
    tail = (cp.stderr or cp.stdout or "").strip().splitlines()
    return f"{what} exited {cp.returncode}" + (f": {tail[-1][:160]}" if tail else "")


# --------------------------------------------------------------------------- PyPI tools
def pypi_latest(ctx: Ctx, name: str) -> str | None:
    doc = ctx.http_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    ctx.desc[name] = (doc.get("info") or {}).get("summary")
    return (doc.get("info") or {}).get("version")


def parse_pipx_list(text: str) -> dict:
    """`pipx list --json` -> {package: version}."""
    doc = json.loads(text or "{}")
    out = {}
    for venv in (doc.get("venvs") or {}).values():
        mp = ((venv or {}).get("metadata") or {}).get("main_package") or {}
        if mp.get("package") and mp.get("package_version"):
            out[mp["package"]] = mp["package_version"]
    return out


def collect_pipx(ctx: Ctx) -> dict:
    pipx = ctx.which("pipx", "~/.local/bin/pipx")
    if not pipx:
        return result("absent")
    cp = ctx.run([pipx, "list", "--json"])
    if cp.returncode != 0:
        return result("failed", error=_cp_error("pipx list", cp))
    try:
        installed = parse_pipx_list(cp.stdout)
    except Exception as exc:  # noqa: BLE001
        return result("failed", error=f"pipx list: {exc}"[:200])
    latest = _lookup_all(list(installed), lambda n: pypi_latest(ctx, n), ctx)
    return _registry_rows("pipx", installed, latest, title="pipx", host="pypi.org",
                          command=lambda n: f"pipx upgrade {n}",
                          link=lambda n: f"https://pypi.org/project/{n}/", desc=ctx.desc)


_UV_LINE = re.compile(r"^(\S+) v(\S+)")


def parse_uv_tool_list(text: str) -> dict:
    """`uv tool list` -> {tool: version}; '- exe' lines and warnings are skipped."""
    out = {}
    for line in (text or "").splitlines():
        m = _UV_LINE.match(line.strip())
        if m and not line.startswith(("-", " ")):
            out[m.group(1)] = m.group(2)
    return out


def collect_uv(ctx: Ctx) -> dict:
    uv = ctx.which("uv", "~/.local/bin/uv", "~/.cargo/bin/uv")
    if not uv:
        return result("absent")
    cp = ctx.run([uv, "tool", "list"])
    if cp.returncode != 0:
        return result("failed", error=_cp_error("uv tool list", cp))
    installed = parse_uv_tool_list(cp.stdout)
    latest = _lookup_all(list(installed), lambda n: pypi_latest(ctx, n), ctx)
    return _registry_rows("uv", installed, latest, title="uv tools", host="pypi.org",
                          command=lambda n: f"uv tool upgrade {n}",
                          link=lambda n: f"https://pypi.org/project/{n}/", desc=ctx.desc)


# --------------------------------------------------------------------------- cargo
_CARGO_LINE = re.compile(r"^(\S+) v(\S+?)(?: \((.+)\))?:$")


def parse_cargo_list(text: str) -> tuple[dict, list]:
    """`cargo install --list` -> ({crate: version} from crates.io, [crates from path/git])."""
    reg, other = {}, []
    for line in (text or "").splitlines():
        m = _CARGO_LINE.match(line.rstrip())
        if not m:
            continue
        if m.group(3):
            other.append(m.group(1))
        else:
            reg[m.group(1)] = m.group(2)
    return reg, other


def crates_latest(ctx: Ctx, name: str) -> str | None:
    doc = ctx.http_json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
    c = doc.get("crate") or {}
    ctx.desc[name] = c.get("description")
    return c.get("max_stable_version") or c.get("newest_version")


def collect_cargo(ctx: Ctx) -> dict:
    cargo = ctx.which("cargo", "~/.cargo/bin/cargo")
    if not cargo:
        return result("absent")
    cp = ctx.run([cargo, "install", "--list"])
    if cp.returncode != 0:
        return result("failed", error=_cp_error("cargo install --list", cp))
    installed, other = parse_cargo_list(cp.stdout)
    latest = _lookup_all(list(installed), lambda n: crates_latest(ctx, n), ctx)
    res = _registry_rows("cargo", installed, latest, title="cargo", host="crates.io",
                         command=lambda n: f"cargo install {n}",
                         link=lambda n: f"https://crates.io/crates/{n}", desc=ctx.desc)
    if other:
        res["hint"] = f"{len(other)} installed from a path or git, not compared: {', '.join(other)}"
        res["checked"] += len(other)
    return res


# --------------------------------------------------------------------------- Home Assistant
def read_env_file(path: Path) -> dict:
    """NAME=value lines (an `export ` prefix and shell quotes are fine; # comments ignored).
    Only HA_URL and HA_TOKEN are kept. A missing or unreadable file is just empty."""
    out: dict = {}
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, eq, raw = line.partition("=")
        if not eq or name.strip() not in HA_KEYS:
            continue
        try:
            val = shlex.split(raw, comments=True)
        except ValueError:
            continue
        if val and val[0]:
            out[name.strip()] = val[0]
    return out


def ha_base(ctx: Ctx) -> str:
    return (ctx.ha_setting("HA_URL") or HA_URL_DEFAULT).rstrip("/")


HA_NO_TOKEN = "set HA_TOKEN in ~/.config/rivet/ha.env to a long-lived access token (your Home Assistant profile, Security tab)"


def ha_token(ctx: Ctx) -> tuple[str | None, str | None]:
    """(token, why-not). HA_TOKEN from the environment, else from ~/.config/rivet/ha.env.
    Never logged, never stored. (None, None) = Home Assistant is not set up for Rivet at all
    (no token and no HA_URL): the collector is then `absent`, not a "can't check" row (2026-11-01)."""
    tok = ctx.ha_setting("HA_TOKEN")
    if tok:
        return tok, None
    asked = bool(ctx.ha_setting("HA_URL"))         # a URL without a token: say what is missing
    return None, (HA_NO_TOKEN if asked else None)


def parse_ha_states(states: list) -> tuple[int, list]:
    """/api/states -> (number of update.* entities, the ones that are `on`)."""
    ups = [s for s in states or [] if str(s.get("entity_id", "")).startswith("update.")]
    return len(ups), [s for s in ups if s.get("state") == "on"]


# update.* entity features (homeassistant.components.update.UpdateEntityFeature)
HA_INSTALL, HA_SPECIFIC_VERSION, HA_BACKUP = 1, 2, 8
# HACS puts this in an integration's release_summary once it has downloaded a version that
# only loads after a core restart (and files a `restart_required` repair). 2026-11-01:
# Alexa Media Player sat at "v5.18.0 downloaded, 5.15.7 running" because of it.
HA_RESTART_RE = re.compile(r"restart of home assistant required", re.I)
# add-on / integration title -> the HA integration whose entities show whether anything uses it
HA_BACKS = {"Matter Server": ("matter", "Matter"), "Mosquitto broker": ("mqtt", "MQTT"),
            "Piper": ("wyoming", "Wyoming voice"), "Whisper": ("wyoming", "Wyoming voice"),
            "Alexa Media Player": ("alexa_media", "Alexa Media Player")}
# One template render: which update entities belong to the Supervisor (core/os/supervisor/
# add-ons) and to HACS, each entity's device, and entity counts for HA_BACKS' domains.
HA_TEMPLATE = (
    "{%- set ns = namespace(rows={}) -%}{%- for s in states.update -%}"
    "{%- set ns.rows = dict(ns.rows, **{s.entity_id: [device_attr(s.entity_id,'manufacturer'),"
    " device_attr(s.entity_id,'model'), device_attr(s.entity_id,'name'),"
    " (device_attr(s.entity_id,'identifiers') or []) | list]}) -%}{%- endfor -%}"
    "{{ {'hassio': integration_entities('hassio') | select('match','update\\.') | list,"
    " 'hacs': integration_entities('hacs') | select('match','update\\.') | list,"
    " 'devices': ns.rows, 'counts': {" + ", ".join(
        f"'{d}': integration_entities('{d}') | count" for d in sorted({v[0] for v in HA_BACKS.values()}))
    + "}} | tojson }}")


def ha_kind(entity_id: str, a: dict, info: dict) -> str:
    """core | os | supervisor | addon | integration | firmware | other."""
    title = a.get("title") or ""
    dev = (info.get("devices") or {}).get(entity_id) or [None, None, None]
    if title == "Home Assistant Core" or entity_id == "update.home_assistant_core_update":
        return "core"
    if title == "Home Assistant Operating System" or entity_id == "update.home_assistant_operating_system_update":
        return "os"
    if title == "Home Assistant Supervisor" or entity_id == "update.home_assistant_supervisor_update":
        return "supervisor"
    if entity_id in (info.get("hacs") or []) or dev[1] == "integration":
        return "integration"
    if entity_id in (info.get("hassio") or []) or dev[1] == "Home Assistant App":
        return "addon"
    if a.get("device_class") == "firmware":
        return "firmware"
    if not info:                              # the template failed: guess from the features
        f = int(a.get("supported_features") or 0)
        if f & HA_BACKUP and title:
            return "addon"
        if not title and "github.com" in str(a.get("release_url") or ""):
            return "integration"
    return "other"


def ha_used_by(kind: str, title: str, a: dict, entity_id: str, info: dict) -> str:
    dev = (info.get("devices") or {}).get(entity_id) or [None, None, None]
    if kind == "core":
        return "Home Assistant on the HA VM. Installing restarts Home Assistant."
    if kind == "os":
        return "The HA VM's operating system. Installing reboots the VM."
    if kind == "supervisor":
        return "The HA VM's supervisor (it also updates itself)."
    if kind == "firmware":
        if int(a.get("supported_features") or 0) & HA_INSTALL:
            return "On the device itself; Home Assistant can install it."
        return f"On the device itself; HA only reports it, update it from the {dev[0] or 'vendor'} app."
    if kind == "integration":
        out = "HACS integration inside Home Assistant (needs an HA restart after updating)"
    elif kind == "addon":
        out = "Add-on on the HA VM, not on this PC"
        word = (title.split() or [""])[0].lower()
        if word and len(word) > 3 and shutil.which(word):
            out = f"Add-on on the HA VM. Not the {title} on this PC"
    else:
        return "Home Assistant update."
    back = HA_BACKS.get(title)
    if back and back[0] in (info.get("counts") or {}):
        n = int(info["counts"][back[0]] or 0)
        out += (f"; backs the {back[1]} integration ({n} entities)" if n else
                f"; nothing uses it yet (0 {back[1]} entities)")
    return out + "."


def collect_ha(ctx: Ctx) -> dict:
    token, why = ha_token(ctx)
    if not token:
        return result("cant_check", hint=why) if why else result("absent")
    base = ha_base(ctx)
    auth = {"Authorization": f"Bearer {token}"}
    try:
        states = ctx.http_json(f"{base}/api/states", headers=auth)
    except HttpError as exc:
        if exc.status in (401, 403):
            return result("failed", error="Home Assistant refused the token (HTTP %d); "
                          "check HA_TOKEN" % exc.status)
        return result("failed", error=f"Home Assistant answered {exc}")
    except Exception as exc:  # noqa: BLE001
        return result("failed", error=f"Could not reach Home Assistant at {base} ({_why(exc)}).")
    total, on = parse_ha_states(states)
    info: dict = {}
    if on:
        try:
            info = ctx.http_json(f"{base}/api/template", headers=auth, data={"template": HA_TEMPLATE},
                                 timeout=ctx.left(5)) or {}
            if not isinstance(info, dict):
                info = {}
        except Exception:  # noqa: BLE001 - classification falls back to the attributes
            info = {}
    items = []
    for s in on:
        a = s.get("attributes") or {}
        eid = s["entity_id"]
        kind = ha_kind(eid, a, info)
        dev = (info.get("devices") or {}).get(eid) or [None, None, None]
        name = (a.get("title") or (a.get("friendly_name") if kind == "firmware" else dev[2])
                or a.get("friendly_name") or eid)
        name = re.sub(r" Update$", "", name)
        installable = kind in ("addon", "integration") and int(a.get("supported_features") or 0) & HA_INSTALL
        extra = ha_addon_facts(kind, name, dev, info, base)
        if kind == "integration" and HA_RESTART_RE.search(str(a.get("release_summary") or "")):
            extra["restart_pending"] = True
        items.append(row("ha", name, a.get("installed_version"), a.get("latest_version"),
                         key=eid, link=f"{base}/config/updates" if kind in ("core", "os", "supervisor", "firmware")
                         else (a.get("release_url") or f"{base}/config/updates"),
                         hint="Update in Home Assistant: Settings › System › Updates",
                         used_by=ha_used_by(kind, name, a, eid, info),
                         what=(f"Firmware for {dev[2] or 'a device'}" + (f" ({dev[0]} {dev[1]})." if dev[0] else "."))
                         if kind == "firmware" else None,
                         kind=kind, entity_id=eid, installable=bool(installable),
                         backup=bool(int(a.get("supported_features") or 0) & HA_BACKUP), **extra))
    return result(checked=total, items=items)


def ha_addon_facts(kind: str, name: str, dev: list, info: dict, base: str) -> dict:
    """2026-10-31 (c): an add-on's Supervisor slug (its device's `hassio` identifier), a link to
    its page through HA's own my-link handler (maps to the add-on page of whatever frontend
    runs) and `unused` when the integration it backs has no entities (the Remove button,
    which re-checks that count live before it uninstalls anything)."""
    if kind != "addon":
        return {}
    ids = dev[3] if len(dev) > 3 and isinstance(dev[3], list) else []
    slug = next((str(i[1]) for i in ids if isinstance(i, (list, tuple)) and len(i) == 2 and i[0] == "hassio"), None)
    out: dict = {}
    if slug and re.fullmatch(r"[A-Za-z0-9_.-]+", slug):
        out["addon_slug"] = slug
        out["addon_link"] = f"{base}/_my_redirect/supervisor_addon?addon={slug}"
    back = HA_BACKS.get(name)
    counts = info.get("counts") or {}
    if back and back[0] in counts and not int(counts[back[0]] or 0):
        out["unused"] = True
    return out


# --------------------------------------------------------------------------- podman
def parse_autoupdate(text: str) -> list:
    """`podman auto-update --dry-run --format json` -> entries whose Updated is pending."""
    try:
        doc = json.loads(text or "[]")
    except Exception:  # noqa: BLE001
        return []
    return [d for d in doc or [] if str(d.get("Updated", "")).lower() == "pending"]


def collect_podman(ctx: Ctx) -> dict:
    podman = ctx.which("podman")
    if not podman:
        return result("absent")
    items, seen = [], set()
    au = ctx.run([podman, "auto-update", "--dry-run", "--format", "json"], timeout=ctx.left(6))
    for d in parse_autoupdate(au.stdout if au.returncode == 0 else ""):
        img = d.get("Image") or "?"
        seen.add(img)
        items.append(row("podman", img, "running", "newer image", sev="unknown", key=img,
                         command=f"podman auto-update  # updates {d.get('Unit') or d.get('Container')}",
                         hint=f"auto-update unit {d.get('Unit') or '?'} has a newer image pending"))
    ps = ctx.run([podman, "ps", "-a", "--format", "json"], timeout=ctx.left(5))
    if ps.returncode != 0:
        return result("failed", error=_cp_error("podman ps", ps))
    try:
        containers = json.loads(ps.stdout or "[]") or []
    except Exception:  # noqa: BLE001
        return result("failed", error="podman ps: unreadable JSON")
    by_image: dict = {}
    for c in containers:
        img = c.get("Image") or ""
        if not img or img.startswith("localhost/") or "@sha256:" in img or img in seen:
            continue
        e = by_image.setdefault(img, {"id": c.get("ImageID"), "names": [], "units": []})
        e["names"] += c.get("Names") or []
        unit = (c.get("Labels") or {}).get("PODMAN_SYSTEMD_UNIT")
        if unit and unit not in e["units"]:
            e["units"].append(unit)
    skopeo = ctx.which("skopeo")
    if by_image and not skopeo:
        return result("cant_check", checked=len(by_image), items=items,
                      hint="install skopeo to compare images with the registry")

    def remote(img: str):
        cp = ctx.run([skopeo, "inspect", "--retry-times", "0", "--format", "{{.Digest}}",
                      f"docker://{img}"], timeout=ctx.left(8))
        if cp.returncode != 0:
            raise RuntimeError(_cp_error("skopeo inspect", cp))
        digest = cp.stdout.strip()
        # the repo's tags (the newer-version question); a failure here is not a failed check
        tags = None
        try:
            lt = ctx.run([skopeo, "list-tags", "--retry-times", "0", f"docker://{image_repo(img)}"],
                         timeout=ctx.left(8))
            if lt.returncode == 0:
                tags = [str(t) for t in (json.loads(lt.stdout or "{}").get("Tags") or [])]
        except Exception:  # noqa: BLE001
            tags = None
        return digest, tags

    latest = _lookup_all(list(by_image), remote, ctx, workers=6)
    errors = 0
    for img, info in sorted(by_image.items()):
        got = latest.get(img)
        if isinstance(got, Exception) or not got or not got[0]:
            errors += 1
            continue
        digest, tags = got
        running = _inspect(ctx, podman, info["id"] or img)       # the image the containers run
        tagged = _inspect(ctx, podman, img)                      # what the tag points at locally
        local = running.get("digests") or []
        if not local:
            continue
        names, units = sorted(set(info["names"])), sorted(info["units"])
        r = classify_container(img, running_version(img, running.get("config")), tags,
                               digest_changed=digest not in local,
                               pulled=digest in (tagged.get("digests") or []) and digest not in local)
        if r is None:
            continue
        pol = container_policy(img, names, units)
        restart = restart_cmd(units, names)
        facts = dict(units=units, containers=names, image=img, what=_hub_desc(ctx, img),
                     tag=image_tag(img), newest_tag=r.get("newest_tag"), rebuilt=r["rebuilt"],
                     stateful=pol["stateful"], runbook=pol["runbook"], runbook_title=pol["runbook_title"],
                     manual=MANUAL_NOTE if pol["stateful"] and not pol["runbook"] else None)
        who = ", ".join(names)
        if r["status"] == "rebuilt":
            note = "Image rebuilt upstream (same version)."
            if r.get("pulled"):
                note += f" The new build is already pulled; restart {who} to use it."
            items.append(row("podman", img, r["installed"], r["installed"], status="rebuilt", sev="unknown",
                             key=img, hint=note, link=container_link(img), **facts))
            continue
        if pol["stateful"]:
            hint = (f"Holds data: upgrade by hand ({pol['runbook_title']})." if pol["runbook"]
                    else "Holds data: upgrade by hand.")
            items.append(row("podman", img, r["installed"], r["latest"], key=img, hint=hint,
                             link=container_link(img), **facts))
        elif r["pull_gets_it"]:
            if r.get("pulled"):
                items.append(row("podman", img, r["installed"], r["latest"], key=img, command=restart,
                                 pulled=True, hint=f"Newer image already pulled; restart {who} to use it.",
                                 link=container_link(img), **facts))
            else:
                items.append(row("podman", img, r["installed"], r["latest"], key=img,
                                 command=f"podman pull {img}", restart=restart, link=container_link(img),
                                 hint=f"Pulling {img} brings {r['latest']}; restart {who} afterwards.", **facts))
        else:
            items.append(row("podman", img, r["installed"], r["latest"], key=img, link=container_link(img),
                             hint=f"Pinned to {image_tag(img)}: change the tag where {who} is defined to upgrade.",
                             **facts))
    if by_image and errors == len(by_image):
        return result("failed", checked=len(by_image), items=items,
                      error="Could not reach the image registries. Not known to be current.")
    return result(checked=len(by_image) + len(seen), items=items,
                  hint=f"{errors} image(s) could not be compared" if errors else None)


def _inspect(ctx: Ctx, podman: str, ref: str) -> dict:
    """{digests: [sha256:...], config: {Env, Labels}} of a local image ({} when unknown)."""
    cp = ctx.run([podman, "image", "inspect", ref], timeout=ctx.left(4))
    try:
        d = (json.loads(cp.stdout or "[]") or [{}])[0] if cp.returncode == 0 else {}
    except Exception:  # noqa: BLE001
        d = {}
    return {"digests": [x.split("@", 1)[1] for x in d.get("RepoDigests") or [] if "@" in x],
            "config": d.get("Config") or {}}


# --------------------------------------------------------------------------- container versions
# 2026-11-01 (a digest-to-digest row told the user nothing): a container row compares
# VERSIONS (the running image's version vs the newest tag of the same repo), never digests.
def image_repo(img: str) -> str:
    """docker.io/library/couchdb:3 -> docker.io/library/couchdb (no tag, no digest)."""
    s = str(img).split("@", 1)[0]
    head, _, last = s.rpartition("/")
    return (head + "/" if head else "") + last.split(":", 1)[0]


def image_tag(img: str) -> str:
    s = str(img).split("@", 1)[0]
    last = s.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


_VER = re.compile(r"^v?(\d+(?:\.\d+)*)")


def running_version(img: str, config: dict | None) -> str:
    """The version the running image IS: a fully specified tag (1.28.1) wins; else the image's
    own <NAME>_VERSION env (COUCHDB_VERSION, REDIS_VERSION, GHOST_VERSION; PG_VERSION for
    postgres), else the OCI version label, else the tag itself ("3", "latest")."""
    tag = image_tag(img)
    m = _VER.match(tag)
    if m and m.group(1).count(".") >= 2:
        return m.group(1)
    cfg = config or {}
    env = {}
    for e in cfg.get("Env") or []:
        k, _, v = str(e).partition("=")
        env[k] = v
    base = image_key(img).rsplit("/", 1)[-1].upper().replace("-", "_")
    for k in ([f"{base}_VERSION"] + (["PG_VERSION"] if base == "POSTGRES" else [])):
        vm = _VER.match(env.get(k) or "")
        if vm:
            return vm.group(1)
    lab = (cfg.get("Labels") or {}).get("org.opencontainers.image.version") or ""
    lm = _VER.match(str(lab))
    if lm and (not m or lm.group(1).startswith(m.group(1))):    # a base image's label can leak through
        return lm.group(1)
    return tag


def newest_tags(tags, tag: str, version: str) -> tuple[str | None, str | None]:
    """(newest overall, newest within the tag's own track) among the repo's version tags of the
    same flavour (suffix: "17-alpine" only looks at "<x.y>-alpine"); release candidates and
    bare majors ("16") are not versions. The track of a floating tag "3" is 3.*, of "latest"
    everything; a fully specified tag has no track beyond itself."""
    m = _VER.match(tag)
    suffix = tag[m.end():] if m else ("" if tag == "latest" else "-" + tag)
    pat = re.compile(r"^v?(\d+(?:\.\d+)+)" + re.escape(suffix) + "$")
    vers = [mm.group(1) for t in tags or [] for mm in [pat.match(t)] if mm]
    if not vers:
        return None, None
    best = max(vers, key=lambda v: _pad(vtuple(v), 6))
    prefix = m.group(1).split(".") if m else []
    if m and len(prefix) >= len(str(version).split(".")):
        return best, None                                       # pinned: no track
    track = [v for v in vers if v.split(".")[:len(prefix)] == prefix]
    return best, (max(track, key=lambda v: _pad(vtuple(v), 6)) if track else None)


def classify_container(img: str, version: str, tags, *, digest_changed: bool, pulled: bool = False) -> dict | None:
    """One running image -> the row it deserves (None = current, no row):
      newer version (in its track for a floating tag, else the repo's newest)  -> status ok,
          installed/latest are versions, `pull_gets_it` when pulling the same tag brings it
      same version but the registry's digest moved                          -> status rebuilt
          (no badge, no button: "image rebuilt upstream (same version)")"""
    tag = image_tag(img)
    best, track = newest_tags(tags, tag, version)
    if not vtuple(version):
        best = track = None                                     # "latest" with no version to compare
    if track and is_newer(version, track) and digest_changed:
        return {"status": "ok", "installed": version, "latest": track, "newest_tag": best,
                "pull_gets_it": True, "pulled": pulled, "rebuilt": False}
    if best and is_newer(version, best):
        return {"status": "ok", "installed": version, "latest": best, "newest_tag": best,
                "pull_gets_it": False, "pulled": False, "rebuilt": digest_changed}
    if digest_changed:
        return {"status": "rebuilt", "installed": version, "latest": version, "newest_tag": best,
                "pull_gets_it": False, "pulled": pulled, "rebuilt": True}
    return None


def restart_cmd(units: list, names: list) -> str | None:
    if units:
        return "systemctl --user restart " + " ".join(units)
    return ("podman restart " + " ".join(sorted(set(names)))) if names else None


def _hub_desc(ctx: Ctx, img: str) -> str | None:
    """Docker Hub's one-line description for docker.io images (cosmetic; know.py's own
    lines win). Cached in know-cache.json so the hourly run asks once."""
    from know import STATIC, first_sentence, image_key, load_cache, save_cache
    key = image_key(img)
    if not img.startswith(("docker.io/", "index.docker.io/")) and "." in img.split("/")[0]:
        return None
    if (("podman", key) in STATIC) and STATIC[("podman", key)][0]:
        return None
    cache = load_cache()
    ck = f"hub:{key}"
    if ck in cache:
        return cache[ck] or None
    repo = key if "/" in key else f"library/{key}"
    try:
        d = (ctx.http_json(f"https://hub.docker.com/v2/repositories/{repo}/", timeout=ctx.left(4)) or {}).get("description")
    except Exception:  # noqa: BLE001
        return None
    cache[ck] = first_sentence(d)
    try:
        save_cache(cache)
    except OSError:
        pass
    return cache[ck]


# --------------------------------------------------------------------------- GNOME Shell extensions
def shell_major(ctx: Ctx) -> str | None:
    cp = ctx.run(["gnome-shell", "--version"], timeout=ctx.left(4))
    m = re.search(r"(\d+)", cp.stdout or "")
    return m.group(1) if m else None


def collect_gnome_ext(ctx: Ctx, ext_dir: Path | None = None) -> dict:
    ge = ctx.which("gnome-extensions")
    if not ge:
        return result("absent")
    cp = ctx.run([ge, "list", "--user"], timeout=ctx.left(4))
    if cp.returncode != 0:
        return result("failed", error=_cp_error("gnome-extensions list", cp))
    uuids = [u.strip() for u in cp.stdout.splitlines() if u.strip()]
    root = ext_dir or HOME / ".local/share/gnome-shell/extensions"
    local = {}
    for u in uuids:
        try:
            md = json.loads((root / u / "metadata.json").read_text())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(md.get("version"), int):           # only extensions.gnome.org builds carry one
            local[u] = (md.get("name") or u, md["version"])
    major = shell_major(ctx) or ""

    def info(u: str):
        q = urllib.parse.urlencode({"uuid": u, "shell_version": major})
        try:
            return ctx.http_json(f"https://extensions.gnome.org/extension-info/?{q}")
        except HttpError as exc:
            if exc.status == 404:
                return None                              # not published there (home-made)
            raise

    got = _lookup_all(list(local), info, ctx)
    items, errors, listed = [], 0, 0
    for u, (name, ver) in sorted(local.items()):
        d = got.get(u)
        if isinstance(d, Exception):
            errors += 1
            continue
        if not d:
            continue
        listed += 1
        remote = d.get("version")
        if isinstance(remote, int) and remote > ver:
            from know import first_sentence
            items.append(row("gnome-ext", name, f"v{ver}", f"v{remote}", key=u, sev="unknown",
                             link="https://extensions.gnome.org" + (d.get("link") or ""),
                             what=first_sentence(d.get("description")),
                             used_by="GNOME Shell extension in ~/.local/share/gnome-shell/extensions",
                             hint="Update from Extension Manager or extensions.gnome.org"))
    if local and errors == len(local):
        return result("failed", checked=len(local),
                      error="Could not reach extensions.gnome.org. Not known to be current.")
    return result(checked=listed, items=items,
                  hint=f"{errors} extension(s) could not be looked up" if errors else None)


# --------------------------------------------------------------------------- AppImages
_UPD = (b"gh-releases-zsync|", b"zsync|http", b"pling-v1-zsync|", b"gh-releases-direct|")
_APPIMG_VER = re.compile(r"[-_ ]v?(\d+(?:\.\d+)+)")


def collect_appimage(ctx: Ctx, app_dir: Path | None = None) -> dict:
    d = app_dir or Path(ctx.env.get("RIVET_APPIMAGE_DIR") or HOME / "Applications")
    files = sorted(p for p in d.glob("*") if p.suffix.lower() == ".appimage") if d.is_dir() else []
    if not files:
        return result("absent")
    items = []
    for p in files:
        try:
            head = ctx.read_bytes(p, 4 << 20)
        except OSError:
            head = b""
        m = _APPIMG_VER.search(p.stem)
        has = any(s in head for s in _UPD)
        items.append(row("appimage", p.name, m.group(1) if m else None, None, status="cant_check",
                         link=None, key=p.name,
                         hint=("Has embedded update info: AppImageUpdate can update it." if has else
                               "No update information embedded in the file. Check the vendor’s site.")))
    return result("cant_check", checked=len(files), items=items)


# --------------------------------------------------------------------------- registry
# (id, title, function, timeout s, min interval s: reuse the previous result when it is
# younger than this, e.g. container registries rate-limit anonymous manifest reads)
REGISTRY = [
    ("npm", "npm globals", collect_npm, DEFAULT_TIMEOUT + 4, 0),       # several prefixes
    ("pipx", "pipx", collect_pipx, DEFAULT_TIMEOUT, 0),
    ("uv", "uv tools", collect_uv, DEFAULT_TIMEOUT, 0),
    ("cargo", "cargo", collect_cargo, DEFAULT_TIMEOUT, 0),
    ("ha", "Home Assistant", collect_ha, DEFAULT_TIMEOUT, 0),
    ("podman", "Containers", collect_podman, DEFAULT_TIMEOUT, 6 * 3600),
    ("gnome-ext", "GNOME extensions", collect_gnome_ext, DEFAULT_TIMEOUT, 0),
    ("appimage", "AppImages", collect_appimage, DEFAULT_TIMEOUT, 0),
]
TITLES = {r[0]: r[1] for r in REGISTRY}


def _parse_iso(s) -> float | None:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(s)).timestamp()
    except Exception:  # noqa: BLE001
        return None


ROW_VERSION = 3     # bump when collectors add row facts, so a reused result is not missing them
                    # (3, 2026-11-01: container rows compare versions, not digests)


def _reuse(prev: dict | None, sid: str, min_interval: float) -> tuple[dict, list] | None:
    if not prev or min_interval <= 0:
        return None
    src = next((s for s in prev.get("sources") or [] if s.get("id") == sid), None)
    at = _parse_iso((src or {}).get("checked_at"))
    if not src or src.get("status") != "ok" or at is None or time.time() - at > min_interval \
            or src.get("v") != ROW_VERSION:
        return None
    return src, [i for i in prev.get("items") or [] if i.get("source") == sid]


def run_one(sid: str, fn, timeout: float, env=None) -> tuple[dict, float]:
    """Run one collector on a daemon thread with a hard deadline: a hung network call
    never holds up the others or the process exit."""
    box: dict = {}
    t0 = time.monotonic()
    ctx = Ctx(deadline=t0 + timeout, env=env)

    def target():
        try:
            box["r"] = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            box["r"] = result("failed", error=f"{type(exc).__name__}: {exc}"[:200])

    th = threading.Thread(target=target, name=f"elsewhere-{sid}", daemon=True)
    th.start()
    th.join(timeout + 1.0)
    r = box.get("r") or result("failed", error=f"timed out after {timeout:.0f} s")
    return r, time.monotonic() - t0


def _source_row(sid: str, title: str, r: dict) -> dict:
    """A failed (or wholly can't-check) source as one row, so the UI never shows it as fine."""
    return row(sid, title, None, None, status=r["status"], key="_source",
               hint=r.get("error") or r.get("hint"))


def show_containers() -> bool:
    """settings.json `show_containers` (2026-11-01, default OFF). Off: the podman collector does not run and no row is listed."""
    try:
        from common import load_settings
        return load_settings().get("show_containers") is True
    except Exception:  # noqa: BLE001
        return False


def collect(prev: dict | None = None, *, only: list[str] | None = None, env=None,
            registry=None, containers: bool | None = None) -> dict:
    """Run every collector concurrently -> the `elsewhere` object of state.json.
    `containers` None = settings.json's `show_containers` (only for the default registry)."""
    t0 = time.monotonic()
    if containers is None:
        containers = True if registry is not None else show_containers()
    reg = [r for r in (registry or REGISTRY) if (not only or r[0] in only)
           and (containers or r[0] != "podman")]
    results: dict = {}
    reused: dict = {}

    def one(entry):
        sid, title, fn, timeout, min_interval = entry
        hit = _reuse(prev, sid, min_interval)
        if hit:
            reused[sid] = hit
            return
        results[sid] = run_one(sid, fn, timeout, env)

    with ThreadPoolExecutor(max_workers=len(reg) or 1) as ex:
        list(ex.map(one, reg))

    sources, items = [], []
    for sid, title, *_ in reg:
        if sid in reused:
            src, rows = reused[sid]
            sources.append(dict(src, reused=True))
            items.extend(rows)
            continue
        r, dt = results[sid]
        rows = list(r.get("items") or [])
        if r["status"] == "failed" or (r["status"] == "cant_check" and not rows):
            rows.insert(0, _source_row(sid, title, r))
        sources.append({
            "id": sid, "title": title, "status": r["status"], "checked": r.get("checked", 0),
            "stale": sum(1 for i in rows if i["status"] == "ok"),
            "error": r.get("error"), "hint": r.get("hint"),
            "checked_at": now_iso(), "duration_ms": int(dt * 1000), "v": ROW_VERSION,
        })
        items.extend(rows)

    try:
        import know
        know.enrich(items)
    except Exception as exc:  # noqa: BLE001 - the description lines are cosmetic
        for i in items:
            i.setdefault("what", None), i.setdefault("used_by", None)
        print(f"know.enrich failed: {type(exc).__name__}: {exc}", flush=True)
    for i in items:
        decide_action(i)
    items.sort(key=sort_key)
    return {
        "checked_at": now_iso(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "counts": {
            "stale": sum(1 for i in items if i["status"] == "ok"),
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "cant_check": sum(1 for i in items if i["status"] == "cant_check"),
        },
        "sources": sources,
        "items": items,
    }


# sources whose Update button runs the update as the user (no root, nothing restarts)
USER_SOURCES = ("npm", "pipx", "uv", "cargo", "podman")


def decide_action(it: dict) -> dict:
    """Which button a row gets (CONTRACT.md "Other software"):
    update      npm / pipx / uv / cargo / podman pull: rivet-apply --other-update ID
    ha_install  HA add-ons + HACS integrations: update.install over the REST API
    open        HA core / OS / supervisor / firmware, GNOME extensions: the link
    copy        what is left to run by hand (podman auto-update, a restart after a pull)
    runbook     a container that holds data (2026-11-01): its runbook link, never Update;
                with no runbook known, no button (the row carries `manual` instead)
    A `rebuilt` container (same version, new digest) gets nothing.
    `confirm`: a major bump asks once in the row before it runs."""
    act = None
    if it.get("source") == "podman" and (it.get("status") == "rebuilt" or it.get("stateful")):
        act = "runbook" if it.get("status") == "ok" and it.get("runbook") else None
    elif it.get("status") == "ok":
        src = it.get("source")
        if src in USER_SOURCES and it.get("command") and not it.get("pulled") \
                and not str(it.get("command")).startswith("podman auto-update"):
            act = "update"
        elif src == "ha" and it.get("installable"):
            act = "ha_install"
        elif it.get("link") and src in ("ha", "gnome-ext"):
            act = "open"
        elif it.get("command"):
            act = "copy"
        elif it.get("link"):
            act = "open"
    elif it.get("link") and it.get("status") != "updated":
        act = "open"
    it["action"] = act
    it["confirm"] = act in ("update", "ha_install") and it.get("severity") == "major"
    return it


_ORDER = {r[0]: n for n, r in enumerate(REGISTRY)}
_STATUS = {"ok": 0, "failed": 1, "cant_check": 2, "updated": 3, "rebuilt": 4}


def sort_key(i: dict):
    return (_STATUS.get(i["status"], 9), SEVERITY_RANK.get(i["severity"], 9),
            _ORDER.get(i["source"], 99), str(i["name"]).lower())

