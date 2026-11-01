/* Driver for qa-e2e.sh, the Looks scenarios (2026-10-31). Runs inside the real WebKit page; it only
 * changes appearance settings (look, theme, type size), never presses Update / Install.
 *   looks       sample-state.json: default look is Periwinkle; every look x light/dark is picked through
 *               Preferences > Look (the real tiles), shot on the updates list, the "nothing pending" front
 *               page (status strip + groups; 2 held + 16 other) and scrolled to Other software; the picker
 *               itself per look; g y cycles; Comfortable type size zooms; settings echo back from the host.
 *   looks-real  the same front-page shots from THIS box's real state.json (copied in, nothing pressed).
 * In every state it also measures the text contrast the renderer actually painted (each visible text
 * element against the first opaque background behind it) and logs anything under 4.5:1, so a colour
 * still hardcoded for Periwinkle shows up as a failure in the other looks.
 * Prints [QA] lines; "DRIVER DONE" when every check passed, "DRIVER FAILED" otherwise. */
(function () {
  const SC = typeof SCENARIO !== 'undefined' ? SCENARIO : 'looks';
  const REAL = SC === 'looks-real';
  const log = (m) => console.log('[QA] ' + SC + ': ' + m);
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const shot = (name) => window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({ cmd: 'qaShot', name }));
  const html = document.documentElement;
  const look = () => html.getAttribute('data-look');
  const theme = () => html.getAttribute('data-theme');
  const st = () => window.rivet.state;
  const lister = () => q('#list') || q('.empty-held');
  const LOOK_IDS = ['periwinkle', 'nord', 'poster', 'workshop', 'terminal'];
  let wait = 0, bad = 0, lowTotal = 0, lowNew = 0; const t0 = Date.now();
  const LIMIT_MS = 290000;
  const check = (cond, what) => { if (!cond) { bad++; log('CHECK FAILED: ' + what); } else log('ok: ' + what); };
  const steps = [];
  const then = (fn, w) => steps.push([fn, w || 3]);
  const until = (pred, what, fn) => steps.push([() => { if (!pred()) return 'again'; if (fn) fn(); }, 1, what]);
  const push = (mut) => { const d = JSON.parse(JSON.stringify(st().doc)); mut(d); window.rivet.receive({ type: 'state', state: d }); };
  const nothingPending = (d) => { d.groups = d.groups.filter(g => g.id === 'held'); d.summary = { total: 0, total_bytes: 0 }; };
  const click = (sel) => { const n = q(sel); if (n) n.click(); return !!n; };

  /* ---- painted contrast ---- */
  const rgba = (s) => { const m = /rgba?\(([^)]+)\)/.exec(s || ''); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (t, u) => [0, 1, 2].map(i => t[i] * t[3] + u[i] * (1 - t[3])).concat([1]);
  const lum = (c) => { const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  function bgBehind(el) {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      let c = rgba(cs.backgroundColor);
      if ((!c || c[3] === 0) && cs.backgroundImage && cs.backgroundImage !== 'none') c = rgba(cs.backgroundImage);   // a gradient: its first stop
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let b = [255, 255, 255, 1];
    for (let i = layers.length - 1; i >= 0; i--) b = over(layers[i], b);
    return b;
  }
  function dimmed(el) { for (let n = el; n && n.nodeType === 1; n = n.parentElement) if (parseFloat(getComputedStyle(n).opacity) < 1) return true; return false; }
  function contrastPass(label) {
    const vw = innerWidth, vh = innerHeight, lows = [];
    for (const el of qa('body *')) {
      if (![...el.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      if (el.closest('[hidden]') || el.closest('button:disabled') || dimmed(el)) continue;     // disabled / blocked rows are exempt
      const fg = rgba(cs.color); if (!fg) continue;
      const bg = bgBehind(el);
      const c = ratio(fg[3] < 1 ? over(fg, bg) : fg, bg);
      const px = parseFloat(cs.fontSize), w = parseInt(cs.fontWeight, 10);
      const need = (px >= 24 || (px >= 18.66 && w >= 700)) ? 3 : 4.5;
      if (c < need) lows.push(c.toFixed(2) + ' ' + (el.className || el.tagName).toString().slice(0, 40) + ' "' + el.textContent.trim().slice(0, 40) + '"');
    }
    lowTotal += lows.length;
    if (!/^periwinkle\//.test(label)) lowNew += lows.length;      // Periwinkle is kept as it was, lows and all
    log('contrast ' + label + ': ' + (lows.length ? lows.length + ' under target' : 'all text >= 4.5:1'));
    lows.slice(0, 12).forEach(x => log('    low ' + x));
  }

  /* ---- picking through the real UI ---- */
  const openPrefs = () => then(() => { if (!q('.look-grid')) click('[data-action="open-settings"]'); }, 3);
  const closePrefs = () => then(() => { if (q('.modal')) click('.modal-h [data-action="close-modal"]'); }, 3);
  function pick(id, th) {
    openPrefs();
    then(() => { check(click(`.look-tile[data-value="${id}"]`), 'Preferences has a tile for ' + id); }, 3);
    then(() => { if (th && theme() !== th) click(`.seg [data-action="set-theme"][data-value="${th}"]`); }, 3);
    until(() => look() === id && st().settings.look === id && (!th || theme() === th), id + ' applied and echoed back by the host');
    closePrefs();
  }
  const fontReady = {};
  function fonts(id) {
    then(() => {
      const fam = { nord: 'Fira Sans', poster: 'Archivo', workshop: 'IBM Plex Sans', terminal: 'JetBrains Mono', periwinkle: 'Manrope' }[id];
      if (fontReady[id] === undefined) fontReady[id] = document.fonts.check('14px "' + fam + '"');
      log('font ' + fam + ' loaded: ' + fontReady[id]);
    }, 1);
  }
  const scrollTop = () => then(() => { const l = lister(); if (l) l.scrollTop = 0; }, 2);
  const scrollOther = () => then(() => { const n = q('.ew-h'), l = lister(); if (n && l) l.scrollTop += n.getBoundingClientRect().top - l.getBoundingClientRect().top - 10; }, 2);

  // the boot state: default look, picker present
  then(() => {
    check(look() === 'periwinkle', 'a fresh profile starts on Periwinkle (data-look=' + look() + ')');
    check(st().settings.look === 'periwinkle' && st().settings.type_size === 'compact', 'settings default look=periwinkle, type_size=compact');
    check(getComputedStyle(q('.header')).backgroundColor === getComputedStyle(document.body).backgroundColor, 'Periwinkle: the header band is the page ground, as before');
  }, 2);

  if (!REAL) {
    // 1. the updates list (dnf + Flatpak pending) in every look, light + dark
    for (const id of LOOK_IDS) for (const th of ['light', 'dark']) {
      pick(id, th); fonts(id);
      scrollTop();
      then(() => contrastPass(id + '/' + th + '/updates'), 1);
      then(() => shot(`looks-${id}-${th}-updates`), 8);
    }
    // 2. the front page with nothing pending: status strip, Held back (2), Other software (16)
    then(() => push(nothingPending), 4);
    until(() => !!q('.status-strip'), 'the nothing-pending front page (status strip)');
  } else {
    then(() => push(nothingPending), 4);
  }
  for (const id of LOOK_IDS) for (const th of ['light', 'dark']) {
    pick(id, th);
    scrollTop();
    then(() => contrastPass(id + '/' + th + '/front'), 1);
    then(() => shot(`${REAL ? 'real' : 'looks'}-${id}-${th}-front`), 8);
    scrollOther();
    then(() => contrastPass(id + '/' + th + '/other'), 1);
    then(() => shot(`${REAL ? 'real' : 'looks'}-${id}-${th}-other`), 8);
  }

  if (!REAL) {
    // 3. the picker itself, one shot per look (dark, then light for the last)
    for (const id of LOOK_IDS) {
      pick(id, 'dark');
      openPrefs();
      then(() => { const n = q('#set-look'); if (n) n.scrollIntoView({ block: 'start' }); }, 2);
      then(() => { check(qa('.look-tile').length === LOOK_IDS.length && q(`.look-tile.on[data-value="${id}"]`), 'picker shows ' + LOOK_IDS.length + ' tiles with ' + id + ' marked in use'); contrastPass(id + '/dark/prefs'); }, 1);
      then(() => shot(`looks-${id}-dark-prefs`), 8);
      closePrefs();
    }
    // 4. g y steps to the next look (terminal -> periwinkle wraps), and a toast says which
    then(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true })); document.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true })); }, 3);
    until(() => look() === 'periwinkle' && st().settings.look === 'periwinkle', 'g y wraps terminal -> periwinkle');
    then(() => { check(/Look: Periwinkle/.test((q('.toast') || {}).textContent || ''), 'a toast names the new look'); document.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true })); document.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true })); }, 3);
    until(() => look() === 'nord', 'g y again -> nord');
    // 5. Comfortable type size: the host zooms the page (fewer CSS px across the same window)
    let w0 = 0;
    then(() => { w0 = innerWidth; openPrefsNow(); }, 3);
    then(() => click('.seg [data-action="set-size"][data-value="comfortable"]'), 4);
    until(() => st().settings.type_size === 'comfortable' && innerWidth < w0 * 0.95, 'Comfortable zooms the page (' + 'innerWidth shrinks)');
    then(() => { log('innerWidth compact=' + w0 + ' comfortable=' + innerWidth); click('.modal-h [data-action="close-modal"]'); }, 3);
    scrollTop();
    then(() => shot('looks-nord-dark-front-comfortable'), 8);
    then(() => { openPrefsNow(); }, 3);
    then(() => click('.seg [data-action="set-size"][data-value="compact"]'), 4);
    until(() => st().settings.type_size === 'compact' && innerWidth >= w0 - 1, 'Compact puts the zoom back');
    then(() => { click('.modal-h [data-action="close-modal"]'); }, 2);
    // leave the profile on Periwinkle, light
    pick('periwinkle', 'light');
  }
  function openPrefsNow() { if (!q('.look-grid')) click('[data-action="open-settings"]'); }

  then(() => { log('painted text under target, all states: ' + lowTotal + ', in the four new looks: ' + lowNew + ' (Periwinkle is kept as it was)'); check(lowNew === 0, 'the four new looks paint every visible text at 4.5:1 or better'); }, 1);

  if (typeof QA_CUT !== 'undefined') { steps.length = QA_CUT; log('cut at ' + QA_CUT + ' steps'); }
  let i = 0, tries = 0, started = false;
  const iv = setInterval(() => {
    if (wait > 0) { wait--; return; }
    if (!started) { if (!q('.ew-row') || !st() || !st().settings) return; started = true; }
    if (i >= steps.length) { clearInterval(iv); log(bad ? bad + ' check(s) failed' : 'all checks passed'); log(bad ? 'DRIVER FAILED' : 'DRIVER DONE'); return; }
    const [fn, w, what] = steps[i];
    let r;
    try { r = fn(); } catch (e) { bad++; log('CHECK FAILED: step ' + i + ' threw ' + e); r = undefined; }
    if (r === 'again') { if (++tries > 75) { bad++; log('CHECK FAILED: waited too long for: ' + what); tries = 0; i++; } return; }
    tries = 0; i++; wait = w;
    if (Date.now() - t0 > LIMIT_MS) { log('DRIVER TIMEOUT at step ' + i); clearInterval(iv); }
  }, 200);
})();
