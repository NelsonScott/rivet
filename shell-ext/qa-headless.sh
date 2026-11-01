#!/bin/bash
# Visual QA for rivet@scottnelson.xyz without touching the live session.
#
#   RIVET_STATE=fixtures/state-13.json timeout 120 dbus-run-session -- bash qa-headless.sh /tmp/qa
#
# Starts a headless nested gnome-shell (`gnome-shell --devkit`
# was broken on the dev box), waits for our enable() line, opens the
# indicator menu over D-Bus and screenshots from inside the shell with Shell.Screenshot.
# Produces $OUT/menu.png (menu open) and $OUT/bar.png (panel only).
set -u
OUT=${1:-/tmp/rivet-qa}
mkdir -p "$OUT"

gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720 > "$OUT/shell.log" 2>&1 &
SH=$!
for _ in $(seq 1 40); do sleep 1; grep -q '\[rivet\] enable' "$OUT/shell.log" && break; done
sleep 4

call() { gdbus call --session --dest org.gnome.Shell \
    --object-path /xyz/scottnelson/Rivet --method "xyz.scottnelson.Rivet.$@"; }

call Reload
sleep 1
call Snap "$OUT/bar.png"
sleep 2
call OpenMenu
sleep 2
call Snap "$OUT/menu.png"
sleep 3

kill "$SH" 2>/dev/null; wait "$SH" 2>/dev/null
grep -E '\[rivet\]' "$OUT/shell.log" || true
