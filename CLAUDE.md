# Rivet: agent notes

A Fedora update app for GNOME: one list for dnf, Flatpak and firmware, each update
rated green / yellow / red with a one-line reason, plus the other software on the
box (npm, pipx, uv, cargo, GNOME extensions, Home Assistant, containers). The
contract between the parts is `CONTRACT.md`; read it before changing a file format,
a bridge command or an event.

## Stack

- Backend: Python 3 stdlib only (`backend/`), plus PyGObject for dnf5daemon (D-Bus)
  and libflatpak. CLIs `rivet-collect`, `rivet-apply`, `rivet-doctor`; hourly
  `rivet-collect.timer` (systemd user unit) writes `~/.local/state/rivet/state.json`.
- App: GTK4 + WebKitGTK 6.0 via PyGObject (`app/rivet_app.py`), the page is plain
  HTML/CSS/JS in `app/ui/` (no build step). App id `xyz.scottnelson.Rivet`, launcher
  `rivet-updater`.
- Shell extension `shell-ext/rivet@scottnelson.xyz`: top-bar badge (GNOME Shell 50).
- AI: the Claude Code CLI (`claude -p`) on the user's subscription, never an API
  key. Triage, "what is this" lines and Fix it all degrade to nothing when `claude`
  is missing.
- Root only through dnf5daemon's polkit action, flatpak-system-helper, and `pkexec`
  for three named cases (akmods retry, a Fix it root step, a held rollback).
  `test_abuse.Privilege` pins the call sites.

## Commands

```sh
cd backend && python3 -m unittest discover -s tests     # backend, ~30 s
python3 app/selftest.py                                 # the bridge, no window
python3 app/qa/looks-contrast.py                        # look tokens >= 4.5:1
timeout 900 dbus-run-session -- bash app/qa-e2e.sh /tmp/rivet-e2e [scenario ...]   # real app, nested headless shell
./install.sh && app/install.sh                          # user scope install (no sudo)
rivet-updater --demo                                    # serves app/ui/sample-state.json when there is no state
```

## QA rules ("it compiles" is not "it works")

1. Backend tests, selftest and looks-contrast clean before any commit; CI runs the
   first two in a Fedora container (`.github/workflows/ci.yml`).
2. UI changes: run the matching `qa-e2e.sh` scenario (nested headless gnome-shell,
   scratch HOME, fake apply/collect/doctor/claude) and LOOK at the PNGs in
   `$OUT/shots`, light and dark. Never test against the live session or a real
   package transaction.
3. Every failure mode gets a fake first (`backend/fixtures`, `app/qa/fake-*.py`);
   `docs/QA-MATRIX.md` lists the scenarios and what each showed.
4. Nothing personal in the tree: `test_abuse.PersonalResidue` scans every tracked
   file. Machine-specific entries go in the git-ignored `backend/know.local.json`.

## Design notes worth knowing

- `state.json` is additive: readers ignore unknown groups, flags and message types.
  Every write is tmp + fsync + rename.
- Risk colour is deterministic first (`backend/rules.py`); the model may raise a
  colour, never lower it.
- Kernel + akmod-nvidia: Rivet waits for the module build and verifies it before it
  offers a reboot (`kmod_verified`).
- Other software rows are rebuilt from state.json on apply; the UI never sends a
  command line. Containers that hold data never get an Update button.
- A newer Fedora (`backend/release.py`) is one unticked dnf row; rivet-apply only
  downloads it, last; `--release-reboot` is the one command that restarts the machine.
- Looks (`app/ui/looks/`) only re-point CSS custom properties; see its README.
