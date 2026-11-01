"""Shared helpers for the Rivet backend (stdlib only)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCHEMA = 1

STATE_DIR = Path(os.environ.get("RIVET_STATE_DIR", Path.home() / ".local/state/rivet"))
LOG_DIR = STATE_DIR / "logs"
STATE_PATH = STATE_DIR / "state.json"
TRIAGE_CACHE_PATH = STATE_DIR / "triage-cache.json"
LAST_SET_PATH = STATE_DIR / "last-pending.json"
CONFIG_DIR = Path(os.environ.get("RIVET_CONFIG_DIR", Path.home() / ".config/rivet"))
SETTINGS_PATH = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "check_interval_min": 60,
    "notify": True,
    "red_includes_reboot": False,
    "theme": "system",
    "show_containers": False,      # 2026-11-01: the podman / Containers section is off by default
    # Claude Code (triage reasons, "what is this" lines) is opt-in: nothing is sent to
    # Anthropic unless the user turns this on in Preferences. Fix it is a button, always explicit.
    "ai_triage": False,
}


def now_iso() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text()))
    except Exception:
        pass
    return s


def write_json_atomic(path: Path, obj) -> None:
    """tmp + fsync + rename, so readers never see a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- logs

LOG_MAX_BYTES = 2_000_000       # apply.log was 430 KB and collect.log 227 KB after one week


def rotate_log(path: Path) -> bool:
    """`name.log` -> `name.log.1` once it passes LOG_MAX_BYTES; one generation is kept.

    Called before a log is opened for a run, so a run's events are never split across
    two files (the doctor reads "the last run" from apply.log)."""
    try:
        if path.stat().st_size < LOG_MAX_BYTES:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
        return True
    except OSError:
        return False


def open_log(name: str):
    """Append handle on LOG_DIR/<name> (rotated first), or None when that fails."""
    try:
        ensure_dirs()
        path = LOG_DIR / name
        rotate_log(path)
        return open(path, "a")
    except Exception:
        return None


def log(msg: str, *, quiet: bool = False, stream=sys.stderr) -> None:
    line = f"[{now_iso()}] {msg}"
    if not quiet:
        print(line, file=stream, flush=True)
    try:
        fh = open_log("collect.log")
        if fh:
            with fh:
                fh.write(line + "\n")
    except Exception:
        pass


def emit_line(sink, line: str) -> None:
    """Write one event line to sink.stream and mirror it to sink.logfh.

    The reader of stdout is the app window. When it is closed mid-run the pipe breaks and
    the write raises BrokenPipeError (Python ignores SIGPIPE, so the error surfaces as an
    exception, which is what we want). The first failure flips sink.stdout_gone, the fd is
    pointed at /dev/null so the interpreter's exit-time flush stays quiet, and the run goes
    on: every later event still reaches the log file, including `done`."""
    if not getattr(sink, "stdout_gone", False):
        try:
            sink.stream.write(line + "\n")
            sink.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            sink.stdout_gone = True
            _silence_stream(sink.stream)
            if sink.logfh:
                try:
                    sink.logfh.write(json.dumps({"ts": now_iso(), "step": "log", "status": "note",
                                                 "msg": "stdout closed (reader went away); "
                                                        "continuing, events go to this log only"}) + "\n")
                except OSError:
                    pass
    if sink.logfh:
        try:
            sink.logfh.write(line + "\n")
            sink.logfh.flush()
        except OSError:
            pass


def _silence_stream(stream) -> None:
    try:
        fd = stream.fileno()
    except Exception:
        return
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, fd)
        os.close(null)
    except OSError:
        pass


# ---------------------------------------------------------------- subprocesses

# Every child started through run() while it is alive, so an interrupt handler can
# take them down with us (a hand-run collector killed with SIGTERM must not leave a
# dnf5 behind; under systemd the cgroup kill does this anyway).
_CHILDREN: set = set()
_CHILDREN_LOCK = threading.Lock()


def kill_children(grace: float = 2.0) -> None:
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + grace
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass


def kill_and_wait(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """kill() and reap, so a timed-out child never lingers as a zombie."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


def run(cmd: list[str], *, timeout: int = 120, check: bool = False, env: dict | None = None):
    """Run a command, never raising on non-zero unless check=True.

    stdin is /dev/null: a hand-run collector must never sit on an fwupdmgr or dnf
    prompt. A timeout kills AND reaps the child and comes back as rc 124."""
    if shutil.which(cmd[0]) is None:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    e = dict(os.environ)
    e.setdefault("LC_ALL", "C")
    if env:
        e.update(env)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=e)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {exc}")
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_and_wait(proc)
            try:
                out, _err = proc.communicate(timeout=5)
            except Exception:
                out = ""
            return subprocess.CompletedProcess(cmd, 124, out or "", f"timeout after {timeout}s")
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(proc)
    cp = subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, cp.stdout, cp.stderr)
    return cp


class LineStream:
    """A child whose output lines are pulled by a thread, so a wall-clock deadline holds
    even while the child is silent.

    `for raw in proc.stdout` only notices the clock when a line arrives: a flatpak or
    fwupdmgr waiting on a polkit prompt, or a stalled claude, hung rivet-apply forever.
    Here the deadline is checked every second whatever the child does; on expiry the child
    is killed AND reaped, `timed_out` is True and iteration stops. stderr is merged into
    stdout (a separate pipe that nobody drains can block the child), stdin is /dev/null.
    """

    def __init__(self, cmd: list[str], *, timeout: float, env: dict | None = None,
                 cwd: str | None = None):
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, cwd=cwd)
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.timed_out = False
        self._q: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self._q.put(line)
        except Exception:
            pass
        finally:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            self._q.put(None)

    def lines(self):
        """Yield raw lines until EOF or the deadline; the child is reaped before returning."""
        idle_after_exit = 0
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.timed_out = True
                kill_and_wait(self.proc)
                return
            try:
                item = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                # exited, but a grandchild still holds the pipe: do not wait for it
                if self.proc.poll() is not None:
                    idle_after_exit += 1
                    if idle_after_exit >= 2:
                        return
                continue
            if item is None:
                break
            yield item
        self.wait()

    def wait(self, timeout: float = 30.0) -> int | None:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_and_wait(self.proc)
        return self.proc.returncode

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode


def excerpt(text: str | None, limit: int = 600) -> str:
    """Flatten an advisory/changelog blob into <= limit chars of plain text."""
    if not text:
        return ""
    import html
    import re

    t = re.sub(r"<li>", "\n- ", text)
    t = re.sub(r"</p>\s*<p>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def human_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---------------------------------------------------------------- root


def root_prefix() -> list[str]:
    """How to run one command as root from here, as argv to put in front of it.

    Nothing when already root. `sudo -n` when that works without a password (it never
    prompts, so it never hangs a GUI child). Otherwise `pkexec`: the desktop's polkit
    dialog asks the user (password, or whatever PAM is set up for polkit), exactly like a
    package install from GNOME Software. Without a NOPASSWD sudo rule, pkexec is the path that
    actually runs."""
    if os.geteuid() == 0:
        return []
    if run(["sudo", "-n", "true"], timeout=10).returncode == 0:
        return ["sudo", "-n"]
    return ["pkexec"]


def root_refusal(prefix: list[str], rc: int, last_line: str = "") -> str | None:
    """Why a root-prefixed command never ran, or None when the exit code is the command's own.

    pkexec: 126 = the dialog was dismissed, 127 = not authorized (or the program is missing).
    sudo -n: 1 plus its "a password is required" line."""
    if prefix[:1] == ["pkexec"]:
        if rc == 126:
            return "the permission prompt was dismissed, so nothing ran"
        if rc == 127:
            return "permission was refused" + (f" ({last_line[:160]})" if last_line else "")
        return None
    if prefix[:1] == ["sudo"] and rc == 1 and re.search(
            r"^sudo: .*(password|a terminal|not allowed|may not run)", last_line, re.I):
        return f"was refused by sudo -n ({last_line[:160]})"
    return None
