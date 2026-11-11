"""A fake Home Assistant on 127.0.0.1 for the HA core feature (2026-11-10): backend tests and app QA.

It answers the read-only calls the core check makes (/api/config, config entries, the update
entity) and plays one install story, really going away (the listening socket is closed, so a
client gets "connection refused") and coming back, like a core update does:

  ok        install accepted -> in progress for a moment -> down -> 502 once -> STARTING -> RUNNING
            on the target version
  rollback  the same, but it comes back on the OLD version (the Supervisor rolled back)
  never     it goes down and never comes back
  invalid   check_config says the configuration is invalid (nothing may be installed)
  nobackup  like ok, but the entity has no BACKUP feature
  idle      install accepted, nothing ever happens (never goes down, never moves)

The same server stands in for GitHub's releases API (/gh/<tag>) and the release posts (/blog/...)
from backend/fixtures/ha_core/, so ha_core.fetch_notes reads canned text over real HTTP.
Point Rivet at it with HA_URL / HA_TOKEN, RIVET_HA_RELEASES_API=<url>/gh/, RIVET_HA_SITE=<url>.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent / "ha_core"
TOKEN = "fake-token"
# made-up but realistic: what a small Home Assistant loads (platform entries like "mqtt.sensor")
COMPONENTS = ["homeassistant", "http", "api", "frontend", "automation", "script", "template", "recorder",
              "backup", "sun", "met", "zha", "sensor", "mqtt", "mqtt.sensor", "light", "zha.light"]
COMPONENTS_NONE = ["homeassistant", "http", "api", "frontend", "recorder", "backup", "sun", "met", "zha",
                   "sensor", "light", "zha.light"]
POSTS = {"2026.11": "/blog/2026/11/04/release-202611/", "2026.10": "/blog/2026/10/07/release-202610/"}
PATCH_NOTES = {
    "2026.11.1": ["- Fix MQTT discovery of lights ([@a] - [#10]) ([mqtt docs])",
                  "- Bump zigpy to 1.2.3 ([@b] - [#11]) ([zha docs]) (dependency)"],
    "2026.11.2": ["- Remove the deprecated `household` option of Sonos ([@c] - [#12]) ([sonos docs]) (breaking-change)",
                  "- Fix Met.no rounding ([@d] - [#13]) ([met docs])"],
    "2026.10.1": ["- Fix ZHA groups ([@e] - [#14]) ([zha docs])"],
}


class FakeHA:
    def __init__(self, story: str = "ok", *, installed: str = "2026.10.4", target: str = "2026.11.1",
                 components: list[str] | None = None, down_s: float = 0.8, progress_polls: int = 1,
                 features: int | None = None):
        self.story, self.installed, self.target = story, installed, target
        self.version = installed
        self.components = list(COMPONENTS if components is None else components)
        self.down_s, self.progress_left = down_s, progress_polls
        self.features = features if features is not None else (23 if story == "nobackup" else 31)
        self.phase = "up"                 # up | progress | down | bad_gateway | starting
        self.requests: list[tuple[str, str, object]] = []
        self.port = 0
        self.srv = None
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeHA":
        self._serve()
        return self

    def _serve(self) -> None:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def _down(self) -> None:
        srv, self.srv = self.srv, None
        if srv:
            srv.shutdown()
            srv.server_close()          # the port is closed now: connection refused

    def stop(self) -> None:
        self._down()

    def _go_down_then_back(self) -> None:
        time.sleep(0.15)                 # the install call has its answer (or its dropped connection)
        with self.lock:
            self.phase = "down"
        self._down()
        if self.story == "never":
            return
        time.sleep(self.down_s)
        with self.lock:
            self.phase = "bad_gateway"
            self.version = self.installed if self.story == "rollback" else self.target
        self._serve()

    # ---------------------------------------------------------------- answers
    def entity(self) -> dict:
        with self.lock:
            busy = self.phase == "progress"
            return {"entity_id": "update.home_assistant_core_update", "state": "on" if self.version != self.target else "off",
                    "attributes": {"title": "Home Assistant Core", "installed_version": self.version,
                                   "latest_version": self.target, "in_progress": busy,
                                   "update_percentage": 40 if busy else None,
                                   "supported_features": self.features,
                                   "release_url": "https://www.home-assistant.io/latest-release-notes/"}}

    def _json(self, h, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _text(self, h, code: int, text: str, ctype: str = "text/html") -> None:
        body = text.encode()
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h, method: str) -> None:
        path = h.path.split("?", 1)[0]
        data = None
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            raw = h.rfile.read(n) if n else b""
            try:
                data = json.loads(raw or b"null")
            except ValueError:
                data = raw.decode("utf-8", "replace")
        self.requests.append((method, path, data))
        # canned notes: GitHub releases and the release posts (no token needed)
        if path.startswith("/gh/"):
            return self._gh(h, path[len("/gh/"):])
        if path.startswith("/blog/"):
            month = next((m for m, p in POSTS.items() if p == path), None)
            f = HERE / f"blog-{month.replace('.', '')}.html" if month else None
            if f and f.is_file():
                return self._text(h, 200, f.read_text())
            return self._text(h, 404, "not found")
        if h.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._json(h, 401, {"message": "unauthorized"})
        with self.lock:
            phase = self.phase
        if phase == "bad_gateway":
            with self.lock:
                self.phase = "starting"
            return self._json(h, 502, {"message": "Bad Gateway"})
        if path == "/api/config":
            with self.lock:
                state = "STARTING" if self.phase == "starting" else "RUNNING"
                if self.phase == "starting":
                    self.phase = "up"
                if self.phase == "progress":
                    self.progress_left -= 1
                    if self.progress_left < 0:
                        threading.Thread(target=self._go_down_then_back, daemon=True).start()
                        self.progress_left = 10 ** 6
            return self._json(h, 200, {"version": self.version, "state": state, "components": self.components,
                                       "safe_mode": False, "recovery_mode": False})
        if path == "/api/config/config_entries/entry":
            doms = sorted({c.split(".")[0] for c in self.components if c not in ("homeassistant", "http", "api")})
            return self._json(h, 200, [{"entry_id": str(i), "domain": d, "state": "loaded"} for i, d in enumerate(doms)])
        if path == "/api/states/update.home_assistant_core_update":
            return self._json(h, 200, self.entity())
        if path == "/api/states":
            return self._json(h, 200, [self.entity(), {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}}])
        if path == "/api/config/core/check_config" and method == "POST":
            if self.story == "invalid":
                return self._json(h, 200, {"result": "invalid", "errors": "Integration error: mqtt - Integration 'mqtt' not found."})
            return self._json(h, 200, {"result": "valid", "errors": None})
        if path == "/api/services/update/install" and method == "POST":
            if self.story == "idle":
                return self._json(h, 200, [])
            with self.lock:
                self.phase = "progress"
            if self.progress_left <= 0:
                threading.Thread(target=self._go_down_then_back, daemon=True).start()
            return self._json(h, 200, [])
        return self._json(h, 404, {"message": f"no fake for {method} {path}"})

    def _gh(self, h, tag: str) -> None:
        m = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d+)", tag)
        if not m:
            return self._json(h, 404, {"message": "Not Found"})
        month = f"{m.group(1)}.{int(m.group(2))}"
        if m.group(3) == "0":
            if month not in POSTS:
                return self._json(h, 404, {"message": "Not Found"})
            return self._json(h, 200, {"tag_name": tag, "html_url": f"https://github.com/home-assistant/core/releases/tag/{tag}",
                                       "body": "https://www.home-assistant.io" + POSTS[month]})
        lines = PATCH_NOTES.get(tag)
        if lines is None:
            return self._json(h, 404, {"message": "Not Found"})
        return self._json(h, 200, {"tag_name": tag, "html_url": f"https://github.com/home-assistant/core/releases/tag/{tag}",
                                   "body": "\n".join(lines)})

    # ---------------------------------------------------------------- for assertions
    def calls(self, method: str | None = None, path: str | None = None) -> list:
        return [r for r in self.requests if (method is None or r[0] == method) and (path is None or r[1] == path)]

    def env(self) -> dict:
        return {"HA_URL": self.url, "HA_TOKEN": TOKEN, "RIVET_HA_RELEASES_API": self.url + "/gh/",
                "RIVET_HA_SITE": self.url}
