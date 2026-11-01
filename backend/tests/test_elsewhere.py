#!/usr/bin/env python3
"""Elsewhere collectors (npm, pipx, uv, cargo, Home Assistant, podman, GNOME extensions,
AppImages) against canned tool output and canned HTTP answers.

    python3 backend/tests/test_elsewhere.py

Nothing real is asked: every subprocess and every HTTP GET goes through FakeCtx, which
fails the test on anything it was not told about. The last class runs rivet-collect
end to end with a fixture and checks that `elsewhere` lands in state.json without
touching the dnf/Flatpak groups or the counts the top-bar badge reads.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
# hermetic: know-cache.json, state.json and the "local usage" grep all live in scratch dirs
SCRATCH = Path(tempfile.mkdtemp(prefix="rivet-ew-test-"))
os.environ["RIVET_STATE_DIR"] = str(SCRATCH / "state")
(SCRATCH / "state").mkdir()
# NB under `unittest discover` another module may have imported common first with ITS state
# dir; patch_state writes to common.STATE_PATH, so write_state() uses that same path.

import elsewhere as E  # noqa: E402
import elsewhere_apply as EA  # noqa: E402
import know as K  # noqa: E402

K.HOME = SCRATCH / "home"          # enrich()'s default: an empty home, no ~/projects
K.load_local(BACKEND / "fixtures" / "know-local.json")   # never this box's git-ignored know.local.json
K.CACHE_PATH = SCRATCH / "state" / "know-cache.json"


class FakeCtx(E.Ctx):
    """cmds: {first-two-words-or-exe: (rc, stdout)}; http: {url-substring: json | Exception}."""

    def __init__(self, cmds=None, http=None, env=None, have=None):
        super().__init__(deadline=time.monotonic() + 10, env=env or {})
        self.cmds, self.http, self.have = cmds or {}, http or {}, have
        self.calls, self.urls = [], []

    def which(self, name, *fallbacks):
        if self.have is not None and name not in self.have:
            return None
        return f"/fake/{name}"

    def run(self, cmd, timeout=None, env=None):
        exe = os.path.basename(cmd[0])
        self.calls.append([exe] + cmd[1:])
        for key in (" ".join([exe] + cmd[1:3]), " ".join([exe] + cmd[1:2]), exe):
            if key in self.cmds:
                rc, out = self.cmds[key]
                return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    def http_json(self, url, *, headers=None, timeout=None, data=None):
        self.urls.append((url, headers or {}) if data is None else (url, headers or {}, data))
        for key, val in self.http.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unexpected GET {url}")


NPM_OUTDATED = json.dumps({
    "@openhands/agent-canvas": {"current": "1.4.0", "wanted": "1.16.0", "latest": "1.34.0",
                                "dependent": "global", "location": "/x"},
    # installed from a newer dist-tag than `latest`: must NOT be offered (would downgrade)
    "verdaccio": {"current": "2026.10.8", "wanted": "2026.9.2", "latest": "2026.9.2"},
    "pnpm": {"current": "9.15.4", "wanted": "12.16.1", "latest": "12.16.1"},
})
NPM_LS = json.dumps({"dependencies": {"@openhands/agent-canvas": {}, "verdaccio": {}, "pnpm": {},
                                      "corepack": {}}})


class Versions(unittest.TestCase):
    def test_severity_is_positional(self):
        self.assertEqual(E.severity("9.15.4", "12.16.1"), "major")
        self.assertEqual(E.severity("1.4.0", "1.34.0"), "minor")
        self.assertEqual(E.severity("2026.10.8", "2026.10.30"), "patch")
        self.assertEqual(E.severity("2026.10.3", "2026.10.5"), "patch")
        self.assertEqual(E.severity("18.3", "18.4"), "minor")
        self.assertEqual(E.severity("v5.15.7", "v5.18.0"), "minor")
        self.assertEqual(E.severity("v77", "v79"), "unknown")          # a counter, not semver
        self.assertEqual(E.severity("abc", "def"), "unknown")

    def test_is_newer(self):
        self.assertTrue(E.is_newer("1.4.0", "1.34.0"))                   # numeric, not string
        self.assertFalse(E.is_newer("2026.10.8", "2026.9.2"))
        self.assertFalse(E.is_newer("1.2", "1.2.0"))
        self.assertTrue(E.is_newer("1.2", "1.2.1"))


def make_home(layout: dict) -> Path:
    """{relative path: text or None (directory)} -> a scratch home."""
    h = Path(tempfile.mkdtemp(prefix="rivet-home-"))
    for rel, text in layout.items():
        p = h / rel
        if text is None:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return h


class NpmCtx(FakeCtx):
    """npm answers per --prefix: {prefix-suffix: (rc, outdated json, ls json)}."""

    def __init__(self, home, per_prefix, **kw):
        super().__init__(**kw)
        self.home, self.per = home, per_prefix
        self.envs = []

    def run(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
        if cmd[1:3] == ["prefix", "-g"]:
            return SimpleNamespace(returncode=0, stdout=str(self.home / ".npm-global") + "\n", stderr="")
        pfx = cmd[cmd.index("--prefix") + 1]
        rc, out, ls = next(v for k, v in self.per.items() if pfx.endswith(k))
        if cmd[1] == "outdated":
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout=ls, stderr="")

    def http_json(self, url, *, headers=None, timeout=None, data=None):
        self.urls.append((url, headers or {}))
        if "registry.npmjs.org" in url:
            return {"description": "Fast, disk space efficient package manager"} if "pnpm" in url else {}
        raise AssertionError(url)


NPM_HOME = {".npm-global/lib/node_modules": None, ".npm-canvas-new/lib/node_modules": None,
            ".nvm/versions/node/v24.15.0/lib/node_modules": None,
            ".nvm/versions/node/v24.15.0/bin/npm": "#!/bin/sh\n",
            ".nvm/versions/node/v26.12.1/lib/node_modules": None,
            ".nvm/versions/node/v26.12.1/bin/npm": "#!/bin/sh\n"}
GLOBAL_OUTDATED = json.dumps({
    "verdaccio": {"current": "2026.10.8", "wanted": "2026.9.2", "latest": "2026.9.2"},   # newer dist-tag
    "pnpm": {"current": "9.15.4", "wanted": "12.16.1", "latest": "12.16.1"}})
GLOBAL_LS = json.dumps({"dependencies": {"verdaccio": {}, "pnpm": {}}})
CANVAS_LS = json.dumps({"dependencies": {"@openhands/agent-canvas": {"version": "1.34.0"}}})
NVM_OUTDATED = json.dumps({"npm": {"current": "11.12.1", "latest": "12.2.1"},
                           "corepack": {"current": "0.34.6", "latest": "0.36.0"}})


class Npm(unittest.TestCase):
    def ctx(self, home, **over):
        per = {"/.npm-global": (1, GLOBAL_OUTDATED, GLOBAL_LS),
               "/.npm-canvas-new": (0, "{}", CANVAS_LS),
               "/v24.15.0": (1, NVM_OUTDATED, json.dumps({"dependencies": {"npm": {}, "corepack": {}}})),
               "/v26.12.1": (0, "{}", json.dumps({"dependencies": {"npm": {}}}))}
        per.update(over)
        return NpmCtx(home, per, env={})

    def test_every_prefix_is_scanned_and_labelled(self):
        """2026-10-31: the retired agent-canvas lived in ~/.npm-global, the live one in
        ~/.npm-canvas-new; both prefixes (and the nvm Node dirs) are scanned now."""
        home = make_home(NPM_HOME)
        ctx = self.ctx(home)
        r = E.collect_npm(ctx, home=home)
        self.assertEqual(r["status"], "ok")
        scanned = sorted({c[c.index("--prefix") + 1] for c in ctx.calls if "--prefix" in c})
        self.assertEqual([Path(p).name for p in scanned], [".npm-canvas-new", ".npm-global", "v24.15.0", "v26.12.1"])
        self.assertEqual(len(scanned), 4)                         # `npm prefix -g` deduped
        by = {i["name"]: i for i in r["items"]}
        self.assertEqual(sorted(by), ["pnpm"])                    # verdaccio downgrade, agent-canvas current, nvm bundled
        self.assertIn("bundled with an nvm Node", r["hint"])
        p = by["pnpm"]
        label = K.tilde(home / ".npm-global")          # "~/.npm-global" on the real box
        self.assertEqual((p["severity"], p["where"], p["prefix"]), ("major", label, str(home / ".npm-global")))
        self.assertEqual(p["npm"], str(home / ".nvm/versions/node/v26.12.1/bin/npm"))   # newest nvm npm, not /usr/bin/npm
        self.assertTrue(p["command"].endswith(f"i -g --prefix {label} pnpm@latest"))
        self.assertEqual(p["what"], "Fast, disk space efficient package manager.")
        self.assertTrue(all(e and e["PATH"].startswith(str(home / ".nvm/versions/node/v")) for e in ctx.envs))
        self.assertEqual(r["checked"], 6)

    def test_same_package_in_two_prefixes_gets_two_rows(self):
        home = make_home(NPM_HOME)
        stale = json.dumps({"@openhands/agent-canvas": {"current": "1.4.0", "latest": "1.34.0"}})
        ctx = self.ctx(home, **{"/.npm-global": (1, stale, "{}"), "/.npm-canvas-new": (1, stale, "{}")})
        ids = sorted(i["id"] for i in E.collect_npm(ctx, home=home)["items"])
        self.assertEqual(ids, ["npm:@openhands/agent-canvas",
                               "npm:@openhands/agent-canvas@" + K.tilde(home / ".npm-canvas-new")])

    def test_registry_error_is_failed_not_up_to_date(self):
        home = make_home({".npm-global/lib/node_modules": None})
        err = json.dumps({"error": {"code": "ENOTFOUND", "summary": "request to registry.npmjs.org failed"}})
        ctx = NpmCtx(home, {"/.npm-global": (1, err, "{}")}, env={"RIVET_NPM_BIN": "/fake/npm"})
        r = E.collect_npm(ctx, home=home)
        self.assertEqual(r["status"], "failed")
        self.assertIn("registry.npmjs.org", r["error"])

    def test_no_npm_is_absent(self):
        self.assertEqual(E.collect_npm(FakeCtx(have=set()), home=make_home({}))["status"], "absent")


class PyPITools(unittest.TestCase):
    PIPX = json.dumps({"venvs": {"httpie": {"metadata": {"main_package": {
        "package": "httpie", "package_version": "3.2.3"}}},
        "black": {"metadata": {"main_package": {"package": "black", "package_version": "25.9.0"}}}}})

    def test_pipx(self):
        ctx = FakeCtx({"pipx list": (0, self.PIPX)},
                      {"pypi/httpie/": {"info": {"version": "3.2.4"}},
                       "pypi/black/": {"info": {"version": "25.9.0"}}})
        r = E.collect_pipx(ctx)
        self.assertEqual((r["status"], r["checked"]), ("ok", 2))
        self.assertEqual([(i["name"], i["severity"], i["command"]) for i in r["items"]],
                         [("httpie", "patch", "pipx upgrade httpie")])

    def test_uv_parse_and_upgrade_command(self):
        out = "ruff v0.13.2\n- ruff\nwarning: something\nhttpx-cli v0.1.0\n- httpx\n"
        self.assertEqual(E.parse_uv_tool_list(out), {"ruff": "0.13.2", "httpx-cli": "0.1.0"})
        ctx = FakeCtx({"uv tool": (0, out)}, {"pypi/ruff/": {"info": {"version": "0.14.1"}},
                                             "pypi/httpx-cli/": {"info": {"version": "0.1.0"}}})
        r = E.collect_uv(ctx)
        self.assertEqual([(i["name"], i["severity"], i["command"]) for i in r["items"]],
                         [("ruff", "minor", "uv tool upgrade ruff")])

    def test_uv_with_nothing_installed(self):
        r = E.collect_uv(FakeCtx({"uv tool": (0, "No tools installed\n")}))
        self.assertEqual((r["status"], r["checked"], r["items"]), ("ok", 0, []))

    def test_pypi_unreachable_fails_the_source(self):
        ctx = FakeCtx({"pipx list": (0, self.PIPX)}, {"pypi.org": OSError("timed out")})
        r = E.collect_pipx(ctx)
        self.assertEqual(r["status"], "failed")
        self.assertIn("pypi.org", r["error"])


class Cargo(unittest.TestCase):
    LIST = ("tauri-driver v2.1.0:\n    tauri-driver\n"
            "mytool v0.1.0 (/home/s/src/mytool):\n    mytool\n"
            "ripgrep v14.1.0:\n    rg\n")

    def test_parse(self):
        reg, other = E.parse_cargo_list(self.LIST)
        self.assertEqual(reg, {"tauri-driver": "2.1.0", "ripgrep": "14.1.0"})
        self.assertEqual(other, ["mytool"])

    def test_crates_io_rows(self):
        ctx = FakeCtx({"cargo install": (0, self.LIST)},
                      {"crates/tauri-driver": {"crate": {"max_stable_version": "2.1.0"}},
                       "crates/ripgrep": {"crate": {"max_stable_version": "15.0.0"}}})
        r = E.collect_cargo(ctx)
        self.assertEqual([(i["name"], i["latest"], i["severity"]) for i in r["items"]],
                         [("ripgrep", "15.0.0", "major")])
        self.assertIn("mytool", r["hint"])
        self.assertEqual(r["checked"], 3)

    def test_crates_io_unreachable_is_failed(self):
        ctx = FakeCtx({"cargo install": (0, self.LIST)}, {"crates.io": OSError("timed out")})
        r = E.collect_cargo(ctx)
        self.assertEqual(r["status"], "failed")
        self.assertIn("crates.io", r["error"])


HA_STATES = [
    {"entity_id": "update.home_assistant_core_update", "state": "on",
     "attributes": {"title": "Home Assistant Core", "installed_version": "2026.10.3",
                    "latest_version": "2026.10.5",
                    "release_url": "https://www.home-assistant.io/changelogs/core-2026.10#2026105"}},
    {"entity_id": "update.zigbee2mqtt_update", "state": "on",
     "attributes": {"friendly_name": "Zigbee2MQTT Update", "title": "Zigbee2MQTT",
                    "installed_version": "2.6.1", "latest_version": "2.7.0"}},
    {"entity_id": "update.samba_share_update", "state": "off",
     "attributes": {"installed_version": "0.30.5", "latest_version": "0.30.5"}},
    {"entity_id": "sensor.cpu", "state": "12"},
]


class HomeAssistant(unittest.TestCase):
    def test_update_entities_that_are_on(self):
        ctx = FakeCtx(http={"/api/states": HA_STATES}, env={"HA_TOKEN": "t0k"})
        r = E.collect_ha(ctx)
        self.assertEqual((r["status"], r["checked"]), ("ok", 3))
        by = {i["name"]: i for i in r["items"]}
        self.assertEqual(sorted(by), ["Home Assistant Core", "Zigbee2MQTT"])
        self.assertEqual(by["Home Assistant Core"]["severity"], "patch")
        self.assertTrue(by["Home Assistant Core"]["link"].endswith("/config/updates"))  # Open in HA
        self.assertTrue(by["Zigbee2MQTT"]["link"].endswith("/config/updates"))   # no release_url
        self.assertIsNone(by["Zigbee2MQTT"]["command"])
        self.assertEqual(ctx.urls[0][1]["Authorization"], "Bearer t0k")
        self.assertTrue(ctx.urls[0][0].startswith(E.HA_URL_DEFAULT))

    def test_kinds_from_the_template_and_what_each_button_is(self):
        states = HA_STATES + [
            {"entity_id": "update.samba_share_update2", "state": "on", "attributes": {
                "title": "Samba share", "installed_version": "0.30.4", "latest_version": "0.30.5", "supported_features": 29}},
            {"entity_id": "update.matter_server_update", "state": "on", "attributes": {
                "title": "Matter Server", "installed_version": "8.4.0", "latest_version": "9.3.0", "supported_features": 29}},
            {"entity_id": "update.home_assistant_operating_system_update", "state": "on", "attributes": {
                "title": "Home Assistant Operating System", "installed_version": "18.3", "latest_version": "18.4",
                "supported_features": 27}},
            {"entity_id": "update.alexa_media_player_update", "state": "on", "attributes": {
                "title": None, "friendly_name": "Alexa Media Player Update", "installed_version": "v5.15.7",
                "latest_version": "v5.18.0", "supported_features": 23,
                "release_url": "https://github.com/alandtse/alexa_media_player/releases/v5.18.0"}},
            {"entity_id": "update.smart_plug_firmware", "state": "on", "attributes": {
                "title": None, "device_class": "firmware", "friendly_name": "Smart Plug Firmware",
                "installed_version": "1.0.08", "latest_version": "1.0.10", "supported_features": 0}},
        ]
        tpl = {"hassio": ["update.home_assistant_core_update", "update.samba_share_update2",
                          "update.matter_server_update", "update.home_assistant_operating_system_update"],
               "hacs": ["update.alexa_media_player_update"],
               "devices": {"update.alexa_media_player_update": ["alandtse", "integration", "Alexa Media Player", [["hacs", "1"]]],
                           "update.matter_server_update": ["Official apps", "Home Assistant App", "Matter Server",
                                                           [["hassio", "core_matter_server"]]],
                           "update.samba_share_update2": ["Community", "Home Assistant App", "Samba share", [["hassio", "core_samba"]]],
                           "update.smart_plug_firmware": ["Acme", "SP-100", "Smart Plug"]},
               "counts": {"matter": 0, "alexa_media": 34, "mqtt": 48, "wyoming": 2}}
        ctx = FakeCtx(http={"/api/states": states, "/api/template": tpl}, env={"HA_TOKEN": "t"})
        r = E.collect_ha(ctx)
        self.assertTrue(any(len(u) == 3 and "template" in u[2] for u in ctx.urls))   # one POST /api/template
        doc = E.collect(registry=[("ha", "Home Assistant", lambda c: r, 2, 0)])
        by = {i["name"]: i for i in doc["items"]}
        self.assertEqual({n: (i["kind"], i["action"]) for n, i in by.items()}, {
            "Home Assistant Core": ("core", "open"), "Home Assistant Operating System": ("os", "open"),
            "Zigbee2MQTT": ("other", "open"), "Samba share": ("addon", "ha_install"),
            "Matter Server": ("addon", "ha_install"), "Alexa Media Player": ("integration", "ha_install"),
            "Smart Plug Firmware": ("firmware", "open")})
        self.assertIn("Add-on on the HA VM", by["Samba share"]["used_by"])
        self.assertIn("nothing uses it yet (0 Matter entities)", by["Matter Server"]["used_by"])
        self.assertTrue(by["Matter Server"]["confirm"])                        # 8 -> 9 asks first
        self.assertTrue(by["Matter Server"]["backup"])
        self.assertIn("34 entities", by["Alexa Media Player"]["used_by"])
        self.assertIn("HA restart", by["Alexa Media Player"]["used_by"])
        self.assertIn("Acme", by["Smart Plug Firmware"]["used_by"])
        self.assertTrue(by["Matter Server"]["what"].startswith("The Matter controller"))   # know.STATIC
        # 2026-10-31 (c): the add-on's slug, its page via HA's my-link handler, "unused" for Remove?
        self.assertEqual(by["Matter Server"]["addon_slug"], "core_matter_server")
        self.assertEqual(by["Matter Server"]["addon_link"],
                         E.HA_URL_DEFAULT.rstrip("/") + "/_my_redirect/supervisor_addon?addon=core_matter_server")
        self.assertTrue(by["Matter Server"]["unused"])
        self.assertNotIn("unused", by["Samba share"])                            # backs nothing we count
        self.assertEqual(by["Samba share"]["addon_slug"], "core_samba")
        self.assertNotIn("addon_slug", by["Alexa Media Player"])               # an integration, not an add-on
        self.assertNotIn("unused", by["Alexa Media Player"])
        self.assertNotIn("addon_link", by["Home Assistant Operating System"])

    def test_template_failure_falls_back_to_the_attributes(self):
        st = [{"entity_id": "update.x_update", "state": "on", "attributes": {
            "title": "X Add-on", "installed_version": "1.0.0", "latest_version": "1.1.0", "supported_features": 29}}]
        ctx = FakeCtx(http={"/api/states": st, "/api/template": E.HttpError(400, "http://ha/api/template")},
                      env={"HA_TOKEN": "t"})
        self.assertEqual(E.collect_ha(ctx)["items"][0]["kind"], "addon")

    def test_token_and_url_from_ha_env(self):
        f = Path(tempfile.mkdtemp(prefix="rivet-haenv-")) / "ha.env"
        f.write_text("# Home Assistant for Rivet\nexport CF_API_TOKEN=abc\nHA_URL='http://ha.test:8123/'\n"
                     "export HA_TOKEN=\"ey.J0 k\"  # long-lived\n")
        ctx = FakeCtx(env={})
        ctx.ha_env_file = f
        self.assertEqual(E.ha_token(ctx), ("ey.J0 k", None))
        self.assertEqual(E.ha_base(ctx), "http://ha.test:8123")
        ctx.env = {"HA_TOKEN": "from-env"}             # the environment wins over the file
        self.assertEqual(E.ha_token(ctx), ("from-env", None))

    def test_a_test_env_never_reads_the_users_file(self):
        self.assertIsNone(E.Ctx(env={}).ha_env_file)
        self.assertEqual(E.Ctx().ha_env_file, E.HA_ENV_FILE)

    def test_no_token_anywhere_is_absent(self):
        # 2026-11-01 abuse pass: no token and no URL = Home Assistant is not set up for Rivet;
        # the source shows nothing (it used to be a CAN'T CHECK row about a private secrets template)
        ctx = FakeCtx(env={})
        self.assertEqual(E.collect_ha(ctx)["status"], "absent")

    def test_a_url_without_a_token_says_what_is_missing(self):
        ctx = FakeCtx(env={"HA_URL": "http://ha:8123"})
        r = E.collect_ha(ctx)
        self.assertEqual(r["status"], "cant_check")
        self.assertEqual(r["hint"], E.HA_NO_TOKEN)

    def test_unreachable_is_failed(self):
        ctx = FakeCtx(http={"/api/states": OSError("timed out")}, env={"HA_TOKEN": "x"})
        r = E.collect_ha(ctx)
        self.assertEqual(r["status"], "failed")
        self.assertIn("Could not reach Home Assistant", r["error"])

    def test_bad_token_is_failed(self):
        ctx = FakeCtx(http={"/api/states": E.HttpError(401, "http://ha/api/states")}, env={"HA_TOKEN": "x"})
        self.assertIn("refused the token", E.collect_ha(ctx)["error"])


class Podman(unittest.TestCase):
    """2026-11-01: container rows compare VERSIONS (running image vs the repo's newest tag), never
    digests; a digest-only change is `rebuilt` (no badge, no button); containers that hold data
    get the runbook (or the manual note), never Update."""
    D = {k: "sha256:" + k * 64 for k in "123456789abcdef"}
    # image -> (container, unit, local digest, registry digest, image env, repo tags)
    WORLD = {
        "docker.io/library/couchdb:3": ("obsidian-couchdb", "obsidian-couchdb.service", "1", "9",
                                        ["COUCHDB_VERSION=3.5.2.1"], ["3", "3.5", "3.5.2", "3.5.2.1", "3.5.3", "3.5.3-nouveau", "latest"]),
        "docker.io/library/postgres:16": ("postiz-temporal-postgres", "postiz-temporal-postgres.service", "2", "2",
                                          ["PG_MAJOR=16", "PG_VERSION=16.14-1.pgdg13+1"], ["16", "16.14", "17.11", "18.6", "19rc1", "18.6-alpine"]),
        "docker.io/library/redis:7.2": ("postiz-redis", "postiz-redis.service", "3", "a",
                                        ["REDIS_VERSION=7.2.14"], ["7", "7.2", "7.2.13", "7.2.14", "7.2.14-alpine"]),
        "docker.io/library/ghost:5-alpine": ("ghost-blog", None, "4", "4",
                                             ["GHOST_VERSION=5.130.6", "NODE_VERSION=20.20.0"], ["5-alpine", "5.130.6-alpine", "6.5.0-alpine", "6.5.0"]),
        "docker.io/library/nginx:1.27": ("web", "web.service", "5", "b",
                                         ["NGINX_VERSION=1.27.3"], ["1.27", "1.27.3", "1.27.4", "1.29.0-alpine"]),
        "docker.io/library/caddy:2.8.4": ("proxy", None, "6", "6", [], ["2.8.4", "2.9.1", "2.10.0-beta.1"]),
        "docker.io/library/memcached:1.6": ("cache", None, "7", "7", ["MEMCACHED_VERSION=1.6.38"], ["1.6.38"]),
    }

    def ctx(self, world=None, pulled=()):
        world = world or self.WORLD
        ps = [{"Names": [c], "Image": img, "ImageID": "id-" + img, "Labels": {"PODMAN_SYSTEMD_UNIT": u} if u else {}}
              for img, (c, u, *_rest) in world.items()]
        ps.append({"Names": ["archivebox"], "Image": "localhost/archivebox:0.7.1", "ImageID": "cc"})
        ctx = FakeCtx({"podman auto-update": (0, "[]"), "podman ps": (0, json.dumps(ps))})
        real_run = ctx.run
        D = self.D

        def run(cmd, timeout=None):
            exe = os.path.basename(cmd[0])
            if exe == "skopeo" and cmd[1] == "inspect":
                return SimpleNamespace(returncode=0, stdout=D[world[cmd[-1].split("docker://", 1)[1]][3]], stderr="")
            if exe == "skopeo" and cmd[1] == "list-tags":
                repo = cmd[-1].split("docker://", 1)[1]
                img = next(i for i in world if E.image_repo(i) == repo)
                return SimpleNamespace(returncode=0, stdout=json.dumps({"Repository": repo, "Tags": world[img][5]}), stderr="")
            if exe == "podman" and cmd[1:3] == ["image", "inspect"]:
                ref = cmd[3]
                img = ref[3:] if ref.startswith("id-") else ref
                _c, _u, loc, reg, env, _t = world[img]
                dig = D[reg] if (img in pulled and not ref.startswith("id-")) else D[loc]
                return SimpleNamespace(returncode=0, stdout=json.dumps([{
                    "RepoDigests": [E.image_repo(img) + "@" + dig], "Config": {"Env": env, "Labels": {}}}]), stderr="")
            return real_run(cmd, timeout)
        ctx.run = run
        return ctx

    def rows(self, **kw):
        r = E.collect_podman(self.ctx(**kw))
        return {i["name"].split("/")[-1]: E.decide_action(i) for i in r["items"]}

    # ---- the pure classification
    def test_running_version_from_tag_env_or_label(self):
        rv = E.running_version
        self.assertEqual(rv("docker.io/temporalio/auto-setup:1.28.1", {"Labels": {"org.opencontainers.image.version": "1.15.12"}}), "1.28.1")
        self.assertEqual(rv("docker.io/library/couchdb:3", {"Env": ["COUCHDB_VERSION=3.5.2.1"]}), "3.5.2.1")
        self.assertEqual(rv("docker.io/library/postgres:16", {"Env": ["PG_VERSION=16.14-1.pgdg13+1"]}), "16.14")
        self.assertEqual(rv("docker.io/library/ghost:5-alpine", {"Env": ["NODE_VERSION=20.1.0", "GHOST_VERSION=5.130.6"]}), "5.130.6")
        self.assertEqual(rv("ghcr.io/a/b:2", {"Labels": {"org.opencontainers.image.version": "2.4.1"}}), "2.4.1")
        self.assertEqual(rv("ghcr.io/a/b:2", {"Labels": {"org.opencontainers.image.version": "1.15.12"}}), "2",
                         "a base image's label that does not fit the tag is ignored")
        self.assertEqual(rv("docker.io/library/x", {}), "latest")
        self.assertEqual((E.image_tag("docker.io/library/x"), E.image_repo("localhost:5000/a/b:1.2")), ("latest", "localhost:5000/a/b"))

    def test_newest_tags_keeps_the_flavour_and_skips_candidates(self):
        nt = E.newest_tags
        self.assertEqual(nt(["16", "16.14", "16.15", "18.6", "19rc1", "18.6-alpine"], "16", "16.14"), ("18.6", "16.15"))
        self.assertEqual(nt(["17.10-alpine", "17.11-alpine", "18.6-alpine", "18.6", "17.11-alpine3.23"], "17-alpine", "17.10"),
                         ("18.6", "17.11"))
        self.assertEqual(nt(["1.28.1", "1.29.0", "latest"], "1.28.1", "1.28.1"), ("1.29.0", None), "a pinned tag has no track")
        self.assertEqual(nt(["2.10.0-beta.1", "2.9.1"], "2.8.4", "2.8.4"), ("2.9.1", None))
        self.assertEqual(nt([], "3", "3.5"), (None, None))

    def test_classify_tag_versus_digest(self):
        c = E.classify_container
        same = c("docker.io/library/redis:7.2", "7.2.14", ["7.2.13", "7.2.14"], digest_changed=True)
        self.assertEqual((same["status"], same["installed"], same["latest"]), ("rebuilt", "7.2.14", "7.2.14"))
        self.assertIsNone(c("docker.io/library/redis:7.2", "7.2.14", ["7.2.14"], digest_changed=False), "current: no row")
        up = c("docker.io/library/couchdb:3", "3.5.2.1", ["3.5.2.1", "3.5.3"], digest_changed=True)
        self.assertEqual((up["status"], up["installed"], up["latest"], up["pull_gets_it"]), ("ok", "3.5.2.1", "3.5.3", True))
        pinned = c("docker.io/temporalio/auto-setup:1.28.1", "1.28.1", ["1.28.1", "1.29.0"], digest_changed=False)
        self.assertEqual((pinned["status"], pinned["latest"], pinned["pull_gets_it"]), ("ok", "1.29.0", False))
        major = c("docker.io/library/postgres:16", "16.14", ["16.14", "18.6"], digest_changed=True)
        self.assertEqual((major["latest"], major["pull_gets_it"], major["rebuilt"]), ("18.6", False, True),
                         "out of the floating tag's track: a pull would not bring it")
        nover = c("docker.io/library/x:latest", "latest", ["1.0.0"], digest_changed=True)
        self.assertEqual(nover["status"], "rebuilt", "no version to compare: never a hash -> hash row")

    # ---- collect_podman end to end on a fake podman + skopeo
    def test_rows_compare_versions_never_digests(self):
        rows = self.rows()
        for name, it in rows.items():
            self.assertFalse(str(it["installed"]).startswith("sha256") or str(it["latest"]).startswith("sha256"), name)
        self.assertNotIn("archivebox:0.7.1", rows, "localhost/ images are not compared")
        self.assertNotIn("memcached:1.6", rows, "a pinned-current image with an unchanged digest has no row")

    def test_stateful_containers_get_the_runbook_or_the_manual_note(self):
        rows = self.rows()
        couch = rows["couchdb:3"]
        self.assertEqual((couch["status"], couch["installed"], couch["latest"], couch["severity"]), ("ok", "3.5.2.1", "3.5.3", "patch"))
        self.assertEqual((couch["action"], couch["stateful"], couch["command"]), ("runbook", True, None))
        self.assertEqual(couch["runbook"], "https://example.com/runbooks/obsidian-sync")
        self.assertEqual(couch["link"], "https://docs.couchdb.org/en/stable/whatsnew/index.html")
        pg = rows["postgres:16"]
        self.assertEqual((pg["installed"], pg["latest"], pg["severity"], pg["action"]), ("16.14", "18.6", "major", "runbook"))
        self.assertEqual(pg["runbook"], "https://example.com/runbooks/postiz", "postiz-* containers use the Postiz runbook")
        ghost = rows["ghost:5-alpine"]
        self.assertEqual((ghost["installed"], ghost["latest"], ghost["action"], ghost["manual"]),
                         ("5.130.6", "6.5.0", None, "Manual: holds data, upgrade by hand"))

    def test_digest_only_change_is_rebuilt_with_no_button(self):
        redis = self.rows()["redis:7.2"]
        self.assertEqual((redis["status"], redis["installed"], redis["action"], redis["severity"]), ("rebuilt", "7.2.14", None, "unknown"))
        self.assertEqual(redis["hint"], "Image rebuilt upstream (same version).")
        r = E.collect_podman(self.ctx())
        self.assertEqual(sum(1 for i in r["items"] if i["status"] == "ok"), 5, "rebuilt rows are not counted as updates")

    def test_only_a_stateless_container_with_a_newer_tag_gets_update(self):
        rows = self.rows()
        web = rows["nginx:1.27"]
        self.assertEqual((web["installed"], web["latest"], web["stateful"], web["action"], web["command"]),
                         ("1.27.3", "1.27.4", False, "update", "podman pull docker.io/library/nginx:1.27"))
        self.assertEqual(web["restart"], "systemctl --user restart web.service")
        caddy = rows["caddy:2.8.4"]
        self.assertEqual((caddy["latest"], caddy["action"], caddy.get("command")), ("2.9.1", "open", None))
        self.assertIn("Pinned to 2.8.4", caddy["hint"])
        self.assertEqual(caddy["link"], "https://hub.docker.com/_/caddy/tags")
        self.assertEqual([n for n, i in rows.items() if i["action"] == "update"], ["nginx:1.27"])

    def test_pulled_but_not_restarted_offers_the_restart(self):
        web = self.rows(pulled=("docker.io/library/nginx:1.27",))["nginx:1.27"]
        self.assertTrue(web["pulled"])
        self.assertEqual((web["action"], web["command"]), ("copy", "systemctl --user restart web.service"))
        redis = self.rows(pulled=("docker.io/library/redis:7.2",))["redis:7.2"]
        self.assertEqual((redis["status"], redis["action"]), ("rebuilt", None))
        self.assertIn("already pulled; restart postiz-redis", redis["hint"])

    def test_autoupdate_rows_stay(self):
        ctx = self.ctx(world={})
        ctx.cmds["podman auto-update"] = (0, json.dumps([{"Unit": "ghost.service", "Container": "ghost",
                                                          "Image": "docker.io/library/ghost:5-alpine", "Updated": "pending"}]))
        it = E.decide_action(E.collect_podman(ctx)["items"][0])
        self.assertEqual(it["action"], "copy")


class GnomeExtensions(unittest.TestCase):
    def test_versions_from_ego(self):
        d = Path(tempfile.mkdtemp(prefix="rivet-ext-"))
        for uuid, md in {"tilingshell@x": {"name": "Tiling Shell", "version": 77},
                         "ding@x": {"name": "DING", "version": 102},
                         "mine@example": {"name": "Mine"}}.items():   # home-made: no version
            (d / uuid).mkdir()
            (d / uuid / "metadata.json").write_text(json.dumps(md))
        ctx = FakeCtx({"gnome-extensions list": (0, "tilingshell@x\nding@x\nmine@example\n"),
                       "gnome-shell --version": (0, "GNOME Shell 50.6\n")},
                      {"uuid=tilingshell%40x": {"version": 79, "link": "/extension/7065/tiling-shell/"},
                       "uuid=ding%40x": {"version": 102, "link": "/extension/2087/ding/"}})
        r = E.collect_gnome_ext(ctx, ext_dir=d)
        self.assertEqual(r["checked"], 2)
        self.assertEqual([(i["name"], i["installed"], i["latest"], i["severity"]) for i in r["items"]],
                         [("Tiling Shell", "v77", "v79", "unknown")])
        self.assertEqual(r["items"][0]["link"], "https://extensions.gnome.org/extension/7065/tiling-shell/")
        self.assertTrue(all("shell_version=50" in u for u, _ in ctx.urls))


class AppImages(unittest.TestCase):
    def test_listed_as_cant_check(self):
        d = Path(tempfile.mkdtemp(prefix="rivet-appimg-"))
        (d / "Obsidian-1.9.12.AppImage").write_bytes(b"\x7fELF" + b"\0" * 64)
        (d / "Tool-v2.0.1-x86_64.AppImage").write_bytes(b"\x7fELF..gh-releases-zsync|me|tool|latest|*.zsync")
        r = E.collect_appimage(FakeCtx(), app_dir=d)
        self.assertEqual(r["status"], "cant_check")
        by = {i["name"]: i for i in r["items"]}
        self.assertEqual(by["Obsidian-1.9.12.AppImage"]["installed"], "1.9.12")
        self.assertIn("No update information", by["Obsidian-1.9.12.AppImage"]["hint"])
        self.assertIn("AppImageUpdate", by["Tool-v2.0.1-x86_64.AppImage"]["hint"])
        self.assertTrue(all(i["status"] == "cant_check" for i in r["items"]))

    def test_no_directory_is_absent(self):
        self.assertEqual(E.collect_appimage(FakeCtx(), app_dir=Path("/nonexistent/x"))["status"], "absent")


class Registry(unittest.TestCase):
    def test_failed_and_slow_collectors_never_read_as_current(self):
        def ok(ctx):
            return E.result(checked=1, items=[E.row("npm", "pnpm", "9.0.0", "12.0.0")])

        def boom(ctx):
            raise RuntimeError("parser broke")

        def slow(ctx):
            time.sleep(5)
            return E.result()

        def failed(ctx):
            return E.result("failed", error="Could not reach crates.io (timed out).")

        reg = [("npm", "npm globals", ok, 2, 0), ("pipx", "pipx", boom, 2, 0),
               ("uv", "uv tools", slow, 0.3, 0), ("cargo", "cargo", failed, 2, 0)]
        t0 = time.monotonic()
        doc = E.collect(registry=reg)
        self.assertLess(time.monotonic() - t0, 3.5)                    # slow one did not hold us up
        st = {s["id"]: s["status"] for s in doc["sources"]}
        self.assertEqual(st, {"npm": "ok", "pipx": "failed", "uv": "failed", "cargo": "failed"})
        self.assertEqual(doc["counts"], {"stale": 1, "failed": 3, "cant_check": 0})
        uv = next(s for s in doc["sources"] if s["id"] == "uv")
        self.assertIn("timed out", uv["error"])
        rows = {i["id"]: i for i in doc["items"]}
        self.assertEqual(rows["cargo:_source"]["status"], "failed")
        self.assertEqual(doc["items"][0]["id"], "npm:pnpm")             # stale first, failures after

    def test_min_interval_reuses_the_previous_result(self):
        calls = []

        def pod(ctx):
            calls.append(1)
            return E.result(checked=1, items=[E.row("podman", "img", "a", "b", sev="unknown")])

        reg = [("podman", "Containers", pod, 2, 3600)]
        first = E.collect(registry=reg)
        second = E.collect(first, registry=reg)
        self.assertEqual(len(calls), 1)
        self.assertEqual([i["id"] for i in second["items"]], ["podman:img"])
        self.assertTrue(second["sources"][0]["reused"])
        old = json.loads(json.dumps(first))
        del old["sources"][0]["v"]                 # a result from before the row facts existed
        E.collect(old, registry=reg)
        self.assertEqual(len(calls), 2)


class Know(unittest.TestCase):
    """What-it-is / used-by lines: static map, local grep, pnpm pin warning, claude -p cache."""

    def home(self):
        return make_home({
            "projects/notes/package.json": json.dumps({"name": "notes"}),
            "projects/notes/pnpm-lock.yaml": "lockfileVersion: '9.0'\n\nsettings:\n",
            "projects/notes/scripts/cloud-setup.sh": "command -v pnpm || npm install -g pnpm@9\n",
            "projects/notes/scripts/e2e.sh": "tauri-driver --port 4444 &\n",
            "projects/webapp/package.json": json.dumps({"packageManager": "pnpm@10.6.1"}),
            "projects/webapp/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "projects/rivet/package.json": json.dumps({"dependencies": {"tauri-driver": "1"}}),   # never counted
            "projects/blog/compose.yaml": "services:\n  ghost:\n    image: docker.io/library/ghost:5-alpine\n",
            ".config/systemd/user/verdaccio-registry.service": "ExecStart=%h/.npm-global/bin/verdaccio gateway\n",
            ".config/systemd/user/other.service": "ExecStart=/usr/bin/verdaccio-free thing verdaccio\n",
        })

    def test_static_local_and_pnpm_warning(self):
        home = self.home()
        items = [E.row("npm", "pnpm", "9.15.4", "12.16.1", where="~/.npm-global"),
                 E.row("npm", "verdaccio", "2026.10.8", "2026.10.9"),
                 E.row("cargo", "tauri-driver", "2.1.0", "2.2.0", what="Webdriver server for Tauri applications."),
                 E.row("podman", "docker.io/library/couchdb:3", "a", "b", sev="unknown",
                       units=["obsidian-couchdb.service"], containers=["obsidian-couchdb"]),
                 E.row("podman", "docker.io/library/ghost:5-alpine", "a", "b", sev="unknown", containers=["ghost-blog"]),
                 E.row("podman", "docker.io/library/redis:7.2", "a", "b", sev="unknown",
                       units=["postiz-redis.service"], containers=["postiz-redis"])]
        K.enrich(items, home=home, cache={})
        by = {i["name"]: i for i in items}
        p = by["pnpm"]
        self.assertEqual(p["what"], "JavaScript package manager, a faster npm.")
        self.assertEqual(p["used_by"], "Builds Notes (%s), Webapp (%s)" % (K.tilde(home / "projects/notes"),
                                                                          K.tilde(home / "projects/webapp")))
        self.assertIn("Major jump 9 → 12.", p["warning"])
        self.assertIn("Webapp pins 10.x", p["warning"])
        self.assertIn("Notes does not pin pnpm (lockfile format 9.0; its scripts install pnpm@9)", p["warning"])
        self.assertTrue(by["verdaccio"]["used_by"].startswith("verdaccio-registry.service"))
        self.assertIn("Notes", by["tauri-driver"]["used_by"])
        self.assertNotIn("Rivet", by["tauri-driver"]["used_by"])
        self.assertEqual(by["tauri-driver"]["what"], "WebDriver server for Tauri apps.")   # STATIC wins
        self.assertEqual(by["docker.io/library/couchdb:3"]["used_by"], "Obsidian LiveSync (obsidian-couchdb.service)")
        self.assertEqual(by["docker.io/library/ghost:5-alpine"]["used_by"],
                         "the blog (compose in %s)" % K.tilde(home / "projects/blog"))
        self.assertEqual(by["docker.io/library/redis:7.2"]["used_by"], "Postiz (postiz-redis.service)")
        self.assertTrue(all(i["ai"] == [] for i in items))

    def test_pinned_pnpm_says_so(self):
        _p, facts = K.pnpm_usage(make_home({"projects/notes/package.json": json.dumps({"packageManager": "pnpm@9.15.4"}),
                                            "projects/notes/pnpm-lock.yaml": "lockfileVersion: '9.0'\n"}))
        self.assertIn("Notes pins 9.x", K.pnpm_warning("9.15.4", "12.16.1", facts))
        self.assertIsNone(K.pnpm_warning("9.15.4", "9.16.0", facts))       # not a major jump

    def test_image_keys(self):
        self.assertEqual(K.image_key("docker.io/library/postgres:17-alpine"), "postgres")
        self.assertEqual(K.image_key("docker.io/temporalio/auto-setup:1.28.1"), "temporalio/auto-setup")
        self.assertEqual(K.image_key("ghcr.io/gitroomhq/postiz-app:latest"), "gitroomhq/postiz-app")
        self.assertEqual(K.image_key("localhost:5000/x/y"), "x/y")

    def test_claude_fills_the_gaps_once_and_is_tagged_ai(self):
        asked = []

        def ask(prompt, timeout):
            asked.append(prompt)
            return {"what": "A thing that does stuff.", "used_for": "home-lab dashboards"}
        cache = {}
        it = E.row("ha", "Mystery Add-on", "1.0.0", "1.1.0", kind="addon",
                   used_by="Add-on on the HA VM, not on this PC.")
        self.assertEqual(K.fill_ai([it], ask=ask, cache=cache), 1)
        self.assertEqual((it["what"], it["used_by"], it["ai"]),
                         ("A thing that does stuff.", "Add-on on the HA VM, not on this PC.", ["what"]))
        self.assertIn('"Mystery Add-on" (a Home Assistant add-on (app))', asked[0])
        again = E.row("ha", "Mystery Add-on", "1.0.0", "1.2.0", kind="addon")
        K.fill_ai([again], ask=ask, cache=cache)
        self.assertEqual(len(asked), 1)                                   # cached by name+kind
        self.assertEqual((again["what"], again["used_by"], again["ai"]),
                         ("A thing that does stuff.", "home-lab dashboards", ["used_by", "what"]))
        self.assertTrue(K.CACHE_PATH.is_file())

    def test_claude_failure_leaves_the_line_empty_and_waits(self):
        def ask(prompt, timeout):
            raise RuntimeError("You've hit your limit")
        cache = {}
        it = E.row("npm", "left-pad", "1.0.0", "1.3.0")
        self.assertEqual(K.fill_ai([it], ask=ask, cache=cache), 0)
        self.assertIsNone(it["what"])
        self.assertIn("failed_at", cache["npm:left-pad"])
        self.assertFalse(K.needs_ai(it, cache))                          # not retried every hour
        self.assertTrue(K.needs_ai(it, cache, now=time.time() + K.FAIL_RETRY_S + 1))

    def test_slow_claude_never_blocks_past_the_budget(self):
        def ask(prompt, timeout):
            time.sleep(0.4)
            return {"what": "x", "used_for": None}
        items = [E.row("npm", f"pkg{i}", "1.0.0", "1.1.0") for i in range(12)]
        t0 = time.monotonic()
        K.fill_ai(items, ask=ask, cache={}, budget=0.5)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(any(i["what"] is None for i in items))

    def test_ask_claude_parses_the_cli_envelope(self):
        env = {"result": 'Sure:\n{"what": "pnpm is a fast package manager", "used_for": "JavaScript builds."}'}
        fake = SimpleNamespace(returncode=0, stdout=json.dumps(env), stderr="")
        orig, orig_bin = K.subprocess.run, K.claude_bin
        K.subprocess.run = lambda *a, **k: fake
        K.claude_bin = lambda: "/usr/bin/claude"          # the CLI need not exist here (CI)
        try:
            self.assertEqual(K.ask_claude("p"), {"what": "Pnpm is a fast package manager.", "used_for": "JavaScript builds"})
            fake.stdout = json.dumps({"is_error": True, "result": "You've hit your 5-hour limit"})
            with self.assertRaises(RuntimeError):
                K.ask_claude("p")
        finally:
            K.subprocess.run, K.claude_bin = orig, orig_bin


class Actions(unittest.TestCase):
    def test_buttons_per_source(self):
        cases = [
            (E.row("npm", "pnpm", "9.0.0", "12.0.0", command="npm i -g pnpm@latest"), ("update", True)),
            (E.row("pipx", "httpie", "3.2.3", "3.2.4", command="pipx upgrade httpie"), ("update", False)),
            (E.row("uv", "ruff", "0.13.2", "0.14.1", command="uv tool upgrade ruff"), ("update", False)),
            (E.row("cargo", "ripgrep", "14.1.0", "15.0.0", command="cargo install ripgrep"), ("update", True)),
            (E.row("gnome-ext", "Tiling Shell", "v77", "v79", sev="unknown", link="https://e.g.o/x"), ("open", False)),
            (E.row("ha", "Home Assistant Core", "2026.10.3", "2026.10.5", link="http://ha/config/updates", kind="core"),
             ("open", False)),
            (E.row("ha", "Matter Server", "8.4.0", "9.3.0", kind="addon", installable=True), ("ha_install", True)),
            (E.row("appimage", "X.AppImage", "1.0", None, status="cant_check"), (None, False)),
            (E.row("cargo", "_source", None, None, status="failed", key="_source"), (None, False)),
        ]
        for it, want in cases:
            E.decide_action(it)
            self.assertEqual((it["action"], it["confirm"]), want, it["id"])


class FakeEm:
    def __init__(self):
        self.events = []

    def emit(self, step, status, pct=None, msg="", log=None, **extra):
        self.events.append(dict(step=step, status=status, pct=pct, msg=msg, log=log, **extra))

    def done(self, status, **kw):
        self.events.append(dict(step="done", status=status, **kw))

    @property
    def last(self):
        return self.events[-1]


def write_state(items):
    doc = {"schema": 1, "groups": [], "elsewhere": {"counts": {}, "sources": [{"id": "cargo", "stale": 1}],
                                                    "items": items}}
    E_path = EA.STATE_PATH
    E_path.parent.mkdir(parents=True, exist_ok=True)
    E_path.write_text(json.dumps(doc))
    return doc, E_path


class OtherUpdate(unittest.TestCase):
    """rivet-apply --other-update ID (elsewhere_apply.py). No real tool or HA is touched."""

    def test_refuses_what_has_no_update_button(self):
        core = E.decide_action(E.row("ha", "Home Assistant Core", "1.0.0", "1.1.0", kind="core",
                                     link="http://ha/config/updates", entity_id="update.home_assistant_core_update"))
        em = FakeEm()
        self.assertEqual(EA.run(em, core["id"], {"elsewhere": {"items": [core]}}), 2)
        self.assertEqual(em.last["other"]["result"], "refused")
        em = FakeEm()
        EA.run(em, "npm:nope", {"elsewhere": {"items": []}})
        self.assertIn("not in the current list", em.last["summary"])
        odd = E.decide_action(E.row("npm", "--global-style", "1.0.0", "2.0.0", command="x"))
        with self.assertRaises(EA.Refused):
            EA.build_argv(odd)
        firm = E.row("ha", "Smart Plug", "1", "2", kind="firmware", action="ha_install")
        with self.assertRaises(EA.Refused):
            EA.run_ha(FakeEm(), firm)

    def test_cargo_update_streams_patches_state_and_reports_the_new_version(self):
        it = E.decide_action(E.row("cargo", "ripgrep", "14.1.0", "15.0.0", command="cargo install ripgrep"))
        _doc, path = write_state([it])
        seen = []

        def stream(argv, em, step, timeout):
            seen.append(argv)
            em.emit(step, "progress", None, "Compiling ripgrep v15.0.0", "Compiling ripgrep v15.0.0")
            return 0, "Installed package `ripgrep v15.0.0`"
        orig = EA.build_argv
        EA.build_argv = lambda it: (["/fake/cargo", "install", "ripgrep"], None)
        try:
            em = FakeEm()
            rc = EA.run_local(em, it, stream=stream, now_fn=lambda it: "15.0.0")
        finally:
            EA.build_argv = orig
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [["/fake/cargo", "install", "ripgrep"]])
        self.assertEqual(em.events[0]["log"], "$ /fake/cargo install ripgrep")
        self.assertEqual((em.last["status"], em.last["other"]["result"]), ("ok", "updated"))
        row = json.loads(path.read_text())["elsewhere"]["items"][0]
        self.assertEqual((row["status"], row["installed"], row["action"]), ("updated", "15.0.0", None))
        self.assertEqual(json.loads(path.read_text())["elsewhere"]["counts"]["stale"], 0)

    def test_a_failing_command_is_a_failure_and_the_row_stays(self):
        it = E.decide_action(E.row("pipx", "httpie", "3.2.3", "3.2.4", command="pipx upgrade httpie"))
        _doc, path = write_state([it])
        orig = EA.build_argv
        EA.build_argv = lambda it: (["/fake/pipx", "upgrade", "httpie"], None)
        try:
            em = FakeEm()
            rc = EA.run_local(em, it, stream=lambda *a, **k: (1, "No apps associated with package httpie"))
        finally:
            EA.build_argv = orig
        self.assertEqual((rc, em.last["status"]), (1, "fail"))
        self.assertIn("No apps associated", em.last["cause"])
        row = json.loads(path.read_text())["elsewhere"]["items"][0]
        self.assertEqual((row["status"], row["last_run"]["result"]), ("ok", "failed"))

    def test_npm_end_to_end_with_a_fake_npm(self):
        """The real rivet_apply.py, a fake `npm` on disk that 'installs' into a scratch prefix."""
        prefix = Path(tempfile.mkdtemp(prefix="rivet-npm-")) / "pfx"
        (prefix / "lib/node_modules/is-odd").mkdir(parents=True)
        (prefix / "lib/node_modules/is-odd/package.json").write_text('{"version": "2.0.0"}')
        npm = prefix.parent / "npm"
        npm.write_text("#!/usr/bin/env python3\nimport sys, json, os\n"
                       "a = sys.argv[1:]; p = a[a.index('--prefix') + 1]\n"
                       "print('added 1 package in 0.4s', flush=True)\n"
                       "open(os.path.join(p, 'lib/node_modules/is-odd/package.json'), 'w').write(json.dumps({'version': '3.0.0'}))\n")
        npm.chmod(0o755)
        it = E.decide_action(E.row("npm", "is-odd", "2.0.0", "3.0.0", command="npm i -g is-odd@latest",
                                   prefix=str(prefix), npm=str(npm), where="pfx"))
        state_dir = Path(tempfile.mkdtemp(prefix="rivet-ew-st-"))
        (state_dir / "state.json").write_text(json.dumps({"schema": 1, "groups": [], "elsewhere": {"items": [it]}}))
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_apply.py"), "--other-update", "npm:is-odd"],
                            env=dict(os.environ, RIVET_STATE_DIR=str(state_dir)), capture_output=True,
                            text=True, timeout=60)
        evs = [json.loads(l) for l in cp.stdout.splitlines() if l.startswith("{")]
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(evs[0]["log"], f"$ {K.tilde(npm)} i -g --prefix {K.tilde(prefix)} is-odd@latest")
        self.assertIn("added 1 package in 0.4s", [e.get("msg") for e in evs])
        self.assertEqual((evs[-1]["step"], evs[-1]["status"], evs[-1]["other"]["result"]), ("done", "ok", "updated"))
        self.assertEqual(json.loads((state_dir / "state.json").read_text())["elsewhere"]["items"][0]["installed"], "3.0.0")

    def test_podman_pull_says_what_to_restart(self):
        it = E.decide_action(E.row("podman", "docker.io/library/redis:7.2", "sha256:aa", "sha256:bb", sev="unknown",
                                   command="podman pull docker.io/library/redis:7.2", image="docker.io/library/redis:7.2",
                                   units=["postiz-redis.service"], containers=["postiz-redis"],
                                   restart="systemctl --user restart postiz-redis.service"))
        write_state([it])
        orig = EA.build_argv
        EA.build_argv = lambda it: (["/fake/podman", "pull", "docker.io/library/redis:7.2"], None)
        try:
            em = FakeEm()
            EA.run_local(em, it, stream=lambda *a, **k: (0, "Writing manifest"), now_fn=lambda it: "sha256:bb")
        finally:
            EA.build_argv = orig
        self.assertEqual(em.last["other"]["result"], "pulled")
        self.assertEqual(em.last["other"]["restart"], "systemctl --user restart postiz-redis.service")
        row = em.last["other"]["item"]
        self.assertEqual((row["action"], row["command"]), ("copy", "systemctl --user restart postiz-redis.service"))

    def test_ha_install_posts_then_polls_until_done(self):
        it = E.decide_action(E.row("ha", "Matter Server", "8.4.0", "9.3.0", kind="addon", installable=True,
                                   backup=True, entity_id="update.matter_server_update"))
        write_state([it])
        polls = iter([{"state": "on", "attributes": {"in_progress": False, "installed_version": "8.4.0",
                                                     "latest_version": "9.3.0", "supported_features": 9}},   # the look before
                      {"state": "on", "attributes": {"in_progress": True, "update_percentage": 40,
                                                     "installed_version": "8.4.0", "latest_version": "9.3.0"}},
                      {"state": "on", "attributes": {"in_progress": True, "installed_version": "8.4.0",
                                                     "latest_version": "9.3.0"}},
                      {"state": "off", "attributes": {"in_progress": False, "installed_version": "9.3.0",
                                                      "latest_version": "9.3.0"}}])

        class HaCtx(FakeCtx):
            def http_json(self, url, *, headers=None, timeout=None, data=None):
                self.urls.append((url, data))
                if url.endswith("/api/services/update/install"):
                    return []
                return next(polls)
        ctx = HaCtx(env={"HA_TOKEN": "t"})
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=ctx, sleep=lambda s: None), 0)
        self.assertEqual(ctx.urls[0], (E.HA_URL_DEFAULT + "/api/states/update.matter_server_update", None))
        self.assertEqual(ctx.urls[1], (E.HA_URL_DEFAULT + "/api/services/update/install",
                                       {"entity_id": "update.matter_server_update", "backup": True}))   # no SPECIFIC_VERSION
        self.assertEqual(em.last["other"]["result"], "installed")
        self.assertEqual(em.last["other"]["item"]["installed"], "9.3.0")
        self.assertIn(40, [e.get("pct") for e in em.events])

    def test_ha_that_never_installs_is_a_failure(self):
        it = E.decide_action(E.row("ha", "Samba share", "0.30.4", "0.30.5", kind="addon", installable=True,
                                   entity_id="update.samba_share_update"))
        write_state([it])

        class HaCtx(FakeCtx):
            def http_json(self, url, *, headers=None, timeout=None, data=None):
                if data is not None:
                    raise E.HttpError(400, url)
                raise AssertionError("no polling after a refusal")
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=HaCtx(env={"HA_TOKEN": "t"}), sleep=lambda s: None), 1)
        self.assertIn("refused the install", em.last["cause"])

        class Stuck(FakeCtx):
            def http_json(self, url, *, headers=None, timeout=None, data=None):
                return [] if data is not None else {"state": "on", "attributes": {
                    "in_progress": False, "installed_version": "0.30.4", "latest_version": "0.30.5"}}
        em = FakeEm()
        self.assertEqual(EA.run_ha(em, it, ctx=Stuck(env={"HA_TOKEN": "t"}), sleep=lambda s: None), 1)
        self.assertIn("stopped without installing", em.last["cause"])


def matter_row(**kw):
    it = E.decide_action(E.row("ha", "Matter Server", "8.4.0", "9.3.0", kind="addon", installable=True,
                               backup=True, entity_id="update.matter_server_update",
                               addon_slug="core_matter_server",
                               addon_link="http://ha/_my_redirect/supervisor_addon?addon=core_matter_server",
                               unused=True))
    it.update(kw)
    return it


class FakeWs:
    """HA's websocket `supervisor/api`: info answers from `infos` in turn (an Exception is raised)."""

    def __init__(self, infos, uninstall=None):
        self.infos, self.uninstall, self.calls, self.closed = list(infos), uninstall, [], False

    def supervisor(self, endpoint, method="get", data=None, timeout=30):
        self.calls.append((method, endpoint, data))
        if endpoint.endswith("/uninstall"):
            if isinstance(self.uninstall, Exception):
                raise self.uninstall
            return {}
        v = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        self.closed = True


class RemoveCtx(FakeCtx):
    def __init__(self, ws, entities=0, **kw):
        super().__init__(env={"HA_TOKEN": "t"}, **kw)
        self.ws, self.entities = ws, entities

    def http_json(self, url, *, headers=None, timeout=None, data=None):
        self.urls.append((url, data))
        assert url.endswith("/api/template"), url
        if isinstance(self.entities, Exception):
            raise self.entities
        return {"n": self.entities}

    def ha_ws(self, base, token, timeout=15):
        return self.ws


INSTALLED = {"slug": "core_matter_server", "state": "started", "version": "8.4.0"}
GONE = E.HaWsError("App core_matter_server is not installed", "unknown_error")


class OtherRemove(unittest.TestCase):
    """rivet-apply --other-remove ID: uninstall an unused HA add-on. No real HA is touched."""

    def test_refuses_anything_but_an_unused_add_on_with_a_slug(self):
        cases = [
            (matter_row(unused=None), "not report it as unused"),
            (matter_row(unused=False), "not report it as unused"),
            (matter_row(unused="yes"), "not report it as unused"),     # only a real true counts
            (matter_row(used_by="nothing uses it yet (0 Matter entities)", unused=None), "not report it as unused"),
            (matter_row(addon_slug=None), "slug is unknown"),
            (matter_row(addon_slug="../core/x"), "slug is unknown"),
            (matter_row(kind="integration"), "only Home Assistant add-ons"),
            (matter_row(kind="core"), "only Home Assistant add-ons"),
            (dict(matter_row(), source="npm"), "only Home Assistant add-ons"),
            (dict(matter_row(), name="Some Add-on"), "cannot tell what uses it"),
        ]
        for it, want in cases:
            ws = FakeWs([INSTALLED])
            em = FakeEm()
            self.assertEqual(EA.run(em, it["id"], {"elsewhere": {"items": [it]}}, remove=True), 2, want)
            self.assertIn(want, em.last["summary"])
            self.assertEqual((em.last["status"], em.last["other"]["result"]), ("fail", "refused"))
            self.assertEqual(ws.calls, [], "a refusal never reaches HA")
        em = FakeEm()
        EA.run(em, "ha:update.gone", {"elsewhere": {"items": []}}, remove=True)
        self.assertIn("not in the current list", em.last["summary"])

    def test_uninstalls_then_polls_until_the_supervisor_says_not_installed(self):
        it = matter_row()
        keep = E.decide_action(E.row("ha", "Samba share", "0.30.4", "0.30.5", kind="addon",
                                     entity_id="update.samba_share_update"))
        _doc, path = write_state([it, keep])
        ws = FakeWs([INSTALLED, INSTALLED, INSTALLED, GONE])     # info, then 2 polls still there, then 404
        ctx = RemoveCtx(ws)
        em = FakeEm()
        self.assertEqual(EA.run_remove(em, it, ctx=ctx, sleep=lambda s: None), 0)
        self.assertIn("integration_entities('matter')", ctx.urls[0][1]["template"])
        self.assertIn(("post", "/addons/core_matter_server/uninstall", {"remove_config": False}), ws.calls)
        self.assertEqual(ws.calls[0], ("get", "/addons/core_matter_server/info", None))
        self.assertEqual(sum(1 for c in ws.calls if c[1].endswith("/info")), 4)
        self.assertTrue(ws.closed)
        self.assertEqual((em.last["status"], em.last["other"]["result"], em.last["other"]["item"]),
                         ("ok", "removed", None))
        self.assertIn("uninstalled from Home Assistant", em.last["summary"])
        self.assertTrue(any("supervisor/api POST /addons/core_matter_server/uninstall" in (e.get("log") or "")
                            for e in em.events), "the exact call is logged")
        ew = json.loads(path.read_text())["elsewhere"]
        self.assertEqual([i["id"] for i in ew["items"]], [keep["id"]], "the row is gone from state.json")
        self.assertEqual(ew["counts"]["stale"], 1)

    def test_a_store_entry_with_no_version_counts_as_removed(self):
        it = matter_row()
        write_state([it])
        ws = FakeWs([INSTALLED, {"slug": "core_matter_server", "state": "unknown", "version": None}])
        em = FakeEm()
        self.assertEqual(EA.run_remove(em, it, ctx=RemoveCtx(ws), sleep=lambda s: None), 0)
        self.assertEqual(em.last["other"]["result"], "removed")

    def test_something_uses_it_now_or_the_count_is_unknown_removes_nothing(self):
        for entities, want in ((3, "has 3 entities now"), (1, "has 1 entity now"),
                               (E.HttpError(500, "http://ha/api/template"), "could not confirm")):
            it = matter_row()
            _doc, path = write_state([it])
            ws = FakeWs([INSTALLED])
            em = FakeEm()
            self.assertEqual(EA.run_remove(em, it, ctx=RemoveCtx(ws, entities=entities), sleep=lambda s: None), 1)
            self.assertIn(want, em.last["cause"])
            self.assertEqual(ws.calls, [], "no Supervisor call at all")
            self.assertEqual(em.last["other"]["link"], it["addon_link"], "the UI keeps an Open in HA fallback")
            row = json.loads(path.read_text())["elsewhere"]["items"][0]
            self.assertEqual((row["id"], row["last_run"]["result"]), (it["id"], "remove_failed"))

    def test_supervisor_refusal_is_a_failure_and_the_row_stays(self):
        it = matter_row()
        _doc, path = write_state([it])
        ws = FakeWs([INSTALLED], uninstall=E.HaWsError("App is in use by the Matter integration", "unknown_error"))
        em = FakeEm()
        self.assertEqual(EA.run_remove(em, it, ctx=RemoveCtx(ws), sleep=lambda s: None), 1)
        self.assertIn("refused to uninstall it (App is in use", em.last["cause"])
        self.assertEqual(len(json.loads(path.read_text())["elsewhere"]["items"]), 1)

    def test_already_gone_is_removed_without_an_uninstall_call(self):
        it = matter_row()
        write_state([it])
        ws = FakeWs([GONE])
        em = FakeEm()
        self.assertEqual(EA.run_remove(em, it, ctx=RemoveCtx(ws), sleep=lambda s: None), 0)
        self.assertFalse(any(c[1].endswith("/uninstall") for c in ws.calls))
        self.assertIn("already uninstalled", em.last["summary"])

    def test_still_listed_after_the_cap_is_a_failure(self):
        it = matter_row()
        write_state([it])
        old = EA.REMOVE_TIMEOUT_S
        EA.REMOVE_TIMEOUT_S = 0.05
        try:
            em = FakeEm()
            rc = EA.run_remove(em, it, ctx=RemoveCtx(FakeWs([INSTALLED])), sleep=lambda s: time.sleep(0.02))
        finally:
            EA.REMOVE_TIMEOUT_S = old
        self.assertEqual(rc, 1)
        self.assertIn("still lists core_matter_server", em.last["cause"])

    def test_the_cli_flag_reaches_run_remove(self):
        it = matter_row(unused=None)
        state_dir = Path(tempfile.mkdtemp(prefix="rivet-ew-rm-"))
        (state_dir / "state.json").write_text(json.dumps({"schema": 1, "groups": [], "elsewhere": {"items": [it]}}))
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_apply.py"), "--other-remove", it["id"]],
                            env=dict(os.environ, RIVET_STATE_DIR=str(state_dir), HA_TOKEN="t",
                                     HA_URL="http://127.0.0.1:9"), capture_output=True, text=True, timeout=60)
        evs = [json.loads(l) for l in cp.stdout.splitlines() if l.startswith("{")]
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertEqual(evs[-1]["other"]["result"], "refused")
        self.assertIn("not report it as unused", evs[-1]["summary"])


class CollectEndToEnd(unittest.TestCase):
    """rivet-collect --fixture: elsewhere comes from the fixture, lands top-level, and the
    badge inputs (summary.total, flags, groups) are exactly what they were without it."""

    def run_collect(self, fixture: dict) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="rivet-ew-"))
        (tmp / "fx.json").write_text(json.dumps(fixture))
        env = dict(os.environ, RIVET_STATE_DIR=str(tmp / "state"), RIVET_CONFIG_DIR=str(tmp / "cfg"),
                   RIVET_NO_NOTIFY="1")
        cp = subprocess.run([sys.executable, str(BACKEND / "rivet_collect.py"), "--no-llm", "--quiet",
                             "--fixture", str(tmp / "fx.json")], env=env, capture_output=True,
                            text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads((tmp / "state" / "state.json").read_text())

    def test_elsewhere_is_additive(self):
        ew = E.collect(registry=[("npm", "npm globals",
                                  lambda c: E.result(checked=1, items=[E.row("npm", "pnpm", "9.0.0", "12.0.0")]),
                                  2, 0)])
        base = self.run_collect({"dnf": [], "flatpak": [], "fwupd": []})
        with_ew = self.run_collect({"dnf": [], "flatpak": [], "fwupd": [], "elsewhere": ew})
        self.assertNotIn("elsewhere", base)
        self.assertEqual(with_ew["elsewhere"]["counts"]["stale"], 1)
        self.assertEqual(with_ew["elsewhere"]["items"][0]["name"], "pnpm")
        for k in ("summary", "flags", "groups"):
            self.assertEqual(with_ew[k], base[k], k)
        self.assertNotIn("elsewhere", [g["id"] for g in with_ew["groups"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
