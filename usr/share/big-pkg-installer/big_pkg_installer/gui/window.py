"""Main window. Pages follow the installer mockup:

welcome → details → (preparing) → dependencies → authentication →
installing → success | failure, plus invalid / unreadable / incompatible.
"""

import platform
import shutil
import subprocess
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import installer, pacman
from ..converter import Callbacks, Conversion, ConversionError, compatible, convert, inspect
from ..debfile import Cancelled, DebError
from ..elf import host_arch
from ..i18n import _, ngettext
from ..settings import Settings
from .dialogs import APP_NAME, LogDialog
from .widgets import (
    CHECK_ICON,
    CROSS_ICON,
    StepList,
    action_bar,
    badge,
    banner,
    button,
    code_box,
    deb_icon,
    file_card,
    file_icon,
    file_status_icon,
    icon_or,
    label,
    page_header,
    property_row,
    scrolled_page,
    status_icon,
)

PKEXEC_DISMISSED = (126, 127)
DEBIAN_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "armv7h": "armhf", "i686": "i386", "riscv64": "riscv64"}
CONVERT_STEPS = ("extract", "layout", "deps", "scripts", "build")


def _convert_labels() -> list[str]:
    return [
        _("Extracting files"),
        _("Adapting folders to this system"),
        _("Checking dependencies"),
        _("Preparing installation scripts"),
        _("Building the package"),
    ]


def _install_labels(downloads: int) -> list[str]:
    return [
        _("Preparing the installation"),
        _("Downloading packages ({done} of {total})").format(done=0, total=downloads) if downloads
        else _("Downloading packages"),
        _("Installing packages"),
        _("Configuring the system"),
        _("Finishing"),
    ]


def _relevant_error_lines(lines: list[str]) -> str:
    keys = ("error", "erro", "exists in filesystem", "conflict", "could not", "failed", "unable", "not found")
    picked = [line for line in lines if any(k in line.lower() for k in keys)]
    return "\n".join(picked or lines[-20:])


class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(600, 680)
        self.set_size_request(360, 480)
        self.conversion: Conversion | None = None
        self.callbacks: Callbacks | None = None
        self.preview: installer.TransactionPreview | None = None
        self.install_proc: subprocess.Popen | None = None
        self.path: Path | None = None
        self.busy = False
        self.installing = False
        self.log_lines: list[str] = []
        self.drop_zone: Gtk.Widget | None = None

        self.title = Adw.WindowTitle(title=APP_NAME)
        header = Adw.HeaderBar(title_widget=self.title)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("About the Installer"), "app.about")
        section.append(_("Help"), "app.help")
        section.append(_("Preferences"), "app.preferences")
        menu.append_section(None, section)
        links = Gio.Menu()
        links.append(_("Send feedback"), "app.feedback")
        links.append(_("Report a problem"), "app.report")
        menu.append_section(None, links)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_("Main menu")))

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        toolbar = Adw.ToolbarView(content=self.toasts)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", self._on_drag_enter)
        drop.connect("leave", self._on_drag_leave)
        self.add_controller(drop)
        self.connect("close-request", self._on_close_request)
        self._show("welcome", self._page_welcome())

    # ------------------------------------------------------------ helpers
    def _show(self, name: str, widget: Gtk.Widget) -> None:
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(widget, name)
        self.stack.set_visible_child_name(name)

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def _copy(self, text: str) -> None:
        self.get_display().get_clipboard().set(text)
        self._toast(_("Copied"))

    def _reset(self) -> None:
        if self.conversion:
            self.conversion.cleanup()
        self.conversion = None
        self.preview = None
        self.callbacks = None
        self.log_lines = []

    # -------------------------------------------------------------- pages
    def _page_welcome(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        icons = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER)
        icons.append(Gtk.Image(icon_name=deb_icon(), pixel_size=112))
        icons.append(Gtk.Image(icon_name=icon_or("application-x-rpm", "package-x-generic"), pixel_size=112))
        box.append(icons)
        box.append(label(_("Install a .deb or .rpm package"), "title-1"))
        box.append(label(_("Select a .deb or .rpm file on your computer to install it on your Linux system in a simple and safe way.")))
        choose = button(_("Choose a package file…"), "folder-open-symbolic", suggested=True)
        choose.add_css_class("pill")
        choose.set_halign(Gtk.Align.CENTER)
        choose.set_margin_top(8)
        choose.connect("clicked", self._on_choose_file)
        box.append(choose)
        zone = Gtk.Box(spacing=10, halign=Gtk.Align.FILL, margin_top=10)
        zone.add_css_class("drop-zone")
        inner = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, hexpand=True)
        inner.append(Gtk.Image(icon_name="folder-download-symbolic"))
        inner.append(label(_("Or drag and drop the file here"), "dim-label"))
        zone.append(inner)
        self.drop_zone = zone
        box.append(zone)
        return scrolled_page(box, valign_center=True)

    def _page_details(self, conv: Conversion) -> Gtk.Widget:
        deb = conv.deb
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.append(page_header(_("Selected package"), _("Review the package information before continuing."),
                               on_back=self._go_welcome))

        native = deb.name.lower()
        if native in pacman.sync_packages():
            hint = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=_("“{name}” is available in the repositories").format(name=native),
                                subtitle=_("Installing the native package is recommended."), use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="emblem-default-symbolic"))
            if shutil.which("pamac-manager"):
                open_button = Gtk.Button(label=_("Open"), valign=Gtk.Align.CENTER)
                open_button.connect("clicked", lambda *_a: subprocess.Popen(["pamac-manager", f"--details={native}"]))
                row.add_suffix(open_button)
            hint.add(row)
            box.append(hint)

        card = Adw.PreferencesGroup()
        head = Gtk.Box(spacing=14, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        head.append(Gtk.Image(gicon=file_icon(deb.path), pixel_size=56))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        names.append(label(deb.name, "title-3", xalign=0.0))
        names.append(badge(deb.version))
        head.append(names)
        head_row = Adw.ActionRow(activatable=False)
        head_row.set_child(head)
        card.add(head_row)
        card.add(property_row(_("Package name"), deb.name))
        card.add(property_row(_("Version"), deb.version))
        card.add(property_row(_("Architecture"), deb.architecture))
        card.add(property_row(_("Size"), GLib.format_size(deb.installed_size) if deb.installed_size else ""))
        card.add(property_row(_("Maintainer"), deb.maintainer))
        homepage = deb.homepage
        if homepage:
            card.add(property_row(_("Website"), homepage, suffix_icon="adw-external-link-symbolic",
                                  on_activate=lambda: Gtk.UriLauncher.new(homepage).launch(self, None, None, None)))
        description = "\n\n".join(t for t in (deb.summary, deb.long_description) if t)
        if description:
            expander = Adw.ExpanderRow(title=_("Description"), expanded=True)
            expander.add_prefix(Gtk.Image(icon_name="text-x-generic-symbolic"))
            desc_label = label(description, "dim-label", xalign=0.0, selectable=True,
                               margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
            desc_row = Adw.ActionRow(activatable=False)
            desc_row.set_child(desc_label)
            expander.add_row(desc_row)
            card.add(expander)
        box.append(card)

        cancel = button(_("Cancel"))
        cancel.connect("clicked", lambda *_a: self._go_welcome())
        proceed = button(_("Continue"), "go-next-symbolic", suggested=True, icon_end=True)
        proceed.connect("clicked", lambda *_a: self._start_conversion())
        return scrolled_page(box, action_bar(cancel, proceed))

    def _page_progress(self, title: str, labels: list[str], cancellable: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        if self.path is not None:
            box.append(Gtk.Image(gicon=file_icon(self.path), pixel_size=88))
        else:
            box.append(Gtk.Image(icon_name=deb_icon(), pixel_size=88))
        box.append(label(title, "title-2"))
        box.append(label(_("This may take a few moments…"), "dim-label"))
        self.progress_bar = Gtk.ProgressBar(margin_top=8)
        box.append(self.progress_bar)
        self.progress_status = label("", "dim-label", xalign=0.0)
        box.append(self.progress_status)
        self.steps = StepList(labels)
        self.steps.set_margin_top(6)
        box.append(self.steps)
        log_scroller = code_box("", min_height=140)
        self.progress_log = log_scroller.get_child().get_buffer() if isinstance(log_scroller, Gtk.ScrolledWindow) else None
        for line in self.log_lines:
            self.progress_log.insert(self.progress_log.get_end_iter(), line + "\n")
        box.append(Gtk.Expander(label=_("Details"), child=log_scroller, margin_top=6))
        self.cancel_button = button(_("Cancel"), CROSS_ICON)
        self.cancel_button.connect("clicked", self._on_cancel)
        bar = Gtk.Box(halign=Gtk.Align.END)
        bar.add_css_class("action-bar")
        bar.append(self.cancel_button)
        self.cancel_button.set_visible(cancellable)
        return scrolled_page(box, bar)

    def _page_dependencies(self, conv: Conversion, preview: installer.TransactionPreview) -> Gtk.Widget:
        spec = conv.spec
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.append(page_header(_("Dependencies"),
                               _("The following packages will be installed so the application works correctly."),
                               icon="folder-download-symbolic"))
        blocking = conv.conflicts and conv.conflicts.owned
        if blocking:
            listed = ", ".join(f"{p} ({o})" for p, o in list(conv.conflicts.owned.items())[:4])
            box.append(banner("error", _("Files already owned by other packages: {list}").format(list=listed)))

        group = Adw.PreferencesGroup()
        head = Gtk.Box(spacing=10, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        head.append(label(_("New packages to install"), "heading", xalign=0.0, hexpand=True))
        count = len(preview.packages)
        head.append(label(ngettext("{n} package", "{n} packages", count).format(n=count), "dim-label"))
        head.append(badge(GLib.format_size(preview.total_size)))
        head_row = Adw.ActionRow(activatable=False)
        head_row.set_child(head)
        group.add(head_row)
        for pkg in preview.packages:
            row = Adw.ActionRow(title=pkg.name, use_markup=False, activatable=False)
            icon = Gtk.Image(icon_name="folder-download-symbolic")
            icon.add_css_class("download-icon")
            row.add_prefix(icon)
            row.add_suffix(label(pkg.version, "dim-label", wrap=False))
            if pkg.local:
                row.set_subtitle(_("converted from the package file"))
            group.add(row)
        box.append(group)

        other = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title=_("Other information"))
        problems = len(conv.warnings)
        if problems:
            expander.set_subtitle(ngettext("{n} warning", "{n} warnings", problems).format(n=problems))
        visible_apps = [a.name for a in conv.apps if not a.hidden]
        expander.add_row(self._info_row(_("Package name on this system"), f"{spec.pkgname} {conv.full_version}"))
        expander.add_row(self._info_row(_("Applications menu"),
                                        ", ".join(visible_apps) if visible_apps else _("No menu entry")))
        if spec.optdepends:
            expander.add_row(self._info_row(_("Optional"), ", ".join(spec.optdepends)))
        for text in conv.warnings:
            expander.add_row(self._info_row(text, "", "dialog-warning-symbolic"))
        for text in conv.notes:
            expander.add_row(self._info_row(text, "", "dialog-information-symbolic"))
        if conv.install_functions:
            scripts = Adw.ActionRow(title=_("Scripts"), use_markup=False,
                                    subtitle=_("These commands from the package run as administrator during installation."))
            show = Gtk.Button(label=_("Show"), valign=Gtk.Align.CENTER)
            text = conv.install_functions
            show.connect("clicked", lambda *_a: LogDialog(text, lambda: self._copy(text)).present(self))
            scripts.add_suffix(show)
            expander.add_row(scripts)
        other.add(expander)
        box.append(other)

        back = button(_("Back"), "go-previous-symbolic")
        back.connect("clicked", lambda *_a: self._back_to_details())
        install = button(_("Install"), "folder-download-symbolic", suggested=True)
        install.set_sensitive(not blocking)
        install.connect("clicked", lambda *_a: self._start_install())
        return scrolled_page(box, action_bar(back, install))

    def _info_row(self, title: str, value: str, icon: str | None = None) -> Adw.ActionRow:
        row = Adw.ActionRow(use_markup=False, title_lines=0, subtitle_lines=0, activatable=False)
        row.set_title(title)
        if value:
            row.set_subtitle(value)
        if icon:
            image = Gtk.Image(icon_name=icon)
            image.add_css_class("warning" if "warning" in icon else "accent")
            row.add_prefix(image)
        return row

    def _page_auth(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(status_icon("accent", icon_or("system-lock-screen-symbolic", "changes-prevent-symbolic")))
        box.append(label(_("Authentication required"), "title-2"))
        box.append(label(_("Type your password in the system authentication window to continue the installation.")))
        box.append(label(_("Administrator permissions are needed to install packages on your system."),
                         "dim-label", "caption", margin_top=10))
        cancel = button(_("Cancel"), CROSS_ICON)
        cancel.connect("clicked", self._on_cancel_auth)
        bar = Gtk.Box(halign=Gtk.Align.CENTER)
        bar.add_css_class("action-bar")
        bar.append(cancel)
        return scrolled_page(box, bar, valign_center=True)

    def _page_success(self) -> Gtk.Widget:
        conv = self.conversion
        apps = [a for a in conv.apps if not a.hidden]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(status_icon("success", CHECK_ICON))
        box.append(label(_("Package installed successfully!"), "title-2"))
        box.append(label(_("The package “{name}” was installed correctly on your system.").format(name=conv.deb.name)))
        if apps:
            names = ", ".join(f"“{a.name}”" for a in apps)
            box.append(label(_("Look for {names} in the applications menu.").format(names=names), "dim-label"))
        card = Adw.PreferencesGroup(margin_top=6)
        row = Adw.ActionRow(title=apps[0].name if apps else conv.deb.name, use_markup=False,
                            subtitle=_("Version {version}").format(version=conv.deb.version))
        if apps and icon_or(apps[0].icon, ""):
            row.add_prefix(Gtk.Image(icon_name=apps[0].icon, pixel_size=48))
        else:
            row.add_prefix(Gtk.Image(gicon=file_icon(conv.package.path), pixel_size=48))
        card.add(row)
        box.append(card)
        if apps:
            launch = button(_("Open application"), "media-playback-start-symbolic", suggested=True)
            launch.add_css_class("pill")
            launch.set_halign(Gtk.Align.CENTER)
            launch.connect("clicked", self._on_launch, apps[0].file_id)
            box.append(launch)
        details = button(_("Show details"), "document-properties-symbolic")
        log_text = "\n".join(self.log_lines)
        details.connect("clicked", lambda *_a: LogDialog(log_text, lambda: self._copy(log_text)).present(self))
        close = button(_("Close"), CROSS_ICON)
        close.connect("clicked", lambda *_a: self.close())
        return scrolled_page(box, action_bar(details, close), valign_center=True)

    def _page_failure(self, title: str, description: str, details: str = "") -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(status_icon("error", CROSS_ICON))
        box.append(label(title, "title-2"))
        box.append(label(description))
        full = "\n".join(self.log_lines) or details
        if details:
            expander = Gtk.Expander(label=_("Error details"), expanded=True, margin_top=6)
            expander.set_child(code_box(details, on_copy=lambda: self._copy(full)))
            box.append(expander)
        copy = button(_("Copy details"), "edit-copy-symbolic")
        copy.connect("clicked", lambda *_a: self._copy(full))
        copy.set_sensitive(bool(full))
        close = button(_("Close"), CROSS_ICON, suggested=True)
        close.connect("clicked", lambda *_a: self.close())
        return scrolled_page(box, action_bar(copy, close), valign_center=True)

    def _page_incompatible(self, conv: Conversion) -> Gtk.Widget:
        deb = conv.deb
        host = DEBIAN_ARCH.get(host_arch(), platform.machine()) if deb.kind == "deb" else host_arch()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(status_icon("warning", "dialog-warning-symbolic"))
        box.append(label(_("Incompatible package"), "title-2"))
        box.append(label(_("This package was built for the {deb_arch} architecture, but your system is {host}.").format(
            deb_arch=deb.architecture, host=host)))
        card = Adw.PreferencesGroup(margin_top=6)
        card.add(property_row(_("Package"), deb.name))
        card.add(property_row(_("Version"), deb.version))
        card.add(property_row(_("Package architecture"), deb.architecture))
        card.add(property_row(_("System architecture"), host))
        box.append(card)
        box.append(banner("warning", _("This package cannot be installed on this computer. Look for a version of the "
                                       "package made for {host}.").format(host=host),
                          icon_or("lightbulb-symbolic", "dialog-information-symbolic")))
        close = button(_("Close"))
        close.connect("clicked", lambda *_a: self.close())
        bar = Gtk.Box(halign=Gtk.Align.CENTER)
        bar.add_css_class("action-bar")
        bar.append(close)
        return scrolled_page(box, bar, valign_center=True)

    def _page_bad_file(self, path: Path, title: str, description: str, hint: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(file_status_icon(path))
        box.append(label(title, "title-2"))
        box.append(label(description))
        card = file_card(path)
        card.set_margin_top(6)
        box.append(card)
        box.append(banner("info", hint))
        close = button(_("Close"))
        close.connect("clicked", lambda *_a: self.close())
        bar = Gtk.Box(halign=Gtk.Align.CENTER)
        bar.add_css_class("action-bar")
        bar.append(close)
        return scrolled_page(box, bar, valign_center=True)

    # ------------------------------------------------------------ loading
    def load(self, path: str) -> None:
        self._reset()
        self.path = Path(path)
        self.title.set_subtitle(self.path.name)
        try:
            with open(self.path, "rb") as f:
                magic = f.read(8)
        except OSError as exc:
            self._show("error", self._page_bad_file(
                self.path, _("This file cannot be installed"), exc.strerror or str(exc),
                _("The file may be damaged or is not a valid package. Download it again or check where it came from.")))
            return
        known = magic == b"!<arch>\n" or magic[:4] == b"\xed\xab\xee\xdb"
        if not known and self.path.suffix.lower() not in (".deb", ".rpm"):
            self._show("error", self._page_bad_file(
                self.path, _("Invalid file"), _("The selected file is not a .deb or .rpm package."),
                _("This installer works only with .deb and .rpm files (packages for Debian, Ubuntu, Fedora, "
                  "openSUSE and their derivatives).")))
            return
        try:
            self.conversion = inspect(path)
        except (DebError, OSError) as exc:
            self._show("error", self._page_bad_file(
                self.path, _("This file cannot be installed"), str(exc),
                _("The file may be damaged or is not a valid package. Download it again or check where it came from.")))
            return
        if not compatible(self.conversion):
            self._show("details", self._page_incompatible(self.conversion))
            return
        self._show("details", self._page_details(self.conversion))

    def _go_welcome(self) -> None:
        self._reset()
        self.path = None
        self.title.set_subtitle("")
        self._show("welcome", self._page_welcome())

    def _back_to_details(self) -> None:
        if self.path:
            self.load(str(self.path))

    def _on_choose_file(self, *_args):
        dialog = Gtk.FileDialog(title=_("Choose a package file"))
        deb_filter = Gtk.FileFilter(name=_("Packages (.deb, .rpm)"))
        for mime in ("application/vnd.debian.binary-package", "application/x-deb", "application/x-rpm",
                     "application/x-redhat-package-manager"):
            deb_filter.add_mime_type(mime)
        deb_filter.add_suffix("deb")
        deb_filter.add_suffix("rpm")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(deb_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self.load(gfile.get_path())

    def _on_drag_enter(self, *_args):
        if self.drop_zone is not None:
            self.drop_zone.add_css_class("drag-hover")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, *_args):
        if self.drop_zone is not None:
            self.drop_zone.remove_css_class("drag-hover")

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self._on_drag_leave()
        if self.busy:
            return False
        files = value.get_files()
        if files and files[0].get_path():
            self.load(files[0].get_path())
            return True
        return False

    # --------------------------------------------------------- conversion
    def _start_conversion(self) -> None:
        self.busy = True
        self.log_lines = []
        self._show("progress", self._page_progress(_("Preparing the package"), _convert_labels(), cancellable=True))
        self.callbacks = Callbacks(
            step=lambda s: GLib.idle_add(self._on_convert_step, s),
            progress=lambda f: GLib.idle_add(self._on_convert_fraction, f),
            log=lambda line: GLib.idle_add(self._append_log, line),
        )
        self.current_step = "extract"
        self._on_convert_step("extract")
        threading.Thread(target=self._convert_thread, daemon=True).start()

    def _convert_thread(self) -> None:
        try:
            convert(self.conversion, self.callbacks)
            preview = installer.preview_transaction(self.conversion.pkgfile)
        except Cancelled:
            GLib.idle_add(self._on_cancelled)
            return
        except (ConversionError, DebError, OSError) as exc:
            for line in (getattr(exc, "log", "") or "").splitlines():
                GLib.idle_add(self._append_log, line)
            GLib.idle_add(self._on_convert_failed, str(exc))
            return
        GLib.idle_add(self._on_converted, preview)

    def _on_convert_step(self, step: str) -> None:
        self.current_step = step
        index = CONVERT_STEPS.index(step)
        self.steps.set_current(index)
        self.progress_bar.set_fraction(index / len(CONVERT_STEPS))
        self.progress_status.set_label(_convert_labels()[index] + "…")

    def _on_convert_fraction(self, fraction: float) -> None:
        if self.current_step != "extract":
            return
        span = 1 / len(CONVERT_STEPS)
        self.progress_bar.set_fraction(span * min(max(fraction, 0.0), 1.0))
        self.progress_status.set_label(_("Extracting files ({percent}%)").format(percent=int(fraction * 100)))

    def _append_log(self, line: str) -> None:
        self.log_lines.append(line)
        buffer = getattr(self, "progress_log", None)
        if buffer is not None:
            buffer.insert(buffer.get_end_iter(), line + "\n")

    def _on_cancel(self, *_args) -> None:
        if self.callbacks and not self.installing:
            self.callbacks.cancel.set()
            self.cancel_button.set_sensitive(False)

    def _on_cancelled(self) -> None:
        self.busy = False
        self._toast(_("Cancelled"))
        self._back_to_details()

    def _on_convert_failed(self, message: str) -> None:
        self.busy = False
        self._show("result", self._page_failure(
            _("The package could not be converted"), message, _relevant_error_lines(self.log_lines) or message))

    def _on_converted(self, preview: installer.TransactionPreview) -> None:
        self.busy = False
        self.preview = preview
        self.steps.set_current(len(CONVERT_STEPS))
        self.progress_bar.set_fraction(1.0)
        self._save_copy()
        if preview.error:
            self._show("result", self._page_failure(
                _("Dependencies are not available"),
                _("Some packages this program needs were not found in the repositories."),
                preview.error))
            return
        self._show("deps", self._page_dependencies(self.conversion, preview))

    def _save_copy(self) -> None:
        settings = Settings.load()
        if not settings.save_copy or not self.conversion.pkgfile:
            return
        try:
            settings.save_path.mkdir(parents=True, exist_ok=True)
            target = settings.save_path / self.conversion.pkgfile.name
            shutil.copy2(self.conversion.pkgfile, target)
        except OSError as exc:
            self._toast(str(exc))
            return
        self._toast(_("Package saved to {path}").format(path=target))

    # ------------------------------------------------------------ install
    def _start_install(self) -> None:
        self.busy = True
        self.installing = False
        self.install_proc = None
        downloads = sum(1 for p in self.preview.packages if not p.local) if self.preview else 0
        self.progress = installer.InstallProgress(downloads)
        self._show("auth", self._page_auth())
        threading.Thread(target=self._install_thread, daemon=True).start()

    def _install_thread(self) -> None:
        conv = self.conversion
        started = {"output": False}

        def log(line: str) -> None:
            if not started["output"] and not line.startswith("$ "):
                started["output"] = True
                GLib.idle_add(self._on_install_started)
            GLib.idle_add(self._on_install_line, line)

        def on_start(proc):
            self.install_proc = proc

        overwrite = conv.conflicts.unowned if conv.conflicts else []
        status = installer.run_install(conv.pkgfile, overwrite, log=log, interactive_tty=False, on_start=on_start)
        ok = status == 0 and installer.is_installed(conv.spec.pkgname, conv.full_version)
        GLib.idle_add(self._on_installed, ok, status, started["output"])

    def _on_install_started(self) -> None:
        self.installing = True
        downloads = self.progress.downloads
        self._show("progress", self._page_progress(_("Installing the package"), _install_labels(downloads),
                                                   cancellable=False))
        self._update_install_progress()

    def _on_install_line(self, line: str) -> None:
        self._append_log(line)
        if self.installing and self.progress.feed(line):
            self._update_install_progress()

    def _update_install_progress(self) -> None:
        progress = self.progress
        index = installer.INSTALL_STEPS.index(progress.step)
        if progress.downloads == 0 and index == 1:
            index = 2
        self.steps.set_current(index)
        if progress.downloads:
            self.steps.set_label(1, _("Downloading packages ({done} of {total})").format(
                done=progress.downloaded, total=progress.downloads))
        self.progress_bar.set_fraction(progress.fraction)
        status = {
            "prepare": _("Preparing the installation…"),
            "download": _("Downloading and installing dependencies…"),
            "install": _("Installing packages…"),
            "configure": _("Configuring the system…"),
            "finish": _("Finishing…"),
        }[progress.step]
        self.progress_status.set_label(status)

    def _on_cancel_auth(self, *_args) -> None:
        proc = self.install_proc
        if proc and proc.poll() is None and not self.installing:
            try:
                proc.terminate()
            except PermissionError:
                self._toast(_("Please wait until the installation finishes"))

    def _on_installed(self, ok: bool, status: int, had_output: bool) -> None:
        self.busy = False
        self.installing = False
        self.install_proc = None
        if ok:
            self.progress.step = "finish"
            self._show("result", self._page_success())
            self.conversion.cleanup()
            return
        if status in PKEXEC_DISMISSED or (not had_output and status != 0):
            self._toast(_("Authorization was not granted"))
            self._show("deps", self._page_dependencies(self.conversion, self.preview))
            return
        self._show("result", self._page_failure(
            _("The installation failed"),
            _("An error happened while installing the package. See the details below."),
            _relevant_error_lines(self.log_lines)))
        self.conversion.cleanup()

    # ---------------------------------------------------------- misc
    def _on_launch(self, _button, file_id: str) -> None:
        info = Gio.DesktopAppInfo.new(file_id)
        if info is None:
            self._toast(_("The application could not be started"))
            return
        try:
            info.launch([], self.get_display().get_app_launch_context())
        except GLib.Error as exc:
            self._toast(exc.message)
            return
        self.close()

    def _on_close_request(self, *_args) -> bool:
        if self.installing:
            self._toast(_("Please wait until the installation finishes"))
            return True
        if self.callbacks:
            self.callbacks.cancel.set()
        if self.install_proc and self.install_proc.poll() is None:
            try:
                self.install_proc.terminate()
            except PermissionError:
                pass
        if self.conversion and not self.busy:
            self.conversion.cleanup()
        return False
