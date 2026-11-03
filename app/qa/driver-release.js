/* Driver for qa-e2e.sh, scenario "release" (2026-11-03): the newer-Fedora row.
 * Runs inside the real WebKit page (RIVET_QA_JS); qa-e2e.sh prepends `const SCENARIO`.
 *
 * The fake collector adds the Fedora 45 row (backend/release.py on a canned check) to the
 * sample state. Checks: the row sits in the System section with the dnf rows, yellow,
 * REBOOT, "44 → 45", unticked; Select all / Safe only / Security only never tick it; ticking
 * it by hand shows the "runs last" banner and the REBOOT REQUIRED chip; Update now streams
 * the release step last (fake-apply `release`); the result screen says Fedora 45 is
 * downloaded and offers Restart now (never clicked) + Later. */
(function () {
  const log = (m) => console.log('[QA] release: ' + m);
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const count = () => (q('[data-action="update-now"] .count') || {}).textContent;
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const expect = (what, ok, got) => log((ok ? 'PASS ' : 'FAIL ') + what + (ok ? '' : ' (got: ' + got + ')'));
  const sel = () => window.rivet.state.selected;
  const ROW = '.card[data-group="dnf"] .row.release';
  const cb = () => q(ROW + ' .cb[data-action="toggle-row"]');
  let stage = 0, wait = 0; const t0 = Date.now();
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (stage === 0 && q('[data-action="update-now"]:not([disabled])')) {
      const row = q(ROW);
      expect('the Fedora 45 row is in the System section', !!row, 'no .row.release');
      if (!row) { clearInterval(iv); log('DRIVER DONE'); return; }
      expect('row reads "Fedora 45" and "44 → 45"', txt(ROW + ' .name') === 'Fedora 45' && txt(ROW + ' .ver') === '44 → 45', txt(ROW + ' .hd'));
      expect('yellow dot + REBOOT badge', !!q(ROW + ' .risk.yellow') && txt(ROW + ' .badge') === 'REBOOT', txt(ROW + ' .badge'));
      expect('verdict names the repo without Fedora 45', /someone\/tool/.test(txt(ROW + ' .verdict')), txt(ROW + ' .verdict'));
      expect('size column is blank', txt(ROW + ' .size') === '', txt(ROW + ' .size'));
      expect('unticked by default', !cb().classList.contains('on') && !sel().has('release:45'), count());
      const before = count();
      expect('the row is the last non-routine row of the section', (() => {
        const rows = qa('.card[data-group="dnf"] > .row');
        const i = rows.indexOf(row);
        return i >= 0 && rows.slice(i + 1).every(r => r.classList.contains('routine'));
      })(), 'position');
      log('header before: ' + before + ' selected, sub="' + txt('#brand-sub') + '"');
      shot('release-row'); stage = 1; wait = 8;
    } else if (stage === 1) {
      // bulk selectors leave it alone
      q('[data-action="select-all-global"]').click();   // all off
      q('[data-action="select-all-global"]').click();   // all on
      expect('Select all does not tick the release', !sel().has('release:45') && q('.summary .selall .cb.on'), 'selall=' + (q('.summary .selall .cb') || {}).className);
      q('[data-action="select-safe"]').click();
      expect('Safe only does not tick the release', !sel().has('release:45'));
      q('[data-action="select-security"]').click();
      expect('Security only does not tick the release', !sel().has('release:45'));
      q('[data-action="select-group"][data-group="dnf"]').click();
      q('[data-action="select-group"][data-group="dnf"]').click();
      expect('the System "Select all" does not tick the release', !sel().has('release:45'));
      expect('no banner while unticked', !q(ROW + ' + .banner'), 'banner present');
      q('[data-action="select-all-global"]').click();
      if (!q('.summary .selall .cb.on')) q('[data-action="select-all-global"]').click();
      stage = 2;
    } else if (stage === 2) {
      cb().click();
      const banner = q(ROW + ' + .banner.yellow');
      expect('ticking it shows the runs-last banner', !!banner && /runs last/.test(banner.textContent), banner ? banner.textContent : 'none');
      expect('REBOOT REQUIRED chip appears', /REBOOT REQUIRED/.test(txt('.summary')), txt('.summary'));
      expect('header count includes it', sel().has('release:45'), count());
      q(ROW).click();
      expect('details open with notes + why caution', /Fedora 45 is out/.test(txt(ROW + ' + .expand .notes')) && /WHY CAUTION/.test(txt(ROW + ' + .expand')) && /full changelog/.test(txt(ROW + ' + .expand')), txt(ROW + ' + .expand'));
      shot('release-ticked'); stage = 3; wait = 8;
    } else if (stage === 3) {
      q(ROW).click();                                   // close the details
      q('[data-action="update-now"]').click(); log('clicked Update now'); stage = 4;
    } else if (stage === 4 && q('.applying')) {
      const labels = qa('.applying .step .t').map(e => e.textContent.replace('…', ''));
      expect('the progress list ends with "Download Fedora 45"', labels[labels.length - 1] === 'Download Fedora 45', labels.join('|'));
      // the sample selection has kernel + akmod-nvidia too, whose note outranks the release one
      expect('the note is the akmod one (it outranks the release note) or the release one', /NVIDIA module for the new kernel|Nothing restarts until you say so/.test(txt('.applying-note')), txt('.applying-note'));
      stage = 5;
    } else if (stage === 5 && q('.applying .step.release.active')) {
      shot('release-applying'); stage = 6; wait = 6;
    } else if (stage >= 4 && stage <= 6 && q('.hero.result')) {
      log('done view: h1="' + txt('main h1') + '" cause="' + txt('.hero .cause') + '"');
      log('reboot="' + txt('.hero .rebootline') + '"');
      const btns = qa('.hero .actions .btn').map(b => b.textContent);
      expect('title says ready for Fedora 45', txt('main h1') === 'Ready for Fedora 45', txt('main h1'));
      expect('reboot line explains the installer', /Fedora 45 is downloaded \(3\.1 GiB\)\. Restart to install it/.test(txt('.hero .rebootline')) && /NVIDIA module rebuilds/.test(txt('.hero .rebootline')), txt('.hero .rebootline'));
      expect('Restart now carries the release, Later is there', !!q('[data-action="restart-now"][data-release="45"]') && btns.includes('Later'), btns.join('|'));
      expect('restart not blocked', !q('[data-action="restart-now"][disabled]'));
      expect('tally counts the packages', /packages installed/.test(txt('.hero .tally')), txt('.hero .tally'));
      shot('release-done'); stage = 7; wait = 8;
    } else if (stage === 7) {
      q('[data-action="restart-later"]').click(); stage = 8; log('clicked Later');
    } else if (stage === 8 && q('[data-action="update-now"]')) {
      log('back on updates: rows=' + qa('.row').length + ' selected=' + count());
      clearInterval(iv); log('DRIVER DONE');
    }
    if (Date.now() - t0 > 60000) { log('DRIVER TIMEOUT at stage ' + stage); clearInterval(iv); }
  }, 200);
})();
