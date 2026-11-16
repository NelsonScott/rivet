"""rivet-apply --other-update ID: the Update / Install in HA buttons of "Other software".

Added 2026-10-31, CONTRACT.md "Other software". The UI sends only a row id; the command is
rebuilt HERE from the row in state.json (source + name + prefix/image/entity), never taken
from the UI and never passed through a shell:

  npm     <npm> i -g --prefix <prefix> <name>@latest     (that prefix's own Node first on PATH)
  pipx    pipx upgrade <name>
  uv      uv tool upgrade <name>
  cargo   cargo install <name>
  podman  podman pull <image>          (running containers keep the old image: the done
                                        event says which unit to restart; nothing restarts)
  ha      POST <ha>/api/services/update/install {entity_id[, backup]}, then poll the entity
          until in_progress clears; add-ons and HACS integrations, and (2026-11-10, run_core)
          Home Assistant core after check_config, confirmed, waiting out its restart; never
          OS / supervisor, never device firmware

rivet-apply --other-remove ID (2026-11-01): the Remove button of an UNUSED Home Assistant
add-on. Supervisor `POST /addons/<slug>/uninstall` through HA's websocket (`supervisor/api`;
the REST proxy /api/hassio/* answers 401 to tokens), after re-checking live that the
integration it backs still has 0 entities; then polls `/addons/<slug>/info` until the
Supervisor says it is not installed, and drops the row from state.json.

All of it runs as the user: no root, no polkit prompt. Events are the rivet-apply
JSON lines (step "other"); the done event carries `other: {id, result, item}` where
`item` is the row as it is after the run, which is also patched into state.json so the
row reads "Updated" until the next check drops it.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
from pathlib import Path

import elsewhere as E
from common import STATE_PATH, now_iso, write_json_atomic

SAFE_NAME = re.compile(r"^[@A-Za-z0-9][@A-Za-z0-9._/:+-]*$")
RUN_TIMEOUT_S = 1200                 # cargo install compiles; npm/pipx take seconds
AFTER_UPDATE_TIMEOUT_S = 900         # a follow-up step (know.py after_update): a service drain can take minutes
HA_POLL_S = float(os.environ.get("RIVET_HA_POLL_S", "3"))
HA_TIMEOUT_S = float(os.environ.get("RIVET_HA_TIMEOUT_S", "1200"))
HA_GONE_S = float(os.environ.get("RIVET_HA_GONE_S", "90"))   # unreachable this long mid-install = failed
STEP = "other"


class Refused(Exception):
    pass


def find_item(state: dict, item_id: str) -> dict:
    for it in ((state.get("elsewhere") or {}).get("items") or []):
        if it.get("id") == item_id:
            return it
    raise Refused(f"{item_id} is not in the current list (check again first).")


def build_argv(it: dict, *, which=E.Ctx().which) -> tuple[list[str], dict | None]:
    """(argv, extra env) for a user-level row; Refused for anything else."""
    src, name = it.get("source"), str(it.get("name") or "")
    if it.get("action") != "update" or it.get("status") != "ok":
        raise Refused(f"{name or it.get('id')} has no Update button.")
    if not SAFE_NAME.match(name):
        raise Refused(f"refusing an odd package name: {name!r}")
    if src == "npm":
        prefix, npm = it.get("prefix"), it.get("npm") or E.npm_bin()
        if not prefix or not os.path.isdir(os.path.join(prefix, "lib", "node_modules")):
            raise Refused(f"npm prefix {prefix!r} is gone.")
        if not npm or not os.path.isfile(npm):
            raise Refused("npm not found.")
        return [npm, "i", "-g", "--prefix", prefix, f"{name}@latest"], E.node_env(npm)
    if src == "pipx":
        exe = which("pipx", "~/.local/bin/pipx")
        return [exe, "upgrade", name] if exe else _missing("pipx"), None
    if src == "uv":
        exe = which("uv", "~/.local/bin/uv", "~/.cargo/bin/uv")
        return [exe, "tool", "upgrade", name] if exe else _missing("uv"), None
    if src == "cargo":
        exe = which("cargo", "~/.cargo/bin/cargo")
        return [exe, "install", name] if exe else _missing("cargo"), None
    if src == "podman":
        exe = which("podman")
        img = str(it.get("image") or name)
        if not SAFE_NAME.match(img):
            raise Refused(f"refusing an odd image name: {img!r}")
        return [exe, "pull", img] if exe else _missing("podman"), None
    raise Refused(f"{src} rows cannot be updated from Rivet.")


def _missing(tool: str):
    raise Refused(f"{tool} is not installed any more.")


def installed_now(it: dict, *, run=None) -> str | None:
    """The version on disk after the run, read the cheapest way per source."""
    src, name = it["source"], it["name"]
    ctx = E.Ctx()
    run = run or ctx.run
    try:
        if src == "npm":
            pj = Path(it["prefix"]) / "lib" / "node_modules" / name / "package.json"
            return json.loads(pj.read_text()).get("version")
        if src == "pipx":
            return E.parse_pipx_list(run([ctx.which("pipx", "~/.local/bin/pipx"), "list", "--json"]).stdout).get(name)
        if src == "uv":
            return E.parse_uv_tool_list(run([ctx.which("uv", "~/.local/bin/uv"), "tool", "list"]).stdout).get(name)
        if src == "cargo":
            return E.parse_cargo_list(run([ctx.which("cargo", "~/.cargo/bin/cargo"), "install", "--list"]).stdout)[0].get(name)
        if src == "podman":
            cp = run([ctx.which("podman"), "image", "inspect", "--format", "{{json .RepoDigests}}",
                      str(it.get("image") or name)])
            ds = [d.split("@", 1)[1] for d in json.loads(cp.stdout or "[]") if "@" in d]
            return ds[0][:19] if ds else None
    except Exception:  # noqa: BLE001 - "unknown" is an answer too
        return None
    return None


def patch_state(item: dict, path: Path | None = None, *, remove: bool = False) -> None:
    """Swap the row into state.json's `elsewhere` (tmp + rename) and recount; `remove`
    drops it instead (an uninstalled add-on)."""
    path = path or STATE_PATH
    try:
        doc = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - no state, nothing to patch
        return
    ew = doc.get("elsewhere")
    if not isinstance(ew, dict):
        return
    items = [item if i.get("id") == item["id"] else i for i in ew.get("items") or []
             if not (remove and i.get("id") == item["id"])]
    ew["items"] = items
    ew["counts"] = {
        "stale": sum(1 for i in items if i.get("status") == "ok"),
        "failed": sum(1 for i in items if i.get("status") == "failed"),
        "cant_check": sum(1 for i in items if i.get("status") == "cant_check"),
    }
    for s in ew.get("sources") or []:
        if s.get("id") == item["source"]:
            s["stale"] = sum(1 for i in items if i.get("source") == s["id"] and i.get("status") == "ok")
    doc["generated_at"] = now_iso()
    write_json_atomic(path, doc)


def updated_row(it: dict, now: str | None, *, result: str, note: str | None) -> dict:
    new = dict(it)
    new["last_run"] = {"at": now_iso(), "result": result, "note": note}
    if result in ("updated", "installed"):
        new.update(status="updated", installed=now or it.get("latest"), action=None, confirm=False,
                   hint=note or new.get("hint"))
    elif result == "pulled":
        restart = it.get("restart") or E.restart_cmd(it.get("units") or [], it.get("containers") or [])
        new.update(status="updated", pulled=True, command=restart, action="copy" if restart else None,
                   confirm=False, hint=note)
    return new


# --------------------------------------------------------------------------- local run
def _post_update(em, it: dict, stream) -> str | None:
    """The follow-up a tool needs once its new version is on disk (a schema migration, a service
    restart), from `after_update` in the local overrides (know.py); the returned note joins the
    row and the summary. A failed follow-up never fails the update itself: the new version IS
    installed. Why this exists: a daemon that refuses to start until its databases are migrated
    stays down after an unattended update until someone runs the migration by hand."""
    import know
    spec = know.AFTER_UPDATE.get(f"{it.get('source')}:{it.get('name')}")
    if not spec:
        return None
    argv = [a.replace("{prefix}", it.get("prefix") or "") for a in spec["argv"]]
    shown = "$ " + " ".join(E.tilde(a) for a in argv)
    hint = f" ({spec['hint']})" if spec.get("hint") else ""
    if os.path.isabs(argv[0]) and not os.path.isfile(argv[0]):
        return f"Updated, but its follow-up step was not run: {E.tilde(argv[0])} is not there{hint}."
    em.emit(STEP, "progress", 95, spec.get("label") or f"Running the follow-up step for {it['name']}…", shown)
    try:
        timeout = float(spec.get("timeout") or AFTER_UPDATE_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = AFTER_UPDATE_TIMEOUT_S
    rc, last = stream(argv, em, STEP, timeout=timeout)
    if rc != 0:
        why = "timed out and was killed" if rc == 124 else f"exited {rc}"
        return f"Updated, but its follow-up step {why}" + (f": {last[:160]}" if last else "") + hint + "."
    return spec.get("ok") or "Its follow-up step ran."


def run_local(em, it: dict, *, stream=None, now_fn=None) -> int:
    from rivet_apply import stream_lines
    stream = stream or stream_lines
    now_fn = now_fn or installed_now
    argv, env = build_argv(it)
    shown = " ".join(E.tilde(a) for a in argv)
    em.emit(STEP, "start", 0, f"Updating {it['name']}…", f"$ {shown}")
    if env:
        os.environ.update(env)           # LineStream inherits os.environ; we are a one-shot process
    rc, last = stream(argv, em, STEP, timeout=RUN_TIMEOUT_S)
    if rc != 0:
        why = ("timed out and was killed" if rc == 124 else f"exited {rc}") + (f": {last[:200]}" if last else "")
        em.emit(STEP, "fail", 100, f"{argv[0].rsplit('/', 1)[-1]} {why}", None)
        row = updated_row(it, None, result="failed", note=why)
        patch_state(row)
        em.done("fail", reboot_required=False, kmod_verified=None,
                summary=f"{it['name']} was not updated ({why}).", cause=why,
                other={"id": it["id"], "result": "failed", "item": row})
        return 1
    now = now_fn(it)
    if it["source"] == "podman":
        restart = it.get("restart") or E.restart_cmd(it.get("units") or [], it.get("containers") or [])
        note = (f"Pulled. The container still runs the old image until you restart it: {restart}"
                if restart else "Pulled. Recreate the container to use it.")
        row = updated_row(it, now, result="pulled", note=note)
        em.emit(STEP, "ok", 100, note, None)
        patch_state(row)
        em.done("ok", reboot_required=False, kmod_verified=None, summary=note,
                other={"id": it["id"], "result": "pulled", "item": row, "restart": restart})
        return 0
    if now and E.is_newer(now, it.get("latest")):
        why = f"it still reports {now}, wanted {it.get('latest')}"
        em.emit(STEP, "fail", 100, f"The command finished but {why}.", None)
        row = updated_row(it, now, result="failed", note=why)
        patch_state(row)
        em.done("fail", reboot_required=False, kmod_verified=None,
                summary=f"{it['name']} did not move to {it.get('latest')} ({why}).", cause=why,
                other={"id": it["id"], "result": "failed", "item": row})
        return 1
    extra = _post_update(em, it, stream)
    msg = f"{it['name']} is now {now or it.get('latest')}." + (f" {extra}" if extra else "")
    row = updated_row(it, now, result="updated", note=extra)
    em.emit(STEP, "ok", 100, msg, None)
    patch_state(row)
    em.done("ok", reboot_required=False, kmod_verified=None, summary=msg,
            other={"id": it["id"], "result": "updated", "item": row})
    return 0


# --------------------------------------------------------------------------- Home Assistant
def ha_conn(ctx: E.Ctx) -> tuple[str, str, dict]:
    """(base url, token, auth header) or Refused."""
    token, why = E.ha_token(ctx)
    if not token:
        raise Refused(f"no Home Assistant token ({why or E.HA_NO_TOKEN}).")
    base = E.ha_base(ctx)
    return base, token, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- HA core (2026-11-10)
# Installing core restarts Home Assistant: its API going away is EXPECTED, for up to this long.
HA_CORE_TIMEOUT_S = float(os.environ.get("RIVET_HA_CORE_TIMEOUT_S", "1800"))
# not down, not in progress and still the old version this long after the install call = it did not start
HA_CORE_IDLE_S = float(os.environ.get("RIVET_HA_CORE_IDLE_S", "180"))
CORE_LOCK = "ha-core-update.lock"


def core_lock_path() -> Path:
    return STATE_PATH.parent / CORE_LOCK


def core_running() -> int | None:
    """The pid of ANOTHER rivet-apply that is installing Home Assistant core right now, else None.
    While it runs no other Home Assistant row action may start (CONTRACT "HA core")."""
    try:
        pid = int(core_lock_path().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


CORE_BUSY = "Home Assistant core is being updated right now; wait until it is back"


def _core_lock(take: bool) -> None:
    p = core_lock_path()
    try:
        if take:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{os.getpid()} {now_iso()}\n")
        elif p.exists() and p.read_text().split()[:1] == [str(os.getpid())]:
            p.unlink()
    except OSError:
        pass


def _nothing_sent(exc: BaseException) -> bool:
    """The connection never opened (refused, no route, no such name): the request was not sent.
    A reset or a timeout AFTER the request went out may be Home Assistant going down to install."""
    r = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    import errno as _e
    import socket as _s
    if isinstance(r, (_s.gaierror, ConnectionRefusedError)):
        return True
    return isinstance(r, OSError) and getattr(r, "errno", None) in (_e.EHOSTUNREACH, _e.ENETUNREACH, _e.EHOSTDOWN)


def _dur(s: float) -> str:
    return f"{int(s // 60)} min" if s >= 120 else f"{int(s)} s"


def run_core(em, it: dict, *, ctx: E.Ctx | None = None, sleep=time.sleep, confirmed: bool = False) -> int:
    """Install Home Assistant core, carefully (CONTRACT "Home Assistant core, investigated"):
      1. only a row the user confirmed (the bridge passes --confirmed after the in-row confirmation
         or for a row ticked for Update now), never from a bulk "safe ones" run;
      2. check_config first: anything but `valid` stops here, nothing installed;
      3. update.install with backup: true where the entity supports it (else the stream says so);
      4. wait patiently: the API going away (refused, 5xx, timeouts) is expected while it restarts,
         up to HA_CORE_TIMEOUT_S; success = the API is back, HA says RUNNING and its version is the
         target; back on the old version after it went down = HA rolled back = failed."""
    if it.get("source") != "ha" or it.get("kind") != "core" or it.get("action") != "ha_install":
        raise Refused(f"{it.get('name')}: only an installable Home Assistant core row comes here.")
    if not confirmed:
        raise Refused(f"{it.get('name')}: Home Assistant core is installed only after you confirm it in its row "
                      "(it restarts Home Assistant).")
    eid = str(it.get("entity_id") or "")
    if not re.match(r"^update\.[a-z0-9_]+$", eid):
        raise Refused(f"refusing an odd entity id: {eid!r}")
    other = core_running()
    if other:
        raise Refused(f"{CORE_BUSY} (rivet-apply {other}).")
    ctx = ctx or E.Ctx(env=os.environ)
    base, _token, auth = ha_conn(ctx)
    target = str(it.get("latest") or "")
    if not target:
        raise Refused(f"{it.get('name')}: no target version in the list (check again first).")
    _core_lock(True)
    try:
        return _run_core(em, it, ctx, base, auth, eid, target, sleep)
    finally:
        _core_lock(False)


def _run_core(em, it, ctx, base, auth, eid, target, sleep) -> int:
    t_start = time.monotonic()
    try:
        live = ctx.http_json(f"{base}/api/states/{eid}", headers=auth, timeout=10) or {}
    except Exception as exc:  # noqa: BLE001
        if E.unreachable(exc):
            return _ha_fail(em, it, f"could not reach Home Assistant at {base} ({E._why(exc)}); nothing was installed",
                            unreachable=True)
        live = {}
    la = (live.get("attributes") or {}) if isinstance(live, dict) else {}
    if la.get("installed_version") and not la.get("in_progress") and not E.is_newer(la.get("installed_version"), target):
        return _core_ok(em, it, la.get("installed_version"), t_start, already=True)
    feats = int(la.get("supported_features") or 0)
    em.emit(STEP, "start", 0, "Checking Home Assistant's configuration first…",
            f"POST {base}/api/config/core/check_config")
    try:
        chk = ctx.http_json(f"{base}/api/config/core/check_config", headers=auth, data={}, timeout=90) or {}
    except Exception as exc:  # noqa: BLE001
        return _ha_fail(em, it, f"the configuration check did not answer ({E._why(exc)}); nothing was installed",
                        unreachable=E.unreachable(exc))
    if chk.get("result") != "valid":
        return _ha_fail(em, it, "Home Assistant's configuration does not check out "
                        f"({str(chk.get('errors') or chk.get('result'))[:200]}); nothing was installed")
    data = {"entity_id": eid}
    if feats & E.HA_SPECIFIC_VERSION:
        data["version"] = target
    if feats & E.HA_BACKUP or (not la and it.get("backup")):
        data["backup"] = True
        em.emit(STEP, "progress", None, "Configuration is valid. Home Assistant takes a backup first, then installs "
                f"{target}…", None)
    else:
        em.emit(STEP, "progress", None, "Configuration is valid. This Home Assistant cannot take a backup as part of "
                f"the update, so it installs {target} without one, as you confirmed.", None)
    em.emit(STEP, "progress", None, f"Asking Home Assistant to install {target}…",
            f"POST {base}/api/services/update/install {json.dumps(data)}")
    try:
        ctx.http_json(f"{base}/api/services/update/install", headers=auth, data=data, timeout=30)
    except E.HttpError as exc:
        if exc.status < 500:
            return _ha_fail(em, it, f"Home Assistant refused the install ({exc})")
        em.emit(STEP, "progress", None, "Home Assistant is going down to install it…", f"({exc})")
    except Exception as exc:  # noqa: BLE001
        if _nothing_sent(exc):
            return _ha_fail(em, it, f"could not reach Home Assistant at {base} ({E._why(exc)}); nothing was installed",
                            unreachable=True)
        # the request went out and the connection dropped or timed out: Home Assistant is busy with it
        em.emit(STEP, "progress", None, "Home Assistant is working on it…", f"({E._why(exc)})")
    t0 = time.monotonic()
    down_since, saw_down, saw_progress, old_back, said_min = None, False, False, 0, -1
    idle_since, last_state = None, None
    while True:
        el = time.monotonic() - t0
        if el > HA_CORE_TIMEOUT_S:
            if saw_down:
                return _ha_fail(em, it, f"Home Assistant has not come back after {_dur(HA_CORE_TIMEOUT_S)}; "
                                "open it (or the VM console) to see what happened", unreachable=True)
            return _ha_fail(em, it, f"Home Assistant did not finish installing {target} within "
                            f"{_dur(HA_CORE_TIMEOUT_S)}; it may still finish, check again later")
        sleep(HA_POLL_S)
        try:
            cfg = ctx.http_json(f"{base}/api/config", headers=auth, timeout=10) or {}
        except E.HttpError as exc:
            if exc.status in (401, 403):
                return _ha_fail(em, it, f"Home Assistant refused the token while it came back ({exc})")
            cfg = None
        except Exception:  # noqa: BLE001 - refused / reset / timed out: it is restarting
            cfg = None
        if cfg is None:
            if not saw_down:
                em.emit(STEP, "progress", None, "Home Assistant is restarting, this is expected. Rivet waits for it "
                        f"(up to {_dur(HA_CORE_TIMEOUT_S)})…", None)
            saw_down, down_since = True, down_since or time.monotonic()
            mins = int((time.monotonic() - down_since) // 60)
            if mins >= 1 and mins != said_min:
                said_min = mins
                em.emit(STEP, "progress", None, f"Still restarting ({mins} min). This is expected; Rivet keeps waiting…", None)
            old_back = 0
            continue
        state = str(cfg.get("state") or "")
        version = cfg.get("version")
        a = {}
        try:
            a = (ctx.http_json(f"{base}/api/states/{eid}", headers=auth, timeout=10) or {}).get("attributes") or {}
        except Exception:  # noqa: BLE001 - /api/config is the authority; the entity is extra
            a = {}
        if a.get("in_progress") and not saw_down:
            saw_progress, idle_since = True, None
            pct = a.get("update_percentage")
            msg = "Home Assistant is preparing the update (backup and download)…" + (
                f" {int(pct)}%" if isinstance(pct, (int, float)) else "")
            if msg != last_state:
                em.emit(STEP, "progress", None, msg, None)
            last_state = msg
            continue
        if state != "RUNNING":
            if state != last_state:
                em.emit(STEP, "progress", None, f"Home Assistant answers again and is starting ({state.lower() or 'not running yet'})…", None)
            last_state = state
            continue
        running = version or a.get("installed_version")
        if running and not E.is_newer(running, target):
            return _core_ok(em, it, running, t_start)
        if saw_down:
            old_back += 1
            if old_back >= 3:
                return _ha_fail(em, it, f"Home Assistant came back on {running}, not {target}: the update did not "
                                "take and Home Assistant rolled back to the old version")
            continue
        idle_since = idle_since or time.monotonic()
        if time.monotonic() - idle_since >= HA_CORE_IDLE_S:
            return _ha_fail(em, it, f"Home Assistant never started installing it (still {running}, wanted {target}); "
                            "see Settings › System › Logs" + ("" if not saw_progress else " (it began, then stopped)"))


def _core_ok(em, it: dict, version, t_start: float, *, already: bool = False) -> int:
    mins = int((time.monotonic() - t_start) // 60)
    msg = (f"Home Assistant was already on {version}." if already else
           f"Home Assistant is now {version} and running" + (f" ({mins} min)." if mins else "."))
    row = updated_row(it, str(version), result="installed", note=None)
    em.emit(STEP, "ok", 100, msg, None)
    patch_state(row)
    em.done("ok", reboot_required=False, kmod_verified=None, summary=msg,
            other={"id": it["id"], "result": "installed", "item": row})
    return 0


def run_ha(em, it: dict, *, ctx: E.Ctx | None = None, sleep=time.sleep) -> int:
    """update.install for one add-on / HACS integration, judged against the version the row
    asked for. 2026-11-01 (Alexa Media Player): HACS downloaded v5.18.0 in 2 s and refreshed
    its repository data, which by then knew v5.19.0 (released that morning); the old check
    compared installed with the LIVE latest_version, saw "still older", and reported a
    failure while the right version sat downloaded, waiting for a core restart. Now:
    the target is the row's `latest` (pinned with `version` where the entity supports it, so
    what the confirmation listed is what runs), success = installed >= target, and an
    integration that needs a restart says so (`restart_pending`; the app restarts Home
    Assistant once afterwards with `--ha-restart`)."""
    if it.get("action") != "ha_install" or it.get("kind") not in ("addon", "integration"):
        raise Refused(f"{it.get('name')}: Home Assistant OS, supervisor and firmware are "
                      "updated by hand in Home Assistant.")
    eid = str(it.get("entity_id") or "")
    if not re.match(r"^update\.[a-z0-9_]+$", eid):
        raise Refused(f"refusing an odd entity id: {eid!r}")
    ctx = ctx or E.Ctx(env=os.environ)
    base, _token, auth = ha_conn(ctx)
    target = str(it.get("latest") or "") or None
    live: dict = {}
    try:
        live = ctx.http_json(f"{base}/api/states/{eid}", headers=auth, timeout=10) or {}
    except Exception:  # noqa: BLE001 - the install call below says whether HA is there
        live = {}
    la = live.get("attributes") or {} if isinstance(live, dict) else {}
    if target and la.get("installed_version") and not la.get("in_progress") \
            and not E.is_newer(la.get("installed_version"), target):
        return _ha_installed(em, it, la, target, already=True)
    data = {"entity_id": eid}
    if target and int(la.get("supported_features") or 0) & E.HA_SPECIFIC_VERSION:
        data["version"] = target
    if it.get("backup"):
        data["backup"] = True
    em.emit(STEP, "start", 0, f"Asking Home Assistant to install {it['name']} {target or ''}…".replace(" …", "…"),
            f"POST {base}/api/services/update/install {json.dumps(data)}")
    try:
        # HA answers a service call after ~10 s even when the update is still running
        ctx.http_json(f"{base}/api/services/update/install", headers=auth, data=data, timeout=30)
    except E.HttpError as exc:
        why = f"Home Assistant refused the install ({exc})"
        return _ha_fail(em, it, why)
    except Exception as exc:  # noqa: BLE001 - a client timeout: HA may well be working on it
        if E.unreachable(exc):
            # 2026-11-01 abuse pass: nobody there (refused, no route, no such name) used to be
            # read as "still working" and polled for 20 minutes. Nothing was sent: fail now.
            return _ha_fail(em, it, f"could not reach Home Assistant at {base} ({E._why(exc)}); "
                            "nothing was installed", unreachable=True)
        em.emit(STEP, "progress", None, "Home Assistant is still working on it…", f"({E._why(exc)})")
    t0, seen_progress, idle = time.monotonic(), False, 0
    gone_since = None
    while time.monotonic() - t0 < HA_TIMEOUT_S:
        try:
            st = ctx.http_json(f"{base}/api/states/{eid}", headers=auth, timeout=10)
            gone_since = None
        except Exception as exc:  # noqa: BLE001 - HA restarting (integrations) or busy
            if E.unreachable(exc):
                gone_since = gone_since or time.monotonic()
                if time.monotonic() - gone_since >= HA_GONE_S:
                    return _ha_fail(em, it, f"Home Assistant stopped answering while installing ({E._why(exc)} "
                                    f"for {int(HA_GONE_S)} s); it may still finish, check again later",
                                    unreachable=True)
            em.emit(STEP, "progress", None, "Waiting for Home Assistant…", f"({E._why(exc)})")
            sleep(HA_POLL_S)
            continue
        a = st.get("attributes") or {}
        want = target or a.get("latest_version")
        if a.get("in_progress"):
            seen_progress, idle = True, 0
            pct = a.get("update_percentage")
            em.emit(STEP, "progress", int(pct) if isinstance(pct, (int, float)) else None,
                    f"Installing {it['name']}…" + (f" {int(pct)}%" if isinstance(pct, (int, float)) else ""), None)
        elif not E.is_newer(a.get("installed_version"), want) or (not target and st.get("state") == "off"):
            return _ha_installed(em, it, a, target)
        else:
            idle += 1
            if idle >= (3 if seen_progress else 10):
                return _ha_fail(em, it, f"Home Assistant stopped without installing it (still "
                                f"{a.get('installed_version')}, wanted {want}); see Settings › System › Logs")
        sleep(HA_POLL_S)
    return _ha_fail(em, it, f"no answer from Home Assistant after {int(HA_TIMEOUT_S)} s; it may still finish")


def _ha_installed(em, it: dict, a: dict, target: str | None, *, already: bool = False) -> int:
    now = a.get("installed_version") or target
    pending = it.get("kind") == "integration" and (
        not already or bool(E.HA_RESTART_RE.search(str(a.get("release_summary") or ""))))
    newer = a.get("latest_version") if target and E.is_newer(target, a.get("latest_version")) else None
    if already:
        msg = f"{it['name']} {now} was already downloaded" + (
            "; Home Assistant needs a restart to load it." if pending else ".")
    elif pending:
        msg = f"{it['name']} {now} is downloaded; Home Assistant restarts to load it."
    else:
        msg = f"{it['name']} is now {now}."
    if newer:
        msg += f" ({newer} is out too; it shows up at the next check.)"
    note = "Waiting for a Home Assistant restart to load it." if pending else None
    row = updated_row(it, now, result="installed", note=note)
    row["restart_pending"] = pending
    em.emit(STEP, "ok", 100, msg, None)
    patch_state(row)
    em.done("ok", reboot_required=False, kmod_verified=None, summary=msg,
            other={"id": it["id"], "result": "installed", "item": row, "restart_pending": pending})
    return 0


def _ha_fail(em, it, why: str, *, unreachable: bool = False) -> int:
    em.emit(STEP, "fail", 100, why, None)
    row = updated_row(it, None, result="failed", note=why)
    patch_state(row)
    other = {"id": it["id"], "result": "failed", "item": row}
    if unreachable:
        other["unreachable"] = True      # the bridge skips the rest of a bulk run's HA rows
    em.done("fail", reboot_required=False, kmod_verified=None, summary=f"{it['name']}: {why}.",
            cause=why, other=other)
    return 1


# --------------------------------------------------------------------------- HA core restart
HA_RESTART_TIMEOUT_S = float(os.environ.get("RIVET_HA_RESTART_TIMEOUT_S", "600"))
RESTART_ID = "ha:_restart"


def restart_issues(ws) -> list[dict]:
    """HACS's `restart_required` repairs: [{issue_id, domain, name}] (the integrations that are
    downloaded but not loaded)."""
    out = []
    for i in (ws.call({"type": "repairs/list_issues"}) or {}).get("issues") or []:
        if i.get("domain") == "hacs" and i.get("translation_key") == "restart_required" and not i.get("ignored"):
            out.append({"issue_id": i.get("issue_id"), "domain": i.get("issue_domain"),
                        "name": (i.get("translation_placeholders") or {}).get("name")})
    return out


def loaded_version(ws, domain: str | None) -> str | None:
    """The version of an integration Home Assistant has LOADED (its manifest as of startup)."""
    if not domain or not re.fullmatch(r"[a-z0-9_]{1,64}", domain):
        return None
    try:
        return (ws.call({"type": "manifest/get", "integration": domain}) or {}).get("version")
    except Exception:  # noqa: BLE001
        return None


def row_domain(it: dict, issues: list[dict]) -> str | None:
    hit = next((i["domain"] for i in issues if i.get("name") == it.get("name") and i.get("domain")), None)
    return hit or (E.HA_BACKS.get(it.get("name")) or (None,))[0]


def _strip_v(v) -> str:
    return re.sub(r"^[vV]", "", str(v or ""))


def pending_restart(ws, state: dict) -> tuple[list[dict], list[dict]]:
    """(HACS restart issues, state.json HA rows flagged restart_pending that are not loaded yet)."""
    issues = restart_issues(ws)
    rows = []
    for it in ((state.get("elsewhere") or {}).get("items") or []):
        if it.get("source") != "ha" or it.get("kind") != "integration":
            continue
        dom = row_domain(it, issues)
        named = any(i.get("name") == it.get("name") for i in issues)
        if it.get("restart_pending") or named:
            have = loaded_version(ws, dom)
            if named or not have or _strip_v(have) != _strip_v(it.get("installed")):
                rows.append(dict(it, _domain=dom))
    return issues, rows


def run_ha_restart(em, state: dict, *, ctx: E.Ctx | None = None, sleep=time.sleep) -> int:
    """`rivet-apply --ha-restart`: restart Home Assistant core ONLY when something is waiting for
    it (a HACS `restart_required` repair, or a row this app installed with restart_pending), after
    HA's own config check passes; wait until it is RUNNING again and confirm the loaded versions.
    Never restarts the VM or the OS."""
    ctx = ctx or E.Ctx(env=os.environ)
    if core_running():
        return _restart_done(em, "refused", CORE_BUSY, [])
    try:
        base, token, auth = ha_conn(ctx)
        ws = ctx.ha_ws(base, token)
    except Refused as exc:
        return _restart_done(em, "refused", str(exc), [])
    except Exception as exc:  # noqa: BLE001
        return _restart_done(em, "failed", f"could not reach Home Assistant ({E._why(exc)}); nothing restarted", [])
    try:
        issues, rows = pending_restart(ws, state)
    except Exception as exc:  # noqa: BLE001
        return _restart_done(em, "failed", f"could not read Home Assistant's repairs ({E._why(exc)}); nothing restarted", [])
    finally:
        ws.close()
    names = sorted({r["name"] for r in rows} | {i["name"] for i in issues if i.get("name")})
    if not names:
        return _restart_done(em, "refused", "Home Assistant does not say it needs a restart, so Rivet did not restart it", [])
    what = ", ".join(names)
    em.emit(STEP, "start", 0, "Checking Home Assistant's configuration before restarting…",
            f"POST {base}/api/config/core/check_config")
    try:
        chk = ctx.http_json(f"{base}/api/config/core/check_config", headers=auth, data={}, timeout=90) or {}
    except Exception as exc:  # noqa: BLE001
        return _restart_done(em, "failed", f"the configuration check did not answer ({E._why(exc)}); not restarting", rows)
    if chk.get("result") != "valid":
        return _restart_done(em, "failed", "Home Assistant's configuration does not check out "
                             f"({str(chk.get('errors') or chk.get('result'))[:200]}); not restarting", rows)
    em.emit(STEP, "progress", 15, f"Restarting Home Assistant to load {what}…",
            f"POST {base}/api/services/homeassistant/restart")
    try:
        ctx.http_json(f"{base}/api/services/homeassistant/restart", headers=auth, data={}, timeout=30)
    except E.HttpError as exc:
        return _restart_done(em, "failed", f"Home Assistant refused the restart ({exc})", rows)
    except Exception:  # noqa: BLE001 - the connection drops as it goes down: expected
        pass
    t0, down = time.monotonic(), False
    while True:
        sleep(HA_POLL_S)
        el = time.monotonic() - t0
        if el > HA_RESTART_TIMEOUT_S:
            return _restart_done(em, "failed", f"Home Assistant was not back after {int(HA_RESTART_TIMEOUT_S)} s", rows)
        try:
            cfg = ctx.http_json(f"{base}/api/config", headers=auth, timeout=5) or {}
            up = cfg.get("state") == "RUNNING"
        except Exception:  # noqa: BLE001
            up = False
        if not up:
            if not down:
                em.emit(STEP, "progress", 40, "Home Assistant is restarting…", None)
            down = True
            continue
        if down:
            break
        if el > 120:
            return _restart_done(em, "failed", "Home Assistant never went down after the restart call", rows)
    em.emit(STEP, "progress", 85, f"Home Assistant is back ({int(time.monotonic() - t0)} s). Checking what it loaded…", None)
    out, still = [], []
    try:
        ws = ctx.ha_ws(base, token)
    except Exception as exc:  # noqa: BLE001
        return _restart_done(em, "failed", f"Home Assistant is back but its websocket is not ({E._why(exc)})", rows)
    try:
        left = {i.get("name") for i in restart_issues(ws)}
        for r in rows:
            have = loaded_version(ws, r.get("_domain"))
            new = {k: v for k, v in r.items() if k != "_domain"}
            try:
                st = ctx.http_json(f"{base}/api/states/{r['entity_id']}", headers=auth, timeout=10) or {}
                new["installed"] = (st.get("attributes") or {}).get("installed_version") or new.get("installed")
            except Exception:  # noqa: BLE001
                pass
            ok = r["name"] not in left and (have is None or _strip_v(have) == _strip_v(new.get("installed")))
            new["restart_pending"] = not ok
            new["loaded"] = have
            if ok:
                note = f"Home Assistant restarted; it now runs {have or new.get('installed')}."
                new.update(status="updated", action=None, confirm=False,
                           last_run={"at": now_iso(), "result": "installed", "note": note})
            else:
                still.append(f"{r['name']} (runs {have or '?'}, downloaded {new.get('installed')})")
                new["last_run"] = {"at": now_iso(), "result": "failed",
                                   "note": f"Home Assistant restarted but still runs {have or '?'}"}
            patch_state(new)
            out.append(new)
    finally:
        ws.close()
    if still:
        return _restart_done(em, "failed", "Home Assistant restarted but did not load " + ", ".join(still), out)
    loaded = ", ".join(f"{r['name']} {r.get('loaded') or r.get('installed')}" for r in out) or what
    return _restart_done(em, "restarted", f"Restarted Home Assistant. Now running {loaded}.", out)


def _restart_done(em, result: str, msg: str, rows: list[dict]) -> int:
    ok = result == "restarted"
    em.emit(STEP, "ok" if ok else "fail", 100, msg, None)
    items = [{k: v for k, v in r.items() if k != "_domain"} for r in rows]
    em.done("ok" if ok else "fail", reboot_required=False, kmod_verified=None,
            summary=msg + ("" if msg.endswith(".") else "."), cause=None if ok else msg,
            other={"id": RESTART_ID, "result": result, "item": None, "items": items})
    return 0 if ok else (2 if result == "refused" else 1)


# --------------------------------------------------------------------------- Remove (HA add-on)
SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
REMOVE_TIMEOUT_S = float(os.environ.get("RIVET_HA_REMOVE_TIMEOUT_S", "300"))


def remove_refusal(it: dict) -> str | None:
    """Why this row may not be uninstalled from Rivet (None = it may). Same rule as the UI."""
    name = it.get("name") or it.get("id")
    if it.get("source") != "ha" or it.get("kind") != "addon":
        return f"{name}: only Home Assistant add-ons can be removed from Rivet."
    if it.get("unused") is not True:
        return f"{name}: Home Assistant does not report it as unused, so Rivet will not remove it."
    if not SLUG.match(str(it.get("addon_slug") or "")):
        return f"{name}: its add-on slug is unknown (check again first)."
    if not E.HA_BACKS.get(it.get("name")):
        return f"{name}: Rivet cannot tell what uses it, so it will not remove it."
    return None


def _gone(exc: Exception) -> bool:
    return isinstance(exc, E.HaWsError) and bool(re.search(r"not installed|does not exist", str(exc), re.I))


def run_remove(em, it: dict, *, ctx: E.Ctx | None = None, sleep=time.sleep) -> int:
    why = remove_refusal(it)
    if why:
        raise Refused(why)
    slug, name = it["addon_slug"], it["name"]
    domain, label = E.HA_BACKS[name]
    ctx = ctx or E.Ctx(env=os.environ)
    token, why = E.ha_token(ctx)
    if not token:
        raise Refused(f"no Home Assistant token ({why or E.HA_NO_TOKEN}).")
    base = E.ha_base(ctx)
    em.emit(STEP, "start", 0, f"Checking that nothing in Home Assistant uses {name}…",
            f"POST {base}/api/template integration_entities('{domain}') | count")
    # state.json may be hours old: count the entities again, now, and refuse on any doubt
    try:
        got = ctx.http_json(f"{base}/api/template", headers={"Authorization": f"Bearer {token}"},
                            data={"template": "{{ {'n': integration_entities('%s') | count} | tojson }}" % domain},
                            timeout=15)
        n = int((got or {}).get("n"))
    except Exception as exc:  # noqa: BLE001
        return _remove_fail(em, it, f"could not confirm that nothing uses it ({E._why(exc)}); nothing was removed")
    if n:
        return _remove_fail(em, it, f"the {label} integration has {n} entit{'y' if n == 1 else 'ies'} now, "
                            "so something uses it; nothing was removed")
    em.emit(STEP, "progress", 10, f"0 {label} entities. Asking the Supervisor about {name}…", None)
    try:
        ws = ctx.ha_ws(base, token)
    except Exception as exc:  # noqa: BLE001
        return _remove_fail(em, it, f"could not open Home Assistant's websocket ({E._why(exc)}); nothing was removed")
    try:
        try:
            info = ws.supervisor(f"/addons/{slug}/info") or {}
        except Exception as exc:  # noqa: BLE001
            if _gone(exc):
                return _removed(em, it, f"{name} was already uninstalled.")
            return _remove_fail(em, it, f"the Supervisor did not answer about {slug} ({E._why(exc)}); nothing was removed")
        if info.get("slug") not in (None, slug):
            return _remove_fail(em, it, f"the Supervisor answered for {info.get('slug')!r}, not {slug}; nothing was removed")
        call = f"supervisor/api POST /addons/{slug}/uninstall {{\"remove_config\": false}}"
        em.emit(STEP, "progress", 20, f"Uninstalling {name} ({info.get('state') or '?'}, {info.get('version') or '?'})…",
                call)
        try:
            ws.supervisor(f"/addons/{slug}/uninstall", "post", {"remove_config": False}, timeout=REMOVE_TIMEOUT_S)
        except E.HaWsError as exc:
            if not _gone(exc):
                return _remove_fail(em, it, f"Home Assistant refused to uninstall it ({exc})")
        except Exception as exc:  # noqa: BLE001 - a client timeout: the Supervisor may still finish
            em.emit(STEP, "progress", None, "The Supervisor is still working on it…", f"({E._why(exc)})")
        t0 = time.monotonic()
        while time.monotonic() - t0 < REMOVE_TIMEOUT_S:
            try:
                left = ws.supervisor(f"/addons/{slug}/info") or {}
            except Exception as exc:  # noqa: BLE001
                if _gone(exc):
                    return _removed(em, it, f"{name} was uninstalled from Home Assistant.")
                em.emit(STEP, "progress", None, "Waiting for the Supervisor…", f"({E._why(exc)})")
            else:
                if not left.get("version") or str(left.get("state") or "").lower() in ("unknown", "not_installed"):
                    return _removed(em, it, f"{name} was uninstalled from Home Assistant.")
            sleep(HA_POLL_S)
        return _remove_fail(em, it, f"the Supervisor still lists {slug} after {int(REMOVE_TIMEOUT_S)} s; "
                            "it may still finish (see Settings › Apps)")
    finally:
        ws.close()


def _removed(em, it: dict, msg: str) -> int:
    em.emit(STEP, "ok", 100, msg, None)
    patch_state(it, remove=True)
    em.done("ok", reboot_required=False, kmod_verified=None, summary=msg,
            other={"id": it["id"], "result": "removed", "item": None, "removed": True})
    return 0


def _remove_fail(em, it: dict, why: str) -> int:
    em.emit(STEP, "fail", 100, why, None)
    row = dict(it, last_run={"at": now_iso(), "result": "remove_failed", "note": why})
    patch_state(row)
    em.done("fail", reboot_required=False, kmod_verified=None, summary=f"{it['name']} was not removed: {why}.",
            cause=why, other={"id": it["id"], "result": "failed", "item": row, "link": it.get("addon_link")})
    return 1


def run(em, item_id: str, state: dict, *, remove: bool = False, confirmed: bool = False) -> int:
    try:
        it = find_item(state, item_id)
        if it.get("source") == "ha" and core_running():
            raise Refused(f"{it.get('name')}: {CORE_BUSY}.")
        if remove:
            return run_remove(em, it)
        if it.get("source") == "ha" and it.get("kind") == "core":
            return run_core(em, it, confirmed=confirmed)
        if it.get("source") == "ha":
            return run_ha(em, it)
        return run_local(em, it)
    except Refused as exc:
        msg = str(exc)
        em.done("fail", reboot_required=False, kmod_verified=None, summary=msg, cause=msg,
                other={"id": item_id, "result": "refused", "item": None})
        return 2
