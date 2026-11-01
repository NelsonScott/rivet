/* Driver for qa-e2e.sh, the abuse-pass scenarios (2026-11-01, docs/QA-MATRIX.md). Runs inside the
 * real WebKit page; every button reaches qa/fake-apply.py / qa/fake-doctor.py (nothing real runs).
 *   abuse           a stale document portal (fake world): the health banner, its one-click fix, the
 *                   banner goes; Home Assistant goes away in the middle of "Update the safe ones":
 *                   the first row installs, the second fails with Fix it, the third is skipped and
 *                   offers nothing, no HA restart; a 0-update day (hero, then a broken Flatpak remote
 *                   in the strip); a 300-update day, render timed (FAIL over 1 s).
 *   abuse-noclaude  no Claude Code CLI (RIVET_CLAUDE_BIN points nowhere) and no Home Assistant: no
 *                   HA group, nothing says Home Assistant, the strip counts right, a failed row and
 *                   the result screen show "Install Claude Code to enable Fix it" instead of Fix it;
 *                   the password prompt is cancelled (fake-apply cancel = the real run_real) and the
 *                   list comes back as it was.
 * Prints [QA] lines; "DRIVER DONE" when every check passed, "DRIVER FAILED" otherwise. */
(function () {
  const SC = typeof SCENARIO !== 'undefined' ? SCENARIO : 'abuse';
  const log = (m) => console.log('[QA] ' + SC + ': ' + m);
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s, root) => (((root || document).querySelector(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const st = () => window.rivet.state;
  const row = (id) => q(`.ew-row[data-id="${id}"]`);
  const runOf = (id) => q(`.ew-run[data-id="${id}"]`);
  const grp = (g) => q(`.ew-group[data-group="${g}"]`);
  const lister = () => q('#list') || q('.empty-held');
  const scrollTo = (el, off) => { const l = lister(); if (el && l) l.scrollTop += el.getBoundingClientRect().top - l.getBoundingClientRect().top - (off == null ? 60 : off); };
  let wait = 0, bad = 0; const t0 = Date.now();
  const LIMIT_MS = 140000;
  const check = (cond, what) => { if (!cond) { bad++; log('CHECK FAILED: ' + what); } else log('ok: ' + what); };
  const steps = [];
  const then = (fn, w) => steps.push([fn, w || 3]);
  const until = (pred, what, fn) => steps.push([() => { if (!pred()) return 'again'; if (fn) fn(); }, 1, what]);
  const clone = () => JSON.parse(JSON.stringify(st().doc));
  const push = (d) => window.rivet.receive({ type: 'state', state: d });
  const nothingPending = (d) => { d.groups = d.groups.filter(g => g.id === 'held'); d.summary = { total: 0, total_bytes: 0 }; return d; };
  const ALEXA = 'ha:update.alexa_media_player_update', MOSQ = 'ha:update.mosquitto_broker_update', TAIL = 'ha:update.samba_share_update';

  if (SC === 'abuse') {
    // ---- 4. the stale document portal: banner, one-click fix, gone
    until(() => q('.health-banner[data-id="doc-portal"]'), 'the health banner', () => {
      log('banner: "' + txt('.health-banner .hb-title') + '" / "' + txt('.health-banner .hb-detail').slice(0, 120) + '…" button="' + txt('.health-banner .btn') + '"');
      check(txt('.health-banner .hb-title') === 'Flatpak apps may not start', 'the banner says what breaks');
      check(/\/fake-health\/run\/doc\/by-app is missing/.test(txt('.health-banner .hb-detail')), 'the detail names the missing dir (this run\'s fake one)');
      check(txt('.health-banner [data-action="health-fix"]') === 'Restart the document portal', 'one button: Restart the document portal');
      shot('abuse-health-banner');
    });
    then(() => q('.health-banner [data-action="health-fix"]').click(), 1);
    until(() => !q('.health-banner'), 'the banner goes after the fix', () => {
      check(/document portal was restarted/.test(txt('.toast')), 'a toast says what was done (' + txt('.toast') + ')');
      check(((st().health || {}).checks || []).length === 0, 'health.json has no problem left');
    });

    // ---- 1. Home Assistant goes away in the middle of a bulk run
    then(() => { scrollTo(grp('ha'), 20); grp('ha').querySelector('.ew-bulk').click(); }, 2);
    then(() => {
      check(qa('.bulk-list li').map(li => li.dataset.id).join(',') === [ALEXA, MOSQ, TAIL].join(','), 'the HA bulk lists Alexa, Mosquitto, Samba share');
      q('[data-action="ew-bulk-go"]').click();
    }, 1);
    until(() => !st().ewBatch && runOf(MOSQ) && runOf(MOSQ).classList.contains('fail') && runOf(TAIL), 'the bulk run ends', () => {
      log('alexa="' + txt('.st', runOf(ALEXA)) + '"');
      log('mosquitto="' + txt('.st', runOf(MOSQ)) + '"');
      log('samba="' + txt('.st', runOf(TAIL)) + '"');
      check(runOf(ALEXA) && runOf(ALEXA).classList.contains('ok'), 'the first HA row installed');
      check(/could not reach Home Assistant .*connection refused/.test(txt('.st', runOf(MOSQ))), 'the second row says Home Assistant could not be reached');
      check(!!runOf(MOSQ).querySelector('[data-action="ew-fix"]'), 'the failed row offers Fix it');
      check(/^– Skipped: Home Assistant could not be reached a moment ago, so nothing was sent/.test(txt('.st', runOf(TAIL))) && runOf(TAIL).classList.contains('skipped'), 'the third row was skipped and says so, neutral (not red)');
      check(!runOf(TAIL).querySelector('[data-action="ew-fix"]'), 'a skipped row offers no Fix it (nothing ran)');
      check(!q('.ew-ha-restart'), 'no Home Assistant restart while it is unreachable');
      check(qa('.ew-row [data-action="ew-update"]:disabled').length === 0, 'the other Update buttons are enabled again');
      scrollTo(grp('ha'), 20);
    });
    then(() => shot('abuse-ha-gone'), 6);

    // ---- 3. a 0-update day, then a broken Flatpak remote on it
    then(() => { const d = nothingPending(clone()); delete d.elsewhere; d.groups.forEach(g => { g.items = []; }); push(d); }, 3);
    then(() => {
      log('0 updates: h1="' + txt('main h1') + '" p="' + txt('main .hero p') + '"');
      check(/You.re up to date/.test(txt('main h1')) && !q('.status-strip'), 'nothing anywhere: the trimmed hero, no strip');
      shot('abuse-zero');
      const d = clone(); d.flags = Object.assign({}, d.flags, { flatpak_remote_errors: [{ scope: 'user', remote: 'broken', error: 'error: Unable to load summary from remote broken: Could not resolve hostname' }] });
      push(d);
    }, 6);
    then(() => {
      log('broken remote strip: "' + txt('.st-line') + '"');
      check(!!q('.status-strip') && !q('main .hero'), 'a broken remote shows the strip, never the up-to-date hero');
      check(/^Fedora: up to date · Flatpak: broken could not be checked$/.test(txt('.st-line')), 'the strip names the remote and does not call Flatpak current');
      check(!!q('.status-strip .glyph.warn'), 'the strip glyph is a warning');
      shot('abuse-broken-remote');
    }, 6);

    // ---- 3. a 300-update day: render time
    let base = null;
    then(() => {
      base = clone();
      const dnf = { id: 'dnf', title: 'System', subtitle: 'dnf', items: [] };
      for (let n = 0; n < 300; n++) {
        const name = 'pkg' + String(n).padStart(3, '0') + (n % 3 ? '-libs' : '');
        dnf.items.push({ id: 'dnf:' + name, source: 'dnf', name, display_name: name, from: '1.0-1.fc44', to: '1.1-1.fc44', size_bytes: 120000 + n,
          ref: name + '-1.1-1.fc44.x86_64', risk: n % 3 ? 'green' : 'yellow', badge: null, verdict: 'Routine bug fix release.',
          reasoning: 'Bug fixes only, per the changelog.', notes_excerpt: '- fix a crash\n- update translations', notes_url: null,
          advisory: { id: 'FEDORA-2026-' + (100000 + n), type: 'bugfix', severity: null }, requires_reboot: false, blocked_by: null, selected_default: true });
      }
      base.groups = [dnf].concat(base.groups.filter(g => g.id === 'held'));
      base.summary = { total: 300, total_bytes: dnf.items.reduce((a, i) => a + i.size_bytes, 0) };
      base.flags = Object.assign({}, base.flags, { flatpak_remote_errors: [] });
      const a = performance.now();
      push(base);
      void document.body.offsetHeight;                // include style + layout
      const ms = performance.now() - a;
      const rows = qa('.card[data-group="dnf"] .row').length;
      log('300 updates: render ' + ms.toFixed(0) + ' ms, ' + rows + ' dnf rows, header "' + txt('#brand-sub') + '"');
      check(st().view === 'updates' && rows > 50, 'the updates view with the rows');
      check(ms < 1000, '300 updates render under 1 s (' + ms.toFixed(0) + ' ms)' + (ms >= 1000 ? ' FLAG' : ''));
    }, 3);
    then(() => {
      const r = q('.row.routine');
      const a = performance.now();
      if (r) r.click();
      void document.body.offsetHeight;
      const ms = performance.now() - a;
      log('routine accordion open: ' + ms.toFixed(0) + ' ms, ' + qa('.routine-body .row').length + ' rows');
      check(!!r && ms < 1000, 'opening the routine accordion of a 300-update day under 1 s (' + ms.toFixed(0) + ' ms)');
      const cb = q('.cb[data-action="toggle-row"]');
      const b = performance.now();
      if (cb) cb.click();
      void document.body.offsetHeight;
      const ms2 = performance.now() - b;
      log('one checkbox click re-render: ' + ms2.toFixed(0) + ' ms');
      check(ms2 < 1000, 'a click on a 300-update day re-renders under 1 s (' + ms2.toFixed(0) + ' ms)');
    }, 3);
    then(() => shot('abuse-300'), 6);
  }

  if (SC === 'abuse-noclaude') {
    let count0 = '', rows0 = 0;
    then(() => {
      check(st().caps.claude === false, 'the bridge says there is no Claude Code CLI');
      // a box with no Home Assistant: the ha source is absent, so no HA row exists; npm:verdaccio failed last time
      const d = clone();
      d.elsewhere.items = d.elsewhere.items.filter(i => i.source !== 'ha').map(i => i.id === 'npm:verdaccio'
        ? Object.assign({}, i, { last_run: { at: null, result: 'failed', note: 'npm exited 1: EACCES: permission denied' } }) : i);
      d.elsewhere.sources = d.elsewhere.sources.map(s => s.id === 'ha' ? Object.assign({}, s, { status: 'absent', checked: 0, stale: 0 }) : s);
      push(d);
    }, 4);
    then(() => {
      check(!grp('ha'), 'no Home Assistant group');
      check(!/Home Assistant/.test(document.body.innerText), 'nothing on the page says Home Assistant');
      const r = row('npm:verdaccio');
      log('failed row: hint="' + txt('.ew-hint', r) + '" note="' + txt('.no-claude', r) + '"');
      check(!q('[data-action="ew-fix"]'), 'no Fix it button anywhere');
      check(txt('.no-claude', r) === 'Install Claude Code to enable Fix it', 'the failed row says how to get Fix it');
      shot('abuse-noclaude-row');
      count0 = txt('[data-action="update-now"] .count'); rows0 = qa('.row').length;
      // the "nothing pending" screen of that box
      push(nothingPending(clone()));
    }, 6);
    then(() => {
      const stale = st().doc.elsewhere.items.filter(i => i.status === 'ok' && i.source !== 'podman').length;
      log('strip: "' + txt('.st-line') + '"');
      check(!/Home Assistant/.test(txt('.st-line')), 'the strip does not mention Home Assistant');
      check(new RegExp(' · ' + stale + ' other updates').test(txt('.st-line')), 'the strip counts ' + stale + ' other updates (This PC only)');
      check(/1 check failed/.test(txt('.st-line')), 'the failed cargo check is counted');
      shot('abuse-noclaude-strip');
    }, 6);
    // back to the list with something pending: the sample, as the collector would write it
    then(() => { push(JSON.parse(JSON.stringify(window.__abuseBase))); }, 3);
    until(() => q('[data-action="update-now"]:not([disabled])'), 'the updates view', () => {
      count0 = txt('[data-action="update-now"] .count'); rows0 = qa('.row').length;
      log('before: ' + count0 + ' selected, ' + rows0 + ' rows');
      q('[data-action="update-now"]').click();
    });
    until(() => q('.hero.result') && (q('[data-action="back-to-list"]') || q('[data-action="restart-later"]')), 'the result screen', () => {
      log('result: h1="' + txt('main h1') + '" cause="' + txt('.hero .cause') + '" tally="' + txt('.hero .tally') + '"');
      check(txt('main h1') === 'Update failed', 'the run failed (nothing landed, firmware not staged after the refusal)');
      check(!/restart/i.test(txt('.hero .rebootline')), 'no "restart to finish" when nothing was installed (' + txt('.hero .rebootline') + ')');
      check(txt('.hero .cause') === 'The administrator password prompt was cancelled or denied, so nothing was installed.', 'the cause says the prompt was cancelled');
      check(!q('[data-action="fix-it"]') && txt('.hero .no-claude') === 'Install Claude Code to enable Fix it', 'no Fix it, the install note instead');
      shot('abuse-cancelled');
    });
    then(() => q('[data-action="back-to-list"]').click(), 2);
    until(() => q('[data-action="update-now"]'), 'back on the list', () => {
      log('after: ' + txt('[data-action="update-now"] .count') + ' selected, ' + qa('.row').length + ' rows');
      check(qa('.row').length === rows0 && txt('[data-action="update-now"] .count') === count0, 'the list and selection are as before (nothing was installed)');
      check(!st().apply || st().view === 'updates', 'no run left half open');
    });
  }

  if (typeof QA_CUT !== 'undefined') { steps.length = QA_CUT; log('cut at ' + QA_CUT + ' steps'); }
  let i = 0, tries = 0, started = false;
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (!started) {
      if (!q('.ew-row') || !st().doc) return;
      started = true;
      window.__abuseBase = JSON.parse(JSON.stringify(st().doc));
    }
    if (i >= steps.length) { clearInterval(iv); log(bad ? bad + ' check(s) failed' : 'all checks passed'); log(bad ? 'DRIVER FAILED' : 'DRIVER DONE'); return; }
    const [fn, w, what] = steps[i];
    let r;
    try { r = fn(); } catch (e) { bad++; log('CHECK FAILED: step ' + i + ' threw ' + e); r = undefined; }
    if (r === 'again') { if (++tries > 150) { bad++; log('CHECK FAILED: waited too long for: ' + what); tries = 0; i++; } return; }
    tries = 0; i++; wait = w;
    if (Date.now() - t0 > LIMIT_MS) { log('DRIVER TIMEOUT at step ' + i); clearInterval(iv); }
  }, 200);
})();
