#!/bin/bash
# End-to-end QA of the real GTK/WebKit app without touching the live session.
#
#   timeout 150 dbus-run-session -- bash app/qa-e2e.sh /tmp/rivet-e2e [scenario ...]
#
# Nested headless gnome-shell (same trick as shell-ext/qa-headless.sh) + the actual
# app with a scratch HOME, fake apply/collect backends, and qa/driver.js clicking
# through Update now -> result screen -> back to the list, once per scenario
# (default: ok warn fail crash held; see qa/fake-apply.py). "held" drives the Held back
# section instead (qa/driver-held.js): try -> watcher sees the game die -> rolled back;
# try the next build -> watcher sees it stay up -> kept. "doctor" drives Fix it on a failed
# update (qa/driver-doctor.js + qa/fake-doctor.py: no Claude call, no command run). "other"
# (alias "elsewhere") checks the Other software section (groups, one-line rows, badges only),
# drives its Update / Install in HA buttons against qa/fake-apply.py, then the "nothing pending"
# status strip and the trimmed hero, light + dark (qa/driver-other.js); "other-row" shoots a row
# collapsed / expanded; "other-bulk" drives "Update the safe ones (N)" (confirmation, a run with
# one failure, Stop after this one); "other-remove" drives Remove on the unused HA add-on
# (confirmation, a refused first try with the HA fallback, then the row goes; the bridge must
# have run rivet-apply --other-remove twice); "other-fix" (2026-11-01) checks containers are off by
# default, fails Alexa Media Player and drives Fix it (the real other_fix.run on canned evidence,
# qa/fake-claude.py instead of claude); "other-real" looks at THIS box's state.json, presses
# nothing. other / other-row / other-bulk run with show_containers on in the scratch settings.json. "looks" picks every look x light/dark through Preferences > Look and scans the painted
# text contrast (qa/driver-looks.js); "looks-real" shoots the looks on THIS box's state.json.
# warn/fail/crash also open
# "Show details" and save PNGs of the real WebKit view to $OUT/shots/. After each run
# it measures the app's CPU while idle: the 2026-09-18 bug was the main loop spinning
# after rivet-apply exited.
set -u
OUT=${1:-/tmp/rivet-e2e}; mkdir -p "$OUT/shots"; shift || true
SCENARIOS=${*:-ok warn fail crash held doctor recheck other other-row other-bulk other-remove other-fix looks abuse abuse-noclaude second-instance}
HERE=$(dirname "$(readlink -f "$0")")

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

FAILED=0
for SC in $SCENARIOS; do
  QAHOME=$OUT/home-$SC; rm -rf "$QAHOME"; mkdir -p "$QAHOME"
  LOG=$OUT/app-$SC.log
  DRIVER=$HERE/qa/driver.js; LIMIT=45
  APPLY_SC=$SC; DOCTOR_SC=ok; ENVX=""
  if [ "$SC" = abuse ] || [ "$SC" = abuse-noclaude ]; then
    # 2026-11-01 abuse pass (qa/driver-abuse.js, docs/QA-MATRIX.md). abuse: a stale document portal
    # (fake-doctor's health world), HA going away mid-bulk (fake-apply abuse), 0 and 300 updates.
    # abuse-noclaude: no claude CLI, no HA, the rpm password prompt cancelled (fake-apply cancel).
    DRIVER=$HERE/qa/driver-abuse.js; LIMIT=150
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HERE/ui/sample-state.json" "$QAHOME/.local/state/rivet/state.json"
    if [ "$SC" = abuse ]; then ENVX="RIVET_HEALTH_SCENARIO=portal-stale"
    else APPLY_SC=cancel; ENVX="RIVET_CLAUDE_BIN=$QAHOME/no-such-claude"; fi
  fi
  if [ "$SC" = second-instance ]; then
    # 2026-11-01: a second `rivet-updater` while one is open must only raise the first window (GApplication
    # single instance; RIVET_QA_UNIQUE=1 keeps it on for this QA run)
    DRIVER=$HERE/qa/driver-render.js; LIMIT=40; ENVX="RIVET_QA_UNIQUE=1"
  fi
  if [ "$SC" = doctor ]; then
    # a failed update, then Fix it -> (fake) diagnosis -> Run the fix -> Fixed
    DRIVER=$HERE/qa/driver-doctor.js; LIMIT=75; APPLY_SC=fail
  fi
  if [ "$SC" = recheck ]; then
    # 2026-10-12: verify "failed" (raced depmod) -> Fix it -> "nothing to run, safe to restart"
    # -> the app re-checks the module itself -> Restart now offered (the driver never clicks it)
    DRIVER=$HERE/qa/driver-doctor.js; LIMIT=75; APPLY_SC=kmodfail; DOCTOR_SC=nofix
  fi
  if [ "$SC" = other-real ]; then
    # look-only shots of THIS box's real state.json (copied in; buttons are never pressed and
    # would only reach qa/fake-apply.py anyway)
    DRIVER=$HERE/qa/driver-other.js; LIMIT=60
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HOME/.local/state/rivet/state.json" "$QAHOME/.local/state/rivet/"
  fi
  if [ "$SC" = looks ]; then
    # 2026-10-31: every look x light/dark through Preferences > Look, painted-contrast scan, g y, type size
    DRIVER=$HERE/qa/driver-looks.js; LIMIT=300
  fi
  if [ "$SC" = looks-real ]; then
    DRIVER=$HERE/qa/driver-looks.js; LIMIT=150
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HOME/.local/state/rivet/state.json" "$QAHOME/.local/state/rivet/"
  fi
  case "$SC" in other|elsewhere|other-row|other-bulk)
    # 2026-11-01: the Containers section is off by default; these scenarios look at it
    mkdir -p "$QAHOME/.config/rivet"; echo '{"show_containers": true}' > "$QAHOME/.config/rivet/settings.json" ;;
  esac
  if [ "$SC" = other-real ] || [ "$SC" = looks-real ]; then
    # the user's own settings (containers on or off as set)
    mkdir -p "$QAHOME/.config/rivet"; cp "$HOME/.config/rivet/settings.json" "$QAHOME/.config/rivet/" 2>/dev/null || true
  fi
  if [ "$SC" = other-row ]; then
    DRIVER=$HERE/qa/driver-other.js; LIMIT=65
  fi
  if [ "$SC" = other-bulk ]; then
    # 2026-10-31 c: the bulk path goes through the bridge's otherUpdateMany -> fake-apply.py per row;
    # 2026-11-01: the HA run ends with --ha-restart, which reads the scratch state.json
    DRIVER=$HERE/qa/driver-other.js; LIMIT=130
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HERE/ui/sample-state.json" "$QAHOME/.local/state/rivet/state.json"
  fi
  if [ "$SC" = other-fix ]; then
    # 2026-11-01: a failed HA install, then Fix it through the bridge's otherFix -> fake-apply.py
    # --other-fix -> the REAL backend on canned evidence with qa/fake-claude.py
    DRIVER=$HERE/qa/driver-other.js; LIMIT=110
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HERE/ui/sample-state.json" "$QAHOME/.local/state/rivet/state.json"
  fi
  if [ "$SC" = other-remove ]; then
    # 2026-11-01: Remove -> the bridge's otherRemove -> qa/fake-apply.py --other-remove (first
    # try refused by the fake live re-check, second removes the row from the scratch state.json)
    DRIVER=$HERE/qa/driver-other.js; LIMIT=95
    # the remove patches state.json on disk, so the sample has to BE the scratch state.json
    mkdir -p "$QAHOME/.local/state/rivet"; cp "$HERE/ui/sample-state.json" "$QAHOME/.local/state/rivet/state.json"
  fi
  if [ "$SC" = other ] || [ "$SC" = elsewhere ]; then
    # 2026-10-31: the Other software section from sample-state.json (qa/driver-other.js);
    # its buttons run qa/fake-apply.py --other-update, never a real npm / podman / HA call
    DRIVER=$HERE/qa/driver-other.js; LIMIT=95
  fi
  if [ "$SC" = held ]; then
    DRIVER=$HERE/qa/driver-held.js; LIMIT=110
    # the trial reads state.json from disk, so the fake collector runs once before the app
    HOME="$QAHOME" RIVET_STATE_DIR="$QAHOME/.local/state/rivet" RIVET_FAKE_SCENARIO=held \
      RIVET_NO_NOTIFY=1 python3 "$HERE/qa/fake-collect.py"
  fi
  { echo "const SCENARIO = \"$SC\";"; [ -n "${QA_CUT:-}" ] && echo "const QA_CUT = $QA_CUT;"; cat "$DRIVER"; } > "$OUT/driver-$SC.js"
  env $ENVX HOME="$QAHOME" RIVET_STATE_DIR="$QAHOME/.local/state/rivet" WAYLAND_DISPLAY="$WD" GDK_BACKEND=wayland \
    RIVET_APPLY_CMD="python3 $HERE/qa/fake-apply.py" RIVET_FAKE_SCENARIO="$APPLY_SC" \
    RIVET_DOCTOR_CMD="python3 $HERE/qa/fake-doctor.py" RIVET_DOCTOR_SCENARIO="$DOCTOR_SC" \
    RIVET_COLLECT_CMD="python3 $HERE/qa/fake-collect.py" \
    RIVET_QA_JS="$OUT/driver-$SC.js" RIVET_QA_SHOTS="$OUT/shots" RIVET_NO_NOTIFY=1 \
    "$HERE/rivet-updater" --demo > "$LOG" 2>&1 &
  APP=$!
  if [ "$SC" = second-instance ]; then
    for _ in $(seq 1 20); do sleep 1; grep -q '\[QA\]' "$LOG" && break; done
    t0=$(date +%s%N)
    env $ENVX HOME="$QAHOME" RIVET_STATE_DIR="$QAHOME/.local/state/rivet" WAYLAND_DISPLAY="$WD" GDK_BACKEND=wayland \
      RIVET_NO_NOTIFY=1 timeout 20 "$HERE/rivet-updater" --demo > "$OUT/app-$SC-second.log" 2>&1
    rc2=$?; dt=$(( ($(date +%s%N) - t0) / 1000000 ))
    echo "$SC: the second launch exited $rc2 after ${dt} ms"
  fi
  for _ in $(seq 1 $LIMIT); do sleep 1; grep -q 'DRIVER DONE\|DRIVER TIMEOUT' "$LOG" && break; done
  # let the last qaShot finish: get_snapshot + save_to_png of a full-size texture costs ~3 s of
  # main-thread CPU on the DMA-BUF tiers and would otherwise be read as "idle" load
  sleep 4
  PY=$(pgrep -P $APP -f rivet_app.py || echo $APP)
  t1=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null); sleep 3; t2=$(awk '{print $14+$15}' /proc/$PY/stat 2>/dev/null)
  ticks=$(( ${t2:-0} - ${t1:-0} ))
  kill $APP 2>/dev/null; sleep 1; kill "$PY" 2>/dev/null   # the launcher forwards TERM, this is belt and braces
  grep -E '\[QA\]|\[rivet\]' "$LOG" | sed 's/^CONSOLE LOG //'
  echo "$SC: idle CPU: $ticks ticks in 3 s (100 ticks = one full core)"
  EXTRA=ok
  if [ "$SC" = other-remove ]; then
    n=$(grep -c "other remove: .*--other-remove ha:update.matter_server_update" "$LOG")
    echo "$SC: bridge ran rivet-apply --other-remove $n time(s) (want 2)"
    [ "$n" = 2 ] || EXTRA=bad
    if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(any(i['id']=='ha:update.matter_server_update' for i in d['elsewhere']['items']))" "$QAHOME/.local/state/rivet/state.json"; then
      echo "$SC: the row is gone from the scratch state.json"; else echo "$SC: the row is still in the scratch state.json"; EXTRA=bad; fi
  fi
  if [ "$SC" = other-fix ]; then
    n=$(grep -c "other fix: .*--other-fix ha:update.alexa_media_player_update" "$LOG")
    echo "$SC: bridge ran rivet-apply --other-fix $n time(s) (want 1)"; [ "$n" = 1 ] || EXTRA=bad
    if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('fake claude saw:', d['allowed'][:1], '... problems:', d['problems']); sys.exit(bool(d['problems']) or not d['session'])" "$QAHOME/.local/state/rivet/fake-claude-argv.json"; then :; else EXTRA=bad; fi
    k=$(grep -c '"step": "fix".*Fixed: HACS had downloaded' "$QAHOME/.local/state/rivet/logs/apply.log" 2>/dev/null)
    echo "$SC: apply.log has the Fix it result: $k line(s)"; [ "${k:-0}" -ge 1 ] || EXTRA=bad
  fi
  if [ "$SC" = second-instance ]; then
    a=$(grep -c 'activate: Rivet is already open' "$LOG")
    w=$(grep -c 'ready\|\[QA\]\|serving' "$OUT/app-$SC-second.log")
    echo "$SC: first instance raised its window $a time(s) (want 1); the second served $w page line(s) (want 0)"
    { [ "$rc2" = 0 ] && [ "$a" = 1 ] && [ "$w" = 0 ] && [ "$dt" -lt 10000 ]; } || EXTRA=bad
  fi
  if [ "$SC" = abuse ]; then
    n=$(grep -c "other restart: .*--ha-restart" "$LOG")
    echo "$SC: Home Assistant restarts while it was unreachable: $n (want 0)"; [ "$n" = 0 ] || EXTRA=bad
    k=$(grep -c "health: .*--fix doc-portal" "$LOG")
    echo "$SC: health fixes run by the bridge: $k (want 1)"; [ "$k" = 1 ] || EXTRA=bad
  fi
  if [ "$SC" = abuse-noclaude ]; then
    n=$(grep -c "other fix: \|doctor diagnose: " "$LOG")
    echo "$SC: Fix it processes started: $n (want 0)"; [ "$n" = 0 ] || EXTRA=bad
  fi
  if [ "$SC" = other-bulk ]; then
    n=$(grep -c "other restart: .*--ha-restart" "$LOG")
    echo "$SC: bridge restarted Home Assistant $n time(s) (want 1)"; [ "$n" = 1 ] || EXTRA=bad
  fi
  if grep -q 'DRIVER DONE' "$LOG" && [ "$ticks" -lt 30 ] && [ "$EXTRA" = ok ]; then echo "$SC: PASS"; else echo "$SC: FAIL"; tail -20 "$LOG"; FAILED=1; fi
done
kill $SH 2>/dev/null; wait 2>/dev/null
ls -l "$OUT/shots" 2>/dev/null | tail -n +2
[ "$FAILED" = 0 ] && echo "E2E PASS" || { echo "E2E FAIL"; exit 1; }
