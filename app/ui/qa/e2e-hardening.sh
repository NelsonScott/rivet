#!/bin/bash
# UI-hardening end-to-end check (2026-10-04), same machinery as app/qa-e2e.sh: a nested headless
# gnome-shell, the real app with a scratch HOME and the fake backends, and
# app/ui/qa/driver-hardening.js clicking through the page. Never touches the live session.
#
#   timeout 200 dbus-run-session -- bash app/ui/qa/e2e-hardening.sh /tmp/rivet-e2e-hardening
#
# Pass = DRIVER DONE, no "FAIL" expectation, exactly one "apply:" line in the app log for the
# double-clicked Update now, and the app idle afterwards (< 30 ticks in 3 s).
set -u
OUT=${1:-/tmp/rivet-e2e-hardening}; mkdir -p "$OUT/shots"
HERE=$(dirname "$(readlink -f "$0")")          # app/ui/qa
APP=$(readlink -f "$HERE/../..")               # app

gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x860 > "$OUT/shell.log" 2>&1 &
SH=$!
WD=""
for _ in $(seq 1 40); do
  sleep 1
  WD=$(grep -o "Using Wayland display name '[^']*'" "$OUT/shell.log" | head -1 | sed "s/.*'\(.*\)'/\1/")
  [ -n "$WD" ] && break
done
if [ -z "$WD" ]; then echo "no headless wayland display"; tail -5 "$OUT/shell.log"; kill $SH; exit 1; fi
sleep 3
echo "headless display: $WD"

QAHOME=$OUT/home; rm -rf "$QAHOME"; mkdir -p "$QAHOME"
LOG=$OUT/app.log
HOME="$QAHOME" RIVET_STATE_DIR="$QAHOME/.local/state/rivet" WAYLAND_DISPLAY="$WD" GDK_BACKEND=wayland \
  RIVET_APPLY_CMD="python3 $APP/qa/fake-apply.py" RIVET_FAKE_SCENARIO=fail \
  RIVET_DOCTOR_CMD="python3 $APP/qa/fake-doctor.py" \
  RIVET_COLLECT_CMD="python3 $APP/qa/fake-collect.py" \
  RIVET_QA_JS="$HERE/driver-hardening.js" RIVET_QA_SHOTS="$OUT/shots" RIVET_NO_NOTIFY=1 \
  "$APP/rivet-updater" --demo > "$LOG" 2>&1 &
PID=$!
for _ in $(seq 1 120); do sleep 1; grep -q 'DRIVER DONE\|DRIVER TIMEOUT' "$LOG" && break; done
PY=$(pgrep -P $PID -f rivet_app.py || echo $PID)
t1=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null); sleep 3; t2=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null)
ticks=$(( ${t2:-0} - ${t1:-0} ))
kill $PID 2>/dev/null; sleep 1
kill $SH 2>/dev/null; wait 2>/dev/null

grep -E '\[QA\]|\[rivet\]' "$LOG" | sed 's/^CONSOLE LOG //'
echo "system Inter fonts (must be none, so a loaded Inter face is the bundled one): $(fc-list : family | grep -ci "^inter")"
echo "console errors (the F1 probes are expected to log two):"
grep -i 'console error\|CONSOLE ERROR\|JS ERROR' "$LOG" | sed 's/^CONSOLE //' | cut -c1-200
APPLIES=$(grep -c 'apply: ' "$LOG")
echo "leaveDoctor commands received by the bridge: $(grep -ci 'leaveDoctor\|unknown command .leaveDoctor' "$LOG") (python logs it only as unknown if cmd_leaveDoctor is missing: $(grep -c "unknown command 'leaveDoctor'" "$LOG") unknown)"
echo "apply commands sent for one double-clicked Update now: $APPLIES (must be 1)"
echo "idle CPU: $ticks ticks in 3 s (100 ticks = one full core)"
ls -l "$OUT/shots" 2>/dev/null | tail -n +2
if grep -q 'DRIVER DONE' "$LOG" && ! grep -q '\[QA\] hardening: FAIL' "$LOG" && [ "$APPLIES" = 1 ] && [ "$ticks" -lt 30 ]; then
  echo "HARDENING PASS"
else
  echo "HARDENING FAIL"; grep -v 'CONSOLE LOG' "$LOG" | tail -20; exit 1
fi
