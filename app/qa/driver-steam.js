/* Driver for qa-e2e.sh, scenario "steam" (2026-11-10): the Steam-only re-check.
 * state.json says Steam runs (3 runtime rows blocked); the fake flatpak (qa/fake-flatpak.sh)
 * says it does not. qa-e2e.sh touches a marker in the fake's dir for each "want <marker>" line.
 *   1. window opened -> focus re-check -> banner gone, rows unblocked and ticked (no collector)
 *   2. Steam started -> focus (qaFocus = the GTK is-active handler) -> banner back, rows blocked
 *   3. Quit Steam -> unblocked in about a second, never the loading screen, no "checking"
 *   4. a Steam that will not quit -> after ~5 s "Steam did not quit", banner stays
 * qa-e2e.sh asserts afterwards that the fake collector never ran. */
(function () {
  const log = (m) => console.log('[QA] steam: ' + m);
  const q = (s) => document.querySelector(s);
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const post = (o) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify(o));
  const expect = (what, ok, got) => log((ok ? 'PASS ' : 'FAIL ') + what + (ok ? '' : ' (got: ' + got + ')'));
  const R = () => window.rivet.state;
  const flags = () => (R().doc && R().doc.flags) || {};
  const runtimes = () => (((R().doc && R().doc.groups) || []).find(g => g.id === 'flatpak') || { items: [] }).items;
  const banner = () => !!q('.banner [data-action="quit-steam"]');
  const toastText = () => txt('.toast');
  const describe = () => runtimes().map(i => i.id.split('/')[0].replace('flatpak:', '') + '=' + (i.blocked_by ? 'blocked' : 'free')
    + (R().selected.has(i.id) ? '+ticked' : '')).join(' ');
  const t0 = Date.now();
  let stage = 0, at = Date.now(), lastCheck = 0, views = new Set(), statuses = new Set(), tClick = 0;
  const since = () => Date.now() - at;
  const next = (s) => { stage = s; at = Date.now(); };
  const iv = setInterval(() => {
    const st = R();
    if (stage === 0) {
      if (!st.doc || !q('[data-action="update-now"]')) return;
      if (flags().steam_running !== false && since() < 8000) return;
      lastCheck = Date.now();
      log('after open: steam_running=' + flags().steam_running + ' blocked_count=' + flags().blocked_count + ' sub="' + txt('#brand-sub') + '"');
      log('  rows: ' + describe());
      expect('window open: state says Steam is not running', flags().steam_running === false, flags().steam_running);
      expect('window open: no Steam banner', !banner(), banner());
      expect('window open: runtime rows unblocked and ticked', runtimes().length === 3
        && runtimes().every(i => !i.blocked_by && i.badge !== 'blocked' && st.selected.has(i.id)), describe());
      expect('checked_at kept (the last full check)', st.doc.checked_at === '2026-09-15T09:41:02-04:00', st.doc.checked_at);
      shot('steam-1-open-unblocked'); log('want steam-running'); next(1);
    } else if (stage === 1) {
      if (Date.now() - lastCheck < 6000) return;           // the focus debounce is 5 s
      log('focus (Steam was started meanwhile)'); lastCheck = Date.now(); post({ cmd: 'qaFocus' }); next(2);
    } else if (stage === 2) {
      if (flags().steam_running !== true && since() < 5000) return;
      log('after focus: ' + describe());
      expect('focus: banner back within ' + since() + ' ms', banner() && flags().steam_running === true, banner());
      expect('focus: runtime rows blocked and unticked', runtimes().every(i => i.blocked_by === 'steam_running'
        && i.badge === 'blocked' && i.risk === 'red' && !st.selected.has(i.id)), describe());
      shot('steam-2-focus-blocked'); next(3);
    } else if (stage === 3) {
      if (since() < 4000) return;                          // let the snapshot's main-thread work finish
      if (!banner()) { expect('a Quit Steam button to click', false, 'none'); next(9); return; }
      views = new Set([st.view]); statuses = new Set([st.doc.status]);
      tClick = Date.now(); q('.banner [data-action="quit-steam"]').click(); log('clicked Quit Steam'); next(4);
    } else if (stage === 4) {
      views.add(st.view); statuses.add(st.doc.status);
      if (flags().steam_running !== false && since() < 9000) return;
      const dt = Date.now() - tClick; lastCheck = Date.now();
      log('Quit Steam: unblocked after ' + dt + ' ms; views seen: ' + [...views].join(',') + '; statuses: ' + [...statuses].join(','));
      expect('Quit Steam unblocks in under 2.5 s (' + dt + ' ms)', flags().steam_running === false && dt < 2500, dt);
      expect('never the loading screen, never "checking"', !views.has('loading') && !statuses.has('checking'), [...views, ...statuses]);
      expect('rows unblocked and ticked again', runtimes().every(i => !i.blocked_by && st.selected.has(i.id)), describe());
      next(5);
    } else if (stage === 5) {
      if (since() < 600) return;
      log('toast: "' + toastText() + '"');
      expect('toast says Steam closed', /Steam closed/.test(toastText()), toastText());
      expect('banner gone', !banner(), banner());
      shot('steam-3-quit-unblocked'); log('want steam-running'); log('want steam-stubborn'); next(6);
    } else if (stage === 6) {
      if (Date.now() - lastCheck < 6000) return;
      post({ cmd: 'qaFocus' }); lastCheck = Date.now(); next(7);
    } else if (stage === 7) {
      if (!banner() && since() < 5000) return;
      if (!banner()) { expect('focus: banner back for the stubborn round', false, flags().steam_running); next(9); return; }
      if (since() < 4000) return;
      tClick = Date.now(); q('.banner [data-action="quit-steam"]').click(); log('clicked Quit Steam (stubborn)'); next(8);
    } else if (stage === 8) {
      if (!/did not quit/.test(toastText()) && since() < 12000) return;
      const dt = Date.now() - tClick;
      log('stubborn: toast after ' + dt + ' ms: "' + toastText() + '"');
      expect('a Steam that will not quit is said plainly after ~5 s', /did not quit/.test(toastText()) && dt > 4500 && dt < 9000, dt);
      expect('banner stays, rows stay blocked', banner() && runtimes().every(i => i.blocked_by), describe());
      next(10);
    } else if (stage === 10) {
      if (since() < 800) return;                           // past the toast's fade-in
      shot('steam-4-did-not-quit'); next(11);
    } else if (stage === 11) {
      if (since() < 4000) return;
      if (document.documentElement.dataset.theme !== 'dark') q('[data-action="toggle-theme"]').click();
      next(12);
    } else if (stage === 12) {
      if (since() < 1000) return;
      shot('steam-5-dark-blocked'); next(9);
    } else if (stage === 9) {
      if (since() < 4000) return;
      clearInterval(iv); log('DRIVER DONE');
    }
    if (Date.now() - t0 > 85000) { log('DRIVER TIMEOUT at stage ' + stage + ' view=' + st.view); clearInterval(iv); }
  }, 50);
})();
