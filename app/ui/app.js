/* Rivet UI — vanilla-JS state machine driven by the app↔UI bridge in CONTRACT.md.
 *
 * In:  window.rivet.receive({type:"state"|"settings"|"progress"|"held"|"error", ...})
 * Out: window.webkit.messageHandlers.rivet.postMessage(JSON.stringify({cmd, ...}))
 *
 * With ?mock=1 there is no host: sample-state.json is loaded and a fake progress
 * stream is generated locally, so the page works in a plain browser.
 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  const MOCK = params.get('mock') === '1';
  const FORCE_THEME = ['dark', 'light'].includes(params.get('theme')) ? params.get('theme') : null;
  const HOST = (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.rivet) || null;

  /* ------------------------------------------------------------------ */
  /* State                                                               */
  /* ------------------------------------------------------------------ */
  const DEFAULT_SETTINGS = { check_interval_min: 60, notify: true, red_includes_reboot: false, theme: 'system', look: 'periwinkle', type_size: 'compact', show_containers: false, ai_triage: false };

  /* Looks (2026-10-31): style.css is Periwinkle; each other look is ui/looks/<id>.css re-pointing the
     same custom properties under :root[data-look="<id>"]. Light/dark stays separate (data-theme).
     sw = the picker's mini preview per theme: [chrome, ground, card, accent, text] (keep in step with
     the look's CSS; qa/looks-contrast.py checks the CSS itself). Nothing is fetched: a look's face is
     used when it is installed on the system (Fedora packages it), else the stack falls back to the
     bundled Inter. Periwinkle's Manrope is bundled (fonts/Manrope.woff2). */
  const LOOKS = [
    { id: 'periwinkle', name: 'Periwinkle', blurb: 'Today’s look: cool white, periwinkle gradient, Manrope', font: "'Manrope','Inter',sans-serif",
      sw: { light: ['#f5f6fa', '#f5f6fa', '#ffffff', '#5b7cfa', '#171a22'], dark: ['#0b0c10', '#0b0c10', '#14151a', '#6b7cf8', '#eef0f6'] } },
    { id: 'nord', name: 'Nord', blurb: 'Polar night or snow storm, frost blue, three quiet layers, Fira Sans', font: "'Fira Sans','Inter',sans-serif",
      sw: { light: ['#e5e9f0', '#eceff4', '#f9fafc', '#4c6e9a', '#2e3440'], dark: ['#262b35', '#2e3440', '#3b4252', '#88c0d0', '#eceff4'] } },
    { id: 'poster', name: 'Poster', blurb: 'The beaver’s palette: cream, ink band, navy action, red rule, Archivo', font: "'Archivo','Inter',sans-serif",
      sw: { light: ['#171614', '#eee5d1', '#f8f2e4', '#1f3a6e', '#171614'], dark: ['#0b0a09', '#1f1c18', '#29251f', '#3a5fa3', '#f4ebd8'] } },
    { id: 'workshop', name: 'Workshop', blurb: 'Warm greys, oxblood buttons, amber links, IBM Plex', font: "'IBM Plex Sans','Inter',sans-serif",
      sw: { light: ['#e8e2d9', '#f2eee8', '#fbf9f6', '#7e2833', '#24201c'], dark: ['#1c1a18', '#252220', '#2f2b28', '#a33a47', '#f2ebe3'] } },
    { id: 'terminal', name: 'Terminal', blurb: 'Near-black or paper, one indigo, mono metadata, tighter rows', font: "'JetBrains Mono',monospace",
      sw: { light: ['#eeeff2', '#f6f7f9', '#ffffff', '#4f46e5', '#16171c'], dark: ['#0b0c0f', '#101116', '#15171d', '#5b5ee6', '#dadde4'] } },
  ];
  const lookById = (id) => LOOKS.find(l => l.id === id) || LOOKS[0];
  const FORCE_LOOK = LOOKS.some(l => l.id === params.get('look')) ? params.get('look') : null;
  function readLS(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function writeLS(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode, blocked storage */ } }

  const state = {
    doc: null,                  // the state.json document
    settings: Object.assign({}, DEFAULT_SETTINGS),
    view: 'boot',               // boot | updates | loading | empty | applying | done | error | held
    selected: new Set(),
    expanded: new Set(),        // item ids (and cluster ids) whose notes/reasoning are open
    openClusters: new Set(),    // cluster ids whose member list is open
    routineOpen: readLS('rivet.routineOpen') === '1',   // the "Routine updates" accordion
    ewOpen: new Set(),          // Other-software row ids whose details are open (not pruned by state pushes)
    ewRuns: {},                 // Other-software row id -> {phase: running|done, lines, msg, pct, ok, summary, restart}
    ewConfirm: null,            // the row id whose major-update confirmation is open
    ewBulkAsk: null,            // {group, ids}: the "Update the safe ones" confirmation on screen
    ewRemoveAsk: null,          // the row id whose Remove confirmation is on screen (otherRemove)
    ewBatch: null,              // {group, ids, done, stopping}: a bulk run in progress (otherUpdateMany)
    ewHaRestart: null,          // {phase, msg}: rivet-apply --ha-restart running (after an integration install)
    ewRestartAsk: null,         // the row id whose "Restart Home Assistant?" confirm is open
    query: '',
    menuOpen: false,
    modal: null,                // 'settings' | null
    toast: null,
    toastTimer: null,
    apply: null,                // { steps, byId, log, result, failed, planned }
    held: null,                 // a held-back trial in progress, see startHeld()
    doctor: null,               // "Fix it": Claude's diagnosis / the fix run, see newDoctor()
    error: null,
    checking: false,            // doc.status === "checking" while the list stays on screen (hourly check)
    checkingSince: null,        // when this UI first saw the current "checking" document
    checkStartedAt: null,       // when THIS UI asked for a check (the app's stub keeps the old generated_at)
    staleCheck: false,          // a "checking" document nobody is working on any more
    listStale: false,           // the groups on screen predate an apply/trial: skeleton, not the list, while re-checking
    caps: { claude: null },     // 2026-11-01: {"type":"caps"}; claude false = no Claude Code CLI, Fix it is not offered
    health: null,               // 2026-11-01: health.json {checked_at, checks:[{id,title,detail,fix_label}]}
    healthRun: null,            // {id, phase: running|done, ok, summary}: a health fix in progress
  };

  // Tunable so a QA driver can shorten the waits (window.rivet.config).
  const config = {
    CHECK_BUTTON_MS: 90 * 1000,   // "Check for updates" appears on the skeleton after this long
    STALE_MS: 10 * 60 * 1000,     // a checking stub older than this is a dead collector
  };

  /* ------------------------------------------------------------------ */
  /* Bridge                                                              */
  /* ------------------------------------------------------------------ */
  function send(msg) {
    if (HOST) { try { HOST.postMessage(JSON.stringify(msg)); } catch (e) { console.error(e); } return; }
    mockHandle(msg);
  }

  function receive(msg) {
    if (typeof msg === 'string') { try { msg = JSON.parse(msg); } catch (e) { console.error('[rivet] receive: not JSON', e); return; } }
    if (!msg || !msg.type) return;
    // A bug while handling one message must not take the UI down with it: the next
    // message (and the next click) still gets handled.
    try {
      if (msg.type === 'state') onState(msg.state);
      else if (msg.type === 'settings') onSettings(msg.settings);
      else if (msg.type === 'progress') onProgress(msg.event);
      else if (msg.type === 'held') onHeld(msg.event);
      else if (msg.type === 'doctor') onDoctor(msg.mode, msg.event);
      else if (msg.type === 'other') onOther(msg.event);
      else if (msg.type === 'caps') { state.caps = Object.assign({}, state.caps, msg.caps || {}); render(); }
      else if (msg.type === 'health') onHealth(msg);
      else if (msg.type === 'error') onError(msg.msg || 'Unknown error');
    } catch (err) {
      console.error('[rivet] receive(' + msg.type + ') failed: ' + ((err && err.stack) || err));
      state.lastBug = { where: 'receive ' + msg.type, error: String((err && err.message) || err) };
      try { toast('Rivet hit a bug handling a ' + msg.type + ' message (see the console)', false, 6000); } catch (e) { /* render itself is guarded */ }
    }
  }

  function onError(msg) {
    state.error = msg;
    // "Update now" pre-sets the applying view before the app answers. If the answer is a
    // refusal (the bridge is busy: "Not now: another operation is still running"), there is
    // nothing to show on that screen: back to the list, with the message.
    if (state.view === 'applying' && state.apply && !state.apply.started) {
      state.apply = null;
      state.view = allItems().length ? 'updates' : 'empty';
      toast(msg, false, 7000);
      return;
    }
    if (state.view !== 'applying' && state.view !== 'held' && state.view !== 'doctor') state.view = 'error';
    render();
  }

  /* ------------------------------------------------------------------ */
  /* Incoming: state.json                                                */
  /* ------------------------------------------------------------------ */
  function onState(doc) {
    if (!doc || typeof doc !== 'object') return;
    if (!Array.isArray(doc.groups)) doc.groups = [];   // a malformed document must not poison every later render
    const first = !state.doc;
    state.doc = doc;
    state.error = interrupted(doc) ? null : (doc.error || null);

    reindex();

    // Re-seed the selection: keep the user's picks for items we already showed,
    // fall back to selected_default for anything new *or* just unblocked, and
    // drop whatever is blocked now.
    const items = allItems();
    const seen = state._seen instanceof Map ? state._seen : new Map();
    const known = state.selected;
    const next = new Set();
    for (const it of items) {
      if (it.blocked_by) continue;
      const wasBlocked = seen.get(it.id);         // true | false | undefined (new)
      const keep = wasBlocked === false ? known.has(it.id) : !!it.selected_default;
      if (keep) next.add(it.id);
    }
    state.selected = next;
    state._seen = new Map(items.map(i => [i.id, !!i.blocked_by]));
    for (const id of [...state.expanded]) if (!state._seen.has(id) && !_clusters.has(id)) state.expanded.delete(id);
    for (const id of [...state.openClusters]) if (!_clusters.has(id)) state.openClusters.delete(id);
    const ewIds = new Set(((doc.elsewhere && doc.elsewhere.items) || []).map(i => i.id));
    for (const id of Object.keys(state.ewRuns)) if (!ewIds.has(id) && state.ewRuns[id].phase !== 'running') delete state.ewRuns[id];

    // Track how long the collector has been "checking": per check (a new stub carries a new
    // generated_at), not per push (the file monitor may push the same stub more than once).
    if (doc.status === 'checking') {
      const key = String(doc.generated_at || '');
      if (!state.checkingSince || key !== state.checkingKey) { state.checkingSince = Date.now(); state.checkingKey = key; }
    } else { state.checkingSince = null; state.checkingKey = null; state.checkStartedAt = null; state.listStale = false; }

    // A state push never replaces a screen that is mid-flight (or showing its verdict).
    if (state.view === 'applying' || state.view === 'done' || state.view === 'held' || state.view === 'doctor') { render(); return; }
    decideView();
    if (first) applyTheme();
    render();
  }

  // The collector carries the previous groups in its "checking" stub on purpose:
  // during the hourly check the list stays (selection, scroll, header buttons), with a
  // progress bar and "Checking…" in the header. The skeleton is only for a check with
  // nothing to show. A stub that nobody is working on any more (killed collector,
  // reboot mid-check) must not trap the user: it is treated as finished-with-nothing.
  function checkingIsStale(doc) {
    if (!doc || doc.status !== 'checking') return false;
    const now = Date.now();
    // the app's own stub (cmd refresh) keeps the OLD generated_at, so a check this UI
    // started within the window is fresh whatever the stamp says
    if (state.checkStartedAt && now - state.checkStartedAt < config.STALE_MS) return false;
    const gen = Date.parse(doc.generated_at);
    const age = isNaN(gen) ? now - (state.checkingSince || now) : now - gen;
    return age > config.STALE_MS;
  }
  function decideView() {
    const doc = state.doc;
    state.checking = false; state.staleCheck = false;
    if (doc.status === 'checking') {
      const stale = checkingIsStale(doc);
      if (allItems().length && !state.listStale) { state.view = 'updates'; state.checking = !stale; state.staleCheck = stale; }
      else if (stale) { state.view = 'error'; state.staleCheck = true; }
      else state.view = 'loading';
    }
    else if (doc.status === 'error' && interrupted(doc)) {
      // the collector was killed mid-check (SIGTERM, timeout) and carried the previous
      // groups: that is "not finished", not "broken"; keep the list if there is one
      if (allItems().length && !state.listStale) { state.view = 'updates'; state.staleCheck = true; }
      else { state.view = 'error'; state.staleCheck = true; }
    }
    else if (doc.status === 'error') state.view = 'error';
    else if (!doc.summary || !doc.summary.total) state.view = 'empty';
    else state.view = 'updates';
  }
  function interrupted(doc) { return !!doc && doc.status === 'error' && /^interrupted\b/i.test(String(doc.error || '')); }
  // While a checking document is on screen, time alone changes what to show (the
  // escape-hatch button after CHECK_BUTTON_MS, staleness after STALE_MS).
  let checkTimer = null;
  function scheduleCheckTick() {
    if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; }
    if (!state.doc || state.doc.status !== 'checking' || !state.checkingSince) return;
    const since = Math.max(state.checkingSince, state.checkStartedAt || 0);
    const waited = Date.now() - since;
    const next = waited < config.CHECK_BUTTON_MS ? config.CHECK_BUTTON_MS - waited : config.STALE_MS - waited;
    if (next <= 0 && waited >= config.STALE_MS) return;
    checkTimer = setTimeout(() => {
      checkTimer = null;
      if (!state.doc || state.doc.status !== 'checking') return;
      if (state.view === 'loading' || state.view === 'updates' || state.view === 'empty' || state.view === 'error') decideView();
      render();
    }, Math.max(50, next) + 20);
  }
  function checkWaited() { return state.checkingSince ? Date.now() - Math.max(state.checkingSince, state.checkStartedAt || 0) : 0; }
  function showCheckButton() { return state.doc && state.doc.status === 'checking' && checkWaited() >= config.CHECK_BUTTON_MS; }

  function onSettings(s) {
    state.settings = Object.assign({}, DEFAULT_SETTINGS, s || {});
    applyTheme();
    applyLook();
    render();
  }

  /* ------------------------------------------------------------------ */
  /* Incoming: apply progress events                                     */
  /* ------------------------------------------------------------------ */
  // `release` (2026-11-03): the newer-Fedora download, last of all (CONTRACT "Release upgrade")
  const STEP_ORDER = ['download', 'install', 'akmod', 'flatpak', 'fwupd', 'verify', 'release'];
  const STEP_LABEL = {
    download: 'Download',
    install: 'Install packages',
    akmod: 'Build NVIDIA kernel module',
    flatpak: 'Update Flatpaks',
    fwupd: 'Firmware',
    verify: 'Verify',
    release: 'Download the new Fedora',
  };
  function stepLabel(id) {
    if (id === 'release') { const r = releaseItem(); return r ? `Download Fedora ${r.to}` : STEP_LABEL.release; }
    return STEP_LABEL[id] || id;
  }

  function planSteps(ids) {
    const items = ids.map(byId).filter(Boolean);
    const has = (src) => items.some(i => i.source === src);
    const kmod = items.some(i => !isRelease(i) && (/^kernel/.test(i.name) || /^akmod-|^kmod-/.test(i.name)));
    const want = new Set(['download', 'verify']);
    if (items.some(i => i.source === 'dnf' && !isRelease(i))) want.add('install');
    if (kmod) want.add('akmod');
    if (has('flatpak')) want.add('flatpak');
    if (has('fwupd')) want.add('fwupd');
    if (items.some(isRelease)) want.add('release');
    return STEP_ORDER.filter(s => want.has(s));
  }

  function newApply(ids) {
    const steps = planSteps(ids).map(id => ({
      id, label: stepLabel(id), state: 'pending', pct: null, detail: '',
      warn: id === 'akmod',
    }));
    const byStep = {};
    steps.forEach(s => { byStep[s.id] = s; });
    return { steps, byId: byStep, log: [], result: null, failed: false, planned: ids.slice(), drawer: true };
  }

  function ensureStep(a, id) {
    if (a.byId[id]) return a.byId[id];
    const s = { id, label: stepLabel(id), state: 'pending', pct: null, detail: '', warn: id === 'akmod' };
    a.byId[id] = s;
    // keep canonical order
    const want = STEP_ORDER.indexOf(id);
    const at = want < 0 ? a.steps.length : a.steps.findIndex(x => STEP_ORDER.indexOf(x.id) > want);
    a.steps.splice(at < 0 ? a.steps.length : at, 0, s);
    return s;
  }

  function onProgress(ev) {
    if (!ev) return;
    if (!state.apply) state.apply = newApply([...state.selected]);
    const a = state.apply;
    a.started = true;                     // the first event: rivet-apply really is running
    if (state.view !== 'applying' && state.view !== 'done') state.view = 'applying';

    if (ev.log) pushLog(a, ev.log, ev.status === 'fail' ? 'warn' : null);

    if (ev.step === 'done') {
      // The first done event is the verdict. Nothing may replace it (2026-09-21: a
      // synthetic "rivet-apply exited 1." used to overwrite the real summary).
      if (a.result) return;
      a.result = ev;
      a.failed = ev.status === 'fail';
      a.warned = !a.failed && (ev.status === 'warn' || (ev.warnings || []).length > 0);
      a.steps.forEach(s => {
        if (s.state === 'active') s.state = a.failed ? 'fail' : 'done';
        else if (s.state === 'pending' && !a.failed) { s.state = 'skip'; s.detail = 'nothing to do'; }
      });
      if (ev.summary) pushLog(a, ev.summary, a.failed || a.warned ? 'warn' : 'ok');
      state.view = 'done';
      render();
      return;
    }

    const s = ensureStep(a, ev.step);
    // A step earlier in the pipeline that was running is finished; one that never
    // reported at all is marked skipped rather than claimed as done.
    const idx = a.steps.indexOf(s);
    for (let i = 0; i < idx; i++) {
      if (a.steps[i].state === 'active') a.steps[i].state = 'done';
      else if (a.steps[i].state === 'pending') { a.steps[i].state = 'skip'; a.steps[i].detail = 'nothing to do'; }
    }

    if (ev.status === 'fail') { s.state = 'fail'; a.failed = true; }
    else if (ev.status === 'ok') { s.state = 'done'; s.pct = 100; s.warned = !!ev.warning; }
    else if (ev.status === 'skip') { s.state = 'skip'; }
    else { s.state = 'active'; }
    if (ev.pct != null) s.pct = ev.pct;
    if (ev.msg) {
      s.detail = ev.msg;
      // a verdict line (ok / fail) always reaches the details log, next to its raw tool line
      if (!ev.log || ev.status === 'ok' || ev.status === 'fail') pushLog(a, ev.msg, ev.status === 'fail' || ev.warning ? 'warn' : ev.status === 'ok' ? 'ok' : null);
    }
    render();
  }

  function pushLog(a, line, cls) {
    a.log.push({ line: String(line), cls: cls || '' });
    if (a.log.length > 800) a.log.splice(0, a.log.length - 800);
  }

  /* ------------------------------------------------------------------ */
  /* Derived helpers                                                     */
  /* ------------------------------------------------------------------ */
  // Rebuilt once per state document: 136+ items are normal, so no linear scans
  // per render (see _index()).
  let _items = [], _index = new Map(), _clusters = new Map();
  function reindex() {
    _items = groups().reduce((a, g) => a.concat(g.items || []), []);
    _index = new Map(_items.map(i => [i.id, i]));
    // The System (dnf) group is shown one row per CLUSTER: the packages of one Fedora
    // advisory, or, without one, a package family at one target version. Same rule as
    // cluster_key() in backend/triage.py, so the verdict really is shared by the members.
    _clusters = new Map();
    for (const g of groups()) {
      if (!CLUSTERED_GROUPS.has(g.id)) continue;
      for (const it of g.items || []) {
        const id = 'cluster:' + clusterKey(it);
        let c = _clusters.get(id);
        if (!c) { c = { id, group: g.id, members: [] }; _clusters.set(id, c); }
        c.members.push(it);          // document order (the collector lists a cluster's packages together)
      }
    }
  }
  const CLUSTERED_GROUPS = new Set(['dnf']);
  function clusterKey(it) {
    const a = it.advisory;
    if (a && a.id) return 'adv:' + a.id;
    return `${it.source}:${String(it.name || '').split(/[-_]/)[0].toLowerCase()}:${it.to}`;
  }
  function clustersOf(groupId) { return [..._clusters.values()].filter(c => c.group === groupId); }
  function clusterById(id) { return _clusters.get(id) || null; }
  // The longest common prefix of the member names that ends on a name boundary: "kernel"
  // for kernel + kernel-core + kernel-devel, "grub2" for grub2-common + grub2-efi-x64,
  // "python3-urllib3" for python3-urllib3 + python3-urllib3+socks (never "libav" for
  // libavcodec-free + libavdevice-free); with no common prefix, the first member's name
  // (ffmpeg-free for ffmpeg-free + libavcodec-free + …).
  function familyName(members) {
    if (members.length === 1) return members[0].display_name || members[0].name;
    const names = members.map(it => String(it.name || it.display_name || ''));
    let p = names[0];
    for (const n of names) { let i = 0; while (i < p.length && i < n.length && p[i] === n[i]) i++; p = p.slice(0, i); }
    const whole = (s) => s && names.every(n => n === s || /[-_+.]/.test(n.charAt(s.length)));
    if (!whole(p)) { const cut = Math.max(p.lastIndexOf('-'), p.lastIndexOf('_')); p = cut > 0 ? p.slice(0, cut) : ''; }
    return whole(p) ? p.replace(/[-_+.]+$/, '') : names[0];
  }
  const BADGE_RANK = { blocked: 0, rebuild: 1, reboot: 2, security: 3 };
  // Per render (effRisk depends on a setting): the worst of the members, and whether the
  // cluster is routine (every member green, no badge, no reboot, not blocked, no advisory
  // of type security), which is what folds it into the "Routine updates" accordion.
  function clusterInfo(c) {
    const ms = c.members;
    const sorted = sortItems(ms);
    let risk = 'green', badge = null;
    for (const it of ms) {
      const r = effRisk(it);
      if ((RANK[r] || 9) < (RANK[risk] || 9)) risk = r;
      if (it.badge && (badge == null || (BADGE_RANK[it.badge] != null ? BADGE_RANK[it.badge] : 9) < (BADGE_RANK[badge] != null ? BADGE_RANK[badge] : 9))) badge = it.badge;
    }
    const crit = ms.some(isCritical);
    const reboot = ms.some(it => it.requires_reboot);
    const blocked = ms.some(isBlocked);
    const security = ms.some(isSecurity);
    return {
      rep: sorted[0], sorted, crit, risk, badge, reboot, blocked, security,
      size: ms.reduce((s, it) => s + (it.size_bytes || 0), 0),
      sameTo: ms.every(it => it.to === ms[0].to), sameFrom: ms.every(it => it.from === ms[0].from),
      routine: !crit && risk === 'green' && !badge && !reboot && !blocked && !security,
      name: familyName(ms),
    };
  }
  // What a checkbox id stands for: an item, every member of a cluster, or every routine
  // package of a group. The selection itself stays a set of item ids (the plan the host gets).
  function targets(id) {
    const it = byId(id);
    if (it) return [it];
    const c = clusterById(id);
    if (c) return c.members;
    if (typeof id === 'string' && id.startsWith('routine:')) {
      const g = id.slice('routine:'.length);
      return clustersOf(g).filter(c => clusterInfo(c).routine).reduce((a, c) => a.concat(c.members), []);
    }
    return [];
  }
  // Held-back (pinned) refs live in their own group and are never part of the pool:
  // not selectable, not counted, not sent in a plan.
  function groups() { return ((state.doc && state.doc.groups) || []).filter(g => g.id !== 'held'); }
  function heldItems() {
    const g = ((state.doc && state.doc.groups) || []).find(x => x.id === 'held');
    return (g && g.items) || [];
  }
  function heldByRef(ref) { return heldItems().find(i => i.ref === ref) || null; }
  function allItems() { return _items; }
  function byId(id) { return _index.get(id) || null; }
  function flags() { return (state.doc && state.doc.flags) || {}; }
  function host() { return (state.doc && state.doc.host) || {}; }

  function isBlocked(it) { return !!it.blocked_by; }
  function selectable(it) { return !isBlocked(it); }
  // The newer-Fedora row (collector release.py, `op: "release"`): shown with the dnf rows,
  // ticked only by hand (never by Select all / Safe only / Security only), applied last.
  function isRelease(it) { return !!it && it.op === 'release'; }
  function releaseItem() { return allItems().find(isRelease) || null; }
  // what the bulk selectors and the "all on" checkboxes reason about
  function sweepable(it) { return selectable(it) && !isRelease(it); }
  function effRisk(it) {
    if (state.settings.red_includes_reboot && it.requires_reboot && it.risk !== 'red') return 'red';
    return it.risk || 'green';
  }
  function isCritical(it) {
    const a = it.advisory;
    return !!(a && a.type === 'security' && (a.severity === 'critical' || a.severity === 'important'));
  }
  function riskLabel(it) {
    const r = effRisk(it);
    if (r === 'red') return isCritical(it) ? 'Critical security' : isBlocked(it) ? 'Blocked' : 'Conflict';
    return r === 'yellow' ? 'Caution' : 'Safe';
  }
  const BADGE_TEXT = { security: 'SECURITY', reboot: 'REBOOT', rebuild: 'REBUILD', blocked: 'BLOCKED' };

  function selectSafe() {
    state.selected = new Set(allItems().filter(it => sweepable(it) && (effRisk(it) !== 'red' || isCritical(it))).map(it => it.id));
  }
  function selectAll() { state.selected = new Set(allItems().filter(sweepable).map(it => it.id)); }
  // "Security" = carries a security advisory (any severity), or is badged as one.
  // Not the badge alone: a kernel with a security advisory is badged REBOOT (contract
  // badge precedence), and the user still means it with "all security updates".
  function isSecurity(it) {
    const a = it.advisory;
    return !!((a && a.type === 'security') || it.badge === 'security' || isCritical(it));
  }
  function selectSecurity() {
    state.selected = new Set(allItems().filter(it => selectable(it) && isSecurity(it)).map(it => it.id));
  }

  const fmtBytes = (b) => {
    b = Number(b) || 0;
    if (b >= 1e9) return (b / 1e9).toFixed(b < 1e10 ? 1 : 0) + ' GB';
    if (b >= 1e6) return (b / 1e6 < 10 ? (b / 1e6).toFixed(1) : Math.round(b / 1e6)) + ' MB';
    if (b >= 1e3) return Math.round(b / 1e3) + ' KB';
    return b + ' B';
  };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function hhmm(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  /* ------------------------------------------------------------------ */
  /* Theme                                                               */
  /* ------------------------------------------------------------------ */
  const mqDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function resolvedTheme() {
    if (FORCE_THEME) return FORCE_THEME;
    const t = state.settings.theme || 'system';
    if (t === 'dark' || t === 'light') return t;
    return mqDark && mqDark.matches ? 'dark' : 'light';
  }
  function applyTheme() {
    document.documentElement.setAttribute('data-theme', resolvedTheme());
    try { localStorage.setItem('rivet.theme', state.settings.theme); } catch (e) {}
  }
  if (mqDark && mqDark.addEventListener) mqDark.addEventListener('change', () => { if ((state.settings.theme || 'system') === 'system') { applyTheme(); render(); } });

  function setTheme(t) {
    state.settings.theme = t;
    applyTheme();
    send({ cmd: 'setSetting', key: 'theme', value: t });
  }

  /* Look + type size. The look is a data-look attribute (index.html sets it before first paint from
     localStorage); type size is the host's WebKit page zoom (rivet_app.py), so every look scales as
     one, layout included. Both persist in settings.json like the theme. */
  function currentLook() { return lookById(FORCE_LOOK || state.settings.look); }
  function applyLook() {
    const look = currentLook();
    document.documentElement.setAttribute('data-look', look.id);
    document.documentElement.setAttribute('data-size', state.settings.type_size === 'comfortable' ? 'comfortable' : 'compact');
    writeLS('rivet.look', look.id);
  }
  function setLook(id, announce) {
    state.settings.look = lookById(id).id;
    applyLook();
    send({ cmd: 'setSetting', key: 'look', value: state.settings.look });
    if (announce) toast('Look: ' + lookById(id).name + ' · g y for the next one', false, 1800);
  }
  function nextLook() {
    const i = LOOKS.findIndex(l => l.id === currentLook().id);
    setLook(LOOKS[(i + 1) % LOOKS.length].id, true);
  }
  function setTypeSize(v) {
    state.settings.type_size = v === 'comfortable' ? 'comfortable' : 'compact';
    applyLook();
    send({ cmd: 'setSetting', key: 'type_size', value: state.settings.type_size });
  }
  applyLook();

  function toast(msg, spin, ms) {
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toast = { msg, spin: !!spin };
    if (ms) state.toastTimer = setTimeout(() => { state.toast = null; render(); }, ms);
    render();
  }

  /* ------------------------------------------------------------------ */
  /* Rendering                                                           */
  /* ------------------------------------------------------------------ */
  const $ = (s) => document.querySelector(s);
  const el = {
    win: $('#window'), main: $('#main'), sub: $('#brand-sub'), name: $('#brand-name'),
    actions: $('#header-actions'), menu: $('#menu'), overlay: $('#overlay'),
    search: $('#search'), searchWrap: $('#search-wrap'), themeBtn: $('#theme-btn'),
    steamItem: $('#steam-item'), menuCheck: $('#menu [data-action="check-now"]'),
    menuSecurity: $('#menu [data-action="select-security"]'), menuSafe: $('#menu [data-action="select-safe"]'),
  };
  const svgChev = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
  const svgCopy = '<svg class="ic-copy" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
  const svgX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';

  if (MOCK) {
    ['#menu-mock', '#menu-mock-sep', '#menu-mock-hint'].forEach(s => { const n = $(s); if (n) n.hidden = false; });
  }

  const VIEWS = {
    boot: () => `<div class="hero"><div class="inner"><h1>Rivet</h1><p>Connecting…</p></div></div>`,
    updates: renderUpdates, loading: renderLoading, empty: renderEmpty,
    applying: renderApplying, done: renderDone, error: renderError, held: renderHeld,
    doctor: renderDoctor,
  };

  // Scroll containers whose position survives a re-render (innerHTML replaces them).
  const SCROLLERS = ['#list', '.hero.result', '.empty-held', '.applying', '.loading', '.modal-b'];

  function render() {
    try {
      renderAll();
    } catch (err) {
      renderCrash(err);
    }
  }

  function renderAll() {
    el.menu.hidden = !state.menuOpen;
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    el.themeBtn.querySelector('.sun').style.display = dark ? '' : 'none';
    el.themeBtn.querySelector('.moon').style.display = dark ? 'none' : '';
    el.searchWrap.style.display = state.view === 'updates' ? '' : 'none';
    if (el.steamItem) el.steamItem.textContent = flags().steam_running ? 'Steam: running · pretend it quit' : 'Steam: closed · pretend it started';
    if (el.menuCheck) { const busy = checkBusy(); el.menuCheck.disabled = busy; el.menuCheck.title = busy ? 'Not while an update or a fix is running' : ''; }
    // the selection shortcuts live in the menu too: the header buttons hide below 720 px
    if (el.menuSecurity) el.menuSecurity.disabled = state.view !== 'updates' || !securityCount();
    if (el.menuSafe) el.menuSafe.disabled = state.view !== 'updates';
    // The view HTML is built BEFORE anything on screen changes, so a renderer that
    // throws leaves the old screen (and header) intact instead of half-updated.
    const html = (state.view === 'updates' || state.view === 'empty' ? renderHealth() : '') + (VIEWS[state.view] || VIEWS.boot)();
    const overlay = (state.modal === 'settings' ? renderSettings() : state.modal === 'held-confirm' ? renderHeldConfirm()
      : state.modal === 'ew-bulk' ? renderEwBulkConfirm() : state.modal === 'ew-remove' ? renderEwRemoveConfirm() : '')
      + (state.toast ? renderToast() : '');
    const keep = SCROLLERS.map(sel => { const n = el.main.querySelector(sel); return n && n.scrollTop ? [sel, n.scrollTop] : null; }).filter(Boolean);
    renderHeader();
    el.main.innerHTML = html;
    keep.forEach(([sel, top]) => { const n = el.main.querySelector(sel); if (n) n.scrollTop = top; });
    if (state.view === 'applying' || (state.view === 'doctor' && state.doctor && /working|fixing/.test(state.doctor.phase))) { const log = el.main.querySelector('.log'); if (log) log.scrollTop = log.scrollHeight; }
    el.overlay.innerHTML = overlay;
    scheduleCheckTick();
  }

  /* ---- health checks (backend/health.py, 2026-11-01) ---- */
  function onHealth(msg) {
    if (msg.health && Array.isArray(msg.health.checks)) state.health = msg.health;
    const ev = msg.event;
    if (ev && state.healthRun) {
      if (ev.step === 'done') {
        state.healthRun = Object.assign({}, state.healthRun, { phase: 'done', ok: ev.status === 'ok', summary: ev.summary || ev.msg || '' });
        if (ev.health && Array.isArray(ev.health.checks)) state.health = ev.health;
        if (ev.status === 'ok') { toast(ev.summary || 'Fixed.', false, 5000); state.healthRun = null; }
      } else if (ev.msg) state.healthRun.msg = ev.msg;
    }
    render();
  }
  function renderHealth() {
    const checks = ((state.health || {}).checks || []).filter(c => c && c.id);
    if (!checks.length) return '';
    return `<div class="health">${checks.map(c => {
      const r = state.healthRun && state.healthRun.id === c.id ? state.healthRun : null;
      const running = r && r.phase === 'running', failed = r && r.phase === 'done' && !r.ok;
      return `<div class="health-banner${failed ? ' fail' : ''}" data-id="${esc(c.id)}" role="status">
        <span class="glyph warn" aria-hidden="true">!</span>
        <div class="hb-body"><div class="hb-title">${esc(c.title)}</div><div class="hb-detail">${esc(c.detail)}</div>
          ${failed ? `<div class="hb-fail">${esc(r.summary)}</div>` : ''}</div>
        ${running ? `<span class="hb-busy"><span class="spin"></span>${esc(r.msg || 'Working…')}</span>`
          : `<button class="btn small primary" data-action="health-fix" data-id="${esc(c.id)}"${checkBusy() ? ' disabled title="Not while an update or a fix is running"' : ''}>${esc(failed ? 'Try again' : c.fix_label || 'Fix it')}</button>`}
      </div>`; }).join('')}</div>`;
  }

  // A view renderer threw: say so on screen, with a way out, instead of leaving a stale
  // DOM whose every later event throws again.
  function renderCrash(err) {
    const text = String((err && err.message) || err || 'unknown error');
    console.error('[rivet] render failed in view "' + state.view + '": ' + ((err && err.stack) || err));
    state.lastBug = { where: 'render ' + state.view, error: text };
    try {
      el.name.textContent = 'Rivet';
      el.sub.textContent = 'something went wrong drawing this screen';
      el.actions.innerHTML = '';
      el.searchWrap.style.display = 'none';
      el.menu.hidden = true; state.menuOpen = false;
      el.overlay.innerHTML = '';
      el.main.innerHTML = `<div class="hero crash" data-crash="1"><div class="inner">
        <div class="ring bad">!</div>
        <h1>Rivet hit a bug drawing this screen</h1>
        <p>The “${esc(state.view)}” screen could not be drawn. Nothing on the system was touched by this; checking again rebuilds the screen from scratch.</p>
        <pre class="crash-msg">${esc(text)}</pre>
        <div class="actions"><button class="btn primary" data-action="check-now" data-force="1">Check for updates</button></div>
      </div></div>`;
    } catch (e2) {
      console.error('[rivet] crash screen failed too: ' + e2);
    }
  }

  function renderHeader() {
    const n = state.selected.size;
    const h = host();
    const os = h.os || 'Fedora';
    let sub;
    switch (state.view) {
      case 'loading': sub = showCheckButton() ? 'Still checking… this is taking longer than usual' : 'Checking for updates…'; break;
      case 'applying': sub = applyingSub(); break;
      case 'done': sub = state.apply && state.apply.failed ? `${os} · update failed`
        : state.apply && state.apply.warned ? `${os} · updated, with a warning` : `${os} · updated just now`; break;
      case 'error': sub = state.staleCheck ? `${os} · the last check did not finish` : `${os} · could not check`; break;
      case 'held': sub = `${os} · ` + (state.held && state.held.phase === 'result'
        ? ({ kept: 'pinned build kept', rolled_back: 'rolled back, pinned again' }[(state.held.result || {}).kind] || 'pinned build')
        : 'trying a pinned build'); break;
      case 'doctor': sub = `${os} · ` + ({ working: 'looking into it', fixing: 'running the fix',
        fixed: state.doctor && state.doctor.result && state.doctor.result.ok ? 'fixed' : 'the fix did not work' }[(state.doctor || {}).phase] || 'diagnosis'); break;
      case 'boot': sub = 'Starting…'; break;
      default: sub = state.checking ? `${os} · checking…${showCheckButton() ? ' (taking a while)' : ''}`
        : state.staleCheck ? `${os} · checked ${hhmm(state.doc && state.doc.checked_at)} · the last check did not finish`
        : `${os} · checked ${hhmm(state.doc && state.doc.checked_at)}`;
    }
    el.name.textContent = state.view === 'applying' ? 'Updating…' : 'Rivet';
    // Other software's pointer lives here, not as a summary chip: the summary row is full at 1280 px
    el.sub.innerHTML = esc(sub) + (state.view === 'updates' ? ewPointer() : '');

    let html = '';
    if (state.view === 'updates') {
      const nSec = securityCount();
      html = `<button class="btn" data-action="select-security" ${nSec ? '' : 'disabled'} title="Select only the updates that carry a security advisory">Security only</button>
              <button class="btn" data-action="select-safe" title="Select everything except conflicts, plus any critical security fix">Safe only</button>
              <button class="btn primary" data-action="update-now" ${n ? '' : 'disabled'}>Update now <span class="count">${n}</span></button>`;
    } else if (state.view === 'empty' || state.view === 'error') {
      html = `<button class="btn" data-action="check-now">Check now</button>`;
    } else if (state.view === 'loading' && showCheckButton()) {
      html = `<button class="btn" data-action="check-now" data-force="1">Check for updates</button>`;
    }
    el.actions.innerHTML = html;
  }

  function securityCount() { return allItems().filter(it => selectable(it) && isSecurity(it)).length; }

  function applyingSub() {
    const a = state.apply;
    if (!a) return '';
    const done = a.steps.filter(s => s.state === 'done' || s.state === 'skip').length;
    const cur = a.steps.find(s => s.state === 'active');
    return `Step ${Math.min(done + 1, a.steps.length)} of ${a.steps.length}${cur ? ' · ' + cur.label.toLowerCase() : ''}`;
  }

  /* ---- Updates list ---- */
  function renderUpdates() {
    const q = state.query.trim().toLowerCase();
    const matches = (it) => !q || (`${it.display_name} ${it.name} ${it.verdict} ${it.from} ${it.to} ${it.ref} ${it.advisory ? it.advisory.id : ''}`).toLowerCase().includes(q);
    const all = sortItems(allItems());
    const f = flags();
    const reboot = all.filter(it => it.requires_reboot).length;
    const security = f.security_count != null ? f.security_count : all.filter(isCritical).length;
    const blocked = f.blocked_count != null ? f.blocked_count : all.filter(isBlocked).length;
    const sel = [...state.selected].map(byId).filter(Boolean);
    const selSize = sel.reduce((s, it) => s + (it.size_bytes || 0), 0);
    const totalSize = (state.doc.summary && state.doc.summary.total_bytes) || all.reduce((s, it) => s + (it.size_bytes || 0), 0);
    const pool = all.filter(sweepable);
    const allOn = pool.length && pool.every(it => state.selected.has(it.id));
    const anyOn = pool.some(it => state.selected.has(it.id));
    const selReboot = sel.some(it => it.requires_reboot);

    let out = (state.checking ? `<div class="progress indet checking" title="Checking for updates…"><i></i></div>` : '') + `<div class="summary">
      <span class="stat"><b>${all.length}</b> update${all.length === 1 ? '' : 's'}</span>
      <span class="stat warn"><b>${reboot}</b> need reboot</span>
      <span class="stat bad"><b>${security}</b> security</span>
      <span class="stat bad"><b>${blocked}</b> blocked</span>
      ${selReboot ? `<span class="chip" title="The selection needs a restart to finish"><span class="dot"></span>REBOOT REQUIRED</span>` : ''}
      ${security ? `<span class="chip red"><span class="dot"></span>SECURITY</span>` : ''}
      ${heldItems().some(i => i.hold_state === 'candidate' || i.hold_state === 'trial') ? `<span class="chip held-chip" data-action="scroll-held" role="button" title="A newer build of something you pinned is out. Scroll to Held back."><span class="dot"></span>PINNED BUILD TO TRY ↓</span>` : ''}
      <span class="spacer"></span>
      <span class="meta"><span>${fmtBytes(selSize)} selected · ${fmtBytes(totalSize)} total</span>
        <label class="selall" data-action="select-all-global"><span class="cb ${allOn ? 'on' : anyOn ? 'mixed' : ''}"></span>Select all</label></span>
    </div>
    <div class="list" id="list">`;

    let shown = 0;
    for (const g of groups()) {
      // System (dnf): one row per cluster, routine clusters folded into one accordion row.
      // A cluster is shown when any member matches the filter; its member list then
      // shows the matching members. Everything else: one row per item, as before.
      const clustered = CLUSTERED_GROUPS.has(g.id)
        ? clustersOf(g.id).map(c => ({ c, info: clusterInfo(c), hit: c.members.filter(matches) })).filter(x => x.hit.length) : null;
      const items = clustered ? clustered.reduce((a, x) => a.concat(x.hit), []) : sortItems((g.items || []).filter(matches));
      if (!items.length) continue;
      const pick = items.filter(sweepable);
      const gAll = pick.length && pick.every(it => state.selected.has(it.id));
      const gAny = pick.some(it => state.selected.has(it.id));
      const gBlocked = items.some(isBlocked);
      const title = `${(g.title || g.id)}${g.subtitle ? ' · ' + g.subtitle : ''}`.toUpperCase();
      let body, rows;
      if (clustered) {
        const routine = clustered.filter(x => x.info.routine);
        const rest = sortClusters(clustered.filter(x => !x.info.routine));
        rows = rest.length + (routine.length ? 1 : 0);
        body = rest.map(x => renderCluster(x, q)).join('') + (routine.length ? renderRoutine(g, routine, q) : '');
      } else {
        rows = items.length;
        body = items.map(renderRow).join('');
      }
      shown += rows;
      out += `<div class="group-h"><span class="group-title">${esc(title)}<span class="n">${items.length}</span>${rows < items.length ? `<span class="rows" title="${items.length} packages shown as ${rows} rows">in ${rows} row${rows === 1 ? '' : 's'}</span>` : ''}</span>
        <label class="selall" data-action="select-group" data-group="${esc(g.id)}"><span>Select all</span><span class="cb ${gAll ? 'on' : gAny ? 'mixed' : ''} ${pick.length ? '' : 'disabled'}"></span></label></div>
        <div class="card" data-group="${esc(g.id)}">`;
      if (gBlocked) {
        out += `<div class="banner red"><span class="risk red"></span><span>Steam is running. Quit Steam before applying Flatpak runtime updates, or game launches break until Steam restarts.</span>
          <span class="spacer"></span><button class="btn danger" data-action="quit-steam">Quit Steam</button></div>`;
      }
      out += body;
      out += `</div>`;
    }
    if (!shown) out += `<div class="empty-search">Nothing matches “${esc(state.query)}”. <button class="btn small ghost" data-action="clear-search">Clear</button></div>`;
    out += renderElsewhereSection(q);
    out += renderHeldSection();      // last: pins are the user's own "not now" (2026-11-03)
    out += `</div>`;
    return out;
  }

  /* ---- Other software (npm, pipx, uv, cargo, HA, podman, GNOME ext, AppImage) ---- */
  // A top-level state.json object (`elsewhere`, its name until 2026-10-31), never a group: no
  // checkbox, never selected, never in a plan, never counted in the summary or the top-bar
  // badge. Each row says what the thing is and who uses it, and offers one button by its
  // `action`: Update / Install in HA (otherUpdate, streamed into the row), Open, or Copy.
  // CONTRACT.md "Other software".
  const EW_SRC = { npm: 'npm', pipx: 'pipx', uv: 'uv', cargo: 'cargo', ha: 'HA', podman: 'podman', 'gnome-ext': 'GNOME ext', appimage: 'AppImage' };
  const EW_BADGE = { major: ['major', 'MAJOR'], minor: ['minor', 'MINOR'], patch: ['patch', 'PATCH'], unknown: ['grey', 'NEW VERSION'] };
  // Where a row lives, in the order the section shows them (CONTRACT.md "Front page", 2026-10-31 c)
  const EW_GROUPS = [
    { id: 'pc', title: 'This PC', sources: ['npm', 'pipx', 'uv', 'cargo', 'gnome-ext', 'appimage'] },
    { id: 'ha', title: 'Home Assistant', sources: ['ha'] },
    { id: 'containers', title: 'Containers', sources: ['podman'] },
  ];
  const EW_SEV = { major: 0, minor: 1, patch: 2, unknown: 3 };
  const EW_STATUS = { ok: 0, updated: 0, failed: 1, cant_check: 2, rebuilt: 3 };
  function elsewhere() { const e = state.doc && state.doc.elsewhere; return e && typeof e === 'object' ? e : null; }
  // settings.show_containers (2026-11-01, default off): no Containers section, no podman rows anywhere
  function ewShowContainers() { return state.settings.show_containers === true; }
  function ewItems() {
    const e = elsewhere(), all = (e && Array.isArray(e.items)) ? e.items : [];
    return ewShowContainers() ? all : all.filter(i => i.source !== 'podman');
  }
  function ewStale() { return ewItems().filter(i => i.status === 'ok').length; }
  function ewRunning() { return Object.values(state.ewRuns).find(r => r.phase === 'running') || null; }
  // one row streaming, a bulk run between two rows, or Home Assistant restarting: no other Update now
  function ewBusy() { return !!(ewRunning() || state.ewBatch || state.ewHaRestart); }
  const EW_FAILED = ['failed', 'fix_failed'];      // update failures; a failed Remove keeps its HA link
  // a row whose last run failed (and is not running now) can ask Claude to fix it (otherFix)
  function ewFixable(it) { return EW_FAILED.includes((it.last_run || {}).result) && !(state.ewRuns[it.id] && state.ewRuns[it.id].phase === 'running'); }
  // Fix it needs the Claude Code CLI (2026-11-01 abuse pass): without it the button is a quiet note
  const NO_CLAUDE_TITLE = 'Fix it asks Claude Code to look into a failure. Install Claude Code (claude.com/claude-code), run claude once to log in, then reopen Rivet.';
  function noClaude() { return state.caps.claude === false; }
  function noClaudeNote(cls) { return `<span class="no-claude ${cls || ''}" title="${NO_CLAUDE_TITLE}">Install Claude Code to enable Fix it</span>`; }
  function ewIntegration(it) { return it.source === 'ha' && it.kind === 'integration'; }
  function ewGroupOf(it) { const g = EW_GROUPS.find(x => x.sources.includes(it.source)); return g ? g.id : 'pc'; }
  // stale rows major -> minor -> patch -> unknown (an updated row keeps its place), then failed, then can't check
  function ewSort(items) {
    const rank = (m, k, d) => (k in m ? m[k] : d);
    return items.map((it, i) => [it, i]).sort((a, b) =>
      (rank(EW_STATUS, a[0].status, 1) - rank(EW_STATUS, b[0].status, 1))
      || (rank(EW_SEV, a[0].severity, 3) - rank(EW_SEV, b[0].severity, 3)) || (a[1] - b[1])).map(p => p[0]);
  }
  // The safe set (same rule as Bridge.other_safe): patch releases and image pulls that need no
  // confirmation, carry no warning and do not restart or reboot Home Assistant or a device.
  // 2026-11-01: Home Assistant add-ons and integrations at any non-major bump too (a major
  // still asks in its own row); an integration makes Home Assistant restart ONCE at the end.
  function ewSafe(it) {
    return it.status === 'ok' && (it.action === 'update' || it.action === 'ha_install') && !it.confirm
      && it.severity !== 'major' && !it.warning && !it.pulled && !/^(core|os|supervisor|firmware)$/.test(it.kind || '')
      && (it.severity === 'patch' || it.source === 'podman' || (it.source === 'ha' && /^(addon|integration)$/.test(it.kind || '')));
  }
  // Remove uninstalls it (otherRemove): only an add-on the backend marked `unused: true`, with its
  // Supervisor slug. The same rule as elsewhere_apply.remove_refusal (CONTRACT.md "Remove").
  function ewRemovable(it) { return it.source === 'ha' && it.status === 'ok' && it.kind === 'addon' && it.unused === true && /^[A-Za-z0-9_.-]{1,80}$/.test(it.addon_slug || ''); }
  function ewUnused(it) { return it.source === 'ha' && it.status === 'ok' && (it.unused === true || /nothing uses it yet/i.test(it.used_by || '')); }
  function ewDigest(it) { return /^sha256:/.test(String(it.installed || '')) || /^sha256:/.test(String(it.latest || '')); }
  function ewName(it) { return it.source === 'podman' ? String(it.name).replace(/^docker\.io\/(library\/)?/, '') : String(it.name || ''); }
  function ewPointer() {
    const n = ewStale();
    return n ? ` · <button class="ew-link" data-action="scroll-elsewhere" title="${n} thing${n === 1 ? '' : 's'} outside dnf and Flatpak with a newer version. Scroll to Other software.">${n} more in Other software ↓</button>` : '';
  }
  // "1.4.0 → 1.34.0" with the bumped tail (from the first differing component) emphasised
  function ewVersion(it) {
    if (it.status === 'updated' && it.pulled) return `<span class="bump patch">${esc(String(it.latest || '').replace(/^sha256:/, ''))}</span> pulled`;
    if (it.status === 'updated') return `now <span class="bump patch">${esc(String(it.installed || it.latest || '').replace(/^sha256:/, ''))}</span>`;
    if (it.status !== 'ok' || !it.latest) return it.installed ? esc(it.installed) : '';
    const a = String(it.installed || ''), b = String(it.latest);
    if (/^sha256:/.test(a) || /^sha256:/.test(b)) return `${esc(a.replace(/^sha256:/, ''))} → <span class="bump ${esc(it.severity)}">${esc(b.replace(/^sha256:/, ''))}</span>`;
    const pa = a.split('.'), pb = b.split('.');
    let i = 0;
    while (i < pb.length - 1 && pa[i] === pb[i]) i++;
    const head = pb.slice(0, i).join('.') + (i ? '.' : '');
    return `${esc(a)} → ${esc(head)}<span class="bump ${esc(it.severity)}">${esc(pb.slice(i).join('.'))}</span>`;
  }
  // one badge family for every source; a digest-only change (podman) is UNKNOWN, labelled NEW BUILD
  function ewBadge(it) {
    if (it.status === 'rebuilt') return '';          // same version, new digest: nothing to do (2026-11-01)
    if (it.status === 'failed') return `<span class="badge critical">CHECK FAILED</span>`;
    if (it.status === 'cant_check') return `<span class="badge grey">CAN’T CHECK</span>`;
    if (it.status === 'updated') return `<span class="badge patch">${it.pulled ? 'PULLED' : 'UPDATED'}</span>`;
    const [cls, text] = EW_BADGE[it.severity] || EW_BADGE.unknown;
    return `<span class="badge ${cls}">${cls === 'grey' && ewDigest(it) ? 'NEW BUILD' : text}</span>`;
  }
  // the one muted line a row may carry under its name: what matters BEFORE pressing its button
  function ewHint(it) {
    if (it.status === 'failed') return ['bad', it.hint || 'The check failed.'];
    if (it.status === 'cant_check') return ['', it.hint || 'Rivet cannot check this one.'];
    if (it.restart_pending && !state.ewRuns[it.id]) return ['warn', 'Downloaded; Home Assistant needs a restart to load it.'];
    if (ewFixable(it) && !state.ewRuns[it.id]) return ['bad', (it.last_run.note || 'The last update failed.').replace(/^(Not fixed: )?/, it.last_run.result === 'fix_failed' ? 'Not fixed: ' : '')];
    if (it.status === 'updated' || it.status === 'rebuilt') return null;
    if (it.source === 'ha' && /^(core|os|supervisor)$/.test(it.kind || '')) return ['', 'Open only: installing it restarts Home Assistant, so you do it there.'];
    if (it.pulled) return ['', it.hint || 'Pulled. Restart it to use the new image.'];
    if (it.warning && state.ewConfirm !== it.id) return ['warn', it.warning];
    if (ewUnused(it)) return ['', 'Nothing uses it; consider removing.'];
    return null;
  }
  const aiTag = (it, field) => (it.ai || []).includes(field) ? ` <span class="ai-tag" title="Written by Claude (claude -p), not from a registry or this box">AI</span>` : '';
  // the exact thing a button will do, as the confirm panel and the details show it
  function ewDoes(it) {
    if (it.action === 'ha_install') return `Home Assistant: update.install ${it.entity_id || ''} ${it.latest || ''}${it.backup ? ' (backup first)' : ''}${ewIntegration(it) ? ', then one Home Assistant restart to load it' : ''}`;
    return it.command ? '$ ' + it.command : '';
  }
  function ewButton(it) {
    const why = ewBusy() ? ' disabled title="Another update is running"' : '';
    switch (it.action) {
      case 'update': return `<button class="btn small primary ew-do" data-action="ew-update" data-id="${esc(it.id)}"${why || ` title="${esc(ewDoes(it))}"`}>Update</button>`;
      case 'ha_install': return `<button class="btn small primary ew-do" data-action="ew-update" data-id="${esc(it.id)}"${why || ` title="${esc(ewDoes(it))}"`}>Install in HA</button>`;
      case 'copy': return it.command ? `<button class="btn small ew-copy" data-action="ew-copy" data-text="${esc(it.command)}" title="Copy: ${esc(it.command)}" aria-label="Copy the command">${svgCopy}Copy</button>` : '';
      case 'open': return it.link ? `<button class="btn small ew-open" data-action="open-url" data-url="${esc(it.link)}" title="${esc(it.link)}">${it.source === 'ha' ? 'Open in HA' : 'Open'} ↗</button>` : '';
      // a container that holds data (2026-11-01): its runbook, never Update; no runbook = plain text
      case 'runbook': return it.runbook ? `<button class="btn small ew-open ew-runbook" data-action="open-url" data-url="${esc(it.runbook)}" title="${esc(it.runbook_title || it.runbook)}">Open runbook ↗</button>` : '';
      default: return it.source === 'podman' && it.stateful && it.status === 'ok' && it.manual ? `<span class="ew-manual" title="${esc(it.manual)}">${esc(it.manual)}</span>` : '';
    }
  }
  // the add-on's own page in Home Assistant (Remove's fallback, and the link for older rows)
  function ewRemoveLink(it) {
    if (it.addon_link) return it.addon_link;
    try { return new URL(it.link).origin + '/config/apps'; } catch (e) { return null; }
  }
  // Remove for an add-on nothing uses (2026-11-01: uninstalls it after a confirmation). A row
  // that only says "nothing uses it yet" (no `unused: true`, from before 2026-10-31 c, or an
  // integration) keeps a link to do it in Home Assistant.
  function ewSecondary(it) {
    const busy = ewBusy() ? ' disabled title="Another update is running"' : '';
    let extra = '';
    if (ewFixable(it) && !state.ewRuns[it.id] && noClaude()) extra += noClaudeNote('small');
    else if (ewFixable(it) && !state.ewRuns[it.id]) extra += `<button class="btn small ew-fix" data-action="ew-fix" data-id="${esc(it.id)}"${busy || ' title="Claude looks into it (Home Assistant state and logs), retries once, restarts Home Assistant only if a restart is pending"'}>Fix it</button>`;
    if (it.restart_pending && it.source === 'ha' && !state.ewRuns[it.id]) extra += `<button class="btn small ew-restart" data-action="ew-restart-ask" data-id="${esc(it.id)}"${busy || ' title="Restart Home Assistant so it loads the downloaded version (asks first)"'}>Restart HA</button>`;
    return extra + ewSecondaryRemove(it);
  }
  function ewSecondaryRemove(it) {
    if (ewRemovable(it)) {
      const why = ewBusy() ? ' disabled title="Another update is running"' : ` title="Uninstall ${esc(it.name)} from Home Assistant (asks first)"`;
      return `<button class="btn small ghost ew-remove" data-action="ew-remove" data-id="${esc(it.id)}"${why}>Remove</button>`;
    }
    if (!ewUnused(it)) return '';
    const url = ewRemoveLink(it);
    return url ? `<button class="btn small ghost ew-remove" data-action="open-url" data-url="${esc(url)}" title="Opens ${esc(it.name)} in Home Assistant, where you can uninstall it.">Remove in HA ↗</button>` : '';
  }
  // the Remove confirmation, in the style of "Update the safe ones"
  function renderEwRemoveConfirm() {
    const it = ewItems().find(i => i.id === state.ewRemoveAsk);
    if (!it) return '';
    const m = /\((0 [^)]*entities)\)/.exec(it.used_by || '');
    return `<div class="backdrop" data-action="ew-remove-cancel"><div class="modal small ew-remove-modal" role="dialog" aria-label="Remove ${esc(it.name)}" data-stop>
      <div class="modal-h"><img class="app-icon sm" src="rivet-icon.png" alt="" aria-hidden="true"><span class="t">Remove ${esc(it.name)}?</span>
        <button class="btn icon" data-action="ew-remove-cancel" aria-label="Close">${svgX}</button></div>
      <div class="modal-b"><p class="confirm-text"><b>Nothing in Home Assistant uses it</b>${m ? ` (${esc(m[1])})` : ''}. Rivet counts that again right before it removes anything, and stops if it changed.</p>
        <p class="confirm-text">Home Assistant uninstalls the add-on and deletes the data it keeps. Integrations that depended on it are not touched: they stay configured and will report that they cannot connect.</p>
        <div class="cmd mono">Home Assistant: supervisor/api POST /addons/${esc(it.addon_slug)}/uninstall</div>
        <div class="confirm-actions"><button class="btn" data-action="ew-remove-cancel">Cancel</button>
          <button class="btn danger" data-action="ew-remove-go" data-id="${esc(it.id)}">Remove</button></div></div>
    </div></div>`;
  }
  function renderEwRun(it, r) {
    const lines = r.lines.slice(-8);
    if (r.phase === 'running') {
      return `<div class="ew-run running" data-id="${esc(it.id)}"><div class="st"><span class="spin"></span>${esc(r.msg || 'Working…')}${r.pct && r.pct < 100 && !/%/.test(r.msg || '') ? ` <b>${r.pct}%</b>` : ''}</div>
        ${lines.length ? `<pre class="ew-log mono">${lines.map(esc).join('\n')}</pre>` : ''}</div>`;
    }
    if (r.ok && it.restart_pending && state.ewHaRestart) {     // installed, now Home Assistant restarts to load it
      return `<div class="ew-run running" data-id="${esc(it.id)}"><div class="st"><span class="spin"></span>${esc(r.summary || 'Downloaded.')} ${esc(state.ewHaRestart.msg || 'Restarting Home Assistant…')}</div></div>`;
    }
    const restart = r.restart ? `<button class="btn small ew-copy" data-action="ew-copy" data-text="${esc(r.restart)}" title="Copy: ${esc(r.restart)}">${svgCopy}Copy restart</button>` : '';
    const ha = r.kind === 'remove' && !r.ok && (r.link || ewRemoveLink(it));
    const fallback = ha ? `<button class="btn small ew-open" data-action="open-url" data-url="${esc(ha)}" title="${esc(ha)}">Open in Home Assistant ↗</button>` : '';
    const fixable = !r.ok && r.result !== 'refused' && r.result !== 'skipped' && r.kind !== 'remove';
    const fix = fixable && noClaude() ? noClaudeNote('small') : fixable ? `<button class="btn small ew-fix" data-action="ew-fix" data-id="${esc(it.id)}"${ewBusy() ? ' disabled title="Another update is running"' : ' title="Claude looks into it (state and logs), retries once, restarts Home Assistant only if a restart is pending"'}>${r.kind === 'fix' ? 'Try again' : 'Fix it'}</button>` : '';
    const skipped = r.result === 'skipped';       // nothing ran (HA went away mid-bulk): neutral, not red
    return `<div class="ew-run ${skipped ? 'skipped' : r.ok ? 'ok' : 'fail'}${r.kind === 'fix' ? ' fix' : ''}" data-id="${esc(it.id)}"><div class="st">${skipped ? '–' : r.ok ? '✓' : '✕'} ${esc(r.summary || (r.ok ? 'Done.' : 'It did not work.'))}${restart}${fallback}${fix}
        <button class="btn small ghost" data-action="ew-dismiss" data-id="${esc(it.id)}">Dismiss</button></div>
      ${!r.ok && lines.length ? `<pre class="ew-log mono">${lines.map(esc).join('\n')}</pre>` : ''}</div>`;
  }
  // "Restart Home Assistant?" in the row (the Restart HA button of a downloaded integration)
  function renderEwRestartConfirm(it) {
    return `<div class="ew-confirm ew-restart-confirm" data-id="${esc(it.id)}">
      <p><b>Restart Home Assistant now?</b> It loads ${esc(it.name)} ${esc(it.installed || '')}. About a minute; automations pause while it restarts. Rivet checks the configuration first and does not restart if it is broken.</p>
      <div class="cmd mono">Home Assistant: homeassistant.restart</div>
      <div class="btns"><button class="btn small" data-action="ew-cancel">Cancel</button><button class="btn small primary" data-action="ew-restart-go" data-id="${esc(it.id)}">Restart</button></div>
    </div>`;
  }
  function renderEwConfirm(it) {
    const ha = it.action === 'ha_install';
    return `<div class="ew-confirm" data-id="${esc(it.id)}">
      <p><b>Major update, ${esc(it.installed || '')} → ${esc(it.latest || '')}.</b> ${ha ? 'Home Assistant installs it on the HA VM; a major version can change how it behaves.' : 'A major version can change how it behaves.'}</p>
      ${it.warning ? `<p class="warn">${esc(it.warning)}</p>` : ''}
      <div class="cmd mono">${esc(ewDoes(it))}</div>
      <div class="btns"><button class="btn small" data-action="ew-cancel">Cancel</button><button class="btn small primary" data-action="ew-go" data-id="${esc(it.id)}">${ha ? 'Install anyway' : 'Update anyway'}</button></div>
    </div>`;
  }
  // One line per row: [source tag, This PC only] name, version, at most one muted hint, badge,
  // [Remove?], the action button, chevron. Everything else is in the details (the chevron).
  function renderEwRow(it, grp) {
    const open = state.ewOpen.has(it.id);
    const run = state.ewRuns[it.id];
    const hint = open ? null : ewHint(it);        // open: the details say it in full
    const queued = state.ewBatch && state.ewBatch.ids.includes(it.id) && !run;
    let html = `<div class="row ew-row ${open ? 'open' : ''}" data-action="ew-expand" data-id="${esc(it.id)}" data-status="${esc(it.status)}" data-severity="${esc(it.severity)}" data-act="${esc(it.action || '')}" data-safe="${ewSafe(it) ? 1 : 0}" role="button" aria-expanded="${open}">
      ${grp === 'pc' ? `<span class="src mono">${esc(EW_SRC[it.source] || it.source)}</span>` : ''}
      <div class="body"><div class="hd"><span class="name" title="${esc(it.name)}">${esc(ewName(it))}</span><span class="ver">${ewVersion(it)}</span></div>
        ${hint ? `<div class="ew-hint ${hint[0]}" title="${esc(hint[1])}">${hint[0] === 'warn' ? '<span class="ic" aria-hidden="true">⚠</span>' : ''}${esc(hint[1])}</div>` : ''}</div>
      ${ewBadge(it)}${ewSecondary(it)}<span class="ew-btn">${ewButton(it)}</span>${svgChev}</div>`;
    if (state.ewConfirm === it.id) html += renderEwConfirm(it);
    if (state.ewRestartAsk === it.id) html += renderEwRestartConfirm(it);
    if (run) html += renderEwRun(it, run);
    else if (queued) html += `<div class="ew-run queued" data-id="${esc(it.id)}"><div class="st">Waiting its turn (${state.ewBatch.ids.indexOf(it.id) + 1} of ${state.ewBatch.ids.length})</div></div>`;
    if (open) html += renderEwDetails(it);
    return html;
  }
  function renderEwDetails(it) {
    const src = ((elsewhere() || {}).sources || []).find(s => s.id === it.source) || {};
    const used = [it.used_by, it.where ? (it.used_by ? 'in ' : '') + it.where : ''].filter(Boolean).join(' · ');
    // HA / extension lines say where the thing runs; an AI line says what it is usually for
    const lbl = (it.ai || []).includes('used_by') ? 'Used for' : (!it.used_by || /^(ha|gnome-ext)$/.test(it.source)) ? 'Where' : 'Used by';
    let about = '';
    if (it.what) about += `<p class="ew-what">${esc(it.what)}${aiTag(it, 'what')}</p>`;
    if (used) about += `<p class="ew-used"><span class="lbl">${lbl}</span>${esc(used)}${aiTag(it, 'used_by')}</p>`;
    if (it.warning && it.status === 'ok') about += `<p class="ew-warn">${esc(it.warning)}</p>`;
    if (ewRemovable(it)) about += `<p class="small">Nothing in Home Assistant uses it, so it may be safe to remove. Remove uninstalls it from Home Assistant after you confirm.</p>`;
    else if (ewUnused(it)) about += `<p class="small">Nothing in Home Assistant uses it, so it may be safe to remove. Remove in HA opens its page in Home Assistant.</p>`;
    if ((it.status === 'failed' || it.status === 'cant_check' || it.pulled) && it.hint) about += `<p>${esc(it.hint)}</p>`;
    if (it.status === 'rebuilt') about += `<p class="small ew-rebuilt">${esc(it.hint || 'Image rebuilt upstream (same version).')}</p>`;
    if (it.source === 'podman' && it.newest_tag && it.newest_tag !== it.latest && it.status === 'ok') about += `<p class="small">Newest tag in the repo: ${esc(it.newest_tag)}.</p>`;
    if (!about) about = `<p>${esc(it.hint || 'Nothing more is known about it.')}</p>`;
    if ((it.ai || []).length) about += '<p class="small">Lines tagged AI were written by Claude (claude -p) and cached; nothing else on this box knew.</p>';
    const facts = [`${esc(src.title || it.source)}: ${esc(src.status || '?')}${src.checked != null ? `, ${src.checked} checked` : ''}`,
      it.checked_at ? `checked ${hhmm(it.checked_at)}` : '', it.kind ? `kind: ${esc(it.kind)}` : ''].filter(Boolean);
    const todo = it.status === 'rebuilt' ? 'Nothing to do: the version did not change.'
      : it.source === 'podman' && it.stateful ? (it.runbook ? `It holds data, so Rivet never updates it. Upgrade it by hand following the runbook (${it.runbook_title || 'see the link'}).` : 'It holds data, so Rivet never updates it. Upgrade it by hand (back up its data first).')
      : it.action === 'update' ? 'Update runs this as you (no root, no prompt) and shows the output here:'
      : it.action === 'ha_install' ? 'Install in HA asks Home Assistant to install it (update.install), then waits until it is done:'
      : it.source === 'ha' && /^(core|os|supervisor)$/.test(it.kind || '') ? 'This restarts or reboots the Home Assistant VM, so it stays a manual step: do it in Home Assistant.'
      : (it.status === 'failed' ? (src.error || it.hint || 'The check failed; the next check tries again.')
        : it.command ? 'Run this yourself in a terminal:' : (it.hint || 'Nothing Rivet can do from here.'));
    return `<div class="expand ew-expand" data-id="${esc(it.id)}">
        <div><h4>WHAT IT IS</h4><div class="why ew-about">${about}</div></div>
        <div><h4>WHAT TO DO</h4><div class="why"><p>${esc(todo)}</p>
          ${ewDoes(it) ? `<div class="notes">${esc(ewDoes(it))}</div>` : ''}
          ${it.command && it.action === 'update' ? `<p class="small"><a href="#" data-action="ew-copy" data-text="${esc(it.command)}">Copy the command</a> to run it yourself instead.</p>` : ''}</div>
          <div class="meta">${facts.map(f => `<span>${f}</span>`).join('')}${it.link ? `<a href="${esc(it.link)}" data-action="open-url" data-url="${esc(it.link)}">${esc(/\/config\/updates$/.test(it.link) ? 'open in Home Assistant ↗' : it.source === 'ha' ? 'release notes ↗' : it.source === 'podman' ? (/\/tags|tab=tags|pkgs\/container/.test(it.link) ? 'tags ↗' : 'release notes ↗') : 'project page ↗')}</a>` : ''}</div></div>
      </div>`;
  }
  // the safe rows of a group as the section shows them (filter applied)
  function ewGroupRows(gid, q) {
    const matches = (it) => !q || `${it.name} ${it.source} ${EW_SRC[it.source] || ''} ${it.command || ''} ${it.hint || ''} ${it.what || ''} ${it.used_by || ''}`.toLowerCase().includes(q);
    return ewSort(ewItems().filter(it => ewGroupOf(it) === gid && matches(it)));
  }
  function ewBulkControl(g, rows) {
    if (g.id === 'ha' && state.ewHaRestart) {
      return `<span class="ew-bulk-run ew-ha-restart"><span class="spin"></span><span>${esc(state.ewHaRestart.msg || 'Restarting Home Assistant…')}</span></span>`;
    }
    const b = state.ewBatch;
    if (b && b.group === g.id) {
      const i = Math.min(b.done.length + 1, b.ids.length);
      return `<span class="ew-bulk-run"><span class="spin"></span><span>Updating ${i} of ${b.ids.length}${b.stopping ? ', stopping after this one' : ''}</span>${b.stopping ? '' : `<button class="btn small ghost" data-action="ew-bulk-stop">Stop after this one</button>`}</span>`;
    }
    const safe = rows.filter(ewSafe);
    if (safe.length < 2) return '';      // one safe row already has its own Update button
    return `<button class="btn small ew-bulk" data-action="ew-bulk" data-group="${esc(g.id)}"${ewBusy() ? ' disabled title="Another update is running"'
      : ` title="${g.id === 'ha' ? 'Add-ons and integrations without a major version' : 'Patch releases and image pulls only'}: ${esc(safe.map(ewName).join(', '))}"`}>Update the safe ones (${safe.length})</button>`;
  }
  function renderElsewhereSection(q, hideEmpty) {
    const e = elsewhere();
    if (!e) return '';
    const all = ewItems();
    const groups = EW_GROUPS.filter(g => g.id !== 'containers' || ewShowContainers()).map(g => ({ g, rows: ewGroupRows(g.id, q) })).filter(x => x.rows.length);
    if (q && !groups.length) return '';
    if (hideEmpty && !all.length) return '';
    const failed = all.filter(i => i.status === 'failed').length;
    const cant = all.filter(i => i.status === 'cant_check').length;
    const checked = (e.sources || []).reduce((n, s) => n + (s.status === 'absent' || (s.id === 'podman' && !ewShowContainers()) ? 0 : (s.checked || 0)), 0);
    const note = [`Outside dnf and Flatpak`,
      failed ? `<span class="bad">${failed} check${failed === 1 ? '' : 's'} failed</span>` : '',
      cant ? `${cant} can’t check` : '', e.checked_at ? `checked ${hhmm(e.checked_at)}` : ''].filter(Boolean).join(' · ');
    const body = groups.length ? groups.map(({ g, rows }) => `<div class="ew-group" data-group="${esc(g.id)}">
        <div class="ew-sub"><span class="ew-sub-t">${esc(g.title)}<span class="n">${rows.length}</span></span>${ewBulkControl(g, rows)}</div>
        <div class="card ew-card ${esc(g.id)}">${rows.map(it => renderEwRow(it, g.id)).join('')}</div></div>`).join('')
      : `<div class="card ew-card"><div class="ew-empty">All ${checked} thing${checked === 1 ? '' : 's'} checked outside dnf and Flatpak ${checked === 1 ? 'is' : 'are'} current.</div></div>`;
    return `<div class="group-h ew-h"><span class="group-title">OTHER SOFTWARE<span class="n">${ewStale()}</span></span>
        <span class="ew-note">${note}</span></div>${body}`;
  }
  // "Update the safe ones (N)": one confirmation that lists exactly what it will touch
  function renderEwBulkConfirm() {
    const a = state.ewBulkAsk;
    if (!a) return '';
    const g = EW_GROUPS.find(x => x.id === a.group) || { title: 'Other software' };
    const rows = a.ids.map(id => ewItems().find(i => i.id === id)).filter(Boolean);
    const pulls = rows.filter(i => i.source === 'podman').length;
    const ha = rows.filter(i => i.action === 'ha_install').length;
    const on = (it) => !(a.off || []).includes(it.id);
    const picked = rows.filter(on);
    const integ = picked.filter(ewIntegration);
    return `<div class="backdrop" data-action="ew-bulk-cancel"><div class="modal small ew-bulk-modal" role="dialog" aria-label="Update the safe ones" data-stop>
      <div class="modal-h"><img class="app-icon sm" src="rivet-icon.png" alt="" aria-hidden="true"><span class="t">Update ${picked.length} in ${esc(g.title)}?</span>
        <button class="btn icon" data-action="ew-bulk-cancel" aria-label="Close">${svgX}</button></div>
      <div class="modal-b"><p class="confirm-text">One after another, each streaming into its own row. ${pulls === rows.length ? 'Image pulls only: nothing is restarted, and each row says what to restart afterwards.'
        : ha === rows.length ? 'Add-ons and integrations only: never Home Assistant itself, its OS or device firmware, and nothing with a major version (those ask in their own row).'
        : 'Patch releases' + (pulls ? ' and image pulls' : '') + ' only: nothing with a major version or a warning.'}</p>
        <ul class="bulk-list">${rows.map(it => `<li data-id="${esc(it.id)}" class="${on(it) ? '' : 'off'}"><div class="bl-hd"><span class="cb ${on(it) ? 'on' : ''}" data-action="ew-bulk-toggle" data-id="${esc(it.id)}" role="checkbox" aria-checked="${on(it)}" tabindex="0" title="${on(it) ? 'Leave this one out' : 'Include it'}"></span><span class="name">${esc(ewName(it))}</span><span class="ver mono">${ewVersion(it)}</span>${ewBadge(it)}</div>
          <div class="cmd mono">${esc(ewDoes(it))}</div></li>`).join('')}</ul>
        <p class="confirm-text fine">Untick any you want to leave out.</p>
        ${integ.length ? `<p class="confirm-text fine ew-bulk-restart">Then Home Assistant restarts once (about a minute) to load ${esc(integ.map(ewName).join(', '))}. Nothing else is restarted.</p>` : ''}
        ${pulls && pulls < rows.length ? `<p class="confirm-text fine">A pull restarts nothing: each row says what to restart afterwards.</p>` : ''}
        ${ha ? `<p class="confirm-text fine">Home Assistant takes a backup first where the add-on supports it.</p>` : ''}
        <div class="confirm-actions"><button class="btn" data-action="ew-bulk-cancel">Not now</button>
          <button class="btn primary" data-action="ew-bulk-go"${picked.length ? '' : ' disabled'}>Update ${picked.length}</button></div></div>
    </div></div>`;
  }

  // {"type":"other"} events from rivet-apply --other-update, streamed into the row
  function onOther(ev) {
    if (!ev) return;
    if (ev.step === 'batch') {          // the bridge's queue is empty: the bulk run is over
      const b = state.ewBatch, info = ev.batch || {};
      state.ewBatch = null;
      if (b && info.stopped) toast(`Stopped. ${b.ids.length - (info.ran || []).length} not started.`, false, 4000);
      render();
      return;
    }
    const id = ev.other_id || (ev.other && ev.other.id);
    if (!id) return;
    if (id === 'ha:_restart') { onHaRestart(ev); return; }
    const r = state.ewRuns[id] || (state.ewRuns[id] = { id, phase: 'running', lines: [], msg: '', pct: null });
    if (ev.step === 'done') {
      const o = ev.other || {};
      if (o.result === 'removed' && ev.status === 'ok') {   // Remove worked: the row goes
        if (state.doc && state.doc.elsewhere && Array.isArray(state.doc.elsewhere.items)) {
          state.doc.elsewhere.items = state.doc.elsewhere.items.filter(i => i.id !== id);
        }
        delete state.ewRuns[id]; state.ewOpen.delete(id);
        toast(ev.summary || 'Removed.', false, 5000);
        render();
        return;
      }
      r.link = o.link || r.link || null;
      r.phase = 'done'; r.ok = ev.status === 'ok'; r.result = o.result || (r.ok ? 'updated' : 'failed');
      r.summary = ev.summary || ev.msg || ''; r.restart = o.restart || null;
      if (o.item && state.doc && state.doc.elsewhere && Array.isArray(state.doc.elsewhere.items)) {
        state.doc.elsewhere.items = state.doc.elsewhere.items.map(i => i.id === o.item.id ? o.item : i);
      }
      const b = state.ewBatch;
      if (b && b.ids.includes(id)) {
        if (!b.done.includes(id)) b.done.push(id);
        if (b.done.length >= b.ids.length) state.ewBatch = null;     // belt and braces: no batch event needed
      } else if (o.result === 'refused' && ev.synthetic) toast(r.summary, false, 6000);
    } else {
      r.phase = 'running';
      if (ev.msg) r.msg = ev.msg;
      if (ev.pct != null) r.pct = ev.pct;
      const line = ev.log || (ev.status === 'fail' || ev.status === 'ok' ? ev.msg : null);
      if (line) { r.lines.push(String(line)); if (r.lines.length > 200) r.lines.splice(0, r.lines.length - 200); }
    }
    render();
  }

  // rivet-apply --ha-restart (2026-11-01): after an integration install, or the row's Restart HA
  function onHaRestart(ev) {
    if (ev.step !== 'done') {
      state.ewHaRestart = { phase: 'running', msg: ev.msg || (state.ewHaRestart || {}).msg || 'Restarting Home Assistant…' };
      render();
      return;
    }
    state.ewHaRestart = null;
    const o = ev.other || {}, ok = ev.status === 'ok';
    const items = Array.isArray(o.items) ? o.items : [];
    if (state.doc && state.doc.elsewhere && Array.isArray(state.doc.elsewhere.items)) {
      state.doc.elsewhere.items = state.doc.elsewhere.items.map(i => items.find(x => x.id === i.id) || i);
    }
    for (const it of items) {
      const good = !it.restart_pending;
      state.ewRuns[it.id] = { id: it.id, phase: 'done', lines: [], ok: good, result: good ? 'installed' : 'failed',
        summary: good ? `${it.name} ${it.loaded || it.installed || ''} is loaded: Home Assistant restarted.` : ((it.last_run || {}).note || ev.summary || 'Home Assistant did not load it.') };
    }
    if (!items.length || !ok) toast(ev.summary || (ok ? 'Home Assistant restarted.' : 'Home Assistant was not restarted.'), false, 6000);
    render();
  }
  function ewStartFix(it) {
    state.ewRuns[it.id] = { id: it.id, kind: 'fix', phase: 'running', lines: [], msg: 'Claude is looking into it…', pct: null };
    send({ cmd: 'otherFix', id: it.id });
  }
  function ewStartRemove(it) {
    state.ewRuns[it.id] = { id: it.id, kind: 'remove', phase: 'running', lines: [], msg: `Checking that nothing in Home Assistant uses ${it.name}…`, pct: null, link: ewRemoveLink(it) };
    send({ cmd: 'otherRemove', id: it.id });
  }
  function ewStart(it) {
    state.ewRuns[it.id] = { id: it.id, phase: 'running', lines: [], msg: it.action === 'ha_install' ? 'Asking Home Assistant…' : 'Starting…', pct: null };
    send({ cmd: 'otherUpdate', id: it.id });
  }

  /* ---- Held back (pinned with `flatpak mask`) ---- */
  const HELD_TAG = {
    pinned: ['grey', () => 'PINNED'],
    known_bad: ['red', (it) => `KNOWN BAD, STAYING ON ${it.good_version || it.from}`],
    candidate: ['yellow', () => 'NEW BUILD: TRY IT'],
    trial: ['yellow', () => 'TRIAL NOT CONFIRMED'],
  };
  function renderHeldSection() {
    const items = heldItems();
    if (!items.length) return '';                       // collapsed when empty
    return `<div class="group-h held-h"><span class="group-title">HELD BACK · PINNED<span class="n">${items.length}</span></span>
        <span class="held-note">Not part of “Update now”. Rivet tells you when a newer build is worth trying.</span></div>
      <div class="card held-card">${items.map(renderHeldRow).join('')}</div>`;
  }
  function renderHeldRow(it) {
    const [cls, text] = HELD_TAG[it.hold_state] || HELD_TAG.pinned;
    const open = state.expanded.has(it.id);
    const hasUpdate = it.to && it.to !== it.from;
    const ver = it.hold_state === 'trial' && it.trial
      ? `${esc(it.trial.from_version || '?')} → ${esc(it.trial.to_version || it.from)} (on trial)`
      : hasUpdate ? `${esc(it.from)} → ${esc(it.to)}` : esc(it.from);
    let btns = '';
    if (it.hold_state === 'candidate') {
      btns = `<button class="btn small primary" data-action="held-try" data-ref="${esc(it.ref)}">Try ${esc(it.to)}</button>`;
    } else if (it.hold_state === 'trial') {
      btns = `<button class="btn small" data-action="held-keep" data-ref="${esc(it.ref)}">It works, keep it</button>
        <button class="btn small danger" data-action="held-rollback" data-ref="${esc(it.ref)}">Roll back</button>`;
    }
    // one line like Other software: the reason is the hint, "held since" lives in the details
    const since = `${it.held_since ? 'held since ' + esc(it.held_since) : 'pinned by hand'}${it.trial && it.trial.stale ? ' · trial started ' + esc(String(it.trial.started_at || '').slice(0, 10)) + ' and never confirmed' : ''}`;
    let html = `<div class="row held-row ${open ? 'open' : ''}" data-action="expand" data-id="${esc(it.id)}" data-hold-state="${esc(it.hold_state)}" role="button" aria-expanded="${open}">
      <span class="risk ${cls}"></span>
      <div class="body"><div class="hd"><span class="name">${esc(it.display_name || it.name)}</span><span class="ver">${ver}</span></div>
        <div class="ew-hint held-reason" title="${esc(it.hold_reason || '')}">${esc(it.hold_reason || '')}</div></div>
      <span class="badge ${cls}">${esc(text(it))}</span>
      <span class="held-btns">${btns}</span>${svgChev}</div>`;
    if (open) {
      const bad = (it.bad_versions || []).join(', ') || 'none recorded';
      html += `<div class="expand">
        <div><h4>WHY IT IS PINNED</h4><div class="why"><p>${esc(it.hold_reason || '')}</p></div>
          <div class="meta"><span class="held-since">${since}</span><span>flatpak: ${esc(it.ref)}</span><span>good: ${esc(it.good_version || it.from)}</span><span>bad: ${esc(bad)}</span></div></div>
        <div><h4>WHAT RIVET DOES</h4><div class="why"><p>${esc(it.verdict || '')}</p>
          ${hasUpdate && (it.hold_state === 'known_bad' || it.hold_state === 'pinned') ? `<p><button class="btn small ghost" data-action="held-try" data-force="1" data-ref="${esc(it.ref)}">Try ${esc(it.to)} anyway</button></p>` : ''}</div></div>
      </div>`;
    }
    return html;
  }

  function startHeld(it, force) {
    return { ref: it.ref, name: it.display_name || it.name, from: it.from, to: it.to, force: !!force,
      label: it.verify_label || null, phase: 'confirm', msg: '', pct: null, log: [], result: null, verdict: false };
  }
  function heldFromTrial(it) {
    const h = startHeld(it, false);
    if (it.trial) { h.from = it.trial.from_version || h.from; h.to = it.trial.to_version || h.to; }
    return h;
  }

  function onHeld(ev) {
    if (!ev) return;
    const info = ev.held || {};
    let h = state.held;
    if (!h) {   // e.g. the app was reopened while a watcher was still running
      h = state.held = { ref: info.ref, name: info.display_name || 'the pinned build', from: info.from_version,
        to: info.to_version, label: info.label, force: false, phase: 'watching', msg: '', pct: null, log: [], result: null, verdict: false };
    }
    if (info.to_version) h.to = info.to_version;
    if (info.from_version) h.from = info.from_version;
    if (info.label) h.label = info.label;
    if (state.view !== 'held') state.view = 'held';
    if (ev.log || ev.msg) { h.log.push(String(ev.msg || ev.log)); if (h.log.length > 200) h.log.shift(); }

    if (ev.step !== 'done') {
      if (ev.verdict === 'ok' || ev.verdict === 'fail') { h.verdict = true; h.phase = 'working'; }
      if (ev.msg) h.msg = ev.msg;
      h.pct = ev.pct != null ? ev.pct : h.pct;
      render();
      return;
    }
    const result = info.result || (ev.status === 'ok' ? 'ok' : 'failed');
    if (info.action === 'try' && result === 'awaiting_verify') {
      h.phase = 'watching'; h.msg = ''; h.pct = null;
      send({ cmd: 'verifyHeld', ref: h.ref, timeout: 240 });
    } else {
      h.phase = 'result';
      h.result = { kind: result, action: info.action, summary: ev.summary || ev.msg || '', cause: ev.cause || '', status: ev.status };
    }
    render();
  }

  function renderHeld() {
    const h = state.held;
    if (!h) return renderEmpty();
    const label = h.label || 'it';
    if (h.phase === 'trying' || h.phase === 'working') {
      const t = h.phase === 'trying' ? `Unpinning ${h.name} and updating to ${h.to}` : (h.msg || 'Finishing…');
      return `<div class="progress indet"><i></i></div><div class="applying"><div class="center">
        <div class="steps"><div class="step active"><span class="ic"></span><div><div class="t">${esc(t)}…</div>${h.phase === 'trying' && h.msg ? `<div class="d">${esc(h.msg)}</div>` : ''}</div></div></div>
        <div class="applying-note">The previous build (${esc(h.from)}) is recorded. If anything goes wrong it comes back and gets pinned again.</div>
      </div></div>`;
    }
    if (h.phase === 'watching') {
      const pct = h.pct != null ? Math.max(0, Math.min(100, h.pct)) : 0;
      return `<div class="hero held-watch"><div class="inner">
        <div class="ring watch">◉</div>
        <h1>${h.label ? `Now launch ${esc(h.label)}. I’m watching.` : `${esc(h.name)} ${esc(h.to)} is installed. Test it.`}</h1>
        <p>${esc(h.name)} ${esc(h.to)} is installed and the pin is off for now. Start the game the way you always do; Rivet never launches it for you.</p>
        <div class="watch-status"><span class="spin"></span><span id="held-status">${esc(h.msg || `Waiting for you to launch ${label}…`)}</span></div>
        <div class="watch-bar"><i style="width:${pct}%"></i></div>
        <div class="actions">
          <button class="btn primary" data-action="held-keep" data-ref="${esc(h.ref)}">It works, keep it</button>
          <button class="btn danger big" data-action="held-rollback" data-ref="${esc(h.ref)}">It’s broken, roll back</button>
        </div>
        <p class="fine">If the game crashes or quits right away, Rivet rolls back to ${esc(h.from)} and pins it again by itself. Your click always wins over the watcher.</p>
      </div></div>`;
    }
    // result
    const r = h.result || { kind: 'failed', summary: '' };
    const T = {
      kept: ['', '✓', `${h.name} ${h.to} kept`],
      rolled_back: ['warn', '↩', `Rolled back to ${h.from}`],
      inconclusive: ['warn', '?', 'Not tested yet'],
      refused: ['warn', '!', 'Not now'],
      failed: ['bad', '!', r.action === 'rollback' ? 'Rollback failed' : 'That did not work'],
    }[r.kind] || ['bad', '!', 'That did not work'];
    const again = r.kind === 'inconclusive'
      ? `<button class="btn" data-action="held-watch" data-ref="${esc(h.ref)}">Watch again</button>
         <button class="btn" data-action="held-keep" data-ref="${esc(h.ref)}">It works, keep it</button>
         <button class="btn danger big" data-action="held-rollback" data-ref="${esc(h.ref)}">Roll back</button>` : '';
    const retry = r.kind === 'failed' && r.action === 'rollback'
      ? `<button class="btn danger big" data-action="held-rollback" data-ref="${esc(h.ref)}">Try the rollback again</button>` : '';
    return `<div class="hero result held-result ${T[0]}" data-result="${esc(r.kind)}"><div class="inner">
      <div class="ring ${T[0]}">${T[1]}</div>
      <h1>${esc(T[2])}</h1>
      <p class="cause">${esc(r.summary)}</p>
      ${r.cause && r.cause !== r.summary ? `<div class="tally">${r.kind === 'rolled_back' ? 'Why: ' : ''}${esc(r.cause)}</div>` : ''}
      <div class="actions">${again}${retry}${r.kind === 'refused' && /Quit Steam/.test(r.summary) ? `<button class="btn danger big" data-action="quit-steam">Quit Steam</button>` : ''}<button class="btn ${again || retry ? '' : 'primary'}" data-action="held-done">${r.kind === 'inconclusive' ? 'Decide later' : 'Done'}</button></div>
    </div></div>`;
  }

  function renderHeldConfirm() {
    const h = state.held;
    if (!h) return '';
    const label = h.label || 'it';
    return `<div class="backdrop" data-action="held-cancel"><div class="modal small" role="dialog" aria-label="Try the new build" data-stop>
      <div class="modal-h"><img class="app-icon sm" src="rivet-icon.png" alt="" aria-hidden="true"><span class="t">Try ${esc(h.name)} ${esc(h.to)}?</span>
        <button class="btn icon" data-action="held-cancel" aria-label="Close">${svgX}</button></div>
      <div class="modal-b"><p class="confirm-text">Rivet removes the pin, updates ${esc(h.name)} from ${esc(h.from)} to ${esc(h.to)}, then watches while you launch ${esc(label)} yourself.
        If the game fails to start, it rolls back to ${esc(h.from)}, pins it again and marks ${esc(h.to)} as bad, automatically.</p>
        ${h.force ? `<p class="confirm-text warn">${esc(h.to)} is already marked bad. This tries it anyway.</p>` : ''}
        <div class="confirm-actions"><button class="btn" data-action="held-cancel">Not now</button>
          <button class="btn primary" data-action="held-confirm">Try ${esc(h.to)}</button></div></div>
    </div></div>`;
  }

  // critical security first, then red, yellow, green; stable otherwise
  const RANK = { red: 1, yellow: 2, green: 3 };
  function sortItems(items) {
    return items.map((it, i) => [it, i]).sort((a, b) => {
      const ca = isCritical(a[0]) ? 0 : 1, cb = isCritical(b[0]) ? 0 : 1;
      if (ca !== cb) return ca - cb;
      const ra = RANK[effRisk(a[0])] || 9, rb = RANK[effRisk(b[0])] || 9;
      if (ra !== rb) return ra - rb;
      return a[1] - b[1];
    }).map(p => p[0]);
  }

  // the badge pill of an item, or of a cluster summary ({crit, risk, badge})
  function badgeHtml(x) {
    const cls = x.crit ? 'critical' : x.risk === 'red' ? 'red' : x.risk === 'yellow' ? 'yellow' : 'green';
    const text = x.crit ? 'SECURITY' : (BADGE_TEXT[x.badge] || (x.badge ? String(x.badge).toUpperCase() : ''));
    return text ? `<span class="badge ${cls}">${esc(text)}</span>` : '';
  }
  const KERNEL_BANNER = `<div class="banner yellow"><span class="ic">⚠</span><span>Kernel + NVIDIA akmod: the NVIDIA module must rebuild before reboot. Rivet waits for it and verifies it.</span></div>`;
  function releaseBanner(it) {
    const r = it.release || {};
    const v = esc(it.to);
    if (r.downloaded) return `<div class="banner yellow"><span class="ic">⚠</span><span>Fedora ${v} is already downloaded. Running it again only checks the download, then offers the restart.</span></div>`;
    return `<div class="banner yellow"><span class="ic">⚠</span><span>Fedora ${v} runs last: Rivet applies the other updates, downloads the release while you keep working, then asks before restarting into the installer (20 to 60 minutes with no desktop).</span></div>`;
  }

  function renderRow(it) {
    const blocked = isBlocked(it);
    const on = state.selected.has(it.id);
    const open = state.expanded.has(it.id);
    const risk = effRisk(it);
    const label = riskLabel(it);
    const cve = it.advisory && /^CVE-/.test(it.advisory.id) ? ' · ' + it.advisory.id : '';
    const release = isRelease(it);
    let html = `<div class="row ${blocked ? 'blocked' : ''} ${open ? 'open' : ''} ${release ? 'release' : ''}" data-action="expand" data-id="${esc(it.id)}" role="button" aria-expanded="${open}">
      <span class="cb ${on ? 'on' : ''} ${blocked ? 'disabled' : ''}" data-action="toggle-row" data-id="${esc(it.id)}" role="checkbox" aria-checked="${on}" tabindex="0" title="${blocked ? 'Blocked while Steam runs' : release ? 'Include the release upgrade in this run (it runs last)' : 'Include in this run'}"></span>
      <span class="risk ${risk}" title="${esc(label)}"></span>
      <div class="body"><div class="hd"><span class="name">${esc(it.display_name || it.name)}</span><span class="ver">${esc(it.from)} → ${esc(it.to)}${esc(cve)}</span></div>
        <div class="verdict">${esc(it.verdict || '')}</div></div>
      ${badgeHtml({ crit: isCritical(it), risk, badge: it.badge })}
      <span class="size" ${release ? 'title="The exact size is known once the download starts"' : ''}>${release ? '' : fmtBytes(it.size_bytes)}</span>${svgChev}</div>`;
    if (open) html += renderDetails(it, label, null);
    if (!blocked && it.name === 'kernel' && flags().akmod_nvidia_pending) html += KERNEL_BANNER;
    if (release && on) html += releaseBanner(it);
    return html;
  }

  // Release notes + reasoning of an item; for a cluster, those of its worst member (the
  // verdict and reasoning are per cluster in the triage, identical across members).
  function renderDetails(it, label, cluster) {
    const adv = it.advisory ? it.advisory.id + (it.advisory.severity ? ' · ' + it.advisory.severity : '') : '—';
    const what = cluster
      ? `${esc(it.source)}: ${cluster.members.map(m => esc(m.name || m.display_name)).join(', ')}`
      : `${esc(it.source)}: ${esc(it.ref || it.name)}`;
    return `<div class="expand">
        <div><h4>RELEASE NOTES · EXCERPT</h4><div class="notes">${esc(it.notes_excerpt || 'No notes available.')}</div>
          <div class="meta"><span>${what}</span><span>advisory: ${esc(adv)}</span>
          ${it.notes_url ? `<a href="${esc(it.notes_url)}" data-action="open-url" data-url="${esc(it.notes_url)}">full changelog ↗</a>` : ''}</div></div>
        <div><h4>WHY ${esc(label.toUpperCase())}</h4><div class="why"><p>${esc(it.reasoning || '')}</p></div></div>
      </div>`;
  }

  /* ---- Clusters (System section): one row per advisory / package family ---- */
  // critical first, then worst colour, then document order; same order as the items
  function sortClusters(xs) {
    return xs.map((x, i) => [x, i]).sort((a, b) => {
      const ca = a[0].info.crit ? 0 : 1, cb = b[0].info.crit ? 0 : 1;
      if (ca !== cb) return ca - cb;
      const ra = RANK[a[0].info.risk] || 9, rb = RANK[b[0].info.risk] || 9;
      if (ra !== rb) return ra - rb;
      return a[1] - b[1];
    }).map(p => p[0]);
  }

  function renderCluster(x, q) {
    const { c, info } = x;
    if (c.members.length === 1) return renderRow(c.members[0]);
    const rep = info.rep;
    const pool = c.members.filter(selectable);
    const on = pool.length > 0 && pool.every(it => state.selected.has(it.id));
    const mixed = !on && pool.some(it => state.selected.has(it.id));
    const open = state.expanded.has(c.id);                                    // notes + reasoning
    const listOpen = state.openClusters.has(c.id) || (!!q && x.hit.length > 0);   // member list; a filter hit opens it
    const label = riskLabel(rep);
    const n = c.members.length;
    const ver = info.sameFrom && info.sameTo ? `${esc(rep.from)} → ${esc(rep.to)}`
      : info.sameTo ? `→ ${esc(rep.to)}` : `${n} versions`;
    const members = sortItems(q ? x.hit : c.members);
    let html = `<div class="row cluster ${info.blocked ? 'blocked' : ''} ${open ? 'open' : ''} ${listOpen ? 'list-open' : ''}" data-action="expand" data-id="${esc(c.id)}" data-members="${n}" role="button" aria-expanded="${open}">
      <span class="cb ${on ? 'on' : mixed ? 'mixed' : ''} ${pool.length ? '' : 'disabled'}" data-action="toggle-row" data-id="${esc(c.id)}" role="checkbox" aria-checked="${on ? 'true' : mixed ? 'mixed' : 'false'}" tabindex="0" title="${pool.length ? `Include all ${n} packages in this run` : 'Blocked while Steam runs'}"></span>
      <span class="risk ${info.risk}" title="${esc(label)}"></span>
      <div class="body"><div class="hd"><span class="name">${esc(info.name)}</span><span class="pkgs" data-action="expand-cluster" data-id="${esc(c.id)}" data-open="${listOpen ? 1 : 0}" role="button" aria-expanded="${listOpen}" title="${listOpen ? 'Hide' : 'Show'} the packages">· ${n} packages</span><span class="ver">${ver}</span></div>
        <div class="verdict">${esc(rep.verdict || '')}</div></div>
      ${badgeHtml(info)}
      <span class="size">${fmtBytes(info.size)}</span>
      <span class="chev-btn" data-action="expand-cluster" data-id="${esc(c.id)}" data-open="${listOpen ? 1 : 0}" role="button" aria-expanded="${listOpen}" title="${listOpen ? 'Hide' : 'Show'} the packages">${svgChev}</span></div>`;
    if (open) html += renderDetails(rep, label, c);
    if (listOpen) html += `<div class="members">${members.map(renderMember).join('')}</div>`;
    if (!info.blocked && c.members.some(it => it.name === 'kernel') && flags().akmod_nvidia_pending) html += KERNEL_BANNER;
    return html;
  }

  // a package inside an open cluster: its own checkbox, version, badge and details
  function renderMember(it) {
    const blocked = isBlocked(it);
    const on = state.selected.has(it.id);
    const open = state.expanded.has(it.id);
    const risk = effRisk(it);
    const label = riskLabel(it);
    let html = `<div class="member ${blocked ? 'blocked' : ''} ${open ? 'open' : ''}" data-action="expand" data-id="${esc(it.id)}" role="button" aria-expanded="${open}">
      <span class="cb ${on ? 'on' : ''} ${blocked ? 'disabled' : ''}" data-action="toggle-row" data-id="${esc(it.id)}" role="checkbox" aria-checked="${on}" tabindex="0" title="${blocked ? 'Blocked while Steam runs' : 'Include in this run'}"></span>
      <span class="risk ${risk}" title="${esc(label)}"></span>
      <span class="name">${esc(it.display_name || it.name)}</span><span class="ver">${esc(it.from)} → ${esc(it.to)}</span>
      <span class="spacer"></span>
      ${badgeHtml({ crit: isCritical(it), risk, badge: it.badge })}
      <span class="size">${fmtBytes(it.size_bytes)}</span>${svgChev}</div>`;
    if (open) html += renderDetails(it, label, null);
    return html;
  }

  // The routine clusters of a group behind one collapsed row at the end of its card.
  function renderRoutine(g, routine, q) {
    const members = routine.reduce((a, x) => a.concat(x.c.members), []);
    const pool = members.filter(selectable);
    const on = pool.length > 0 && pool.every(it => state.selected.has(it.id));
    const mixed = !on && pool.some(it => state.selected.has(it.id));
    const open = state.routineOpen || !!q;                                    // a filter hit opens it
    const size = members.reduce((s, it) => s + (it.size_bytes || 0), 0);
    const id = 'routine:' + g.id;
    const n = members.length;
    let html = `<div class="row routine ${open ? 'open' : ''}" data-action="toggle-routine" data-group="${esc(g.id)}" data-open="${open ? 1 : 0}" data-members="${n}" role="button" aria-expanded="${open}">
      <span class="cb ${on ? 'on' : mixed ? 'mixed' : ''} ${pool.length ? '' : 'disabled'}" data-action="toggle-row" data-id="${esc(id)}" role="checkbox" aria-checked="${on ? 'true' : mixed ? 'mixed' : 'false'}" tabindex="0" title="Include all ${n} routine packages in this run"></span>
      <span class="risk green" title="Safe"></span>
      <div class="body"><div class="hd"><span class="name">Routine updates</span><span class="pkgs">· ${n} package${n === 1 ? '' : 's'} in ${routine.length} update${routine.length === 1 ? '' : 's'}</span></div>
        <div class="verdict">Bug fixes and small version bumps, all rated safe: nothing here needs a reboot or carries a security advisory.</div></div>
      <span class="size">${fmtBytes(size)}</span>${svgChev}</div>`;
    if (open) html += `<div class="routine-body">${routine.map(x => renderCluster(x, q)).join('')}</div>`;
    return html;
  }

  /* ---- Applying ---- */
  function renderApplying() {
    const a = state.apply || (state.apply = newApply([...state.selected]));
    const nDone = a.steps.filter(s => s.state === 'done' || s.state === 'skip').length;
    const cur = a.steps.find(s => s.state === 'active');
    const overall = a.steps.length ? (nDone + (cur && cur.pct != null ? cur.pct / 100 : 0)) / a.steps.length : 0;
    const rows = a.steps.map(s => {
      const st = s.state;
      const pct = st === 'active' && s.pct != null ? `<span class="pct">${Math.round(s.pct)}%</span>` : '';
      const ic = st === 'done' ? (s.warned ? '!' : '✓') : st === 'fail' ? '!' : st === 'skip' ? '–' : '';
      return `<div class="step ${st} ${s.warn ? 'warn' : ''} ${s.warned ? 'warned' : ''}"><span class="ic">${ic}</span>
        <div><div class="t">${esc(s.label)}${st === 'active' ? '…' : ''}</div>${s.detail ? `<div class="d">${esc(s.detail)}</div>` : ''}</div>${pct}</div>`;
    }).join('');
    const tools = a.steps.map(s => ({ download: 'dnf5', install: 'dnf5', akmod: 'akmods', flatpak: 'flatpak', fwupd: 'fwupd', verify: 'modinfo', release: 'dnf5 system-upgrade' })[s.id] || s.id)
      .filter((v, i, arr) => arr.indexOf(v) === i).join(' · ');
    const note = a.byId.akmod
      ? '⚠ Rivet will not offer a restart until the NVIDIA module for the new kernel has been built and verified.'
      : a.byId.release
      ? 'The new Fedora is only downloaded here. Nothing restarts until you say so.'
      : 'You can keep using the computer while this runs.';
    return `<div class="progress"><i style="width:${Math.round(overall * 100)}%"></i></div>
      <div class="applying"><div class="center">
        <div class="steps">${rows}</div>
        <div class="applying-note">${note}</div>
      </div></div>
      <div class="drawer ${a.drawer ? '' : 'closed'}">
        <div class="drawer-h" data-action="toggle-drawer"><span class="tri">▼</span><span class="t">Details</span><span class="s">${esc(tools)}</span><kbd>Esc closes</kbd></div>
        <div class="log">${a.log.map((l, i) => `<div class="${l.cls} ${i === a.log.length - 1 && cur ? 'cur' : ''}">${esc(l.line)}</div>`).join('') || '<div class="dim">Waiting for the transaction…</div>'}</div>
      </div>`;
  }

  /* ---- Done ---- */
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

  // "226 of 226 packages installed · 3 Flatpaks updated · firmware staged"
  function tally(r) {
    const R = r.results || {};
    const parts = [];
    if (R.dnf && R.dnf.planned) parts.push(`${R.dnf.installed} of ${plural(R.dnf.planned, 'package')} installed`);
    if (R.flatpak) {
      const f = R.flatpak, bits = [];
      if (f.ok || (!f.failed && !f.skipped)) bits.push(`${plural(f.ok, 'Flatpak')} updated`);
      if (f.failed) bits.push(`${plural(f.failed, 'Flatpak')} failed`);
      if (f.skipped) bits.push(`${f.skipped} skipped (Steam running)`);
      parts.push(bits.join(', '));
    }
    if (R.fwupd) {
      if (R.fwupd.ok) parts.push(R.fwupd.ok === 1 ? 'firmware staged' : `${R.fwupd.ok} firmware updates staged`);
      if (R.fwupd.failed) parts.push(`${plural(R.fwupd.failed, 'firmware update')} failed`);
    }
    return parts.join(' · ');
  }

  function tildePath(p) { return String(p || '').replace(/^\/home\/[^/]+\//, '~/').replace(/^\/var\/home\/[^/]+\//, '~/'); }

  function renderDone() {
    const a = state.apply || newApply([]);
    const r = a.result || {};
    const failed = a.failed;
    const warned = !!a.warned;
    const reboot = !!r.reboot_required;
    const kmodBad = r.kmod_verified === false;
    const n = a.planned.length;
    const R = r.results || {};
    const someLanded = !!((R.dnf && R.dnf.installed) || (R.flatpak && R.flatpak.ok) || (R.fwupd && R.fwupd.ok));
    // the newer Fedora is downloaded and waits for the restart (rivet-apply done.release)
    const rel = r.release && r.release.downloaded ? r.release : null;

    // 1. one plain sentence: what happened
    let cause;
    if (failed) cause = r.cause || r.summary || 'Something went wrong; the details below have the tool output.';
    else if (warned) cause = r.cause || (r.warnings || [])[0] || r.summary || '';
    else if (rel) cause = `${plural(n - 1, 'update')} applied. Fedora ${esc(rel.version)} downloaded.`;
    else cause = `${plural(n, 'update')} applied.`;

    // 2. what succeeded vs failed
    const counts = tally(r);
    const miss = (R.dnf && R.dnf.missing) || [];
    const missing = miss.length
      ? `Not installed: ${miss.slice(0, 8).join(', ')}${miss.length > 8 ? ` and ${miss.length - 8} more` : ''}` : '';

    // 3. reboot: needed? safe?
    let rb, rbCls = '';
    if (rel && !kmodBad) {
      rb = `Fedora ${rel.version} is downloaded${rel.size ? ` (${rel.size})` : ''}. Restart to install it: the screen shows text progress for 20 to 60 minutes, then the computer comes back on Fedora ${rel.version}.`
        + (rel.nvidia ? ' The NVIDIA module rebuilds on that first start, which adds a few minutes.' : '');
      rbCls = failed ? '' : 'good';
    } else if (kmodBad) {
      rb = 'Do NOT restart yet: the NVIDIA kernel module could not be verified for the new kernel, and you would boot without a display driver.'; rbCls = 'bad';
    } else if (reboot && failed && someLanded) {
      rb = 'The parts that did install need a restart to take effect.'
        + (r.kmod_verified === true ? ' The NVIDIA module is verified for the new kernel, so restarting is safe.' : '');
    } else if (reboot && r.kmod_verified === true) {
      rb = 'A restart is needed to finish. The NVIDIA module was rebuilt and verified for the new kernel — it is safe to restart.'; rbCls = 'good';
    } else if (reboot && failed && !someLanded) {
      rb = 'Nothing changed, so there is nothing to restart for.';
    } else if (reboot) {
      rb = 'A restart is needed to finish. No NVIDIA module rebuild was involved, so restarting is safe.';
    } else {
      rb = r.synthetic ? '' : 'No restart needed.';
    }

    // 4. details: the tail of the log + where the whole thing lives
    const tail = a.log.slice(-30);
    const logPath = tildePath(r.log_path || '~/.local/state/rivet/logs/apply.log');
    const details = `<div class="details ${a.details ? 'open' : ''}">
        <div class="details-h" data-action="toggle-details"><span class="tri">▶</span>${a.details ? 'Hide details' : 'Show details'}</div>
        ${a.details ? `<div class="details-b">
          ${r.error ? `<div class="firsterr"><b>First error</b> ${esc(r.error)}</div>`
            : r.last_msg ? `<div class="firsterr"><b>Last message (${esc(r.last_step || 'unknown step')})</b> ${esc(r.last_msg)}</div>` : ''}
          <div class="log">${tail.map(l => `<div class="${l.cls}">${esc(l.line)}</div>`).join('') || '<div class="dim">No tool output was captured.</div>'}</div>
          <div class="logpath">Last ${tail.length} lines · full log: <code>${esc(logPath)}</code></div>
        </div>` : ''}
      </div>`;

    // "Fix it" = ask Claude (read-only) what happened and what to do; it proposes a fix
    // that runs only on a second click. Check again merely re-runs the collector.
    const fixBtn = (cls) => noClaude() ? noClaudeNote() : `<button class="btn ${cls}" data-action="fix-it" title="Ask Claude to look into it and propose a fix">Fix it</button>`;
    const restartBtns = `<button class="btn primary" data-action="restart-now" ${rel ? `data-release="${esc(rel.version)}"` : ''} ${kmodBad ? 'disabled title="Blocked: the NVIDIA module was not verified"' : rel ? `title="Restart into the Fedora ${esc(rel.version)} installer"` : ''}>Restart now</button><button class="btn" data-action="restart-later">Later</button>`;
    const actions = failed && rel
      ? `${restartBtns}<button class="btn" data-action="back-to-list">Back to updates</button>${fixBtn('')}`
      : failed
      ? `<button class="btn" data-action="back-to-list">Back to updates</button><button class="btn" data-action="check-now">Check again</button>${fixBtn('primary')}`
      : kmodBad ? `${restartBtns}${fixBtn('primary')}`
      : reboot ? restartBtns + (warned ? fixBtn('') : '')
      : `<button class="btn primary" data-action="restart-later">Done</button>${warned ? fixBtn('') : ''}`;
    const tone = failed || kmodBad ? 'bad' : warned ? 'warn' : '';
    const title = failed ? (someLanded ? 'Some updates failed' : 'Update failed')
      : kmodBad ? 'Applied — do not reboot yet'
      : warned ? 'Updated, with a warning' : rel ? `Ready for Fedora ${esc(rel.version)}` : 'All done';
    return `<div class="hero result ${tone}"><div class="inner">
      <div class="ring ${tone}">${failed ? '!' : kmodBad ? '⚠' : warned ? '!' : '✓'}</div>
      <h1>${title}</h1>
      <p class="cause">${esc(cause)}</p>
      ${counts ? `<div class="tally">${esc(counts)}</div>` : ''}
      ${missing ? `<div class="missing">${esc(missing)}</div>` : ''}
      ${rb ? `<div class="rebootline ${rbCls}">${esc(rb)}</div>` : ''}
      <div class="actions">${actions}</div>
      ${failed || warned || a.details ? details : ''}
    </div></div>`;
  }

  /* ---- Fix it (rivet-doctor) ---- */
  function newDoctor(mode) {
    return { mode, phase: mode === 'run' ? 'fixing' : 'working', log: [], msg: 'Starting…', pct: null, step: null,
      diagnosis: null, fixPath: null, error: null, result: null, recheck: null, details: false };
  }

  function onDoctor(mode, ev) {
    if (!ev) return;
    let d = state.doctor;
    if (!d) d = state.doctor = newDoctor(mode || 'diagnose');
    if (state.view !== 'doctor') state.view = 'doctor';
    const line = ev.log || ev.msg;
    if (line) pushLog(d, line, ev.status === 'fail' ? 'warn' : ev.status === 'ok' ? 'ok' : /^\$ /.test(String(ev.log || '')) ? 'cmd' : /^\(not allowed/.test(String(ev.log || '')) ? 'dim' : '');
    if (mode === 'recheck') {
      // the bridge re-ran `rivet-apply --verify-kmod` after a "nothing to run, safe to restart"
      // diagnosis of a run whose verify had failed (2026-10-12: it had raced depmod)
      if (ev.step !== 'done') { d.recheck = d.recheck || { pending: true }; render(); return; }
      d.recheck = { pending: false, ok: ev.kmod_verified === true, reboot: !!ev.reboot_required,
        summary: ev.summary || ev.cause || ev.msg || '', kver: ev.kver || '' };
      render();
      return;
    }
    if (ev.step !== 'done') {
      if (ev.msg) d.msg = ev.msg;
      if (ev.pct != null) d.pct = ev.pct;
      if (ev.step) d.step = ev.step;
      render();
      return;
    }
    if ((mode || d.mode) === 'diagnose') {
      d.phase = 'result';
      if (ev.status === 'ok' && ev.diagnosis) { d.diagnosis = ev.diagnosis; d.fixPath = ev.fix_path || null; d.error = null; }
      else { d.diagnosis = null; d.error = ev.cause || ev.summary || ev.msg || 'No diagnosis came back.'; }
    } else {
      d.phase = 'fixed';
      d.result = { ok: ev.status === 'ok' || ev.status === 'warn', warn: ev.status === 'warn', summary: ev.summary || ev.msg || '', cause: ev.cause || '', results: ev.results || {} };
    }
    d.logPath = ev.log_path || d.logPath;
    render();
  }

  function doctorDetails(d, extra) {
    const tail = d.log.slice(-40);
    const logPath = tildePath(d.logPath || '~/.local/state/rivet/logs/doctor.log');
    return `<div class="details ${d.details ? 'open' : ''}">
        <div class="details-h" data-action="toggle-doctor-details"><span class="tri">▶</span>${d.details ? 'Hide details' : 'Show details'}</div>
        ${d.details ? `<div class="details-b">${extra || ''}
          <div class="log">${tail.map(l => `<div class="${l.cls}">${esc(l.line)}</div>`).join('') || '<div class="dim">Nothing was logged.</div>'}</div>
          <div class="logpath">Last ${tail.length} lines · full log: <code>${esc(logPath)}</code></div>
        </div>` : ''}
      </div>`;
  }

  function renderDoctor() {
    const d = state.doctor;
    if (!d) return renderEmpty();
    if (d.phase === 'working' || d.phase === 'fixing') {
      const fixing = d.phase === 'fixing';
      const title = fixing ? (d.step === 'verify' ? 'Checking that it worked' : 'Running the fix')
        : (d.pct != null && d.pct >= 30 ? 'Claude is looking into it' : 'Collecting evidence');
      const note = fixing ? 'Each step streams below. If a step fails, Rivet stops there and tells you which one.'
        : 'Claude Code gets read-only tools: it can inspect logs, packages and the journal, but cannot change anything on this pass.';
      const bar = d.pct != null ? `<div class="progress"><i style="width:${Math.max(2, Math.min(100, d.pct))}%"></i></div>` : `<div class="progress indet"><i></i></div>`;
      return `${bar}<div class="applying"><div class="center">
        <div class="steps"><div class="step active"><span class="ic"></span><div><div class="t">${esc(title)}…</div><div class="d">${esc(d.msg || '')}</div></div></div></div>
        <div class="applying-note">${esc(note)}</div>
      </div></div>
      <div class="drawer"><div class="drawer-h"><span class="tri">▼</span><span class="t">Details</span><span class="s">${fixing ? 'bash · pkexec' : 'claude · rpm · dnf5 · journalctl'}</span></div>
        <div class="log">${d.log.map((l, i) => `<div class="${l.cls} ${i === d.log.length - 1 ? 'cur' : ''}">${esc(l.line)}</div>`).join('') || '<div class="dim">Starting…</div>'}</div></div>`;
    }
    if (d.phase === 'result') {
      const g = d.diagnosis;
      if (!g) {
        return `<div class="hero result bad doctor-result" data-result="nodiag"><div class="inner">
          <div class="ring bad">!</div><h1>Could not get a diagnosis</h1>
          <p class="cause">${esc(d.error || '')}</p>
          <div class="actions"><button class="btn" data-action="back-to-list">Back to updates</button><button class="btn" data-action="check-now">Check again</button><button class="btn primary" data-action="fix-it">Try again</button></div>
          ${doctorDetails(d)}
        </div></div>`;
      }
      const tone = g.severity === 'broken' ? 'bad' : g.severity === 'attention' ? 'warn' : '';
      const rb = g.reboot || {};
      const rbLine = rb.safe === 'yes' ? ['good', 'Safe to restart. ' + (rb.why || '')]
        : rb.safe === 'no' ? ['bad', 'Do NOT restart yet. ' + (rb.why || '')]
        : ['', 'Restart: not sure. ' + (rb.why || '')];
      const rc = d.recheck;
      const recheckLine = !rc ? '' : rc.pending ? '<div class="rebootline">Checking the NVIDIA module again…</div>'
        : rc.ok ? `<div class="rebootline good">Checked again just now: ${esc(rc.summary)}</div>`
        : `<div class="rebootline bad">Checked again just now, still not verified. ${esc(rc.summary)}</div>`;
      const canRestart = !!(rc && rc.ok && rc.reboot);
      const fx = g.fix || {};
      const fixCard = fx.available
        ? `<div class="fixcard" data-fix="available">
            <div class="fx-h"><span>${esc(fx.title || 'Proposed fix')}</span><span class="chip risk-${esc(fx.risk || 'medium')}"><span class="dot"></span>${esc((fx.risk || 'medium').toUpperCase())} RISK</span>${fx.needs_root ? '<span class="chip"><span class="dot"></span>NEEDS ROOT</span>' : ''}</div>
            <div class="fx-x">${esc(fx.explanation || '')}</div>
            <pre class="cmds">${(fx.commands || []).map(c => esc(c)).join('\n')}</pre>
            ${fx.reversible ? `<div class="fx-r">Undo: ${esc(fx.reversible)}</div>` : ''}
          </div>`
        : `<div class="fixcard" data-fix="none"><div class="fx-h"><span>Nothing to run</span></div><div class="fx-x">${esc(fx.explanation || 'No command would help here.')}</div></div>`;
      const evidence = (g.evidence || []).length ? `<ul class="evidence">${g.evidence.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : '';
      const meta = g._meta || {};
      const metaLine = meta.model ? `Claude (${esc(meta.model)}) · ${meta.checks || 0} checks · ${Math.round((meta.duration_ms || 0) / 1000)}s · confidence ${esc(g.confidence || '?')}` : '';
      return `<div class="hero result ${tone} doctor-result" data-result="diagnosis"><div class="inner">
        <div class="ring ${tone}">${tone === 'bad' ? '!' : tone === 'warn' ? '!' : '✓'}</div>
        <h1>${esc(g.headline || 'Diagnosis')}</h1>
        <p class="cause">${esc(g.what_happened || '')}</p>
        <div class="rebootline ${rbLine[0]}">${esc(rbLine[1])}</div>
        ${recheckLine}
        <div class="actions"><button class="btn" data-action="back-to-list">Back to updates</button><button class="btn ${fx.available || canRestart ? '' : 'primary'}" data-action="check-now">Check again</button>${fx.available ? '<button class="btn primary" data-action="run-fix">Run the fix</button>' : ''}${canRestart ? '<button class="btn primary" data-action="restart-now">Restart now</button>' : ''}</div>
        ${fixCard}
        ${metaLine ? `<div class="metaline">${metaLine}</div>` : ''}
        ${doctorDetails(d, evidence)}
      </div></div>`;
    }
    // fixed
    const r = d.result || { ok: false, summary: '' };
    const g = d.diagnosis || {};
    const ok = !!r.ok;
    const after = ok && /kernel|akmod|nvidia|driver/i.test(JSON.stringify((g.fix || {}).commands || []))
      ? 'A restart is likely needed for a kernel or driver change. Check again first: Rivet offers the restart only after verifying the NVIDIA module.' : '';
    const warn = ok && !!r.warn;
    return `<div class="hero result ${ok ? (warn ? 'warn' : '') : 'bad'} doctor-result" data-result="${ok ? (warn ? 'fixed-note' : 'fixed') : 'fixfail'}"><div class="inner">
      <div class="ring ${ok ? (warn ? 'warn' : '') : 'bad'}">${ok ? '✓' : '!'}</div>
      <h1>${ok ? (warn ? 'Fixed, with a note' : 'Fixed') : 'The fix did not work'}</h1>
      <p class="cause">${esc(r.summary || r.cause || '')}</p>
      ${ok && (g.after_fix || {}).expect ? `<div class="tally">${esc(g.after_fix.expect)}</div>` : ''}
      ${after ? `<div class="rebootline">${esc(after)}</div>` : ''}
      <div class="actions"><button class="btn" data-action="back-to-list">Back to updates</button>${ok ? '' : '<button class="btn" data-action="fix-it">Ask Claude again</button>'}<button class="btn primary" data-action="check-now">Check again</button></div>
      ${doctorDetails(d)}
    </div></div>`;
  }

  /* ---- Empty / loading / error ---- */
  // Nothing pending in dnf / Flatpak / fwupd. With rows in Held back or Other software, the
  // screen is a one-sentence status strip (where the updates screen has its summary bar) over
  // those sections; the celebratory hero is only for "nothing anywhere", and trimmed.
  // The header keeps the one Check now. CONTRACT.md "Front page" (2026-10-31 c).
  function kernelLine() {
    const h = host();
    return [h.kernel_running ? `Kernel ${esc(h.kernel_running)}` : '', h.secure_boot ? 'Secure Boot on' : h.secure_boot === false ? 'Secure Boot off' : ''].filter(Boolean).join(' · ');
  }
  function renderStatusStrip() {
    const held = heldItems(), ew = ewItems(), e = elsewhere();
    const stale = ewStale(), failed = ew.filter(i => i.status === 'failed').length, cant = ew.filter(i => i.status === 'cant_check').length;
    const toTry = held.filter(i => i.hold_state === 'candidate' || i.hold_state === 'trial').length;
    const clean = !(state.doc && state.doc.summary && state.doc.summary.total);
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    // 2026-11-01: a Flatpak remote that could not be read is never "up to date"
    const fre = Array.isArray(flags().flatpak_remote_errors) ? flags().flatpak_remote_errors : [];
    const segs = [`<span class="st-seg st-main">${fre.length ? 'Fedora' : 'Fedora and Flatpak'}: ${clean ? 'up to date' : 'updates waiting'}</span>`];
    if (fre.length) segs.push(`<span class="st-seg bad fp-remote" title="${esc(fre.map(e => (e.remote || e.scope) + ': ' + e.error).join('\n'))}">Flatpak: ${esc(fre.length === 1 ? (fre[0].remote || fre[0].scope) + ' could not be checked' : fre.length + ' remotes could not be checked')}</span>`);
    if (held.length) segs.push(`<button class="st-seg st-link" data-action="scroll-held" title="Scroll to Held back">${held.length} held back${toTry ? `, ${toTry} to try` : ''}</button>`);
    if (stale) segs.push(`<button class="st-seg st-link" data-action="scroll-elsewhere" title="Scroll to Other software">${plural(stale, 'other update', 'other updates')}</button>`);
    if (failed) segs.push(`<button class="st-seg st-link bad" data-action="scroll-elsewhere" title="Scroll to Other software">${plural(failed, 'check', 'checks')} failed</button>`);
    if (!stale && !failed && cant) segs.push(`<button class="st-seg st-link" data-action="scroll-elsewhere">${cant} can’t check</button>`);
    if (e && !ew.length) segs.push(`<span class="st-seg">other software current</span>`);
    const sub = kernelLine();
    return `<div class="status-strip" data-clean="${clean ? 1 : 0}">
      <span class="glyph ${clean && !fre.length ? 'ok' : 'warn'}" aria-hidden="true">${clean && !fre.length ? '✓' : '!'}</span>
      <div class="st-body"><div class="st-line">${segs.join('<span class="st-sep" aria-hidden="true"> · </span>')}</div>
        ${sub ? `<div class="st-sub">${sub}</div>` : ''}</div>
    </div>`;
  }
  function renderEmpty() {
    if (heldItems().length || ewItems().length || (flags().flatpak_remote_errors || []).length) {
      return `${renderStatusStrip()}<div class="list" id="list">${renderElsewhereSection('', true)}${renderHeldSection()}</div>`;
    }
    const sub = kernelLine();
    return `<div class="hero trimmed"><div class="inner">
      <div class="ring">✓</div>
      <h1>You’re up to date</h1>
      <p>Nothing to update in Fedora, Flatpak, firmware${elsewhere() ? ' or other software' : ''}.</p>
      ${sub ? `<p class="fine">${sub}</p>` : ''}
    </div></div>`;
  }

  function renderError() {
    if (state.staleCheck) {
      const started = state.doc && state.doc.generated_at;
      const cut = interrupted(state.doc);
      return `<div class="hero" data-stale="1" data-interrupted="${cut ? 1 : 0}"><div class="inner">
        <div class="ring warn">?</div>
        <h1>The last check did not finish${cut ? '. Check again.' : ''}</h1>
        <p>${cut ? `The checker was interrupted${started ? ' at ' + hhmm(started) : ''} (stopped or timed out) before it could report, so there is nothing current to show.`
          : `A check started${started ? ' at ' + hhmm(started) : ''} and never reported back, so there is nothing current to show. The checker may have been interrupted (a reboot, a crash, a kill).`}</p>
        <div class="actions"><button class="btn primary" data-action="check-now" data-force="1">Check for updates</button></div>
      </div></div>`;
    }
    return `<div class="hero"><div class="inner">
      <div class="ring bad">!</div>
      <h1>Could not check for updates</h1>
      <p>${esc(state.error || (state.doc && state.doc.error) || 'The collector did not finish.')}</p>
      <div class="actions"><button class="btn primary" data-action="check-now">Try again</button></div>
    </div></div>`;
  }

  function renderLoading() {
    const n = (state.doc && state.doc.summary && state.doc.summary.total) || 0;
    const widths = [[140, 220], [190, 150], [120, 250], [170, 130], [150, 200], [110, 180]];
    const slow = showCheckButton();
    return `<div class="progress indet"><i></i></div>
      <div class="loading"><div class="center">
        <h2>${slow ? 'Still checking…' : 'Checking repositories and reading release notes…'}</h2>
        <div class="sub">dnf5 · flatpak · fwupd${n ? ` · ${n} changelogs from the last run` : ''}</div>
        ${slow ? `<div class="slow-check"><span>This is taking longer than usual (${Math.round(checkWaited() / 1000)} s). If the checker got stuck you can start it again.</span>
          <button class="btn" data-action="check-now" data-force="1">Check for updates</button></div>` : ''}
        <div class="card">${widths.map(([a, b]) => `<div class="skel-row"><span class="skel box"></span><div style="flex:1"><div class="skel l1" style="max-width:${a}px"></div><div class="skel l2" style="max-width:${b}px"></div></div><span class="skel end"></span></div>`).join('')}</div>
      </div></div>`;
  }

  /* ---- Settings modal ---- */
  const INTERVALS = [[15, 'Every 15 minutes'], [30, 'Every 30 minutes'], [60, 'Every hour'], [180, 'Every 3 hours'], [720, 'Twice a day'], [1440, 'Daily'], [0, 'Only when I ask']];
  function renderLookTile(l, dark) {
    const [chrome, ground, card, accent, text] = l.sw[dark ? 'dark' : 'light'];
    const on = currentLook().id === l.id;
    return `<button class="look-tile${on ? ' on' : ''}" data-action="set-look" data-value="${l.id}" aria-pressed="${on}" title="${esc(l.blurb)}">
      <span class="lt-prev" aria-hidden="true" style="background:${ground}">
        <span class="lt-chrome" style="background:${chrome}"><i style="background:${accent}"></i><i class="w" style="background:${chrome === ground ? text : ground};opacity:.55"></i></span>
        <span class="lt-card" style="background:${card};border-color:${text}22"><i style="background:${text}"></i><i class="s" style="background:${text}"></i><b style="background:${accent}"></b></span>
        <span class="lt-card" style="background:${card};border-color:${text}22"><i style="background:${text}"></i><i class="s" style="background:${text}"></i></span>
      </span>
      <span class="lt-name" style="font-family:${l.font}">${esc(l.name)}${on ? '<span class="lt-on">In use</span>' : ''}</span>
      <span class="lt-blurb">${esc(l.blurb)}</span>
    </button>`;
  }
  function renderSettings() {
    const s = state.settings;
    const theme = s.theme || 'system';
    const dark = resolvedTheme() === 'dark';
    const size = s.type_size === 'comfortable' ? 'comfortable' : 'compact';
    return `<div class="backdrop" data-action="close-modal"><div class="modal" role="dialog" aria-label="Preferences" data-stop>
      <div class="modal-h"><img class="app-icon sm" src="rivet-icon.png" alt="" aria-hidden="true"><span class="t">Preferences</span>
        <button class="btn icon" data-action="close-modal" aria-label="Close">${svgX}</button></div>
      <div class="modal-b">
        <div class="set-h">APPEARANCE</div>
        <div class="set-card"><div class="set-row plain"><div><div class="l">Theme</div><div class="s">System follows the GNOME dark/light setting</div></div>
          <div class="right"><span class="seg">
            ${[['system', 'System'], ['dark', 'Dark'], ['light', 'Light']].map(([v, t]) => `<button class="${theme === v ? 'on' : ''}" data-action="set-theme" data-value="${v}">${t}</button>`).join('')}
          </span></div></div>
          <div class="set-row plain"><div><div class="l">Type size</div><div class="s">Comfortable makes everything about 12% bigger</div></div>
          <div class="right"><span class="seg">
            ${[['compact', 'Compact'], ['comfortable', 'Comfortable']].map(([v, t]) => `<button class="${size === v ? 'on' : ''}" data-action="set-size" data-value="${v}">${t}</button>`).join('')}
          </span></div></div></div>

        <div class="set-h" id="set-look">LOOK</div>
        <div class="set-desc">Each look has a light and a dark version. <kbd>g</kbd> <kbd>y</kbd> steps through them from anywhere.</div>
        <div class="look-grid">${LOOKS.map(l => renderLookTile(l, dark)).join('')}</div>

        <div class="set-h">CHECKING FOR UPDATES</div>
        <div class="set-card"><div class="set-row plain"><div><div class="l">Check interval</div><div class="s">Applied to the rivet-collect timer</div></div>
          <div class="right"><select class="select" data-action="set-interval">${INTERVALS.map(([v, t]) => `<option value="${v}" ${Number(s.check_interval_min) === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div></div></div>

        <div class="set-h">NOTIFICATIONS</div>
        <div class="set-card"><div class="set-row" data-action="set-toggle" data-key="notify"><div><div class="l">Notify when the pending set changes</div><div class="s">One desktop notification per change, not per check</div></div>
          <div class="right"><span class="toggle ${s.notify ? 'on' : ''}"></span></div></div></div>

        <div class="set-h">OTHER SOFTWARE</div>
        <div class="set-card"><div class="set-row" data-action="set-toggle" data-key="show_containers"><div><div class="l">Show containers</div><div class="s">Off for now: podman images are neither checked nor listed</div></div>
          <div class="right"><span class="toggle ${s.show_containers ? 'on' : ''}"></span></div></div></div>

        <div class="set-h">CLAUDE CODE</div>
        <div class="set-card"><div class="set-row" data-action="set-toggle" data-key="ai_triage"><div><div class="l">Explain and rate updates with Claude Code</div><div class="s">Sends package names and release notes to Anthropic through your own Claude Code login. Off by default.</div></div>
          <div class="right"><span class="toggle ${s.ai_triage ? 'on' : ''}"></span></div></div></div>

        <div class="set-h">WHAT COUNTS AS RED</div>
        <div class="set-desc">Red items are left out of “Safe only” and always flagged in the top bar.</div>
        <div class="set-card"><div class="set-row" data-action="set-toggle" data-key="red_includes_reboot"><div><div class="l">Anything that needs a reboot</div><div class="s">Off by default: kernel and firmware updates stay amber</div></div>
          <div class="right"><span class="toggle ${s.red_includes_reboot ? 'on' : ''}"></span></div></div></div>
      </div></div></div>`;
  }
  function renderToast() { return `<div class="toast">${state.toast.spin ? '<span class="spin"></span>' : ''}${esc(state.toast.msg)}</div>`; }

  /* ------------------------------------------------------------------ */
  /* Actions                                                             */
  /* ------------------------------------------------------------------ */
  const actions = {
    'toggle-theme': () => setTheme(resolvedTheme() === 'dark' ? 'light' : 'dark'),
    'set-theme': (t) => setTheme(t.dataset.value),
    'set-look': (t) => setLook(t.dataset.value),
    'set-size': (t) => setTypeSize(t.dataset.value),
    'next-look': () => { state.menuOpen = false; nextLook(); },
    'toggle-menu': () => { state.menuOpen = !state.menuOpen; },
    'open-settings': () => { state.modal = 'settings'; state.menuOpen = false; },
    'close-modal': () => { state.modal = null; },
    'check-now': (t) => {
      state.menuOpen = false;
      const force = !!(t && t.dataset && t.dataset.force === '1');   // the crash screen's way out
      if (checkBusy() && !force) return;             // never yank the floor from under an update or a fix
      requestCheck(state.view === 'updates' || state.view === 'empty');
    },
    'quit-steam': () => { toast('Asking Steam to quit…', true, 4000); send({ cmd: 'quitSteam' }); },
    'select-safe': () => { state.menuOpen = false; if (state.view === 'updates') selectSafe(); },
    'select-security': () => { state.menuOpen = false; if (state.view === 'updates' && securityCount()) selectSecurity(); },
    'select-all-global': () => {
      const pool = allItems().filter(sweepable);
      const on = pool.length && pool.every(it => state.selected.has(it.id));
      state.selected = new Set(on ? [] : pool.map(it => it.id));
    },
    'select-group': (t) => {
      const g = groups().find(x => x.id === t.dataset.group);
      if (!g) return;
      const pool = (g.items || []).filter(sweepable);
      const on = pool.length && pool.every(it => state.selected.has(it.id));
      pool.forEach(it => on ? state.selected.delete(it.id) : state.selected.add(it.id));
    },
    // one item, every member of a cluster, or every routine package: tri-state, so a
    // mixed cluster goes to "all on" first, and "all on" goes to "all off"
    'toggle-row': (t) => {
      const pool = targets(t.dataset.id).filter(selectable);
      if (!pool.length) return;
      const on = pool.every(it => state.selected.has(it.id));
      pool.forEach(it => on ? state.selected.delete(it.id) : state.selected.add(it.id));
    },
    'expand': (t) => { const id = t.dataset.id; state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id); },
    'expand-cluster': (t) => { const id = t.dataset.id; t.dataset.open === '1' ? state.openClusters.delete(id) : state.openClusters.add(id); },
    'toggle-routine': (t) => { state.routineOpen = t.dataset.open !== '1'; writeLS('rivet.routineOpen', state.routineOpen ? '1' : '0'); },
    'update-now': (t) => {
      const ids = [...state.selected];
      if (!ids.length) return;
      // One run at a time: a second click (double-click, or a click that lands before the
      // first progress event) is ignored until a terminal event arrives.
      if (state.view === 'applying' || (state.apply && !state.apply.result)) return;
      if (t && t.disabled !== undefined) t.disabled = true;
      state.apply = newApply(ids);
      state.view = 'applying';
      send({ cmd: 'apply', items: ids });
    },
    'toggle-drawer': () => { if (state.apply) state.apply.drawer = !state.apply.drawer; },
    'toggle-details': () => { if (state.apply) state.apply.details = !state.apply.details; },
    'restart-now': (t) => {
      const v = t && t.dataset && t.dataset.release;
      toast(v ? `Restarting into the Fedora ${v} installer…` : 'Restarting…', true);
      send(v ? { cmd: 'restart', release: Number(v) } : { cmd: 'restart' });
    },
    'restart-later': () => { requestCheck(false); },
    'back-to-list': () => {
      // the bridge holds state pushes back while the doctor view is up; tell it we left
      if (state.view === 'doctor' || state.doctor) send({ cmd: 'leaveDoctor' });
      state.apply = null; state.doctor = null; state.view = state.doc && state.doc.summary && state.doc.summary.total ? 'updates' : 'empty';
    },
    // fix it: diagnose (Claude, read-only) -> result -> run the fix -> fixed | failed
    'fix-it': () => {
      if (doctorBusy()) return;                      // the app ignores a second rivet-doctor anyway; keep the log
      const r = state.apply && state.apply.result;
      state.doctor = newDoctor('diagnose'); state.view = 'doctor';
      send({ cmd: 'fixIt', log: (r && r.log_path) || null });
    },
    'run-fix': () => {
      const d = state.doctor;
      if (!d || !d.fixPath || d.phase !== 'result') return;
      d.mode = 'run'; d.phase = 'fixing'; d.log = []; d.msg = 'Starting…'; d.pct = null; d.step = 'fix'; d.details = false;
      send({ cmd: 'runFix', fix: d.fixPath });
    },
    'toggle-doctor-details': () => { if (state.doctor) state.doctor.details = !state.doctor.details; },
    'clear-search': () => { state.query = ''; el.search.value = ''; },
    'open-url': (t) => send({ cmd: 'openUrl', url: t.dataset.url }),
    // held back: try -> confirm -> (try) -> watching -> kept | rolled back
    'held-try': (t) => {
      const it = heldByRef(t.dataset.ref);
      if (!it) return;
      state.held = startHeld(it, t.dataset.force === '1');
      state.modal = 'held-confirm';
    },
    'held-cancel': () => { state.modal = null; state.held = null; },
    'held-confirm': () => {
      const h = state.held;
      if (!h) return;
      state.modal = null; h.phase = 'trying'; state.view = 'held';
      send({ cmd: 'tryHeld', ref: h.ref, force: h.force });
    },
    'held-watch': (t) => {
      const h = state.held || (state.held = heldFromTrial(heldByRef(t.dataset.ref) || { ref: t.dataset.ref }));
      h.phase = 'watching'; h.msg = ''; h.pct = null; h.result = null; h.verdict = false; state.view = 'held';
      send({ cmd: 'verifyHeld', ref: h.ref, timeout: 240 });
    },
    'held-keep': (t) => heldManual('keepHeld', t.dataset.ref, 'Keeping it and removing the pin'),
    'held-rollback': (t) => heldManual('rollbackHeld', t.dataset.ref, 'Rolling back and pinning again'),
    'ew-expand': (t) => { const id = t.dataset.id; state.ewOpen.has(id) ? state.ewOpen.delete(id) : state.ewOpen.add(id); },
    'ew-update': (t) => {
      const it = ewItems().find(i => i.id === t.dataset.id);
      if (!it || ewBusy()) return;
      if (it.confirm) { state.ewConfirm = it.id; return; }
      ewStart(it);
    },
    'ew-go': (t) => { const it = ewItems().find(i => i.id === t.dataset.id); state.ewConfirm = null; if (it && !ewBusy()) ewStart(it); },
    // "Update the safe ones (N)": confirm (the exact list) -> otherUpdateMany -> rows stream one by one
    'ew-bulk': (t) => {
      if (ewBusy()) return;
      const ids = ewGroupRows(t.dataset.group, state.view === 'updates' ? state.query.trim().toLowerCase() : '').filter(ewSafe).map(i => i.id);
      if (!ids.length) return;
      state.ewBulkAsk = { group: t.dataset.group, ids }; state.modal = 'ew-bulk'; state.ewConfirm = null;
    },
    'ew-bulk-cancel': () => { state.modal = null; state.ewBulkAsk = null; },
    'ew-bulk-go': () => {
      const a = state.ewBulkAsk;
      if (!a) return;
      const ids = a.ids.filter(id => !(a.off || []).includes(id));
      if (!ids.length) return;
      state.modal = null; state.ewBulkAsk = null;
      if (ewBusy()) return;
      ids.forEach(id => { delete state.ewRuns[id]; });
      state.ewBatch = { group: a.group, ids: ids.slice(), done: [], stopping: false };
      send({ cmd: 'otherUpdateMany', ids });
    },
    'ew-bulk-stop': () => { if (state.ewBatch && !state.ewBatch.stopping) { state.ewBatch.stopping = true; send({ cmd: 'otherStop' }); } },
    'ew-cancel': () => { state.ewConfirm = null; state.ewRestartAsk = null; },
    'ew-bulk-toggle': (t) => {
      const a = state.ewBulkAsk;
      if (!a) return;
      a.off = a.off || [];
      const i = a.off.indexOf(t.dataset.id);
      if (i >= 0) a.off.splice(i, 1); else a.off.push(t.dataset.id);
    },
    // Fix it on a failed row: otherFix -> claude -p with a tiny tool budget -> the result line
    'health-fix': (t) => {
      const id = t.dataset.id;
      if (!id || checkBusy() || (state.healthRun && state.healthRun.phase === 'running')) return;
      const c = ((state.health || {}).checks || []).find(x => x.id === id);
      state.healthRun = { id, phase: 'running', msg: c ? c.fix_label + '…' : 'Working…' };
      send({ cmd: 'healthFix', id });
    },
    'ew-fix': (t) => {
      const it = ewItems().find(i => i.id === t.dataset.id);
      if (!it || ewBusy()) return;
      state.ewConfirm = null; state.ewRestartAsk = null;
      ewStartFix(it);
    },
    'ew-restart-ask': (t) => { if (!ewBusy()) { state.ewRestartAsk = t.dataset.id; state.ewConfirm = null; } },
    'ew-restart-go': (t) => {
      const it = ewItems().find(i => i.id === t.dataset.id);
      state.ewRestartAsk = null;
      if (!it || ewBusy()) return;
      state.ewHaRestart = { phase: 'running', msg: 'Restarting Home Assistant…' };
      send({ cmd: 'haRestart' });
    },
    // Remove (an unused HA add-on): confirm -> otherRemove -> the row streams, then goes
    'ew-remove': (t) => {
      const it = ewItems().find(i => i.id === t.dataset.id);
      if (!it || !ewRemovable(it) || ewBusy()) return;
      state.ewRemoveAsk = it.id; state.modal = 'ew-remove'; state.ewConfirm = null;
    },
    'ew-remove-cancel': () => { state.modal = null; state.ewRemoveAsk = null; },
    'ew-remove-go': (t) => {
      const it = ewItems().find(i => i.id === t.dataset.id);
      state.modal = null; state.ewRemoveAsk = null;
      if (it && ewRemovable(it) && !ewBusy()) ewStartRemove(it);
    },
    'ew-dismiss': (t) => { delete state.ewRuns[t.dataset.id]; },
    'ew-copy': (t) => {
      const text = String(t.dataset.text || '').slice(0, 500);
      if (!text) return;
      send({ cmd: 'copyText', text });
      toast('Copied: ' + text, false, 2600);
    },
    'scroll-elsewhere': () => { setTimeout(() => { const n = document.querySelector('.ew-h'), l = document.getElementById('list') || document.querySelector('.empty-held'); if (n && l) l.scrollTop += n.getBoundingClientRect().top - l.getBoundingClientRect().top - 14; }, 0); },
    'scroll-held': () => { setTimeout(() => { const n = document.querySelector('.held-h'); if (n) n.scrollIntoView({ block: 'start' }); }, 0); },
    'held-done': () => { requestCheck(false); },
    'set-toggle': (t) => {
      const k = t.dataset.key;
      state.settings[k] = !state.settings[k];
      send({ cmd: 'setSetting', key: k, value: state.settings[k] });
    },
    // mock-only
    'sim-updates': () => { state.menuOpen = false; if (MOCK) mockShow(true); },
    'sim-empty': () => { state.menuOpen = false; if (MOCK) mockShow(false); },
    'toggle-steam': () => { state.menuOpen = false; if (MOCK) mockSteam(!flags().steam_running); },
  };

  function doctorBusy() { return !!(state.doctor && /working|fixing/.test(state.doctor.phase)); }
  function heldBusy() { return !!(state.held && state.held.phase !== 'result' && state.held.phase !== 'confirm'); }
  // "Check for updates" would null the apply/doctor record, and the next progress event
  // would rebuild a blank step list: refuse while anything is mid-flight.
  function checkBusy() {
    return state.view === 'applying' || (state.view === 'held' && heldBusy()) || (state.view === 'doctor' && doctorBusy());
  }
  // Every path that asks the app for a fresh check goes through here, so the UI knows
  // the check is its own (see checkingIsStale). keepList: the list on screen is still
  // current (a re-check from the list), so it stays up with a progress bar; after an
  // apply or a trial it is not, and the skeleton shows until the collector reports.
  function requestCheck(keepList) {
    state.apply = null; state.doctor = null; state.held = null;
    state.checkStartedAt = Date.now(); state.checkingSince = Date.now();
    state.checkingKey = String((state.doc && state.doc.generated_at) || '');   // the app's stub reuses this stamp
    state.staleCheck = false; state.checking = false;
    state.listStale = !keepList;
    if (keepList && allItems().length) { state.view = 'updates'; state.checking = true; }
    else state.view = 'loading';
    send({ cmd: 'refresh' });
  }

  function heldManual(cmd, ref, msg) {
    let h = state.held;
    if (h && h.verdict) return;            // the watcher already decided and is acting on it
    if (!h || h.ref !== ref) h = state.held = heldFromTrial(heldByRef(ref) || { ref });
    h.phase = 'working'; h.msg = msg; h.result = null; state.view = 'held';
    send({ cmd, ref });
  }

  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-action]');
    if (!t) { if (state.menuOpen && !e.target.closest('#menu')) { state.menuOpen = false; render(); } return; }
    if (t.tagName === 'SELECT') return;
    if (/^(close-modal|held-cancel|ew-bulk-cancel|ew-remove-cancel)$/.test(t.dataset.action) && t.classList.contains('backdrop') && e.target !== t) return;
    e.preventDefault(); e.stopPropagation();
    const fn = actions[t.dataset.action];
    if (!fn) return;
    try { fn(t, e); } catch (err) { console.error('[rivet] action "' + t.dataset.action + '" failed: ' + ((err && err.stack) || err)); }
    render();
  });
  document.addEventListener('change', (e) => {
    const t = e.target.closest('[data-action="set-interval"]');
    if (t) {
      const v = parseInt(t.value, 10);
      state.settings.check_interval_min = v;
      send({ cmd: 'setSetting', key: 'check_interval_min', value: v });
    }
  });
  el.search.addEventListener('input', () => { state.query = el.search.value; render(); });

  let gChord = 0;
  document.addEventListener('keydown', (e) => {
    const inField = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (inField && document.activeElement === el.search) {
        if (el.search.value) { state.query = ''; el.search.value = ''; } else el.search.blur();
        render(); return;
      }
      if (state.menuOpen) state.menuOpen = false;
      else if (state.modal) { if (state.modal === 'held-confirm') state.held = null; state.ewBulkAsk = null; state.ewRemoveAsk = null; state.modal = null; }
      else if (state.view === 'applying' && state.apply && state.apply.drawer) state.apply.drawer = false;
      render(); return;
    }
    if (inField) { if (e.key === 'Enter' && document.activeElement === el.search) el.search.blur(); return; }
    // two-key chords: g y = next look, g L = Preferences at the Look grid
    if (gChord && Date.now() - gChord < 1200 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      gChord = 0;
      if (e.key === 'y') { e.preventDefault(); nextLook(); render(); return; }
      if (e.key === 'L' || e.key === 'l') {
        e.preventDefault(); state.modal = 'settings'; state.menuOpen = false; render();
        const n = document.getElementById('set-look'); if (n) n.scrollIntoView({ block: 'start' });
        return;
      }
    }
    gChord = 0;
    if (e.key === 'g' && !e.ctrlKey && !e.metaKey && !e.altKey) { gChord = Date.now(); return; }
    if (e.key === '/') { e.preventDefault(); if (state.view === 'updates') { el.search.focus(); el.search.select(); } }
    else if (e.key === ',') { state.modal = 'settings'; render(); }
    else if ((e.key === 'r' || e.key === 'R') && !e.ctrlKey && !e.metaKey && !checkBusy()) { actions['check-now'](); render(); }
    else if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('cb')) { e.preventDefault(); e.target.click(); }
  });

  /* ------------------------------------------------------------------ */
  /* Mock host (?mock=1): sample-state.json + a fake progress stream      */
  /* ------------------------------------------------------------------ */
  let mockDoc = null, mockTimers = [];
  const mockSettings = Object.assign({}, DEFAULT_SETTINGS);

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function clearMockTimers() { mockTimers.forEach(clearTimeout); mockTimers = []; }
  function later(ms, fn) { mockTimers.push(setTimeout(fn, ms)); }

  function loadSample() {
    return fetch('sample-state.json', { cache: 'no-store' }).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function mockShow(withUpdates) {
    if (!mockDoc) return;
    const d = clone(mockDoc);
    if (!withUpdates) {
      d.groups = d.groups.filter(g => g.id === 'held');
      d.summary = { total: 0, total_bytes: 0 };
      d.flags = { steam_running: d.flags.steam_running, kernel_pending: false, akmod_nvidia_pending: false, reboot_required: false, blocked_count: 0, security_count: 0 };
      d.host = Object.assign({}, d.host, { kernel_running: '7.2.5-200.fc44.x86_64' });
    }
    d.status = 'ok';
    state.apply = null;
    state.selected = new Set(); state._seen = new Map();
    receive({ type: 'state', state: d });
  }

  function mockSteam(running) {
    if (!mockDoc) return;
    mockDoc.flags.steam_running = running;
    let blocked = 0;
    for (const g of mockDoc.groups) for (const it of g.items) {
      if (it.source !== 'flatpak') continue;
      if (running) {
        it.blocked_by = 'steam_running'; it.badge = 'blocked'; it.risk = 'red'; it.selected_default = false;
      } else {
        it.blocked_by = null; it.badge = null; it.risk = 'yellow'; it.selected_default = true;
        it.verdict = it.verdict.replace(/Steam is using it now.*$/, 'Steam is closed, so it is safe to swap now.')
          .replace(/Same Steam conflict.*$/, 'Safe now that Steam is closed.')
          .replace(/Same Steam conflict\.$/, 'Safe now that Steam is closed.');
      }
      if (it.blocked_by) blocked++;
    }
    mockDoc.flags.blocked_count = blocked;
    receive({ type: 'state', state: clone(mockDoc) });
  }

  function mockProgress(ids) {
    clearMockTimers();
    const items = ids.map(byId).filter(Boolean);
    const steps = planSteps(ids);
    const ev = (o) => receive({ type: 'progress', event: Object.assign({ ts: new Date().toISOString(), pct: null, log: null, msg: null }, o) });
    let t = 0;
    const at = (ms, o) => { t += ms; later(t, () => ev(o)); };
    const total = items.reduce((s, i) => s + (i.size_bytes || 0), 0);

    at(200, { step: 'download', status: 'start', pct: 0, msg: `0 B of ${fmtBytes(total)}`, log: `Resolving ${ids.length} updates (dnf5, flatpak, fwupd)…` });
    items.forEach((it, i) => {
      const p = Math.round((i + 1) / items.length * 100);
      at(220, { step: 'download', status: 'progress', pct: p, msg: `${fmtBytes(total * p / 100)} of ${fmtBytes(total)}`, log: `Downloading ${it.ref} … done (${fmtBytes(it.size_bytes)})` });
    });
    at(300, { step: 'download', status: 'ok', pct: 100, msg: `${fmtBytes(total)} · done`, log: 'Verifying GPG signatures … ok' });

    if (steps.includes('install')) {
      const rpms = items.filter(i => i.source === 'dnf');
      at(300, { step: 'install', status: 'start', pct: 0, msg: 'Running transaction check', log: 'Running transaction check … ok' });
      rpms.forEach((it, i) => {
        at(260, { step: 'install', status: 'progress', pct: Math.round((i + 1) / rpms.length * 100), msg: `${i + 1} of ${rpms.length} packages`, log: `Installing : ${it.ref} [${i + 1}/${rpms.length}]` });
      });
      at(250, { step: 'install', status: 'ok', pct: 100, msg: `${rpms.length} packages · done`, log: 'Transaction complete' });
    }
    if (steps.includes('akmod')) {
      at(250, { step: 'akmod', status: 'start', pct: 0, msg: 'please wait, do not reboot yet', log: 'akmods: building NVIDIA 615.71.09 for kernel 7.2.5-200.fc44' });
      ['CC [M] nvidia/nv-frontend.o', 'CC [M] nvidia/nv-instance.o', 'CC [M] nvidia-drm/nvidia-drm-gem.o', 'CC [M] nvidia-modeset/nvidia-modeset-linux.o', 'LD [M] nvidia.ko nvidia-drm.ko nvidia-modeset.ko nvidia-uvm.ko', 'signing modules with the Machine Owner Key'].forEach((line, i, a) => {
        at(320, { step: 'akmod', status: 'progress', pct: Math.round((i + 1) / a.length * 100), msg: 'building', log: 'nvidia-kmod: ' + line });
      });
      at(300, { step: 'akmod', status: 'ok', pct: 100, msg: 'kmod-nvidia for 7.2.5 built and signed', log: 'akmods: kmod-nvidia-7.2.5-200.fc44.x86_64-615.71.09 installed' });
    }
    if (steps.includes('flatpak')) {
      const fps = items.filter(i => i.source === 'flatpak');
      at(250, { step: 'flatpak', status: 'start', pct: 0, msg: `${fps.length} refs`, log: 'flatpak: starting update' });
      fps.forEach((it, i) => at(300, { step: 'flatpak', status: 'progress', pct: Math.round((i + 1) / fps.length * 100), msg: it.display_name, log: `flatpak: deploying ${it.ref} … done` }));
      at(250, { step: 'flatpak', status: 'ok', pct: 100, msg: 'all refs deployed', log: 'flatpak: done' });
    }
    if (steps.includes('fwupd')) {
      at(250, { step: 'fwupd', status: 'start', pct: 0, msg: 'staging capsule', log: 'fwupd: writing dbx capsule' });
      at(500, { step: 'fwupd', status: 'ok', pct: 100, msg: 'scheduled for next boot', log: 'fwupd: dbx-20260707 scheduled for next boot' });
    }
    at(250, { step: 'verify', status: 'start', pct: 10, msg: 'boot entries, module signatures, deployments', log: 'Checking /boot/loader/entries … 7.2.5 present, default' });
    if (steps.includes('akmod')) at(350, { step: 'verify', status: 'progress', pct: 60, msg: 'modinfo nvidia', log: 'modinfo -k 7.2.5-200.fc44.x86_64 nvidia: 615.71.09, signed' });
    at(350, { step: 'verify', status: 'ok', pct: 100, msg: 'everything checks out', log: 'Verification complete' });
    at(300, {
      step: 'done', status: 'ok',
      reboot_required: items.some(i => i.requires_reboot),
      kmod_verified: steps.includes('akmod') ? true : null,
      summary: `Applied ${ids.length} updates (${fmtBytes(total)}).`,
    });
  }

  // Held-back trial without a backend. ?heldfail=1 makes the watcher see the game die.
  function mockHeld(action, ref) {
    const it = (mockDoc && ((mockDoc.groups.find(g => g.id === 'held') || {}).items || []).find(i => i.ref === ref)) || {};
    const name = it.display_name || 'gamescope', from = (it.trial && it.trial.from_version) || it.from || '3.16.28';
    const to = (it.trial && it.trial.to_version) || it.to || '3.16.30', label = it.verify_label || 'the game';
    const held = { ref, display_name: name, label, from_version: from, to_version: to };
    const ev = (o) => receive({ type: 'held', event: Object.assign({ ts: new Date().toISOString(), pct: null, log: null, msg: null, held }, o) });
    const done = (result, summary, status) => {
      const g = mockDoc && mockDoc.groups.find(x => x.id === 'held');
      if (g && result === 'kept') g.items = g.items.filter(i => i.ref !== ref);
      if (g && result === 'rolled_back') g.items.forEach(i => { if (i.ref === ref) { i.hold_state = 'known_bad'; i.bad_versions = (i.bad_versions || []).concat([to]); delete i.trial; } });
      ev({ step: 'done', status: status || 'ok', summary, msg: summary, held: Object.assign({}, held, { action, result }) });
    };
    let t = 0; const at = (ms, fn) => { t += ms; later(t, fn); };
    const fail = params.get('heldfail') === '1';
    if (action === 'try') {
      at(300, () => ev({ step: 'trial', status: 'start', pct: 0, msg: `Checking ${name}…` }));
      at(700, () => ev({ step: 'trial', status: 'progress', pct: 10, msg: `Removing the pin on ${name}…` }));
      at(900, () => ev({ step: 'trial', status: 'progress', pct: 30, msg: `Updating ${name} to ${to}…` }));
      at(1200, () => done('awaiting_verify', `${name} ${to} is installed. Now launch ${label}. I'm watching.`));
    } else if (action === 'verify') {
      at(200, () => ev({ step: 'watch', status: 'start', pct: 0, msg: `Waiting for you to launch ${label}…` }));
      at(3000, () => ev({ step: 'watch', status: 'progress', pct: 1, msg: `${label} is running…` }));
      if (fail) {
        at(1500, () => ev({ step: 'watch', status: 'fail', verdict: 'fail', msg: `${name} ${to} failed: the journal says: Failed to get Xwayland server id. Rolling back…` }));
        at(1500, () => done('rolled_back', `Rolled back to ${from} and pinned again. ${to} is marked bad.`));
      } else {
        [10, 25, 40, 55, 70].forEach(sec => at(1100, () => ev({ step: 'watch', status: 'progress', pct: Math.round(sec / 75 * 100), msg: `${label} is running, ${sec} s…` })));
        at(1100, () => ev({ step: 'watch', status: 'ok', verdict: 'ok', pct: 100, msg: `${label} stayed up for 75 s. Keeping ${name} ${to}.` }));
        at(900, () => done('kept', `${name} ${to} kept. Pin removed.`));
      }
    } else if (action === 'keep') {
      at(700, () => done('kept', `${name} ${to} kept. Pin removed.`));
    } else {
      at(900, () => done('rolled_back', `Rolled back to ${from} and pinned again. ${to} is marked bad.`));
    }
  }

  // "Fix it" without a backend: the 2026-09-22 story (kernel never installed).
  function mockDoctor(mode) {
    const ev = (o) => receive({ type: 'doctor', mode, event: Object.assign({ ts: new Date().toISOString(), pct: null, log: null, msg: null }, o) });
    let t = 0; const at = (ms, o) => { t += ms; later(t, () => ev(o)); };
    if (mode === 'diagnose') {
      at(100, { step: 'doctor', status: 'start', pct: 0, msg: 'Looking into what happened…' });
      at(400, { step: 'doctor', status: 'progress', pct: 12, msg: 'Collecting evidence: last rivet-apply run…' });
      at(500, { step: 'doctor', status: 'progress', pct: 30, msg: 'Asking Claude (opus) to look into it…', log: '$ claude -p --model opus (read-only tools)' });
      at(900, { step: 'doctor', status: 'progress', pct: 36, msg: 'Checking: Which kernels does rpm know about?', log: '$ rpm -q kernel-core' });
      at(300, { step: 'doctor', status: 'progress', log: '  kernel-core-7.2.5-200.fc44.x86_64' });
      at(700, { step: 'doctor', status: 'progress', pct: 45, msg: 'Checking: NVIDIA modules per kernel', log: '$ ls /lib/modules/*/extra/nvidia/' });
      at(600, { step: 'doctor', status: 'progress', log: '(not allowed, read-only mode: sudo dnf5 makecache)' });
      at(800, { step: 'doctor', status: 'progress', pct: 70, msg: 'Checking: what dnf offers now', log: "$ dnf5 -q check-upgrade 'kernel*'" });
      at(900, { step: 'doctor', status: 'ok', pct: 100, msg: 'The new kernel was never installed, so nothing is broken', log: '4 checks, 9 turns, 41s' });
      at(100, { step: 'done', status: 'ok', pct: 100, summary: 'The new kernel was never installed, so nothing is broken', fix_path: '/mock/doctor/last.json', log_path: '~/.local/state/rivet/logs/doctor.log',
        diagnosis: {
          headline: 'The new kernel was never installed, so nothing is broken',
          what_happened: "Rivet asked dnf to install kernel 7.2.6, but dnf's own package index was three hours older than the checker's and did not know that version yet, so it installed nothing. Rivet then waited ten minutes for an NVIDIA module build for a kernel that is not on the disk. The running kernel 7.2.5 still has its NVIDIA module.",
          severity: 'attention', confidence: 'high',
          reboot: { safe: 'yes', why: 'Nothing changed: 7.2.5 boots with its verified NVIDIA module.' },
          fix: { available: true, title: 'Refresh dnf and install the kernel', explanation: 'Refresh the package index, install the 12 packages Rivet meant to install, then build the NVIDIA module for the new kernel.',
            commands: ['sudo dnf5 -y --refresh upgrade kernel kernel-core kernel-modules kernel-modules-core kernel-modules-extra kernel-devel kernel-devel-matched kernel-tools kernel-tools-libs python3-perf selinux-policy selinux-policy-targeted', 'sudo akmods --kernels 7.2.6-200.fc44.x86_64'],
            needs_root: true, risk: 'low', reversible: 'The old kernel stays installed and selectable at boot.' },
          after_fix: { verify_commands: ['rpm -q kernel-core-7.2.6-200.fc44.x86_64', 'modinfo -k 7.2.6-200.fc44.x86_64 nvidia'], expect: 'kernel-core 7.2.6 is installed and its NVIDIA module loads.' },
          evidence: ["apply.log: '0 package(s) in the transaction. Nothing left to do.' right after 'Preparing 12 package(s)'", 'rpm -q kernel-core: 7.1.10, 7.2.4, 7.2.5 -- no 7.2.6', 'dnf5daemon-server cache revision 1789950156 (20:01) vs user cache 1790036627 (23:05)'],
          _meta: { model: 'opus', checks: 4, turns: 9, duration_ms: 41000 },
        } });
      return;
    }
    at(100, { step: 'fix', status: 'start', pct: 0, msg: 'Running: Refresh dnf and install the kernel' });
    at(250, { step: 'fix', status: 'progress', msg: 'Asking for permission to run step 1 as root (face or password)…' });
    at(300, { step: 'fix', status: 'progress', pct: 0, msg: 'Step 1 of 2: sudo dnf5 -y --refresh upgrade kernel …', log: '$ dnf5 -y --refresh upgrade kernel kernel-core …' });
    at(900, { step: 'fix', status: 'progress', log: 'Upgrading kernel-core-0:7.2.6-200.fc44.x86_64' });
    at(900, { step: 'fix', status: 'progress', log: 'Complete!' });
    at(400, { step: 'fix', status: 'progress', pct: 50, msg: 'Step 2 of 2: sudo akmods --kernels 7.2.6-200.fc44.x86_64', log: '$ akmods --kernels 7.2.6-200.fc44.x86_64' });
    at(1200, { step: 'fix', status: 'progress', log: 'Building and installing nvidia-kmod  [  OK  ]' });
    at(300, { step: 'fix', status: 'ok', pct: 100, msg: '2 of 2 step(s) ran without errors.' });
    at(300, { step: 'verify', status: 'start', pct: 0, msg: 'Checking that it worked…' });
    at(500, { step: 'verify', status: 'progress', pct: 0, msg: 'rpm -q kernel-core-7.2.6-200.fc44.x86_64', log: '$ rpm -q kernel-core-7.2.6-200.fc44.x86_64' });
    at(500, { step: 'verify', status: 'ok', pct: 100, msg: 'kernel-core 7.2.6 is installed and its NVIDIA module loads.' });
    at(200, { step: 'done', status: 'ok', pct: 100, summary: 'Fixed: Refresh dnf and install the kernel.', results: { ran: 2, planned: 2 } });
  }

  // Other software's Update without a backend: a short canned stream, nothing changes
  function mockOther(id, then) {
    const it = ewItems().find(i => i.id === id) || { name: id };
    const ev = (o) => receive({ type: 'other', event: Object.assign({ other_id: id, pct: null, log: null, msg: null }, o) });
    let t = 0; const at = (ms, o) => { t += ms; later(t, () => ev(o)); };
    at(150, { step: 'other', status: 'start', pct: 0, msg: `Updating ${it.name}…`, log: it.command ? '$ ' + it.command : `POST /api/services/update/install {"entity_id": "${it.entity_id || ''}"}` });
    at(500, { step: 'other', status: 'progress', msg: 'changed 1 package in 2s (mock)', log: 'changed 1 package in 2s (mock)' });
    at(400, { step: 'done', status: 'ok', summary: `${it.name} is now ${it.latest || '?'}. (mock: nothing ran)`,
      other: { id, result: 'updated', item: Object.assign({}, it, { status: 'updated', installed: it.latest, action: null, confirm: false }) } });
    if (then) later(t + 300, then);
  }

  // "Update the safe ones" without a backend: mockOther per row, one after another
  function mockOtherMany(ids) {
    const queue = ids.slice(), ran = [];
    mockBatch = { stop: false };
    const next = () => {
      if (mockBatch.stop || !queue.length) {
        receive({ type: 'other', event: { step: 'batch', status: 'done', batch: { ids, ran, refused: [], stopped: !!(mockBatch.stop && queue.length) } } });
        mockBatch = null; return;
      }
      const id = queue.shift(); ran.push(id);
      mockOther(id, next);
    };
    next();
  }
  let mockBatch = null;
  // Remove without a backend: a short canned uninstall, then the row goes (nothing reaches HA)
  function mockRemove(id) {
    const it = ewItems().find(i => i.id === id) || { name: id };
    const ev = (o) => receive({ type: 'other', event: Object.assign({ other_id: id, pct: null, log: null, msg: null }, o) });
    later(300, () => ev({ step: 'other', status: 'progress', msg: `Uninstalling ${it.name}… (mock)`, log: `supervisor/api POST /addons/${it.addon_slug}/uninstall` }));
    later(900, () => ev({ step: 'done', status: 'ok', summary: `${it.name} was uninstalled from Home Assistant. (mock: nothing ran)`, other: { id, result: 'removed', item: null, removed: true } }));
  }

  function mockHandle(msg) {
    switch (msg.cmd) {
      case 'ready':
        receive({ type: 'settings', settings: mockSettings });
        loadSample().then(d => {
          mockDoc = d;
          receive({ type: 'state', state: clone(d) });
        }).catch(err => {
          receive({ type: 'error', msg: 'Mock mode could not load sample-state.json (' + err.message + '). Serve this directory over HTTP: python3 -m http.server' });
        });
        break;
      case 'refresh':
        if (!mockDoc) { loadSample().then(d => { mockDoc = d; mockHandle({ cmd: 'refresh' }); }); return; }
        receive({ type: 'state', state: Object.assign(clone(mockDoc), { status: 'checking' }) });
        later(1400, () => {
          const d = clone(mockDoc);
          d.status = 'ok';
          d.checked_at = d.generated_at = new Date().toISOString();
          receive({ type: 'state', state: d });
        });
        break;
      case 'apply': mockProgress(msg.items || []); break;
      case 'fixIt': mockDoctor('diagnose'); break;
      case 'runFix': mockDoctor('run'); break;
      case 'tryHeld': mockHeld('try', msg.ref); break;
      case 'verifyHeld': mockHeld('verify', msg.ref); break;
      case 'keepHeld': clearMockTimers(); mockHeld('keep', msg.ref); break;
      case 'rollbackHeld': clearMockTimers(); mockHeld('rollback', msg.ref); break;
      case 'quitSteam': later(700, () => { mockSteam(false); toast('Steam closed. Flatpak runtime updates are unblocked.', false, 3000); }); break;
      case 'restart': later(1200, () => toast('(mock) logind Reboot(true) would be called here.', false, 3000)); break;
      case 'openUrl': window.open(msg.url, '_blank', 'noopener'); break;
      case 'otherUpdate': mockOther(msg.id); break;
      case 'otherUpdateMany': mockOtherMany(msg.ids || []); break;
      case 'otherStop': if (mockBatch) mockBatch.stop = true; break;
      case 'otherRemove': mockRemove(msg.id); break;
      case 'otherFix': later(1200, () => receive({ type: 'other', event: { other_id: msg.id, step: 'done', status: 'ok', summary: 'Fixed: Home Assistant needed a restart to load it (mock: nothing ran).', other: { id: msg.id, result: 'fixed', item: null } } })); break;
      case 'haRestart': later(1200, () => receive({ type: 'other', event: { other_id: 'ha:_restart', step: 'done', status: 'ok', summary: 'Restarted Home Assistant. (mock)', other: { id: 'ha:_restart', result: 'restarted', items: [] } } })); break;
      case 'copyText': try { navigator.clipboard.writeText(msg.text).catch(() => {}); } catch (e) { /* no clipboard in this browser */ } break;
      case 'setSetting': mockSettings[msg.key] = msg.value; receive({ type: 'settings', settings: mockSettings }); break;
      default: break;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                                */
  /* ------------------------------------------------------------------ */
  const early = (window.rivet && window.rivet._q) || [];
  window.rivet = { receive, state, render, send, config, views: VIEWS };
  render();
  send({ cmd: 'ready' });
  early.forEach(receive);

  // ?q= and ?expand= are screenshot-QA conveniences in mock mode
  if (MOCK && (params.get('q') || params.get('expand') || params.get('bulk'))) {
    document.documentElement.classList.add('no-anim');
    const tq = setInterval(() => {
      if (!state.doc) return;
      clearInterval(tq);
      if (params.get('q')) { state.query = params.get('q'); el.search.value = state.query; }
      // ids of Other-software rows open those rows' details too
      if (params.get('expand')) params.get('expand').split(',').forEach(id => { state.expanded.add(id); if (ewItems().some(i => i.id === id)) state.ewOpen.add(id); });
      if (params.get('bulk')) { const b = document.querySelector(`[data-action="ew-bulk"][data-group="${params.get('bulk')}"]`); if (b) b.click(); }
      render();
    }, 60);
  }

  // ?view=applying|done in mock mode: jump straight there (for screenshot QA)
  if (MOCK && params.get('view')) {
    const want = params.get('view');
    // deterministic screenshots: no fade/spin animations mid-capture
    document.documentElement.classList.add('no-anim');
    const tries = setInterval(() => {
      if (!state.doc || !state.doc.groups.length) return;
      clearInterval(tries);
      if (want === 'applying' || want === 'done') {
        const ids = [...state.selected];
        state.apply = newApply(ids);
        state.view = 'applying';
        render();
        mockProgress(ids);
        if (want === 'applying') {
          // stop the stream partway so the screenshot catches a mid-run frame
          later(2600, clearMockTimers);
        }
      } else if (want === 'loading') {
        requestCheck(false); render();
      } else if (want === 'empty') { mockShow(false); }
      else if (want === 'clean') { mockDoc.groups = []; if (mockDoc.elsewhere) mockDoc.elsewhere.items = []; mockShow(false); }
      else if (want === 'settings') { state.modal = 'settings'; render(); }
    }, 60);
  }
})();
