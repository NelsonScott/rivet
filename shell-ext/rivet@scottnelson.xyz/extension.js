import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Rivet top-bar indicator.
//
// Data source: ~/.local/state/rivet/state.json (schema in CONTRACT.md in the source repo),
// written atomically by `rivet-collect`. We watch it with a Gio.FileMonitor and never poll.
// QA/demo: RIVET_STATE=/path/to/fixture.json points the indicator at a fixture instead.

const STATE_DIR = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'state', 'rivet']);
const DEFAULT_STATE = GLib.build_filenamev([STATE_DIR, 'state.json']);
const APP_DESKTOP = 'xyz.scottnelson.Rivet.desktop';
// ~/.local/bin (install.sh) wins; the rivet-updater RPM puts the CLIs in /usr/bin.
// Resolved at spawn time, so installing either one later needs no shell restart.
function cliPath(name) {
    const local = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', name]);
    if (GLib.file_test(local, GLib.FileTest.EXISTS))
        return local;
    return GLib.find_program_in_path(name) ?? local;
}
const DEBOUNCE_MS = 150; // state.json is tmp+rename'd; coalesce the event burst

function statePath() {
    return GLib.getenv('RIVET_STATE') || DEFAULT_STATE;
}

function plural(n, one, many) {
    return `${n} ${n === 1 ? one : many}`;
}

// "checked 09:41" from an ISO-8601 timestamp; '' when unusable.
function fmtChecked(iso) {
    if (!iso) return '';
    const t = GLib.DateTime.new_from_iso8601(iso, null);
    if (!t) return '';
    return `checked ${t.to_local().format('%H:%M')}`;
}

function allItems(state) {
    const out = [];
    for (const g of state?.groups ?? []) {
        if (g?.id === 'held') continue;   // pinned refs: shown in the app, never counted here
        if (Array.isArray(g?.items)) out.push(...g.items);
    }
    return out;
}

// state.json -> everything the view needs. Never throws.
function digest(state) {
    const items = allItems(state);
    const flags = state?.flags ?? {};
    const total = Number.isFinite(state?.summary?.total) ? state.summary.total : items.length;
    const reboot = items.filter(i => i?.requires_reboot).length ||
        (flags.reboot_required ? 1 : 0);
    const security = Number.isFinite(flags.security_count)
        ? flags.security_count
        : items.filter(i => i?.advisory?.type === 'security').length;
    const blocked = Number.isFinite(flags.blocked_count)
        ? flags.blocked_count
        : items.filter(i => i?.blocked_by).length;
    const red = items.some(i => i?.risk === 'red');

    const parts = [plural(total, 'update', 'updates')];
    if (reboot > 0) parts.push(`${reboot} need${reboot === 1 ? 's' : ''} reboot`);
    if (security > 0) parts.push(`${security} security`);
    if (blocked > 0) parts.push(`${blocked} blocked by Steam`);

    return {
        total,
        red,
        summary: parts.join(' · '),
        checked: fmtChecked(state?.checked_at ?? state?.generated_at),
    };
}

class Indicator extends PanelMenu.Button {
    static {
        GObject.registerClass(this);
    }

    _init(extPath) {
        super._init(0.5, 'Rivet');

        // ---- panel: ⭳ 13 (+ red dot) ----
        const box = new St.BoxLayout({style_class: 'rivet-box'});
        this._icon = new St.Icon({
            gicon: new Gio.FileIcon({
                file: Gio.File.new_for_path(GLib.build_filenamev([extPath, 'rivet-symbolic.svg'])),
            }),
            style_class: 'system-status-icon rivet-icon',
        });
        this._count = new St.Label({text: '', y_align: Clutter.ActorAlign.CENTER, style_class: 'rivet-count'});
        this._dot = new St.Widget({
            style_class: 'rivet-dot',
            y_align: Clutter.ActorAlign.START,
            visible: false,
        });
        box.add_child(this._icon);
        box.add_child(this._count);
        box.add_child(this._dot);
        this.add_child(box);

        // ---- popup: summary + checked + two actions ----
        this.menu.actor.add_style_class_name('rivet-menu');

        const head = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false, style_class: 'rivet-head'});
        const headBox = new St.BoxLayout({vertical: true, x_expand: true, style_class: 'rivet-headbox'});
        this._summary = new St.Label({text: '…', style_class: 'rivet-summary', x_expand: true});
        this._summary.clutter_text.line_wrap = true;
        this._checked = new St.Label({text: '', style_class: 'rivet-checked', x_expand: true});
        headBox.add_child(this._summary);
        headBox.add_child(this._checked);
        head.add_child(headBox);
        this.menu.addMenuItem(head);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._openItem = new PopupMenu.PopupMenuItem('Open Rivet');
        this._openItem.connect('activate', () => this._openApp());
        this.menu.addMenuItem(this._openItem);

        this._checkItem = new PopupMenu.PopupMenuItem('Check now');
        this._checkItem.connect('activate', () => this._checkNow());
        this.menu.addMenuItem(this._checkItem);

        this._state = null;
        this._debounce = 0;
        this._monitors = [];
        this._attachMonitors();
        this._reload();
    }

    // --- watching -------------------------------------------------------

    // One monitor on state.json. GIO's local monitor watches the parent directory
    // entry, so tmp+rename is seen; the directory has to exist for that, hence the
    // make_directory_with_parents (harmless: it's the backend's own state dir).
    _attachMonitors() {
        const file = Gio.File.new_for_path(statePath());
        try {
            file.get_parent()?.make_directory_with_parents(null);
        } catch (e) {
            // already exists (or unwritable) — nothing to do
        }
        try {
            const mon = file.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, null);
            mon.connect('changed', () => this._queueReload());
            this._monitors.push(mon);
        } catch (e) {
            console.warn(`[rivet] cannot watch ${statePath()}: ${e.message}`);
        }
    }

    _queueReload() {
        if (this._debounce) GLib.source_remove(this._debounce);
        this._debounce = GLib.timeout_add(GLib.PRIORITY_DEFAULT, DEBOUNCE_MS, () => {
            this._debounce = 0;
            this._reload();
            return GLib.SOURCE_REMOVE; // one-shot; not a poll
        });
    }

    // --- reading --------------------------------------------------------

    _reload() {
        const path = statePath();
        let bytes = null;
        try {
            const [ok, data] = GLib.file_get_contents(path);
            if (ok) bytes = data;
        } catch (e) {
            // not there yet (rivet-collect has never run) — show nothing at all
            this._render({kind: 'missing'});
            return;
        }
        if (!bytes) {
            this._render({kind: 'missing'});
            return;
        }
        let state;
        try {
            state = JSON.parse(new TextDecoder().decode(bytes));
            if (!state || typeof state !== 'object') throw new Error('not an object');
        } catch (e) {
            console.warn(`[rivet] malformed ${path}: ${e.message}`);
            this._render({kind: 'bad'});
            return;
        }
        this._state = state;
        const status = state.status;
        if (status === 'checking') {
            this._render({kind: 'checking', d: digest(state)});
        } else if (status === 'error') {
            this._render({kind: 'error', d: digest(state), msg: state.error || 'check failed'});
        } else {
            this._render({kind: 'ok', d: digest(state)});
        }
    }

    // --- drawing --------------------------------------------------------

    _render(view) {
        const {kind, d} = view;

        if (kind === 'missing') {
            this._count.text = '';
            this._count.visible = false;
            this._dot.visible = false;
            this._summary.text = 'No update data yet';
            this._checked.text = 'run “Check now”';
            this._setDim(true);
            this._setShown(false);
            console.log('[rivet] panel: hidden (no state.json)');
            return;
        }

        if (kind === 'bad') {
            this._setShown(true);
            this._count.visible = true;
            this._count.text = '!';
            this._dot.visible = false;
            this._summary.text = 'Update state unreadable';
            this._checked.text = 'state.json is malformed';
            this._setDim(true);
            console.log('[rivet] panel: ! (malformed state)');
            return;
        }

        if (kind === 'checking') {
            this._setShown(true);
            this._count.visible = true;
            this._count.text = '…';
            this._dot.visible = false;
            this._summary.text = 'Checking for updates…';
            this._checked.text = d.checked;
            this._setDim(true);
            console.log('[rivet] panel: … (checking)');
            return;
        }

        if (kind === 'error') {
            this._setShown(true);
            this._count.visible = true;
            this._count.text = d.total > 0 ? String(d.total) : '!';
            this._dot.visible = false;
            this._summary.text = `Check failed: ${view.msg}`;
            this._checked.text = d.checked;
            this._setDim(true);
            console.log(`[rivet] panel: error (${view.msg})`);
            return;
        }

        // kind === 'ok'
        this._setDim(false);
        if (d.total === 0) {
            this._count.visible = false;
            this._count.text = '';
            this._dot.visible = false;
            this._summary.text = 'Up to date';
            this._checked.text = d.checked;
            this._setShown(false); // nothing pending: stay out of the bar
            console.log('[rivet] panel: hidden (0 pending)');
            return;
        }
        this._setShown(true);
        this._count.visible = true;
        this._count.text = String(d.total);
        this._count.style_class = d.red ? 'rivet-count rivet-count-red' : 'rivet-count';
        this._dot.visible = d.red;
        this._summary.text = d.summary;
        this._checked.text = d.checked;
        console.log(`[rivet] panel: ${d.total}${d.red ? ' (red)' : ''} — ${d.summary}`);
    }

    // Hide the whole panel button, container included, so no empty gap is left.
    _setShown(shown) {
        this.visible = shown;
        if (this.container) this.container.visible = shown;
    }

    _setDim(dim) {
        // The panel icon is a FileIcon with baked-in stroke colour, so CSS `color`
        // cannot dim it — use actor opacity instead.
        this._icon.opacity = dim ? 130 : 255;
        if (dim) {
            this._icon.add_style_class_name('rivet-dim');
            this._count.style_class = 'rivet-count rivet-dim';
        } else {
            this._icon.remove_style_class_name('rivet-dim');
            this._count.style_class = 'rivet-count';
        }
    }

    // --- actions --------------------------------------------------------

    _openApp() {
        this.menu.close(true);
        try {
            const app = Gio.DesktopAppInfo.new(APP_DESKTOP);
            if (app) {
                app.launch([], null);
                return;
            }
        } catch (e) {
            console.warn(`[rivet] ${APP_DESKTOP} launch failed: ${e.message}`);
        }
        this._spawn([cliPath('rivet-updater')]);
    }

    _checkNow() {
        this.menu.close(true);
        this._spawn([cliPath('rivet-collect'), '--quiet']);
    }

    _spawn(argv) {
        try {
            const proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
            proc.wait_async(null, (p, res) => {
                try {
                    p.wait_finish(res);
                } catch (e) {
                    console.warn(`[rivet] ${argv[0]} failed: ${e.message}`);
                }
            });
        } catch (e) {
            console.warn(`[rivet] cannot spawn ${argv[0]}: ${e.message}`);
            Main.notify('Rivet', `Could not run ${argv[0]}`);
        }
    }

    destroy() {
        if (this._debounce) {
            GLib.source_remove(this._debounce);
            this._debounce = 0;
        }
        for (const mon of this._monitors) mon.cancel();
        this._monitors = [];
        super.destroy();
    }
}

const DBUS_IFACE = `<node><interface name="xyz.scottnelson.Rivet">
  <method name="OpenMenu"/><method name="CloseMenu"/><method name="Reload"/>
  <method name="Snap"><arg type="s" name="path" direction="in"/></method>
</interface></node>`;

export default class RivetExtension extends Extension {
    OpenMenu() { this._indicator?.menu.open(true); }
    CloseMenu() { this._indicator?.menu.close(true); }
    Reload() { this._indicator?._reload(); }

    // QA helper (same trick as claude-usage@scottnelson): screenshot from inside the shell.
    Snap(path) {
        try {
            const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
            const shooter = new Shell.Screenshot();
            shooter.screenshot(false, stream, (o, res) => {
                try { shooter.screenshot_finish(res); console.log(`[rivet] snap -> ${path}`); }
                catch (e) { console.warn(`[rivet] snap failed: ${e.message}`); }
                finally { stream.close(null); }
            });
        } catch (e) {
            console.warn(`[rivet] snap failed: ${e.message}`);
        }
    }

    enable() {
        console.log('[rivet] enable');
        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, this);
        this._dbus.export(Gio.DBus.session, '/xyz/scottnelson/Rivet');
        this._indicator = new Indicator(this.path);
        // position 1 in the right box: immediately right of claude-usage (which takes 0)
        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'right');
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
