/* Rivet · prototype B · one vanilla-JS state machine, no framework, all mock. */
(() => {
  'use strict';

  /* ======================================================================
   * Mock data — a realistic pending set
   * ==================================================================== */
  const GROUPS = [
    { id: 'dnf',     title: 'System packages', src: 'dnf' },
    { id: 'flatpak', title: 'Apps & runtimes',  src: 'Flatpak' },
    { id: 'fwupd',   title: 'Firmware',         src: 'fwupd' },
  ];

  const ITEMS = [
    {
      id: 'openssl', group: 'dnf', name: 'openssl', from: '3.5.1', to: '3.5.2', size: '7.1 MB', bytes: 7.1,
      risk: 'red', badge: { cls: 'solid-red', text: 'Critical security' }, noteInline: 'CVE-2026-4417',
      verdict: 'Fixes a remotely triggerable TLS handshake crash. Apply today; no restart needed, long-running services reload on their own.',
      notes: `openssl-3.5.2-1.fc44\n* Security fix for CVE-2026-4417: NULL dereference in TLS 1.3\n  PSK binder verification (remote DoS, CVSS 7.5)\n* Fix CVE-2026-4380: timing side channel in RSA-PSS decoding\n* Bump libssl/libcrypto ABI-compatible`,
      reasoning: 'Two CVEs, one of them reachable pre-authentication over the network. Nothing in the changelog changes behaviour or config formats, so the fix is low-risk and high-value. Rated <strong>critical security</strong> and pinned to the top.',
    },
    {
      id: 'kernel', group: 'dnf', name: 'kernel', from: '7.2.4', to: '7.2.5', size: '148 MB', bytes: 148,
      risk: 'yellow', badge: { cls: 'yellow', text: 'Reboot' }, reboot: true,
      verdict: 'Stable point release: amdgpu and xe fixes, a Btrfs corner case. Safe, but the new kernel only runs after a restart.',
      notes: `kernel-7.2.5-300.fc44\n* Linux 7.2.5 stable update\n* drm/amdgpu: fix VRAM leak on suspend/resume\n* drm/xe: restore VRR after DPMS off\n* btrfs: avoid ENOSPC during relocation of zoned devices\n* wifi: iwlwifi: fix rare firmware assert on channel switch`,
      reasoning: 'Point release from the stable series, no config changes and no ABI-affecting driver changes. The only cost is a reboot, and because <strong>akmod-nvidia</strong> ships alongside it, Rivet will build the NVIDIA module for 7.2.5 before offering the restart.',
      alert: 'Kernel + NVIDIA akmod: the module must rebuild before reboot. Rivet waits for it and verifies the build.',
    },
    {
      id: 'akmod-nvidia', group: 'dnf', name: 'akmod-nvidia', from: '610.57.04', to: '615.71.09', size: '312 MB', bytes: 312,
      risk: 'yellow', badge: { cls: 'yellow', text: 'Rebuild' }, reboot: true, noteInline: 'NVIDIA module must rebuild before reboot',
      verdict: 'New NVIDIA driver branch. It will be compiled against kernel 7.2.5 during install; expect 2–3 minutes and do not restart mid-build.',
      notes: `akmod-nvidia-615.71.09-1.fc44 (RPM Fusion)\n* Update to 615.71.09 (new feature branch)\n* Fixes flicker with VRR + multiple monitors on Wayland\n* Adds nvidia-drm.fbdev=1 default on kernels >= 7.1\n* Drops support for Maxwell (GM10x) GPUs`,
      reasoning: 'Driver branch change, not just a point release, and the akmod has to compile a fresh kernel module. Mismatched module and kernel means a black screen at boot, so the update is <strong>caution</strong>: Rivet builds the module, verifies it loads, then asks to restart. Your RTX card is not in the dropped-Maxwell list.',
    },
    {
      id: 'gnome-software', group: 'dnf', name: 'Software', pkg: 'gnome-software', from: '50.3', to: '50.4', size: '4.2 MB', bytes: 4.2,
      risk: 'green', badge: null,
      verdict: 'Translation updates and a fix for a spurious “restart required” banner. Routine.',
      notes: `gnome-software-50.4-1.fc44\n* Fix false “Restart required” after Flatpak-only updates\n* Update translations (de, fr, pt_BR, ja)\n* Fix a crash when an app has no screenshots`,
      reasoning: 'Bug-fix release with no dependency changes. Rated <strong>safe</strong>. (Yes, Rivet still updates the app it replaces.)',
    },
    {
      id: 'gstreamer', group: 'dnf', name: 'GStreamer codecs', pkgCount: 5, from: '1.28.6', to: '1.28.7', size: '18 MB', bytes: 18,
      risk: 'green', badge: null,
      pkgs: ['gstreamer1', 'gstreamer1-plugins-base', 'gstreamer1-plugins-good', 'gstreamer1-plugins-bad-free', 'gstreamer1-plugin-libav'],
      verdict: 'Five codec packages moving together. Playback fixes for AV1 and HEVC streams; apps pick it up next launch.',
      notes: `gstreamer1-1.28.7 (5 packages)\n* av1dec: fix green frames after seek on dav1d 1.5\n* hlsdemux2: handle discontinuities in live playlists\n* pulsesink: fix underrun when device switches mid-stream\n* libav: bump to FFmpeg 8.0.2`,
      reasoning: 'Patch release across the whole 1.28 set, all five packages must move together and do. No plugin API changes. Rated <strong>safe</strong>.',
    },
    {
      id: 'fd-platform', group: 'flatpak', name: 'org.freedesktop.Platform', from: '25.08 · 3f1a9c2', to: '8be07d1', size: '610 MB', bytes: 610,
      risk: 'red', badge: { cls: 'red', text: 'Blocked' }, blocked: true, noteInline: 'Steam is running: quit it first',
      verdict: 'Shared runtime that Steam is using right now. Swapping it under a running game breaks launches until Steam restarts.',
      notes: `org.freedesktop.Platform 25.08 (flathub)\n* glibc 2.42.1, mesa 25.2.4, pipewire 1.4.3\n* Fix crash in libwayland when compositor restarts\n* Security: curl CVE-2026-3991`,
      reasoning: 'Runtime updates are normally safe, but <strong>Steam (com.valvesoftware.Steam) has this runtime mounted</strong>. Flatpak keeps the old deployment until the app exits, and game launches that fork new sandboxes see a mismatch. Rated <strong>conflict</strong> until Steam quits.',
    },
    {
      id: 'fd-gl', group: 'flatpak', name: 'org.freedesktop.Platform.GL.default', from: '25.08 · 91c0e4a', to: 'd27f5b0', size: '96 MB', bytes: 96,
      risk: 'red', badge: { cls: 'red', text: 'Blocked' }, blocked: true, noteInline: 'Steam is running: quit it first',
      verdict: 'Mesa 25.2.4 for Flatpak apps. Same Steam conflict as the base runtime.',
      notes: `org.freedesktop.Platform.GL.default 25.08\n* mesa 25.2.3 → 25.2.4 (radv, zink fixes)`,
      reasoning: 'Extension of the runtime Steam is using. Rated <strong>conflict</strong> for the same reason, it unblocks the moment Steam exits.',
    },
    {
      id: 'fd-i386', group: 'flatpak', name: 'org.freedesktop.Platform.Compat.i386', from: '25.08 · 5ac31d9', to: 'e0b7c12', size: '142 MB', bytes: 142,
      risk: 'red', badge: { cls: 'red', text: 'Blocked' }, blocked: true, noteInline: 'Steam is running: quit it first',
      verdict: '32-bit compatibility layer Steam needs for older games. Blocked while Steam runs.',
      notes: `org.freedesktop.Platform.Compat.i386 25.08\n* glibc 2.42.1 (i386), libdrm 2.4.128`,
      reasoning: 'Steam loads this for 32-bit titles and Proton prefixes. Rated <strong>conflict</strong> while Steam is running.',
    },
    {
      id: 'dbx', group: 'fwupd', name: 'Secure Boot dbx', from: '20260402', to: '20260707', size: '96 KB', bytes: 0.1,
      risk: 'yellow', badge: { cls: 'yellow', text: 'Firmware' }, reboot: true,
      verdict: 'UEFI revocation list from Microsoft. Applies on the next restart; your Fedora shim is not on the list.',
      notes: `UEFI dbx 2026-07-07 (LVFS)\n* Revokes 14 additional bootloaders affected by BlackLotus-class\n  vulnerabilities (CVE-2023-24932 follow-up)\n* Fedora shim 15.9-1 verified NOT revoked`,
      reasoning: 'Firmware writes are always <strong>caution</strong>, but dbx updates are low-risk when the current shim is confirmed outside the revocation list, which fwupd checked. Takes effect at reboot, so it rides along with the kernel restart.',
    },
  ];

  const RISK_RANK = { red: 0, yellow: 1, green: 2 };
  const RISK_LABEL = { red: 'Conflict / critical', yellow: 'Caution', green: 'Safe' };

  /* ======================================================================
   * Apply timeline (~10 s)
   * ==================================================================== */
  const STEPS = [
    { id: 'download', name: 'Download', dur: 2200, doneText: '1.3 GB · done',  activeText: p => `${fmtMB(1337 * p)} of 1.3 GB` },
    { id: 'install',  name: 'Install',  dur: 2400, doneText: '13 packages · done', activeText: p => `${Math.min(13, Math.floor(13 * p) + 1)} of 13 packages` },
    { id: 'nvidia',   name: 'Building NVIDIA module, please wait', warn: true, dur: 3200, doneText: 'nvidia 615.71.09 for 7.2.5 · built and signed', activeText: p => `${fmtClock(3200 * (1 - p) * 40)} left · do not restart yet` },
    { id: 'verify',   name: 'Verify',   dur: 1400, doneText: 'module loads, boot entry written, dbx staged', activeText: () => 'checking modprobe, initramfs, boot entries' },
    { id: 'restart',  name: 'Restart',  dur: 0,    doneText: '', activeText: () => 'ready' },
  ];
  const TOTAL = STEPS.reduce((a, s) => a + s.dur, 0);

  const LOG = [
    [0,    'Resolving transaction: 13 packages, 3 runtimes, 1 firmware'],
    [150,  'Downloading openssl-3.5.2-1.fc44.x86_64.rpm … done (7.1 MB)', 'ok'],
    [500,  'Downloading kernel-core-7.2.5-300.fc44.x86_64.rpm … done (74.2 MB)', 'ok'],
    [900,  'Downloading akmod-nvidia-615.71.09-1.fc44.x86_64.rpm … done (312 MB)', 'ok'],
    [1300, 'Downloading gstreamer1-* (5) … done (18 MB)', 'ok'],
    [1700, 'flatpak: fetching org.freedesktop.Platform//25.08 … done (610 MB)', 'ok'],
    [2000, 'fwupd: downloaded dbx-20260707.cab (96 KB)', 'ok'],
    [2250, 'Running transaction check … ok'],
    [2500, 'Installing : openssl-3.5.2-1.fc44.x86_64                 [1/13]'],
    [2900, 'Installing : gnome-software-50.4-1.fc44.x86_64            [2/13]'],
    [3200, 'Installing : gstreamer1-1.28.7-1.fc44.x86_64              [3/13]'],
    [3600, 'Installing : kernel-core-7.2.5-300.fc44.x86_64            [8/13]'],
    [4000, 'Installing : akmod-nvidia-615.71.09-1.fc44.x86_64         [9/13]'],
    [4300, 'Running scriptlet: dracut -f /boot/initramfs-7.2.5-300.fc44.img'],
    [4600, 'Running scriptlet: akmods --kernels 7.2.5-300.fc44.x86_64'],
    [4700, 'akmods: Building NVIDIA module 615.71.09 for kernel 7.2.5-300.fc44', 'warn'],
    [4750, 'akmods: this may take several minutes — do not power off', 'warn'],
    [5200, 'nvidia-kmod: CC [M] nvidia/nv-frontend.o'],
    [5700, 'nvidia-kmod: CC [M] nvidia/nv-instance.o'],
    [6200, 'nvidia-kmod: CC [M] nvidia-drm/nvidia-drm-drv.o'],
    [6700, 'nvidia-kmod: CC [M] nvidia-modeset/nvidia-modeset-linux.o'],
    [7200, 'nvidia-kmod: LD [M] nvidia.ko nvidia-drm.ko nvidia-modeset.ko nvidia-uvm.ko'],
    [7600, 'akmods: signing modules with /etc/pki/akmods/private/private_key.priv'],
    [7800, 'akmods: kmod-nvidia-7.2.5-300.fc44.x86_64-615.71.09 built', 'ok'],
    [8000, 'verify: modprobe --dry-run -S 7.2.5-300.fc44.x86_64 nvidia … ok', 'ok'],
    [8300, 'verify: initramfs-7.2.5-300.fc44.img contains nvidia-drm … ok', 'ok'],
    [8600, 'verify: bootctl list → 7.2.5-300.fc44 is default … ok', 'ok'],
    [8900, 'fwupd: dbx 20260707 staged, applies on next boot', 'ok'],
    [9100, 'flatpak: org.freedesktop.Platform 25.08 deployed (3 refs)', 'ok'],
    [9200, 'Transaction complete. Restart required for kernel 7.2.5.', 'ok'],
  ];

  /* ======================================================================
   * State
   * ==================================================================== */
  const S = {
    view: 'list',            // list | loading | empty | applying | done
    theme: 'dark',
    steamRunning: true,
    restartPending: false,
    selected: new Set(),
    expanded: new Set(),
    search: '',
    overlay: null,           // null | menu | settings | topbar | shortcuts
    logOpen: true,
    apply: null,             // { start, elapsed, stepIdx, logCount, timer }
    restarting: false,
    toast: null,
    lastChecked: '09:41',
    prefs: { interval: 'Every 30 minutes', redSecurity: true, redConflicts: true, redCopr: false, notify: 'security', pauseSteam: true, autoSafe: false },
  };
  const INTERVALS = ['Every 15 minutes', 'Every 30 minutes', 'Hourly', 'Daily', 'Manually'];

  const app = document.getElementById('app');
  const root = document.documentElement;
  const $ = (sel, el = app) => el.querySelector(sel);
  const $$ = (sel, el = app) => [...el.querySelectorAll(sel)];

  /* ======================================================================
   * Derived helpers
   * ==================================================================== */
  const itemsIn = g => ITEMS.filter(i => i.group === g).sort((a, b) => RISK_RANK[a.risk] - RISK_RANK[b.risk]);
  const isBlocked = i => !!i.blocked && S.steamRunning;
  const effRisk = i => (i.blocked && !S.steamRunning) ? 'green' : i.risk;
  const effBadge = i => (i.blocked && !S.steamRunning) ? null : i.badge;
  const selectable = () => ITEMS.filter(i => !isBlocked(i));
  const matches = i => {
    const q = S.search.trim().toLowerCase();
    if (!q) return true;
    return [i.name, i.pkg, i.verdict, i.from, i.to, ...(i.pkgs || [])].filter(Boolean).some(s => s.toLowerCase().includes(q));
  };
  const stats = () => {
    const pkgs = ITEMS.reduce((a, i) => a + (i.pkgCount || 1), 0);
    const reboot = ITEMS.filter(i => i.reboot).length;
    const security = ITEMS.filter(i => i.badge && i.badge.cls === 'solid-red').length;
    const blocked = ITEMS.filter(isBlocked).length;
    const selBytes = ITEMS.filter(i => S.selected.has(i.id)).reduce((a, i) => a + i.bytes, 0);
    return { pkgs, reboot, security, blocked, selBytes };
  };
  function fmtMB(mb) { return mb >= 1000 ? (mb / 1000).toFixed(1) + ' GB' : Math.round(mb) + ' MB'; }
  function fmtClock(ms) { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`; }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ======================================================================
   * Icons (inline SVG, stroke-based)
   * ==================================================================== */
  const I = {
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 7h14M5 12h14M5 17h14"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 13.5A8.5 8.5 0 0 1 10.5 3a7 7 0 1 0 10.5 10.5z"/></svg>',
    chev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.5 20h19L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    sparkle: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8z"/></svg>',
    notes: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M7 4h10a2 2 0 0 1 2 2v14l-4-2-3 2-3-2-4 2V6a2 2 0 0 1 2-2z"/><path d="M9 9h6M9 13h4"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2zM10 20a2 2 0 0 0 4 0"/></svg>',
    keyboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/></svg>',
    flask: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="m9 12 2 2 4-4"/></svg>',
  };

  /* ======================================================================
   * Rendering
   * ==================================================================== */
  function render() {
    root.dataset.theme = S.theme;
    let html = '';
    switch (S.view) {
      case 'list':     html = viewList(); break;
      case 'loading':  html = viewLoading(); break;
      case 'empty':    html = viewEmpty(); break;
      case 'applying': html = viewApplying(); break;
      case 'done':     html = viewDone(); break;
    }
    html += renderOverlay();
    if (S.toast) html += `<div class="toast">${esc(S.toast.text)}${S.toast.action ? `<button class="btn" data-action="${S.toast.action.act}">${esc(S.toast.action.label)}</button>` : ''}</div>`;
    if (S.restarting) html += `<div class="restart-scrim"><div><div class="sp"></div>Restarting…</div></div>`;
    app.innerHTML = html;
    afterRender();
  }

  function afterRender() {
    // indeterminate group checkboxes
    $$('.check[data-group]').forEach(cb => {
      const ids = itemsIn(cb.dataset.group).filter(i => !isBlocked(i)).map(i => i.id);
      const n = ids.filter(id => S.selected.has(id)).length;
      cb.checked = ids.length > 0 && n === ids.length;
      cb.indeterminate = n > 0 && n < ids.length;
      cb.disabled = ids.length === 0;
    });
    const all = $('.check[data-all]');
    if (all) {
      const ids = selectable().map(i => i.id);
      const n = ids.filter(id => S.selected.has(id)).length;
      all.checked = ids.length > 0 && n === ids.length;
      all.indeterminate = n > 0 && n < ids.length;
    }
    const log = $('.log');
    if (log) log.scrollTop = log.scrollHeight;
    if (S.view === 'applying') patchApply();
  }

  /* ---- header bar ---- */
  function headerbar({ left = '', title, sub = '', right = '', showSearch = false, showMenu = true, closeAct = 'noop' }) {
    return `
    <header class="headerbar">
      ${left}
      <div class="title"><div class="t">${title}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>
      <div class="spacer"></div>
      <div class="right">
        ${showSearch ? `<label class="search" title="Search updates ( / )">${I.search}<input id="search" type="search" placeholder="Search updates" value="${esc(S.search)}" data-input="search" autocomplete="off"><kbd>/</kbd></label>` : ''}
        ${right}
        <button class="btn icon" data-action="toggleTheme" title="${S.theme === 'dark' ? 'Switch to light' : 'Switch to dark'}" aria-label="Toggle theme">${S.theme === 'dark' ? I.sun : I.moon}</button>
        ${showMenu ? `<button class="btn icon" data-action="openMenu" title="Main menu" aria-label="Main menu" aria-haspopup="menu">${I.menu}</button>` : ''}
        <button class="btn icon sm" data-action="${closeAct}" title="Close" aria-label="Close">${I.close}</button>
      </div>
    </header>`;
  }

  /* ---- 1. updates list ---- */
  function viewList() {
    const st = stats();
    const nSel = S.selected.size;
    const left = `<button class="btn primary" data-action="updateNow" ${nSel ? '' : 'disabled'}>Update now <span class="count">${nSel}</span></button>
      <button class="btn" data-action="selectSafe" title="Select only green items">Safe only</button>`;
    const sub = `${st.pkgs} updates available · checked ${S.lastChecked}`;
    const head = headerbar({ left, title: 'Rivet', sub, showSearch: true, closeAct: 'closeWindow' });

    const chips = `
      <div class="summary">
        <label class="chip" title="Select every unblocked update"><input type="checkbox" class="check" data-all="1"> Select all</label>
        <span class="chip">${st.pkgs} updates</span>
        ${st.reboot ? `<span class="chip yellow"><span class="dot"></span>Reboot required · ${st.reboot}</span>` : ''}
        ${st.security ? `<span class="chip red"><span class="dot"></span>${st.security} critical security</span>` : ''}
        ${st.blocked ? `<span class="chip red">${st.blocked} blocked by Steam</span>` : ''}
        <span class="spacer"></span>
        <span class="chip ghost">${nSel ? fmtMB(st.selBytes) + ' selected' : 'nothing selected'}</span>
      </div>
      ${S.restartPending ? `<div class="banner yellow">${I.warn.replace('<svg', '<svg style="width:16px;height:16px;flex:none"')}<span>Kernel 7.2.5 is installed but not running yet. Restart when convenient.</span><span class="spacer"></span><button class="btn" data-action="restartNow">Restart now</button></div>` : ''}`;

    const groups = GROUPS.map(g => {
      const items = itemsIn(g.id).filter(matches);
      if (!items.length) return '';
      const steamBanner = g.id === 'flatpak' && S.steamRunning ? `
        <div class="group-banner red">
          <span class="risk red"></span>
          <span>Steam is running. Quit it before applying Flatpak runtime updates, or game launches break until Steam restarts.</span>
          <span class="spacer"></span>
          <button class="btn" data-action="quitSteam">Quit Steam</button>
        </div>` : '';
      return `
        <section class="group" data-group="${g.id}">
          <div class="group-head">
            <h2>${g.title} <span class="src">· ${g.src}</span> <span class="n">${items.reduce((a, i) => a + (i.pkgCount || 1), 0)}</span></h2>
            <label>Select all <input type="checkbox" class="check" data-group="${g.id}"></label>
          </div>
          <div class="card">
            ${steamBanner}
            ${items.map(rowHTML).join('')}
          </div>
        </section>`;
    }).join('');

    const body = groups || `<div class="no-match">No updates match “${esc(S.search)}”.</div>`;
    return `${head}<main class="content"><div class="content-inner">${chips}${body}</div></main>`;
  }

  function rowHTML(i) {
    const blocked = isBlocked(i);
    const risk = effRisk(i);
    const badge = effBadge(i);
    const open = S.expanded.has(i.id);
    const checked = S.selected.has(i.id);
    const note = i.noteInline && !(i.blocked && !S.steamRunning) ? ` · <span class="note ${risk === 'red' ? 'red' : ''}">${esc(i.noteInline)}</span>` : '';
    return `
      <div class="row ${open ? 'open' : ''} ${blocked ? 'blocked' : ''}" data-id="${i.id}">
        <div class="row-main" data-action="toggleRow" role="button" tabindex="0" aria-expanded="${open}">
          <input type="checkbox" class="check" data-select="${i.id}" ${checked ? 'checked' : ''} ${blocked ? 'disabled' : ''} aria-label="Select ${esc(i.name)}">
          <span class="risk ${risk}" title="${RISK_LABEL[risk]}"></span>
          <div class="row-text">
            <div class="row-name">${esc(i.name)}${i.pkgCount ? `<span class="pk">${i.pkgCount} packages</span>` : ''}${i.pkg ? `<span class="pk">${esc(i.pkg)}</span>` : ''}</div>
            <div class="row-ver">${esc(i.from)}<span class="arrow">→</span>${esc(i.to)}${note}</div>
            <div class="row-verdict"><span class="ai">${I.sparkle} verdict</span>${esc(i.verdict)}</div>
          </div>
          ${badge ? `<span class="badge ${badge.cls}">${esc(badge.text)}</span>` : ''}
          <span class="size">${esc(i.size)}</span>
          <span class="chev">${I.chev}</span>
        </div>
        ${i.alert ? `<div class="row-alert yellow">${I.warn}<span>${esc(i.alert)}</span></div>` : ''}
        <div class="row-detail"><div>
          <div class="detail-inner">
            <div>
              <h4>${I.notes} Release notes</h4>
              <pre class="notes">${esc(i.notes)}</pre>
            </div>
            <div>
              <h4>${I.sparkle} Why it's rated ${RISK_LABEL[risk].toLowerCase()}</h4>
              <p class="reason"><span class="verdict-pill"><span class="risk ${risk}"></span>${RISK_LABEL[risk]}</span><br>${i.reasoning}</p>
              ${i.pkgs ? `<ul class="pkgs">${i.pkgs.map(p => `<li>${esc(p)}</li>`).join('')}</ul>` : ''}
            </div>
            <div class="detail-actions">
              <button class="btn" data-action="toast" data-msg="Prototype: would open the full changelog for ${esc(i.name)}">Full changelog</button>
              ${blocked ? `<button class="btn" data-action="quitSteam">Quit Steam to unblock</button>` : ''}
              <button class="btn flat" data-action="toast" data-msg="Prototype: ${esc(i.name)} would be held back until you un-skip it">Skip this version</button>
            </div>
          </div>
        </div></div>
      </div>`;
  }

  /* ---- 6. loading ---- */
  function viewLoading() {
    const head = headerbar({ title: 'Rivet', sub: 'Checking…', closeAct: 'closeWindow' });
    const widths = [[140, 220], [190, 150], [120, 250], [170, 130], [150, 200], [110, 240]];
    return `${head}
      <div class="progress-strip"><div class="pulse"></div></div>
      <main class="content" style="padding-top:28px"><div class="content-inner">
        <div class="load-title">Reviewing release notes…</div>
        <div class="load-sub">reading 13 changelogs · rating each one</div>
        <div class="card">${widths.map(([a, b]) => `
          <div class="skel-row"><span class="skel box static"></span>
            <div style="flex:1"><div class="skel" style="height:12px;width:${a}px"></div><div class="skel" style="height:9px;width:${b}px;margin-top:7px"></div></div>
            <div class="skel static" style="height:9px;width:44px"></div></div>`).join('')}
        </div>
      </div></main>`;
  }

  /* ---- 5. empty ---- */
  function viewEmpty() {
    const head = headerbar({ title: 'Rivet', closeAct: 'closeWindow' });
    return `${head}
      <div class="center"><div class="center-box">
        <div class="big-ok pop">${I.check}</div>
        <h1>You’re up to date</h1>
        <p>Last checked today at ${S.lastChecked}${S.restartPending ? ' · kernel 7.2.5 is waiting for a restart' : ''}</p>
        <div class="actions">
          <button class="btn" data-action="checkNow">Check now</button>
          ${S.restartPending ? `<button class="btn primary" data-action="restartNow">Restart now</button>` : ''}
        </div>
      </div></div>`;
  }

  /* ---- 2a. applying ---- */
  function viewApplying() {
    const a = S.apply;
    const left = `<button class="btn" data-action="cancelApply" ${a.stepIdx > 0 ? 'disabled title="Cannot cancel once install has started"' : ''}>Cancel</button>`;
    const head = headerbar({ left, title: 'Updating…', sub: `<span data-bind="sub">Step ${a.stepIdx + 1} of ${STEPS.length}</span>`, showMenu: false, closeAct: 'toast' });
    const steps = STEPS.map((s, k) => {
      const cls = k < a.stepIdx ? 'done' : k === a.stepIdx ? 'active' : 'pending';
      return `<div class="step ${cls} ${s.warn ? 'warn' : ''}" data-step="${k}">
        <span class="ind">${cls === 'done' ? I.check : ''}</span>
        <div class="st"><div class="n">${s.name}${s.id === 'nvidia' && cls === 'active' ? '…' : ''}</div><div class="d" data-bind="d${k}">${cls === 'done' ? s.doneText : ''}</div></div>
        ${cls === 'active' && s.dur ? `<span class="pct" data-bind="pct">0%</span>` : ''}
      </div>`;
    }).join('');
    return `${head}
      <div class="progress-strip"><div class="fill" data-bind="fill"></div></div>
      <main class="content" style="padding-top:32px"><div class="content-inner steps">
        <div class="card">${steps}</div>
        <div class="apply-note">${I.shield}<span>Rivet holds the restart until the NVIDIA module is built and verified for kernel 7.2.5.</span></div>
      </div></main>
      <div class="drawer ${S.logOpen ? '' : 'closed'}">
        <div class="drawer-head" data-action="toggleLog"><span class="chev">${I.chev}</span><span class="lbl">Details</span><span class="src">dnf · akmods · flatpak · fwupd</span><span class="spacer"></span><kbd>Esc</kbd></div>
        <div class="log" data-bind="log">${LOG.slice(0, a.logCount).map(logLine).join('')}</div>
      </div>`;
  }
  const logLine = ([, text, cls]) => `<div class="${cls || ''}">${esc(text)}</div>`;

  /* ---- 2b. done ---- */
  function viewDone() {
    const head = headerbar({ title: 'Rivet', showMenu: false, closeAct: 'later' });
    return `${head}
      <div class="center"><div class="center-box">
        <div class="big-ok pop">${I.check}</div>
        <h1>All done</h1>
        <p>Restart required for kernel 7.2.5. The NVIDIA module was rebuilt and verified — it is safe to restart. Secure Boot dbx applies on the way up.</p>
        <div class="meta"><span class="chip green">13 updated</span><span class="chip red"><span class="dot"></span>CVE-2026-4417 fixed</span><span class="chip yellow"><span class="dot"></span>Reboot required</span></div>
        <div class="actions">
          <button class="btn primary" data-action="restartNow">Restart now</button>
          <button class="btn" data-action="later">Later</button>
        </div>
      </div></div>`;
  }

  /* ---- overlays ---- */
  function renderOverlay() {
    switch (S.overlay) {
      case 'menu': return menuHTML();
      case 'settings': return settingsHTML();
      case 'topbar': return topbarHTML();
      case 'shortcuts': return shortcutsHTML();
      default: return '';
    }
  }

  function menuHTML() {
    return `<div class="scrim" style="background:transparent" data-action="closeOverlay"></div>
      <div class="popover" role="menu">
        <button class="mi" data-action="checkNow">${I.refresh} Check for updates</button>
        <button class="mi" data-action="openSettings">${I.gear} Preferences <kbd>Ctrl ,</kbd></button>
        <button class="mi" data-action="openShortcuts">${I.keyboard} Keyboard shortcuts <kbd>?</kbd></button>
        <div class="sep"></div>
        <div class="lbl">Prototype · simulate</div>
        <button class="mi" data-action="simList">${I.flask} Updates available</button>
        <button class="mi" data-action="simEmpty">${I.flask} Up to date</button>
        <button class="mi" data-action="simLoading">${I.flask} Checking</button>
        <button class="mi" data-action="simApplying">${I.flask} Applying</button>
        <button class="mi" data-action="openTopbar">${I.bell} Top-bar badge &amp; notification</button>
        <div class="sep"></div>
        <button class="mi" data-action="toast" data-msg="Rivet 0.1 · prototype B · nothing here touches your system">${I.info} About Rivet</button>
      </div>`;
  }

  function settingsHTML() {
    const p = S.prefs;
    const cb = (key) => `<input type="checkbox" class="check" data-pref="${key}" ${p[key] ? 'checked' : ''}>`;
    const radio = (val, label) => `<label class="pref-row clickable"><div class="t">${label}</div><span class="spacer"></span><input type="radio" class="radio" name="notify" data-pref="notify" value="${val}" ${p.notify === val ? 'checked' : ''}></label>`;
    return `<div class="scrim" data-action="closeOverlay">
      <div class="dialog" role="dialog" aria-label="Preferences" data-stop>
        <header class="headerbar"><div class="title"><div class="t">Preferences</div></div><div class="spacer"></div><button class="btn icon sm" data-action="closeOverlay" aria-label="Close">${I.close}</button></header>
        <div class="body">
          <div class="pref-h">Appearance</div>
          <div class="card"><div class="pref-row"><div class="t">Theme</div><span class="spacer"></span>
            <span class="segment"><button class="${S.theme === 'light' ? 'on' : ''}" data-action="setTheme" data-theme="light">Light</button><button class="${S.theme === 'dark' ? 'on' : ''}" data-action="setTheme" data-theme="dark">Dark</button></span></div></div>

          <div class="pref-h">Checking for updates</div>
          <div class="card">
            <div class="pref-row"><div class="t">Check interval</div><span class="spacer"></span><button class="btn dropdown" data-action="cycleInterval">${esc(p.interval)} ${I.chev}</button></div>
            <label class="pref-row clickable"><div><div class="t">Apply green updates automatically</div><div class="d">Only items Rivet rates safe, never anything that needs a restart</div></div><span class="spacer"></span><input type="checkbox" class="switch" data-pref="autoSafe" ${p.autoSafe ? 'checked' : ''}></label>
          </div>

          <div class="pref-h">What counts as red</div>
          <div class="pref-d">Red items are excluded from “Safe only” and always flagged in the top bar.</div>
          <div class="card">
            <label class="pref-row clickable"><div class="t">Security advisories</div><span class="spacer"></span>${cb('redSecurity')}</label>
            <label class="pref-row clickable"><div class="t">Conflicts with running apps</div><span class="spacer"></span>${cb('redConflicts')}</label>
            <label class="pref-row clickable"><div class="t">Third-party COPR version bumps</div><span class="spacer"></span>${cb('redCopr')}</label>
          </div>

          <div class="pref-h">Notifications</div>
          <div class="card">${radio('security', 'Security fixes only')}${radio('any', 'Any update')}${radio('never', 'Never')}</div>

          <div class="pref-h">Apps</div>
          <div class="card">
            <label class="pref-row clickable"><div><div class="t">Pause Flatpak updates while Steam runs</div><div class="d">Prevents runtime swaps under a running game</div></div><span class="spacer"></span><input type="checkbox" class="switch" data-pref="pauseSteam" ${p.pauseSteam ? 'checked' : ''}></label>
          </div>
        </div>
      </div></div>`;
  }

  function topbarHTML() {
    const st = stats();
    return `<div class="scrim" data-action="closeOverlay">
      <div class="shell" data-stop role="dialog" aria-label="GNOME top bar preview">
        <div class="topbar">
          <div class="pill act"><span></span><span></span></div>
          <div class="spacer"></div>
          <div class="pill" style="background:transparent">Sep 15&nbsp;&nbsp;09:41</div>
          <div class="spacer"></div>
          <div class="tray">
            <span class="up" title="Rivet: ${st.pkgs} updates">${I.refresh}<span class="n">${st.pkgs}</span><span class="alert"></span></span>
            <span class="bat"></span>
            <span class="car">▾</span>
          </div>
        </div>
        <div class="notif">
          <div class="top">
            <div class="app">${I.refresh}</div>
            <div style="min-width:0"><div class="t">Rivet: ${st.pkgs} updates</div><div class="d">${st.reboot} need a restart (kernel, NVIDIA, dbx), 1 critical security fix, 3 wait for Steam</div></div>
          </div>
          <div class="acts"><button data-action="notifReview">Review</button><div class="vsep"></div><button data-action="notifSafe">Update safe ones</button></div>
        </div>
        <div class="caption">GNOME top bar: Rivet indicator with count and red security dot · notification card</div>
        <button class="btn icon close" data-action="closeOverlay" aria-label="Close" style="background:rgba(0,0,0,.4);color:#fff">${I.close}</button>
      </div></div>`;
  }

  function shortcutsHTML() {
    return `<div class="scrim" data-action="closeOverlay">
      <div class="dialog" style="width:420px" data-stop role="dialog" aria-label="Keyboard shortcuts">
        <header class="headerbar"><div class="title"><div class="t">Keyboard shortcuts</div></div><div class="spacer"></div><button class="btn icon sm" data-action="closeOverlay" aria-label="Close">${I.close}</button></header>
        <div class="body"><div class="keys">
          <kbd>/</kbd><span>Focus search</span>
          <kbd>Esc</kbd><span>Close menu, dialog or log drawer · clear search</span>
          <kbd>Ctrl ,</kbd><span>Preferences</span>
          <kbd>Ctrl A</kbd><span>Select all updates</span>
          <kbd>Ctrl Enter</kbd><span>Update now</span>
          <kbd>?</kbd><span>This list</span>
        </div></div>
      </div></div>`;
  }

  /* ======================================================================
   * Apply engine (timer-driven; DOM patched in place to keep animations smooth)
   * ==================================================================== */
  function startApply(fromStep = 0) {
    stopApply();
    const skipped = STEPS.slice(0, fromStep).reduce((a, s) => a + s.dur, 0);
    S.apply = { start: performance.now() - skipped, elapsed: skipped, stepIdx: fromStep, logCount: LOG.filter(l => l[0] <= skipped).length, timer: null };
    S.view = 'applying';
    S.overlay = null;
    render();
    S.apply.timer = setInterval(tickApply, 100);
  }
  function stopApply() { if (S.apply && S.apply.timer) clearInterval(S.apply.timer); }

  function tickApply() {
    const a = S.apply;
    a.elapsed = performance.now() - a.start;
    // which step
    let acc = 0, idx = STEPS.length - 1;
    for (let k = 0; k < STEPS.length; k++) { if (a.elapsed < acc + STEPS[k].dur) { idx = k; break; } acc += STEPS[k].dur; }
    const newLog = LOG.filter(l => l[0] <= a.elapsed).length;
    const stepChanged = idx !== a.stepIdx;
    a.stepIdx = idx;
    if (a.elapsed >= TOTAL) {
      stopApply();
      a.logCount = LOG.length;
      setTimeout(() => { S.view = 'done'; S.restartPending = true; render(); }, 700);
      patchApply();
      return;
    }
    if (stepChanged) { a.logCount = newLog; render(); return; }
    if (newLog !== a.logCount) {
      const log = $('[data-bind="log"]');
      if (log) { log.insertAdjacentHTML('beforeend', LOG.slice(a.logCount, newLog).map(logLine).join('')); log.scrollTop = log.scrollHeight; }
      a.logCount = newLog;
    }
    patchApply();
  }

  function patchApply() {
    const a = S.apply; if (!a) return;
    const fill = $('[data-bind="fill"]');
    if (fill) fill.style.width = Math.min(100, a.elapsed / TOTAL * 100).toFixed(1) + '%';
    let acc = 0; for (let k = 0; k < a.stepIdx; k++) acc += STEPS[k].dur;
    const s = STEPS[a.stepIdx];
    const p = s.dur ? Math.min(1, (a.elapsed - acc) / s.dur) : 1;
    const d = $(`[data-bind="d${a.stepIdx}"]`);
    if (d) d.textContent = s.activeText(p);
    const pct = $('[data-bind="pct"]');
    if (pct) pct.textContent = Math.round(p * 100) + '%';
    const sub = $('[data-bind="sub"]');
    if (sub) sub.textContent = `Step ${a.stepIdx + 1} of ${STEPS.length} · about ${fmtClock((TOTAL - a.elapsed) * 40)} left`;
  }

  /* ======================================================================
   * Actions
   * ==================================================================== */
  let toastTimer = null;
  function toast(text, action) {
    S.toast = { text, action };
    render();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { S.toast = null; render(); }, 3200);
  }

  const ACTIONS = {
    noop() {},
    toggleTheme() { S.theme = S.theme === 'dark' ? 'light' : 'dark'; persistTheme(); },
    setTheme(el) { S.theme = el.dataset.theme; persistTheme(); },
    openMenu() { S.overlay = S.overlay === 'menu' ? null : 'menu'; },
    openSettings() { S.overlay = 'settings'; },
    openShortcuts() { S.overlay = 'shortcuts'; },
    openTopbar() { S.overlay = 'topbar'; },
    closeOverlay() { S.overlay = null; },
    closeWindow() { S.overlay = null; toast('Prototype: the window would close and Rivet keeps checking in the background'); },
    toast(el) { S.overlay = null; toast(el && el.dataset.msg ? el.dataset.msg : 'Prototype action'); },
    toggleRow(el) { const id = el.closest('.row').dataset.id; S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id); },
    selectSafe() {
      S.selected = new Set(ITEMS.filter(i => effRisk(i) === 'green').map(i => i.id));
      toast(`${S.selected.size} safe updates selected`);
    },
    quitSteam() {
      S.steamRunning = false;
      S.overlay = null;
      ITEMS.filter(i => i.blocked).forEach(i => S.selected.add(i.id));
      toast('Steam closed · Flatpak runtimes unblocked and selected', { label: 'Undo', act: 'relaunchSteam' });
    },
    relaunchSteam() { S.steamRunning = true; ITEMS.filter(i => i.blocked).forEach(i => S.selected.delete(i.id)); S.toast = null; },
    updateNow() { if (S.selected.size) startApply(0); },
    simApplying() { startApply(2); },
    cancelApply() { stopApply(); S.apply = null; S.view = 'list'; toast('Update cancelled, nothing was changed'); },
    toggleLog() { S.logOpen = !S.logOpen; },
    later() { stopApply(); S.apply = null; S.view = 'empty'; S.restartPending = true; },
    restartNow() {
      S.restarting = true; S.overlay = null; render();
      setTimeout(() => { S.restarting = false; S.restartPending = false; S.view = 'empty'; S.lastChecked = '09:47'; toast('Prototype: rebooted into kernel 7.2.5 (pretend)'); }, 1600);
    },
    checkNow() { S.overlay = null; startLoading(() => { S.view = S.restartPending ? 'empty' : 'list'; render(); }); },
    simList() { S.overlay = null; S.restartPending = false; S.view = 'list'; },
    simEmpty() { S.overlay = null; S.restartPending = false; S.view = 'empty'; },
    simLoading() { S.overlay = null; startLoading(() => { S.view = 'list'; render(); }); },
    notifReview() { S.overlay = null; S.view = 'list'; },
    notifSafe() { S.overlay = null; S.view = 'list'; ACTIONS.selectSafe(); },
    cycleInterval() { S.prefs.interval = INTERVALS[(INTERVALS.indexOf(S.prefs.interval) + 1) % INTERVALS.length]; },
  };

  let loadTimer = null;
  function startLoading(then) {
    clearTimeout(loadTimer);
    S.view = 'loading';
    render();
    loadTimer = setTimeout(then, 2600);
  }

  function dispatch(name, el) {
    const fn = ACTIONS[name];
    if (!fn) return;
    fn(el);
    render();
  }

  /* ======================================================================
   * Events (delegated)
   * ==================================================================== */
  app.addEventListener('click', e => {
    const stop = e.target.closest('[data-stop]');
    const t = e.target.closest('[data-action]');
    if (!t) return;
    if (t.classList.contains('scrim') && stop) return;          // click inside dialog, not scrim
    if (e.target.closest('input, label')) return;                // checkboxes handled by change
    e.preventDefault();
    dispatch(t.dataset.action, t);
  });

  app.addEventListener('change', e => {
    const el = e.target;
    if (el.dataset.select) { el.checked ? S.selected.add(el.dataset.select) : S.selected.delete(el.dataset.select); render(); }
    else if (el.dataset.group) {
      const ids = itemsIn(el.dataset.group).filter(i => !isBlocked(i)).map(i => i.id);
      ids.forEach(id => el.checked ? S.selected.add(id) : S.selected.delete(id));
      render();
    }
    else if (el.dataset.all) { selectable().forEach(i => el.checked ? S.selected.add(i.id) : S.selected.delete(i.id)); render(); }
    else if (el.dataset.pref) {
      const k = el.dataset.pref;
      S.prefs[k] = el.type === 'radio' ? el.value : el.checked;
      if (k === 'pauseSteam' && !el.checked) toast('Heads-up: Flatpak runtimes would now update under a running Steam');
      render();
    }
  });

  app.addEventListener('input', e => {
    if (e.target.dataset.input === 'search') {
      S.search = e.target.value;
      const inner = $('.content-inner');
      // re-render list body only, keep the input focused
      render();
      const inp = $('#search'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
      void inner;
    }
  });

  app.addEventListener('keydown', e => {
    // row expand with keyboard
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.row-main')) { e.preventDefault(); dispatch('toggleRow', e.target); }
  });

  document.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (typing && document.activeElement.id === 'search') {
        if (S.search) { S.search = ''; render(); $('#search') && $('#search').focus(); } else document.activeElement.blur();
        return;
      }
      if (S.overlay) { S.overlay = null; render(); return; }
      if (S.view === 'applying' && S.logOpen) { S.logOpen = false; render(); return; }
      if (S.toast) { S.toast = null; render(); }
      return;
    }
    if (typing) return;
    if (e.key === '/') { e.preventDefault(); const inp = $('#search'); if (inp) inp.focus(); }
    else if (e.key === '?') { S.overlay = 'shortcuts'; render(); }
    else if (e.key === ',' && e.ctrlKey) { e.preventDefault(); S.overlay = 'settings'; render(); }
    else if (e.key === 'a' && e.ctrlKey && S.view === 'list') { e.preventDefault(); selectable().forEach(i => S.selected.add(i.id)); render(); }
    else if (e.key === 'Enter' && e.ctrlKey && S.view === 'list') { e.preventDefault(); dispatch('updateNow'); }
  });

  /* ======================================================================
   * Boot
   * ==================================================================== */
  function persistTheme() { try { localStorage.setItem('rivet.theme', S.theme); } catch (_) {} }

  function boot() {
    const q = new URLSearchParams(location.search);
    let theme = null;
    try { theme = localStorage.getItem('rivet.theme'); } catch (_) {}
    if (q.get('theme')) theme = q.get('theme');
    if (!theme) theme = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    S.theme = theme === 'light' ? 'light' : 'dark';

    // default selection: everything that isn't blocked
    selectable().forEach(i => S.selected.add(i.id));
    if (q.get('steam') === 'quit') ACTIONS.quitSteam();
    if (q.get('expand')) q.get('expand').split(',').forEach(id => S.expanded.add(id));
    if (q.get('search')) S.search = q.get('search');

    const state = q.get('state') || 'list';
    switch (state) {
      case 'applying': { const step = Math.max(0, Math.min(STEPS.length - 1, parseInt(q.get('step') ?? '2', 10))); startApply(step); if (q.get('pause') === '1') stopApply(); return; }
      case 'done':     S.view = 'done'; S.restartPending = true; break;
      case 'empty':    S.view = 'empty'; break;
      case 'loading':  startLoading(() => { S.view = 'list'; render(); }); return;
      case 'settings': S.overlay = 'settings'; break;
      case 'topbar':   S.overlay = 'topbar'; break;
      case 'menu':     S.overlay = 'menu'; break;
      case 'shortcuts': S.overlay = 'shortcuts'; break;
      default:         S.view = 'list';
    }
    S.toast = null;
    render();
  }

  boot();
  // expose for poking around in devtools
  window.rivet = { S, ITEMS, STEPS, render, dispatch };
})();
