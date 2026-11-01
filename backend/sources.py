"""Collectors: dnf5, Flatpak (user + system), fwupd.

Each collector returns a list of partial items (no risk/badge/verdict yet) plus
whatever side facts the caller needs. Nothing here raises: a broken source
yields an empty list and a warning, so one dead tool cannot blank state.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import flatpak_nvidia
import holds
from common import excerpt, log, run
from rules import flatpak_id, severity_norm

BODHI = "https://bodhi.fedoraproject.org/updates/"

# ---------------------------------------------------------------- dnf5


def _strip_epoch(evr: str) -> str:
    return evr.split(":", 1)[1] if ":" in evr else evr


def _installed_versions() -> dict[tuple[str, str], str]:
    """(name, arch) -> evr, straight from the rpmdb (no repo I/O)."""
    cp = run(["rpm", "-qa", "--qf", "%{NAME}\t%{ARCH}\t%{EPOCH}\t%{VERSION}-%{RELEASE}\n"], timeout=60)
    out: dict[tuple[str, str], str] = {}
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, arch, epoch, vr = parts
        evr = vr if epoch in ("(none)", "", "0") else f"{epoch}:{vr}"
        prev = out.get((name, arch))
        # keep the highest installed (multiple kernels)
        if prev is None or _vercmp(evr, prev) > 0:
            out[(name, arch)] = evr
    return out


def _vercmp(a: str, b: str) -> int:
    """Crude but adequate rpm-ish version compare for picking the newest installed."""
    def key(v: str):
        return [
            int(x) if x.isdigit() else x
            for x in re.split(r"[._\-:]", v)
            if x != ""
        ]
    ka, kb = key(a), key(b)
    for x, y in zip(ka, kb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return 1 if x > y else -1
        return 1 if str(x) > str(y) else -1
    return (len(ka) > len(kb)) - (len(ka) < len(kb))


def _download_sizes() -> dict[tuple[str, str], int]:
    cp = run(
        [
            "dnf5", "repoquery", "--upgrades", "--latest-limit", "1", "--quiet",
            "--queryformat", "%{name}\t%{arch}\t%{downloadsize}\n",
        ],
        timeout=120,
    )
    sizes: dict[tuple[str, str], int] = {}
    for line in cp.stdout.splitlines():
        p = line.split("\t")
        if len(p) != 3:
            continue
        try:
            sizes[(p[0], p[1])] = int(p[2])
        except ValueError:
            pass
    return sizes


def _advisories() -> list[dict]:
    cp = run(["dnf5", "updateinfo", "info", "--json", "--quiet"], timeout=180)
    if cp.returncode != 0 or not cp.stdout.strip():
        log(f"updateinfo unavailable (rc={cp.returncode}); continuing without advisories")
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        log(f"updateinfo JSON parse failed: {exc}")
        return []
    return data if isinstance(data, list) else []


def _advisory_index(advs: list[dict]) -> dict[str, dict]:
    """'curl-8.18.0-10.fc44.x86_64' -> advisory. Also a name-only fallback map."""
    idx: dict[str, dict] = {}
    for a in advs:
        for pkg in (a.get("collections") or {}).get("packages") or []:
            idx.setdefault(pkg, a)
    return idx


def collect_dnf() -> list[dict]:
    cp = run(["dnf5", "check-upgrade", "--json", "--quiet"], timeout=180)
    if cp.returncode not in (0, 100) or not cp.stdout.strip():
        log(f"dnf5 check-upgrade failed (rc={cp.returncode}): {cp.stderr.strip()[:200]}")
        return []
    try:
        upgrades = json.loads(cp.stdout).get("upgrades") or []
    except json.JSONDecodeError as exc:
        log(f"check-upgrade JSON parse failed: {exc}")
        return []

    installed = _installed_versions()
    sizes = _download_sizes()
    advs = _advisories()
    adv_idx = _advisory_index(advs)
    # name -> advisories mentioning that source name at all (fallback)
    by_name: dict[str, list[dict]] = {}
    for pkg, a in adv_idx.items():
        m = re.match(r"^(.*?)-[^-]+-[^-]+\.[^.]+$", pkg)
        if m:
            by_name.setdefault(m.group(1), []).append(a)

    items = []
    for u in upgrades:
        name, arch, evr = u.get("name"), u.get("arch"), u.get("evr")
        if not (name and arch and evr):
            continue
        bare = _strip_epoch(evr)
        adv = (
            adv_idx.get(f"{name}-{bare}.{arch}")
            or adv_idx.get(f"{name}-{evr}.{arch}")
        )
        if adv is None:
            for cand in by_name.get(name, []):
                if cand.get("Title", "").startswith(f"{name}-{bare}"):
                    adv = cand
                    break
        advisory = None
        notes = ""
        notes_url = None
        if adv:
            advisory = {
                "id": adv.get("Name"),
                "type": (adv.get("Type") or "unspecified").lower(),
                "severity": severity_norm(adv.get("Severity")),
            }
            notes = excerpt(adv.get("Description"))
            if str(adv.get("Name", "")).startswith(("FEDORA-", "FEDORA-EPEL-")):
                notes_url = BODHI + adv["Name"]
            else:
                refs = adv.get("references") or []
                notes_url = next((r.get("Url") for r in refs if r.get("Url")), None)
        items.append(
            {
                "id": f"dnf:{name}",
                "source": "dnf",
                "name": name,
                "display_name": name,
                "from": installed.get((name, arch)) or installed.get((name, "noarch")) or "(new)",
                "to": evr,
                "size_bytes": sizes.get((name, arch), 0),
                "ref": f"{name}-{bare}.{arch}",
                "advisory": advisory,
                "notes_excerpt": notes,
                "notes_url": notes_url,
                "_repo": u.get("repository"),
                "_arch": arch,
            }
        )
    items.sort(key=lambda i: i["name"].lower())
    return items


# ---------------------------------------------------------------- Flatpak

_FP_COLUMNS = "ref,origin,download-size,name,version"


def _flatpak_installed(scope: str) -> dict[str, dict]:
    cp = run(["flatpak", "list", f"--{scope}", "--columns=ref,version,active,options,name"],
             timeout=60)
    out = {}
    for line in cp.stdout.splitlines():
        p = line.split("\t")
        if len(p) >= 1 and p[0]:
            out[p[0]] = {
                "version": p[1] if len(p) > 1 else "",
                "commit": p[2] if len(p) > 2 else "",
                # `flatpak list` prints bare refs; the options column says which kind it is
                "kind": ("runtime" if "runtime" in p[3] else "app") if len(p) > 3 else "",
                "name": p[4] if len(p) > 4 else "",
            }
    return out


def _parse_size(s: str) -> int:
    """'259.3 MB' -> bytes. flatpak prints human sizes in --columns output."""
    m = re.match(r"^\s*([\d.,]+)\s*([kKMGT]?i?B|bytes)?\s*$", s or "")
    if not m:
        return 0
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = (m.group(2) or "B").lower().rstrip("b").rstrip("i")
    mult = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4}.get(unit, 1)
    return int(val * mult)


def _flatpak_masks(scope: str) -> list[str]:
    """Patterns from `flatpak mask`: refs the user has pinned against updates.

    `flatpak update` skips them, but `remote-ls --updates` still lists them. Showing one as pending
    invites un-pinning a build that was pinned for a reason (2026-09-21: gamescope 3.16.29 broke
    game launches, so 3.16.28 was pinned)."""
    cp = run(["flatpak", "mask", f"--{scope}"], timeout=30)
    if cp.returncode != 0:
        return []
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip() and "No masked" not in ln]


def _is_masked(ref: str, masks: list[str]) -> bool:
    """Flatpak mask patterns are partial refs with globs: `id//branch`, `id`, `runtime/id/arch/branch`."""
    return holds.is_masked(ref, masks)


def _full_ref(bare: str, inst: dict, registry: dict) -> str:
    """`flatpak list` ref -> full ref. Registry key first, then the options column, then a guess."""
    key, _hold = holds.find(registry, bare)
    if key:
        return key
    kind = inst.get("kind") or ("runtime" if ".Platform" in bare or ".Sdk" in bare else "app")
    return f"{kind}/{bare}"


# Remotes the last collect_flatpak() could not read: [{"scope", "remote", "error"}]
# (state.json flags.flatpak_remote_errors, 2026-11-01).
REMOTE_ERRORS: list[dict] = []


def _err_line(cp) -> str:
    lines = [x.strip() for x in (cp.stderr or "").splitlines() if x.strip()]
    return (lines[-1] if lines else f"rc={cp.returncode}")[:200]


def _remote_ls_updates(scope: str) -> tuple[str | None, list[dict]]:
    """`remote-ls --updates` for one installation -> (listing or None, remotes that failed).

    2026-11-01 abuse pass: ONE unreachable or broken remote makes the whole command fail
    ("error: Unable to load summary from remote X"), which used to drop every update of that
    installation without a word: Flatpak read as "up to date". On a failure each remote is
    asked on its own; the working ones still count, the broken ones are reported."""
    cmd = ["flatpak", "remote-ls", f"--{scope}", "--updates", f"--columns={_FP_COLUMNS}"]
    cp = run(cmd, timeout=180)
    if cp.returncode == 0:
        return cp.stdout, []
    log(f"flatpak remote-ls --{scope} failed (rc={cp.returncode}): {cp.stderr.strip()[:200]}; asking each remote")
    rem = run(["flatpak", "remotes", f"--{scope}", "--columns=name"], timeout=30)
    names = [ln.split()[0] for ln in rem.stdout.splitlines() if ln.strip()] if rem.returncode == 0 else []
    if not names:
        # no remotes at all is not an error (a fresh box has no user remotes)
        return (None, []) if rem.returncode == 0 else (None, [{"scope": scope, "remote": None, "error": _err_line(cp)}])
    out, bad = [], []
    for r in names:
        c = run(cmd + [r], timeout=120)
        if c.returncode == 0:
            out.append(c.stdout)
        else:
            bad.append({"scope": scope, "remote": r, "error": _err_line(c)})
            log(f"flatpak remote {r} ({scope}) could not be read: {_err_line(c)}")
    return "\n".join(out), bad


def collect_flatpak() -> list[dict]:
    """Pending Flatpak items, plus `held: True` items for refs pinned with `flatpak mask`
    (2026-09-21b: shown in their own group instead of dropped; see holds.py)."""
    items = []
    REMOTE_ERRORS.clear()
    masks_by_scope = {scope: _flatpak_masks(scope) for scope in ("user", "system")}
    registry = holds.load()
    held_refs: set[str] = set()
    for scope in ("user", "system"):
        masks = masks_by_scope[scope]
        listing, bad = _remote_ls_updates(scope)
        REMOTE_ERRORS.extend(bad)
        if listing is None:
            continue
        installed = _flatpak_installed(scope)
        for line in listing.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            cols += [""] * (5 - len(cols))
            ref, origin, dlsize, disp, version = cols[:5]
            if not ref:
                continue
            # `flatpak list` prints refs without the app/ or runtime/ prefix
            inst = installed.get(ref) or installed.get(ref.split("/", 1)[-1], {})
            if _is_masked(ref, masks):
                _key, hold = holds.find(registry, ref)
                held = holds.held_item(
                    ref, scope, hold, display_name=disp, size_bytes=_parse_size(dlsize),
                    installed=inst.get("version") or inst.get("commit", "")[:12] or "(installed)",
                    available=version or None, origin=origin)
                log(f"flatpak: {ref} is pinned by `flatpak mask`; held back ({held['hold_state']}, "
                    f"{held['from']} -> {held['to']})")
                items.append(held)
                held_refs.add(ref)
                continue
            appid = flatpak_id(ref)
            items.append(
                {
                    "id": f"flatpak:{ref}",
                    "source": "flatpak",
                    "name": appid,
                    "display_name": disp or appid,
                    "from": inst.get("version") or inst.get("commit", "")[:12] or "(installed)",
                    "to": version or "(latest)",
                    "size_bytes": _parse_size(dlsize),
                    "ref": ref,
                    "advisory": None,
                    "notes_excerpt": "",
                    "notes_url": None,
                    "_scope": scope,
                    "_origin": origin,
                }
            )
        # pinned refs with nothing newer on the remote, and trials waiting for a verdict
        # (unmasked by --try-held, so only the registry still knows about them)
        for bare, inst in installed.items():
            ref = _full_ref(bare, inst, registry)
            if ref in held_refs:
                continue
            _key, hold = holds.find(registry, ref)
            trial = bool(hold and hold.get("pending_trial") and hold.get("scope", scope) == scope)
            if not (_is_masked(ref, masks) or trial):
                continue
            items.append(holds.held_item(
                ref, scope, hold, display_name=inst.get("name") or "", available=None,
                installed=inst.get("version") or inst.get("commit", "")[:12] or "(installed)"))
            held_refs.add(ref)
    all_masks = [m for ms in masks_by_scope.values() for m in ms]
    items += [i for i in _flatpak_installs({i["ref"] for i in items})
              if not _is_masked(i["ref"], all_masks)]
    items.sort(key=lambda i: (i["source"], i["display_name"].lower()))
    return items


def _flatpak_dryrun() -> list[dict]:
    """What `flatpak update` would do, from libflatpak (see flatpak_dryrun.py)."""
    fixture = os.environ.get("RIVET_FLATPAK_DRYRUN_FIXTURE")  # QA: canned ops, no libflatpak
    if fixture:
        return json.loads(Path(fixture).read_text()).get("ops") or []
    helper = Path(__file__).resolve().parent / "flatpak_dryrun.py"
    cp = run([sys.executable, str(helper)], timeout=180)
    try:
        doc = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        doc = {}
    for err in doc.get("errors") or ([] if doc else [f"rc={cp.returncode} {cp.stderr.strip()[:160]}"]):
        log(f"flatpak dry run: {err}")
    return doc.get("ops") or []


def _install_item(scope: str, ref: str, origin: str, size: int, *, op: str = "install") -> dict:
    appid = flatpak_id(ref)
    nv = flatpak_nvidia.parse_ref(ref)
    item = {
        "id": f"flatpak:{ref}",
        "source": "flatpak",
        "name": appid,
        "display_name": appid,
        "from": "(not installed)" if op == "install" else "(installed)",
        "to": ref.rsplit("/", 1)[-1],
        "size_bytes": int(size or 0),
        "ref": ref,
        "advisory": None,
        "notes_excerpt": ("Flatpak wants this runtime for apps that are already installed; "
                          "`flatpak update` would add it." if op == "install" else ""),
        "notes_url": None,
        "op": op,
        "scope": scope,
        "origin": origin,
        "_scope": scope,
        "_origin": origin,
    }
    if nv:
        version = flatpak_nvidia.untag(nv["tag"])
        have = sorted({flatpak_nvidia.untag(h["tag"]) for h in flatpak_nvidia.installed(scope)
                       if h["kind"] == nv["kind"]} - {version})
        item.update(
            display_name=flatpak_nvidia.display_name(nv["kind"], version),
            to=version,
            notes_excerpt=flatpak_nvidia.notes(version, have),
        )
        if have:
            item["from"] = have[-1]
    return item


def _flatpak_installs(known_refs: set[str]) -> list[dict]:
    """Pending Flatpak work that `remote-ls --updates` cannot see (2026-09-21).

    1. libflatpak dry run: related refs Flatpak now wants and does not have (plus any
       update remote-ls missed). Uninstall ops are ignored -- cleanup, not an update,
       and GNOME Software does not list them either.
    2. The NVIDIA runtime for the host driver RPM. The dry run only knows the module
       that is loaded now; between a driver upgrade and the reboot only the rpm knows.
    """
    out: list[dict] = []
    seen = set(known_refs)
    for op in _flatpak_dryrun():
        if op.get("op") not in ("install", "update") or op.get("ref") in seen:
            continue
        seen.add(op["ref"])
        out.append(_install_item(op["scope"], op["ref"], op.get("remote") or "flathub",
                                 op.get("download_bytes") or 0, op=op["op"]))
    try:
        gaps = flatpak_nvidia.missing(flatpak_nvidia.host_driver_version())
    except Exception as exc:  # noqa: BLE001 - one dead probe must not blank the list
        log(f"nvidia runtime check failed: {exc}")
        gaps = []
    for gap in gaps:
        if gap["ref"] in seen:
            continue
        if not flatpak_nvidia.remote_has(gap["scope"], gap["ref"]):
            log(f"nvidia runtime {gap['ref']} is not on Flathub yet; not listed")
            continue
        seen.add(gap["ref"])
        out.append(_install_item(gap["scope"], gap["ref"], flatpak_nvidia.REMOTE, 0))
    return out


def steam_running() -> bool:
    cp = run(["flatpak", "ps", "--columns=application"], timeout=30)
    return "com.valvesoftware.Steam" in cp.stdout


# ---------------------------------------------------------------- fwupd


def collect_fwupd() -> list[dict]:
    cp = run(["fwupdmgr", "get-updates", "--json"], timeout=120)
    if not cp.stdout.strip():
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    items = []
    for dev in data.get("Devices") or []:
        rels = dev.get("Releases") or []
        if not rels:
            continue
        rel = rels[0]
        flags = set(dev.get("Flags") or []) | set(rel.get("Flags") or [])
        dev_id = dev.get("DeviceId") or dev.get("Name", "")
        items.append(
            {
                "id": f"fwupd:{dev_id}",
                "source": "fwupd",
                "name": dev.get("Name") or dev_id,
                "display_name": rel.get("Name") or dev.get("Name") or dev_id,
                "from": dev.get("Version") or "",
                "to": rel.get("Version") or "",
                "size_bytes": int(rel.get("Size") or 0),
                "ref": dev_id,
                "advisory": None,
                "notes_excerpt": excerpt(rel.get("Description") or rel.get("Summary")),
                "notes_url": rel.get("DetailsUrl") or dev.get("DetailsUrl"),
                "_needs_reboot": "needs-reboot" in flags,
                "_vendor": dev.get("Vendor"),
                "_plugin": dev.get("Plugin"),
            }
        )
    return items
