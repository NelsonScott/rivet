#!/usr/bin/env bash
# Rivet installer (user scope only -- no sudo, nothing outside $HOME).
#
#   ./install.sh              install, then enable + start the timer
#   ./install.sh --no-start   install and enable the timer, but do NOT start it
#                             this boot (use when a first run right now would be
#                             disruptive; it starts on the next login/boot)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"
STATE="$HOME/.local/state/rivet"

START_TIMER=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START_TIMER=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$BIN" "$UNITS" "$STATE/logs" "$HOME/.config/rivet"

link() {  # link <target> <name>
  ln -sfn "$1" "$BIN/$2"
  echo "  $BIN/$2 -> $1"
}
echo "symlinking CLIs:"
link "$REPO/backend/rivet_collect.py" rivet-collect
link "$REPO/backend/rivet_apply.py"   rivet-apply
link "$REPO/backend/rivet_doctor.py"  rivet-doctor

echo "installing user units:"
for u in rivet-collect.service rivet-collect.timer; do
  install -m 0644 "$REPO/systemd/$u" "$UNITS/$u"
  echo "  $UNITS/$u"
done

echo "reloading systemd --user:"
systemctl --user daemon-reload

if [ "$START_TIMER" -eq 1 ]; then
  systemctl --user enable --now rivet-collect.timer
  echo "  timer enabled and started"
else
  systemctl --user enable rivet-collect.timer
  echo "  timer enabled but NOT started (--no-start); it begins at the next login/boot,"
  echo "  or run: systemctl --user start rivet-collect.timer"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH" ;;
esac

echo
systemctl --user list-timers rivet-collect.timer --no-pager || true
echo "done."
