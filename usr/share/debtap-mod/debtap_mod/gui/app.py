"""Adw.Application: one window per .deb file."""

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_ID, PROJECT_URL
from .dialogs import APP_NAME, AboutDialog, PreferencesDialog
from .widgets import load_css
from .window import InstallerWindow

HELP_URL = PROJECT_URL + "#usage"
FEEDBACK_URL = "https://forum.biglinux.com.br"
ISSUES_URL = PROJECT_URL + "/issues"


class DebtapApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self._add_action("about", lambda *_a: AboutDialog().present(self.get_active_window()))
        self._add_action("preferences", lambda *_a: PreferencesDialog().present(self.get_active_window()))
        self._add_action("help", lambda *_a: self._open(HELP_URL))
        self._add_action("feedback", lambda *_a: self._open(FEEDBACK_URL))
        self._add_action("report", lambda *_a: self._open(ISSUES_URL))
        self._add_action("quit", lambda *_a: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.help", ["F1"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _open(self, uri: str) -> None:
        Gtk.UriLauncher.new(uri).launch(self.get_active_window(), None, None, None)

    def do_startup(self):
        Adw.Application.do_startup(self)
        load_css()

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


def main(argv: list[str]) -> int:
    app = DebtapApplication()
    return app.run(argv)
