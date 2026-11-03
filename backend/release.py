"""A newer Fedora release: is one out, would this box's repos follow, is it downloaded.

The hourly check asks Fedora's own release list (https://fedoraproject.org/releases.json,
the file the Fedora website is generated from; one entry per image, `version` "45",
"45 Beta", "44", ...) for the newest numbered release and compares it with VERSION_ID in
/etc/os-release. A newer one becomes ONE row in the System (dnf) group, built by `item()`:
yellow, badged REBOOT, never ticked by default, `op: "release"`, with the facts under
`release`. Nothing here is ever root, and nothing here downloads a package.

Repos: `dnf5 system-upgrade download` loads every enabled repo at the new $releasever and
stops at the first one that does not exist there (a COPR whose owner has not built for the
new Fedora yet, a vendor repo pinned to one release). So, when a release is out, every
enabled repo whose URL carries $releasever is probed once at the new version (one small
GET: the metalink / mirrorlist itself, or baseurl/repodata/repomd.xml; redirects followed,
nothing parsed). `missing_repos` is what rivet-apply then passes as --disable-repo, and
what the row says. A probe that cannot be read (timeout, 5xx, no network) is `unknown`:
neither held against the repo nor disabled.

Downloaded: dnf5 keeps an offline transaction under /usr/lib/sysimage/libdnf5/offline/
(world-readable). When one is there, the row says "downloaded, restart to install it".

Fixture (QA): rivet-collect --fixture with a `release` object replaces the network: the
keys of `check()`'s result, any subset; `missing_repos` and `downloaded` default to none.
RIVET_NO_RELEASE=1 skips the check (tests, offline QA).
"""
from __future__ import annotations

import configparser
import json
import os
import platform
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import log

RELEASES_URL = "https://fedoraproject.org/releases.json"
NOTES_URL = "https://docs.fedoraproject.org/en-US/fedora/f{v}/release-notes/"
REPOS_DIR = Path(os.environ.get("RIVET_REPOS_DIR", "/etc/yum.repos.d"))
OFFLINE_DIR = Path(os.environ.get("RIVET_OFFLINE_DIR", "/usr/lib/sysimage/libdnf5/offline"))
OS_RELEASE = Path(os.environ.get("RIVET_OS_RELEASE", "/etc/os-release"))
FETCH_TIMEOUT_S = 15
PROBE_TIMEOUT_S = 10
UA = "rivet-updater (+https://github.com/NelsonScott/rivet)"


# ---------------------------------------------------------------- what is out


def current_version() -> int | None:
    """VERSION_ID of the running system, as an int (44), or None off Fedora."""
    try:
        for line in OS_RELEASE.read_text().splitlines():
            if line.startswith("VERSION_ID="):
                v = line.split("=", 1)[1].strip().strip('"')
                return int(v) if v.isdigit() else None
    except Exception:
        pass
    return None


def newest_release(doc) -> int | None:
    """The highest plain-numbered `version` in releases.json ("45"; never "45 Beta")."""
    best = None
    for entry in doc if isinstance(doc, list) else []:
        v = str((entry or {}).get("version", "")).strip()
        if v.isdigit() and (best is None or int(v) > best):
            best = int(v)
    return best


def fetch_json(url: str, timeout: float = FETCH_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- repos at the new release


def _basearch() -> str:
    m = platform.machine()
    return m or "x86_64"


def enabled_release_repos(repos_dir: Path = REPOS_DIR) -> list[dict]:
    """Enabled repos whose URL mentions $releasever: [{id, name, kind, url}] (url unexpanded)."""
    out = []
    for path in sorted(repos_dir.glob("*.repo")):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError) as exc:
            log(f"release: skipping {path.name}: {exc}")
            continue
        for sid in cp.sections():
            s = cp[sid]
            if s.get("enabled", "1").strip().lower() not in ("1", "true", "yes"):
                continue
            for kind in ("metalink", "mirrorlist", "baseurl"):
                raw = (s.get(kind) or "").strip()
                if not raw:
                    continue
                url = raw.split()[0]            # baseurl may list several; the first is probed
                if "$releasever" in url:
                    out.append({"id": sid, "name": (s.get("name") or sid).strip(), "kind": kind, "url": url})
                break
    return out


def expand(url: str, version: int, arch: str) -> str:
    return (url.replace("$releasever_major", str(version)).replace("$releasever", str(version))
            .replace("$basearch", arch).replace("$arch", arch))


def probe_url(repo: dict, version: int, arch: str) -> str:
    url = expand(repo["url"], version, arch)
    if repo["kind"] == "baseurl":
        url = url.rstrip("/") + "/repodata/repomd.xml"
    return url


def http_status(url: str, timeout: float = PROBE_TIMEOUT_S) -> int | None:
    """The final status of a GET (redirects followed, body not read), None when unreachable."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def probe_repos(version: int, *, repos_dir: Path = REPOS_DIR, arch: str | None = None,
                status=http_status) -> list[dict]:
    """[{id, name, status: ok|missing|unknown, http}] for every enabled $releasever repo."""
    arch = arch or _basearch()
    repos = enabled_release_repos(repos_dir)
    if not repos:
        return []
    urls = [probe_url(r, version, arch) for r in repos]
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
        codes = list(ex.map(status, urls))
    out = []
    for r, url, code in zip(repos, urls, codes):
        st = "ok" if code is not None and 200 <= code < 300 else "missing" if code in (403, 404, 410) else "unknown"
        out.append({"id": r["id"], "name": r["name"], "status": st, "http": code, "url": url})
    return out


# ---------------------------------------------------------------- downloaded already?


def offline_staged(version: int, offline_dir: Path | None = None) -> bool:
    """dnf5 has an offline transaction waiting (system-upgrade download ran, no reboot yet).

    The directory is world-readable; the transaction file names the target release when
    it was made by system-upgrade. A file that names a DIFFERENT release is not ours."""
    offline_dir = OFFLINE_DIR if offline_dir is None else offline_dir
    try:
        files = [p for p in offline_dir.iterdir() if p.is_file() and p.suffix == ".json"]
    except OSError:
        return False
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            return True                                   # there, just not readable: staged
        m = re.search(r'"releasever"\s*:\s*"?(\d+)', text)
        if m is None or m.group(1) == str(version):
            return True
    return False


# ---------------------------------------------------------------- the check


def check(*, fixture: dict | None = None, probe: bool = True) -> dict | None:
    """None when this box runs the newest release (or nothing can be said); else the facts:

    {version, current, notes_url, repos: [...], missing_repos: [ids], checked_repos: N,
     downloaded: bool}. Never raises."""
    if fixture is not None:
        rel = dict(fixture)
        try:
            rel["version"] = int(rel["version"])
        except (KeyError, TypeError, ValueError):
            return None
        rel.setdefault("current", rel["version"] - 1)
        rel.setdefault("notes_url", NOTES_URL.format(v=rel["version"]))
        rel.setdefault("repos", [])
        rel.setdefault("missing_repos", [])
        rel.setdefault("checked_repos", len(rel["repos"]))
        rel.setdefault("downloaded", False)
        return rel
    try:
        cur = current_version()
        if cur is None:
            return None
        newest = newest_release(fetch_json(RELEASES_URL))
        if newest is None or newest <= cur:
            return None
        repos = probe_repos(newest) if probe else []
        return {
            "version": newest,
            "current": cur,
            "notes_url": NOTES_URL.format(v=newest),
            "repos": repos,
            "missing_repos": [r["id"] for r in repos if r["status"] == "missing"],
            "checked_repos": len(repos),
            "downloaded": offline_staged(newest),
        }
    except Exception as exc:  # noqa: BLE001 - the hourly check must never fail over this
        log(f"release check skipped: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- the row


def is_release(item: dict | None) -> bool:
    return bool(item) and item.get("op") == "release"


def _repo_label(rid: str) -> str:
    """copr:copr.fedorainfracloud.org:owner:proj -> owner/proj; anything else as is."""
    m = re.match(r"^copr:[^:]+:([^:]+):([^:]+)$", rid)
    return f"{m.group(1)}/{m.group(2)}" if m else rid


def verdict(rel: dict) -> tuple[str, str]:
    """(one line for the row, the 'why' paragraph), deterministic, no model."""
    v, cur = rel["version"], rel.get("current")
    missing = [_repo_label(r) for r in rel.get("missing_repos") or []]
    if rel.get("downloaded"):
        line = f"Fedora {v} is downloaded. Restart to install it when you have an hour."
    elif missing:
        line = (f"Fedora {v} is out. {len(missing)} of your repos "
                f"{'has' if len(missing) == 1 else 'have'} no Fedora {v} builds yet "
                f"({', '.join(missing[:3])}{', …' if len(missing) > 3 else ''}); Rivet would leave "
                f"{'it' if len(missing) == 1 else 'them'} out.")
    else:
        line = f"Fedora {v} is out. Finish the other updates first, then upgrade when you have an hour."
    why = (f"A release upgrade replaces every package on the system with its Fedora {v} build: "
           f"the kernel, the NVIDIA driver, systemd and glibc all at once, so it needs a restart "
           f"and an offline install of 20 to 60 minutes with no desktop. Rivet runs it last, "
           f"downloads everything while you keep working, and asks before the restart. ")
    if rel.get("checked_repos"):
        if missing:
            why += (f"{len(missing)} of {rel['checked_repos']} enabled repos "
                    f"{'has' if len(missing) == 1 else 'have'} no Fedora {v} packages yet "
                    f"({', '.join(missing)}): they are disabled for the upgrade and their packages "
                    f"stay at the Fedora {cur} build until they catch up.")
        else:
            why += f"All {rel['checked_repos']} enabled repos already serve Fedora {v}."
    unknown = [r for r in rel.get("repos") or [] if r.get("status") == "unknown"]
    if unknown:
        why += (f" {len(unknown)} repo{'s' if len(unknown) > 1 else ''} could not be checked "
                f"({', '.join(_repo_label(r['id']) for r in unknown[:3])}).")
    return line, why


def notes(rel: dict) -> str:
    v = rel["version"]
    return (f"Fedora {v} is out. Upgrading replaces every installed package with its Fedora {v} "
            f"build, then restarts into an offline installer (text screen, 20 to 60 minutes). "
            f"Rivet applies the other updates first, downloads Fedora {v} in the background, "
            f"and only restarts when you click Restart now. The release notes are linked below.")


def item(rel: dict) -> dict:
    """The dnf-group row for a newer release (rules.apply_rules colours it)."""
    v, cur = rel["version"], rel.get("current")
    return {
        "id": f"release:{v}",
        "source": "dnf",
        "name": f"fedora-{v}",
        "display_name": f"Fedora {v}",
        "from": str(cur) if cur is not None else "",
        "to": str(v),
        "size_bytes": 0,                       # known only when the download starts
        "ref": f"system-upgrade:{v}",
        "advisory": None,
        "notes_excerpt": notes(rel),
        "notes_url": rel.get("notes_url") or NOTES_URL.format(v=v),
        "op": "release",
        "release": {
            "version": v,
            "current": cur,
            "missing_repos": list(rel.get("missing_repos") or []),
            "checked_repos": int(rel.get("checked_repos") or 0),
            "unknown_repos": [r["id"] for r in rel.get("repos") or [] if r.get("status") == "unknown"],
            "downloaded": bool(rel.get("downloaded")),
        },
    }
