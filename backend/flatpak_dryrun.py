#!/usr/bin/env python3
"""Ask libflatpak what `flatpak update` would do, without doing it.

    python3 flatpak_dryrun.py          -> JSON on stdout

`flatpak remote-ls --updates` only lists installed refs that have a newer commit.
`flatpak update` (and GNOME Software) do more: they INSTALL related refs that are
now wanted and missing, and UNINSTALL autopruned ones. The case that bit on
2026-09-21: the host NVIDIA driver went 610.57.04 -> 615.71.09 with a reboot, so
every runtime suddenly wanted org.freedesktop.Platform.GL.nvidia-615-71-09, which
is not an "update" of anything. remote-ls said nothing, Rivet said "all up to
date", GNOME Software listed four runtimes.

So: build the same FlatpakTransaction the CLI builds (update every installed ref,
system installs as dependency sources, unused/autoprune uninstall ops), let it
resolve, record the operations in the `ready` signal and return False there, which
aborts before anything is downloaded or deployed. Metadata reads only.

Runs as its own process so a libflatpak hang or crash costs the collector one
timeout, not the whole run. Output:
  {"ok": true, "ops": [{"scope","op","ref","remote","download_bytes","installed_bytes"}]}
"""
from __future__ import annotations

import json
import sys


def dry_run() -> dict:
    import gi

    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak, GLib

    ops: list[dict] = []
    errors: list[str] = []
    for scope, factory in (("user", Flatpak.Installation.new_user),
                           ("system", Flatpak.Installation.new_system)):
        try:
            inst = factory(None)
            tx = Flatpak.Transaction.new_for_installation(inst, None)
            tx.set_no_interaction(True)
            tx.add_default_dependency_sources()
            tx.set_include_unused_uninstall_ops(True)
            for ref in inst.list_installed_refs(None):
                try:
                    tx.add_update(ref.format_ref(), None, None)
                except GLib.Error:
                    pass  # remote disabled/gone: the CLI skips these too

            def on_ready(t, _scope=scope):
                for op in t.get_operations():
                    ops.append({
                        "scope": _scope,
                        "op": op.get_operation_type().value_nick,  # install|update|uninstall|install-bundle
                        "ref": op.get_ref(),
                        "remote": op.get_remote(),
                        "download_bytes": int(op.get_download_size() or 0),
                        "installed_bytes": int(op.get_installed_size() or 0),
                    })
                return False  # abort: nothing is downloaded, nothing is deployed

            tx.connect("ready", on_ready)
            try:
                tx.run(None)
            except GLib.Error as exc:
                if not exc.matches(Flatpak.error_quark(), Flatpak.Error.ABORTED):
                    errors.append(f"{scope}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - report, never crash the collector
            errors.append(f"{scope}: {exc}")
    return {"ok": not errors, "errors": errors, "ops": ops}


if __name__ == "__main__":
    try:
        json.dump(dry_run(), sys.stdout)
    except Exception as exc:  # noqa: BLE001
        json.dump({"ok": False, "errors": [str(exc)], "ops": []}, sys.stdout)
    sys.stdout.write("\n")
