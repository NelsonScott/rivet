# Rivet looks

A look re-points the custom properties `style.css` already uses, under `:root[data-look="<id>"]`, with a
light and a dark block each. No build step: `index.html` links every file here and sets `data-look` before
first paint (from `localStorage`, or `?look=` for screenshot QA); `app.js` `LOOKS` is the list, its order,
the picker's names, blurbs and mini-preview swatches. No font is fetched from the web: Periwinkle's Manrope
is bundled (`../fonts/`); the other looks use their face when it is installed on the system (for example
`mozilla-fira-sans-fonts`, `ibm-plex-sans-fonts`, `jetbrains-mono-fonts`) and otherwise fall back to the
bundled Inter.

1. **Periwinkle** (default, no file): `style.css` as it was. Cool white / near-black, periwinkle gradient, Manrope.
2. `nord.css`: **Nord**. Polar Night / Snow Storm, Frost blue, three quiet layers (edge, ground, cards), Fira Sans.
3. `poster.css`: **Poster**. The beaver icon's palette: cream ground, ink top band, navy action, one red rule,
   Archivo condensed caps on ink label blocks, square corners, hard offset shadows.
4. `workshop.css`: **Workshop**. Warm greys, oxblood buttons, amber links and section titles, IBM Plex.
5. `terminal.css`: **Terminal**. Near-black or paper white, one indigo, JetBrains Mono for all metadata, tighter rows.

Tokens a look sets beyond the original ones: `--chrome` (header + status strip band; falls back to `--bg`),
`--on-accent` (text on primary buttons, checkboxes, segmented controls), `--on-red-solid`, `--ok-solid`,
`--primary-shadow`, `--danger-line`. A look may also scope tokens to a region (Poster redefines the text tokens
inside its black band) and add a few rules of its own; keep those under its `:root[data-look]` selector.

Type size (Compact / Comfortable) is separate from the look: the host applies it as WebKit page zoom
(`rivet_app.py` `TYPE_ZOOM`), so every look scales as one. Both persist in `~/.config/rivet/settings.json`.

Switching: Preferences > Look (tiles), `g y` for the next look, `g L` jumps to the tiles, menu > Next look.

QA: `python3 app/qa/looks-contrast.py` checks every look's token pairs at 4.5:1; the `looks` e2e scenario
(`app/qa-e2e.sh … looks`) picks each look through the real tiles, shoots light + dark, and scans the text the
renderer actually painted, which is what catches a colour still hardcoded for Periwinkle.
