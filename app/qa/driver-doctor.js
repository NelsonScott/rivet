/* Driver for qa-e2e.sh, scenario "doctor": a failed update -> Fix it -> Claude's
 * diagnosis (fake, no model call) -> Run the fix (fake, touches nothing) -> Fixed ->
 * Check again -> back on the list. Runs inside the real WebKit page (RIVET_QA_JS). */
(function () {
  const S = (typeof SCENARIO === 'string') ? SCENARIO : 'doctor';
  const log = (m) => console.log('[QA] ' + S + ': ' + m);
  const sub = () => document.getElementById('brand-sub').textContent;
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const btns = () => qa('.hero .actions .btn').map(b => b.textContent + (b.disabled ? '(disabled)' : '')).join('|');
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  let stage = 0, wait = 0, sawWorking = false, sawFixing = false; const t0 = Date.now();
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (/looking into it/.test(sub())) sawWorking = true;
    if (/running the fix/.test(sub())) sawFixing = true;
    if (stage === 0 && q('[data-action="update-now"]:not([disabled])')) {
      q('[data-action="update-now"]').click(); stage = 1; log('clicked Update now');
    } else if (stage === 1 && q('.hero.result') && q('[data-action="fix-it"]')) {
      log('failed result: h1="' + txt('main h1') + '" buttons=' + btns());
      q('[data-action="fix-it"]').click(); stage = 2; log('clicked Fix it -> sub="' + sub() + '"');
    } else if (stage === 2 && q('.doctor-result[data-result="diagnosis"]')) {
      log('diagnosis: saw working screen=' + sawWorking + ' h1="' + txt('main h1') + '" sub="' + sub() + '"');
      log('what happened="' + txt('.hero .cause').slice(0, 120) + '…"');
      log('reboot="' + txt('.hero .rebootline') + '"');
      log('fix="' + txt('.fixcard .fx-h') + '" commands=' + qa('.fixcard pre.cmds').length + ' lines=' + (txt('.fixcard pre.cmds').match(/sudo/g) || []).length + ' buttons=' + btns());
      q('[data-action="toggle-doctor-details"]').click(); stage = S === 'recheck' ? 20 : 3; wait = 3;
    } else if (stage === 20 && q('.doctor-result[data-result="diagnosis"] [data-action="restart-now"]')) {
      log('recheck: Restart offered. lines="' + qa('.hero .rebootline').map(e => e.textContent.trim()).join(' | ') + '" buttons=' + btns());
      shot('doctor-recheck'); stage = 21; wait = 8;
    } else if (stage === 21) {
      q('[data-action="back-to-list"]').click(); stage = 8; log('clicked Back to updates (never Restart) -> sub="' + sub() + '"');
    } else if (stage === 3) {
      log('details: evidence items=' + qa('.details ul.evidence li').length + ' log lines=' + qa('.details .log > div').length
        + ' denied line present=' + qa('.details .log > div').some(d => /not allowed/.test(d.textContent)));
      shot('doctor-diagnosis'); stage = 4; wait = 8;
    } else if (stage === 4) {
      q('[data-action="run-fix"]').click(); stage = 5; log('clicked Run the fix -> sub="' + sub() + '"');
    } else if (stage === 5 && q('.doctor-result[data-result="fixed"], .doctor-result[data-result="fixfail"]')) {
      log('after fix: saw fixing screen=' + sawFixing + ' result=' + q('.doctor-result').dataset.result + ' h1="' + txt('main h1') + '" sub="' + sub() + '"');
      log('summary="' + txt('.hero .cause') + '" tally="' + txt('.hero .tally') + '" buttons=' + btns());
      q('[data-action="toggle-doctor-details"]').click(); stage = 6; wait = 3;
    } else if (stage === 6) {
      log('fix log lines=' + qa('.details .log > div').length + ' last="' + (qa('.details .log > div').slice(-1)[0] || {}).textContent + '"');
      shot('doctor-fixed'); stage = 7; wait = 8;
    } else if (stage === 7) {
      q('[data-action="check-now"]').click(); stage = 8; log('clicked Check again -> sub="' + sub() + '"');
    } else if (stage === 8 && q('[data-action="update-now"]')) {
      log('back on updates: sub="' + sub() + '" rows=' + qa('.row').length);
      stage = 9; clearInterval(iv); log('DRIVER DONE');
    }
    if (Date.now() - t0 > 65000) { log('DRIVER TIMEOUT at stage ' + stage + ' sub="' + sub() + '"'); clearInterval(iv); }
  }, 200);
})();
