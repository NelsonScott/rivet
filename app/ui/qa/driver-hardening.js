/* Driver for app/ui/qa/e2e-hardening.sh: the 2026-10-04 UI hardening, inside the real WebKit
 * page (RIVET_QA_JS), against the fake backends (collect -> sample-state.json in 1.5 s,
 * apply scenario "fail" so Fix it is offered, doctor --fake ok). Every "expect" line carries
 * its own ok/FAIL; the runner fails the run on any FAIL or a missing DRIVER DONE.
 *
 *  F1 render()/receive()/click guards: a throwing view renderer shows the crash hero with a
 *     way out, and a bad message does not kill the queue
 *  F2 a "checking" document with groups keeps the list (selection, scroll, buttons) + bar
 *  F3 the skeleton grows a "Check for updates" button after CHECK_BUTTON_MS; a checking stub
 *     older than STALE_MS is stale: previous groups if any, else the "never finished" hero
 *  F4 a state push never replaces the doctor view
 *  F5 Check for updates (menu, R) is refused while applying / while the doctor works; Update
 *     now sends exactly one apply (the runner counts "apply:" in the app log)
 *  F6 Inter is served from fonts/Inter.woff2 (the client actually requested it) and the
 *     Google stylesheet is not on the render path */
(function () {
  const log = (m) => console.log('[QA] hardening: ' + m);
  let fails = 0;
  const expect = (what, ok, detail) => { if (!ok) fails++; log((ok ? 'ok   ' : 'FAIL ') + what + (detail !== undefined ? ' -> ' + detail : '')); };
  const U = window.rivet, S = U.state;
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const sub = () => txt('#brand-sub');
  const count = () => (q('[data-action="update-now"] .count') || {}).textContent;
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const keyR = () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', bubbles: true, cancelable: true }));
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const menuCheck = () => q('#menu [data-action="check-now"]');
  let doc = null, stage = 0, wait = 0, savedCount = null, origUpdates = null; const t0 = Date.now();
  const minsAgo = (m) => new Date(Date.now() - m * 60000).toISOString();

  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    try { step(); } catch (e) { fails++; log('FAIL driver threw at stage ' + stage + ': ' + (e.stack || e)); clearInterval(iv); log('DRIVER DONE'); }
    if (Date.now() - t0 > 110000) { log('DRIVER TIMEOUT at stage ' + stage + ' view=' + S.view + ' sub="' + sub() + '"'); clearInterval(iv); }
  }, 200);

  function step() {
    if (stage === 0 && q('[data-action="update-now"]:not([disabled])')) {
      doc = clone(S.doc);
      log('updates view: sub="' + sub() + '" selected=' + count());
      // F6 fonts
      const faces = [...document.fonts].map(f => f.family.replace(/"/g, '') + ':' + f.status);
      log('fonts: ' + faces.join(' ') + ' | body font-family="' + getComputedStyle(document.body).fontFamily + '"');
      const inter = [...document.fonts].find(f => /Inter/.test(f.family));
      expect('F6 Inter face is declared and loaded from the app', !!inter && inter.status === 'loaded', inter && inter.status);
      // file:// subresources never show in resource timing, so prove the source another way:
      // the only Inter @font-face comes from style.css and points at fonts/Inter.woff2 (the
      // runner also confirms no Inter is installed system-wide)
      const faceRules = [...document.styleSheets].flatMap(ss => { try { return [...ss.cssRules]; } catch (e) { return []; } })
        .filter(r => r instanceof CSSFontFaceRule).map(r => r.style.getPropertyValue('font-family') + ' <- ' + r.style.getPropertyValue('src'));
      const interRules = faceRules.filter(r => /Inter/.test(r));
      expect('F6 the only Inter @font-face is the bundled fonts/Inter.woff2', interRules.length === 1 && /fonts\/Inter\.woff2/.test(interRules[0]), interRules.join(' | '));
      const remote = qa('link[href^="http"]');
      expect('F6 no remote stylesheet or font link (no Google Fonts)', remote.length === 0, remote.map(l => l.href.slice(0, 60)).join(' | '));
      const manRules = faceRules.filter(r => /Manrope/.test(r));
      expect('F6 the only Manrope @font-face is the bundled fonts/Manrope.woff2', manRules.length === 1 && /fonts\/Manrope\.woff2/.test(manRules[0]), manRules.join(' | '));
      // F2: scroll + change the selection, then an hourly "checking" stub with groups
      const list = q('#list'); list.scrollTop = 400;
      const row = q('.cb.on[data-action="toggle-row"]'); row.click();
      savedCount = count();
      log('before the check: scrollTop=' + Math.round(q('#list').scrollTop) + ' selected=' + savedCount);
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'checking', generated_at: new Date().toISOString() }) });
      expect('F2 view stays "updates" while checking with groups', S.view === 'updates', S.view);
      expect('F2 indeterminate bar shown', !!q('.progress.indet.checking'));
      expect('F2 header says checking', /checking/i.test(sub()), sub());
      expect('F2 scroll position kept', Math.abs(q('#list').scrollTop - 400) < 2, Math.round(q('#list').scrollTop));
      expect('F2 selection kept', count() === savedCount, count() + ' vs ' + savedCount);
      expect('F2 header buttons kept', !!q('[data-action="update-now"]') && !!q('[data-action="select-safe"]'));
      expect('F2 menu Check for updates enabled during a plain check', menuCheck() && !menuCheck().disabled);
      shot('hardening-checking-with-list');
      stage = 1; wait = 2;
    } else if (stage === 1) {
      U.receive({ type: 'state', state: clone(doc) });
      expect('F2 bar gone once the check reports', !q('.progress.checking') && S.view === 'updates' && /checked/.test(sub()), sub());
      // F3a: stale stub, nothing to show
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'checking', generated_at: minsAgo(20), groups: [], summary: { total: 0, total_bytes: 0 } }) });
      expect('F3 stale checking stub with no groups -> "never finished" hero', S.view === 'error' && !!q('[data-stale]'), S.view + ' h1="' + txt('main h1') + '"');
      expect('F3 ...with a Check for updates button', /Check for updates/.test(txt('[data-stale] .actions')));
      expect('F3 ...and the header has Check now', !!q('#header-actions [data-action="check-now"]'));
      shot('hardening-stale-check');
      stage = 1.5; wait = 8;
    } else if (stage === 1.5) {
      // F3b: stale stub WITH groups -> the list, flagged
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'checking', generated_at: minsAgo(20) }) });
      expect('F3 stale checking stub with groups -> list stays, flagged in the header', S.view === 'updates' && /did not finish/.test(sub()) && !q('.progress.checking'), S.view + ' "' + sub() + '"');
      // F3c: fresh stub, nothing to show -> skeleton; the button appears after CHECK_BUTTON_MS
      U.config.CHECK_BUTTON_MS = 1500;
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'checking', generated_at: new Date().toISOString(), groups: [], summary: { total: 0, total_bytes: 0 } }) });
      expect('F3 fresh checking stub with nothing to show -> skeleton', S.view === 'loading' && !!q('.skel-row'), S.view);
      expect('F3 no escape button yet', !q('.slow-check') && !q('#header-actions [data-action="check-now"]'));
      stage = 2; wait = 13;   // 2.6 s
    } else if (stage === 2) {
      expect('F3 after CHECK_BUTTON_MS the skeleton offers Check for updates', !!q('.slow-check [data-action="check-now"]') && !!q('#header-actions [data-action="check-now"]'), txt('.slow-check'));
      expect('F3 header says it is taking long', /longer than usual/.test(sub()), sub());
      shot('hardening-slow-check');
      stage = 2.5; wait = 8;
    } else if (stage === 2.5) {
      U.config.CHECK_BUTTON_MS = 90000;
      q('.slow-check [data-action="check-now"]').click();
      log('clicked the escape-hatch button -> view=' + S.view);
      stage = 3; wait = 0;
    } else if (stage === 3 && S.view === 'updates' && !S.checking && q('[data-action="update-now"]')) {   // after the escape-hatch check
      expect('F3 the button started a real check; the list is back', /checked/.test(sub()), sub());
      // F1: a throwing renderer
      origUpdates = U.views.updates;
      U.views.updates = () => { throw new Error('QA boom: renderer exploded'); };
      U.render();
      expect('F1 throwing renderer -> crash hero', !!q('[data-crash]') && /hit a bug/.test(txt('main h1')), txt('main h1'));
      expect('F1 crash hero shows the error', /QA boom/.test(txt('.crash-msg')), txt('.crash-msg'));
      expect('F1 crash hero has a Check for updates button', /Check for updates/.test(txt('[data-crash] .actions')));
      expect('F1 header updated consistently (no stale buttons)', !q('#header-actions .btn') && /went wrong/.test(sub()), sub());
      shot('hardening-crash');
      stage = 3.5; wait = 8;
    } else if (stage === 3.5) {
      U.views.updates = origUpdates;
      // F1: a message whose handler throws must not kill the queue
      // a settings object whose getter throws: onSettings dies before touching any state
      U.receive({ type: 'settings', settings: { get theme() { throw new Error('QA bad settings'); } } });
      expect('F1 a throwing message handler is contained (toast, no exception)', !!q('.toast') && /bug handling a settings/.test(txt('.toast')), txt('.toast'));
      U.receive({ type: 'state', state: clone(doc) });
      expect('F1 the next message is handled normally', S.view === 'updates' && !!q('[data-action="update-now"]') && !q('[data-crash]'), S.view);
      q('[data-crash] .actions .btn, [data-action="update-now"]');   // no-op: both paths lead back to the list
      stage = 4; wait = 2;
    } else if (stage === 4) {
      // F5: Update now once (a second click must be a no-op), then refuse check-now while applying
      const b = q('[data-action="update-now"]:not([disabled])');
      b.click();
      (q('[data-action="update-now"]') || b).click();                 // the double-click
      log('clicked Update now twice -> view=' + S.view);
      expect('F5 applying after Update now', S.view === 'applying' && !!S.apply);
      keyR();
      expect('F5 R shortcut ignored while applying', S.view === 'applying' && !!S.apply, S.view);
      expect('F5 menu Check for updates disabled while applying', menuCheck().disabled === true);
      menuCheck().click();
      expect('F5 clicking the disabled menu item changes nothing', S.view === 'applying' && !!S.apply, S.view);
      stage = 5;
    } else if (stage === 5 && q('.hero.result') && q('[data-action="fix-it"]')) {
      log('failed result: h1="' + txt('main h1') + '"');
      expect('F5 menu Check for updates enabled again on the result screen', menuCheck().disabled === false);
      q('[data-action="fix-it"]').click();
      expect('F4 doctor view after Fix it', S.view === 'doctor' && S.doctor && S.doctor.phase === 'working', S.view);
      stage = 6; wait = 3;
    } else if (stage === 6) {
      const lines = S.doctor.log.length;
      U.receive({ type: 'state', state: clone(doc) });
      expect('F4 a state push while the doctor works does not replace the view', S.view === 'doctor' && S.doctor && S.doctor.log.length >= lines, S.view);
      keyR();
      expect('F5 R shortcut ignored while the doctor works', S.view === 'doctor' && !!S.doctor, S.view);
      expect('F5 menu Check for updates disabled while the doctor works', menuCheck().disabled === true);
      q('[data-action="fix-it"]');   // not on screen while working; a second Fix it is impossible
      stage = 7;
    } else if (stage === 7 && q('.doctor-result[data-result="diagnosis"]')) {
      log('diagnosis: h1="' + txt('main h1') + '" sub="' + sub() + '"');
      expect('F5 menu Check for updates enabled on the diagnosis', menuCheck().disabled === false);
      q('.doctor-result [data-action="check-now"]').click();
      expect('F3 Check again after a failed run shows the skeleton, not the pre-apply list', S.view === 'loading', S.view);
      stage = 8;
    } else if (stage === 8 && S.view === 'updates' && q('[data-action="update-now"]')) {
      log('back on updates: sub="' + sub() + '" rows=' + qa('.row').length + ' selected=' + count());
      const BUSY = 'Not now: another operation is still running. Wait for it to finish.';
      // A1: "Update now" answered with a busy refusal before any progress event
      S.view = 'applying'; S.apply = { steps: [], byId: {}, log: [], result: null, failed: false, planned: [], drawer: true }; U.render();
      U.receive({ type: 'error', msg: BUSY });
      expect('A1 apply refused before it started -> back on the list with the message', S.view === 'updates' && S.apply === null && /Not now/.test(txt('.toast')) && !!q('[data-action="update-now"]:not([disabled])'), S.view + ' toast="' + txt('.toast') + '"');
      // A3: a held refusal (synthetic done/fail, result refused) must not strand the user
      const ref = (S.doc.groups.find(g => g.id === 'held') || { items: [{}] }).items[0].ref || 'org.freedesktop.Platform.VulkanLayer.gamescope/x86_64/25.08';
      U.receive({ type: 'held', event: { ts: null, step: 'done', status: 'fail', pct: 100, msg: BUSY, log: null, summary: BUSY, cause: BUSY, synthetic: true, held: { action: 'try', result: 'refused', ref } } });
      expect('A3 held refusal -> "Not now" result with a Done button', S.view === 'held' && q('.held-result') && q('.held-result').dataset.result === 'refused' && /Not now/.test(txt('.held-result h1')) && !!q('[data-action="held-done"]'), S.view + ' h1="' + txt('main h1') + '"');
      q('[data-action="held-done"]').click();
      expect('A3 Done leaves the held screen', S.view !== 'held' && S.held === null, S.view);
      // A4: a doctor refusal (synthetic done/fail with cause) -> the no-diagnosis screen with a way out
      U.receive({ type: 'doctor', mode: 'diagnose', event: { step: 'done', status: 'fail', pct: 100, msg: BUSY, summary: BUSY, cause: 'Not now: the doctor is still busy with the fix. Wait for it to finish.', synthetic: true, diagnosis: null, fix_path: null } });
      expect('A4 doctor refusal -> "Could not get a diagnosis" with the cause and Back to updates', S.view === 'doctor' && q('.doctor-result[data-result="nodiag"]') && /still busy/.test(txt('.hero .cause')) && !!q('[data-action="back-to-list"]'), S.view + ' cause="' + txt('.hero .cause') + '"');
      q('[data-action="back-to-list"]').click();
      expect('A2 Back to updates from the doctor view returns to the list (and sends leaveDoctor: see the app log)', S.view === 'updates' && S.doctor === null, S.view);
      stage = 9; wait = 10;   // let the collector push that the refresh above started settle
    } else if (stage === 9) {
      expect('still on the list, idle', S.view === 'updates' || S.view === 'loading', S.view);
      // A5: the collector was killed mid-check: status error / "interrupted", previous groups carried
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'error', error: 'interrupted', generated_at: new Date().toISOString() }) });
      expect('A5 interrupted check with groups -> the carried list stays, flagged', S.view === 'updates' && /did not finish/.test(sub()) && !!q('[data-action="update-now"]'), S.view + ' "' + sub() + '"');
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'error', error: 'interrupted', generated_at: new Date().toISOString(), groups: [], summary: { total: 0, total_bytes: 0 } }) });
      expect('A5 interrupted check with nothing carried -> "did not finish. Check again." hero with the button', S.view === 'error' && q('[data-interrupted="1"]') && /did not finish\. Check again\./.test(txt('main h1')) && /Check for updates/.test(txt('.hero .actions')), S.view + ' h1="' + txt('main h1') + '"');
      U.receive({ type: 'state', state: Object.assign(clone(doc), { status: 'error', error: 'dnf5 exploded: no such repo', generated_at: new Date().toISOString(), groups: [], summary: { total: 0, total_bytes: 0 } }) });
      expect('A5 a real collector error still shows the broken-check hero', S.view === 'error' && !q('[data-stale]') && /Could not check/.test(txt('main h1')) && /dnf5 exploded/.test(txt('.hero p')), txt('main h1'));
      U.receive({ type: 'state', state: clone(doc) });
      expect('back on the list', S.view === 'updates', S.view);
      clearInterval(iv);
      log(fails ? fails + ' expectation(s) FAILED' : 'all expectations ok');
      log('DRIVER DONE');
    }
  }
})();
