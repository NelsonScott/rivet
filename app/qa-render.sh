#!/bin/bash
# Paint check on the LIVE session: does what the compositor shows match what WebKit drew?
#
#   bash app/qa-render.sh [outdir] [tier ...]        default tiers: 1 (expect OK) 3 (expect STALE)
#
# Launches a QA instance of the real app through app/rivet-updater with RIVET_RENDER_TIER forced,
# a scratch HOME, a ~130-row state (qa/tall-state.py) and qa/driver-render.js, which scrolls
# the list and snapshots the WebKit view. At each step the shell extension's Snap() grabs
# the whole screen; the window is located as the brightest region, cropped to the WebKit
# snapshot's size and diffed against it. Mean abs diff < 12 = OK, else STALE.
#
# Needs: a running GNOME Wayland session with rivet@scottnelson.xyz enabled (Snap lives there),
# and it pops a window on screen for ~25 s per tier, so say so before running it. The
# nested-headless harness (qa-e2e.sh) cannot see this bug: headless mutter composites the
# very same frames correctly. Negative control: tier 3 (old all-software launcher env) must
# come out STALE with WebKitGTK >= 2.54, which proves the detector still works.
set -u
OUT=${1:-/tmp/rivet-render}; shift || true
TIERS=${*:-1 3}
HERE=$(dirname "$(readlink -f "$0")")
export DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}
mkdir -p "$OUT"
snap() { gdbus call --session --dest org.gnome.Shell --object-path /xyz/scottnelson/Rivet \
           --method xyz.scottnelson.Rivet.Snap "$1" >/dev/null || echo "snap failed (extension not loaded?)"; sleep 2; }
FAILED=0
for T in $TIERS; do
  QAHOME=$OUT/tier-$T; rm -rf "$QAHOME"; mkdir -p "$QAHOME/shots" "$QAHOME/.local/state/rivet"
  LOG=$OUT/app-tier-$T.log
  python3 "$HERE/qa/tall-state.py" "$QAHOME/.local/state/rivet/state.json" > /dev/null
  HOME="$QAHOME" RIVET_STATE_DIR="$QAHOME/.local/state/rivet" RIVET_RENDER_TIER=$T \
    RIVET_COLLECT_CMD="python3 $HERE/qa/fake-collect.py" RIVET_APPLY_CMD="python3 $HERE/qa/fake-apply.py" \
    RIVET_QA_JS="$HERE/qa/driver-render.js" RIVET_QA_SHOTS="$QAHOME/shots" RIVET_NO_NOTIFY=1 \
    "$HERE/rivet-updater" > "$LOG" 2>&1 &
  APP=$!
  for n in 1 2 3; do
    for _ in $(seq 1 30); do sleep 1; grep -q "READY$n" "$LOG" && break; done
    sleep 2; snap "$QAHOME/shots/comp-$n.png"
  done
  # idle CPU of the app itself (the python child of the launcher) over 3 s; the 2026-09-18
  # bug was a spinning main loop, and a renderer tier that redraws constantly shows up here too
  for _ in $(seq 1 15); do sleep 1; grep -q 'DRIVER DONE' "$LOG" && break; done
  sleep 4                      # the last qaShot's PNG encode (~3 s of main-thread CPU) must not count as idle
  PY=$(pgrep -P $APP -f rivet_app.py | head -1)
  t1=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null); sleep 3; t2=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null)
  TICKS=$(( ${t2:-0} - ${t1:-0} ))
  kill $APP 2>/dev/null; sleep 1; kill $PY 2>/dev/null
  echo "== tier $T =="
  echo "  idle CPU: $TICKS ticks in 3 s (100 = one full core)"
  grep -E '\[QA\]|Traceback|Error' "$LOG" | sed 's/^.*CONSOLE LOG //' | cut -c1-160 | head -6
  VERDICT=$(python3 - "$QAHOME/shots" <<'PY'
# Where on screen is what WebKit drew? Template-match each WebKit snapshot (scaled to the
# window's on-screen size, scale unknown so a few are tried) against the full-screen grab.
# A correctly painted window matches at ~0.9+; a stale/blank one has nothing to match (<0.5).
import sys, cv2, numpy as np
d = sys.argv[1]
def load(n):
    im = cv2.imread(f'{d}/{n}.png'); return None if im is None else cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
comp = [load(f'comp-{n}') for n in (1, 2, 3)]; wk = [load(f'wk-{n}') for n in (1, 2, 3)]
if any(c is None for c in comp) or any(w is None for w in wk): print('  missing shots'); print('MISSING'); sys.exit()
LOGICAL_W = 1280                       # DEFAULT_SIZE in app/rivet_app.py (scratch HOME has no window.json)
worst = 1.0
for n in range(3):
    screen = cv2.resize(comp[n], None, fx=0.5, fy=0.5); best = (0, None, None)
    for s in (1.0, 1.25, 1.5, 1.75, 2.0):
        w = int(LOGICAL_W * s / 2); h = int(w * wk[n].shape[0] / wk[n].shape[1])
        if w >= screen.shape[1] or h >= screen.shape[0]: continue
        r = cv2.matchTemplate(screen, cv2.resize(wk[n], (w, h), interpolation=cv2.INTER_AREA), cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]: best = (mx, s, (loc[0] * 2, loc[1] * 2))
    worst = min(worst, best[0])
    print(f"  step {n+1}: WebKit frame found on screen with match {best[0]:.2f} (scale {best[1]}, at {best[2]})  ({'ok' if best[0] > 0.8 else 'STALE'})")
print('OK' if worst > 0.8 else 'STALE')
PY
)
  echo "$VERDICT" | head -n -1
  R=$(echo "$VERDICT" | tail -1)
  EXPECT=OK; [ "$T" = 3 ] && EXPECT=STALE
  if [ "$R" = "$EXPECT" ]; then echo "tier $T: $R (expected $EXPECT) PASS"; else echo "tier $T: $R (expected $EXPECT) FAIL"; FAILED=1; fi
done
[ "$FAILED" = 0 ] && echo "RENDER PASS" || { echo "RENDER FAIL"; exit 1; }
