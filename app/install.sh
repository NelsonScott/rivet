#!/usr/bin/env bash
# Install Rivet's app side into ~/.local: launcher symlink, .desktop, icon.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
hicolor="$HOME/.local/share/icons/hicolor"
mkdir -p "$bin" "$apps"

ln -sfn "$here/rivet-updater" "$bin/rivet-updater"
# App icon: the beaver, as the pre-rendered PNG set in icons/hicolor (rendered from the 1024 px
# icons/xyz.scottnelson.Rivet.png; to regenerate after changing it:
#   for s in 16 24 32 48 64 128 256 512; do magick icons/xyz.scottnelson.Rivet.png \
#     -resize ${s}x${s} -strip icons/hicolor/${s}x${s}/apps/xyz.scottnelson.Rivet.png; done)
# so installing needs no image tools.
for f in "$here"/icons/hicolor/*/apps/xyz.scottnelson.Rivet.png; do
  dst="$hicolor/${f#"$here/icons/hicolor/"}"
  cmp -s "$f" "$dst" || install -Dm0644 "$f" "$dst"
done
rm -f "$hicolor/.rivet-icon.sha256"     # the old render-on-install marker
# the repo copy says Exec=rivet-updater; a desktop session's PATH may lack ~/.local/bin, so pin it here
sed "s|^Exec=rivet-updater\$|Exec=$bin/rivet-updater|" "$here/xyz.scottnelson.Rivet.desktop" > "$apps/xyz.scottnelson.Rivet.desktop"
chmod 0644 "$apps/xyz.scottnelson.Rivet.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$apps"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

echo "installed:"
echo "  $bin/rivet-updater -> $here/rivet-updater"
echo "  $apps/xyz.scottnelson.Rivet.desktop"
echo "  $hicolor/<size>/apps/xyz.scottnelson.Rivet.png"
