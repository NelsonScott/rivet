/* Driver for qa-e2e.sh, the Other software scenarios. Runs inside the real WebKit page; every
 * button reaches qa/fake-apply.py (nothing real runs). SCENARIO picks the story:
 *   other       (alias elsewhere) the section on the updates screen: groups This PC / Home
 *               Assistant / Containers in that order, severity sort, one-line rows with badges
 *               only (no dot), one hint at most, Remove on the unused add-on; then the buttons:
 *               Update (ruff), a major with the in-row confirm (pnpm), Install in HA (Samba share),
 *               a failing pull (nginx), Copy from the details; then the "nothing pending" screen
 *               (status strip, one Check now) and the truly empty one (trimmed hero). 2026-10-31 c.
 *   other-row   a row collapsed and expanded (details behind the chevron), light + dark.
 *   other-bulk  "Update the safe ones (N)": the confirmation lists exactly the safe rows (never a
 *               major, a warning, HA OS or firmware), Cancel runs nothing, a run streams row by
 *               row with the rest queued, Stop after this one drops the rest. Light + dark.
 *   other-remove Remove on the unused add-on (Matter Server, 2026-11-01): the confirmation (name,
 *               "Nothing in Home Assistant uses it", what happens, Cancel / Remove), Cancel runs
 *               nothing, the first Remove fails (fake-apply's live re-check says no) with the reason
 *               and an Open in Home Assistant fallback, the second removes it and the row goes,
 *               also after the state.json push. qa-e2e.sh checks the bridge ran --other-remove.
 *   other-real  look only: THIS box's real state.json, never presses a button.
 *   other-fix   (2026-11-01) containers OFF (the default): no Containers section, no podman row,
 *               the Preferences toggle; then Alexa Media Player fails the way it did on 2026-11-01,
 *               the row shows the reason and Fix it, Fix it runs the REAL backend (canned evidence,
 *               qa/fake-claude.py) and its "Fixed: …" line lands in the row. qa-e2e.sh checks the
 *               claude command line and apply.log.
 * other / other-row / other-bulk run with show_containers on (qa-e2e.sh writes settings.json);
 * other-bulk's Home Assistant bulk (2026-11-01) leaves one row out with its checkbox and ends
 * with ONE Home Assistant restart for the integration.
 * Prints [QA] lines; "DRIVER DONE" when every check passed, "DRIVER FAILED" otherwise. */
(function () {
  const SC = typeof SCENARIO !== 'undefined' ? SCENARIO : 'other';
  const log = (m) => console.log('[QA] ' + SC + ': ' + m);
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const click = (s) => { const e = q(s); if (e) e.click(); return !!e; };
  const txt = (s, root) => (((root || document).querySelector(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const theme = () => document.documentElement.getAttribute('data-theme');
  const st = () => window.rivet.state;
  const row = (id) => q(`.ew-row[data-id="${id}"]`);
  const runOf = (id) => q(`.ew-run[data-id="${id}"]`);
  const lister = () => q('#list') || q('.empty-held');
  const scrollTo = (el, off) => { const l = lister(); if (el && l) l.scrollTop += el.getBoundingClientRect().top - l.getBoundingClientRect().top - (off == null ? 60 : off); };
  const btnText = (id) => txt('.ew-btn .btn', row(id));
  const SEV = { major: 0, minor: 1, patch: 2, unknown: 3 };
  let wait = 0, bad = 0, sel0 = -1, count0 = ''; const t0 = Date.now();
  const LIMIT_MS = SC === 'other' || SC === 'elsewhere' || SC === 'other-bulk' || SC === 'other-remove' || SC === 'other-fix' ? 110000 : 55000;
  const check = (cond, what) => { if (!cond) { bad++; log('CHECK FAILED: ' + what); } else log('ok: ' + what); };
  const steps = [];
  const then = (fn, w) => steps.push([fn, w || 3]);
  const until = (pred, what, fn) => steps.push([() => { if (!pred()) return 'again'; if (fn) fn(); }, 1, what]);
  const push = (mut) => { const d = JSON.parse(JSON.stringify(st().doc)); mut(d); window.rivet.receive({ type: 'state', state: d }); };
  const nothingPending = (d) => { d.groups = d.groups.filter(g => g.id === 'held'); d.summary = { total: 0, total_bytes: 0 }; };
  const toggleTheme = () => then(() => q('#theme-btn').click(), 4);
  const dumpRows = () => qa('.ew-row').forEach(r => log('  row: [' + (r.closest('.ew-group') || {}).dataset?.group + '] ' + (txt('.src', r) ? '[' + txt('.src', r) + '] ' : '')
    + txt('.name', r) + ' ' + txt('.ver', r) + ' {' + txt('.badge', r) + '} btn=' + (txt('.ew-btn .btn', r) || '-') + (txt('.ew-remove', r) ? ' +' + txt('.ew-remove', r) : '')
    + (txt('.ew-hint', r) ? '\n        hint: ' + txt('.ew-hint', r) : '')));

  // the shape every Other-software screen must have, whatever the data
  function shapeChecks(doc) {
    const items = ((doc.elsewhere && doc.elsewhere.items) || []).filter(i => st().settings.show_containers || i.source !== 'podman');
    const groups = qa('.ew-group').map(g => g.dataset.group);
    check(st().settings.show_containers || (!groups.includes('containers') && !q('.ew-row[data-id^="podman:"]')), 'containers off: no Containers section, no podman row');
    check(groups.join(',') === ['pc', 'ha', 'containers'].filter(g => groups.includes(g)).join(','), 'groups in the order This PC, Home Assistant, Containers (' + groups + ')');
    check(qa('.ew-sub-t').every(h => /^(This PC|Home Assistant|Containers)\d+$/.test(txt('.ew-sub-t', h.parentNode))), 'each group has its sub-heading with a count');
    check(qa('.ew-row').length === items.length, 'one row per elsewhere item (' + qa('.ew-row').length + '/' + items.length + ')');
    check(qa('.ew-row .risk').length === 0, 'no severity dot on any Other-software row');
    check(qa('.ew-row').every(r => r.querySelectorAll('.badge').length === (r.dataset.status === 'rebuilt' ? 0 : 1)), 'every row carries exactly one badge (a rebuilt image none)');
    check(qa('.ew-row .cb').length === 0, 'no checkbox on an Other-software row');
    check(qa('.ew-row').every(r => !r.querySelector('.ew-what, .ew-used')), 'what it is / used by are not in the collapsed rows');
    check(qa('.ew-row').every(r => r.querySelectorAll('.ew-hint').length <= 1 && (!r.querySelector('.ew-hint') || r.querySelector('.ew-hint').scrollHeight <= 22)), 'at most one hint per row, one line high');
    for (const g of qa('.ew-group')) {
      const ranks = [...g.querySelectorAll('.ew-row')].filter(r => r.dataset.status === 'ok').map(r => SEV[r.dataset.severity]);
      check(ranks.every((x, i) => !i || ranks[i - 1] <= x), g.dataset.group + ': stale rows sorted major, minor, patch, unknown (' + ranks + ')');
    }
    // containers (2026-11-01): versions, never digests; rebuilt = no badge, no button; data = no Update
    const pods = qa('.ew-group[data-group="containers"] .ew-row[data-status="ok"]');
    check(pods.every(r => /^(MAJOR|MINOR|PATCH|NEW VERSION)$/.test(txt('.badge', r))), 'podman rows use the same badge family');
    check(qa('.ew-row[data-id^="podman:"]').every(r => !/sha256|\b[0-9a-f]{12}\b/.test(txt('.ver', r))), 'no container row shows a digest');
    check(qa('.ew-row[data-id^="podman:"][data-status="rebuilt"]').every(r => !r.querySelector('.badge') && !r.querySelector('.ew-btn .btn') && !r.querySelector('.ew-hint')), 'a rebuilt image: no badge, no button, no hint');
    const items0 = (doc.elsewhere && doc.elsewhere.items) || [];
    check(items0.filter(i => i.source === 'podman' && i.stateful).every(i => !row(i.id) || !row(i.id).querySelector('[data-action="ew-update"]')), 'a container that holds data never has Update');
    check(!/elsewhere/i.test(document.body.innerText), 'the word "elsewhere" is not on the page');
  }

  if (SC === 'other' || SC === 'elsewhere') {
    then(() => {
      sel0 = st().selected.size; count0 = txt('[data-action="update-now"] .count');
      log('section="' + txt('.ew-h .group-title') + '" note="' + txt('.ew-note') + '" pointer="' + txt('.ew-link') + '"');
      dumpRows();
      check(/^OTHER SOFTWARE\d+$/.test(txt('.ew-h .group-title')), 'the section is called OTHER SOFTWARE');
      check(/^Outside dnf and Flatpak/.test(txt('.ew-note')), 'subtitle "Outside dnf and Flatpak"');
      check(/more in Other software/.test(txt('.ew-link')), 'the updates screen keeps the header pointer');
      const heldH = q('.held-h'), ewH = q('.ew-h');
      check(heldH && ewH && (ewH.compareDocumentPosition(heldH) & Node.DOCUMENT_POSITION_FOLLOWING), 'Held back comes last, after Other software');
      shapeChecks(st().doc);
      const inGroup = (g) => qa(`.ew-group[data-group="${g}"] .ew-row`).map(r => r.dataset.id);
      check(inGroup('pc').includes('npm:pnpm') && inGroup('pc').includes('cargo:_source') && inGroup('pc').includes('gnome-ext:tilingshell@ferrarodomenico.com'), 'npm, cargo and GNOME extensions are under This PC');
      check(inGroup('ha').length === 6 && inGroup('ha').every(id => /^ha:/.test(id)), 'every HA row is under Home Assistant');
      check(inGroup('containers').length === 6 && inGroup('containers').every(id => /^podman:/.test(id)), 'podman rows are under Containers');
      const P = (n) => 'podman:docker.io/' + n;
      check(btnText(P('library/couchdb:3')) === 'Open runbook ↗' && /runbooks\/obsidian-sync$/.test(row(P('library/couchdb:3')).querySelector('.ew-runbook').dataset.url), 'CouchDB (holds data, runbook known): Open runbook');
      check(txt('.ver', row(P('library/couchdb:3'))) === '3.5.2.1 → 3.5.3' && txt('.badge', row(P('library/couchdb:3'))) === 'PATCH', 'CouchDB shows versions: 3.5.2.1 → 3.5.3, PATCH');
      check(txt('.ew-manual', row(P('library/ghost:5-alpine'))) === 'Manual: holds data, upgrade by hand' && !row(P('library/ghost:5-alpine')).querySelector('.ew-btn .btn'), 'Ghost (holds data, no runbook): the manual note as plain text');
      check(txt('.ver', row(P('library/redis:7.2'))) === '7.2.14' && !row(P('library/redis:7.2')).querySelector('.badge'), 'redis (rebuilt upstream): its version only, no badge');
      check(btnText(P('library/nginx:1.27')) === 'Update' && btnText(P('library/caddy:2')) === 'Update', 'stateless containers with a newer tag: Update');
      check(btnText(P('traefik/whoami:v1.10.1')) === 'Open ↗', 'a stateless container pinned to a tag: Open (the tags page)');
      check(/Major jump 9 → 12/.test(txt('.ew-hint', row('npm:pnpm'))) && row('npm:pnpm').querySelector('.ew-hint .ic'), 'pnpm shows its major-jump warning as a one-line hint');
      const matter = row('ha:update.matter_server_update');
      check(txt('.ew-hint', matter) === 'Nothing uses it; consider removing.', 'the unused add-on says "nothing uses it; consider removing"');
      const rm = matter.querySelector('.ew-remove');
      check(rm && txt('.ew-remove', matter) === 'Remove' && rm.dataset.action === 'ew-remove' && rm.classList.contains('ghost'), 'the unused add-on has a secondary Remove button');
      check(qa('.ew-remove').length === 1, 'only the unused add-on offers Remove');
      check(!row('ha:update.samba_share_update').querySelector('.ew-hint') && !row('uv:ruff').querySelector('.ew-hint'), 'rows with nothing to warn about are one line');
      check(btnText('npm:pnpm') === 'Update' && btnText('uv:ruff') === 'Update' && btnText('podman:docker.io/library/nginx:1.27') === 'Update', 'user-level rows get Update');
      check(btnText('ha:update.matter_server_update') === 'Install in HA' && btnText('ha:update.alexa_media_player_update') === 'Install in HA', 'add-ons and HACS integrations get Install in HA');
      check(btnText('ha:update.home_assistant_operating_system_update') === 'Open in HA ↗' && btnText('ha:update.smart_plug_firmware') === 'Open in HA ↗', 'HA OS and firmware stay Open in HA');
      check(btnText('gnome-ext:tilingshell@ferrarodomenico.com') === 'Open ↗', 'GNOME extensions stay Open');
      check(qa('.ew-row').some(r => r.dataset.status === 'failed' && /CHECK FAILED/.test(txt('.badge', r))), 'a failed collector says CHECK FAILED');
      check(qa('.ew-row').some(r => r.dataset.status === 'cant_check' && /CAN.T CHECK/.test(txt('.badge', r))), 'an AppImage says CAN’T CHECK');
      const bulk = (g) => txt(`.ew-group[data-group="${g}"] .ew-bulk`);
      check(bulk('pc') === 'Update the safe ones (2)' && bulk('ha') === 'Update the safe ones (3)' && bulk('containers') === 'Update the safe ones (2)', 'each group offers its safe ones; HA add-ons and integrations at any non-major bump (' + [bulk('pc'), bulk('ha'), bulk('containers')] + ')');
      check(/^Open only: installing it restarts Home Assistant/.test(txt('.ew-hint', row('ha:update.home_assistant_operating_system_update'))), 'HA OS says in the row why it is Open only');
      check(Number(txt('.summary .stat b')) === st().doc.summary.total, 'summary total unchanged by Other software');
      q('.ew-link').click();
    }, 4);
    then(() => { check(q('#list').scrollTop > 0, 'the header pointer scrolls to Other software'); shot('other-' + theme()); }, 8);
    toggleTheme();
    then(() => q('.ew-link').click(), 3);
    then(() => shot('other-' + theme()), 8);
    // --- Update, no confirm (ruff 0.13 -> 0.14)
    then(() => { log('Update on ruff'); row('uv:ruff').querySelector('[data-action="ew-update"]').click(); }, 1);
    until(() => runOf('uv:ruff'), 'the run panel opens', () => {
      check(qa('.ew-row [data-action="ew-update"]:disabled').length > 0, 'other Update buttons are disabled while one runs');
      check(qa('.ew-bulk:disabled').length === qa('.ew-bulk').length, 'the bulk buttons are disabled while one runs');
    });
    until(() => runOf('uv:ruff') && runOf('uv:ruff').classList.contains('ok'), 'ruff finishes', () => {
      log('ruff done: "' + txt('.st', runOf('uv:ruff')) + '" badge=' + txt('.badge', row('uv:ruff')) + ' ver="' + txt('.ver', row('uv:ruff')) + '"');
      check(txt('.badge', row('uv:ruff')) === 'UPDATED' && /now 0\.14\.1/.test(txt('.ver', row('uv:ruff'))), 'the row refreshes: UPDATED, now 0.14.1');
      check(!row('uv:ruff').querySelector('[data-action="ew-update"]'), 'no Update button after it ran');
      check(st().selected.size === sel0 && txt('[data-action="update-now"] .count') === count0, 'the dnf selection is untouched');
    });
    // --- major bump: confirm in the row (pnpm 9 -> 12)
    then(() => { row('npm:pnpm').querySelector('[data-action="ew-update"]').click(); }, 2);
    then(() => {
      const c = q('.ew-confirm[data-id="npm:pnpm"]');
      check(!!c && !runOf('npm:pnpm'), 'a major bump asks first and runs nothing yet');
      check(/i -g --prefix ~\/.npm-global pnpm@latest/.test(txt('.cmd', c)), 'the confirm shows the exact command');
      check(/Website does not pin/.test(txt('.warn', c)) && !row('npm:pnpm').querySelector('.ew-hint'), 'the warning moves into the confirm (shown once)');
      scrollTo(row('npm:pnpm'));
    }, 2);
    then(() => shot('other-confirm-' + theme()), 8);
    then(() => q('.ew-confirm [data-action="ew-go"]').click(), 1);
    until(() => runOf('npm:pnpm') && runOf('npm:pnpm').classList.contains('ok'), 'pnpm finishes', () => check(!q('.ew-confirm'), 'the confirm closes once it runs'));
    // --- Install in HA (Samba share add-on)
    then(() => { row('ha:update.samba_share_update').querySelector('[data-action="ew-update"]').click(); }, 1);
    until(() => runOf('ha:update.samba_share_update') && /[1-9]\d*%/.test(txt('.st', runOf('ha:update.samba_share_update'))), 'HA progress streams', () => {
      scrollTo(row('ha:update.samba_share_update'));
      check(/update\/install/.test(txt('.ew-log', runOf('ha:update.samba_share_update'))), 'the HA call is shown');
      shot('other-running-' + theme());
    });
    until(() => runOf('ha:update.samba_share_update') && runOf('ha:update.samba_share_update').classList.contains('ok'), 'Samba share finishes');
    // --- a pull that fails (nginx)
    then(() => { row('podman:docker.io/library/nginx:1.27').querySelector('[data-action="ew-update"]').click(); }, 1);
    until(() => runOf('podman:docker.io/library/nginx:1.27') && runOf('podman:docker.io/library/nginx:1.27').classList.contains('fail'), 'nginx fails', () => {
      const r = runOf('podman:docker.io/library/nginx:1.27');
      check(/no such host/.test(txt('.st', r)) && !!r.querySelector('.ew-log'), 'a failure is red, says why and keeps the output');
      check(txt('.badge', row('podman:docker.io/library/nginx:1.27')) === 'PATCH', 'a failed row is still stale');
      scrollTo(row('podman:docker.io/library/nginx:1.27'));
    });
    then(() => shot('other-results-' + theme()), 8);
    // --- Copy from the details
    then(() => { row('npm:verdaccio').click(); }, 2);
    then(() => { const a = q('.ew-expand [data-action="ew-copy"]'); a.click(); }, 2);
    then(() => check(/^Copied: .*verdaccio@latest/.test(txt('.toast')), 'Copy the command shows a toast'), 1);
    // --- nothing pending in dnf/Flatpak/fwupd: the status strip, no hero
    then(() => push(nothingPending), 3);
    then(() => {
      const doc = st().doc, ew = doc.elsewhere.items;
      const stale = ew.filter(i => i.status === 'ok').length, held = doc.groups.find(g => g.id === 'held').items.length;
      const want = `Fedora and Flatpak: up to date · ${held} held back, 1 to try · ${stale} other updates · 1 check failed`;
      log('strip="' + txt('.status-strip .st-line') + '" sub="' + txt('.st-sub') + '"');
      check(st().view === 'empty' && !!q('.status-strip') && !q('main .hero'), 'nothing pending + rows below: a status strip, no hero');
      check(txt('.status-strip .st-line') === want, 'the strip reads as one sentence: "' + want + '"');
      const line = q('.st-line'), segs = qa('.st-line > *');
      check(segs.every(s => Math.abs(s.getBoundingClientRect().top - segs[0].getBoundingClientRect().top) < 4), 'the strip fits on one line at 1280 px');
      check(/^Kernel .* · Secure Boot on$/.test(txt('.st-sub')), 'the kernel / Secure Boot line is the quiet subline');
      check(q('.status-strip .glyph.ok') && txt('.status-strip .glyph') === '✓', 'green check: Fedora and Flatpak are clean');
      check(qa('main [data-action="check-now"]').length === 0 && qa('#header-actions [data-action="check-now"]').length === 1, 'one Check now, in the header');
      check(!q('#brand-sub .ew-link'), 'no second pointer in the header (the strip says it)');
      check(qa('.ew-row').length === ew.length && !!q('.held-row'), 'Held back and Other software rows are right under the strip');
      check(line.getBoundingClientRect().bottom < 140, 'the strip sits at the top of the page');
      q('.st-link[data-action="scroll-elsewhere"]').click();
    }, 3);
    then(() => { check(q('#list').scrollTop > 0, 'the "other updates" segment scrolls to its section'); q('#list').scrollTop = 0; }, 2);
    then(() => shot('strip-after-runs-' + theme()), 8);
    // --- no rows anywhere: the trimmed hero
    then(() => push(d => { d.groups = []; d.summary = { total: 0, total_bytes: 0 }; d.elsewhere.items = []; }), 3);
    then(() => {
      log('empty: h1="' + txt('main h1') + '" p="' + txt('main .hero p') + '" fine="' + txt('main .hero .fine') + '"');
      check(!!q('.hero.trimmed') && txt('main h1') === 'You’re up to date' && !q('.status-strip'), 'nothing anywhere: the hero, trimmed');
      check(qa('main .hero .btn').length === 0 && qa('#header-actions [data-action="check-now"]').length === 1, 'the trimmed hero has no button; Check now stays in the header');
      check(q('.hero.trimmed .ring').getBoundingClientRect().height <= 64, 'the ring is the small one');
    }, 2);
    then(() => shot('empty-' + theme()), 8);
    toggleTheme();
    then(() => shot('empty-' + theme()), 8);
  }

  if (SC === 'other-row') {
    // the "nothing pending" screen untouched: the status strip over Held back + Other software
    then(() => push(nothingPending), 3);
    then(() => {
      check(!!q('.status-strip') && !q('main .hero') && /^Fedora and Flatpak: up to date · 2 held back, 1 to try · 16 other updates · 1 check failed$/.test(txt('.st-line')), 'the strip, as one sentence: "' + txt('.st-line') + '"');
      const strip = q('.status-strip').getBoundingClientRect(), h = q('.ew-h').getBoundingClientRect();
      check(strip.height <= 80 && h.top - strip.bottom <= 44, 'compact: the first section starts right under the strip (' + Math.round(strip.height) + ' px, gap ' + Math.round(h.top - strip.bottom) + ')');
      shot('strip-' + theme());
    }, 8);
    toggleTheme();
    then(() => shot('strip-' + theme()), 8);
    then(() => { scrollTo(q('.ew-h'), 10); }, 2);
    then(() => {
      const r = row('npm:pnpm');
      log('collapsed pnpm: ' + Math.round(r.getBoundingClientRect().height) + ' px, hint="' + txt('.ew-hint', r) + '"');
      check(!q('.ew-expand') && r.getAttribute('aria-expanded') === 'false', 'rows start collapsed');
      check(r.getBoundingClientRect().height <= 72, 'a row with a hint is at most two short lines');
      check(row('uv:ruff').getBoundingClientRect().height <= 54, 'a row without a hint is one line');
      shot('row-collapsed-' + theme());
      r.querySelector('svg.chev').dispatchEvent(new MouseEvent('click', { bubbles: true }));   // the chevron opens it
    }, 8);
    then(() => {
      const r = row('npm:pnpm'), x = q('.ew-expand[data-id="npm:pnpm"]');
      log('expanded pnpm: "' + txt('.ew-about', x).slice(0, 160) + '…"');
      check(!!x && r.getAttribute('aria-expanded') === 'true' && r.classList.contains('open'), 'the chevron opens the details');
      check(/WHAT IT IS/.test(txt('h4', x)) && /JavaScript package manager/.test(txt('.ew-what', x)), 'details: what it is');
      check(/^USED BY ?Builds Website/i.test(txt('.ew-used', x)) && /~\/.npm-global/.test(txt('.ew-used', x)), 'details: used by and where');
      check(/Website does not pin pnpm.*stay on 9/.test(txt('.ew-warn', x)), 'details: the whole warning');
      check(!r.querySelector('.ew-hint'), 'the truncated hint is not repeated while open');
      check(/i -g --prefix ~\/.npm-global pnpm@latest/.test(txt('.notes', x)), 'details: the exact command');
    }, 2);
    then(() => shot('row-expanded-' + theme()), 8);
    toggleTheme();
    then(() => shot('row-expanded-' + theme()), 8);
    then(() => row('npm:pnpm').click(), 2);
    then(() => { check(!q('.ew-expand[data-id="npm:pnpm"]'), 'clicking the row again collapses it'); shot('row-collapsed-' + theme()); }, 8);
    then(() => { row('ha:update.matter_server_update').click(); }, 2);
    then(() => {
      const x = q('.ew-expand[data-id="ha:update.matter_server_update"]');
      log('expanded matter: "' + txt('.ew-about', x) + '"');
      check(/nothing uses it yet \(0 Matter entities\)/.test(txt('.ew-used', x)) && /Remove uninstalls it from Home Assistant after you confirm/.test(txt('.ew-about', x)), 'the unused add-on explains Remove');
      check(!!row('ha:update.matter_server_update').querySelector('.ew-remove'), 'Remove stays on the open row');
      scrollTo(row('ha:update.matter_server_update'));
    }, 2);
    then(() => shot('row-unused-' + theme()), 8);
    // containers (2026-11-01): a rebuilt image says so only in its details; CouchDB points at its runbook
    then(() => { row('ha:update.matter_server_update').click(); row('podman:docker.io/library/redis:7.2').click(); }, 2);
    then(() => {
      const x = q('.ew-expand[data-id="podman:docker.io/library/redis:7.2"]');
      log('expanded redis: "' + txt('.ew-about', x) + '" / "' + txt('.why p', x.children[1]) + '"');
      check(/Image rebuilt upstream \(same version\)/.test(txt('.ew-rebuilt', x)), 'rebuilt: one quiet line in the details');
      check(/Nothing to do/.test(x.textContent), 'and nothing to do');
      row('podman:docker.io/library/couchdb:3').click();
    }, 2);
    then(() => {
      const x = q('.ew-expand[data-id="podman:docker.io/library/couchdb:3"]');
      check(/holds data, so Rivet never updates it.*Notes sync/.test(x.textContent), 'CouchDB: holds data, upgrade by hand following the runbook');
      check(/release notes/.test(txt('.meta a', x)), 'CouchDB links its release notes');
      scrollTo(row('podman:docker.io/library/couchdb:3'));
    }, 2);
    then(() => shot('row-containers-' + theme()), 8);
  }

  if (SC === 'other-bulk') {
    const grp = (g) => q(`.ew-group[data-group="${g}"]`);
    const listIds = () => qa('.bulk-list li').map(li => li.dataset.id);
    then(() => push(nothingPending), 3);
    // the HA group: exactly the safe add-ons
    then(() => { grp('ha').querySelector('.ew-bulk').click(); }, 2);
    then(() => {
      log('ha confirm: "' + txt('.ew-bulk-modal .t') + '" ids=' + listIds());
      check(listIds().join(',') === 'ha:update.alexa_media_player_update,ha:update.mosquitto_broker_update,ha:update.samba_share_update', 'HA: the add-ons and the integration, Alexa minor included (never Matter 9.x, HA OS or firmware)');
      check(qa('.bulk-list .cmd').every(c => /^Home Assistant: update\.install update\./.test(txt('.cmd', c.parentNode))), 'each line says the exact HA call');
      check(qa('.bulk-list li .cb.on').length === 3 && txt('[data-action="ew-bulk-go"]') === 'Update 3', 'every row starts ticked: Update 3');
      check(/restarts once .* to load Alexa Media Player/.test(txt('.ew-bulk-restart')), 'it says Home Assistant restarts once for the integration');
      q('.confirm-actions [data-action="ew-bulk-cancel"]').click();
    }, 2);
    then(() => check(!q('.ew-bulk-modal') && !Object.keys(st().ewRuns).length, 'Not now closes it and runs nothing'), 1);
    // This PC: the patch releases only
    then(() => { grp('pc').querySelector('.ew-bulk').click(); }, 2);
    then(() => {
      log('pc confirm ids=' + listIds());
      check(listIds().join(',') === 'npm:verdaccio,pipx:httpie', 'This PC: verdaccio + httpie, never pnpm (major, warned) or ruff (minor)');
      check(qa('.bulk-list .cmd').map(c => txt('.cmd', c.parentNode)).join(' | ') === '$ ~/.nvm/versions/node/v26.12.1/bin/npm i -g --prefix ~/.npm-global verdaccio@latest | $ pipx upgrade httpie', 'each line shows the exact command');
      shot('bulk-confirm-pc-' + theme());
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    }, 8);
    then(() => check(!q('.ew-bulk-modal'), 'Escape closes the confirmation'), 1);
    // Containers: the two stateless pulls, one fails (fake-apply's nginx), confirmation shots light + dark
    then(() => { scrollTo(grp('containers'), 20); grp('containers').querySelector('.ew-bulk').click(); }, 2);
    then(() => {
      log('containers confirm: "' + txt('.ew-bulk-modal .confirm-text') + '" ids=' + listIds());
      check(listIds().join(',') === 'podman:docker.io/library/nginx:1.27,podman:docker.io/library/caddy:2', 'Containers: only the two stateless pulls (never CouchDB, Ghost, redis or a pinned tag)');
      check(/nothing is restarted/.test(txt('.ew-bulk-modal .confirm-text')), 'it says a pull restarts nothing');
      shot('bulk-confirm-' + theme());
    }, 8);
    toggleTheme();
    then(() => shot('bulk-confirm-' + theme()), 8);
    then(() => q('[data-action="ew-bulk-go"]').click(), 1);
    until(() => runOf('podman:docker.io/library/nginx:1.27') && q('.ew-bulk-run'), 'the bulk run starts', () => {
      log('running: group="' + txt('.ew-bulk-run') + '" queued=' + qa('.ew-run.queued').length);
      check(/^Updating 1 of 2/.test(txt('.ew-bulk-run')) && !!q('.ew-bulk-run [data-action="ew-bulk-stop"]'), 'the group says Updating 1 of 2, with Stop after this one');
      check(qa('.ew-run.queued').length === 1 && /Waiting its turn \(2 of 2\)/.test(txt('.ew-run.queued')), 'the other one waits its turn');
      check(qa('.ew-row [data-action="ew-update"]:not(:disabled)').length === 0, 'every Update button is disabled during the bulk run');
      scrollTo(grp('containers'), 20);
    });
    then(() => shot('bulk-running-' + theme()), 6);
    until(() => !st().ewBatch, 'the bulk run ends', () => {
      const res = ['nginx:1.27', 'caddy:2'].map(n => { const r = runOf('podman:docker.io/library/' + n); return n + '=' + (r ? (r.classList.contains('ok') ? 'ok' : r.classList.contains('fail') ? 'fail' : 'running') : 'none'); });
      log('bulk done: ' + res.join(' '));
      check(res.join(' ') === 'nginx:1.27=fail caddy:2=ok', 'one failure does not stop the rest; each row has its own result');
      check(!q('.ew-bulk-run') && !q('.ew-group[data-group="containers"] .ew-bulk'), 'the group button is gone (nothing safe left to run)');
      scrollTo(grp('containers'), 20);
    });
    then(() => shot('bulk-done-' + theme()), 8);
    // Stop after this one: This PC's two, stopped during the first
    then(() => { scrollTo(grp('pc'), 20); grp('pc').querySelector('.ew-bulk').click(); }, 2);
    then(() => q('[data-action="ew-bulk-go"]').click(), 1);
    until(() => q('.ew-bulk-run [data-action="ew-bulk-stop"]'), 'the second bulk run starts', () => q('.ew-bulk-run [data-action="ew-bulk-stop"]').click());
    then(() => check(/stopping after this one/.test(txt('.ew-bulk-run')), 'Stop says it will stop after the running row'), 1);
    until(() => !st().ewBatch, 'the stopped run ends', () => {
      log('after stop: verdaccio=' + (runOf('npm:verdaccio') || {}).className + ' httpie=' + ((runOf('pipx:httpie') || {}).className || 'none') + ' toast="' + txt('.toast') + '"');
      check(runOf('npm:verdaccio') && runOf('npm:verdaccio').classList.contains('ok') && !runOf('pipx:httpie'), 'the running row finished, the next never started');
      check(/^Stopped\. 1 not started\./.test(txt('.toast')), 'a toast says what was not started');
      check(txt('.badge', row('pipx:httpie')) === 'PATCH' && !!row('pipx:httpie').querySelector('[data-action="ew-update"]:not(:disabled)'), 'the skipped row is still there with its own Update');
    });
    until(() => !q('.toast'), 'the toast clears');
    // Home Assistant (2026-11-01): leave Mosquitto out with its checkbox, run Alexa + Samba share,
    // then ONE Home Assistant restart for the integration
    const AL = 'ha:update.alexa_media_player_update', MQ = 'ha:update.mosquitto_broker_update', TS = 'ha:update.samba_share_update';
    then(() => { scrollTo(grp('ha'), 20); grp('ha').querySelector('.ew-bulk').click(); }, 2);
    then(() => q(`.bulk-list .cb[data-id="${MQ}"]`).click(), 2);
    then(() => {
      log('after unticking Mosquitto: button="' + txt('[data-action="ew-bulk-go"]') + '" off=' + qa('.bulk-list li.off').map(li => li.dataset.id));
      check(txt('[data-action="ew-bulk-go"]') === 'Update 2' && qa('.bulk-list li.off').map(li => li.dataset.id).join() === MQ, 'unticking a row leaves it out: Update 2');
      shot('bulk-ha-untick-' + theme());
    }, 8);
    then(() => q('[data-action="ew-bulk-go"]').click(), 1);
    until(() => q('.ew-ha-restart'), 'Home Assistant restarts after the integration', () => {
      log('restart: "' + txt('.ew-ha-restart') + '" alexa="' + txt('.st', runOf(AL)) + '"');
      check(/Restarting Home Assistant|Home Assistant is (restarting|back)|configuration/.test(txt('.ew-ha-restart')), 'the HA group says Home Assistant is restarting');
      check(!runOf(MQ) && !(st().ewBatch && st().ewBatch.ids.includes(MQ)), 'Mosquitto was left out');
      check(qa('.ew-row [data-action="ew-update"]:not(:disabled)').length === 0, 'every Update button is disabled while Home Assistant restarts');
      scrollTo(grp('ha'), 20);
      shot('bulk-ha-restart-' + theme());
    });
    until(() => !q('.ew-ha-restart') && runOf(AL) && runOf(AL).classList.contains('ok') && /loaded: Home Assistant restarted/.test(txt('.st', runOf(AL))), 'the restart ends', () => {
      log('after restart: alexa="' + txt('.st', runOf(AL)) + '" samba=' + (runOf(TS) || {}).className + ' mosquitto=' + ((runOf(MQ) || {}).className || 'none'));
      check(!!runOf(TS) && !runOf(MQ), 'Samba share ran, Mosquitto did not');
      check(!row(AL).querySelector('.ew-restart'), 'no Restart HA button left on Alexa');
      scrollTo(grp('ha'), 20);
    });
    then(() => shot('bulk-ha-done-' + theme()), 8);
  }

  if (SC === 'other-fix') {
    const AL = 'ha:update.alexa_media_player_update';
    then(() => {
      shapeChecks(st().doc);
      check(st().settings.show_containers === false, 'show_containers is off by default');
      check(!/Containers/.test(txt('.ew-h')) && !q('.ew-group[data-group="containers"]'), 'no Containers section');
      click('[data-action="open-settings"]');
    }, 3);
    then(() => {
      const t = q('.set-row[data-key="show_containers"]');
      check(!!t && /Show containers/.test(t.textContent) && !t.querySelector('.toggle.on'), 'Preferences has a Show containers toggle, off');
      shot('prefs-containers-' + theme());
    }, 8);
    then(() => q('.modal [data-action="close-modal"]').click(), 2);
    then(() => { scrollTo(row(AL)); row(AL).querySelector('[data-action="ew-update"]').click(); }, 1);
    until(() => runOf(AL) && runOf(AL).classList.contains('fail'), 'Alexa fails', () => {
      const r = runOf(AL), fx = r.querySelector('[data-action="ew-fix"]');
      log('failed: "' + txt('.st', r) + '"');
      check(/stopped without installing it/.test(txt('.st', r)), 'the row shows the short reason');
      check(fx && txt('[data-action="ew-fix"]', r) === 'Fix it' && !fx.disabled, 'and a Fix it button');
      scrollTo(row(AL));
    });
    then(() => shot('fix-offered-' + theme()), 8);
    then(() => runOf(AL).querySelector('[data-action="ew-fix"]').click(), 1);
    until(() => runOf(AL) && runOf(AL).classList.contains('running') && /Checking: Fix it: (state|restart)|Asking Claude|Evidence/.test(txt('.st', runOf(AL))), 'Fix it streams', () => {
      log('fixing: "' + txt('.st', runOf(AL)) + '"');
      check(qa('.ew-row [data-action="ew-update"]:not(:disabled)').length === 0, 'Update buttons are disabled while Fix it runs');
      shot('fix-running-' + theme());
    });
    until(() => runOf(AL) && runOf(AL).classList.contains('ok'), 'Fix it ends', () => {
      log('fixed: "' + txt('.st', runOf(AL)) + '"');
      check(/^✓ Fixed: HACS had downloaded v5\.18\.0 but Home Assistant needed a restart; restarted, now v5\.18\.0/.test(txt('.st', runOf(AL))), 'the result line lands in the row');
      check(!runOf(AL).querySelector('[data-action="ew-fix"]'), 'no Fix it button once it is fixed');
      scrollTo(row(AL));
    });
    then(() => shot('fix-done-' + theme()), 8);
    toggleTheme();
    then(() => shot('fix-done-' + theme()), 8);
  }

  if (SC === 'other-remove') {
    const MID = 'ha:update.matter_server_update';
    const haCount = () => Number(txt('.ew-group[data-group="ha"] .ew-sub-t .n'));
    let ha0 = 0;
    then(() => push(nothingPending), 3);
    then(() => {
      ha0 = haCount();
      const rm = row(MID).querySelector('.ew-remove');
      log('matter row: ' + txt('.name', row(MID)) + ' hint="' + txt('.ew-hint', row(MID)) + '" remove="' + txt('.ew-remove', row(MID)) + '"');
      check(rm && txt('.ew-remove', row(MID)) === 'Remove' && rm.classList.contains('ghost') && !rm.disabled, 'Matter Server: a secondary Remove button');
      scrollTo(row(MID));
      rm.click();
    }, 2);
    then(() => {
      const m = q('.ew-remove-modal');
      log('dialog: "' + txt('.t', m) + '" | ' + qa('.ew-remove-modal .confirm-text').map(p => txt('.confirm-text', p.parentNode) && p.textContent.trim()).join(' | '));
      check(!!m && txt('.t', m) === 'Remove Matter Server?', 'the confirmation names the add-on');
      check(/^Nothing in Home Assistant uses it \(0 Matter entities\)/.test(txt('.confirm-text', m)), 'it says nothing in Home Assistant uses it (0 Matter entities)');
      check(/uninstalls the add-on and deletes the data it keeps/.test(m.textContent) && /Integrations that depended on it are not touched/.test(m.textContent), 'it says what happens and what is not touched');
      check(/supervisor\/api POST \/addons\/core_matter_server\/uninstall/.test(txt('.cmd', m)), 'it shows the exact Supervisor call');
      check(qa('.ew-remove-modal .confirm-actions .btn').map(b => b.textContent.trim()).join('/') === 'Cancel/Remove', 'Cancel / Remove');
      check(!runOf(MID), 'nothing runs before confirming');
      shot('remove-confirm-' + theme());
    }, 8);
    toggleTheme();
    then(() => shot('remove-confirm-' + theme()), 8);
    then(() => q('.confirm-actions [data-action="ew-remove-cancel"]').click(), 2);
    then(() => check(!q('.ew-remove-modal') && !runOf(MID) && !!row(MID), 'Cancel closes it, runs nothing, the row stays'), 1);
    // first try: the (fake) live re-check finds entities -> fails, keeps the row and a fallback
    then(() => row(MID).querySelector('.ew-remove').click(), 2);
    then(() => q('.confirm-actions [data-action="ew-remove-go"]').click(), 1);
    until(() => runOf(MID) && runOf(MID).classList.contains('running'), 'the row shows its busy state', () => {
      log('running: "' + txt('.st', runOf(MID)) + '"');
      check(/Checking that nothing in Home Assistant uses Matter Server/.test(txt('.st', runOf(MID))), 'busy: it says it is checking first');
      check(row(MID).querySelector('.ew-remove').disabled, 'Remove is disabled while it runs');
      check(qa('.ew-row [data-action="ew-update"]:not(:disabled)').length === 0, 'every Update button is disabled while it runs');
    });
    until(() => runOf(MID) && runOf(MID).classList.contains('fail'), 'the first remove fails', () => {
      const r = runOf(MID), fb = r.querySelector('[data-action="open-url"]');
      log('failed: "' + txt('.st', r) + '"');
      check(/Matter Server was not removed: the Matter integration has 2 entities now/.test(txt('.st', r)), 'a failure shows the reason');
      check(fb && /Open in Home Assistant/.test(fb.textContent) && /\/_my_redirect\/supervisor_addon\?addon=core_matter_server$/.test(fb.dataset.url), 'and an Open in Home Assistant fallback to the add-on page');
      check(!!row(MID) && haCount() === ha0, 'the row stays');
      scrollTo(row(MID));
    });
    then(() => shot('remove-failed-' + theme()), 8);
    // second try: it goes
    then(() => row(MID).querySelector('.ew-remove').click(), 2);
    then(() => q('.confirm-actions [data-action="ew-remove-go"]').click(), 1);
    until(() => runOf(MID) && /Uninstalling Matter Server/.test(txt('.st', runOf(MID))), 'the uninstall streams', () => shot('remove-running-' + theme()));
    until(() => !row(MID), 'the row disappears', () => {
      log('removed: toast="' + txt('.toast') + '" ha group=' + haCount());
      check(/Matter Server was uninstalled from Home Assistant/.test(txt('.toast')), 'a toast says it was uninstalled');
      check(haCount() === ha0 - 1 && !st().doc.elsewhere.items.some(i => i.id === MID), 'the Home Assistant group lost exactly that row');
      check(!q('.ew-remove'), 'no Remove left anywhere');
    });
    then(() => shot('remove-done-' + theme()), 8);
    then(() => check(!row(MID) && !st().doc.elsewhere.items.some(i => i.id === MID), 'still gone after the state.json push'), 6);
    until(() => !q('.toast'), 'the toast clears');
  }

  if (SC === 'other-real') {
    // the real state.json of this box: look only, never press a button
    then(() => {
      dumpRows();
      log('view=' + st().view + ' strip="' + txt('.status-strip .st-line') + '" sub="' + txt('.st-sub') + '"');
      shapeChecks(st().doc);
      check(qa('.ew-row[data-id^="podman:"] .name').every(n => n.scrollWidth <= n.clientWidth + 1), 'podman image names are not cut off');
      if (lister()) lister().scrollTop = 0;
    }, 4);
    then(() => shot('real-' + theme()), 8);
    then(() => { scrollTo(q('.ew-h'), 10); }, 3);
    then(() => shot('real-other-' + theme()), 8);
    then(() => { const l = lister(); if (l) l.scrollTop += 560; }, 3);
    then(() => shot('real-other-more-' + theme()), 8);
    toggleTheme();
    then(() => { if (lister()) lister().scrollTop = 0; }, 3);
    then(() => shot('real-' + theme()), 8);
    then(() => { scrollTo(q('.ew-h'), 10); }, 3);
    then(() => shot('real-other-' + theme()), 8);
    then(() => { const l = lister(); if (l) l.scrollTop += 560; }, 3);
    then(() => shot('real-other-more-' + theme()), 8);
    // the same real rows with dnf/Flatpak/fwupd emptied in the page only (nothing is written):
    // what the front page looks like on this box when nothing is pending
    then(() => { push(nothingPending); if (lister()) lister().scrollTop = 0; }, 3);
    then(() => { if (lister()) lister().scrollTop = 0; log('real strip (simulated, dnf emptied): "' + txt('.st-line') + '"'); check(!!q('.status-strip') && !q('main .hero'), 'real rows: the strip, not the hero'); shot('real-strip-' + theme()); }, 8);
    toggleTheme();
    then(() => shot('real-strip-' + theme()), 8);
  }

  // QA_CUT=n (env of qa-e2e.sh): run only the first n steps, to bisect a failure
  if (typeof QA_CUT !== 'undefined') { steps.length = QA_CUT; log('cut at ' + QA_CUT + ' steps'); }
  let i = 0, tries = 0, started = false;
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (!started) { if (!q('.ew-row')) return; started = true; }
    if (i >= steps.length) {
      const anims = document.getAnimations ? document.getAnimations().filter(a => a.playState === 'running') : [];
      log('running animations at the end: ' + anims.length + (anims.length ? ' (' + anims.map(a => (a.animationName || '?') + ' on .' + String((a.effect && a.effect.target && a.effect.target.className) || '').replace(/\s+/g, '.')).join(', ') + ')' : ''));
      clearInterval(iv); log(bad ? bad + ' check(s) failed' : 'all checks passed'); log(bad ? 'DRIVER FAILED' : 'DRIVER DONE'); return;
    }
    const [fn, w, what] = steps[i];
    let r;
    try { r = fn(); } catch (e) { bad++; log('CHECK FAILED: step ' + i + ' threw ' + e); r = undefined; }
    if (r === 'again') { if (++tries > 75) { bad++; log('CHECK FAILED: waited too long for: ' + what); tries = 0; i++; } return; }
    tries = 0; i++; wait = w;
    if (Date.now() - t0 > LIMIT_MS) { log('DRIVER TIMEOUT at step ' + i); clearInterval(iv); }
  }, 200);
})();
