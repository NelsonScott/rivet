/* Driver for app/qa-render.sh (runs inside the real WebKit page via RIVET_QA_JS).
 * Forces the light theme (the window locator looks for the brightest window on screen),
 * then takes a WebKit-side snapshot at three scroll offsets of the tall list. qa-render.sh
 * screenshots the composited screen at the same moments and compares the two. */
(function () {
  const log = (m) => console.log('[QA] render: ' + m);
  const q = (s) => document.querySelector(s);
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  let stage = 0, wait = 0;
  setInterval(() => {
    if (wait > 0) { wait--; return; }
    const list = q('#list');
    if (stage === 0 && q('[data-action="update-now"]:not([disabled])') && list) {
      if (document.documentElement.dataset.theme !== 'light') { q('[data-action="toggle-theme"]').click(); wait = 2; return; }
      log('READY1 rows=' + document.querySelectorAll('.row').length + ' scrollHeight=' + list.scrollHeight + ' clientHeight=' + list.clientHeight);
      shot('wk-1'); stage = 1; wait = 5;
    } else if (stage === 1) {
      list.scrollTop = Math.floor(list.scrollHeight * 0.25); log('READY2 scrollTop=' + list.scrollTop); shot('wk-2'); stage = 2; wait = 5;
    } else if (stage === 2) {
      list.scrollTop = Math.floor(list.scrollHeight * 0.6); log('READY3 scrollTop=' + list.scrollTop); shot('wk-3'); stage = 3; wait = 5;
    } else if (stage === 3) { log('DRIVER DONE'); stage = 4; }
  }, 1000);
})();
