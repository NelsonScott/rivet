/* Rivet prototype A — one vanilla-JS state machine, no framework, all mock data. */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* Mock data: a realistic pending set                               */
  /* ------------------------------------------------------------------ */
  const GROUPS = [
    { id: 'system',   title: 'SYSTEM · DNF' },
    { id: 'flatpak',  title: 'APPS · FLATPAK' },
    { id: 'firmware', title: 'FIRMWARE · FWUPD' },
  ];

  const gstNote = (mod) =>
`gstreamer1${mod} 1.28.7 — stable release

* Bug-fix release, ABI/API compatible with 1.28.x
* No new plugins, no format changes
* Fixes: ${mod === '' ? 'race in GstBus when flushing' :
          mod === '-plugins-base' ? 'audioconvert clipping with float32 input' :
          mod === '-plugins-good' ? 'matroskademux seeking on files with cues at end' :
          mod === '-plugins-bad-free' ? 'AV1 parser on odd-sized frames' :
          'x264enc keyframe interval reset'}`;

  const gst = (mod, size, verdict) => ({
    id: 'gstreamer1' + mod, group: 'system', name: 'gstreamer1' + mod, from: '1.28.6', to: '1.28.7', size,
    risk: 'green', badge: null, verdict,
    notes: gstNote(mod),
    why: 'Point release inside the same 1.28 branch: the changelog lists only bug fixes and no plugin or format changes. Nothing on this machine pins a GStreamer version. No restart, no conflict.',
    repo: 'updates', advisory: 'FEDORA-2026-1f2c9a'
  });

  const UPDATES = [
    { id: 'openssl', group: 'system', name: 'openssl', from: '3.5.1', to: '3.5.2', size: 7.1,
      risk: 'red', critical: true, badge: 'SECURITY',
      verdict: 'Fixes CVE-2026-4417, a remotely triggerable crash in TLS 1.3 handshake parsing. Install today.',
      notes: `openssl 3.5.2 — security release

* CVE-2026-4417: out-of-bounds read while parsing a malformed
  ClientHello with a truncated key_share extension. A remote peer
  can crash any process accepting TLS 1.3 connections. Severity: High.
* Fixed a memory leak in X509_STORE_CTX when verification fails early.`,
      why: 'The advisory is rated High and the affected code path (TLS 1.3 server-side handshake) is reachable by anything on this box that listens on TLS: sshd is not affected, but a local web server such as Caddy is. There is no configuration workaround. The fix is a point release on the same 3.5 branch with no API changes, so applying it is low-risk and no reboot is needed; long-running services pick it up on restart.',
      repo: 'updates', advisory: 'FEDORA-2026-8d31e5 · CVE-2026-4417' },

    { id: 'kernel', group: 'system', name: 'kernel', from: '7.2.4', to: '7.2.5', size: 148,
      risk: 'yellow', badge: 'REBOOT', reboot: true,
      banner: 'Kernel + NVIDIA akmod: the NVIDIA module must rebuild before reboot. Rivet waits for it.',
      verdict: 'Routine stable bump: amdgpu suspend regression and a Btrfs corner case fixed. Needs a reboot.',
      notes: `Linux 7.2.5 — stable

* amdgpu: fix resume from s2idle on RDNA4 (regression in 7.2.3)
* btrfs: avoid ENOSPC on nearly-full metadata chunk during balance
* xhci: quirk for Genesys Logic hubs dropping devices on wake
* 143 other changes, mostly driver fixes`,
      why: 'Stable-branch release; the delta is bug fixes. Your GPU is NVIDIA, so the amdgpu change is irrelevant, but the xhci hub quirk matches the Genesys hub on the Blue Yeti path. Marked caution only because a new kernel needs a reboot, and because akmod-nvidia must finish rebuilding for 7.2.5 before that reboot or the desktop will not start.',
      repo: 'updates', advisory: 'FEDORA-2026-77a0c2' },

    { id: 'akmod-nvidia', group: 'system', name: 'akmod-nvidia', from: '610.57.04', to: '615.71.09', size: 312,
      risk: 'yellow', badge: 'REBUILD', reboot: true,
      verdict: 'New 615 driver branch. NVIDIA module must rebuild before reboot or you boot to a black screen.',
      notes: `NVIDIA 615.71.09 (RPM Fusion akmod)

* New production branch 615
* Wayland: explicit sync fixes for GNOME 50 on multi-monitor
* Fixes flicker after resume with DisplayPort MST
* Drops support for Maxwell (GTX 9xx) — not applicable here`,
      why: 'This is a branch change (610 → 615), not a point release, which is why it is caution rather than safe. The akmod builds the kernel module locally after install; Rivet runs that build as its own step and only offers the restart once the module for 7.2.5 exists. The explicit-sync fixes are relevant: you run two monitors on Wayland.',
      repo: 'rpmfusion-nonfree-updates', advisory: '—' },

    { id: 'gnome-software', group: 'system', name: 'Software', from: '50.3', to: '50.4', size: 6.4,
      risk: 'green', badge: null,
      verdict: 'Bug-fix release; safe. Stays installed for the app store, Rivet handles updates.',
      notes: `gnome-software 50.4

* Fix crash when a Flatpak remote is unreachable during refresh
* Correctly show “Restart required” for firmware updates
* Translation updates`,
      why: 'Point release of the same GNOME cycle. Safe. Rivet replaces its updater role, but it is harmless to keep it current for browsing and installing apps.',
      repo: 'updates', advisory: 'FEDORA-2026-b03e91' },

    gst('',                   3.2, 'Core library point release with bug fixes only; no format changes. Safe.'),
    gst('-plugins-base',      4.1, 'Point release; fixes float audio clipping. Safe.'),
    gst('-plugins-good',      2.8, 'Point release; Matroska seeking fix. Safe.'),
    gst('-plugins-bad-free',  5.6, 'Point release; AV1 parser fix. Safe.'),
    gst('-plugins-ugly-free', 1.9, 'Point release; x264 keyframe fix. Safe.'),

    { id: 'fd-platform', group: 'flatpak', name: 'org.freedesktop.Platform', from: '25.08', to: '25.08 (2026-09-12)', size: 610,
      risk: 'red', badge: 'BLOCKED', blockedBy: 'steam',
      verdict: 'Runtime shared by 14 apps. Steam is using it now; swapping it underneath breaks game launches.',
      notes: `org.freedesktop.Platform 25.08 — 2026-09-12 build

* glib 2.88.1, gtk4 4.20.2, pipewire 1.6.3
* mesa 25.2.4 in the GL extension
* Fixes CVE-2026-3902 in libxml2 (Medium)`,
      why: 'Steam (com.valvesoftware.Steam) is running on this runtime right now. Flatpak commits the new runtime atomically, but the running Steam keeps the old deployment mapped and any game launched afterwards gets the new one, which breaks Proton prefixes until Steam restarts. Marked as a conflict rather than a security risk: the bundled CVE is Medium and not reachable from a game. Quit Steam and this becomes a safe update.',
      repo: 'flathub', advisory: 'CVE-2026-3902 (libxml2, Medium)' },

    { id: 'fd-gl', group: 'flatpak', name: 'org.freedesktop.Platform.GL.default', from: '25.08', to: '25.08 (2026-09-12)', size: 190,
      risk: 'red', badge: 'BLOCKED', blockedBy: 'steam',
      verdict: 'Mesa 25.2.4 refresh for the runtime. Same Steam conflict as the runtime.',
      notes: `org.freedesktop.Platform.GL.default 25.08

* mesa 25.2.4: RADV/ANV fixes, zink stability
* Updated to match the 2026-09-12 runtime build`,
      why: 'Extension of the runtime above; it is installed and updated together with it and has the same Steam conflict. On its own it is a safe driver point release.',
      repo: 'flathub', advisory: '—' },

    { id: 'fd-i386', group: 'flatpak', name: 'org.freedesktop.Platform.Compat.i386', from: '25.08', to: '25.08 (2026-09-12)', size: 320,
      risk: 'red', badge: 'BLOCKED', blockedBy: 'steam',
      verdict: '32-bit compat layer used by Proton titles. Same Steam conflict.',
      notes: `org.freedesktop.Platform.Compat.i386 25.08

* 32-bit libraries rebuilt against the 2026-09-12 runtime
* Only used by Steam and Proton on this machine`,
      why: 'The only consumer of this extension here is Steam. Replacing it while Steam runs is exactly the case that breaks 32-bit game launches until Steam restarts, so it is blocked with the runtime.',
      repo: 'flathub', advisory: '—' },

    { id: 'dbx', group: 'firmware', name: 'Secure Boot dbx', from: '20260402', to: '20260707', size: 0.096,
      risk: 'yellow', badge: 'FIRMWARE', reboot: true,
      verdict: 'Adds recently revoked boot loaders to the UEFI blocklist. Applied at next reboot; cannot be rolled back.',
      notes: `UEFI dbx 2026-07-07 (LVFS)

* Revokes 27 additional shim/GRUB binaries affected by BootHole-class issues
* Applies via fwupd capsule on next boot
* Cannot be downgraded once applied`,
      why: 'Rivet checked the installed shim (shim-x64 15.9) and grub2-efi against the new revocation list: neither is on it, so the update will not make this machine unbootable. Caution only because dbx updates are one-way and need a reboot to land.',
      repo: 'lvfs', advisory: 'LVFS · dbx-20260707' },
  ];

  const byId = Object.fromEntries(UPDATES.map(u => [u.id, u]));
  const sortedUpdates = [...UPDATES].sort((a, b) => (b.critical ? 1 : 0) - (a.critical ? 1 : 0)); // critical security first

  /* ------------------------------------------------------------------ */
  /* State                                                               */
  /* ------------------------------------------------------------------ */
  const params = new URLSearchParams(location.search);
  const load = (k, d) => { try { const v = localStorage.getItem('rivet.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const save = (k, v) => { try { localStorage.setItem('rivet.' + k, JSON.stringify(v)); } catch (e) {} };

  const state = {
    view: 'updates',                 // updates | loading | empty | applying | done | topbar
    theme: document.documentElement.getAttribute('data-theme'),
    steamRunning: true,
    selected: new Set(),
    expanded: new Set(),
    query: '',
    menuOpen: false,
    modal: null,                     // 'settings' | null
    toast: null,
    lastChecked: '09:41',
    settings: load('settings', {
      interval: 'Every 30 minutes', redSecurity: true, redConflict: true, redCopr: false,
      notify: 'security', pauseSteam: true,
    }),
    apply: null,                     // { steps, step, progress, log, drawer, timer, start }
  };

  function isBlocked(u) { return !!u.blockedBy && state.steamRunning && state.settings.pauseSteam; }
  function riskOf(u) { return (u.blockedBy && !isBlocked(u)) ? 'green' : u.risk; }
  function badgeOf(u) { return (u.blockedBy && !isBlocked(u)) ? null : u.badge; }
  function verdictOf(u) { return (u.blockedBy && !isBlocked(u)) ? u.verdict.replace(/Steam is using it now.*$/, 'Steam is closed, safe to apply now.').replace(/Same Steam conflict.*$/, 'Safe now that Steam is closed.') : u.verdict; }
  function selectable(u) { return !isBlocked(u); }
  function selectSafe() { state.selected = new Set(UPDATES.filter(u => (selectable(u) && riskOf(u) !== 'red') || u.critical).map(u => u.id)); }
  function selectAllPossible() { state.selected = new Set(UPDATES.filter(selectable).map(u => u.id)); }
  selectAllPossible();

  const fmtMB = (mb) => mb >= 1000 ? (mb / 1000).toFixed(1) + ' GB' : mb < 1 ? Math.round(mb * 1000) + ' KB' : (mb < 10 ? mb.toFixed(1) : Math.round(mb)) + ' MB';
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ------------------------------------------------------------------ */
  /* Transitions                                                         */
  /* ------------------------------------------------------------------ */
  function go(view, extra) {
    if (state.apply && state.apply.timer) { clearInterval(state.apply.timer); state.apply.timer = null; }
    state.view = view;
    state.menuOpen = false;
    if (view === 'loading') {
      const dur = extra && extra.duration != null ? extra.duration : 3200;
      if (dur > 0) setTimeout(() => { if (state.view === 'loading') { state.lastChecked = nowHHMM(); go('updates'); } }, dur);
    }
    if (view === 'applying') startApply(extra || {});
    render();
  }
  function nowHHMM() { const d = new Date(); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }

  function setTheme(t) {
    state.theme = t; document.documentElement.setAttribute('data-theme', t); save('theme', t);
    try { localStorage.setItem('rivet.theme', t); } catch (e) {}
  }
  function toast(msg, spin, ms) {
    state.toast = { msg, spin }; render();
    if (ms) setTimeout(() => { state.toast = null; render(); }, ms);
  }

  /* ------------------------------------------------------------------ */
  /* Applying: stepped progress + log                                    */
  /* ------------------------------------------------------------------ */
  function buildSteps() {
    const ids = [...state.selected];
    const items = ids.map(id => byId[id]);
    const total = items.reduce((s, u) => s + u.size, 0);
    const nRpm = items.filter(u => u.group === 'system').length;
    const nFp = items.filter(u => u.group === 'flatpak').length;
    const nFw = items.filter(u => u.group === 'firmware').length;
    const nvidia = ids.includes('akmod-nvidia');
    const reboot = items.some(u => u.reboot);
    const parts = [nRpm && `${nRpm} packages`, nFp && `${nFp} Flatpak refs`, nFw && `${nFw} firmware`].filter(Boolean).join(' · ');
    const rpmName = (u) => `${u.name === 'Software' ? 'gnome-software' : u.name}-${u.to.split(' ')[0]}-1.fc44.x86_64`;

    const steps = [
      { id: 'download', label: 'Download', dur: 2600,
        detail: p => `${fmtMB(total * p)} of ${fmtMB(total)}`, done: `${fmtMB(total)} · done`,
        log: [
          [0.02, `Resolving ${ids.length} updates (dnf5, flatpak, fwupd)…`],
          ...items.filter(u => u.group === 'system').map((u, i, a) => [0.05 + 0.75 * (i / a.length), `Downloading ${rpmName(u)}.rpm … done (${fmtMB(u.size)})`]),
          ...items.filter(u => u.group === 'flatpak').map((u, i) => [0.82 + 0.05 * i, `flatpak: pulling ${u.name}/x86_64/25.08 … done (${fmtMB(u.size)})`]),
          ...items.filter(u => u.group === 'firmware').map(u => [0.95, `fwupd: downloaded dbx-${u.to}.cab (${fmtMB(u.size)})`]),
          [0.99, 'Verifying GPG signatures … ok', 'ok'],
        ] },
      { id: 'install', label: 'Install', dur: 2600,
        detail: p => `${Math.max(1, Math.round(ids.length * p))} of ${ids.length} · ${parts}`, done: `${parts} · done`,
        log: [
          [0.02, 'Running transaction check … ok'],
          ...items.filter(u => u.group === 'system').map((u, i, a) => [0.05 + 0.7 * (i / a.length), `Installing : ${rpmName(u)} [${i + 1}/${a.length}]`]),
          ...(nvidia ? [[0.78, 'Running scriptlet: akmods --kernels 7.2.5-300.fc44.x86_64']] : []),
          ...items.filter(u => u.group === 'flatpak').map((u, i) => [0.8 + 0.05 * i, `flatpak: deploying ${u.name} … done`]),
          ...items.filter(u => u.group === 'firmware').map(() => [0.93, 'fwupd: scheduled dbx capsule for next boot']),
          [0.98, 'Transaction complete', 'ok'],
        ] },
      ...(nvidia ? [{ id: 'nvidia', label: 'Building NVIDIA kernel module', dur: 3000, warn: true,
        detail: p => `${Math.round(3 * (1 - p) * 60 / 60 * 10) / 10 || '<1'} min left · please wait, do not reboot yet`,
        done: 'nvidia 615.71.09 for kernel 7.2.5 · built and signed',
        log: [
          [0.0, 'akmods: Building NVIDIA module 615.71.09 for kernel 7.2.5-300.fc44', 'warn'],
          [0.03, 'akmods: this may take several minutes — do not power off', 'warn'],
          [0.1, 'nvidia-kmod: CC [M] nvidia/nv-frontend.o'],
          [0.25, 'nvidia-kmod: CC [M] nvidia/nv-instance.o'],
          [0.4, 'nvidia-kmod: CC [M] nvidia-drm/nvidia-drm-gem.o'],
          [0.55, 'nvidia-kmod: CC [M] nvidia-modeset/nvidia-modeset-linux.o'],
          [0.7, 'nvidia-kmod: LD [M] nvidia.ko  nvidia-drm.ko  nvidia-modeset.ko  nvidia-uvm.ko'],
          [0.85, 'akmods: signing modules with MOK (Machine Owner Key)'],
          [0.97, 'akmods: kmod-nvidia-7.2.5-300.fc44.x86_64-615.71.09 installed', 'ok'],
        ] }] : []),
      { id: 'verify', label: 'Verify', dur: 1400,
        detail: () => 'boot entries, module signatures, Flatpak deployments', done: 'everything checks out',
        log: [
          [0.05, 'Checking /boot/loader/entries … 7.2.5 present, default'],
          ...(nvidia ? [[0.35, 'modinfo nvidia (7.2.5): 615.71.09, signed, matches installed driver', 'ok']] : []),
          [0.6, 'dracut: initramfs for 7.2.5 built (32 MB)'],
          [0.8, 'flatpak: all runtimes deployed cleanly'],
          [0.95, 'Verification complete', 'ok'],
        ] },
      { id: 'restart', label: reboot ? 'Restart' : 'Finish', dur: 0, detail: () => reboot ? 'kernel 7.2.5 needs a reboot' : 'no reboot needed', done: '' },
    ];
    return { steps, total, reboot, nvidia, flatpak: nFp > 0, firmware: nFw > 0, count: ids.length };
  }

  function startApply(opts) {
    const b = buildSteps();
    const speed = parseFloat(params.get('speed')) || 1;
    state.apply = { ...b, step: 0, progress: 0, log: [], drawer: opts.drawer !== false, timer: null, logged: new Set(), t: 0 };
    const jump = Math.min(parseInt(opts.step || 0, 10) || 0, b.steps.length - 2);
    for (let i = 0; i < jump; i++) { finishStep(i); }
    if (jump) state.apply.progress = 0.45;
    if (opts.paused) return;
    let last = performance.now();
    state.apply.timer = setInterval(() => {
      const now = performance.now(); const dt = (now - last) * speed; last = now;
      tickApply(dt);
    }, 60);
  }
  function finishStep(i) {
    const a = state.apply; const s = a.steps[i];
    s.log.forEach(([, line, cls], j) => { const k = i + ':' + j; if (!a.logged.has(k)) { a.logged.add(k); a.log.push({ line, cls }); } });
    s.state = 'done';
    a.step = i + 1; a.progress = 0;
  }
  function tickApply(dt) {
    const a = state.apply; if (!a) return;
    const s = a.steps[a.step];
    if (!s || s.dur === 0) { clearInterval(a.timer); a.timer = null; state.view = 'done'; render(); return; }
    a.progress = Math.min(1, a.progress + dt / s.dur);
    s.log.forEach(([at, line, cls], j) => { const k = a.step + ':' + j; if (a.progress >= at && !a.logged.has(k)) { a.logged.add(k); a.log.push({ line, cls }); } });
    if (a.progress >= 1) finishStep(a.step);
    render();
  }

  /* ------------------------------------------------------------------ */
  /* Rendering                                                           */
  /* ------------------------------------------------------------------ */
  const $ = (s) => document.querySelector(s);
  const el = { win: $('#window'), main: $('#main'), sub: $('#brand-sub'), name: $('#brand-name'), actions: $('#header-actions'),
    menu: $('#menu'), overlay: $('#overlay'), search: $('#search'), searchWrap: $('#search-wrap'), themeBtn: $('#theme-btn'), steamItem: $('#steam-item') };

  const svgChev = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
  const svgX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';

  function render() {
    el.win.classList.toggle('is-topbar', state.view === 'topbar');
    el.menu.hidden = !state.menuOpen;
    el.steamItem.textContent = state.steamRunning ? 'Steam: running · simulate quitting it' : 'Steam: closed · simulate launching it';
    el.themeBtn.querySelector('.sun').style.display = state.theme === 'dark' ? '' : 'none';
    el.themeBtn.querySelector('.moon').style.display = state.theme === 'dark' ? 'none' : '';
    el.searchWrap.style.display = state.view === 'updates' ? '' : 'none';
    renderHeader();
    const views = { updates: renderUpdates, loading: renderLoading, empty: renderEmpty, applying: renderApplying, done: renderDone, topbar: renderTopbar };
    el.main.innerHTML = views[state.view]();
    if (state.view === 'applying') { const log = el.main.querySelector('.log'); if (log) log.scrollTop = log.scrollHeight; }
    el.overlay.innerHTML = (state.modal === 'settings' ? renderSettings() : '') + (state.toast ? renderToast() : '');
  }

  function renderHeader() {
    const sel = [...state.selected].map(id => byId[id]);
    const n = sel.length;
    const subs = {
      updates: `Fedora 44 · checked ${state.lastChecked}`,
      loading: 'Checking…',
      empty: `Fedora 44 · checked ${state.lastChecked}`,
      applying: state.apply ? `Step ${Math.min(state.apply.step + 1, state.apply.steps.length)} of ${state.apply.steps.length} · ${etaText()}` : '',
      done: 'Fedora 44 · updated just now',
      topbar: '',
    };
    el.name.textContent = state.view === 'applying' ? 'Updating…' : 'Rivet';
    el.sub.textContent = subs[state.view];
    let html = '';
    if (state.view === 'updates') {
      html = `<button class="btn" data-action="select-safe" title="Select only green + critical security items">Safe only</button>
              <button class="btn primary" data-action="update-now" ${n ? '' : 'disabled'}>Update now <span class="count">${n}</span></button>`;
    } else if (state.view === 'applying') {
      const canCancel = state.apply && state.apply.step === 0;
      html = `<button class="btn" data-action="cancel-apply" ${canCancel ? '' : 'disabled title="Cannot cancel after download"'}>Cancel</button>`;
    }
    el.actions.innerHTML = html;
  }
  function etaText() {
    const a = state.apply; let ms = 0;
    for (let i = a.step; i < a.steps.length; i++) ms += a.steps[i].dur * (i === a.step ? (1 - a.progress) : 1);
    const min = Math.ceil(ms / 60000 * 60); // demo seconds → "minutes"
    return `about ${min} minute${min === 1 ? '' : 's'} left`;
  }

  /* ---- Updates list ---- */
  function renderUpdates() {
    const q = state.query.trim().toLowerCase();
    const matches = (u) => !q || (u.name + ' ' + verdictOf(u) + ' ' + u.from + ' ' + u.to + ' ' + (u.advisory || '')).toLowerCase().includes(q);
    const all = sortedUpdates;
    const reboot = all.filter(u => u.reboot).length;
    const security = all.filter(u => u.critical).length;
    const blocked = all.filter(isBlocked).length;
    const sel = [...state.selected].map(id => byId[id]);
    const selSize = sel.reduce((s, u) => s + u.size, 0);
    const totalSize = all.reduce((s, u) => s + u.size, 0);
    const selectableAll = all.filter(selectable);
    const allOn = selectableAll.every(u => state.selected.has(u.id));
    const anyOn = selectableAll.some(u => state.selected.has(u.id));
    const selReboot = sel.some(u => u.reboot);

    let out = `<div class="summary">
      <span class="stat"><b>${all.length}</b> updates</span>
      <span class="stat warn" title="kernel, akmod-nvidia, Secure Boot dbx"><b>${reboot}</b> need reboot</span>
      <span class="stat bad"><b>${security}</b> security</span>
      <span class="stat bad"><b>${blocked}</b> blocked</span>
      ${selReboot ? `<span class="chip" title="The selection includes the kernel; a restart will be offered at the end"><span class="dot"></span>REBOOT REQUIRED</span>` : ''}
      ${security ? `<span class="chip red"><span class="dot"></span>CRITICAL SECURITY</span>` : ''}
      <span class="spacer"></span>
      <span class="meta"><span>${fmtMB(selSize)} selected · ${fmtMB(totalSize)} total</span>
        <label class="selall" data-action="select-all-global"><span class="cb ${allOn ? 'on' : anyOn ? 'mixed' : ''}"></span>Select all</label></span>
    </div>
    <div class="list" id="list">`;

    let shown = 0;
    for (const g of GROUPS) {
      const items = all.filter(u => u.group === g.id && matches(u));
      if (!items.length) continue;
      shown += items.length;
      const gs = items.filter(selectable);
      const gAll = gs.length && gs.every(u => state.selected.has(u.id));
      const gAny = gs.some(u => state.selected.has(u.id));
      const gBlocked = items.some(isBlocked);
      out += `<div class="group-h"><span class="group-title">${g.title}<span class="n">${items.length}</span></span>
        <label class="selall" data-action="select-group" data-group="${g.id}"><span>Select all</span><span class="cb ${gAll ? 'on' : gAny ? 'mixed' : ''} ${gs.length ? '' : 'disabled'}"></span></label></div>
        <div class="card">`;
      if (gBlocked) {
        out += `<div class="banner red"><span class="risk red"></span><span>Steam is running. Quit Steam before applying Flatpak runtime updates, or game launches break until Steam restarts.</span>
          <span class="spacer"></span><button class="btn danger" data-action="quit-steam">Quit Steam</button></div>`;
      }
      out += items.map(renderRow).join('');
      out += `</div>`;
    }
    if (!shown) out += `<div class="empty-search">Nothing matches “${esc(state.query)}”. <button class="btn small ghost" data-action="clear-search">Clear</button></div>`;
    out += `</div>`;
    return out;
  }

  function renderRow(u) {
    const blocked = isBlocked(u);
    const on = state.selected.has(u.id);
    const open = state.expanded.has(u.id);
    const risk = riskOf(u); const badge = badgeOf(u);
    const badgeCls = u.critical ? 'critical' : risk === 'red' ? 'red' : risk === 'yellow' ? 'yellow' : 'green';
    const riskLabel = { green: 'Safe', yellow: 'Caution', red: u.critical ? 'Critical security' : 'Conflict' }[risk];
    let html = `<div class="row ${blocked ? 'blocked' : ''} ${open ? 'open' : ''}" data-action="expand" data-id="${u.id}" role="button" aria-expanded="${open}">
      <span class="cb ${on ? 'on' : ''} ${blocked ? 'disabled' : ''}" data-action="toggle-row" data-id="${u.id}" role="checkbox" aria-checked="${on}" tabindex="0" title="${blocked ? 'Blocked while Steam runs' : 'Include in this run'}"></span>
      <span class="risk ${risk}" title="${riskLabel}"></span>
      <div class="body"><div><span class="name">${esc(u.name)}</span><span class="ver">${esc(u.from)} → ${esc(u.to)}${u.critical ? ' · CVE-2026-4417' : ''}</span></div>
        <div class="verdict">${esc(verdictOf(u))}</div></div>
      ${u.critical ? `<span class="badge critical">CRITICAL SECURITY</span>` : badge ? `<span class="badge ${badgeCls}">${badge}</span>` : ''}
      <span class="size">${fmtMB(u.size)}</span>${svgChev}</div>`;
    if (open) {
      html += `<div class="expand">
        <div><h4>RELEASE NOTES · EXCERPT</h4><div class="notes">${esc(u.notes)}</div>
          <div class="meta"><span>repo: ${esc(u.repo)}</span><span>advisory: ${esc(u.advisory)}</span><a href="#" data-action="noop">full changelog ↗</a></div></div>
        <div><h4>WHY ${riskLabel.toUpperCase()}</h4><div class="why"><p>${esc(u.why)}</p></div></div>
      </div>`;
    }
    if (u.banner && !blocked) html += `<div class="banner yellow"><span class="ic">⚠</span><span>${esc(u.banner)}</span></div>`;
    return html;
  }

  /* ---- Applying ---- */
  function renderApplying() {
    const a = state.apply;
    const nDone = a.steps.filter(s => s.state === 'done').length;
    const overall = (nDone + (a.steps[a.step] && a.steps[a.step].dur ? a.progress : 0)) / (a.steps.length - 1);
    const rows = a.steps.map((s, i) => {
      const st = s.state === 'done' ? 'done' : i === a.step ? 'active' : 'pending';
      const d = st === 'done' ? s.done : st === 'active' ? (typeof s.detail === 'function' ? s.detail(a.progress) : s.detail) : (i === a.steps.length - 1 ? s.detail() : '');
      const pct = st === 'active' && s.dur ? `<span class="pct">${Math.round(a.progress * 100)}%</span>` : '';
      return `<div class="step ${st} ${s.warn ? 'warn' : ''}"><span class="ic">${st === 'done' ? '✓' : ''}</span>
        <div><div class="t">${esc(s.label)}${st === 'active' && s.dur ? '…' : ''}</div>${d ? `<div class="d">${esc(d)}</div>` : ''}</div>${pct}</div>`;
    }).join('');
    const tools = ['dnf5', a.nvidia && 'akmods', a.flatpak && 'flatpak', a.firmware && 'fwupd'].filter(Boolean).join(' · ');
    return `<div class="progress"><i style="width:${Math.round(overall * 100)}%"></i></div>
      <div class="applying"><div class="center">
        <div class="steps">${rows}</div>
        <div class="applying-note">${a.nvidia ? '⚠ Rivet will not offer a restart until the NVIDIA module for kernel 7.2.5 has been built and verified.' : 'You can keep using the computer while this runs.'}</div>
      </div></div>
      <div class="drawer ${a.drawer ? '' : 'closed'}">
        <div class="drawer-h" data-action="toggle-drawer"><span class="tri">▼</span><span class="t">Details</span><span class="s">${tools}</span><kbd>Esc closes</kbd></div>
        <div class="log">${a.log.map((l, i) => `<div class="${l.cls || ''} ${i === a.log.length - 1 && a.steps[a.step] && a.steps[a.step].dur ? 'cur' : ''}">${esc(l.line)}</div>`).join('') || '<div class="dim">Waiting for transaction…</div>'}</div>
      </div>`;
  }

  /* ---- Done ---- */
  function renderDone() {
    const a = state.apply || buildSteps();
    const reboot = a.reboot;
    return `<div class="hero"><div class="inner">
      <div class="ring">✓</div>
      <h1>All done</h1>
      <p>${reboot
        ? `${a.count} updates applied. Restart required for kernel 7.2.5.${a.nvidia ? ' The NVIDIA module was rebuilt and verified — it is safe to reboot.' : ''}${state.selected.has('dbx') ? ' The Secure Boot dbx update lands during the restart.' : ''}`
        : `${a.count} updates applied. No restart needed.`}</p>
      <div class="actions">
        ${reboot ? `<button class="btn primary" data-action="restart-now">Restart now</button><button class="btn" data-action="restart-later">Later</button>`
                 : `<button class="btn primary" data-action="restart-later">Close</button>`}
      </div>
    </div></div>`;
  }

  /* ---- Empty ---- */
  function renderEmpty() {
    return `<div class="hero"><div class="inner">
      <div class="ring">✓</div>
      <h1>You’re up to date</h1>
      <p>Last checked today at ${state.lastChecked}. Fedora 44, kernel 7.2.5, NVIDIA 615.71.09.</p>
      <div class="actions"><button class="btn" data-action="check-now">Check now</button></div>
    </div></div>`;
  }

  /* ---- Loading ---- */
  function renderLoading() {
    const widths = [[140, 220], [190, 150], [120, 250], [170, 130], [150, 200], [110, 180]];
    return `<div class="progress indet"><i></i></div>
      <div class="loading"><div class="center">
        <h2>Reviewing release notes…</h2>
        <div class="sub">reading ${UPDATES.length} changelogs · asking the model for a verdict on each</div>
        <div class="card">${widths.map(([a, b]) => `<div class="skel-row"><span class="skel box"></span><div style="flex:1"><div class="skel l1" style="width:${a}px"></div><div class="skel l2" style="width:${b}px"></div></div><span class="skel end"></span></div>`).join('')}</div>
      </div></div>`;
  }

  /* ---- Top bar + notification mock ---- */
  function renderTopbar() {
    const all = sortedUpdates;
    const blocked = all.filter(isBlocked).length;
    return `<div class="desktop">
      <div class="gnome-bar">
        <div class="pill"><span style="width:14px;height:5px;border-radius:3px;background:#fff"></span><span style="width:5px;height:5px;border-radius:50%;background:rgba(255,255,255,.45)"></span></div>
        <span class="spacer"></span><span class="clock">Sep 15&nbsp;&nbsp;09:41</span><span class="spacer"></span>
        <div class="tray" data-action="sim-updates" title="Rivet: ${all.length} updates">
          <span class="upk"><span style="font-size:12px;font-weight:800">⟳</span><span class="n">${all.length}</span><span class="alert"></span></span>
          <span class="batt"></span><span style="font-size:11px;font-weight:700">▾</span>
        </div>
      </div>
      <div class="notif">
        <div class="top"><div class="app-icon">⟳</div><div>
          <div class="t">Rivet: ${all.length} updates</div>
          <div class="d">2 need reboot (kernel, NVIDIA), 1 critical security fix${blocked ? ', 3 wait on Steam' : ''}</div></div></div>
        <div class="acts"><button data-action="sim-updates">Review</button><span class="vs"></span><button data-action="update-safe">Update safe ones</button></div>
      </div>
      <div class="mini-window">
        <div class="brand"><div class="app-icon" style="width:26px;height:26px;border-radius:8px;font-size:13px">⟳</div><div><div class="brand-name" style="font-size:13px">Top-bar badge</div><div class="brand-sub">What the tray shows on hover</div></div></div>
        <div class="ln"><span class="risk red"></span><b>openssl</b><span>CVE-2026-4417</span><span class="ver">3.5.1 → 3.5.2</span></div>
        <div class="ln"><span class="risk yellow"></span><b>kernel</b><span>reboot</span><span class="ver">7.2.4 → 7.2.5</span></div>
        <div class="ln"><span class="risk yellow"></span><b>akmod-nvidia</b><span>rebuild</span><span class="ver">610 → 615</span></div>
        <div class="ln"><span class="risk ${blocked ? 'red' : 'green'}"></span><b>Flatpak runtime</b><span>${blocked ? 'blocked by Steam' : 'ready'}</span><span class="ver">25.08</span></div>
        <div class="ln" style="color:var(--fg-2)">+ ${all.length - 4} more, all safe</div>
      </div>
      <div class="mock-caption">GNOME top bar + notification mock <kbd>Esc</kbd> back to the app <button data-action="sim-updates">Open Rivet</button></div>
    </div>`;
  }

  /* ---- Settings modal ---- */
  function renderSettings() {
    const s = state.settings;
    const check = (k) => `<span class="cb ${s[k] ? 'on' : ''}"></span>`;
    const radio = (v) => `<span class="radio ${s.notify === v ? 'on' : ''}"></span>`;
    return `<div class="backdrop" data-action="close-modal"><div class="modal" role="dialog" aria-label="Preferences" data-stop>
      <div class="modal-h"><div class="app-icon" style="width:26px;height:26px;border-radius:8px;font-size:13px">⟳</div><span class="t">Preferences</span>
        <button class="btn icon" data-action="close-modal" aria-label="Close">${svgX}</button></div>
      <div class="modal-b">
        <div class="set-h">APPEARANCE</div>
        <div class="set-card"><div class="set-row" style="cursor:default"><div><div class="l">Theme</div><div class="s">Remembered on this machine</div></div>
          <div class="right"><span class="seg"><button class="${state.theme === 'dark' ? 'on' : ''}" data-action="set-theme" data-value="dark">Dark</button><button class="${state.theme === 'light' ? 'on' : ''}" data-action="set-theme" data-value="light">Light</button></span></div></div></div>

        <div class="set-h">CHECKING FOR UPDATES</div>
        <div class="set-card"><div class="set-row" style="cursor:default"><div class="l">Check interval</div>
          <div class="right"><select class="select" data-action="set-interval">${['Every 15 minutes', 'Every 30 minutes', 'Every hour', 'Daily', 'Manually'].map(o => `<option ${s.interval === o ? 'selected' : ''}>${o}</option>`).join('')}</select></div></div></div>

        <div class="set-h">WHAT COUNTS AS RED</div>
        <div class="set-desc">Red items are excluded from “Safe only” and always flagged in the top bar.</div>
        <div class="set-card">
          <div class="set-row" data-action="set-toggle" data-key="redSecurity"><div class="l">Security advisories rated High or Critical</div><div class="right">${check('redSecurity')}</div></div>
          <div class="set-row" data-action="set-toggle" data-key="redConflict"><div class="l">Conflicts with running apps</div><div class="right">${check('redConflict')}</div></div>
          <div class="set-row" data-action="set-toggle" data-key="redCopr"><div class="l">Third-party COPR version bumps</div><div class="right">${check('redCopr')}</div></div>
        </div>

        <div class="set-h">NOTIFICATIONS</div>
        <div class="set-card">
          <div class="set-row" data-action="set-notify" data-value="security"><div class="l">Security fixes only</div><div class="right">${radio('security')}</div></div>
          <div class="set-row" data-action="set-notify" data-value="any"><div class="l">Any update</div><div class="right">${radio('any')}</div></div>
          <div class="set-row" data-action="set-notify" data-value="never"><div class="l">Never</div><div class="right">${radio('never')}</div></div>
        </div>

        <div class="set-h">APPS</div>
        <div class="set-card"><div class="set-row" data-action="set-toggle" data-key="pauseSteam"><div><div class="l">Pause Flatpak updates while Steam runs</div><div class="s">Prevents runtime swaps under a running game</div></div>
          <div class="right"><span class="toggle ${s.pauseSteam ? 'on' : ''}"></span></div></div></div>
      </div></div></div>`;
  }
  function renderToast() { return `<div class="toast">${state.toast.spin ? '<span class="spin"></span>' : ''}${esc(state.toast.msg)}</div>`; }

  /* ------------------------------------------------------------------ */
  /* Actions (event delegation)                                          */
  /* ------------------------------------------------------------------ */
  const actions = {
    'toggle-theme': () => setTheme(state.theme === 'dark' ? 'light' : 'dark'),
    'set-theme': (t) => setTheme(t.dataset.value),
    'toggle-menu': () => { state.menuOpen = !state.menuOpen; },
    'open-settings': () => { state.modal = 'settings'; state.menuOpen = false; },
    'close-modal': () => { state.modal = null; },
    'check-now': () => go('loading'),
    'sim-updates': () => go('updates'),
    'sim-empty': () => go('empty'),
    'sim-loading': () => go('loading', { duration: 0 }),
    'sim-applying': () => go('applying'),
    'sim-done': () => { if (!state.apply) state.apply = { ...buildSteps() }; go('done'); },
    'sim-topbar': () => go('topbar'),
    'toggle-steam': () => { state.steamRunning = !state.steamRunning; state.menuOpen = false; reconcileSelection(); },
    'quit-steam': () => { state.steamRunning = false; reconcileSelection(); toast('Steam closed. Flatpak runtime updates are unblocked.', false, 2500); },
    'select-safe': () => selectSafe(),
    'select-all-global': () => { const s = UPDATES.filter(selectable); const all = s.every(u => state.selected.has(u.id)); state.selected = new Set(all ? [] : s.map(u => u.id)); },
    'select-group': (t) => { const s = UPDATES.filter(u => u.group === t.dataset.group && selectable(u)); const all = s.every(u => state.selected.has(u.id)); s.forEach(u => all ? state.selected.delete(u.id) : state.selected.add(u.id)); },
    'toggle-row': (t) => { const u = byId[t.dataset.id]; if (!selectable(u)) return; state.selected.has(u.id) ? state.selected.delete(u.id) : state.selected.add(u.id); },
    'expand': (t) => { const id = t.dataset.id; state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id); },
    'update-now': () => { if (state.selected.size) go('applying'); },
    'update-safe': () => { selectSafe(); go('applying'); },
    'cancel-apply': () => { if (state.apply && state.apply.step === 0) { go('updates'); toast('Update cancelled. Nothing was changed.', false, 2500); } },
    'toggle-drawer': () => { state.apply.drawer = !state.apply.drawer; },
    'restart-now': () => { toast('Restarting…', true); setTimeout(() => { state.toast = null; state.selected = new Set(); go('empty'); }, 1800); },
    'restart-later': () => { state.selected = new Set(); go('empty'); toast('Rivet will remind you to restart in an hour.', false, 3000); },
    'clear-search': () => { state.query = ''; el.search.value = ''; },
    'set-toggle': (t) => { state.settings[t.dataset.key] = !state.settings[t.dataset.key]; save('settings', state.settings); reconcileSelection(); },
    'set-notify': (t) => { state.settings.notify = t.dataset.value; save('settings', state.settings); },
    'set-interval': () => {},
    'noop': () => {},
  };
  function reconcileSelection() { UPDATES.forEach(u => { if (!selectable(u)) state.selected.delete(u.id); else if (u.blockedBy) state.selected.add(u.id); }); }

  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-action]');
    if (!t) { if (state.menuOpen && !e.target.closest('#menu')) { state.menuOpen = false; render(); } return; }
    if (t.tagName === 'SELECT') return;
    if (t.dataset.action === 'close-modal' && t.classList.contains('backdrop') && e.target !== t) return; // clicks inside the dialog
    e.preventDefault();
    e.stopPropagation();
    const fn = actions[t.dataset.action];
    if (fn) { fn(t, e); render(); }
  });
  document.addEventListener('change', (e) => {
    const t = e.target.closest('[data-action="set-interval"]');
    if (t) { state.settings.interval = t.value; save('settings', state.settings); }
  });
  el.search.addEventListener('input', () => { state.query = el.search.value; render(); });

  document.addEventListener('keydown', (e) => {
    const inField = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (inField && document.activeElement === el.search) { if (el.search.value) { state.query = ''; el.search.value = ''; } else el.search.blur(); render(); return; }
      if (state.menuOpen) state.menuOpen = false;
      else if (state.modal) state.modal = null;
      else if (state.view === 'applying' && state.apply.drawer) state.apply.drawer = false;
      else if (state.view === 'topbar') state.view = 'updates';
      else if (state.view === 'done') return;
      render(); return;
    }
    if (inField) { if (e.key === 'Enter' && document.activeElement === el.search) el.search.blur(); return; }
    if (e.key === '/') { e.preventDefault(); if (state.view === 'updates') { el.search.focus(); el.search.select(); } }
    else if (e.key === ',' ) { state.modal = 'settings'; render(); }
    else if (e.key === 'r' && !e.ctrlKey && !e.metaKey && state.view !== 'applying') { go('loading'); }
    else if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('cb')) { e.target.click(); }
    else if (e.key === ' ' && e.target.classList && e.target.classList.contains('cb')) { e.preventDefault(); e.target.click(); }
  });

  /* ------------------------------------------------------------------ */
  /* Boot: ?state=updates|loading|empty|applying|done|topbar|settings    */
  /*       &theme=dark|light  &step=N (applying)  &speed=2  &paused=1    */
  /*       &steam=0  &drawer=0  &expand=id,id  &scroll=end|px  &menu=1   */
  /*       &q=text (prefilled filter)                                    */
  /* ------------------------------------------------------------------ */
  if (params.get('steam') === '0') { state.steamRunning = false; reconcileSelection(); }
  if (params.get('expand')) params.get('expand').split(',').forEach(id => byId[id] && state.expanded.add(id));
  if (params.get('menu') === '1') state.menuOpen = true;
  if (params.get('q')) { state.query = params.get('q'); el.search.value = state.query; }
  const s0 = params.get('state') || 'updates';
  if (s0 === 'settings') { state.modal = 'settings'; render(); }
  else if (s0 === 'loading') go('loading', { duration: params.has('hold') ? 0 : 3200 });
  else if (s0 === 'applying') go('applying', { step: params.get('step'), paused: params.get('paused') === '1', drawer: params.get('drawer') !== '0' });
  else if (s0 === 'done') { state.apply = { ...buildSteps() }; go('done'); }
  else go(['updates', 'empty', 'topbar'].includes(s0) ? s0 : 'updates');

  if (params.get('scroll')) { const l = $('#list'); if (l) l.scrollTop = params.get('scroll') === 'end' ? 1e6 : parseInt(params.get('scroll'), 10) || 0; }
  window.rivet = { state, go, render }; // for poking at it in devtools
})();
