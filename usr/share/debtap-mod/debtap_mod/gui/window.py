"""Main window: welcome → details → progress → (review) → result."""

import shutil
import subprocess
import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .. import installer, pacman
from ..converter import Callbacks, Conversion, ConversionError, compatible, convert, inspect
from ..debfile import Cancelled, DebError
from ..i18n import _, ngettext

STEP_TITLES = {
    "extract": _("Extracting files"),
    "layout": _("Adapting folders to this system"),
    "deps": _("Finding dependencies"),
    "scripts": _("Preparing installation scripts"),
    "build": _("Building the package"),
    "auth": _("Waiting for authorization"),
    "install": _("Installing"),
}
STEP_WEIGHTS = {"extract": (0.0, 0.55), "layout": (0.55, 0.57), "deps": (0.57, 0.65), "scripts": (0.65, 0.66), "build": (0.66, 1.0)}

PKEXEC_DISMISSED = (126, 127)


def _icon_or(name: str, fallback: str) -> str:
    display = Gdk.Display.get_default()
    if name and display and Gtk.IconTheme.get_for_display(display).has_icon(name):
        return name
    return fallback


def _label(text: str = "", css: tuple[str, ...] = (), wrap: bool = True, xalign: float = 0.5) -> Gtk.Label:
    label = Gtk.Label(label=text, wrap=wrap, xalign=xalign, justify=Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for cls in css:
        label.add_css_class(cls)
    return label


def _clamped(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=600, child=child, margin_top=18, margin_bottom=24, margin_start=12, margin_end=12)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


def _text_view(monospace: bool = True) -> tuple[Gtk.ScrolledWindow, Gtk.TextBuffer]:
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=monospace, wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_top_margin(8)
    view.set_bottom_margin(8)
    view.set_left_margin(8)
    view.set_right_margin(8)
    scrolled = Gtk.ScrolledWindow(child=view, min_content_height=180, vexpand=True)
    scrolled.add_css_class("card")
    return scrolled, view.get_buffer()


class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(620, 700)
        self.set_size_request(360, 400)
        self.conversion: Conversion | None = None
        self.callbacks: Callbacks | None = None
        self.busy = False
        self.installing = False
        self.log_lines: list[str] = []

        self.title = Adw.WindowTitle(title=_("Install .deb package"))
        header = Adw.HeaderBar(title_widget=self.title)
        menu = Gio.Menu()
        menu.append(_("About"), "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_("Main menu")))

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        toolbar = Adw.ToolbarView(content=self.toasts)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self._build_progress(), "progress")

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        self.connect("close-request", self._on_close_request)

    # ---------------------------------------------------------------- pages
    def _build_welcome(self) -> Gtk.Widget:
        button = Gtk.Button(label=_("Choose a .deb file…"), halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.connect("clicked", self._on_choose_file)
        return Adw.StatusPage(
            icon_name=_icon_or("application-x-deb", "package-x-generic-symbolic"),
            title=_("Install a .deb package"),
            description=_("Open a Debian package or drop it here. It will be converted into a native package for this system and installed."),
            child=button,
        )

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER)
        box.append(Adw.Spinner(width_request=48, height_request=48, halign=Gtk.Align.CENTER))
        self.progress_title = _label("", ("title-2",))
        self.progress_subtitle = _label("", ("dim-label",))
        box.append(self.progress_title)
        box.append(self.progress_subtitle)
        self.progress_bar = Gtk.ProgressBar(show_text=False, margin_start=24, margin_end=24)
        box.append(self.progress_bar)
        log_scroller, self.progress_log = _text_view()
        expander = Gtk.Expander(label=_("Details"), child=log_scroller, margin_top=6)
        box.append(expander)
        self.cancel_button = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.CENTER)
        self.cancel_button.add_css_class("pill")
        self.cancel_button.connect("clicked", self._on_cancel)
        box.append(self.cancel_button)
        return _clamped(box)

    def _replace_page(self, name: str, widget: Gtk.Widget) -> None:
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(widget, name)
        self.stack.set_visible_child_name(name)

    def _row(self, title: str, value: str) -> Adw.ActionRow:
        row = Adw.ActionRow(use_markup=False, subtitle_selectable=True)
        row.set_title(title)
        row.set_subtitle(value or "—")
        row.add_css_class("property")
        return row

    def _build_details(self, conv: Conversion) -> Gtk.Widget:
        deb = conv.deb
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top.append(Gtk.Image(icon_name=_icon_or("application-x-deb", "package-x-generic-symbolic"), pixel_size=96))
        top.append(_label(deb.summary or deb.name, ("title-1",)))
        top.append(_label(f"{deb.name} {deb.version}", ("dim-label",)))
        box.append(top)

        if not compatible(conv):
            box.append(self._message_group(conv.warnings, "dialog-error-symbolic", "error"))
        else:
            hint = Adw.PreferencesGroup()
            row = Adw.ActionRow(
                title=_("Prefer native packages when available"),
                subtitle=_("Programs from the system repositories, Flatpak or AppImage usually work better. Converted .deb packages may not work perfectly."),
            )
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            hint.add(row)
            native = deb.name.lower()
            if native in pacman.sync_packages():
                native_row = Adw.ActionRow(
                    title=_("“{name}” is available in the repositories").format(name=native),
                    subtitle=_("Installing the native package is recommended."),
                )
                native_row.add_prefix(Gtk.Image(icon_name="emblem-default-symbolic"))
                if shutil.which("pamac-manager"):
                    open_button = Gtk.Button(label=_("Open"), valign=Gtk.Align.CENTER)
                    open_button.connect("clicked", lambda *_a: subprocess.Popen(["pamac-manager", f"--details={native}"]))
                    native_row.add_suffix(open_button)
                hint.add(native_row)
            box.append(hint)

        details = Adw.PreferencesGroup(title=_("Details"))
        details.add(self._row(_("Package"), deb.name))
        details.add(self._row(_("Version"), deb.version))
        details.add(self._row(_("Architecture"), deb.architecture))
        details.add(self._row(_("Installed size"), GLib.format_size(deb.installed_size) if deb.installed_size else ""))
        details.add(self._row(_("Maintainer"), deb.control.get("maintainer", "")))
        homepage = deb.control.get("homepage", "").strip()
        if homepage:
            link = Adw.ActionRow(title=_("Website"), subtitle=homepage, activatable=True)
            link.add_css_class("property")
            link.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            link.connect("activated", lambda *_a: Gtk.UriLauncher.new(homepage).launch(self, None, None, None))
            details.add(link)
        box.append(details)

        if deb.long_description:
            desc = Adw.PreferencesGroup(title=_("Description"))
            desc.add(Adw.Bin(child=_label(deb.long_description, xalign=0.0)))
            box.append(desc)

        options = Adw.PreferencesGroup()
        self.review_switch = Adw.SwitchRow(
            title=_("Review before installing"),
            subtitle=_("Show the dependencies and the scripts that will run as administrator"),
        )
        options.add(self.review_switch)
        box.append(options)

        install = Gtk.Button(label=_("Install"), halign=Gtk.Align.CENTER, sensitive=compatible(conv))
        install.add_css_class("pill")
        install.add_css_class("suggested-action")
        install.connect("clicked", self._on_install_clicked)
        box.append(install)
        return _clamped(box)

    def _message_group(self, messages, icon: str, css: str, title: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        for message in messages:
            row = Adw.ActionRow(use_markup=False, title_lines=0)
            row.set_title(message)
            image = Gtk.Image(icon_name=icon)
            image.add_css_class(css)
            row.add_prefix(image)
            group.add(row)
        return group

    def _build_review(self, conv: Conversion) -> Gtk.Widget:
        spec = conv.spec
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(_label(_("Ready to install"), ("title-1",)))
        box.append(_label(_("Check what will be installed."), ("dim-label",)))

        summary = Adw.PreferencesGroup()
        summary.add(self._row(_("Package name on this system"), f"{spec.pkgname} {conv.full_version}"))
        size = conv.pkgfile.stat().st_size if conv.pkgfile else 0
        summary.add(self._row(_("Package size"), GLib.format_size(size)))
        visible = [a.name for a in conv.apps if not a.hidden]
        summary.add(self._row(_("Applications menu"), ", ".join(visible) if visible else _("No menu entry")))
        box.append(summary)

        if conv.warnings:
            box.append(self._message_group(conv.warnings, "dialog-warning-symbolic", "warning", _("Warnings")))
        if conv.notes:
            box.append(self._message_group(conv.notes, "dialog-information-symbolic", "accent", _("Notes")))

        deps = Adw.PreferencesGroup(title=_("Dependencies"))
        required = Adw.ExpanderRow(
            title=_("Required"),
            subtitle=ngettext("{n} package", "{n} packages", len(spec.depends)).format(n=len(spec.depends)),
        )
        installed = pacman.installed_packages()
        for dep in spec.depends:
            row = Adw.ActionRow(title=dep)
            if dep not in installed:
                row.set_subtitle(_("will be downloaded"))
            required.add_row(row)
        deps.add(required)
        if spec.optdepends:
            optional = Adw.ExpanderRow(
                title=_("Optional"),
                subtitle=ngettext("{n} package", "{n} packages", len(spec.optdepends)).format(n=len(spec.optdepends)),
            )
            for name, reason in spec.optdepends.items():
                row = Adw.ActionRow(use_markup=False)
                row.set_title(name)
                row.set_subtitle(reason)
                optional.add_row(row)
            deps.add(optional)
        box.append(deps)

        if conv.install_functions:
            scripts = Adw.PreferencesGroup(
                title=_("Scripts"),
                description=_("These commands from the Debian package run as administrator during installation."),
            )
            scroller, buffer = _text_view()
            buffer.set_text(conv.install_functions)
            scripts.add(scroller)
            box.append(scripts)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_a: self._back_to_details())
        install = Gtk.Button(label=_("Install"), sensitive=not (conv.conflicts and conv.conflicts.owned))
        install.add_css_class("pill")
        install.add_css_class("suggested-action")
        install.connect("clicked", lambda *_a: self._start_install())
        buttons.append(cancel)
        buttons.append(install)
        box.append(buttons)
        return _clamped(box)

    def _build_result(self, success: bool, title: str, description: str) -> Gtk.Widget:
        conv = self.conversion
        apps = [a for a in conv.apps if not a.hidden] if conv else []
        if success:
            icon = _icon_or(apps[0].icon if apps else "", "emblem-ok-symbolic")
        else:
            icon = "dialog-error-symbolic"
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        if success and apps:
            for app in apps:
                launch = Gtk.Button(label=_("Open {name}").format(name=app.name))
                launch.add_css_class("pill")
                launch.add_css_class("suggested-action")
                launch.connect("clicked", self._on_launch, app.file_id)
                box.append(launch)
        if not success and self.log_lines:
            scroller, buffer = _text_view()
            buffer.set_text("\n".join(self.log_lines))
            outer.append(Gtk.Expander(label=_("Details"), child=scroller))
            copy = Gtk.Button(label=_("Copy details"))
            copy.add_css_class("pill")
            copy.connect("clicked", self._on_copy_log)
            box.append(copy)
        close = Gtk.Button(label=_("Close"))
        close.add_css_class("pill")
        close.connect("clicked", lambda *_a: self.close())
        box.append(close)
        outer.append(box)
        page = Adw.StatusPage(icon_name=icon, title=title, description=description, child=Adw.Clamp(maximum_size=560, child=outer))
        if not success:
            page.add_css_class("compact")
        return page

    # ------------------------------------------------------------- loading
    def load(self, path: str) -> None:
        try:
            self.conversion = inspect(path)
        except (DebError, OSError) as exc:
            self._show_error(_("This file cannot be installed"), str(exc))
            return
        self.title.set_subtitle(self.conversion.deb.path.name)
        self._replace_page("details", self._build_details(self.conversion))

    def _on_choose_file(self, *_args):
        dialog = Gtk.FileDialog(title=_("Choose a .deb file"))
        deb_filter = Gtk.FileFilter(name=_("Debian packages"))
        deb_filter.add_mime_type("application/vnd.debian.binary-package")
        deb_filter.add_mime_type("application/x-deb")
        deb_filter.add_suffix("deb")
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

    def _on_drop(self, _target, value, _x, _y) -> bool:
        if self.busy:
            return False
        files = value.get_files()
        if files and files[0].get_path():
            self.load(files[0].get_path())
            return True
        return False

    def _back_to_details(self):
        if self.conversion:
            self.conversion.cleanup()
            self.conversion = inspect(str(self.conversion.deb.path))
            self._replace_page("details", self._build_details(self.conversion))

    # ---------------------------------------------------------- conversion
    def _set_step(self, step: str):
        self.current_step = step
        self.progress_title.set_label(STEP_TITLES.get(step, ""))
        if step in STEP_WEIGHTS:
            self.progress_bar.set_fraction(STEP_WEIGHTS[step][0])
        if step == "auth":
            self.progress_subtitle.set_label(_("Type your password in the window that will appear."))
        elif step == "install":
            self.progress_subtitle.set_label(_("Missing dependencies are downloaded from the repositories."))
        elif step == "build":
            self.progress_subtitle.set_label(_("Compressing the files, this may take a minute."))

    def _set_fraction(self, fraction: float):
        start, end = STEP_WEIGHTS.get(self.current_step, (0.0, 1.0))
        self.progress_bar.set_fraction(start + (end - start) * min(max(fraction, 0.0), 1.0))

    def _append_log(self, line: str):
        self.log_lines.append(line)
        buffer = self.progress_log
        buffer.insert(buffer.get_end_iter(), line + "\n")

    def _on_install_clicked(self, *_args):
        conv = self.conversion
        self.busy = True
        self.log_lines = []
        self.progress_log.set_text("")
        self.progress_subtitle.set_label(conv.deb.name)
        self.cancel_button.set_sensitive(True)
        self.current_step = "extract"
        self.stack.set_visible_child_name("progress")
        self.callbacks = Callbacks(
            step=lambda s: GLib.idle_add(self._set_step, s),
            progress=lambda f: GLib.idle_add(self._set_fraction, f),
            log=lambda line: GLib.idle_add(self._append_log, line),
        )
        threading.Thread(target=self._convert_thread, daemon=True).start()

    def _convert_thread(self):
        try:
            convert(self.conversion, self.callbacks)
        except Cancelled:
            GLib.idle_add(self._on_cancelled)
            return
        except (ConversionError, DebError, OSError) as exc:
            log = getattr(exc, "log", "")
            if log:
                for line in log.splitlines():
                    GLib.idle_add(self._append_log, line)
            GLib.idle_add(self._show_error, _("The package could not be converted"), str(exc))
            return
        GLib.idle_add(self._on_converted)

    def _on_cancelled(self):
        self.busy = False
        self.toasts.add_toast(Adw.Toast(title=_("Cancelled")))
        self._back_to_details()

    def _on_cancel(self, *_args):
        if self.callbacks and not self.installing:
            self.callbacks.cancel.set()
            self.cancel_button.set_sensitive(False)

    def _on_converted(self):
        conv = self.conversion
        blocking = conv.conflicts and conv.conflicts.owned
        if self.review_switch.get_active() or blocking:
            self.busy = False
            self._replace_page("review", self._build_review(conv))
        else:
            self._start_install()

    # -------------------------------------------------------------- install
    def _start_install(self):
        self.busy = True
        self.installing = True
        self.cancel_button.set_sensitive(False)
        self.stack.set_visible_child_name("progress")
        self._set_step("auth")
        self.progress_bar.set_fraction(1.0)
        threading.Thread(target=self._install_thread, daemon=True).start()

    def _install_thread(self):
        conv = self.conversion
        first_line = {"seen": False}

        def log(line: str):
            if not first_line["seen"] and not line.startswith("$ "):
                first_line["seen"] = True
                GLib.idle_add(self._set_step, "install")
            GLib.idle_add(self._append_log, line)

        overwrite = conv.conflicts.unowned if conv.conflicts else []
        status = installer.run_install(conv.pkgfile, overwrite, log=log, interactive_tty=False)
        ok = status == 0 and installer.is_installed(conv.spec.pkgname, conv.full_version)
        GLib.idle_add(self._on_installed, ok, status)

    def _on_installed(self, ok: bool, status: int):
        self.busy = False
        self.installing = False
        conv = self.conversion
        if ok:
            apps = [a for a in conv.apps if not a.hidden]
            if apps:
                names = ", ".join(f"“{a.name}”" for a in apps)
                description = _("Look for {names} in the applications menu.").format(names=names)
                title = _("{name} is installed").format(name=apps[0].name)
            else:
                description = _("The package {name} was installed. It has no entry in the applications menu.").format(name=conv.spec.pkgname)
                title = _("Installation complete")
            conv.cleanup()
            self._replace_page("result", self._build_result(True, title, description))
        elif status in PKEXEC_DISMISSED:
            self.toasts.add_toast(Adw.Toast(title=_("Authorization was not granted")))
            if conv.spec:
                self._replace_page("review", self._build_review(conv))
        else:
            conv.cleanup()
            self._show_error(
                _("The installation failed"),
                _("pacman returned an error (code {code}). Open the details to see what happened.").format(code=status),
            )

    # ------------------------------------------------------------ helpers
    def _show_error(self, title: str, description: str):
        self.busy = False
        self.installing = False
        self._replace_page("result", self._build_result(False, title, description))

    def _on_launch(self, _button, file_id: str):
        info = Gio.DesktopAppInfo.new(file_id)
        if info is None:
            self.toasts.add_toast(Adw.Toast(title=_("The application could not be started")))
            return
        try:
            info.launch([], self.get_display().get_app_launch_context())
        except GLib.Error as exc:
            self.toasts.add_toast(Adw.Toast(title=exc.message))
            return
        self.close()

    def _on_copy_log(self, *_args):
        self.get_display().get_clipboard().set("\n".join(self.log_lines))
        self.toasts.add_toast(Adw.Toast(title=_("Copied")))

    def _on_close_request(self, *_args) -> bool:
        if self.installing:
            self.toasts.add_toast(Adw.Toast(title=_("Please wait until the installation finishes")))
            return True
        if self.callbacks:
            self.callbacks.cancel.set()
        if self.conversion and not self.busy:
            self.conversion.cleanup()
        return False
