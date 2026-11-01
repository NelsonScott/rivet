#!/usr/bin/env python3
"""Rivet — GTK4 + WebKitGTK 6.0 shell around app/ui.

The UI is plain HTML/JS in app/ui. It talks to this process over the bridge
defined in CONTRACT.md:

    JS  -> py : window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({cmd, ...}))
    py  -> JS : window.rivet.receive({type: "state"|"settings"|"progress"|"error", ...})

Everything that is not GTK lives in `Bridge`, so the command dispatcher can be
exercised without opening a window (see app/selftest.py).

Usage: rivet-updater [--demo]
  --demo  use app/ui/sample-state.json when there is no real state.json, and
          default the apply command to `rivet-apply --dry-run` (falling back to
          a built-in fake event stream when rivet-apply is not installed yet).
"""

import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, Gtk, WebKit  # noqa: E402

try:
    gi.require_version("Adw", "1")
    from gi.repository import Adw  # noqa: E402

    HAVE_ADW = True
except (ValueError, ImportError):  # pragma: no cover - libadwaita is present on F44
    Adw = None
    HAVE_ADW = False

APP_ID = "xyz.scottnelson.Rivet"
HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
INDEX = os.path.join(UI_DIR, "index.html")
SAMPLE = os.path.join(UI_DIR, "sample-state.json")

STATE_DIR = os.path.expanduser("~/.local/state/rivet")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONF_DIR = os.path.expanduser("~/.config/rivet")
SETTINGS_FILE = os.path.join(CONF_DIR, "settings.json")
WINDOW_FILE = os.path.join(CONF_DIR, "window.json")
APPLY_LOG = os.path.join(STATE_DIR, "logs", "apply.log")
HEALTH_FILE = os.path.join(STATE_DIR, "health.json")     # backend/health.py, 2026-11-01



def _cli(name):
    """Where a backend CLI lives: ~/.local/bin (install.sh, and what QA's scratch HOME fakes)
    wins; else /usr/bin (the rivet-updater RPM). Neither: the ~/.local/bin path, so the
    "not installed yet (...)" messages keep naming the from-source location."""
    local = os.path.expanduser("~/.local/bin/" + name)
    if not os.path.exists(local) and os.path.exists("/usr/bin/" + name):
        return "/usr/bin/" + name
    return local


COLLECT_BIN = _cli("rivet-collect")
APPLY_BIN = _cli("rivet-apply")
DOCTOR_BIN = _cli("rivet-doctor")
DOCTOR_DIR = os.path.join(STATE_DIR, "doctor")
DOCTOR_LOG = os.path.join(STATE_DIR, "logs", "doctor.log")

DEFAULT_SETTINGS = {
    "check_interval_min": 60,
    "notify": True,
    "red_includes_reboot": False,
    "theme": "system",
    "look": "periwinkle",          # ui/looks/<id>.css; the page owns the list, unknown ids fall back
    "type_size": "compact",        # compact | comfortable (WebKit page zoom, see TYPE_ZOOM)
    "show_containers": False,      # 2026-11-01: the Containers (podman) section, off for now
    "ai_triage": False,            # Claude Code reasons + "what is this" lines: opt-in, off by default
}
TYPE_ZOOM = {"compact": 1.0, "comfortable": 1.12}
DEFAULT_SIZE = (1280, 860)

STEAM_REF = "com.valvesoftware.Steam"


def log(*a):
    print("[rivet]", *a, file=sys.stderr, flush=True)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log("cannot read %s: %s" % (path, exc))
        return default


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


class Bridge:
    """Every UI command, with no GTK widgets involved.

    push(obj)     -> send a message to the UI
    confirm(text, detail, cb) -> ask the user; cb(bool). Injected by the window.
    """

    def __init__(self, push, confirm=None, demo=False):
        self.push = push
        self.confirm = confirm or (lambda text, detail, cb: cb(False))
        self.demo = demo
        self.settings = self.load_settings()
        self.last_kmod_verified = None
        self._applying = False
        self.on_busy_changed = None  # injected by the window: cb(busy: bool) on every change
        self.doctor_active = False   # the UI is on the doctor view (investigation or result)
        self._state_pending = False  # state.json changed while pushes were held back
        self._cur_step = "download"
        self._done_seen = False
        self._last_msg = None
        self._monitor = None
        self._fake_timer = None
        self._procs = []             # live Gio.Subprocess objects; emptied by _release()
        self._collect_proc = None    # the running rivet-collect, if any (one at a time)
        self._buf = b""
        self._held_watch = None      # the --verify-held process, while it is watching
        self._held_verdict = False   # the watcher reached a verdict and is acting on it
        self._doctor = None          # the running rivet-doctor process context, if any
        self._other = None           # the running Other-software update, if any
        self._batch = None           # "Update the safe ones": {ids, queue, ran, refused, stop}
        self._health = None          # the running `rivet-doctor health` process, if any

    BUSY_MSG = "Not now: another operation is still running. Wait for it to finish."
    NO_CLAUDE = ("Fix it needs Claude Code: install it (claude.com/claude-code), run `claude` once "
                 "to log in, then open Rivet again.")

    # ------------------------------------------------------------ capabilities
    # 2026-11-01 abuse pass: on a machine without the Claude Code CLI, Fix it must not be offered
    # (the UI shows a note instead) and must not start if a stale page asks for it anyway.
    # Same lookup as the backend (rivet_doctor.claude_bin); RIVET_CLAUDE_BIN, when set, decides.
    CLAUDE_CANDIDATES = ("~/.local/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude")

    @classmethod
    def claude_path(cls):
        env = os.environ.get("RIVET_CLAUDE_BIN")
        if env:
            return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
        for c in cls.CLAUDE_CANDIDATES:
            c = os.path.expanduser(c)
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return shutil.which("claude")

    def push_caps(self):
        self.push({"type": "caps", "caps": {"claude": bool(self.claude_path())}})

    # ----------------------------------------------------------------- busy
    # `applying` is set while anything changes the system (rivet-apply, a held-back
    # verify/keep/rollback). `busy` adds a running rivet-doctor. The window watches
    # busy so a close mid-operation hides instead of killing the child's stdout pipe.
    @property
    def applying(self):
        return self._applying

    @applying.setter
    def applying(self, value):
        value = bool(value)
        if value == self._applying:
            return
        self._applying = value
        self._notify_busy()

    @property
    def doctor_running(self):
        return self._doctor is not None and not self._doctor["done"]

    @property
    def busy(self):
        return self._applying or self.doctor_running

    def _notify_busy(self):
        cb = self.on_busy_changed
        if cb is None:
            return
        try:
            cb(self.busy)
        except Exception:
            log("busy callback failed:\n" + traceback.format_exc())

    # ------------------------------------------------------- child processes
    def _track(self, proc):
        self._procs.append(proc)
        return proc

    def _release(self, proc):
        """Exit-callback bookkeeping: forget the process and close its pipes."""
        try:
            self._procs.remove(proc)
        except ValueError:
            pass
        for stream in (proc.get_stdout_pipe(), proc.get_stderr_pipe()):
            if stream is None or stream.is_closed():
                continue
            try:
                stream.close(None)
            except GLib.Error as exc:
                log("closing a child pipe: %s" % exc.message)

    @staticmethod
    def _exit_info(proc):
        """(exit code or None, how it ended) for a waited-for Gio.Subprocess.

        get_exit_status() is only meaningful after get_if_exited(); a signaled
        child must read "killed by signal 15", not "exited 1".
        """
        if proc.get_if_exited():
            rc = proc.get_exit_status()
            return rc, "exited with code %s" % rc
        if proc.get_if_signaled():
            sig = proc.get_term_sig()
            try:
                name = " (%s)" % signal.Signals(sig).name
            except (ValueError, AttributeError):
                name = ""
            return None, "was killed by signal %s%s" % (sig, name)
        return None, "ended"

    @staticmethod
    def _interrupted(proc, rc):
        """Died by a signal, or exited 128+N (the shell convention a handler uses
        after catching signal N: 143 = SIGTERM, 130 = SIGINT)."""
        if proc.get_if_signaled():
            return True
        if rc is None or rc <= 128:
            return False
        try:
            signal.Signals(rc - 128)
            return True
        except ValueError:
            return False

    # ---------------------------------------------------------------- state
    def load_settings(self):
        s = dict(DEFAULT_SETTINGS)
        s.update(read_json(SETTINGS_FILE, {}) or {})
        return s

    def save_settings(self):
        write_json_atomic(SETTINGS_FILE, self.settings)

    def load_state(self):
        doc = read_json(STATE_FILE)
        if doc is None and self.demo:
            doc = read_json(SAMPLE)
            if doc is not None:
                log("demo: serving app/ui/sample-state.json")
        if doc is None:
            return {
                "schema": 1,
                "generated_at": None,
                "checked_at": None,
                "status": "error",
                "error": "No state yet — run rivet-collect (%s)."
                % ("installed" if os.path.exists(COLLECT_BIN) else "not installed"),
                "host": {"os": "Fedora", "kernel_running": os.uname().release, "secure_boot": None},
                "flags": {},
                "summary": {"total": 0, "total_bytes": 0},
                "groups": [],
            }
        return doc

    def push_state(self):
        self._state_pending = False
        self.push({"type": "state", "state": self.load_state()})
        self.push_health()

    # ------------------------------------------------------------ health (2026-11-01)
    # backend/health.py writes health.json (rivet-collect, `rivet-doctor health`); the page
    # shows each problem as a banner with its one fix. Own message type, so it never waits
    # behind a held-back state push.
    def push_health(self, doc=None):
        doc = doc if isinstance(doc, dict) else read_json(HEALTH_FILE)
        if isinstance(doc, dict) and isinstance(doc.get("checks"), list):
            self.push({"type": "health", "health": doc})

    def _health_spawn(self, *args, on_event=None):
        if self._health is not None:
            return False
        argv = self._doctor_argv("health", *args)
        if argv is None:
            if self.demo:
                self.push_health({"checked_at": None, "checks": [], "demo": True})
            return False
        log("health: %s" % " ".join(argv))
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            log("health: could not start %s: %s" % (argv[0], exc.message))
            return False
        self._health = proc
        self._track(proc)
        proc.communicate_utf8_async(None, None, self._health_finished, on_event)
        return True

    def _health_finished(self, proc, res, on_event):
        try:
            try:
                _ok, out, _err = proc.communicate_utf8_finish(res)
            except GLib.Error as exc:
                log("health: %s" % exc.message)
                out = ""
            done = None
            for line in (out or "").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if on_event:
                    self.push({"type": "health", "event": ev})
                if ev.get("step") == "done":
                    done = ev
            if done is None and on_event:
                rc, how = self._exit_info(proc)
                self.push({"type": "health", "event": {
                    "ts": None, "step": "done", "status": "fail", "pct": 100, "synthetic": True,
                    "summary": "rivet-doctor health %s before reporting a result." % how,
                    "msg": None, "log": None, "health": None}})
            self.push_health((done or {}).get("health"))
        except Exception:
            log("internal error in the health reader:\n%s" % traceback.format_exc())
        finally:
            self._health = None
            self._release(proc)

    HEALTH_IDS = ("doc-portal",)

    def cmd_healthFix(self, msg):
        cid = str(msg.get("id") or "")
        if cid not in self.HEALTH_IDS:
            log("healthFix: refusing %r" % cid)
            return
        if not self._health_spawn("--fix", cid, on_event=True):
            self.push({"type": "health", "event": {
                "ts": None, "step": "done", "status": "fail", "pct": 100, "synthetic": True, "msg": None,
                "log": None, "health": None, "fixed": None,
                "summary": "Not now: a health check is still running." if self._health is not None
                else "rivet-doctor is not installed yet (%s)." % DOCTOR_BIN}})

    def _flush_pending_state(self):
        """Push the state.json change that arrived while pushes were held back."""
        if self._state_pending and not (self.applying or self.doctor_active):
            log("pushing the state.json change held back meanwhile")
            self.push_state()

    def push_settings(self):
        self.push({"type": "settings", "settings": self.settings})

    def push_error(self, msg):
        self.push({"type": "error", "msg": str(msg)})

    # -------------------------------------------------------------- monitor
    def start_monitor(self):
        """Watch state.json so the collector timer refreshes an open window.

        The backend writes atomically (tmp + rename), so watch the directory:
        a rename does not always reach a monitor attached to the old inode.
        """
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            d = Gio.File.new_for_path(STATE_DIR)
            self._monitor = d.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._on_dir_changed)
        except GLib.Error as exc:
            log("file monitor unavailable: %s" % exc)

    def _on_dir_changed(self, _mon, gfile, other, event):
        # An atomic rewrite shows up as RENAMED(tmp -> state.json): the interesting
        # name can be in either slot, so check both.
        names = set()
        for f in (gfile, other):
            if f is not None:
                names.add(os.path.basename(f.get_path() or ""))
        if "state.json" not in names:
            return
        if event in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.RENAMED,
        ):
            if self.applying or self.doctor_active:
                # A state push makes the UI redraw the list, which would throw the
                # user out of the progress / doctor screen. Hold it until they leave.
                self._state_pending = True
                log("state.json changed on disk — holding (%s)"
                    % ("applying" if self.applying else "doctor view"))
                return
            log("state.json changed on disk — pushing")
            self.push_state()

    # -------------------------------------------------------------- dispatch
    def handle(self, raw):
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
        except (ValueError, TypeError) as exc:
            log("bad message from UI: %s" % exc)
            return
        cmd = msg.get("cmd")
        fn = getattr(self, "cmd_" + str(cmd), None)
        if fn is None:
            log("unknown command %r" % cmd)
            return
        try:
            fn(msg)
        except Exception as exc:  # keep the UI alive whatever happens
            log("command %s failed: %r" % (cmd, exc))
            self.push_error("%s failed: %s" % (cmd, exc))

    # commands -------------------------------------------------------------
    def cmd_ready(self, _msg):
        # A freshly loaded page shows the list, unless a doctor is still streaming
        # (its next event pulls the UI back onto the doctor view).
        self.doctor_active = self.doctor_running
        self.push_settings()
        self.push_caps()
        self.push_state()
        self._health_spawn()          # a fresh look (a stat + a systemctl call), result pushed

    def cmd_leaveDoctor(self, _msg):
        """The UI left the doctor view (back to the list): state pushes resume."""
        self.doctor_active = False
        self._flush_pending_state()

    cmd_backToList = cmd_leaveDoctor     # alias, in case the UI names it that way

    def cmd_refresh(self, _msg):
        self.doctor_active = False        # the UI is on the loading screen now
        if self._collect_proc is not None:
            # the escape-hatch button on a slow check: one collector at a time
            log("refresh: a collector is already running, not starting another")
            return
        cmd = os.environ.get("RIVET_COLLECT_CMD")
        argv = shlex.split(cmd) if cmd else [COLLECT_BIN, "--quiet"]
        if not cmd and not os.path.exists(COLLECT_BIN):
            log("rivet-collect not installed; re-reading state.json only")
            self.push_state()
            self.push_error("rivet-collect is not installed yet (%s)." % COLLECT_BIN)
            return
        doc = self.load_state()
        doc = dict(doc)
        doc["status"] = "checking"
        doc["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")   # a fresh stub, not a stale one
        self.push({"type": "state", "state": doc})
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE
            )
        except GLib.Error as exc:
            self.push_error("could not start %s: %s" % (argv[0], exc.message))
            self.push_state()
            return
        self._track(proc)
        self._collect_proc = proc
        ctx = {"proc": proc, "buf": b"", "argv": argv, "last": None, "lines": 0}
        self._collect_read(proc.get_stderr_pipe(), ctx)

    def _collect_read(self, stream, ctx):
        stream.read_bytes_async(self.CHUNK, GLib.PRIORITY_DEFAULT, None, self._collect_chunk, ctx)

    def _collect_chunk(self, stream, res, ctx):
        # Drain the collector's stderr: a pipe nobody reads fills up after 64 KB and
        # a chatty collector blocks on it forever. Lines go to our log, the last one
        # is kept for the error message.
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            log("collector stderr: %s" % exc.message)
            data = b""
        except Exception:
            log("collector stderr reader failed:\n" + traceback.format_exc())
            data = b""
        try:
            if data:
                ctx["buf"] += data
                while b"\n" in ctx["buf"]:
                    line, ctx["buf"] = ctx["buf"].split(b"\n", 1)
                    self._collect_line(line, ctx)
                self._collect_read(stream, ctx)
                return
            if ctx["buf"].strip():
                self._collect_line(ctx["buf"], ctx)
            ctx["buf"] = b""
        except Exception:
            log("collector stderr handling failed:\n" + traceback.format_exc())
        ctx["proc"].wait_async(None, self._collect_done, ctx)

    def _collect_line(self, raw, ctx):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            return
        ctx["last"] = line
        ctx["lines"] += 1
        if ctx["lines"] <= 200:
            log("collector: %s" % line)
        elif ctx["lines"] == 201:
            log("collector: (more stderr follows; not logged)")

    def _collect_done(self, proc, res, ctx):
        self._collect_proc = None
        try:
            try:
                proc.wait_finish(res)
                rc, how = self._exit_info(proc)
            except GLib.Error as exc:
                self.push_error("collector failed: %s" % exc.message)
                rc, how = None, None
            if rc != 0 and how is not None:
                tail = (" Last message: %s" % ctx["last"][:200]) if ctx.get("last") else ""
                name = os.path.basename(ctx["argv"][0])
                if self._interrupted(proc, rc):
                    # rivet-collect catches SIGTERM/SIGINT, writes status "error" /
                    # error "interrupted" to state.json itself and exits 128+N. The
                    # state push below carries that message; an error toast here
                    # would only talk over it.
                    log("%s interrupted (%s); state.json carries the message.%s" % (name, how, tail))
                else:
                    self.push_error("%s %s.%s" % (name, how, tail))
        except Exception:
            log("collector exit handling failed:\n" + traceback.format_exc())
        finally:
            self._release(proc)
            self.push_state()

    def cmd_apply(self, msg):
        items = [str(i) for i in (msg.get("items") or [])]
        if not items:
            return
        if self.busy:
            log("apply refused: %s is still running"
                % ("a held-back operation" if self._held_watch else
                   "an update" if self.applying else "rivet-doctor"))
            self.push_error(self.BUSY_MSG)
            return
        self.doctor_active = False
        plan_path = os.path.join(
            GLib.get_tmp_dir(), "rivet-plan-%d-%d.json" % (os.getpid(), int(time.time()))
        )
        write_json_atomic(plan_path, {"items": items})
        env_cmd = os.environ.get("RIVET_APPLY_CMD")
        if env_cmd:
            argv = shlex.split(env_cmd) + ["--plan", plan_path]
        elif os.path.exists(APPLY_BIN):
            argv = [APPLY_BIN] + (["--dry-run"] if self.demo else []) + ["--plan", plan_path]
        elif self.demo:
            log("demo: rivet-apply not installed, using the built-in fake stream")
            self.applying = True
            self._done_seen = False
            self._cur_step = "download"
            self._fake_apply(items)
            return
        else:
            self.push_error("rivet-apply is not installed yet (%s)." % APPLY_BIN)
            return
        log("apply: %s" % " ".join(argv))
        self.applying = True
        self._done_seen = False
        self._cur_step = "download"
        self._last_msg = None
        self.last_kmod_verified = None   # a verdict belongs to ONE run, never the previous
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            )
        except GLib.Error as exc:
            self.applying = False
            self.push_error("could not start %s: %s" % (argv[0], exc.message))
            return
        self._track(proc)
        self._buf = b""
        self._read_chunk(proc.get_stdout_pipe(), proc)

    CHUNK = 4096

    def _read_chunk(self, stream, proc):
        stream.read_bytes_async(self.CHUNK, GLib.PRIORITY_DEFAULT, None, self._on_chunk, proc)

    def _on_chunk(self, stream, res, proc):
        # Raw chunked reads, split on newlines here. A zero-length read is EOF
        # and only EOF. DataInputStream.read_line_finish() hands back b"" for
        # BOTH a blank line and EOF (never None), which made the old reader
        # re-arm itself forever once rivet-apply exited: the main loop spun at
        # 85% CPU and the window stopped answering clicks (2026-09-18).
        #
        # Nothing in here may escape: an exception in a GLib callback is only
        # printed, the read would never be re-armed and `applying` would stay
        # set for good. So: log it, synthesize the failure, keep draining.
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            log("apply stream error: %s" % exc.message)
            data = b""
        except Exception:
            self._apply_internal_error("read")
            data = b""
        if not data:
            try:
                self._flush_partial()
            except Exception:
                self._apply_internal_error("last line")
            proc.wait_async(None, self._on_apply_exit, None)
            return
        self._buf += data
        try:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                self._handle_line(line)
        except Exception:
            self._apply_internal_error("line")
        self._read_chunk(stream, proc)

    def _apply_internal_error(self, where):
        """A bug in our own reader: never leave the UI waiting or `applying` stuck."""
        log("internal error in the apply reader (%s):\n%s" % (where, traceback.format_exc()))
        if self._done_seen:
            return
        self._done_seen = True
        self.applying = False
        self._push_synthetic_done(
            "Rivet hit an internal error while reading rivet-apply's output (%s); "
            "the run itself may well have finished. Check the log." % where, None)

    def _handle_line(self, raw):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            self._forward_event(line)

    def _flush_partial(self):
        tail, self._buf = self._buf, b""
        if tail.strip():
            self._handle_line(tail)

    def _on_apply_exit(self, proc, res, _u):
        try:
            try:
                proc.wait_finish(res)
            except GLib.Error as exc:
                log("apply wait: %s" % exc.message)
            self._apply_finished(proc)
        except Exception:
            self._apply_internal_error("exit")
        finally:
            self.applying = False
            self._release(proc)
            self._flush_pending_state()

    def _forward_event(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            # Not a contract event (a stray tool line): keep it, attached to the
            # step that is currently running, so it shows up in the details log.
            ev = {"ts": None, "step": self._cur_step, "status": "progress", "pct": None,
                  "msg": None, "log": line}
        if self._done_seen:
            log("ignoring output after the done event: %s" % line[:120])
            return
        if ev.get("step") == "done":
            self.last_kmod_verified = ev.get("kmod_verified")
            self.applying = False
            self._done_seen = True
        elif ev.get("step"):
            self._cur_step = ev["step"]
        if ev.get("step") != "done" and (ev.get("msg") or ev.get("log")):
            self._last_msg = ev.get("msg") or ev.get("log")
        self.push({"type": "progress", "event": ev})

    def _apply_finished(self, proc):
        self.applying = False
        rc, how = self._exit_info(proc)
        if self._done_seen:
            # rivet-apply told us how it ended; its summary is the truth. The old
            # code pushed "rivet-apply exited 1." over it whenever kmod_verified
            # was null, which hid the real story on 2026-09-21.
            if rc != 0:
                log("rivet-apply %s after its done event" % how)
            return
        # No done event at all (crash, kill, OOM): say what IS known.
        self._done_seen = True
        self._push_synthetic_done("rivet-apply %s before reporting a result." % how, rc)

    def _push_synthetic_done(self, cause, rc):
        summary = "%s Last step reached: %s.%s Full log: %s" % (
            cause, self._cur_step,
            (" Last message: %s." % str(self._last_msg)[:240].rstrip(".")) if self._last_msg else "",
            APPLY_LOG)
        self.push({"type": "progress", "event": {
            "ts": None, "step": "done", "status": "fail", "pct": 100, "msg": summary, "log": None,
            "reboot_required": False, "kmod_verified": None, "summary": summary,
            "cause": cause, "error": None, "last_msg": self._last_msg, "warnings": [], "results": {},
            "log_path": APPLY_LOG, "synthetic": True, "last_step": self._cur_step,
            "exit_code": rc}})

    # ------------------------------------------------------------ held back
    # Try / verify / keep / roll back a pinned Flatpak ref (CONTRACT.md, "Held back").
    # Each command is one rivet-apply process streamed to the UI as {"type":"held"}
    # messages. Every process gets its OWN buffer: the watcher and a manual Keep /
    # Roll back can overlap, and they must not share the apply reader's state.
    def _held_argv(self, *args):
        env_cmd = os.environ.get("RIVET_APPLY_CMD")
        if env_cmd:
            return shlex.split(env_cmd) + list(args)
        if self.demo or not os.path.exists(APPLY_BIN):
            return None          # demo never touches a real pin
        return [APPLY_BIN] + list(args)

    def _held_run(self, action, ref, *args):
        argv = self._held_argv(*args)
        if argv is None:
            if self.demo:
                self._fake_held(action, ref)
            else:
                self.push_error("rivet-apply is not installed yet (%s)." % APPLY_BIN)
            return None
        log("held %s: %s" % (action, " ".join(argv)))
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self._held_push_done(action, ref, "could not start %s: %s" % (argv[0], exc.message))
            return None
        self._track(proc)
        ctx = {"proc": proc, "buf": b"", "done": False, "action": action, "ref": ref,
               "last": None, "cancelled": False}
        self._held_read(proc.get_stdout_pipe(), ctx)
        return ctx

    def _held_read(self, stream, ctx):
        stream.read_bytes_async(self.CHUNK, GLib.PRIORITY_DEFAULT, None, self._held_chunk, ctx)

    def _held_chunk(self, stream, res, ctx):
        # Same rules as _on_chunk: a zero-length read is EOF and only EOF, and no
        # exception may escape (it would leave `applying` set and the UI waiting).
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            log("held stream error: %s" % exc.message)
            data = b""
        except Exception:
            self._held_internal_error(ctx, "read")
            data = b""
        if not data:
            try:
                if ctx["buf"].strip():
                    self._held_line(ctx["buf"], ctx)
            except Exception:
                self._held_internal_error(ctx, "last line")
            ctx["buf"] = b""
            ctx["proc"].wait_async(None, self._held_exit, ctx)
            return
        ctx["buf"] += data
        try:
            while b"\n" in ctx["buf"]:
                line, ctx["buf"] = ctx["buf"].split(b"\n", 1)
                self._held_line(line, ctx)
        except Exception:
            self._held_internal_error(ctx, "line")
        self._held_read(stream, ctx)

    def _held_internal_error(self, ctx, where):
        log("internal error in the held reader (%s, %s):\n%s"
            % (ctx["action"], where, traceback.format_exc()))
        if ctx["done"] or ctx["cancelled"]:
            return
        ctx["done"] = True
        self._held_push_done(
            ctx["action"], ctx["ref"],
            "Rivet hit an internal error while reading rivet-apply's output (%s). Nothing "
            "is rolled back automatically; the trial, if one was started, is still on disk."
            % where)

    def _held_line(self, raw, ctx):
        line = raw.decode("utf-8", "replace").strip()
        if not line or ctx["cancelled"] or ctx["done"]:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            ev = {"ts": None, "step": "held", "status": "progress", "pct": None, "msg": None,
                  "log": line}
        if ev.get("verdict") in ("ok", "fail") and ctx is self._held_watch:
            self._held_verdict = True      # from here on the watcher owns the outcome
        if ev.get("step") == "done":
            ctx["done"] = True
        elif ev.get("msg") or ev.get("log"):
            ctx["last"] = ev.get("msg") or ev.get("log")
        self.push({"type": "held", "event": ev})

    def _held_exit(self, proc, res, ctx):
        # A cancelled watcher (manual Keep / Roll back won) must NOT clear `applying`:
        # the manual operation that replaced it is running and owns the flag now.
        owns_flag = ctx["action"] in ("keep", "rollback", "verify") and not ctx["cancelled"]
        try:
            try:
                proc.wait_finish(res)
            except GLib.Error as exc:
                log("held wait: %s" % exc.message)
            rc, how = self._exit_info(proc)
            if ctx is self._held_watch:
                self._held_watch = None
            if owns_flag:
                self.applying = False
            if ctx["cancelled"]:
                return
            if not ctx["done"]:
                ctx["done"] = True
                self._held_push_done(
                    ctx["action"], ctx["ref"],
                    "rivet-apply %s before reporting a result. Nothing is rolled back "
                    "automatically; the trial, if one was started, is still on disk.%s" % (
                        how, (" Last message: %s" % str(ctx["last"])[:200]) if ctx["last"] else ""))
            # No refresh here: the result screen's Done button asks for one, and two
            # collectors racing each other redraw the list twice.
        except Exception:
            self._held_internal_error(ctx, "exit")
        finally:
            if ctx is self._held_watch:
                self._held_watch = None
            if owns_flag:
                self.applying = False
            self._release(proc)
            self._flush_pending_state()

    def _held_push_done(self, action, ref, summary, result="failed", clear=True):
        """One synthetic terminal event. `clear` releases `applying`; a refusal of a
        second command must leave the running operation's flag alone."""
        if clear:
            self.applying = False
        self.push({"type": "held", "event": {
            "ts": None, "step": "done", "status": "fail", "pct": 100, "msg": summary, "log": None,
            "summary": summary, "cause": summary, "synthetic": True, "log_path": APPLY_LOG,
            "held": {"action": action, "result": result, "ref": ref}}})

    def _held_refuse(self, action, ref):
        log("held %s refused: another operation is still running" % action)
        self._held_push_done(action, ref, self.BUSY_MSG, result="refused", clear=False)

    def _held_stop_watch(self):
        """Manual Keep / Roll back wins over the watcher, unless it already has a verdict."""
        ctx = self._held_watch
        if ctx is None:
            return True
        if self._held_verdict:
            return False
        ctx["cancelled"] = True
        self._held_watch = None
        self.applying = False            # the flag changes hands here, not at its exit
        try:
            ctx["proc"].send_signal(15)    # held_ops turns SIGTERM into a clean exit
        except Exception as exc:
            log("could not stop the watcher: %r" % exc)
        return True

    def cmd_tryHeld(self, msg):
        ref = str(msg.get("ref") or "")
        if not ref:
            return
        if self.busy:
            self._held_refuse("try", ref)
            return
        self._held_verdict = False
        args = ["--try-held", ref] + (["--force"] if msg.get("force") else [])
        self._held_run("try", ref, *args)

    def cmd_verifyHeld(self, msg):
        ref = str(msg.get("ref") or "")
        if not ref:
            return
        if self._held_watch is not None:
            if self._held_watch["ref"] == ref:
                # The UI is already waiting on exactly this watcher (a re-send after a
                # reload, say): it will deliver its own done event. Say so, start nothing.
                log("verifyHeld: already watching %s" % ref)
                self.push({"type": "held", "event": {
                    "ts": None, "step": "watch", "status": "progress", "pct": None, "msg": None,
                    "log": "Already watching; nothing restarted.",
                    "held": {"action": "verify", "ref": ref}}})
                return
            self._held_refuse("verify", ref)
            return
        if self.busy:                      # an apply, a manual keep/rollback or the doctor
            self._held_refuse("verify", ref)
            return
        self._held_verdict = False
        timeout = msg.get("timeout") or 240
        self.applying = True               # keep collector refreshes from redrawing the screen
        self._held_watch = self._held_run("verify", ref, "--verify-held", ref,
                                          "--timeout", str(timeout))
        if self._held_watch is None and not self.demo:
            self.applying = False

    def cmd_keepHeld(self, msg):
        self._held_manual("keep", "--keep-held", msg)

    def cmd_rollbackHeld(self, msg):
        self._held_manual("rollback", "--rollback-held", msg)

    def _held_manual(self, action, flag, msg):
        ref = str(msg.get("ref") or "")
        if not ref:
            return
        if (self.applying and self._held_watch is None) or self.doctor_running:
            # busy with something that is NOT the watcher (the watcher is the one
            # thing a manual Keep / Roll back is allowed to replace)
            self._held_refuse(action, ref)
            return
        if not self._held_stop_watch():
            log("held %s ignored: the watcher already reached a verdict" % action)
            return
        self.applying = True
        if self._held_run(action, ref, flag, ref) is None and not self.demo:
            self.applying = False

    def _fake_held(self, action, ref):
        """--demo: a canned stream, so the screens can be seen without touching a pin."""
        held = {"ref": ref, "display_name": "gamescope", "label": "the game",
                "from_version": "3.16.28", "to_version": "3.16.30"}
        seq = {
            "try": [("trial", "start", 0, "Checking gamescope…"),
                    ("trial", "progress", 30, "Updating gamescope to 3.16.30…"),
                    ("trial", "awaiting_verify", 100, "gamescope 3.16.30 is installed. Now launch the game. I'm watching."),
                    ("done", "ok", "awaiting_verify", "Demo: nothing was changed.")],
            "verify": [("watch", "start", 0, "Waiting for you to launch the game…"),
                       ("watch", "progress", 30, "The game is running, 25 s…"),
                       ("watch", "progress", 80, "The game is running, 60 s…"),
                       ("done", "ok", "kept", "gamescope 3.16.30 kept. Pin removed. (demo)")],
            "keep": [("done", "ok", "kept", "gamescope 3.16.30 kept. Pin removed. (demo)")],
            "rollback": [("done", "ok", "rolled_back",
                          "Rolled back to 3.16.28 and pinned again. 3.16.30 is marked bad. (demo)")],
        }[action]

        def step(i):
            if i < len(seq):
                name, status, a, b = seq[i]
                if name == "done":
                    ev = {"step": "done", "status": status, "summary": b, "msg": b,
                          "held": dict(held, action=action, result=a)}
                    self.applying = False
                else:
                    ev = {"step": name, "status": status, "pct": a, "msg": b, "log": None,
                          "held": held}
                self.push({"type": "held", "event": ev})
                GLib.timeout_add(900, step, i + 1)
            return False

        GLib.timeout_add(250, step, 0)

    # ------------------------------------------------------------ fix it (doctor)
    # "Fix it" on a failed result: rivet-doctor diagnose (Claude, read-only tools)
    # streams {"type":"doctor","mode":"diagnose","event":ev}; its done event carries
    # the diagnosis and the path of the saved fix. "Run the fix" streams the same
    # way with mode "run". One process at a time; a second click is ignored.
    def _doctor_argv(self, *args):
        env_cmd = os.environ.get("RIVET_DOCTOR_CMD")
        if env_cmd:
            return shlex.split(env_cmd) + list(args)
        if os.path.exists(DOCTOR_BIN):
            # --demo never calls Claude and never runs a fix: canned story
            return [DOCTOR_BIN] + list(args) + (["--fake", "ok"] if self.demo else [])
        return None

    def _doctor_run(self, mode, *args):
        self.doctor_active = True         # the UI is on the doctor view from here on
        if self.doctor_running:
            if self._doctor["mode"] == mode:
                # The contract's "second click is ignored": the running process keeps
                # streaming into the view the UI just reset, and its done ends the wait.
                log("doctor: already running %s, ignoring the second click" % mode)
                return None
            self._doctor_push_done(mode, "Not now: the doctor is still busy with the %s. "
                                   "Wait for it to finish." % (
                                       "diagnosis" if self._doctor["mode"] == "diagnose" else "fix"))
            return None
        if self.applying:
            self._doctor_push_done(mode, self.BUSY_MSG)
            return None
        argv = self._doctor_argv(*args)
        if argv is None:
            self._doctor_push_done(mode, "rivet-doctor is not installed yet (%s)." % DOCTOR_BIN)
            return None
        log("doctor %s: %s" % (mode, " ".join(argv)))
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self._doctor_push_done(mode, "could not start %s: %s" % (argv[0], exc.message))
            return None
        self._track(proc)
        ctx = {"proc": proc, "buf": b"", "done": False, "mode": mode, "last": None, "step": None}
        self._doctor = ctx
        self._notify_busy()
        self._doctor_read(proc.get_stdout_pipe(), ctx)
        return ctx

    def _doctor_read(self, stream, ctx):
        stream.read_bytes_async(self.CHUNK, GLib.PRIORITY_DEFAULT, None, self._doctor_chunk, ctx)

    def _doctor_chunk(self, stream, res, ctx):
        # Same rules as _on_chunk: zero-length read = EOF, and nothing may escape.
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            log("doctor stream error: %s" % exc.message)
            data = b""
        except Exception:
            self._doctor_internal_error(ctx, "read")
            data = b""
        if not data:
            try:
                if ctx["buf"].strip():
                    self._doctor_line(ctx["buf"], ctx)
            except Exception:
                self._doctor_internal_error(ctx, "last line")
            ctx["buf"] = b""
            ctx["proc"].wait_async(None, self._doctor_exit, ctx)
            return
        ctx["buf"] += data
        try:
            while b"\n" in ctx["buf"]:
                line, ctx["buf"] = ctx["buf"].split(b"\n", 1)
                self._doctor_line(line, ctx)
        except Exception:
            self._doctor_internal_error(ctx, "line")
        self._doctor_read(stream, ctx)

    def _doctor_internal_error(self, ctx, where):
        log("internal error in the doctor reader (%s, %s):\n%s"
            % (ctx["mode"], where, traceback.format_exc()))
        if ctx["done"]:
            return
        self._doctor_push_done(
            ctx["mode"], "Rivet hit an internal error while reading rivet-doctor's output "
            "(%s). Check %s." % (where, DOCTOR_LOG), ctx=ctx)

    def _doctor_line(self, raw, ctx):
        line = raw.decode("utf-8", "replace").strip()
        if not line or ctx["done"]:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            ev = {"ts": None, "step": ctx["step"] or "doctor", "status": "progress", "pct": None,
                  "msg": None, "log": line}
        if ev.get("step") == "done":
            ctx["done"] = True
        else:
            if ev.get("step"):
                ctx["step"] = ev["step"]
            if ev.get("msg") or ev.get("log"):
                ctx["last"] = ev.get("msg") or ev.get("log")
        self.push({"type": "doctor", "mode": ctx["mode"], "event": ev})
        if ev.get("step") == "done" and ctx["mode"] == "diagnose":
            self._maybe_recheck(ev)

    # 2026-10-12: rivet-apply's verify raced depmod, said "NVIDIA module NOT verified", and
    # the doctor then found nothing wrong ("nothing to run, safe to restart"). The run's
    # kmod_verified=False still blocked Restart, with no way out but a reboot from a terminal.
    # So a diagnosis of that shape re-runs the check for real: `rivet-apply --verify-kmod`
    # (read-only, ~1 s). Its events stream to the UI as mode "recheck"; a done with
    # kmod_verified true lifts the block.
    FAKE_RECHECK = [
        {"ts": None, "step": "verify", "status": "start", "pct": 0,
         "msg": "Verifying the NVIDIA module for 7.2.6-200.fc44.x86_64…", "log": None},
        {"ts": None, "step": "verify", "status": "progress", "pct": 90,
         "msg": "modinfo -k 7.2.6-200.fc44.x86_64 nvidia: ok", "log": None},
        {"ts": None, "step": "verify", "status": "ok", "pct": 100,
         "msg": "NVIDIA module verified for the new kernel.", "log": None},
        {"ts": None, "step": "done", "status": "ok", "pct": 100, "reboot_required": True,
         "kmod_verified": True, "kver": "7.2.6-200.fc44.x86_64",
         "summary": "The NVIDIA module is verified for 7.2.6-200.fc44.x86_64. A reboot is needed to finish."},
    ]

    def _maybe_recheck(self, ev):
        if ev.get("status") != "ok" or self.last_kmod_verified is not False:
            return
        g = ev.get("diagnosis") or {}
        if (g.get("fix") or {}).get("available") or (g.get("reboot") or {}).get("safe") != "yes":
            return
        argv = self._held_argv("--verify-kmod")      # same resolution rules as the held ops
        if argv is None:
            if self.demo:
                self._recheck_events([dict(e) for e in self.FAKE_RECHECK])
            else:
                log("recheck: rivet-apply is not installed (%s)" % APPLY_BIN)
            return
        log("recheck: %s" % " ".join(argv))
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            log("recheck: could not start %s: %s" % (argv[0], exc.message))
            return
        self._track(proc)
        proc.communicate_utf8_async(None, None, self._recheck_finished, None)

    def _recheck_finished(self, proc, res, _u):
        try:
            try:
                _ok, out, _err = proc.communicate_utf8_finish(res)
            except GLib.Error as exc:
                log("recheck: %s" % exc.message)
                out = ""
            evs = []
            for line in (out or "").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    evs.append(ev)
            self._recheck_events(evs)
        except Exception:
            log("internal error in the recheck reader:\n%s" % traceback.format_exc())
        finally:
            self._release(proc)

    def _recheck_events(self, evs):
        done = next((e for e in evs if e.get("step") == "done"), None)
        if done is None:
            log("recheck: rivet-apply --verify-kmod ended without a done event")
            return
        if done.get("kmod_verified") is True:
            self.last_kmod_verified = True
        log("recheck: kmod_verified=%r" % done.get("kmod_verified"))
        for ev in evs:
            self.push({"type": "doctor", "mode": "recheck", "event": ev})

    def _doctor_exit(self, proc, res, ctx):
        try:
            try:
                proc.wait_finish(res)
            except GLib.Error as exc:
                log("doctor wait: %s" % exc.message)
            rc, how = self._exit_info(proc)
            if not ctx["done"]:
                self._doctor_push_done(
                    ctx["mode"], "rivet-doctor %s before reporting a result.%s" % (
                        how, (" Last message: %s" % str(ctx["last"])[:200]) if ctx["last"] else ""),
                    ctx=ctx)
            elif rc != 0:
                log("rivet-doctor %s after its done event" % how)
        except Exception:
            self._doctor_internal_error(ctx, "exit")
        finally:
            ctx["done"] = True
            self._release(proc)
            self._notify_busy()

    def _doctor_push_done(self, mode, summary, ctx=None):
        if ctx is not None:
            ctx["done"] = True
        self.push({"type": "doctor", "mode": mode, "event": {
            "ts": None, "step": "done", "status": "fail", "pct": 100, "msg": summary, "log": None,
            "summary": summary, "cause": summary, "synthetic": True, "log_path": DOCTOR_LOG,
            "diagnosis": None, "fix_path": None}})
        if ctx is not None:
            self._notify_busy()

    def cmd_fixIt(self, msg):
        if not self.claude_path() and not self.demo:
            self.doctor_active = True
            self._doctor_push_done("diagnose", self.NO_CLAUDE)
            return
        log_path = str(msg.get("log") or APPLY_LOG)
        self._doctor_run("diagnose", "diagnose", "--log", log_path)

    def cmd_runFix(self, msg):
        self.doctor_active = True
        path = str(msg.get("fix") or "")
        # Only a diagnosis rivet-doctor wrote itself, never an arbitrary file.
        real = os.path.realpath(path) if path else ""
        if not real or not real.startswith(os.path.realpath(DOCTOR_DIR) + os.sep) \
                or not os.path.isfile(real):
            self._doctor_push_done("run", "No saved diagnosis to run (%s)." % (path or "none"))
            return
        self._doctor_run("run", "run", "--fix", real)

    def cmd_quitSteam(self, _msg):
        argv = ["flatpak", "kill", STEAM_REF]
        log("quitSteam: %s" % " ".join(argv))
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
            )
        except GLib.Error as exc:
            self.push_error("could not run flatpak kill: %s" % exc.message)
            return
        self._track(proc)
        proc.wait_async(None, self._steam_done, None)

    def _steam_done(self, proc, res, _u):
        try:
            proc.wait_finish(res)
        except GLib.Error as exc:
            log("flatpak kill: %s" % exc.message)
        finally:
            self._release(proc)
        # let the collector recompute blocked_by, which is its job per contract
        GLib.timeout_add(600, lambda: (self.cmd_refresh({}), False)[1])

    def cmd_restart(self, _msg):
        if self.last_kmod_verified is False:
            self.push_error(
                "Refusing to reboot: the NVIDIA kernel module was not verified for the new kernel."
            )
            return
        detail = "The system will reboot now to finish applying the updates."
        self.confirm("Restart now?", detail, self._do_reboot)

    def _do_reboot(self, ok):
        if not ok:
            return
        if self.last_kmod_verified is False:
            self.push_error("Refusing to reboot: NVIDIA kernel module not verified.")
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.call_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "Reboot",
                GLib.Variant("(b)", (True,)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            self.push_error("Reboot refused: %s" % exc.message)

    def cmd_openUrl(self, msg):
        url = msg.get("url") or ""
        if not url.startswith(("http://", "https://")):
            log("refusing to open %r" % url)
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error as exc:
            self.push_error("could not open %s: %s" % (url, exc.message))

    # ------------------------------------------------------------ other software
    # An Update / Install in HA button (CONTRACT.md "Other software", 2026-10-31 b): one
    # `rivet-apply --other-update ID`. The UI sends only the row id; rivet-apply rebuilds
    # the command from state.json. Streamed as {"type":"other","event":ev}, own buffer.
    def cmd_otherUpdate(self, msg):
        return self._other_spawn(msg, "--other-update")

    # Fix it on a failed row (CONTRACT.md, 2026-11-01): one `rivet-apply --other-fix ID`
    # (a bounded claude -p run, see backend/other_fix.py), streamed exactly like an Update.
    def cmd_otherFix(self, msg):
        if not self.claude_path() and not self.demo:
            self._other_push_done(str(msg.get("id") or ""), self.NO_CLAUDE, result="refused", clear=False)
            return False
        if self._batch is not None:
            rid = str(msg.get("id") or "")
            self._other_push_done(rid, self.BUSY_MSG, result="refused", clear=False)
            return False
        return self._other_spawn(msg, "--other-fix")

    # Restart Home Assistant core (2026-11-01): after an integration install that left
    # `restart_pending` (once per bulk run, at its end), or from the row's Restart button.
    # rivet-apply --ha-restart refuses unless Home Assistant says a restart is pending.
    RESTART_ID = "ha:_restart"

    def cmd_haRestart(self, _msg=None):
        if self._batch is not None:
            self._other_push_done(self.RESTART_ID, self.BUSY_MSG, result="refused", clear=False)
            return False
        return self._other_spawn({"id": self.RESTART_ID}, "--ha-restart")

    def _ha_restart_auto(self):
        log("other: an integration waits for a Home Assistant restart; restarting it")
        self._other_spawn({"id": self.RESTART_ID}, "--ha-restart")
        return False

    # Remove on an UNUSED Home Assistant add-on (CONTRACT.md "Remove", 2026-11-01): one
    # `rivet-apply --other-remove ID`, streamed exactly like an Update. rivet-apply checks
    # the row (HA add-on, `unused: true`, a slug) and re-counts its entities live first.
    def cmd_otherRemove(self, msg):
        if self._batch is not None:
            rid = str(msg.get("id") or "")
            log("otherRemove %s refused: a bulk update is running" % rid)
            self._other_push_done(rid, self.BUSY_MSG, result="refused", clear=False)
            return False
        return self._other_spawn(msg, "--other-remove")

    def _other_spawn(self, msg, flag):
        rid = str(msg.get("id") or "")
        if not rid or len(rid) > 300:
            return False
        if self.busy:
            log("%s %s refused: busy" % (flag, rid))
            self._other_push_done(rid, self.BUSY_MSG, result="refused", clear=False)
            return False
        env_cmd = os.environ.get("RIVET_APPLY_CMD")
        if env_cmd:
            argv = shlex.split(env_cmd) + [flag, rid]
        elif self.demo or not os.path.exists(APPLY_BIN):
            if self.demo:
                if flag in ("--other-fix", "--ha-restart"):
                    self._fake_other_misc(rid, flag)
                else:
                    self._fake_other(rid, remove=flag == "--other-remove")
                return True
            self.push_error("rivet-apply is not installed yet (%s)." % APPLY_BIN)
            return False
        else:
            argv = [APPLY_BIN, flag, rid]
        log("other %s: %s" % ({"--other-remove": "remove", "--other-fix": "fix", "--ha-restart": "restart"}
                              .get(flag, "update"), " ".join(argv)))
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self._other_push_done(rid, "could not start %s: %s" % (argv[0], exc.message), clear=False)
            return False
        self.applying = True
        self._track(proc)
        ctx = {"proc": proc, "buf": b"", "done": False, "id": rid, "last": None, "restart": False}
        self._other = ctx
        self._other_read(proc.get_stdout_pipe(), ctx)
        return True

    # "Update the safe ones (N)" (CONTRACT.md, 2026-10-31 c): the same otherUpdate path once per
    # id, the next only after the previous rivet-apply has exited. Every id must be in the
    # safe set as state.json has it NOW; anything else is refused and skipped, never run.
    # 2026-11-01: Home Assistant add-ons and integrations join at any non-major bump; a major still asks in its own row.
    @staticmethod
    def other_safe(it):
        return bool(it) and (it.get("status") == "ok" and it.get("action") in ("update", "ha_install")
                             and not it.get("confirm") and it.get("severity") != "major"
                             and not it.get("warning") and not it.get("pulled")
                             and it.get("kind") not in ("core", "os", "supervisor", "firmware")
                             and (it.get("severity") == "patch" or it.get("source") == "podman"
                                  or (it.get("source") == "ha" and it.get("kind") in ("addon", "integration"))))

    def cmd_otherUpdateMany(self, msg):
        ids = []
        for rid in (msg.get("ids") or [])[:50]:
            rid = str(rid or "")
            if rid and len(rid) <= 300 and rid not in ids:
                ids.append(rid)
        if not ids:
            return
        if self.busy or self._batch is not None:
            log("otherUpdateMany refused: busy")
            for rid in ids:
                self._other_push_done(rid, self.BUSY_MSG, result="refused", clear=False)
            self._push_batch({"ids": ids, "ran": [], "refused": ids, "stopped": False})
            return
        doc = self.load_state() or {}
        rows = {i.get("id"): i for i in ((doc.get("elsewhere") or {}).get("items") or []) if isinstance(i, dict)}
        b = {"ids": ids, "queue": [], "ran": [], "refused": [], "skipped": [], "stop": False, "restart": False,
             "restarted": False, "ha_down": None}
        for rid in ids:
            if self.other_safe(rows.get(rid)):
                b["queue"].append(rid)
            else:
                b["refused"].append(rid)
                self._other_push_done(rid, "Not part of a bulk update: %s needs its own click (or is gone)."
                                      % ((rows.get(rid) or {}).get("name") or rid), result="refused", clear=False)
        log("other update many: %d to run, %d refused" % (len(b["queue"]), len(b["refused"])))
        self._batch = b
        self._batch_next()

    def cmd_otherStop(self, _msg):
        if self._batch is not None:
            log("other update many: stop after the current row (%d dropped)" % len(self._batch["queue"]))
            self._batch["stop"] = True

    def _push_batch(self, b):
        self.push({"type": "other", "event": {"ts": None, "step": "batch", "status": "done", "pct": 100,
                                              "msg": None, "log": None, "batch": b}})

    def _batch_next(self):
        b = self._batch
        if b is None:
            return False
        if (b["stop"] or not b["queue"]) and b["restart"] and not b["restarted"] and not b["ha_down"]:
            # an integration from this run waits for a restart: ONE restart for the whole run
            b["restarted"] = True
            log("other update many: restarting Home Assistant once for the integrations")
            if self._other_spawn({"id": self.RESTART_ID}, "--ha-restart"):
                return False
        if b["stop"] or not b["queue"]:
            self._batch = None
            self._push_batch({"ids": b["ids"], "ran": b["ran"], "refused": b["refused"], "skipped": b["skipped"],
                              "stopped": bool(b["stop"] and b["queue"])})
            return False
        rid = b["queue"].pop(0)
        if b.get("ha_down") and rid.startswith("ha:"):
            # CONTRACT "Update the safe ones" (2026-11-01 abuse pass): a failed row does not stop
            # the rest, EXCEPT that once Home Assistant could not be reached its remaining rows are
            # skipped (each would only wait for the same dead host); other rows still run.
            b["skipped"].append(rid)
            self._other_push_done(rid, "Skipped: Home Assistant could not be reached a moment ago, so nothing "
                                  "was sent. Check again when it is back.", result="skipped", clear=False)
            GLib.idle_add(self._batch_next)
            return False
        b["ran"].append(rid)
        if not self.cmd_otherUpdate({"id": rid}):
            # it never started (busy, spawn error): its done is already pushed, go on
            GLib.idle_add(self._batch_next)
        return False

    def _other_read(self, stream, ctx):
        stream.read_bytes_async(self.CHUNK, GLib.PRIORITY_DEFAULT, None, self._other_chunk, ctx)

    def _other_chunk(self, stream, res, ctx):
        # Same rules as _on_chunk: a zero-length read is EOF and only EOF, nothing may escape.
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            log("other stream error: %s" % exc.message)
            data = b""
        except Exception:
            log("other reader:\n" + traceback.format_exc())
            data = b""
        if not data:
            try:
                if ctx["buf"].strip():
                    self._other_line(ctx["buf"], ctx)
            except Exception:
                log("other last line:\n" + traceback.format_exc())
            ctx["buf"] = b""
            ctx["proc"].wait_async(None, self._other_exit, ctx)
            return
        ctx["buf"] += data
        try:
            while b"\n" in ctx["buf"]:
                line, ctx["buf"] = ctx["buf"].split(b"\n", 1)
                self._other_line(line, ctx)
        except Exception:
            log("other line:\n" + traceback.format_exc())
        self._other_read(stream, ctx)

    def _other_line(self, raw, ctx):
        line = raw.decode("utf-8", "replace").strip()
        if not line or ctx["done"]:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            ev = {"ts": None, "step": "other", "status": "progress", "pct": None, "msg": None, "log": line}
        if ev.get("step") == "done":
            ctx["done"] = True
            ev.setdefault("other", {"id": ctx["id"], "result": "failed", "item": None})
            if isinstance(ev.get("other"), dict) and ev["other"].get("restart_pending") and ev.get("status") == "ok":
                ctx["restart"] = True
            if isinstance(ev.get("other"), dict) and ev["other"].get("unreachable"):
                ctx["ha_down"] = str(ev.get("cause") or "unreachable")[:160]
        elif ev.get("msg") or ev.get("log"):
            ctx["last"] = ev.get("msg") or ev.get("log")
        ev.setdefault("other_id", ctx["id"])
        self.push({"type": "other", "event": ev})

    def _other_exit(self, proc, res, ctx):
        try:
            try:
                proc.wait_finish(res)
            except GLib.Error as exc:
                log("other wait: %s" % exc.message)
            rc, how = self._exit_info(proc)
            if not ctx["done"]:
                ctx["done"] = True
                self._other_push_done(ctx["id"], "rivet-apply %s before reporting a result.%s" % (
                    how, (" Last message: %s" % str(ctx["last"])[:200]) if ctx["last"] else ""),
                    clear=False)
        except Exception:
            log("other exit:\n" + traceback.format_exc())
        finally:
            if self._other is ctx:
                self._other = None
            self.applying = False
            self._release(proc)
            self._flush_pending_state()
            if self._batch is not None:
                if ctx.get("restart"):
                    self._batch["restart"] = True
                if ctx.get("ha_down"):
                    self._batch["ha_down"] = ctx["ha_down"]
                GLib.idle_add(self._batch_next)
            elif ctx.get("restart"):
                GLib.idle_add(self._ha_restart_auto)

    def _other_push_done(self, rid, summary, result="failed", clear=True):
        if clear:
            self.applying = False
        self.push({"type": "other", "event": {
            "ts": None, "step": "done", "status": "fail", "pct": 100, "msg": summary, "log": None,
            "summary": summary, "cause": summary, "synthetic": True, "other_id": rid,
            "other": {"id": rid, "result": result, "item": None}}})

    def _fake_other(self, rid, remove=False):
        """--demo: a canned run for the row, so the screens can be seen; nothing changes."""
        doc = self.load_state() or {}
        it = next((i for i in ((doc.get("elsewhere") or {}).get("items") or []) if i.get("id") == rid), {})
        name, to = it.get("name") or rid, it.get("latest") or "?"
        ha = it.get("source") == "ha"
        if remove:
            self.applying = True

            def fin():
                self.applying = False
                self.push({"type": "other", "event": {
                    "ts": None, "step": "done", "status": "ok", "pct": 100, "other_id": rid,
                    "summary": "%s was uninstalled from Home Assistant. (demo: nothing was changed)" % name,
                    "other": {"id": rid, "result": "removed", "item": None, "removed": True}}})
                return False
            self.push({"type": "other", "event": {"ts": None, "step": "other", "status": "start", "pct": 0,
                                                  "msg": "Uninstalling %s… (demo)" % name, "other_id": rid,
                                                  "log": "supervisor/api POST /addons/%s/uninstall" % it.get("addon_slug")}})
            GLib.timeout_add(1200, fin)
            return
        cmd = ("POST /api/services/update/install {\"entity_id\": \"%s\"}" % it.get("entity_id")) if ha \
            else "$ " + str(it.get("command") or "")
        seq = [("start", 0, "Updating %s…" % name, cmd),
               ("progress", 40, "Installing %s… 40%%" % name if ha else "fetching %s@%s" % (name, to), None),
               ("progress", None, "changed 1 package in 2s (demo, nothing ran)", "changed 1 package in 2s (demo, nothing ran)"),
               ("ok", 100, "%s is now %s. (demo)" % (name, to), None)]
        self.applying = True

        def step(i):
            if i < len(seq):
                status, pct, m, lg = seq[i]
                self.push({"type": "other", "event": {"ts": None, "step": "other", "status": status,
                                                      "pct": pct, "msg": m, "log": lg, "other_id": rid}})
                GLib.timeout_add(500, step, i + 1)
            else:
                row = dict(it, status="updated", installed=to, action=None, confirm=False)
                self.applying = False
                self.push({"type": "other", "event": {
                    "ts": None, "step": "done", "status": "ok", "pct": 100, "other_id": rid,
                    "summary": "%s is now %s. (demo: nothing was changed)" % (name, to),
                    "other": {"id": rid, "result": "installed" if ha else "updated", "item": row}}})
                if self._batch is not None:
                    GLib.idle_add(self._batch_next)
            return False

        GLib.timeout_add(200, step, 0)

    def _fake_other_misc(self, rid, flag):
        """--demo for Fix it and the HA restart: canned events, nothing runs."""
        fix = flag == "--other-fix"
        seq = ([("start", 0, "Gathering what happened… (demo)", None),
                ("progress", 30, "Asking Claude (opus) to look into it… (demo)", "$ claude -p --model opus (Fix it tools only)"),
                ("progress", None, "Checking: state", "$ rivet-apply --fix-tool state %s" % rid)] if fix else
               [("start", 0, "Checking Home Assistant's configuration before restarting… (demo)", None),
                ("progress", 40, "Home Assistant is restarting… (demo)", None)])
        self.applying = True

        def step(i):
            if i < len(seq):
                status, pct, m, lg = seq[i]
                self.push({"type": "other", "event": {"ts": None, "step": "fix" if fix else "other", "status": status,
                                                      "pct": pct, "msg": m, "log": lg, "other_id": rid}})
                GLib.timeout_add(500, step, i + 1)
                return False
            self.applying = False
            summary = ("Fixed: Home Assistant needed a restart to load it (demo: nothing was changed)." if fix
                       else "Restarted Home Assistant. (demo: nothing was changed)")
            self.push({"type": "other", "event": {
                "ts": None, "step": "done", "status": "ok", "pct": 100, "other_id": rid, "summary": summary,
                "other": {"id": rid, "result": "fixed" if fix else "restarted", "item": None, "items": []}}})
            if self._batch is not None:
                GLib.idle_add(self._batch_next)
            return False

        GLib.timeout_add(200, step, 0)

    def cmd_copyText(self, msg):
        """Other software's Copy button (2026-10-31): one line of text onto the GTK clipboard."""
        text = str(msg.get("text") or "").replace("\r", " ").replace("\n", " ").strip()[:500]
        if not text:
            return
        try:
            from gi.repository import Gdk  # noqa: PLC0415 - only needed here
            clip = Gdk.Display.get_default().get_clipboard()
            try:
                clip.set(text)
            except TypeError:
                clip.set_content(Gdk.ContentProvider.new_for_value(text))
        except Exception as exc:  # noqa: BLE001
            self.push_error("could not copy: %s" % exc)

    def cmd_setSetting(self, msg):
        key, value = msg.get("key"), msg.get("value")
        if key not in DEFAULT_SETTINGS:
            log("ignoring unknown setting %r" % key)
            return
        if key == "look" and not (isinstance(value, str) and re.fullmatch(r"[a-z0-9-]{1,32}", value)):
            log("refusing look=%r" % value)
            return
        if key in ("show_containers", "ai_triage") and not isinstance(value, bool):
            log("refusing %s=%r" % (key, value))
            return
        if key == "type_size" and value not in TYPE_ZOOM:
            log("refusing type_size=%r" % value)
            return
        if key == "check_interval_min":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
            if value <= 0:
                log("refusing check_interval_min=%r" % msg.get("value"))
                self.push_error("The check interval must be at least 1 minute.")
                self.push_settings()           # put the saved value back on screen
                return
        self.settings[key] = value
        self.save_settings()
        if key == "check_interval_min":
            self.write_timer_dropin(value)
        self.push_settings()
        if key in ("show_containers", "ai_triage") and value and not self.busy:
            self.cmd_refresh({})           # both only take effect on a check: check now

    # ------------------------------------------------------------ systemd
    def write_timer_dropin(self, minutes):
        """Contract: the app applies the interval by rewriting a timer drop-in."""
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return False
        if minutes <= 0:
            log("refusing a timer interval of %s min" % minutes)
            return False
        unit_dir = os.path.expanduser("~/.config/systemd/user/rivet-collect.timer.d")
        try:
            os.makedirs(unit_dir, exist_ok=True)
            path = os.path.join(unit_dir, "interval.conf")
            body = "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=%dmin\n" % minutes
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# written by Rivet; edit Preferences instead\n" + body)
        except OSError as exc:
            log("could not write timer drop-in: %s" % exc)
            self.push_error("Could not write the timer drop-in: %s" % exc)
            return False
        if not os.path.exists(
            os.path.expanduser("~/.config/systemd/user/rivet-collect.timer")
        ) and not os.path.exists("/usr/lib/systemd/user/rivet-collect.timer"):
            return True  # unit not installed yet; the drop-in will apply when it is
        # ONE shell, sequential: the restart must see the reloaded drop-in. Two
        # concurrent Gio.Subprocess calls raced each other here before.
        systemctl = os.environ.get("RIVET_SYSTEMCTL_BIN") or "systemctl"
        script = "%s --user daemon-reload && %s --user restart rivet-collect.timer" % (
            shlex.quote(systemctl), shlex.quote(systemctl))
        argv = ["bash", "-c", script]
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as exc:
            log("%s: %s" % (script, exc.message))
            self.push_error("The interval is saved, but the timer could not be restarted: %s"
                            % exc.message)
            return False
        self._track(proc)
        # communicate_* drains stderr for us (a bare wait would leave the pipe unread)
        proc.communicate_utf8_async(None, None, self._timer_done, script)
        return True

    def _timer_done(self, proc, res, script):
        try:
            err = ""
            try:
                _ok, _out, err = proc.communicate_utf8_finish(res)
            except GLib.Error as exc:
                log("%s: %s" % (script, exc.message))
            rc, how = self._exit_info(proc)
            if rc != 0:
                tail = (err or "").strip().splitlines()
                tail = tail[-1] if tail else "no error output"
                log("timer restart failed (%s): %s" % (how, tail))
                self.push_error("The interval is saved, but systemctl %s: %s" % (how, tail))
        except Exception:
            log("timer restart handling failed:\n" + traceback.format_exc())
        finally:
            self._release(proc)

    # ---------------------------------------------------- demo event stream
    FAKE = [
        ("download", "start", 0, "resolving", "Resolving updates (dnf5, flatpak, fwupd)…"),
        ("download", "progress", 35, "35% of the payload", "Downloading kernel-7.2.5-200.fc44.x86_64.rpm … done"),
        ("download", "progress", 80, "80% of the payload", "Downloading akmod-nvidia-615.71.09-1.fc44.x86_64.rpm … done"),
        ("download", "ok", 100, "download complete", "Verifying GPG signatures … ok"),
        ("install", "start", 0, "transaction check", "Running transaction check … ok"),
        ("install", "progress", 50, "installing packages", "Installing : kernel-7.2.5-200.fc44.x86_64"),
        ("install", "ok", 100, "transaction complete", "Transaction complete"),
        ("akmod", "start", 0, "please wait, do not reboot yet", "akmods: building NVIDIA 615.71.09 for kernel 7.2.5-200.fc44"),
        ("akmod", "progress", 60, "compiling", "nvidia-kmod: LD [M] nvidia.ko nvidia-drm.ko"),
        ("akmod", "ok", 100, "built and signed", "akmods: kmod-nvidia-7.2.5-200.fc44.x86_64-615.71.09 installed"),
        ("fwupd", "ok", 100, "scheduled for next boot", "fwupd: dbx-20260707 scheduled for next boot"),
        ("verify", "start", 10, "boot entries and module signatures", "Checking /boot/loader/entries … 7.2.5 present"),
        ("verify", "ok", 100, "everything checks out", "modinfo -k 7.2.5-200.fc44.x86_64 nvidia: 615.71.09, signed"),
    ]

    def _fake_apply(self, items):
        seq = list(self.FAKE)
        seq.append(("done", "ok", None, None, None))

        def step(i):
            if i >= len(seq):
                return False
            name, status, pct, msg, logline = seq[i]
            if name == "done":
                ev = {
                    "ts": None, "step": "done", "status": "ok", "reboot_required": True,
                    "kmod_verified": True,
                    "summary": "Demo run: %d items, nothing was actually changed." % len(items),
                }
            else:
                ev = {"ts": None, "step": name, "status": status, "pct": pct,
                      "msg": msg, "log": logline}
            self._forward_event(json.dumps(ev))
            GLib.timeout_add(420, step, i + 1)
            return False

        GLib.timeout_add(250, step, 0)


# ---------------------------------------------------------------- GTK shell
class RivetApp(Adw.Application if HAVE_ADW else Gtk.Application):
    def __init__(self, demo=False):
        # QA only: a driver instance must not hand itself over to an already-running
        # Rivet (the live one owns APP_ID on the session bus).
        # RIVET_QA_UNIQUE=1 (qa-e2e.sh second-instance) keeps the real single-instance behaviour
        # inside the nested session to prove a second launch only raises the first window.
        qa_alone = os.environ.get("RIVET_QA_JS") and os.environ.get("RIVET_QA_UNIQUE") != "1"
        flags = Gio.ApplicationFlags.NON_UNIQUE if qa_alone else Gio.ApplicationFlags.FLAGS_NONE
        super().__init__(application_id=APP_ID, flags=flags)
        self.demo = demo
        self.win = None
        self.webview = None
        self.bridge = None
        self._hidden_while_busy = False

    def do_activate(self):
        if self.win:
            # a second `rivet-updater` (desktop icon, notification, terminal) lands here over D-Bus and
            # exits: one window, one Bridge, one writer of state.json / settings.json
            log("activate: Rivet is already open; raising its window")
            self._hidden_while_busy = False
            self.win.present()
            return
        self.build()
        self.win.present()

    def _on_busy_changed(self, busy):
        """Bridge callback: an operation that outlived a close has finished."""
        if busy or not self._hidden_while_busy or not self.win:
            return
        self._hidden_while_busy = False
        log("operation finished while the window was hidden — bringing it back")
        self.win.present()

    def build(self):
        size = read_json(WINDOW_FILE, {}) or {}
        w = int(size.get("width") or DEFAULT_SIZE[0])
        h = int(size.get("height") or DEFAULT_SIZE[1])

        WindowClass = Adw.ApplicationWindow if HAVE_ADW else Gtk.ApplicationWindow
        self.win = WindowClass(application=self, title="Rivet")
        self.win.set_default_size(max(w, 640), max(h, 480))
        self.win.set_size_request(640, 480)

        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler("rivet", None)
        ucm.connect("script-message-received::rivet", self.on_script_message)

        self.webview = WebKit.WebView(user_content_manager=ucm)
        self.webview.set_background_color(_rgba(0, 0, 0, 0))
        settings = self.webview.get_settings()
        settings.set_property("enable-developer-extras", True)
        settings.set_property("enable-write-console-messages-to-stdout", True)
        settings.set_property("allow-file-access-from-file-urls", True)
        settings.set_property("allow-universal-access-from-file-urls", True)
        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)

        header_cls = Adw.HeaderBar if HAVE_ADW else Gtk.HeaderBar
        header = header_cls()
        header.set_title_widget(Gtk.Label(label=""))
        if HAVE_ADW:
            header.add_css_class("flat")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.webview)
        if HAVE_ADW:
            self.win.set_content(box)
        else:
            self.win.set_child(box)

        self.bridge = Bridge(self.push, confirm=self.confirm, demo=self.demo)
        self.bridge.on_busy_changed = self._on_busy_changed
        self.bridge.start_monitor()
        if HAVE_ADW:
            self._apply_adw_scheme(self.bridge.settings.get("theme", "system"))
        self._apply_type_size(self.bridge.settings.get("type_size", "compact"))

        self.win.connect("close-request", self.on_close)
        qa_js = os.environ.get("RIVET_QA_JS")
        if qa_js:
            self.webview.connect("load-changed", self._on_load_changed, qa_js)
        self.webview.load_uri(Gio.File.new_for_path(INDEX).get_uri())

    def _on_load_changed(self, view, event, qa_js):
        """RIVET_QA_JS=<file.js>: run a driver script in the page after load.

        Test-only. It lets a headless run click through the real UI without a
        pointer (see the Done-button regression check in app/qa-e2e.sh).
        """
        if event != WebKit.LoadEvent.FINISHED:
            return
        try:
            with open(qa_js, "r", encoding="utf-8") as fh:
                js = fh.read()
        except OSError as exc:
            log("RIVET_QA_JS: %s" % exc)
            return
        log("RIVET_QA_JS: running %s" % qa_js)
        try:
            view.evaluate_javascript(js, -1, None, None, None, None, None)
        except TypeError:
            view.evaluate_javascript(js, -1, None, None, None)

    def _apply_type_size(self, size):
        """Type size = WebKit page zoom: every look, its type and its layout scale as one."""
        try:
            self.webview.set_zoom_level(TYPE_ZOOM.get(size, 1.0))
        except Exception as exc:  # pragma: no cover
            log("zoom: %s" % exc)

    def _apply_adw_scheme(self, theme):
        try:
            sm = Adw.StyleManager.get_default()
            sm.set_color_scheme({
                "dark": Adw.ColorScheme.FORCE_DARK,
                "light": Adw.ColorScheme.FORCE_LIGHT,
            }.get(theme, Adw.ColorScheme.DEFAULT))
        except Exception as exc:  # pragma: no cover
            log("style manager: %s" % exc)

    # --------------------------------------------------------- bridge glue
    def push(self, obj):
        if not self.webview:
            return
        payload = json.dumps(json.dumps(obj, ensure_ascii=False))
        js = "window.rivet && window.rivet.receive(JSON.parse(%s));" % payload
        try:
            self.webview.evaluate_javascript(js, -1, None, None, None, None, None)
        except TypeError:
            # older/newer signature: (script, length, world, source, cancellable, cb, data)
            self.webview.evaluate_javascript(js, -1, None, None, None)

    def on_script_message(self, _ucm, value):
        # WebKit 6.0 hands over a JSCValue; older bindings wrap it.
        if hasattr(value, "get_js_value"):
            value = value.get_js_value()
        try:
            raw = value.to_string()
        except Exception as exc:
            log("bad script message: %r" % exc)
            return
        if not self.bridge:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = {}
        if HAVE_ADW and msg.get("cmd") == "setSetting" and msg.get("key") == "theme":
            self._apply_adw_scheme(msg.get("value"))
        if msg.get("cmd") == "setSetting" and msg.get("key") == "type_size" and msg.get("value") in TYPE_ZOOM:
            self._apply_type_size(msg.get("value"))
        if msg.get("cmd") == "qaShot":
            self._qa_shot(str(msg.get("name") or "shot"))
            return
        self.bridge.handle(raw)

    def _qa_shot(self, name):
        """QA only (RIVET_QA_JS + RIVET_QA_SHOTS=<dir>): PNG of the real WebKit view."""
        outdir = os.environ.get("RIVET_QA_SHOTS")
        if not (outdir and os.environ.get("RIVET_QA_JS")):
            return
        path = os.path.join(outdir, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".png")

        def got(view, res, _u=None):
            try:
                tex = view.get_snapshot_finish(res)
                tex.save_to_png(path)
                log("qa shot: %s" % path)
            except Exception as exc:
                log("qa shot failed: %r" % exc)
        self.webview.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE,
                                  None, got)

    def confirm(self, text, detail, cb):
        if hasattr(Gtk, "AlertDialog"):
            dlg = Gtk.AlertDialog()
            dlg.set_message(text)
            dlg.set_detail(detail)
            dlg.set_buttons(["Cancel", "Restart"])
            dlg.set_cancel_button(0)
            dlg.set_default_button(1)

            def done(d, res, _u=None):
                try:
                    cb(d.choose_finish(res) == 1)
                except GLib.Error:
                    cb(False)

            dlg.choose(self.win, None, done)
            return
        dlg = Gtk.MessageDialog(  # pragma: no cover - GTK < 4.10 fallback
            transient_for=self.win, modal=True, text=text, secondary_text=detail,
            buttons=Gtk.ButtonsType.OK_CANCEL,
        )
        dlg.connect("response", lambda d, r: (cb(r == Gtk.ResponseType.OK), d.destroy()))
        dlg.present()

    def on_close(self, win):
        try:
            write_json_atomic(WINDOW_FILE, {
                "width": win.get_width() or DEFAULT_SIZE[0],
                "height": win.get_height() or DEFAULT_SIZE[1],
            })
        except OSError as exc:
            log("could not save window size: %s" % exc)
        if self.bridge and self.bridge.busy:
            # Quitting now would close the stdout pipe of a running rivet-apply (or
            # rivet-doctor fix) mid-transaction. Hide instead: the app stays alive,
            # the window comes back by itself when the operation ends (or on the
            # next launch, which activates this instance).
            log("close while busy: hiding the window, the operation keeps running")
            self._hidden_while_busy = True
            win.set_visible(False)
            return True
        return False


def _rgba(r, g, b, a):
    from gi.repository import Gdk

    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    demo = "--demo" in argv
    argv = [a for a in argv if a != "--demo"]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if HAVE_ADW:
        Adw.init()
    app = RivetApp(demo=demo)
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
