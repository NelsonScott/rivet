#!/bin/sh
# Stand-in `flatpak` for the Steam-only re-check (2026-11-10): qa-e2e.sh and selftest.py put it
# first on PATH as `flatpak`, so neither ever asks (or kills) the real Steam.
#   $FAKE_STEAM_DIR/steam-running   exists = `flatpak ps` lists com.valvesoftware.Steam
#   $FAKE_STEAM_DIR/steam-stubborn  exists = `flatpak kill` does not make it go away
#   $FAKE_STEAM_DIR/flatpak.calls   one line per call (argv)
# `kill` takes Steam out of `ps` 0.4 s later, like the real one (it lingers a moment).
# Any other verb goes to the real flatpak: nothing in QA should reach it.
d=${FAKE_STEAM_DIR:?FAKE_STEAM_DIR is not set}
mkdir -p "$d"
echo "$*" >> "$d/flatpak.calls"
case "$1" in
  ps)
    [ -e "$d/steam-running" ] && echo com.valvesoftware.Steam
    echo org.gnome.Calculator
    exit 0 ;;
  kill)
    if [ -e "$d/steam-running" ] && [ ! -e "$d/steam-stubborn" ]; then
      ( sleep 0.4; rm -f "$d/steam-running" ) >/dev/null 2>&1 &
    fi
    exit 0 ;;
esac
exec /usr/bin/flatpak "$@"
