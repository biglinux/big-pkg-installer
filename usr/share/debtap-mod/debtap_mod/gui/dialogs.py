"""About, Preferences and log dialogs."""

from gi.repository import Adw, Gio, GLib, Gtk

from .. import PROJECT_URL, __version__
from ..i18n import _
from ..settings import Settings
from .widgets import badge, code_box, deb_icon, label

APP_NAME = _("Package Installer")
CREDITS = (
    ("Bruno Goncalves", "BigLinux"),
    ("Tales A. Mendonça", "BigLinux"),
    ("George Savvidis", _("original debtap")),
)
LICENSE_URL = "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"


def _open_uri(parent: Gtk.Widget, uri: str) -> None:
    Gtk.UriLauncher.new(uri).launch(parent.get_root(), None, None, None)


def _nav_row(title: str, icon: str, external: bool = False) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, activatable=True)
    row.add_prefix(Gtk.Image(icon_name=icon))
    row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic" if external else "go-next-symbolic"))
    return row


class AboutDialog(Adw.Dialog):
    def __init__(self):
        super().__init__(title=_("About {name}").format(name=APP_NAME), content_width=440)
        self.nav = Adw.NavigationView()
        self.nav.add(self._main_page())
        self.set_child(self.nav)

    def _page(self, title: str, child: Gtk.Widget) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scroller = Gtk.ScrolledWindow(child=child, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        toolbar.set_content(scroller)
        return Adw.NavigationPage(title=title, child=toolbar)

    def _main_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=24, margin_end=24, margin_bottom=20)
        box.append(Gtk.Image(icon_name=deb_icon(), pixel_size=96))
        box.append(label(APP_NAME, "title-1"))
        box.append(label("BigLinux", "dim-label"))
        version = badge(__version__)
        version.set_halign(Gtk.Align.CENTER)
        box.append(version)
        box.append(label(_("A simple and safe way to install .deb and .rpm packages."), margin_top=4))

        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=12)
        rows.add_css_class("boxed-list")
        credits = _nav_row(_("Credits"), "system-users-symbolic")
        credits.connect("activated", lambda *_a: self.nav.push(self._credits_page()))
        license_row = _nav_row(_("License"), "text-x-generic-symbolic")
        license_row.connect("activated", lambda *_a: self.nav.push(self._license_page()))
        site = _nav_row(_("Project website"), "web-browser-symbolic", external=True)
        site.connect("activated", lambda *_a: _open_uri(self, PROJECT_URL))
        for row in (credits, license_row, site):
            rows.append(row)
        box.append(rows)
        box.append(label(_("Made for BigLinux with GTK4/Libadwaita"), "dim-label", "caption", margin_top=12))
        return self._page(_("About"), box)

    def _credits_page(self) -> Adw.NavigationPage:
        group = Adw.PreferencesGroup(margin_start=18, margin_end=18, margin_top=6, margin_bottom=18)
        for name, role in CREDITS:
            group.add(Adw.ActionRow(title=name, subtitle=role, use_markup=False))
        return self._page(_("Credits"), group)

    def _license_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24, margin_bottom=20)
        box.append(label(_("This program is free software, distributed under the GNU General Public License, "
                           "version 2 or later. It comes with absolutely no warranty."), xalign=0.0))
        link = Gtk.LinkButton(uri=LICENSE_URL, label="GNU GPL v2.0", halign=Gtk.Align.START)
        box.append(link)
        return self._page(_("License"), box)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self):
        super().__init__(title=_("Preferences"))
        self.settings = Settings.load()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=_("Converted packages"),
            description=_("The .deb or .rpm file is converted into a native package before it is installed."),
        )
        self.save_switch = Adw.SwitchRow(
            title=_("Keep a copy of the converted package"),
            subtitle=_("Useful to reinstall it later or on another computer"),
            active=self.settings.save_copy,
        )
        self.save_switch.connect("notify::active", self._on_changed)
        self.folder_row = Adw.ActionRow(title=_("Folder"), subtitle=str(self.settings.save_path), use_markup=False)
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose)
        self.folder_row.add_suffix(choose)
        self.folder_row.set_sensitive(self.settings.save_copy)
        group.add(self.save_switch)
        group.add(self.folder_row)
        page.add(group)
        self.add(page)

    def _on_changed(self, *_args):
        self.settings.save_copy = self.save_switch.get_active()
        self.folder_row.set_sensitive(self.settings.save_copy)
        self.settings.save()

    def _on_choose(self, *_args):
        dialog = Gtk.FileDialog(title=_("Folder"), initial_folder=Gio.File.new_for_path(str(self.settings.save_path)))
        dialog.select_folder(self.get_root(), None, self._on_folder)

    def _on_folder(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            self.settings.save_dir = folder.get_path()
            self.folder_row.set_subtitle(self.settings.save_dir)
            self.settings.save()


class LogDialog(Adw.Dialog):
    def __init__(self, text: str, on_copy):
        super().__init__(title=_("Details"), content_width=640, content_height=480)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=_("Copy details"))
        copy.connect("clicked", lambda *_a: on_copy())
        header.pack_start(copy)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12, margin_bottom=12)
        view = code_box(text, min_height=360)
        view.set_vexpand(True)
        box.append(view)
        toolbar.set_content(box)
        self.set_child(toolbar)
