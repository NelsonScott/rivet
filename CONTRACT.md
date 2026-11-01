# Rivet — component contract (v1, 2026-09-15)

> **Changed 2026-11-01 (f): opt-in Claude, local fonts, no author-only paths.**
> - Settings `ai_triage` (bool, default false; Preferences "Explain and rate updates with Claude
>   Code"). Off: `rivet-collect` never starts `claude` (triage verdicts are the deterministic ones,
>   no "what is this" pass); true is the only value that turns it on, and turning it on starts a
>   check. Fix it is unchanged (a button), default model `opus`, fallback `sonnet`.
> - Home Assistant: `HA_URL` (was `RIVET_HA_URL`) and `HA_TOKEN` from the environment, else from
>   `~/.config/rivet/ha.env` (`NAME=value` lines; `export ` and quotes allowed; only those two names
>   are read). The secrets-helper hook is gone.
> - Removed: the one-time copy of the pre-rename dirs and the old command-name symlinks (see
>   History), the launcher-icon health check (one launcher only), and the built-in gamescope hold
>   seed (a mask with no registry entry shows as `pinned`, as before for any other ref).
> - The page loads no web fonts: Manrope and Inter are bundled in `app/ui/fonts`. The app icon
>   ships as a pre-rendered hicolor PNG set in `app/icons/hicolor`.

> **Changed 2026-11-01 (d): the abuse pass before the public release (all additive; tests and
> results in docs/QA-MATRIX.md).**
> - **Home Assistant not set up = nothing shown.** No `HA_TOKEN` (env or `ha.env`) and no
>   `HA_URL`: the `ha` source is `absent` (no row, no hint). A URL without a token stays
>   `cant_check` ("set HA_TOKEN to a long-lived access token"). The default URL is HA's own
>   `http://homeassistant.local:8123`, no longer a LAN IP. An Install in HA that cannot reach HA
>   (connection refused, no route, the name does not resolve) fails at once ("could not reach Home
>   Assistant at … ; nothing was installed") instead of polling 20 minutes; HA that stops answering
>   mid-install fails after `RIVET_HA_GONE_S` (90 s). Both carry `done.other.unreachable: true`.
> - **Bulk run when Home Assistant goes away** (decision): a failed row still does not stop the rest,
>   EXCEPT that after a row ends `unreachable` the bridge skips the batch's remaining `ha:` rows
>   (synthetic done, `result: "skipped"`, "Skipped: Home Assistant could not be reached a moment ago,
>   so nothing was sent…", listed in `batch.skipped`) and does not restart Home Assistant at the end.
>   This PC / container rows still run. A skipped row is neutral (`.ew-run.skipped`, "–") and offers no
>   Fix it (nothing ran); the failed one does.
> - **Claude Code missing.** New message `{"type":"caps","caps":{"claude":bool}}` after `settings` on
>   `ready` (same lookup as the backend: `RIVET_CLAUDE_BIN` decides when set). `claude: false`: every
>   Fix it button (row, run panel, result screen) is the note "Install Claude Code to enable Fix it"
>   (`.no-claude`), and `otherFix` / `fixIt` are refused with that hint without spawning anything.
>   A Claude run that fails says so in one clause (`rivet_doctor.plain_claude_error`): not
>   installed, not logged in, out of usage, timed out, out of turns, unreadable answer.
> - **Health checks** (`backend/health.py`): `~/.local/state/rivet/health.json` = `{checked_at,
>   checks: [{id, title, detail, fix_label}]}`, problems only, its own file (never part of state.json).
>   `doc-portal`: xdg-document-portal active but `/run/user/<uid>/doc/by-app` missing (every Flatpak
>   then dies in bwrap). Written by `rivet-collect` (not with a fixture or
>   `RIVET_NO_HEALTH=1`) and `rivet-doctor health`; `rivet-doctor health --fix ID` runs the
>   check's FIXED user-level command (`systemctl --user restart xdg-document-portal.service`),
>   checks again, ends `done` with `health`. Bridge: `ready` runs `rivet-doctor
>   health` once; `healthFix {id}` (ids from that table only, one at a time); messages
>   `{"type":"health","health":{…}}` and `{"type":"health","event":ev}`. UI: one `.health-banner`
>   per problem above the updates list / front page, with the fix button.
> - **Flatpak.** A remote that cannot be read no longer hides every update of its installation:
>   each remote is asked on its own, the broken ones land in `flags.flatpak_remote_errors:
>   [{scope, remote, error}]` and the status strip says "Fedora: up to date · Flatpak: <remote> could
>   not be checked" (never the up-to-date hero). A flatpak update that fails because the app or
>   runtime is in use says to close it (`results.flatpak.reasons`).
> - **dnf.** A dependency conflict ends with the cause "dnf found a dependency conflict around <pkg>,
>   so nothing was installed. Untick it and update the rest…". A cancelled or denied password prompt
>   for the rpm transaction stops the run: the plan's Flatpak and firmware items get a `skip` event
>   each ("skipped: the password prompt was cancelled…", `results.stopped_after_refusal`), and
>   `reboot_required` is true only when something that needs a reboot actually landed.
> - **One instance.** A second `rivet-updater` hands over to the running one over D-Bus (GApplication) and
>   exits; the first logs "activate: Rivet is already open" and raises its window. QA runs stay
>   NON_UNIQUE unless `RIVET_QA_UNIQUE=1`. `app/xyz.scottnelson.Rivet.desktop` says `Exec=rivet-updater`; app/install.sh
>   writes the absolute path into the installed copy.

> **Changed 2026-11-01 (c): Home Assistant joins the bulk flow, Fix it on a failed row, HA core
> restart, containers off by default (all additive).** Prompted by "Alexa Media Player: Home
> Assistant stopped without installing it (still v5.18.0)": run Home Assistant rows like the
> others, and let Claude Code retry what it needs. Cause of that
> failure: HACS downloaded v5.18.0 in 2 s and filed a `restart_required` repair; v5.19.0 had been
> released that morning, so `latest_version` moved and the old check ("installed older than the
> LIVE latest") called it a failure. Now: Install in HA is judged against the row's `latest`
> (pinned with `version` where the entity supports it), an integration that needs a restart ends
> `restart_pending`, and the app restarts Home Assistant ONCE (new `rivet-apply --ha-restart`,
> bridge `haRestart`, automatic after an integration install or at the end of a bulk run). HA
> add-ons and integrations are in the safe set at any non-major bump; the bulk confirmation has a
> checkbox per row. A failed update row offers **Fix it** (new `rivet-apply --other-fix ID
> [--dry-run]`, bridge `otherFix {id}`): one bounded `claude -p --model opus` run. New setting
> `show_containers` (default false): no podman collection, no Containers section. Details in
> "Other software": "Install in HA, judged against its target", "HA core restart", "Fix it on a
> failed row", "Containers off by default"; "Safe set" amended.

> **Changed 2026-11-01 (b): container rows compare versions, not digests.** A digest-to-digest row
> told the user nothing. A podman row's `installed` / `latest` are the running
> image's version and the newest tag of the same repo; a digest-only change is the new status
> `rebuilt` (no badge, no button, one quiet line in the details); containers that hold data get
> action `runbook` (or the `manual` note), never Update. New row facts `tag`, `newest_tag`,
> `rebuilt`, `stateful`, `runbook`, `runbook_title`, `manual`. Details in "Containers" under
> "Other software". `ROW_VERSION` 3 (the 6 h podman reuse is dropped once).

> **Changed 2026-11-01: Remove uninstalls an unused Home Assistant add-on (additive).** The
> "Remove?" link on an `unused: true` add-on row becomes a **Remove** button with a
> confirmation; new bridge command `otherRemove {id}` → `rivet-apply --other-remove ID`
> (live entity re-check, then Supervisor uninstall over HA's websocket, poll until not
> installed, row dropped from state.json). Details in "Other software", "Remove an unused add-on".

> **Changed 2026-10-31 (c): the front page (UI + one bridge command, all additive).** When
> nothing is pending in dnf/Flatpak/fwupd but Held back or Other software has rows, the
> "You're up to date" hero becomes a one-line status strip; the hero (trimmed, no button) is
> kept only when there are no rows anywhere. Other software rows are one line (details behind
> the chevron), grouped This PC / Home Assistant / Containers, badges only (no severity dot),
> podman digests badged NEW BUILD. HA rows gain `addon_slug`, `unused`, `addon_link` (a
> "Remove?" button for an unused add-on). New bridge commands `otherUpdateMany {ids}` /
> `otherStop {}` ("Update the safe ones (N)", sequential `rivet-apply --other-update ID`
> runs; rivet-apply itself is unchanged) and event `{"step":"batch"}`. Details in "Other
> software" ("Front page", "Safe set") and the bridge section.

> **Changed 2026-10-31 (b): "Elsewhere" is now "Other software", says what each thing is, and
> can update the safe ones (all additive).** The state.json key stays `elsewhere`. Each item
> gains `what`, `used_by`, `ai`, `warning`, `where`, `action` (`update | ha_install | open |
> copy | null`), `confirm`, per-source facts (`prefix`/`npm`, `kind`/`entity_id`/`installable`/
> `backup`, `units`/`containers`/`image`/`restart`/`pulled`) and an item status `updated`. New
> `backend/know.py` + cache `know-cache.json` (`claude -p`, once per name). New
> `rivet-apply --other-update ID`, bridge command `otherUpdate {id}`, message type `other`.
> The npm source scans every global prefix. Details in "Other software" below.

> **Changed 2026-10-31: "Elsewhere" (read-only, all additive; renamed and extended the same day, see above).** `state.json` gains a top-level
> object `elsewhere` (NOT a group: never in `groups`, `summary`, `flags`, the pending set, the
> notification or the top-bar badge) that lists what else on the box is stale outside dnf and
> Flatpak: npm globals, pipx, uv tools, cargo, Home Assistant `update.*` entities, podman images,
> GNOME Shell extensions, AppImages. New `rivet-collect --no-elsewhere` / `RIVET_NO_ELSEWHERE=1`,
> new bridge command `copyText {text}`. Nothing is ever installed from it. Details in "Elsewhere"
> below.

> **Changed 2026-10-04: hardening (backend only, all additive).** `state.json` gains the error value
> `"interrupted"` (`status: "error"`, previous groups carried) when `rivet-collect` is killed
> mid-check; it exits 128+signal (143 on SIGTERM). Every state.json / holds.json / last-pending.json
> write is tmp + fsync + rename (it already was; now a stated guarantee). Logs under
> `logs/` rotate at 2 MB to `<name>.1`. `rivet-apply` / `rivet-doctor` keep running and keep
> logging when the app closes their stdout. A `holds.json` that does not parse is never
> overwritten (a `--try-held` etc. ends `done fail` with `held.result: "failed"`; a copy is kept as
> `holds.json.bad-<ts>`). `systemd/rivet-collect.service` `TimeoutStartSec` 300 -> 1015. Details
> in "Robustness" below.

> **Changed 2026-09-21 (b): "Held back".** New state.json group `held` + `flags.held_count` /
> `flags.held_candidates`, new file `holds.json`, new `rivet-apply --try-held / --verify-held /
> --keep-held / --rollback-held`, new bridge commands `tryHeld / verifyHeld / keepHeld /
> rollbackHeld` and message type `held`. All additive: a reader that ignores unknown groups,
> flags and message types keeps working. Details in the section "Held back" below.

Rivet replaces GNOME Software for updates on a Fedora 44 GNOME (Wayland) workstation.
Three components, built by separate people, meet at the files and interfaces below. Do not
change this contract unilaterally; if you must, write the change here first and say so in
your report.

## Layout

```
backend/      rivet_collect.py, rivet_apply.py, triage.py, pyproject or plain scripts
              installs as ~/.local/bin/rivet-collect, rivet-apply, rivet-doctor (install.sh)
app/          rivet_app.py (GTK4 + WebKitGTK 6.0, Python/PyGObject), ui/ (from proto/a)
              installs as ~/.local/bin/rivet-updater + ~/.local/share/applications/xyz.scottnelson.Rivet.desktop
              + hicolor icons xyz.scottnelson.Rivet.png (app/install.sh)
shell-ext/    rivet@scottnelson.xyz (GNOME Shell 50 extension: top-bar badge)
systemd/      rivet-collect.service + rivet-collect.timer (user units)
proto/        the clickable prototypes (A is the chosen design; keep as reference)
```

State dir: `~/.local/state/rivet/`. Log dir: `~/.local/state/rivet/logs/`.

## state.json  (written by backend, read by app + extension)

Path: `~/.local/state/rivet/state.json`. Written atomically (tmp + rename). Schema:

```json
{
  "schema": 1,
  "generated_at": "2026-09-15T15:40:00-04:00",
  "checked_at":   "2026-09-15T15:39:40-04:00",
  "status": "ok | checking | error",
  "error": null,
  "host": {"os": "Fedora 44", "kernel_running": "7.2.4-200.fc44.x86_64", "secure_boot": true},
  "flags": {
    "steam_running": true,
    "kernel_pending": true,
    "akmod_nvidia_pending": true,
    "reboot_required": true,
    "blocked_count": 3,
    "security_count": 1
  },
  "summary": {"total": 13, "total_bytes": 1700000000},
  "groups": [
    {"id": "dnf",     "title": "System",          "subtitle": "dnf",     "items": [ ...item ]},
    {"id": "flatpak", "title": "Apps & runtimes", "subtitle": "Flatpak", "items": [ ...item ]},
    {"id": "fwupd",   "title": "Firmware",        "subtitle": "fwupd",   "items": [ ...item ]}
  ]
}
```

item:

```json
{
  "id": "dnf:kernel",                 // "<source>:<name or ref>" — stable across runs
  "source": "dnf | flatpak | fwupd",
  "name": "kernel",
  "display_name": "kernel",           // human label (Flatpak: app name; fwupd: device name)
  "from": "7.2.4-200.fc44",
  "to":   "7.2.5-200.fc44",
  "size_bytes": 148000000,
  "ref": "kernel-7.2.5-200.fc44.x86_64",   // dnf nevra | flatpak ref | fwupd device id
  "risk": "green | yellow | red",
  "badge": null | "security" | "reboot" | "rebuild" | "blocked",
  "verdict": "one sentence from the triage model",
  "reasoning": "2-4 sentences: why this color, citing the release note",
  "notes_excerpt": "verbatim excerpt of the changelog/advisory, <= 600 chars",
  "notes_url": "https://bodhi.fedoraproject.org/updates/FEDORA-2026-...",  // or null
  "advisory": {"id": "FEDORA-2026-abc", "type": "security|bugfix|enhancement", "severity": "critical|important|moderate|low|null"} | null,
  "requires_reboot": true,
  "blocked_by": null | "steam_running",
  "selected_default": true            // false for blocked items
}
```

Rules the backend applies (deterministic, before the model sees anything):
- `risk = red` if advisory severity is critical/important, or `blocked_by` set.
- `risk = yellow` if requires_reboot, or name matches kernel*, akmod-*, kmod-*, *nvidia*, mesa*, systemd*, glibc, or a Flatpak runtime (org.freedesktop.Platform*, org.gnome.Platform*, *.GL.*, *.Compat.*).
- else `green`, unless the model argues otherwise (the model may raise, never lower, a color).
- Flatpak runtime items get `blocked_by: "steam_running"` when `flatpak ps` lists com.valvesoftware.Steam.
- kernel + akmod-nvidia pending together → both `badge: "rebuild"|"reboot"`, and `flags.akmod_nvidia_pending = true`.
- Badge precedence (clarified 2026-09-15), since an item gets exactly one badge:
  `blocked` > the kernel+akmod pairing (`rebuild` for akmod-*/kmod-*, `reboot` for kernel*)
  > `security` > `reboot`. So a kernel carrying a security advisory shows `reboot`, because the
  rebuild/reboot is the actionable fact for it. `flags.security_count` is therefore computed
  from the ADVISORY (type security, or severity critical/important), not from the badge, and
  can legitimately exceed the number of items badged `security`.

## Held back (Flatpak refs pinned with `flatpak mask`), added 2026-09-21

Why: gamescope 3.16.29 broke game launches; 3.16.28 was rolled back by hand and masked.
A mask hides a ref from `flatpak update` for good, so something has to say when a newer build is
worth trying, and make trying it safe.

**Registry** `~/.local/state/rivet/holds.json` (backend/holds.py, atomic writes):
`{"version": 1, "holds": {"<full ref>": {scope, mask, display_name, reason, held_since,
good: {version, commit}, bad_versions: [..], verify: {type: "steam-game", appid, label, process,
alive_seconds, fail_patterns: [regex]}, history: [{date, event, note}],
pending_trial?: {from_commit, from_version, to_version, to_commit, started_at, stage: "updating"|"awaiting_verify"},
notified_candidate?: "<version>"}}, "released": [ ...holds whose trial was kept... ]}`.
Seeding: when the file is missing AND the system mask list has
`org.freedesktop.Platform.VulkanLayer.gamescope//25.08`, the collector writes the gamescope entry.
A mask with no entry is still shown: `hold_state: "pinned"`, reason "Pinned with flatpak mask (no note)".
To hold something by hand: `sudo flatpak mask --system '<id>//<branch>'` + (optionally) an entry here.

**state.json**: a fourth group `{"id": "held", "title": "Held back", "subtitle": "pinned"}`, always
present, empty when nothing is pinned. Its items carry the usual item keys plus `held: true`,
`hold_state`, `hold_reason`, `held_since`, `good_version`, `bad_versions`, `verify_label`, `scope`,
`origin` and, while a trial is open, `trial: {from_version, to_version, started_at, stage, stale}`
(`stale` = older than 24 h: "trial never confirmed", shown with Keep / Roll back; nothing is ever
rolled back silently). `hold_state`:
`known_bad` = `to` is in `bad_versions` or compares <= the newest bad version (numeric compare:
3.16.9 < 3.16.28 < 3.16.30); `candidate` = `to` is newer than every bad version; `pinned` = no
registry entry, or nothing newer on the remote; `trial` = `pending_trial` is set.
Held items are NEVER `selected_default`, never counted in `summary.total` / `total_bytes`, not in
the pending set that drives the notification, never triaged, never `risk: "red"` (the top-bar
extension also skips the group), and `rivet-apply --plan` ignores them even when asked.
`flags.held_count`, `flags.held_candidates`. With `--notify`, a candidate is announced once per
version (`notified_candidate`): "gamescope 3.16.30 is out; the pinned 3.16.28 still works. Try it
from Rivet when you have a minute."

**rivet-apply** (backend/held_ops.py), same JSONL events, `step` = `trial | watch | keep | rollback`,
every event carries `held: {ref, display_name, label, from_version, to_version}`, and every command
ends with the usual `done` event plus `held.action` (`try|verify|keep|rollback`) and `held.result`
(`awaiting_verify | kept | rolled_back | inconclusive | refused | failed`):
- `--try-held <ref> [--force]`: refuses a `known_bad` build without `--force`, refuses while the
  verify target runs ("Close the game first.": the `process`, or any gamescope) and while Steam runs
  (a running Steam keeps the OLD layer mounted, so the test would prove nothing). Records the
  current commit, writes `pending_trial` (stage `updating`) BEFORE the mask comes off, removes the
  mask, `flatpak update --<scope> -y --noninteractive <ref>`, then emits
  `{"step":"trial","status":"awaiting_verify"}`. Update failed or changed nothing: re-mask, clear
  the trial, `result: "failed"`.
- `--verify-held <ref> [--timeout 240]`: passive, NEVER launches anything. Polls `pgrep -x <process>`
  and follows `journalctl -f --since now`. ok = process alive `alive_seconds` in a row; fail = a
  `fail_patterns` line, a gamescope core dump, or two exits within 20 s of starting; no launch
  before the timeout = `inconclusive` (trial stays open, nothing rolled back). ok calls keep, fail
  calls rollback, in the same process; the `watch` event that announces it has `verdict: "ok"|"fail"`.
  SIGTERM while still watching = clean exit, nothing changed (the app's manual buttons use this).
- `--keep-held <ref>`: mask stays off, `good` := the new version/commit, hold moves to `released`
  with a `released` history event. "gamescope 3.16.30 kept. Pin removed."
- `--rollback-held <ref>`: `flatpak update --commit=<from_commit>`, re-mask, tried version appended
  to `bad_versions`, trial cleared. Works from the on-disk trial alone (after a crash or reboot).
  If the rollback itself fails the trial stays open and the bad build is NOT pinned in place.
  "Rolled back to 3.16.28 and pinned again. 3.16.30 is marked bad."
Privilege: plain `flatpak` as the user (flatpak-system-helper + polkit), as everywhere else, except
that the helper refuses `update --commit=` ("Can't update to a specific commit without root
permissions"): the rollback runs through `sudo -n` when that works without a password (never
interactive) and `pkexec` otherwise; with `sudo -n` available the mask changes use it too, so an
unattended rollback needs no prompt.

**Bridge**: commands `tryHeld {ref, force?}`, `verifyHeld {ref, timeout?}`, `keepHeld {ref}`,
`rollbackHeld {ref}`; events reach the UI as `{"type":"held","event": <event>}`. Each is one
rivet-apply process with its own chunked reader (zero-length read = EOF). The UI sends
`verifyHeld` itself when a try ends `awaiting_verify`. A manual keep/rollback stops a running
watcher first (manual always wins), unless the watcher already emitted a `verdict`. A held process
that dies without `done` becomes one synthetic `done`/`fail` with `held.result: "failed"`; it never
triggers a rollback. `--demo` plays a canned trial and never runs rivet-apply.

## Other software (staleness outside dnf/Flatpak), added 2026-10-31

Named "Elsewhere" in its first version (read-only, same day); renamed for clarity.
The section is **Other software**, subtitle "Outside dnf and Flatpak". Code identifiers and
the state.json key stay `elsewhere` (the contract is additive; renaming the key buys nothing).

Why: "what else on this box is stale" had no answer. npm globals (CLI tools, local servers),
Home Assistant add-ons and container images age silently because nothing in dnf or Flatpak
knows about them. Collecting only LOOKS. Since 2026-10-31 (b) each row also says what the
thing is and who uses it, and user-level rows can be updated from the row (see "Update
buttons" below); everything else keeps a command to copy or a page to open.

**Collectors** (`backend/elsewhere.py`, stdlib only), one function per source, run concurrently
on daemon threads, each with a hard deadline (10 s). A collector that raises, times out or cannot reach its registry is
`failed`, **never "up to date"**:

| source id   | how                                                                 | suggested action |
|-------------|----------------------------------------------------------------------|------------------|
| `npm`       | for EVERY global prefix: `npm outdated -g --json --prefix P` (rc 1 = something is outdated) + `npm ls -g --depth=0 --json --prefix P` for the count. Prefixes (2026-10-31 b, deduplicated by real path, only those with `lib/node_modules`): `npm prefix -g`, `~/.npm-global`, `~/.npm-canvas-new`, `$RIVET_NPM_PREFIXES` (colon list), every `~/.nvm/versions/node/*` (driven by that Node's own npm). The npm binary is `$RIVET_NPM_BIN`, else the newest nvm Node's `bin/npm` with its bin dir first on PATH (never the `npm` shell function, never `/usr/bin/npm` = Node 22 when nvm exists). npm/corepack inside an nvm Node dir are counted, not listed (they come with Node). A `latest` OLDER than the install (an install from a newer dist-tag) is not stale. Row `where` = the prefix (`~/.npm-global`); a name stale in two prefixes gets `id` `npm:<name>@<prefix>` for the non-default one. Description: `registry.npmjs.org/<name>/latest` `description`. | `<npm> i -g --prefix <prefix> <name>@latest` |
| `pipx`      | `pipx list --json` + `https://pypi.org/pypi/<pkg>/json` (`info.summary` = `what`) | `pipx upgrade <pkg>` |
| `uv`        | `uv tool list` + PyPI (uv has no outdated command)                    | `uv tool upgrade <tool>` |
| `cargo`     | `cargo install --list` + `https://crates.io/api/v1/crates/<name>` (`max_stable_version`, `description`); path/git installs are counted, not compared | `cargo install <name>` |
| `ha`        | `GET <HA_URL or http://homeassistant.local:8123>/api/states` with a Bearer token; `update.*` entities with state `on` (`title`, `installed_version`, `latest_version`, `release_url`, `supported_features`, `device_class`), plus ONE `POST /api/template` (2026-10-31 b) that returns the Supervisor's and HACS's update entities, each entity's device (manufacturer, model, name) and entity counts for the integrations an add-on backs (matter, mqtt, wyoming, alexa_media). Row `kind`: `core`, `os`, `supervisor`, `addon` (Supervisor "app", runs ON THE HA VM, not on the PC), `integration` (HACS), `firmware` (`device_class: firmware`), `other`; a failed template falls back to the attributes. | add-on / integration with the INSTALL feature: **Install in HA**; core / os / supervisor / firmware / other: link `<ha>/config/updates` (integrations: `release_url`) |
| `podman`    | `podman auto-update --dry-run --format json` (`Updated: pending`) + for every non-`localhost/` image of `podman ps -a`: `skopeo inspect --format {{.Digest}} docker://<image>` vs the image's `RepoDigests`, plus `skopeo list-tags docker://<repo>` (2026-11-01: versions, see "Containers"). Reused for 6 h (registries rate-limit anonymous manifest reads). Row facts (2026-10-31 b): `units` (the containers' `PODMAN_SYSTEMD_UNIT` label = their quadlet), `containers`, `image`, `restart` (`systemctl --user restart <units>`, else `podman restart <names>`). When the TAG already points at the registry digest but the container still runs the old image, the row is `pulled: true` with the restart as its command. Description: Docker Hub `description` (cached in know-cache.json). | `podman pull <image>`; after a pull: the restart (copy) |
| `gnome-ext` | `gnome-extensions list --user` + each `metadata.json` `version` vs `https://extensions.gnome.org/extension-info/?uuid=…&shell_version=<major>` (`description` = `what`); extensions with no EGO version (home-made) are skipped | link: the EGO page |
| `appimage`  | `*.AppImage` under `~/Applications` (`RIVET_APPIMAGE_DIR`): always `cant_check`; the hint says whether update info (zsync) is embedded | none |

A source whose tool is not installed is `absent` and shows nothing.

**Home Assistant token.** `$HA_TOKEN` when set, else the `HA_TOKEN=` line of
`~/.config/rivet/ha.env` (same for `HA_URL`; a test's env dict never reads the file). The token is never logged, written to state or passed on. This is the only credential any
Rivet run reads; triage and the doctor still read none (see "Triage model"). No token: the source
is `absent` (2026-11-01 d; with `HA_URL` set but no token: `cant_check`, hint "set HA_TOKEN to a
long-lived access token"). Collecting only reads with it
(`GET /api/states`, `POST /api/template`, which renders and changes nothing); the one write is
**Install in HA** (`POST /api/services/update/install`), and only from `rivet-apply --other-update`.

**What it is / used by** (2026-10-31 b, `backend/know.py`). Two short lines per row, filled
cheapest source first; a later layer only fills what an earlier one left empty:
1. the collectors: registry descriptions (above) -> `what`; Home Assistant's kind -> `used_by`
   ("Add-on on the HA VM. Not on this PC.", "nothing uses it yet (0 Matter entities)",
   "HACS integration inside Home Assistant (needs an HA restart after updating)", "Device
   firmware … update it from the vendor app");
2. `know.STATIC` (generic entries such as pnpm or tauri-driver, plus this box's own from the
   git-ignored `backend/know.local.json`, e.g. couchdb -> "notes sync"; it wins
   over the registry's `what`) and a cheap read-only grep for
   `used_by`: user units (`~/.config/systemd/user/*.service`), `~/projects/*/{package.json,
   Makefile,justfile,README.md,CLAUDE.md,scripts/*.sh}` (never Rivet itself), compose files
   `~/projects/*/*compose*.y*ml` and the podman `units`. pnpm: the projects with a
   `pnpm-lock.yaml` ("Builds Notes (~/projects/notes)"), and on a major bump `warning` says per
   project whether `package.json` `packageManager` pins pnpm ("Notes pins 9.x") or not (then
   the lockfile format and any `pnpm@N` in its scripts);
3. `claude -p --model haiku` (`RIVET_KNOW_MODEL`; the Claude Code CLI on the subscription, no
   API key, `--tools "" --setting-sources ""`), ONE call per name+kind for rows still missing a
   line, asked for `{"what", "used_for"}`, cached in `~/.local/state/rivet/know-cache.json`
   (key `<source>[:<ha kind>]:<name>`; Docker Hub descriptions under `hub:<image>`). It runs in
   `rivet-collect` AFTER state.json is written (written again if a line was filled), 4 in
   parallel, 60 s per call, 150 s in all and never past 950 s into the run (the unit's
   TimeoutStartSec is 1015 s), never with `--no-llm`, never with a fixture. A refusal
   or timeout leaves the line empty and is retried after 6 h; an answer (or "never heard of it")
   is final. Lines from this layer are listed in the row's `ai` (`["what"]`, …) and tagged
   **AI** in the UI.

**state.json**, top level, written by `rivet-collect` (absent until the first run that has it;
carried unchanged in the `checking` stub and in `error` documents):

```json
"elsewhere": {
  "checked_at": "2026-10-31T09:41:20-04:00",
  "duration_ms": 1830,
  "counts": {"stale": 8, "failed": 1, "cant_check": 1},
  "sources": [
    {"id": "npm", "title": "npm globals", "status": "ok | failed | cant_check | absent",
     "checked": 4, "stale": 2, "error": null, "hint": null,
     "checked_at": "…", "duration_ms": 1131, "reused": true}
  ],
  "items": [
    {"id": "npm:pnpm", "source": "npm", "name": "pnpm",
     "installed": "9.15.4", "latest": "12.16.1",
     "severity": "major | minor | patch | unknown",
     "checked_at": "…", "status": "ok | cant_check | failed | updated",
     "link": "https://www.npmjs.com/package/pnpm" | null,
     "hint": "one plain sentence" | null,
     "command": "~/.nvm/versions/node/v26.12.1/bin/npm i -g --prefix ~/.npm-global pnpm@latest" | null,
     "what": "JavaScript package manager, a faster npm." | null,
     "used_by": "Builds Notes (~/projects/notes)" | null,
     "ai": [],                       // "what" / "used_by" when claude -p wrote that line
     "warning": "Major jump 9 → 12. Notes does not pin pnpm (…)" | null,
     "where": "~/.npm-global" | null,
     "action": "update | ha_install | open | copy" | null,
     "confirm": true,                // a major bump asks once in the row before it runs
     "prefix": "/home/you/.npm-global", "npm": "/home/…/v26.12.1/bin/npm",
     "last_run": {"at": "…", "result": "updated | installed | pulled | failed", "note": "…" | null}}
  ]
}
```

- `items` holds only what needs attention: stale rows (`status: "ok"`, latest newer than
  installed), per-package lookups that failed (`failed`), AppImages (`cant_check`), and ONE
  row per `failed` (or wholly `cant_check`) source with `id: "<source>:_source"`, `name` = the
  source title, `installed`/`latest` null and the reason in `hint`. Up-to-date packages are only
  counted (`sources[].checked`). `reused` is set when a source's previous result was kept
  (`podman`, 6 h).
- `severity` is positional: the first differing numeric component 0 = `major`, 1 = `minor`,
  later = `patch`; CalVer is read the same way (2026.10.3 -> 2026.10.5 = patch). Single-number
  versions (extensions.gnome.org) and digests are `unknown`.
- Order: stale rows by severity (major, minor, patch, unknown), then source, then name; then
  `failed`; then `cant_check`.
- `counts.stale` = rows with `status: "ok"`. Nothing in `elsewhere` ever feeds `summary`,
  `flags`, `last-pending.json` or the notification.
- `status: "updated"` (2026-10-31 b) is written only by `rivet-apply --other-update` into the
  row it just ran (installed = the new version, `action` null, or for a podman pull `action:
  "copy"` with the restart command); it sorts last, is not counted as stale and disappears
  with the next check (which only lists stale rows).
- `action` (computed by `elsewhere.decide_action()` after enrichment): `update` = npm, pipx,
  uv, cargo, `podman pull` (status ok, a command, not a pending podman auto-update, not a
  pulled-but-not-restarted image); `ha_install` = HA `addon`/`integration` with
  `installable`; `open` = HA core/os/supervisor/firmware/other and GNOME extensions (their
  link), and failed rows that have a link; `copy` = anything else with a command (podman
  auto-update, the restart after a pull). `confirm` = `severity: "major"` and action
  update/ha_install.

**Timing.** The Elsewhere thread starts right after the `checking` stub, in parallel with
dnf/Flatpak/fwupd, and never delays them. When the main check is done first (it essentially
never is), state.json is written with the PREVIOUS `elsewhere` and written again when the
thread finishes (at most `max(timeout) + 5` s later). `--fixture` runs never collect it (a
fixture may carry a ready `elsewhere` object); `--no-elsewhere` / `RIVET_NO_ELSEWHERE=1`
switch it off (the backend tests set it).

**Update buttons** (2026-10-31 b; an Open link left the work to the user). The
UI sends `otherUpdate {id}`, never a command; the bridge runs `rivet-apply --other-update ID`,
which REBUILDS the argv from the row in state.json (`backend/elsewhere_apply.py`; names must
match `^[@A-Za-z0-9][@A-Za-z0-9._/:+-]*$`, no shell): npm `<npm> i -g --prefix <prefix>
<name>@latest` (that Node's bin first on PATH), `pipx upgrade`, `uv tool upgrade`, `cargo
install`, `podman pull <image>` (never a restart: the done event names it), all as the user,
no root, no prompt, 20 min cap. **Install in HA** = `POST <ha>/api/services/update/install
{"entity_id", "backup": true when the entity has the BACKUP feature}` (30 s client timeout;
HA answers after ~10 s even when still working), then `GET /api/states/<entity_id>` every 3 s
(`RIVET_HA_POLL_S`) until `in_progress` clears and the entity is `off` / at
`latest_version` (20 min cap, `RIVET_HA_TIMEOUT_S`; not in progress and still old after 3
polls = failed). Only `addon` and `integration` rows: HA core, OS and supervisor restart or
reboot the VM and keep **Open in HA** (done by hand); firmware is Open only.
Events: the rivet-apply JSON lines with `step: "other"` (start with `log: "$ <command>"`, one
progress event per output line, ok/fail), and `done` with `other: {id, result: "updated |
installed | pulled | failed | refused", item: <the row after the run> | null, restart?}`;
the same row is patched into state.json (tmp + rename, counts recomputed). A row that is not
in state.json, has another action, or is HA core/os/supervisor/firmware ends `done fail`,
`result: "refused"`, exit 2, having run nothing.

**UI.** A section "OTHER SOFTWARE" with the note "Outside dnf and Flatpak · …" after Held back
in the update list, and below the hero when everything else is up to date (like Held back). One
`.row.ew-row[data-id]` per item: severity dot, source tag, name, `installed → latest` with the
bumped part in the severity colour, then **What it is** (`.ew-what`) and **Used by / where**
(`.ew-used`, `used_by` · `where`), each with a small `.ai-tag` "AI" when listed in `ai`, the
`warning` in amber (`.ew-warn`) or, failing everything, the hint; a badge (MAJOR red / MINOR
amber / PATCH green / NEW VERSION grey; CHECK FAILED solid red; CAN'T CHECK grey; UPDATED
green) and one button by `action`: **Update** / **Install in HA** (`otherUpdate`; with
`confirm` the first click opens `.ew-confirm` in the row: the exact command or HA call, the
warning, Cancel / "Update anyway"), **Open in HA ↗** / **Open ↗** (`openUrl`), **Copy**
(`copyText`, toast "Copied …"). While a run streams, the row shows `.ew-run` (status line + the
last output lines); other rows' Update buttons are disabled; afterwards the row shows the
result (green "Updated to …", podman: what to restart; red on failure with the log) until the
next state push without a run. No checkbox, never selectable, never in `apply`. The header
subline gains "· N more in Other software ↓" (a button that scrolls to the section; not a
summary chip, the summary row is full at 1280 px). Rows expand for the hint, the command, the
link and the per-source status.

**Front page** (2026-10-31 c; supersedes the row layout described under "UI" above where they
differ). A UX review: the hero said "You're up to date" about dnf/Flatpak while 2 held
and 12 other updates sat below the fold.
- **Status strip.** `view: empty` (nothing pending in `groups` dnf/flatpak/fwupd) with any
  Held back item OR any `elsewhere.items` row shows `.status-strip` where the updates view has
  its summary bar: a glyph (`.glyph.ok` green check; `.glyph.warn` when something needs a look)
  and ONE sentence of segments, e.g. "Fedora and Flatpak: up to date · 2 held back · 12 other
  updates" (`.st-seg`; the counted ones are buttons that scroll to their section; a failed
  Other-software check adds "· N check(s) failed"), with a quiet subline "Kernel … · Secure
  Boot on". The header keeps the only **Check now**; its "N more in Other software ↓" pointer
  is dropped on this screen (the strip says it). No rows anywhere: the hero stays, trimmed
  (smaller ring, one sentence, the kernel line in fine print, no button). The updates view is
  unchanged (it has the summary bar, no hero).
- **One-line rows.** `.row.ew-row` = [`.src` tag, This PC only] name, `installed → latest`,
  at most one muted one-line `.ew-hint` (a `warning`, "nothing uses it; consider removing", a
  failed/can't-check reason, a pulled-but-not-restarted note), badge, [secondary button],
  button, chevron. What it is / Used by / Where and the full warning live in `.ew-expand`,
  opened by the row or its chevron. No severity dot: the badge carries the status (MAJOR red,
  MINOR amber, PATCH green, NEW VERSION grey, podman digest-only change NEW BUILD grey; CHECK
  FAILED, CAN'T CHECK, UPDATED / PULLED as before). Held back rows keep their dot (dot and
  badge share one colour there) and become one line too (reason as the hint, "held since" in
  the details).
- **Groups.** `.ew-group[data-group]` sub-headings with counts, in this order: `pc` "This PC"
  (npm, pipx, uv, cargo, gnome-ext, appimage), `ha` "Home Assistant", `containers`
  "Containers" (podman); an empty group is not shown. Within a group: stale rows by
  severity (major, minor, patch, unknown; an `updated` row keeps its severity's place), then
  `failed`, then `cant_check`, each stable in document order.
- **Remove** (2026-11-01, supersedes "Remove?": do it like the others). See "Remove an unused add-on" below. A row that only says "nothing uses it yet"
  in `used_by` (from before (c), or an integration) without `unused: true` keeps a secondary
  **Remove in HA ↗** that opens `addon_link` (`openUrl`) and uninstalls nothing.
- **Safe set** (UI and bridge use the same rule): `status: "ok"`, `action` `update` or
  `ha_install`, `confirm` false, `severity` not `major`, no `warning`, not `pulled`, `kind` not
  core/os/supervisor/firmware, AND (`severity: "patch"` OR `source: "podman"` OR (2026-11-01 c)
  `source: "ha"` with `kind` `addon`/`integration`, i.e. HA add-ons and integrations at any
  non-major bump; a major still asks in its own row). (2026-11-01 c) Every row in the
  confirmation has a checkbox (`.bulk-list .cb[data-action="ew-bulk-toggle"]`), all ticked;
  unticking leaves it out, the button reads "Update N" for the ticked ones and is disabled at 0,
  and `otherUpdateMany` gets only the ticked ids. When a ticked row is a HACS integration the
  confirmation says "Then Home Assistant restarts once (about a minute) to load …" and each such
  line's call reads "…, then one Home Assistant restart to load it". HA core / OS / supervisor rows
  stay Open only and say so in the row: "Open only: installing it restarts Home Assistant, so you
  do it there." A group with
  two or more safe rows shows **Update the safe ones (N)**; one click opens a confirmation
  listing every row it will touch with the exact command / HA call; confirming sends
  `otherUpdateMany {ids}`. While it runs the group button reads "Updating i of N" with **Stop
  after this one** (`otherStop`), each row streams into its own `.ew-run` as for a single
  Update, and other Update buttons are disabled.

HA row facts added in (c), from the same `/api/template` render (each update entity's device
`identifiers`): `addon_slug` (the `hassio` identifier of an `addon` row, e.g.
`core_matter_server`), `addon_link` = `<ha>/_my_redirect/supervisor_addon?addon=<slug>` (HA's
own my-link handler, which maps to the add-on page of whatever frontend version runs), and
`unused: true` when the integration the add-on backs (`HA_BACKS`) has 0 entities.

**The `unused` rule.** `unused` is present (and only ever `true`) on an `addon` row whose title
is a key of `HA_BACKS` (Matter Server → `matter`, Mosquitto broker → `mqtt`, Piper / Whisper →
`wyoming`, …) when `integration_entities(<domain>) | count` was 0 at the last check. No other
row has it: an add-on Rivet cannot map to an integration is never "unused", and an HA row only
exists while its `update.*` entity is `on` (an add-on with nothing pending is not listed at all).

**Remove an unused add-on** (2026-11-01, additive). An `addon` row with `unused: true` and an
`addon_slug` shows a secondary **Remove** button (ghost style, disabled like Update while
anything in Other software runs). It opens a confirmation modal in the style of "Update the
safe ones" (`.ew-remove-modal`): "Remove <name>?", "Nothing in Home Assistant uses it (0 <X>
entities)", that Home Assistant uninstalls the add-on and deletes the data it keeps, that
integrations that depended on it are not touched (they stay configured and report they cannot
connect), the exact call, and **Cancel** / **Remove**. Remove sends:

    {"cmd": "otherRemove", "id": "<row id>"}

The bridge runs `rivet-apply --other-remove ID` (refused while another run or a bulk update is
going, like `otherUpdate`), streamed as `{"type":"other","event":ev}` exactly like an Update
(the row shows `.ew-run` busy state; events land in `logs/apply.log`). rivet-apply rebuilds
everything from the row in state.json and refuses (`done fail`, `result: "refused"`, exit 2,
nothing sent to HA) unless: `source: "ha"`, `kind: "addon"`, `unused` is exactly `true`,
`addon_slug` matches `^[A-Za-z0-9_.-]{1,80}$`, and the title is in `HA_BACKS`. Then:
1. **Live re-check**: `POST <ha>/api/template` counting the backed integration's entities NOW.
   Any entity, or no answer, ends `done fail` with nothing removed.
2. Over HA's **websocket** (`/api/websocket`, the same HA_TOKEN; it must be an admin's), the
   Supervisor through `supervisor/api`: `GET /addons/<slug>/info` (already "not installed" /
   "does not exist" = done ok, nothing sent), then `POST /addons/<slug>/uninstall
   {"remove_config": false}` (the add-on and its `/data` go; its `addon_configs` folder, if
   any, stays). The REST proxy `/api/hassio/addons/...` is NOT used: it answers 401 to a
   long-lived token (verified 2026-11-01).
3. Poll `GET /addons/<slug>/info` every `RIVET_HA_POLL_S` until the Supervisor says it is not
   installed (an error "not installed" / "does not exist", or no `version`), cap
   `RIVET_HA_REMOVE_TIMEOUT_S` (300 s).
On success: `done ok`, `other: {id, result: "removed", item: null, removed: true}`, and the row
is DROPPED from state.json (tmp + rename, counts recomputed); the UI removes the row and toasts
the summary. On failure: `done fail`, `other: {id, result: "failed", item: <row with last_run
{result: "remove_failed", note}>, link: addon_link}`; the row stays and its red `.ew-run` shows
the reason plus **Open in Home Assistant ↗** (`addon_link`). Integrations (config entries) have
no Remove.

**Install in HA, judged against its target** (2026-11-01 c, supersedes the success rule in
"Update buttons" above). `run_ha` first reads the entity (`GET /api/states/<entity_id>`): when its
`installed_version` is already at or past the row's `latest` (e.g. HACS downloaded it earlier)
nothing is installed. Otherwise `update.install` carries `version: <row latest>` when the entity
has the SPECIFIC_VERSION feature (2), so what the confirmation listed is what runs. Success =
`installed_version` not older than the row's `latest` (never the live `latest_version`, which can
move while installing); "still X, wanted Y" otherwise. For `kind: "integration"` the done event
and the patched row carry `restart_pending: true` (HACS loads a new version only after a core
restart; an already-downloaded one only when its `release_summary` says "Restart of Home
Assistant required"), summary "<name> <v> is downloaded; Home Assistant restarts to load it."
When a newer version appeared meanwhile the summary adds "(<v> is out too; it shows up at the
next check.)". The collector sets `restart_pending: true` on an integration row whose
`release_summary` says a restart is required. A row with `restart_pending` and no run shows the
hint "Downloaded; Home Assistant needs a restart to load it." and a secondary **Restart HA**
(in-row confirmation, then `haRestart`).

**HA core restart** (2026-11-01 c). `rivet-apply --ha-restart [TAG]` (TAG = the bridge's row
tag `ha:_restart`, ignored). Over the websocket: HACS's `repairs/list_issues` entries with
`translation_key: "restart_required"` plus state.json HA integration rows with
`restart_pending` whose loaded version (`manifest/get`) differs from `installed`. Nothing
pending: `done fail`, `result: "refused"`, exit 2, nothing sent. Else `POST
/api/config/core/check_config` (anything but `valid` = no restart, `done fail`), `POST
/api/services/homeassistant/restart`, poll `/api/config` every `RIVET_HA_POLL_S` until it went
down and is `RUNNING` again (cap `RIVET_HA_RESTART_TIMEOUT_S`, 600 s; never down within 120 s =
fail), then per row: `manifest/get` and the entity again. Loaded = installed and no repair left:
row `status: "updated"`, `restart_pending: false`, `loaded: "<v>"`; otherwise the row keeps
`restart_pending` and the run fails ("did not load …"). Events `step: "other"`, done
`other: {id: "ha:_restart", result: "restarted | refused | failed", item: null, items: [rows]}`.
Never the VM, the OS or the supervisor. **Bridge**: `haRestart {}` spawns it through
`otherUpdate`'s plumbing (row id `ha:_restart`, refused during a bulk run); the bridge also
spawns it by itself once after an `--other-update` whose done has `other.restart_pending` (a
single Install in HA), or once at the end of a bulk run that had one (before the `batch` event,
also after Stop). UI: while it runs the HA group's header shows "Restarting Home Assistant…"
(`.ew-ha-restart`, the restart's own messages), rows waiting for it keep a busy panel, every
Update is disabled; at the end each row reads "<name> <v> is loaded: Home Assistant restarted."
(or the failure).

**Fix it on a failed row** (2026-11-01 c, `backend/other_fix.py`). A row whose
`last_run.result` is `failed` or `fix_failed` (update failures; a failed Remove keeps its HA link)
shows the reason and **Fix it** (in the red `.ew-run` panel; after a reload, as the row's hint plus
a secondary button). Bridge `otherFix {id}` → `rivet-apply --other-fix ID`, streamed exactly like
an Update (`step: "fix"` events, refused during a bulk run; `--demo` plays a canned one). It:
1. gathers, read-only: the row (name, versions, kind, entity), the apply.log lines of the row's
   LAST run (from its last `done` back to the previous `done`), for HA rows the last **200**
   relevant lines of HA core's log since its start (Supervisor `core/logs/latest` through the
   REST proxy; matched on the integration domain, the entity, the name, `hacs`, `restart`;
   `/api/error_log` no longer exists in 2026.x) and the live facts (entity, `/api/config`, HACS
   restart repairs, the version HA has LOADED);
2. runs `claude -p --model opus` (`RIVET_FIX_MODEL`; fallback `RIVET_FIX_FALLBACK_MODELS`,
   sonnet) through `rivet_doctor.run_claude`: the subscription CLI, never `--bare`, no API key
   in its environment (the HA token neither), `--setting-sources "" --strict-mcp-config
   --permission-prompts none --no-session-persistence`, `--tools Bash`, `--json-schema` below,
   at most 14 turns, 30 min (`RIVET_FIX_TIMEOUT`);
3. tool budget = exact commands, nothing else: `rivet-apply --fix-tool state ID` (live facts
   again), `logs ID` (the log lines again), `retry ID` (`rivet-apply --other-update ID` once),
   and for an HA integration also `restart ID` (`rivet-apply --ha-restart`, which refuses unless
   a restart is pending) and `reload ID` (reloads that integration's config entries,
   `/api/config/config_entries/entry?domain=` then `…/entry/<id>/reload`). They are the
   `--allowedTools` (`Bash(<cmd>)`, no wildcard) AND a PreToolUse hook (`backend/fix_guard.py`,
   passed with `--settings`) that denies every other tool call, because Claude Code auto-approves
   read-only shell commands (cat, ls, grep, head, sed) in every permission mode (verified
   2026-11-01). `--fix-tool` refuses outside a Fix it session (`RIVET_FIX_SESSION` dir with the
   id, verbs and commands), refuses another row's id or a verb outside the budget, and runs
   `retry` / `restart` / `reload` at most once per session (exit 3 the second time);
4. answer `{fixed: bool, result: "Fixed: … | Not fixed: …" (one line, shown in the row), cause,
   actions[]}`. A claimed fix is checked: the entity must be at the row's `latest` and, for an
   integration, loaded at it with no restart repair left; otherwise it is reported as "Not fixed:
   <what HA still says>". Done `other: {id, result: "fixed | not_fixed | refused", item: <row with
   last_run {result: "fixed" | "fix_failed", note: result}>, fix: {fixed, result, claude_result,
   cause, actions, ran, verified, model, checks, denied}}`; the row is patched in state.json.
   Claude unreachable / out of usage / timed out: "Not fixed: Claude could not finish (…)" plus
   what had run. Every event (narrated tool calls included) lands in `logs/apply.log`; retry and
   restart log their own runs there too.
`rivet-apply --other-fix ID --dry-run` uses the canned 2026-11-01 Alexa evidence
(`backend/fixtures/fix-alexa.json`), builds the same prompt, budget and command line, asserts them
(budget, exact allowlist, no wildcard, Bash only, the guard hook and that it denies cat / Read / a
chained command, opus, no --bare, no API key, row facts + apply.log excerpt + ≤ 200 HA log lines in
the prompt), prints them, and calls neither claude nor Home Assistant (exit 0 / 1).
`RIVET_FIX_CANNED=<file>` makes a real run use canned evidence and canned tool answers (app QA,
with `RIVET_CLAUDE_BIN` = `app/qa/fake-claude.py`).

**Containers off by default** (2026-11-01 c). Settings `show_containers` (bool, default false; app `DEFAULT_SETTINGS` and the
backend's `common.DEFAULT_SETTINGS`; Preferences › Other software › "Show containers"). Off:
`rivet-collect` does not run the podman collector (no `podman` source, no podman rows), and the
UI drops podman rows and the Containers group everywhere (section, counts, strip, safe sets)
even from an older state.json. Turning it on saves and starts a check. Everything below is
unchanged behind the flag.

**Containers** (2026-11-01, supersedes the digest rows and NEW BUILD above). A container row
never shows a digest. Per running image (`podman ps -a`, not `localhost/`):
- **Running version**: a fully specified tag (`1.28.1`, three or more numbers) wins; else the
  image's own `<NAME>_VERSION` env (`COUCHDB_VERSION`, `REDIS_VERSION`, `GHOST_VERSION`;
  `PG_VERSION` for postgres; distro suffixes like `-1.pgdg13+1` dropped); else the OCI
  `org.opencontainers.image.version` label when it fits the tag; else the tag ("latest").
- **Newest tag**: among `skopeo list-tags` of the same repo, tags of the form `<x.y[.z…]><suffix>`
  where the suffix is the running tag's flavour (`17-alpine` only compares `*-alpine`); release
  candidates and bare majors (`16`) are not versions. For a floating tag (`3`, `7.2`, `latest`)
  its own track (3.*) is preferred: when a newer version exists IN the track and the registry
  digest of the tag moved, `latest` is that version and pulling the same tag brings it
  (`pull_gets_it`). Otherwise `latest` is the repo's newest version tag (`newest_tag`).
- **Rows**: newer version → `status: "ok"`, `installed` → `latest` as versions, severity from the
  versions, `link` = the project's release notes (`know.RELEASE_NOTES`) or the registry's tags
  page. Same version but the registry digest moved → `status: "rebuilt"`, `installed` = `latest`
  = the version, `severity: "unknown"`, hint "Image rebuilt upstream (same version)." (plus the
  restart when the new build is already pulled); the UI shows NO badge, NO button and no hint
  on the row, only that line in the details; it is not counted as an update (`counts.stale`,
  the strip, the section count) and sorts after `cant_check`. Same version, same digest → no row.
- **Holds data** (`stateful: true`): the image repo is in `know.STATEFUL_IMAGES` (couchdb,
  postgres, redis, valkey, mariadb, mysql, mongo, temporalio/*, ghost) or a container / unit name
  starts with an app in `know.STATEFUL_APPS` (`postiz-`, `obsidian-`, `ghost-`). Never an Update
  button, never in the safe set. `action: "runbook"` with `runbook` (URL) / `runbook_title` when
  `know.RUNBOOKS` knows the app's runbook (from `know.local.json`), shown as **Open
  runbook ↗** (`openUrl`); otherwise `action: null` and `manual: "Manual: holds data, upgrade by
  hand"`, shown as plain text where the button would be.
- **Stateless** with a newer version: **Update** (`podman pull <image>`, as before) only when
  `pull_gets_it`; a pinned tag (or a newer version outside the floating tag's track) gets
  `action: "open"` (the release notes / tags page) and the hint "Pinned to <tag>: change the tag
  where <containers> is defined to upgrade."

**Shell extension.** Unchanged and must stay so: it reads `groups`/`summary`, and `elsewhere` is
neither, so Other software can never inflate the top-bar badge. (There is no flag for counting it; one
would need its own contract change.)

## Robustness (added 2026-10-04)

**Atomic writes.** The backend writes `state.json`, `holds.json`, `last-pending.json` and
`triage-cache.json` with `common.write_json_atomic`: a temp file in the same directory, fsync,
`os.replace`. A reader (the app's file monitor, the extension) never sees a partial document;
the only thing it can observe is the old file or the new one. Temp files are named
`.state.json.<random>.tmp` and are unlinked on failure.

**`error: "interrupted"`.** `rivet-collect` installs SIGTERM / SIGINT / SIGHUP handlers before
it writes the `checking` stub. A signal while the stub is on disk (systemd `TimeoutStartSec`,
logout, Ctrl-C in a terminal) writes, atomically, the same carried document as the stub but with
`status: "error"` and `error: "interrupted"` (previous `groups`, `flags`, `summary`, `checked_at`;
`generated_at` = now), terminates its own child processes, and exits `128 + signal`: **143** for
SIGTERM, 130 for SIGINT. Once the final document is written the handler is disarmed, so a late
signal cannot replace a good result (the exit code is still 128+signal). The app should treat
`error == "interrupted"` as "the last check did not finish; check again", not as a broken
system; the groups it carries are the previous run's.

**Logs rotate.** `logs/apply.log`, `logs/collect.log`, `logs/doctor.log` are renamed to
`<name>.1` (one generation kept) when they exceed 2 MB, checked when a run opens the log, so a
single run's events are never split across two files. `done.log_path` keeps naming the live file.

**stdout can go away.** When the app window is closed mid-run, `rivet-apply` and
`rivet-doctor` get a broken pipe on stdout. They note that once in their log
(`{"step":"log","status":"note","msg":"stdout closed …"}`), stop writing to stdout, and finish
every remaining step (akmod wait, verify, Flatpak, fwupd) with all events, `done` included, in
the log file. The process therefore outlives the window; an app that restarts should read the
tail of `apply.log` for the `done` of a run it did not watch to the end rather than assume a
closed pipe stopped anything.

**Child timeouts hold during silence.** Every streamed child (`flatpak`, `fwupdmgr`, the fix
commands of `rivet-doctor run`, the Claude CLI) is read on a thread with a wall-clock deadline,
so a child blocked on a polkit prompt or a stalled CLI is killed AND reaped at its timeout
(step event `fail`/`progress` "timed out … and was killed", rc 124) instead of hanging until its
next output line. `common.run` gives every child `stdin=/dev/null`, so a hand-run collector can
never sit on an `fwupdmgr` or `dnf` prompt. The Claude CLI's stderr is merged into stdout and
shows up as `doctor progress` log lines.

**holds.json is never overwritten when unreadable.** If the file exists but is not a JSON
object, `holds.load()` keeps a copy as `holds.json.bad-<YYYYmmdd-HHMMSS>` (once per distinct
content), logs it, and returns an empty registry: masked refs still show as `hold_state:
"pinned"` with the no-note reason. `holds.save()` raises `RegistryUnreadable` instead of
writing, so `--try-held / --keep-held / --rollback-held` end with the usual `done` event,
`status: "fail"`, `held: {ref, action, result: "failed"}` and a `summary`/`cause` that names the
file and the copy; no mask is touched. The candidate notification is simply not recorded.

**Collect budget.** `systemd/rivet-collect.service` `TimeoutStartSec=1015`: the longest
per-step chain (Flatpak: 2x30 mask + 2x180 remote-ls + 2x60 list + 180 dry run = 720 s; the dnf
chain is 540 s and runs in parallel) + 15 s host facts + 220 s triage (two rounds of 4 parallel
110 s calls, 96 clusters) = 955 s, plus 60 s. A run that still overruns ends as
`error: "interrupted"`, exit 143.

## Backend CLIs

`rivet-collect [--no-llm] [--quiet] [--no-elsewhere]`
: Gathers dnf (`dnf5 check-upgrade`/`repoquery --upgrades` + `dnf5 updateinfo info`), Flatpak
(`flatpak remote-ls --updates --columns=...` for user + system), fwupd (`fwupdmgr get-updates --json`).
Writes state.json with `status: "checking"` first, then the full document. Runs triage
unless `--no-llm`. Exit 0 even when nothing is pending (empty groups); 1 on a failure
(`status: "error"`, `error: "<Type>: message"`); 128+signal when killed mid-check
(`error: "interrupted"`, see "Robustness"). Never starts a dnf,
Flatpak or fwupd transaction -- metadata reads only.

Notifications are **opt-in via `--notify`** (amended 2026-09-15): the systemd unit passes it,
an interactive or QA run does not, so a test can never pop a window over a full-screen game.
`RIVET_NO_NOTIFY=1` in the environment mutes it unconditionally. With `--notify` set, a
desktop notification goes out via Gio (`org.freedesktop.Notifications`, falling back to
`notify-send`) ONLY when the pending set changed since the last run (compare item ids): title "Rivet: N updates", body like "2 need reboot (kernel, NVIDIA), 1 blocked by Steam",
with the `desktop-entry` hint set to `xyz.scottnelson.Rivet`, which is what makes GNOME Shell launch
xyz.scottnelson.Rivet.desktop on click (`notify-send --action` implies `--wait`, which a Type=oneshot unit
must not do, so no action list is sent).

`rivet-apply --ha-restart [TAG]` (2026-11-01 c)
: Restarts Home Assistant core only when an integration waits for it (HACS `restart_required`,
or a `restart_pending` row), after `check_config`; waits until it is back and confirms the loaded
versions. Exit 0 restarted, 1 failed, 2 refused (nothing pending). See "HA core restart".

`rivet-apply --other-fix <ID> [--dry-run]` / `--fix-tool VERB ID` (2026-11-01 c)
: Fix it on a failed row (bounded `claude -p --model opus`); `--dry-run` asserts the prompt and
tool budget on canned evidence without claude. `--fix-tool` is the tool budget itself and works
only inside a Fix it session. See "Fix it on a failed row".

`rivet-apply --other-update <ID>` (2026-10-31 b)
: Updates ONE Other-software row of state.json as the user (npm / pipx / uv / cargo /
`podman pull`, or HA `update.install` for an add-on or HACS integration). Events with
`step: "other"`, `done.other = {id, result, item}`; exit 0 ok, 1 failed, 2 refused. See
"Other software", "Update buttons".

`rivet-apply --plan <plan.json>`
: plan.json = `{"items": ["dnf:kernel", "flatpak:org.freedesktop.Platform/x86_64/25.08", ...]}`.
Emits JSON lines on stdout, one event per line:
`{"ts": "...", "step": "download|install|akmod|verify|flatpak|fwupd|done", "status": "start|progress|ok|fail|skip", "pct": 0-100|null, "msg": "human line", "log": "raw tool output line"|null}`
and finally `{"step":"done","status":"ok|warn|fail","reboot_required":bool,"kmod_verified":bool|null,"summary":"..."}`.

Amended 2026-09-21 (all additions optional for a reader; an older UI keeps working):
- `done.status` gains **`warn`** = everything is installed, something on the way complained
  (exit code 0, amber in the UI, never red). Extra `done` fields: `cause` (one plain-English
  sentence, for warn and fail), `error` (the first real tool error line, or null), `warnings`
  ([str]), `log_path`, and `results`:
  `{"dnf": {"planned": N, "installed": M, "missing": [names], "leftover": [nevras]},
    "flatpak": {"ok", "failed", "skipped", "failed_names"}, "fwupd": {"ok", "failed", "failed_names"}}`.
- Step events may carry `warning` (str, on an `ok` event: ok-with-warning), `error` (str),
  `verify` (the `results.dnf` object), `cause`, and `phase: "main" | "updater"`.
- **dnf's verdict is verified, not trusted.** When the transaction reports an error, rivet-apply
  compares the rpmdb (`rpm -qa`, in memory, because `rpm -q name-0:V-R.A` never matches an
  explicit 0 epoch) with every planned package and every element dnf resolved: inbound packages
  must be present (any one of several installed versions will do, so kernel* is fine; newer than
  planned also counts), removed/replaced ones must be gone. All good → `install ok` + `warning`,
  plus a read-only `dnf5 check` (runs as the user, no prompt). Otherwise `install fail` with
  "M of N installed", the first ≤8 missing names and the first real error line.
- **Updater packages go last.** `dnf5*`, `libdnf5*`, `python3-libdnf5`, `dbus-broker`, `polkit`,
  `polkit-libs` are held out of the main transaction and applied in a second, small one, because
  upgrading dnf5daemon-server makes systemd restart it at the end of the transaction that is
  running inside it (2026-09-21: SIGTERM killed systemd's %transfiletriggerpostun, dnf history
  said "Error", all 458 elements were in fact installed). The UI still sees ONE `install` step:
  main fills 0-90 %, the updater phase 90-100 % (tagged `phase`), one final ok/fail.
  polkit's `auth_admin_keep` normally covers both; a main transaction longer than ~5 min can
  mean a second password prompt.
- Error text comes from, in order: the D-Bus error of `do_transaction`, `transaction_script_error`
  / `transaction_unpack_error` / failed-download signals, resolve problems, then
  `journalctl -u dnf5daemon-server --since @<tx start>` matched on the level word INSIDE the
  message (the daemon logs to stdout, so journald files everything at priority info and
  `-p warning..err` matches nothing).
- akmod wait + `verify` run whenever the plan has a kernel OR akmod-nvidia (it used to take both)
  and the new kernel/driver actually landed, whatever dnf said. With akmod-nvidia in the plan the
  kmod package must also carry the new driver EVR, not merely exist.

Amended 2026-09-22 (stale daemon metadata):
- **dnf5daemon-server has its own metadata cache** (`/var/cache/dnf5daemon-server`), not the one
  `dnf5 check-upgrade` fills for rivet-collect (`~/.cache/libdnf5`). It was 3 h behind, did not
  know kernel-7.2.6, resolved the plan to an empty transaction, and apply reported "12 of 12
  installed" then waited 600 s for a kmod for a kernel that was never installed. Rules now:
  - Every main-phase session starts with `Base.clean("expire-cache")` (no polkit action, verified
    unprivileged) so the daemon re-syncs every repo before resolving; the UI sees "Refreshing
    package metadata…" and the per-repo "Fetching …" download lines (resolve is pumped under a
    main loop now, so signals arrive during it). Cold cache: ~40-100 s.
  - After resolve, a planned package that is neither in the transaction (any action) nor already
    installed at >= its target is **not offered**: refreshed and resolved once more, then
    `install fail` with "0 of N installed", the missing names and the cause "dnf does not offer
    kernel-7.2.6-200.fc44 yet: its package metadata is behind the checker's." No transaction
    starts, no akmod wait, `kmod_verified` stays null.
  - **Success is verified too**: `verify_installed` runs on every outcome, `results.dnf` always
    carries real rpmdb numbers, and dnf saying "ok" while something planned is missing is a
    failure ("dnf said the transaction succeeded but X is not installed"). An empty transaction
    because everything is already at target is `install ok` ("Nothing to install: …").
  - The akmod wait only starts when the kernel/driver is actually in the rpmdb after the run.
- **Repo signing keys** (`repo_gpgcheck=1`, e.g. a vendor repo): the daemon emits
  `repo_key_import_request` and blocks resolve until `confirm_key` answers. rivet-apply confirms
  a key whose URL is a `gpgkey=` in `/etc/yum.repos.d` (what `dnf -y` trusts; polkit
  `confirm_key` is auth_admin, so a password prompt is possible) and refuses any other, which
  fails that repo load with a clear line instead of hanging.
Amended 2026-10-04 (akmods vs the updater transaction):
- **The akmod wait sits between the two dnf transactions.** When the main transaction installed a
  kernel or the NVIDIA driver (per the rpmdb) and an updater transaction follows, rivet-apply
  waits for akmods BEFORE starting the updater transaction (`install progress 90` "Letting akmods
  finish before the updater's own transaction…", then the normal `akmod start/progress/ok|fail`
  events, then "Now the updater's own N package(s)…"). The `akmod` step therefore runs once per
  run, in the middle of the `install` step; the end-of-run wait reuses that verdict and emits
  nothing. Why: 2026-10-04 akmods (async, root) finished building kmod-nvidia-7.2.8 while the
  deferred updater transaction held the rpm lock, could not install the RPM it had just built,
  and left a `.failed.log` that the wait reported as a build failure. A main transaction that
  failed outright never waits (no updater transaction follows either).
- **A lost rpm lock is repaired, a real build failure is not.** A `*for-<kver>.failed.log` whose
  tail says `Could not install newly built RPMs` next to a built `kmod-nvidia-<kver>-*.rpm` in
  `/var/cache/akmods/nvidia/` makes rivet-apply run `akmods --force --kernels <kver>` ONCE as root
  (`--force` because akmods skips a kernel that has a `.failed.log`; 15 min cap, stdin /dev/null,
  its output streamed as `akmod progress` lines, an `akmod progress` line first says why). Root =
  `common.root_prefix()`: `sudo -n` when that is passwordless, else `pkexec`, i.e. the desktop's
  polkit dialog (without a NOPASSWD sudo rule this is the path that
  runs, and an `akmod progress` "Asking for permission…" line precedes it). The rpmdb
  then decides: kmod installed -> `akmod ok` with `retried: true` and "needed a retry" in the
  message; akmods failing, the prompt dismissed (pkexec 126) or refused (127), or the kmod still
  missing -> `akmod fail` with
  `retried: true`, the usual "akmods reported a build failure for <kver>" plus what the retry said.
  Any other `.failed.log` (no built RPM, a different error) fails immediately with no retry, as
  before. The 10-minute poll and `verify` are unchanged. `RIVET_AKMOD_CACHE` overrides the cache
  dir (tests).

Amended 2026-10-12 (verify raced depmod):
- **`akmod ok` waits for depmod.** rpm lists `kmod-nvidia-<kver>` as soon as its header is in
  the rpmdb, a few seconds before the package's `%post` runs depmod, and `modinfo -k <kver>
  nvidia` resolves the NAME through modules.dep, so in that gap it fails although the module is
  on disk. On 2026-10-12 verify ran in the gap (20:03:37; akmods "Successful" at 20:03:39) and the
  run ended `fail` "NVIDIA module NOT verified" with nothing wrong. Now, once the rpmdb shows the
  kmod, the akmod wait polls modinfo every 2 s for up to 30 s (`akmod progress 97` "… waiting
  for depmod to index it…") before `akmod ok`. The 600 s build deadline is unchanged.
- **`verify fail` says which gap.** The `verify progress 90` event's `log` and the `verify fail`
  event's `cause` (also appended to `done.cause`) name one of: not in the rpmdb; in the rpmdb
  but nvidia.ko not on disk; on disk and readable by path but absent from modules.dep ("depmod
  has not run … `sudo depmod -a <kver>` fixes it"); on disk but unreadable.
- **`rivet-apply --verify-kmod [KVER]`** re-runs only the module check (read-only) for KVER or
  the newest installed kernel: the usual `verify` events, then `done` with `kmod_verified`
  true/false, `reboot_required` = KVER is not the running kernel, and `kver`. With no
  akmod-nvidia installed: `done ok`, `kmod_verified: null`. Exit 0 / 1.
- **App: re-check after a "nothing to run" diagnosis.** When a `diagnose` done arrives with
  `fix.available: false` and `reboot.safe: "yes"` while the last run's `kmod_verified` is false,
  the app runs `--verify-kmod` and pushes its events as `{"type":"doctor","mode":"recheck",
  "event":…}` after the diagnosis done. A recheck `done` with `kmod_verified: true` lifts the
  restart block (`restart` is accepted again); the doctor view shows "Checked again just now …"
  and a **Restart now** button. Any other outcome leaves the block in place and says so.
  Demo mode (no rivet-apply) synthesizes a passing recheck. QA: `qa-e2e.sh recheck`
  (fake-apply scenario `kmodfail`, fake-doctor scenario `nofix`), selftest "recheck" section.

Privilege: use dnf5daemon over the system D-Bus (org.rpm.dnf.v0, polkit prompt appears once) for
RPMs; `flatpak update -y --noninteractive <ref>` as the user (and `--system` where the ref is a
system install); `fwupdmgr update -y` (fwupd D-Bus; firmware that needs a reboot just stages).
After an RPM transaction that included kernel* or akmod-nvidia: wait for akmods (poll
`/var/cache/akmods/nvidia/` and `rpm -q kmod-nvidia-<target-kernel>`, up to 10 min), then
`verify` = the kmod package for the target kernel exists AND `modinfo -k <target-kernel> nvidia`
succeeds. Never call reboot itself.

Triage model: `backend/triage.py` sends batched item facts + notes excerpts to a model and gets
back `{id: {verdict, reasoning, risk_raise: null|"yellow"|"red"}}`.

Provider (amended 2026-09-15; opt-in since 2026-11-01, settings `ai_triage`): **`claude -p --model sonnet --output-format json`
only** -- the Claude Code CLI on this box, which draws on the user's Claude subscription. There is
deliberately NO paid-API path: no OpenRouter, no Anthropic/OpenAI key, so triage reads no credential and costs nothing per run. (The one credential an
Rivet run reads is Elsewhere's Home Assistant token, 2026-10-31; see "Elsewhere".) Because a
`systemd --user` service does not inherit the login PATH, triage.py resolves the CLI explicitly
(`$RIVET_CLAUDE_BIN`, then `~/.local/bin/claude`, then `/usr/local/bin`, `/usr/bin`) and the
service unit sets `Environment=PATH=%h/.local/bin:...`.

The CLI exits 0 even for API failures, reporting them inside the JSON envelope
(`is_error`, `api_error_status`), so both are checked. When the model is unreachable -- rate
limit, quota, offline -- triage degrades to deterministic rule-based verdicts and `status`
stays `ok`; only real model answers are cached.

All items go out in ONE call. Items sharing an advisory (or a package family at one target
version) are collapsed into clusters and the verdict is fanned back out to each member id, so
135 pending packages become 38 clusters. Verdicts are cached by (id, from, to) in
`~/.local/state/rivet/triage-cache.json` so re-runs are free.

`rivet-doctor diagnose [--log PATH] [--model opus]` / `rivet-doctor run --fix PATH` (added 2026-09-22)
: The **Fix it** button. `diagnose` gathers read-only evidence (the last apply.log run with
its progress lines trimmed, state.json, `uname`, kernels in rpm and /lib/modules with their
NVIDIA modules, the loaded driver, /var/cache/akmods/nvidia, the akmods/dnf5daemon journal,
`dnf5 check-upgrade`, the daemon vs user metadata cache revisions, disk, Flatpak GL runtimes)
and hands it to `claude -p --model opus --output-format stream-json --json-schema …` with
`--tools Bash,Read,Grep,Glob`, an allowlist of read-only commands and `--permission-prompts
none --setting-sources ""`, so anything outside the allowlist is denied by the CLI itself (a
`sudo`/`touch` probe was denied, verified 2026-09-22). Claude's tool calls are narrated as
`doctor progress` events (`msg` "Checking: …", `log` "$ cmd"; denied calls as "(not allowed,
read-only mode: …)"). The answer is a fixed JSON shape: `headline`, `what_happened`,
`severity fine|attention|broken`, `reboot {safe yes|no|unknown, why}`, `fix {available, title,
explanation, commands[], needs_root, risk low|medium|high, reversible}`, `after_fix
{verify_commands[], expect}`, `confidence`, `evidence[]`, plus `_meta` (model, turns, checks,
denied, duration_ms). A fix whose command matches the denylist (reboot/shutdown/poweroff as a
command, mkfs, dd if=, rm -rf of a top-level dir, …) is disarmed (`available: false`,
`blocked`). Saved to `~/.local/state/rivet/doctor/<ts>.json` (+ `last.json`); the done event:
`{"step":"done","status":"ok","diagnosis":{…},"fix_path":"…"}`; on failure `status: "fail"`
with `cause`. `run --fix PATH` strips `sudo` from each command (a root tool named bare counts as
root when `needs_root`), groups CONSECUTIVE steps of the same privilege into one `bash` script
(`set -o pipefail`, a `__RIVET_STEP__ i` marker before each step, stop at the first failure),
and runs a root group through `common.root_prefix()` = `pkexec` on this box: ONE polkit prompt
per fix, not one per step, with an "Asking for permission…"
`fix progress` first. Markers become the "Step N of M" progress events (`log` = the command as
it runs, without sudo); output streams as `fix progress` `log` lines; the first non-zero exit
ends it ("Step N failed (exit rc)"); a dismissed (126) or refused (127) prompt ends it with
"Step N did not run: …" and `results.refused`. Then it runs the verify commands
(`verify` step, each must exit 0) and ends `done ok|fail` with `results {ran, planned, …}`.
Never reboots. Events mirror to `~/.local/state/rivet/logs/doctor.log`. `--fake ok|nofix|nodiag`
(diagnose) and `--fake ok|runfail` (run) replay the 2026-09-22 story without Claude or any
command, for `--demo`, app/qa and app/selftest. `rivet-doctor evidence` prints the bundle.
The model (2026-09-22; opus since 2026-11-01): opus by default (`--model`, `RIVET_DOCTOR_MODEL`), via the Claude Code CLI on the subscription,
no API key and no paid API path (same rule as triage). `total_cost_usd` in the CLI envelope is a
notional API-rate figure, not a charge; what a run really spends is subscription allowance, so the
prompt holds Claude to a budget (~8 calls) and two fallbacks exist: out of time or turns -> the same
model answers from the evidence with no tools; out of usage for that model -> the next model down
`RIVET_DOCTOR_FALLBACK_MODELS` (default sonnet) starts over. An exhausted per-model allowance
comes back as a hard rejection, which `--fallback-model` does NOT cover, hence the ladder in
`ask()`. All models exhausted -> one plain sentence saying the limit resets by itself and that
nothing was changed.

## App ↔ UI bridge (app/rivet_app.py ↔ app/ui/app.js)

The UI is proto/a adapted. JS calls Python with
`window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({cmd, ...}))`; commands:
`ready`, `refresh`, `apply {items:[ids]}`, `quitSteam`, `restart`, `openUrl {url}`, `setSetting {key,value}`,
`copyText {text}` (2026-10-31: puts one line, <= 500 chars, on the GTK clipboard; Other software's Copy),
`otherUpdate {id}` (2026-10-31 b: one `rivet-apply --other-update ID`; refused with a synthetic
`done` while anything else runs; sets `applying` while it runs; events reach the UI as
`{"type":"other","event":<event>}`, a process that ends without `done` gets a synthetic fail,
`--demo` without RIVET_APPLY_CMD plays a canned run and changes nothing),
`otherUpdateMany {ids:[...]}` (2026-10-31 c: "Update the safe ones". Runs `otherUpdate`'s exact
path for each id in turn, the next one only after the previous process has exited; each row
streams its own `other` events as usual. Every id is checked against the row in state.json
first and must be in the "Safe set" (see Other software, "Front page"); one that is not, or
is missing, gets a synthetic `done` with `result: "refused"` and is skipped. Refused as a
whole (a `refused` done per id) while anything else runs. A failed row does not stop the
rest. When the queue is empty: `{"type":"other","event":{"step":"batch","status":"done",
"batch":{"ids":[...],"ran":[...],"refused":[...],"stopped":bool}}}`), `otherStop {}` (drops the
rest of the queue; the row that is running finishes), `otherRemove {id}` (2026-11-01: one
`rivet-apply --other-remove ID` through `otherUpdate`'s exact plumbing and events; also refused
during a bulk run; `--demo` plays a canned removal; see Other software, "Remove an unused add-on"),
`otherFix {id}` / `haRestart {}` (2026-11-01 c: see "Fix it on a failed row" and "HA core
restart"; both through `otherUpdate`'s plumbing, refused during a bulk run),
the four held-back commands above, and `fixIt {log?}` / `runFix {fix: path}` (2026-09-22: one
rivet-doctor process at a time, a second click is ignored; `runFix` accepts only a file inside
`~/.local/state/rivet/doctor/`). Doctor output is pushed as
`{"type":"doctor","mode":"diagnose"|"run","event":<doctor event>}`; a process that ends without
`done` gets a synthetic fail naming the exit code, like apply. UI: the failed / do-not-reboot /
warning result screens gain **Fix it** (Check again only re-runs the collector); the `doctor`
view shows the investigation live (steps card + log drawer), then the diagnosis (headline,
what happened, reboot verdict, actions, fix card with the exact commands, risk and root chips,
evidence under Show details), then **Run the fix** → streamed → Fixed / The fix did not work.
Python pushes to JS by evaluating `window.rivet.receive(<json>)` with messages:
`{"type":"state", "state": <state.json>}`, `{"type":"progress", "event": <apply event>}`,
`{"type":"settings", "settings": {...}}`, `{"type":"error", "msg"}`.
The app never replaces a `done` event from rivet-apply. Only when the process ends WITHOUT one
does it synthesize `{"step":"done","status":"fail","synthetic":true, cause, summary, log_path}`
naming the exit code, the last step reached and its last message (amended 2026-09-21).
Settings file: `~/.config/rivet/settings.json` `{check_interval_min: 60, notify: true, red_includes_reboot: false, theme: "system|dark|light", look: "periwinkle", type_size: "compact|comfortable", show_containers: false, ai_triage: false}` (`show_containers` 2026-11-01 c, `ai_triage` 2026-11-01 f: bools only; true starts a check). `look` is an id from `app/ui/looks/` (the page owns the list; `[a-z0-9-]{1,32}` accepted, unknown ids render as Periwinkle); `type_size` is applied by the host as WebKit page zoom (1.0 / 1.12).
`restart` = logind `org.freedesktop.login1.Manager.Reboot(true)` over the system bus, only after
the UI confirmed and only when the last apply reported `kmod_verified != false`.
UI grouping (added 2026-10-04, UI only, no bridge change): the System (dnf) section shows ONE
row per cluster, keyed exactly like `cluster_key()` in backend/triage.py (`adv:<advisory id>`,
else `<source>:<first name token>:<to>`), so the row's verdict/reasoning really is the one the
triage gave every member. A multi-package cluster row carries the worst risk/badge of its
members, the summed size, the shared version transition, a tri-state checkbox and a chevron
that opens the member list (each member has its own checkbox and details). Clusters that are
routine (every member green, no badge, no `requires_reboot`, not blocked, no security
advisory) are folded into one collapsed "Routine updates" row at the end of the section
(open/closed remembered in the page's localStorage). **Selection semantics are unchanged**:
`apply {items}` still carries item ids only; a cluster/routine checkbox merely adds or removes
its members (mixed -> all on -> all off). The header count stays the package count; the section
title says "125 · in 17 rows". DOM contract for QA drivers: top-level rows are `.row`
(cluster rows add `.cluster`, members are `.member`, the accordion row is `.row.routine` with
its body in `.routine-body`), every checkbox is `.cb[data-action="toggle-row"][data-id]` where
`data-id` is an item id, `cluster:<key>` or `routine:<group id>`; members of a collapsed cluster
have no DOM at all, so a driver reads the selection from `window.rivet.state.selected`.
Window: 1280x860 default, remembers size; GTK4 + WebKitGTK 6.0 via gi (`gi.require_version('WebKit','6.0')`).
Set `GSK_RENDERER=cairo` and `WEBKIT_DISABLE_DMABUF_RENDERER=1` in the launcher (WebKitGTK
needs both on some NVIDIA setups).

## Shell extension (shell-ext/rivet@scottnelson.xyz)

Reads state.json with a Gio.FileMonitor; panel indicator shows an icon + count; red dot when any
item is red; hidden (or dim) when zero pending. Click → `Gio.AppInfo` launch of xyz.scottnelson.Rivet.desktop.
Metadata shell-version ["50"]. QA: headless nested shell + fixture (`shell-ext/qa-headless.sh`).

## systemd (systemd/)

`rivet-collect.timer`: OnBootSec=3min, OnUnitActiveSec=1h (interval from settings is applied
by the app rewriting a drop-in). `rivet-collect.service`: Type=oneshot, ExecStart=%h/.local/bin/rivet-collect --quiet --notify,
TimeoutStartSec=1015 (2026-10-04, derived in the unit file; was 300, which was shorter than the
collector's own worst case).

## History

- 2026-11-01 (e): the app took its current name (names only, no behaviour change). App id
  `xyz.scottnelson.Rivet`, launcher `rivet-updater` (plain `rivet` is taken by other tools), CLIs
  `rivet-collect`, `rivet-apply`, `rivet-doctor`, units `rivet-collect.service` /
  `rivet-collect.timer`, env vars `RIVET_*`, shell extension `rivet@scottnelson.xyz` with D-Bus
  name/path `xyz.scottnelson.Rivet` / `/xyz/scottnelson/Rivet`, page bridge `window.rivet` /
  `messageHandlers.rivet`, dirs `~/.config/rivet` and `~/.local/state/rivet`. A one-time copy of the
  old dirs and symlinks from the old command names covered the hand-over; both were removed in (f).
  `know.py` ships only generic entries; what one box runs (its tools, container apps, runbook
  links) goes in the git-ignored `backend/know.local.json` (or `$RIVET_KNOW_LOCAL`), see
  `know.load_local`.
