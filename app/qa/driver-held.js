/* Driver for qa-e2e.sh, scenario "held": the Held back section, a trial that fails and rolls
 * back by itself, then a second trial that the watcher keeps. Runs inside the real WebKit page. */
(function () {
  const log = (m) => console.log('[QA] held: ' + m);
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const rows = () => qa('.held-row').map(r => (r.querySelector('.name').textContent + ' ' + r.querySelector('.ver').textContent
    + ' [' + r.querySelector('.badge').textContent + '] btn=' + ((r.querySelector('.held-btns .btn') || {}).textContent || '-')).replace(/\s+/g, ' '));
  let stage = 0, wait = 0, round = 1, sawRunning = false; const t0 = Date.now();
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    const tryBtn = q('.held-row [data-action="held-try"]');
    if (stage === 0 && tryBtn && q('[data-action="update-now"]')) {
      log('round ' + round + ': section="' + txt('.held-h .group-title') + '" update-now count=' + txt('[data-action="update-now"] .count')
        + ' stat="' + txt('.summary .stat') + '"');
      rows().forEach(r => log('  row: ' + r));
      log('  reason="' + txt('.held-row .held-reason') + '" since="' + txt('.held-row .held-since') + '"');
      log('  held rows with a checkbox: ' + qa('.held-row .cb').length + ' (must be 0)');
      log('  top chip: "' + txt('.held-chip') + '"');
      if (round === 1) { shot('held-list-top'); stage = 0.5; wait = 8; return; }
      q('.held-h').scrollIntoView({ block: 'start' }); shot('held-list-' + round); stage = 1; wait = 8;
    } else if (stage === 0.5) {
      q('.held-chip').click(); stage = 0.6; wait = 3;
    } else if (stage === 0.6) {
      log('  chip click scrolled the list to ' + Math.round(q('#list').scrollTop) + ' px');
      shot('held-list-' + round); stage = 1; wait = 8;
    } else if (stage === 1) {
      q('.held-row [data-action="held-try"]').click(); stage = 2; wait = 2;
    } else if (stage === 2 && q('.modal [data-action="held-confirm"]')) {
      log('confirm: title="' + txt('.modal .t') + '" text="' + txt('.modal .confirm-text') + '"');
      if (round === 1) { shot('held-confirm'); wait = 8; }
      stage = 3;
    } else if (stage === 3) {
      log('clicked "' + txt('.modal [data-action="held-confirm"]') + '"');
      q('.modal [data-action="held-confirm"]').click(); stage = 4;
    } else if (stage === 4 && q('.held-watch')) {
      log('watching: h1="' + txt('.held-watch h1') + '" status="' + txt('#held-status') + '" buttons='
        + qa('.held-watch .actions .btn').map(b => b.textContent).join('|'));
      if (round === 1) { shot('held-watching'); }
      stage = 5;
    } else if (stage === 5) {
      const st = txt('#held-status');
      if (!sawRunning && /is running, \d+ s/.test(st)) { sawRunning = true; log('live status: "' + st + '"'); if (round === 2) shot('held-running'); }
      if (q('.held-result')) {
        log('result: kind=' + q('.held-result').dataset.result + ' h1="' + txt('.held-result h1') + '" summary="' + txt('.held-result .cause') + '"');
        shot(round === 1 ? 'held-rolled-back' : 'held-kept'); stage = 6; wait = 8;
      }
    } else if (stage === 6) {
      q('[data-action="held-done"]').click(); log('clicked Done'); stage = 7; wait = 5;
    } else if (stage === 7 && q('[data-action="update-now"]') && !q('.skel-row')) {
      if (round === 1) { round = 2; sawRunning = false; stage = 0; return; }
      log('after keep: held rows=' + rows().length + ' chip="' + txt('.held-chip') + '"'); rows().forEach(r => log('  row: ' + r));
      q('.held-h').scrollIntoView({ block: 'start' }); shot('held-after'); stage = 8; wait = 10;
    } else if (stage === 8) {
      clearInterval(iv); log('DRIVER DONE');
    }
    if (Date.now() - t0 > 100000) { log('DRIVER TIMEOUT at stage ' + stage + ' round ' + round + ' view=' + window.rivet.state.view); clearInterval(iv); }
  }, 200);
})();
