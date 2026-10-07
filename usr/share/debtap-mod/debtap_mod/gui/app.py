"""Adw.Application: one window per .deb file."""

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_ID, PROJECT_URL, __version__
from ..i18n import _
from .window import InstallerWindow


class DebtapApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(_("Install .deb package"))
        self._add_action("about", self._on_about)
        self._add_action("quit", lambda *_args: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = InstallerWindow(application=self)
        window.present()

    def do_open(self, files, _n_files, _hint):
        for gfile in files:
            path = gfile.get_path()
            window = InstallerWindow(application=self)
            if path:
                window.load(path)
            window.present()

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Install .deb package"),
            application_icon="application-x-deb",
            developer_name="BigLinux",
            version=__version__,
            website=PROJECT_URL,
            issue_url=PROJECT_URL + "/issues",
            license_type=Gtk.License.GPL_2_0,
            comments=_("Converts Debian packages into native pacman packages and installs them."),
            developers=["Bruno Goncalves", "Tales A. Mendonça", "George Savvidis (debtap)"],
        )
        about.present(self.get_active_window())


def main(argv: list[str]) -> int:
    app = DebtapApplication()
    return app.run(argv)
