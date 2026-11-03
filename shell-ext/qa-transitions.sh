#!/bin/bash
# Exercises the Gio.FileMonitor path end to end in one headless shell:
#   no state.json -> status:checking -> malformed JSON -> 13 pending -> 0 pending
# and snapshots each step. Nothing is written to the real state dir.
#
#   timeout 200 dbus-run-session -- bash qa-transitions.sh /tmp/rivet-qa-t
set -u
. "$(dirname "$(readlink -f "$0")")/../app/qa/private-runtime.sh"   # own runtime dir: keeps the live document portal
OUT=${1:-/tmp/rivet-qa-transitions}
FIX=$(cd "$(dirname "$0")/fixtures" && pwd)
mkdir -p "$OUT/state"
export RIVET_STATE="$OUT/state/state.json"
rm -f "$RIVET_STATE"

gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720 > "$OUT/shell.log" 2>&1 &
SH=$!
for _ in $(seq 1 40); do sleep 1; grep -q '\[rivet\] enable' "$OUT/shell.log" && break; done
sleep 4

call() { gdbus call --session --dest org.gnome.Shell \
    --object-path /xyz/scottnelson/Rivet --method "xyz.scottnelson.Rivet.$@" >/dev/null; }

snap() { sleep 2; call Snap "$OUT/$1.png"; sleep 2; }

snap 1-missing
for step in 2-checking:state-checking 3-malformed:state-malformed 4-13:state-13 5-empty:state-empty; do
    name=${step%%:*}; fixture=${step##*:}
    # atomic tmp+rename, exactly how rivet-collect writes it
    cp "$FIX/$fixture.json" "$RIVET_STATE.tmp" && mv "$RIVET_STATE.tmp" "$RIVET_STATE"
    snap "$name"
done

call OpenMenu
snap 6-menu-empty

kill "$SH" 2>/dev/null; wait "$SH" 2>/dev/null
grep -E '\[rivet\]' "$OUT/shell.log" || true
