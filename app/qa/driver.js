/* Driver for qa-e2e.sh: runs inside the real WebKit page (RIVET_QA_JS).
 * Logs go to stdout through enable-write-console-messages-to-stdout.
 * qa-e2e.sh prepends `const SCENARIO = "ok|warn|fail|crash";`.
 *
 * The "ok" run also checks the System section's clustering (2026-10-04): one row per
 * advisory / package family, routine clusters behind one accordion row, a cluster
 * checkbox toggling every member. The selection is read from window.rivet.state
 * (members of a collapsed cluster have no checkbox in the DOM). */
(function () {
  const S = (typeof SCENARIO === 'string') ? SCENARIO : 'ok';
  const log = (m) => console.log('[QA] ' + S + ': ' + m);
  const sub = () => document.getElementById('brand-sub').textContent;
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const count = () => (q('[data-action="update-now"] .count') || {}).textContent;
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const leave = () => q('[data-action="restart-later"]') || q('[data-action="back-to-list"]');
  const selected = () => [...window.rivet.state.selected].sort();
  const expect = (what, ok, got) => log((ok ? 'PASS ' : 'FAIL ') + what + (ok ? '' : ' (got: ' + got + ')'));
  const dnfItems = () => ((window.rivet.state.doc.groups.find(g => g.id === 'dnf') || {}).items || []);
  let stage = 0, wait = 0; const t0 = Date.now();
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (stage === 0 && q('[data-action="update-now"]:not([disabled])')) {
      log('updates view: ' + sub() + ' default-selected=' + count());
      if (S !== 'ok') { q('[data-action="update-now"]').click(); stage = 1; log('clicked Update now'); return; }
      // clusters: fewer rows than packages, the title says so, routine accordion collapsed
      const rows = qa('.card[data-group="dnf"] .row').length, items = dnfItems().length;
      log('dnf: ' + items + ' packages in ' + rows + ' rows, title="' + txt('.group-title') + '"');
      expect('dnf section shows fewer rows than packages', rows < items, rows + ' rows / ' + items + ' packages');
      expect('routine accordion present and collapsed', !!q('.row.routine') && !q('.routine-body'), !!q('.row.routine') + ' ' + !!q('.routine-body'));
      expect('menu has Security only / Safe only', !!q('#menu [data-action="select-security"]') && !!q('#menu [data-action="select-safe"]'));
      shot('clusters'); stage = 10; wait = 10;
    } else if (stage === 10) {
      // open the accordion, then the package list of the first multi-package cluster
      q('.row.routine').click();
      expect('routine accordion opens on click', !!q('.routine-body') && qa('.routine-body .row').length > 0, qa('.routine-body .row').length);
      const cl = q('.row.cluster');
      if (!cl) { expect('a multi-package cluster exists', false, 'none'); stage = 12; return; }
      cl.querySelector('[data-action="expand-cluster"]').click();
      const members = qa('.row.cluster.list-open + .members .member');
      log('cluster "' + txt('.row.cluster .name') + '" ' + txt('.row.cluster .pkgs') + ' -> ' + members.length + ' member rows, ' + members.filter(m => m.querySelector('.cb.on')).length + ' on');
      expect('cluster expands to its members', members.length > 1 && members.length === Number(cl.dataset.members), members.length + ' vs ' + cl.dataset.members);
      shot('clusters-open'); stage = 11; wait = 10;
    } else if (stage === 11) {
      // the cluster checkbox toggles every member (tri-state: mixed -> all on -> all off)
      // every click re-renders the list, so the row is looked up afresh each time
      const cb = () => q('.row.cluster .cb[data-action="toggle-row"]');
      const ids = () => qa('.row.cluster.list-open + .members .member .cb').map(e => e.dataset.id);
      const onNow = () => ids().filter(id => window.rivet.state.selected.has(id)).length;
      const n = ids().length, before = onNow();
      cb().click();
      const after1 = onNow();
      cb().click();
      const after2 = onNow();
      log('cluster checkbox: ' + before + '/' + n + ' on -> ' + after1 + '/' + n + ' -> ' + after2 + '/' + n + ' (header ' + count() + ')');
      expect('cluster checkbox toggles all members', (before === n ? after1 === 0 && after2 === n : after1 === n && after2 === 0), before + '->' + after1 + '->' + after2);
      q('.row.routine').click();
      expect('routine accordion closes again', !q('.routine-body'));
      stage = 12;
    } else if (stage === 12) {
      q('[data-action="select-security"]').click();
      const vis = qa('.cb.on[data-action="toggle-row"]').map(e => e.dataset.id);
      log('security-only -> ' + count() + ' selected: ' + selected().join(' ') + ' (visible checkboxes on: ' + vis.join(' ') + ')');
      q('[data-action="select-safe"]').click();
      log('safe-only -> ' + count() + ' selected');
      q('[data-action="update-now"]').click(); stage = 1; log('clicked Update now');
    } else if (stage === 1 && q('.hero.result') && leave()) {
      log('done view: h1="' + txt('main h1') + '" tone=' + (q('.hero.result').className.replace('hero result', '').trim() || 'ok') + ' sub="' + sub() + '"');
      log('cause="' + txt('.hero .cause') + '"');
      log('tally="' + txt('.hero .tally') + '" missing="' + txt('.hero .missing') + '"');
      log('reboot="' + txt('.hero .rebootline') + '" buttons=' + [...document.querySelectorAll('.hero .actions .btn')].map(b => b.textContent).join('|'));
      if (S === 'ok') { stage = 3; return; }
      log('details expander present=' + !!q('[data-action="toggle-details"]') + ' open=' + !!q('.details.open'));
      q('[data-action="toggle-details"]').click(); stage = 2; wait = 3;
    } else if (stage === 2) {
      const lines = [...document.querySelectorAll('.details .log > div')].map(d => d.textContent);
      log('details open: ' + lines.length + ' log lines, first-error="' + txt('.details .firsterr') + '"');
      log('log tail: "' + (lines[lines.length - 1] || '').slice(0, 160) + '"');
      log('log path: "' + txt('.details .logpath') + '"');
      shot(S + '-details'); stage = 3; wait = 8;
    } else if (stage === 3) {
      if (S !== 'ok') { q('[data-action="toggle-details"]').click(); shot(S); stage = 4; wait = 8; return; }
      stage = 4;
    } else if (stage === 4) {
      const b = leave(); const label = b.textContent; b.click(); stage = 5;
      log('clicked ' + label + ' -> sub="' + sub() + '"');
    } else if (stage === 5 && q('[data-action="update-now"]')) {
      log('back on updates: sub="' + sub() + '" rows=' + document.querySelectorAll('.row').length + ' selected=' + count());
      stage = 6; clearInterval(iv); log('DRIVER DONE');
    }
    if (Date.now() - t0 > 40000) { log('DRIVER TIMEOUT at stage ' + stage + ' sub="' + sub() + '"'); clearInterval(iv); }
  }, 200);
})();
