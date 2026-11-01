# Rivet: Fedora updates in one place, with the risky ones flagged.
#
# An application, not a library: the backend is a flat directory of stdlib
# scripts that import each other as siblings (no Python package, no
# pyproject), and the app finds the backend at ../backend. So both live in a
# private tree, %%{_datadir}/rivet/{backend,app}, the same layout as the git
# checkout install.sh symlinks from, and the CLIs are symlinks into it.
# The bundled Manrope and Inter fonts (app/ui/fonts, OFL-1.1) are the page's faces; it loads no web fonts.

%global app_id xyz.scottnelson.Rivet
%global ext_uuid rivet@scottnelson.xyz
%global pkgdir %{_datadir}/rivet
# keep -s, drop -P: the CLIs add their own directory to sys.path explicitly
%global py3_shebang_flags s

Name:           rivet-updater
Version:        0.1.0
Release:        1%{?dist}
Summary:        Fedora updates in one place, with the risky ones flagged

License:        MIT AND OFL-1.1
URL:            https://github.com/NelsonScott/rivet
# make-srpm.sh builds this tarball from git HEAD. Once the repo is public and tagged, switch to:
# Source0:        %%{url}/archive/v%%{version}/rivet-%%{version}.tar.gz
# (and %%autosetup -n rivet-%%{version}, the prefix GitHub uses)
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
# %%check: the backend tests import gi (Gio over D-Bus) and run the real CLIs
BuildRequires:  python3-gobject
BuildRequires:  python3-rpm
BuildRequires:  bash

Requires:       python3-gobject
# rivet_apply.py imports rpm to compare versions the way rpm does
Requires:       python3-rpm
# typelibs the code loads: Gtk 4.0, Adw 1, WebKit 6.0, Flatpak 1.0
# (rpmlint's explicit-lib-dependency is expected: no typelib() provides exist)
Requires:       gtk4
Requires:       libadwaita
Requires:       webkitgtk6.0
Requires:       flatpak-libs
Requires:       flatpak
# org.rpm.dnf.v0 on the system bus (installs) and the dnf5 CLI (checks)
Requires:       dnf5daemon-server
Requires:       dnf5
Requires:       polkit
Requires:       hicolor-icon-theme
Requires:       systemd
Recommends:     gnome-shell
Recommends:     fwupd
Recommends:     mokutil
Recommends:     libnotify

%description
Rivet replaces GNOME Software for updates. It shows every pending dnf, Flatpak
and firmware update in one list, says in one line what each one does, and
colours it green, yellow or red so you know which ones to read before you
click. Kernel plus NVIDIA akmod updates wait for the module to build before a
reboot is offered, Flatpak runtime updates are held while Steam is running,
and stale npm, pipx, uv and cargo tools, GNOME extensions and containers are
listed too. An hourly systemd user timer does the checking; the GNOME Shell
extension rivet@scottnelson.xyz shows a top-bar badge (enable it with
gnome-extensions enable rivet@scottnelson.xyz). Optionally, and only when
turned on in Preferences, Claude Code (the user's own login) writes the
one-line reasons; its Fix it button runs only when pressed.

%prep
%autosetup -n %{name}-%{version}

%build
# nothing to build: Python scripts and a plain HTML/CSS/JS page

%install
# backend: every module, no tests or fixtures
install -d %{buildroot}%{pkgdir}/backend
cp -p backend/*.py %{buildroot}%{pkgdir}/backend/
# helpers with a shebang that are also run directly (python3 FILE) stay runnable either way
chmod 0755 %{buildroot}%{pkgdir}/backend/flatpak_dryrun.py

# app: the GTK/WebKit shell, its launcher and the page (no QA drivers)
install -d %{buildroot}%{pkgdir}/app
install -pm0755 app/rivet_app.py app/rivet-updater %{buildroot}%{pkgdir}/app/
cp -pr app/ui %{buildroot}%{pkgdir}/app/
rm -rf %{buildroot}%{pkgdir}/app/ui/qa

%py3_shebang_fix %{buildroot}%{pkgdir}/backend %{buildroot}%{pkgdir}/app/rivet_app.py
%py_byte_compile %{python3} %{buildroot}%{pkgdir}

install -d %{buildroot}%{_bindir}
ln -s ../share/rivet/app/rivet-updater %{buildroot}%{_bindir}/rivet-updater
ln -s ../share/rivet/backend/rivet_collect.py %{buildroot}%{_bindir}/rivet-collect
ln -s ../share/rivet/backend/rivet_apply.py %{buildroot}%{_bindir}/rivet-apply
ln -s ../share/rivet/backend/rivet_doctor.py %{buildroot}%{_bindir}/rivet-doctor

# systemd user units: same files install.sh copies, with the CLI in /usr/bin
install -Dpm0644 systemd/rivet-collect.timer %{buildroot}%{_userunitdir}/rivet-collect.timer
sed 's|^ExecStart=%%h/.local/bin/rivet-collect |ExecStart=%{_bindir}/rivet-collect |' \
    systemd/rivet-collect.service > rivet-collect.service
grep -q '^ExecStart=%{_bindir}/rivet-collect ' rivet-collect.service
install -Dpm0644 rivet-collect.service %{buildroot}%{_userunitdir}/rivet-collect.service
# the hourly check is the point of the package: enable the timer for every user.
# The preset is fine only because the check itself is local: Claude Code triage is
# opt-in (settings ai_triage, default off), so an enabled timer sends nothing anywhere
# beyond the package metadata fetches dnf, Flatpak and fwupd already make.
install -d %{buildroot}%{_userpresetdir}
echo 'enable rivet-collect.timer' > %{buildroot}%{_userpresetdir}/80-rivet-updater.preset

# desktop entry, metainfo, icons (the pre-rendered PNG set app/install.sh copies too)
desktop-file-install --dir=%{buildroot}%{_datadir}/applications app/%{app_id}.desktop
install -Dpm0644 data/%{app_id}.metainfo.xml %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
for s in 16 24 32 48 64 128 256 512; do
    install -Dpm0644 app/icons/hicolor/${s}x${s}/apps/%{app_id}.png \
        %{buildroot}%{_datadir}/icons/hicolor/${s}x${s}/apps/%{app_id}.png
done

# GNOME Shell extension (top-bar badge); each user enables it
install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}
install -pm0644 shell-ext/%{ext_uuid}/{extension.js,metadata.json,stylesheet.css,rivet-symbolic.svg} \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
# Backend tests: no display, no network (fakes for dnf, systemctl, HA and claude).
# The git-tracked-files scan skips itself outside a git checkout.
cd backend && HOME=$(mktemp -d) %{python3} -m unittest discover -s tests

%post
%systemd_user_post rivet-collect.timer

%preun
%systemd_user_preun rivet-collect.timer

%files
%license LICENSE app/ui/fonts/OFL-Inter.txt app/ui/fonts/OFL-Manrope.txt
%doc README.md CONTRACT.md
%{_bindir}/rivet-updater
%{_bindir}/rivet-collect
%{_bindir}/rivet-apply
%{_bindir}/rivet-doctor
%{pkgdir}/
%{_userunitdir}/rivet-collect.service
%{_userunitdir}/rivet-collect.timer
%{_userpresetdir}/80-rivet-updater.preset
%{_datadir}/applications/%{app_id}.desktop
%{_metainfodir}/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/%{app_id}.png
%{_datadir}/gnome-shell/extensions/%{ext_uuid}/

%changelog
* Sun Nov 01 2026 Scott Nelson <8241108+NelsonScott@users.noreply.github.com> - 0.1.0-1
- First package
