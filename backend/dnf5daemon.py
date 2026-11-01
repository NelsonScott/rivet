"""Minimal dnf5daemon client (org.rpm.dnf.v0) over the SYSTEM bus.

Privilege comes from polkit: `org.rpm.dnf.v0.rpm.execute_transaction` is
auth_admin_keep, so the user gets ONE authentication dialog per apply run. We never
shell out to sudo and never write to the rpmdb ourselves.

Flow:  open_session -> rpm.upgrade(specs) -> goal.resolve -> goal.do_transaction
Progress arrives as signals on the session object path while a GLib main loop
runs; every signal is translated into a Rivet event by the `on_event` callback.

API discovered with `busctl introspect org.rpm.dnf.v0 /org/rpm/dnf/v0` and the
shipped XML in /usr/share/dbus-1/interfaces/org.rpm.dnf.v0.*.xml.
"""
from __future__ import annotations

import glob
import re

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.rpm.dnf.v0"
ROOT_PATH = "/org/rpm/dnf/v0"
I_SESSION = "org.rpm.dnf.v0.SessionManager"
I_RPM = "org.rpm.dnf.v0.rpm.Rpm"
I_GOAL = "org.rpm.dnf.v0.Goal"
I_BASE = "org.rpm.dnf.v0.Base"
I_REPO = "org.rpm.dnf.v0.rpm.Repo"

# Goal.resolve result codes (libdnf5 GoalProblem): 0 clean, 1 advisory/info, >=2 fatal.
RESOLVE_OK = 0
RESOLVE_WARN = 1

TX_ACTIONS = {
    1: "install", 2: "upgrade", 3: "downgrade", 4: "reinstall",
    5: "remove", 6: "replaced", 7: "reason change",
}


class DnfError(RuntimeError):
    pass


def configured_key_urls() -> set[str]:
    """Every gpgkey= URL in /etc/yum.repos.d: the keys the admin already vouched for."""
    urls: set[str] = set()
    for path in glob.glob("/etc/yum.repos.d/*.repo"):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r"^\s*gpgkey\s*=\s*(.+)$", text, re.M):
            for u in re.split(r"[\s,]+", m.group(1)):
                if u.strip():
                    urls.add(u.strip())
                    urls.add(_substitute(u.strip()))
    return urls


def _substitute(url: str) -> str:
    """$basearch / $releasever the way libdnf5 spells them in the signal."""
    rel = ""
    try:
        for line in open("/etc/os-release", encoding="utf-8"):
            if line.startswith("VERSION_ID="):
                rel = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    import platform
    return (url.replace("$basearch", platform.machine()).replace("$arch", platform.machine())
            .replace("$releasever", rel))


class DnfDaemon:
    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda *a, **k: None)
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session: str | None = None
        self._subs: list[int] = []
        self._dl_total: dict[str, int] = {}
        self._dl_done: dict[str, int] = {}
        self._dl_names: dict[str, str] = {}
        self._last_dl_pct = -1
        self._tx_total = 0
        self._loop: GLib.MainLoop | None = None
        self._tx_ok: bool | None = None
        self._tx_error: str | None = None
        self._replied = False
        # Every real failure reason seen on the wire, oldest first: scriptlet and
        # unpack errors, failed downloads, the D-Bus error of do_transaction.
        # rivet-apply puts the first one in front of the user.
        self.errors: list[str] = []
        # Repo keys the daemon asked about during this session (key_id -> confirmed?).
        self.key_requests: dict[str, bool] = {}

    # ---------------------------------------------------------------- plumbing

    def _call(self, iface: str, method: str, params: GLib.Variant | None,
              reply: str | None, *, path: str | None = None, timeout: int = 60_000):
        try:
            res = self.bus.call_sync(
                BUS_NAME,
                path or self.session or ROOT_PATH,
                iface,
                method,
                params,
                GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NONE,
                timeout,
                None,
            )
        except GLib.Error as exc:
            raise DnfError(f"{iface}.{method}: {exc.message}") from exc
        return res.unpack() if res is not None else None

    # NOTE: an a{sv} member of a tuple format string must be a PLAIN dict whose
    # values are GLib.Variant -- passing an already-built GLib.Variant("a{sv}")
    # makes GLib try to iterate it as a mapping and raises KeyError: 0.

    # ---------------------------------------------------------------- session

    def open(self) -> str:
        opts = {
            "load_system_repo": GLib.Variant("b", True),
            "load_available_repos": GLib.Variant("b", True),
        }
        (path,) = self._call(I_SESSION, "open_session", GLib.Variant("(a{sv})", (opts,)),
                             "(o)", path=ROOT_PATH, timeout=180_000)
        self.session = path
        self._subscribe()
        return path

    def close(self) -> None:
        for sid in self._subs:
            try:
                self.bus.signal_unsubscribe(sid)
            except Exception:
                pass
        self._subs.clear()
        if self.session:
            try:
                self._call(I_SESSION, "close_session",
                           GLib.Variant("(o)", (self.session,)), "(b)", path=ROOT_PATH)
            except DnfError:
                pass
            self.session = None

    def _subscribe(self) -> None:
        for iface, signal, handler in (
            (I_BASE, "download_add_new", self._on_dl_new),
            (I_BASE, "download_progress", self._on_dl_progress),
            (I_BASE, "download_end", self._on_dl_end),
            (I_BASE, "repo_key_import_request", self._on_key_request),
            (I_RPM, "transaction_before_begin", self._on_tx_begin),
            (I_RPM, "transaction_action_start", self._on_action_start),
            (I_RPM, "transaction_elem_progress", self._on_elem_progress),
            (I_RPM, "transaction_script_error", self._on_script_error),
            (I_RPM, "transaction_unpack_error", self._on_unpack_error),
            (I_RPM, "transaction_verify_progress", self._on_verify_progress),
            (I_RPM, "transaction_after_complete", self._on_tx_complete),
        ):
            sid = self.bus.signal_subscribe(
                BUS_NAME, iface, signal, None, None,
                Gio.DBusSignalFlags.NONE,
                self._wrap(handler),
            )
            self._subs.append(sid)

    def _wrap(self, handler):
        def cb(_conn, _sender, _path, _iface, _signal, params):
            args = params.unpack()
            # every signal carries the session object path as arg0
            if self.session and args and args[0] != self.session:
                return
            try:
                handler(*args[1:])
            except Exception as exc:  # a bad signal must never abort the apply
                self.on_event("install", "progress", None, f"(signal error: {exc})", None)
        return cb

    # ---------------------------------------------------------------- signals

    def _on_dl_new(self, download_id, description, total):
        self._dl_total[download_id] = max(int(total or 0), 0)
        self._dl_names[download_id] = description
        self.on_event("download", "progress", self._dl_pct(), f"Fetching {description}", None)

    def _on_dl_progress(self, download_id, total, downloaded):
        self._dl_total[download_id] = max(int(total or 0), 0)
        self._dl_done[download_id] = max(int(downloaded or 0), 0)
        pct = self._dl_pct()
        if pct is not None and pct != self._last_dl_pct:
            self._last_dl_pct = pct
            self.on_event("download", "progress", pct, f"Downloading packages… {pct}%", None)

    def _on_dl_end(self, download_id, status, message):
        name = self._dl_names.get(download_id, download_id)
        if int(status or 0) != 0:
            self.errors.append(f"download failed: {name}: {message}")
            self.on_event("download", "fail", None, f"{name}: {message}", message)

    def _dl_pct(self):
        tot = sum(self._dl_total.values())
        if tot <= 0:
            return None
        return max(0, min(100, int(100 * sum(self._dl_done.values()) / tot)))

    def _on_key_request(self, key_id, user_ids, fingerprint, key_url, _ts):
        """A repo with repo_gpgcheck=1 whose key the daemon has never seen (a vendor repo,
        2026-09-22). The daemon blocks resolve() until confirm_key answers, so an
        unanswered request is a hang. Trust exactly what `dnf -y` trusts: a key served
        from a gpgkey= URL written in /etc/yum.repos.d by the admin. Anything else is
        refused, which makes the daemon fail the repo load with a clear error."""
        trusted = key_url in configured_key_urls()
        self.key_requests[key_id] = trusted
        who = ", ".join(user_ids) if user_ids else key_id
        if trusted:
            self.on_event("download", "progress", None,
                          f"Importing the signing key for a new repository ({who})…",
                          f"key {key_id} {fingerprint} from {key_url}")
        else:
            msg = (f"Refused an unexpected repository signing key ({who}, {key_url}): "
                   "it is not listed in any /etc/yum.repos.d file.")
            self.errors.append(msg)
            self.on_event("download", "progress", None, msg, f"key {key_id} {fingerprint}")
        # Async: this runs inside the main loop that resolve() is pumping.
        self.bus.call(
            BUS_NAME, self.session, I_REPO, "confirm_key",
            GLib.Variant("(sb)", (key_id, trusted)),
            None, Gio.DBusCallFlags.NONE, 300_000, None, self._on_key_confirmed, key_id,
        )

    def _on_key_confirmed(self, _src, res, key_id):
        try:
            self.bus.call_finish(res)
        except GLib.Error as exc:   # polkit denied / dismissed (confirm_key is auth_admin)
            msg = f"Could not import repository key {key_id}: {exc.message}"
            self.errors.append(msg)
            self.on_event("download", "progress", None, msg, None)

    def _on_tx_begin(self, total):
        self._tx_total = int(total or 0)
        self.on_event("install", "start", 0, f"Installing {self._tx_total} packages…", None)

    def _on_action_start(self, nevra, action, total):
        verb = TX_ACTIONS.get(int(action or 0), "processing")
        self.on_event("install", "progress", None, f"{verb.capitalize()} {nevra}", nevra)

    def _on_elem_progress(self, nevra, processed, total):
        tot = int(total or 0) or self._tx_total
        if tot > 0:
            pct = max(0, min(100, int(100 * int(processed or 0) / tot)))
            self.on_event("install", "progress", pct, f"{nevra}", nevra)

    # Scriptlet/unpack errors are reported as progress, not as a failed step: rpm
    # treats most scriptlet failures as non-critical and the package still lands.
    # rivet-apply decides ok/fail afterwards by checking the rpmdb (2026-09-21).
    def _on_script_error(self, nevra, scriptlet_type, return_code):
        msg = f"scriptlet failed for {nevra} (type {scriptlet_type}, rc={return_code})"
        self.errors.append(msg)
        self.on_event("install", "progress", None, msg, msg)

    def _on_unpack_error(self, nevra):
        msg = f"unpack error for {nevra}"
        self.errors.append(msg)
        self.on_event("install", "progress", None, msg, msg)

    def _on_verify_progress(self, processed, total):
        tot = int(total or 0)
        if tot > 0:
            pct = max(0, min(100, int(100 * int(processed or 0) / tot)))
            self.on_event("install", "progress", pct, f"Verifying… {pct}%", None)

    REPLY_GRACE_S = 5

    def _on_tx_complete(self, success):
        self._tx_ok = bool(success)
        if not (self._loop and self._loop.is_running()):
            return
        if self._tx_ok or self._replied:
            self._loop.quit()
            return
        # Failure: the method reply usually follows this signal and carries the
        # error text. Quitting right here (the old behaviour) threw it away and
        # left the user with a bare "RPM transaction failed."
        def give_up():
            if self._loop and self._loop.is_running():
                self._loop.quit()
            return False
        GLib.timeout_add_seconds(self.REPLY_GRACE_S, give_up)

    # ---------------------------------------------------------------- goal

    def upgrade(self, specs: list[str]) -> None:
        self._call(
            I_RPM, "upgrade",
            GLib.Variant("(asa{sv})", (specs, {})),
            None, timeout=300_000,
        )

    # resolve() items are (object_type, action, reason, extra{}, package{}) --
    # the PACKAGE attrs (full_nevra, download_size, name, evr, repo_id, ...) are
    # the LAST dict, index 4; index 3 only carries "replaces".
    PKG = 4

    def expire_cache(self) -> bool:
        """Mark every repo's cached metadata expired so the next load re-syncs it.

        dnf5daemon-server keeps its OWN cache (/var/cache/dnf5daemon-server), not the
        one `dnf5 check-upgrade` fills for the collector (~/.cache/libdnf5). On
        2026-09-22 the daemon's copy of `updates` was three hours behind, did not know
        kernel-7.2.6 existed, and resolved the plan to nothing. Base.clean("expire-cache")
        needs no polkit action (verified unprivileged that night) and only costs a
        metalink sync check per repo, like any plain `dnf` run.
        """
        ok, err = self._call(I_BASE, "clean", GLib.Variant("(s)", ("expire-cache",)),
                             "(bs)", timeout=120_000)
        if not ok:
            self.errors.append(f"could not expire the metadata cache: {err}")
        return bool(ok)

    def resolve(self) -> tuple[list, int]:
        """Goal.resolve, pumping signals meanwhile.

        Loading fresh metadata happens inside this call (a minute or two after an
        expire), and a repo_key_import_request emitted during it must be ANSWERED or
        the daemon waits forever -- so this is an async call under a main loop, not
        call_sync, which would never deliver the signal.
        """
        loop = GLib.MainLoop()
        out: dict = {}

        def on_done(_src, res, _ud):
            try:
                out["reply"] = self.bus.call_finish(res)
            except GLib.Error as exc:
                out["error"] = exc.message
            if loop.is_running():
                loop.quit()

        self.bus.call(
            BUS_NAME, self.session, I_GOAL, "resolve",
            GLib.Variant("(a{sv})", ({"allow_erasing": GLib.Variant("b", False)},)),
            GLib.VariantType("(a(sssa{sv}a{sv})u)"), Gio.DBusCallFlags.NONE, 600_000, None,
            on_done, None,
        )
        loop.run()
        if "error" in out:
            raise DnfError(f"{I_GOAL}.resolve: {out['error']}")
        items, result = out["reply"].unpack()
        return items, int(result)

    def problems(self) -> list[str]:
        try:
            (probs,) = self._call(I_GOAL, "get_transaction_problems_string", None, "(as)")
            return list(probs)
        except DnfError:
            return []

    def do_transaction(self, *, comment: str = "Rivet", timeout_s: int = 7200) -> bool:
        """Run the staged transaction, pumping signals until it completes.

        polkit shows ONE dialog here (execute_transaction / auth_admin_keep).
        """
        self._loop = GLib.MainLoop()
        self._tx_ok = None
        self._tx_error = None
        self._replied = False
        opts = {"comment": GLib.Variant("s", comment)}

        def on_done(_src, res, _ud):
            self._replied = True
            try:
                self.bus.call_finish(res)
            except GLib.Error as exc:
                self._tx_error = exc.message
                self._tx_ok = False
            # A clean reply before transaction_after_complete: keep waiting for
            # the signal (on_owner_changed below covers a daemon that died).
            if self._loop.is_running() and self._tx_ok is not None:
                self._loop.quit()

        def on_owner_changed(_c, _s, _p, _i, _sig, params):
            # The daemon went away mid-transaction (2026-09-21: systemd restarted
            # dnf5daemon-server because the transaction upgraded it). No further
            # signal can arrive for this session, so stop waiting. The caller
            # verifies the rpmdb instead of trusting this status.
            if self._tx_ok is None:
                self._tx_ok = False
                self._tx_error = self._tx_error or (
                    "dnf5daemon-server restarted during the transaction")
            if self._loop.is_running():
                self._loop.quit()

        watch = self.bus.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
            "/org/freedesktop/DBus", BUS_NAME, Gio.DBusSignalFlags.NONE,
            on_owner_changed,
        )

        self.bus.call(
            BUS_NAME, self.session, I_GOAL, "do_transaction",
            GLib.Variant("(a{sv})", (opts,)),
            None, Gio.DBusCallFlags.NONE, timeout_s * 1000, None, on_done, None,
        )

        def bail():
            self._tx_error = self._tx_error or f"timed out after {timeout_s}s"
            self._tx_ok = False
            if self._loop.is_running():
                self._loop.quit()
            return False

        tid = GLib.timeout_add_seconds(timeout_s + 60, bail)
        self._loop.run()
        GLib.source_remove(tid)
        self.bus.signal_unsubscribe(watch)
        if self._tx_error:
            self.errors.append(self._tx_error)
            raise DnfError(self._tx_error)
        return bool(self._tx_ok)
