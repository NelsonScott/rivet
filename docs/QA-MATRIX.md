# QA matrix: the abuse pass (2026-11-01)

Before the public release, every way a stranger's machine (or a bad day on this one) can hurt
Rivet, simulated through the fake layers, never against the real system. One line per scenario:
where the test lives and what it showed. "Fixed" = the test failed first and the code changed.

Suites: `python3 -m unittest discover -s backend/tests` (backend), `python3 app/selftest.py`
(bridge), `dbus-run-session -- bash app/qa-e2e.sh OUT` (real GTK/WebKit app in a nested headless
shell), `python3 app/qa/looks-contrast.py` (look tokens).

| # | Scenario | Test | Result |
|---|----------|------|--------|
| 1 | No Home Assistant at all (no token, no URL) | `test_abuse.NoHomeAssistant`; e2e `abuse-noclaude` (no HA group, page never says Home Assistant, strip counts right) | Fixed: was a CAN'T CHECK row about a private secrets template; now `absent`, nothing shown |
| 1 | HA configured, connection refused | `test_abuse.HomeAssistantUnreachable.test_connection_refused_is_one_failed_row_fast` (real socket) | Pass: one CHECK FAILED row "(connection refused)" in < 3 s |
| 1 | HA name does not resolve / hangs (timeout) | `…test_a_name_that_does_not_resolve_says_so`, `…test_timeout_is_a_failed_row_at_the_deadline` (silent server) | Pass: failed row at the deadline, plain reason |
| 1 | HA gone before an Install in HA | `test_abuse.HomeAssistantGoesAway.test_gone_before_the_install_fails_at_once_and_offers_fix_it` | Fixed: polled 20 min ("still working"); now fails at once, row `failed` (Fix it), `unreachable: true` |
| 1 | HA gone in the middle of an install | `…test_gone_in_the_middle_of_an_install_fails_after_a_grace_period` | Fixed: same 20 min wait; now fails after `RIVET_HA_GONE_S` (90 s) |
| 1 | HA gone in the middle of a bulk run | selftest "abuse: Home Assistant goes away…"; e2e `abuse` | Fixed + decided (CONTRACT 2026-11-01 d): row 1 ok, row 2 failed with Fix it, later HA rows skipped (no Fix it), This PC rows still run, no HA restart |
| 2 | `claude` not installed | `test_abuse.ClaudeBroken.test_not_installed`; selftest "no Claude Code CLI"; e2e `abuse-noclaude` | Fixed: Fix it was offered, then said "Claude could not finish (the Claude Code CLI … was not found)"; now `caps.claude=false`, "Install Claude Code to enable Fix it" note, commands refused, nothing spawned |
| 2 | `claude` not logged in | `…test_not_logged_in` (fake-claude `logged-out`) | Fixed: "Not fixed: Claude Code is not logged in (run `claude` once…)" |
| 2 | `claude -p` garbage / timeout / out of turns / out of usage | `…test_garbage`, `…test_timeout`, `…test_budget_exhausted`, `…test_out_of_usage` | Fixed: one plain line each in the row; the whole run (garbage included) in apply.log |
| 3 | dnf transaction dies mid-way | `test_abuse.Dnf.test_a_transaction_that_dies_midway_names_what_is_missing` | Pass: fail, "M of N", missing names, updater phase not started |
| 3 | Reboot required | `…test_a_kernel_says_reboot_required` (+ existing e2e `recheck`) | Pass |
| 3 | Package held back by a conflict | `…test_a_conflict_holds_everything_back_and_says_which` | Fixed: cause was "dnf could not run the transaction"; now names the package and says to untick it |
| 3 | 0-update day | `test_abuse.UpdateDays.test_a_zero_update_day`; e2e `abuse` (trimmed hero) | Pass |
| 3 | 300-update day | `…test_a_300_update_day`; e2e `abuse` times the render (FAIL over 1 s) | Pass: render 11 ms, routine accordion 25 ms, a click 25 ms |
| 4 | Flatpak runtime/app in use | `test_abuse.Flatpak.test_a_runtime_in_use_fails_with_what_to_close` | Fixed: "failed (rc=1)."; now "in use by a running app; close … and update again" |
| 4 | Flatpak broken remote | `…test_one_broken_remote_no_longer_hides_the_others`; e2e `abuse` (strip) | Fixed: one broken remote hid EVERY update of that installation (read as up to date); now per-remote, `flags.flatpak_remote_errors`, strip says it |
| 4 | Stale xdg-document-portal | `test_abuse.HealthChecks` (5 tests); selftest "health checks"; e2e `abuse` (banner, one click, gone) | New: Doctor/health check + one-click `systemctl --user restart xdg-document-portal.service` |
| 5 | No npm / pipx / uv / cargo / GNOME extensions / podman / AppImages | `test_abuse.NoToolsAnywhere` | Fixed: npm bound `HOME` at import (a patched home still scanned the real nvm); now every source `absent`, zero rows |
| 5 | Personal residue in UI copy and sample data | `test_abuse.PersonalResidue` | Fixed: sample paths/IP/Notion links, desktop `Exec`, hints about a private secrets template, a game-name fallback |
| 6 | Privilege: what runs as root | `test_abuse.Privilege` (call-site audit), `Dnf.test_cancelled_password_prompt_installs_nothing_and_says_so` | Pass: root only via dnf5daemon polkit, akmods retry, a Fix it root step, held rollback |
| 6 | Cancelled password prompt | `Dnf.test_cancelled_password_prompt_installs_nothing_and_says_so`; e2e `abuse-noclaude` (fake-apply `cancel` = real run_real) | Fixed: after "nothing was installed" the run went on to Flatpak + fwupd (a second prompt, "firmware staged") and said "restart to take effect"; now it stops there, no reboot line, list and selection unchanged |
| 7 | Every look: failed / busy / Fix it / health banner | `app/qa/looks-contrast.py` (15 new pairs) | Pass: Nord, Poster, Terminal, Workshop >= 4.5:1 light and dark; Periwinkle info-only as before |
| 9 | A newer Fedora release is out (2026-11-03) | `test_release` (release list parsing, VERSION_ID, repo probe ok/missing/unknown against a local HTTP server, the row's colour/badge/unticked default, collector fixture, apply: runs last with `--disable-repo`, skipped after a failed rpm transaction, dismissed prompt, `--allowerasing`, timeout, dry run; `--release-reboot` refuses without a download); selftest "release upgrade"; e2e `release` (row, selection rules, banner, progress step, "Ready for Fedora 45", Restart now never clicked) | New: one yellow REBOOT row "Fedora 45 · 44 → 45", never swept in by Select all / Safe only / Security only; the restart goes through `dnf5 system-upgrade reboot` with its own confirmation. Not simulated: the real download and the offline install (needs a real newer release; verified by reading dnf5's documented flags only) |
| 10 | Other-software rows ticked for Update now (2026-11-10) | selftest "otherUpdateMany ticked:true" (majors / minor / warned / HA major run, firmware / core / pulled / missing refused, waits for the system run's exit, `refresh` deferred until the batch ends, still busy after the wait = refused, `restart` refused meanwhile); e2e `other-tick` (one system item + ruff + Samba share: shortcuts never tick an Other row, count 3, bridge log order apply → ruff → Samba, rows stream on the result screen with Done disabled, outcome kept after the re-check), `other-tick-fail` (system run fails: nothing else runs, one line on the result screen, still ticked on the list), `other-tick-only` (no `apply`, verdaccio + pnpm major stream in place, no confirmation) | New |
| 11 | Home Assistant core: release notes investigated, installed with care (2026-11-10) | `test_ha_core` (38: patch vs monthly incl. a year rollover, the rating matrix, "model may only raise" (none_touch vs notes naming MQTT = red, a touch on a domain not in use dropped = unsure = yellow, none_touch naming one = touches), bad JSON / no claude / AI off = "Could not check…" yellow, unreadable notes never safe, release-post + GitHub-body parsing on canned HTML, a post or change list Rivet cannot parse = could not read, a skipped month read too, an unpublished release = could not read, a long skip partial = yellow, the real CLI call through qa/fake-claude.py (touches / none / garbage / missing), cache once per release (no refetch, no second ask, new target = new question), failure reused for 1 h then retried, AI off then on, collect_ha marks core installable + confirm; install against `fixtures/fake_ha.py` that really closes its port: back on the new version, rolled back (failed, Fix it offered, Fix it budget state/logs only), never back (fails at the cap, unreachable), check_config invalid (no install request), no BACKUP feature (said in the stream, no `backup` sent), nothing happens (idle cap), refused without `--confirmed`, the core lock refuses other HA rows / the HA restart / a second core run, a dead pid does not block; OS/supervisor/firmware stay Open); `test_abuse.TriageLockdown.test_ha_core_check_has_the_same_lockdown_and_no_token` (no tools/settings/MCP, @ defused, no HA token or API key in the CLI's env, not in the prompt); selftest "HA core is tickable…" (tickable, never safe, OS/supervisor/firmware neither, a ticked batch runs core LAST after the one HA restart with `--confirmed`, a safe-ones run never includes it, the row confirmation is the only other `--confirmed`); e2e `other-hacore` (red MONTHLY "Touches MQTT and Automation…" AI line from the real backend, details, tick asks first, confirm light + dark, "restarting, this is expected", back on 2026.11.1 → UPDATED; bridge ran `--confirmed` once; fake HA saw check_config then install) and `other-hacore-none` (amber MONTHLY "4 breaking changes, none touch…", rolled back → red with Fix it) | New |
| 8 | Second instance while one is open | selftest "single instance"; e2e `second-instance` | Pass: GApplication hands over; second exits 0, first raises its window, one Bridge |
| 10 | Steam quit or started since the last check; Quit Steam (2026-11-10) | `test_steam_check` (both directions equal a full `rivet-collect --fixture` with the other Steam value, round trip, no change = no write, collector stub / failed probe / a write in between left alone, model raises kept, AI cache verdicts, held rows untouched, wait-gone cap, the CLI with a fake `flatpak`); selftest "Steam-only re-check"; e2e `steam` (qa/fake-flatpak.sh: open, focus, Quit Steam, a Steam that will not quit, light + dark) | Fixed: the banner and BLOCKED rows stayed up to an hour after Steam quit, and Quit Steam re-ran the whole collector (21 s). Now open/focus re-checks in ~40 ms, Quit Steam unblocks in ~0.5 s with no collector and no loading screen, a stubborn Steam is said after 5 s. Not simulated: the GTK focus signal itself (a headless window is never active; `qaFocus` runs the same handler) and the real Steam's exit time |

## Not simulated (and why)

- **The real polkit dialog text and a real cancel**: needs an interactive session; the text comes
  from `/usr/share/polkit-1/actions/org.rpm.dnf.v0.policy` ("Authentication is required to execute
  an rpm transaction."), read, not shown. The Fix it root step asks pkexec to run `/usr/bin/bash -c
  <script>`, so a stranger's dialog shows a bash command line (left as a known rough edge).
- **A real HACS / Supervisor going down mid-install**: the HA side is a fake ctx / closed socket.
- **A real Home Assistant core update (2026-11-10)**: the Supervisor's actual sequence (when
  `in_progress` turns on, whether `update_percentage` moves, how long the backup takes, whether the
  `update.install` call returns before HA stops or drops the connection, what `/api/config` answers
  while it starts, how a real rollback looks) is modelled by `fixtures/fake_ha.py`, not observed. The
  release-post markup and GitHub release bodies are hand-written in the shape of the real ones
  (checked against the 2025.10 post and release on 2026-11-10); a future layout change would show as
  "Could not check the release notes", never as "safe". GitHub's unauthenticated rate limit (60/h) is
  not exercised. The real model's answers are not judged (qa/fake-claude.py answers).
- **A real `flatpak update` refusing a running app**: Flatpak's exact wording varies by version;
  the fake prints the 1.16 shape and the matcher is deliberately broad.

## Not covered

- Painted-contrast scan (driver-looks.js) of the new failed/busy/health states: not covered (token pairs only).
- Final full e2e run: 12 existing scenarios PASS; `looks`, `abuse`, `abuse-noclaude`, `second-instance` passed in the earlier run but were not re-run after the last UI tweak (neutral skipped row, no reboot line): not covered.
