"""Small building blocks shared by the pages."""

from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

CSS_FILE = Path(__file__).with_name("style.css")
ICONS_DIR = Path(__file__).with_name("icons")
CROSS_ICON = "debtap-cross-symbolic"
CHECK_ICON = "debtap-check-symbolic"


def load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    # bundled status icons, identical in every icon theme
    Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS_DIR))
    provider = Gtk.CssProvider()
    provider.load_from_path(str(CSS_FILE))
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def icon_or(name: str, fallback: str) -> str:
    display = Gdk.Display.get_default()
    if name and display and Gtk.IconTheme.get_for_display(display).has_icon(name):
        return name
    return fallback


DEB_ICON = "application-x-deb"


def deb_icon() -> str:
    return icon_or(DEB_ICON, "package-x-generic")


def label(text: str = "", *css: str, wrap: bool = True, xalign: float = 0.5, selectable: bool = False, **props) -> Gtk.Label:
    widget = Gtk.Label(label=text, wrap=wrap, xalign=xalign, selectable=selectable, **props)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_justify(Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT)
    for cls in css:
        widget.add_css_class(cls)
    return widget


def status_icon(kind: str, icon: str) -> Gtk.Widget:
    """Big round status icon: success, error, accent (colored circle) or warning."""
    image = Gtk.Image(icon_name=icon, pixel_size=36 if kind != "warning" else 64)
    if kind == "warning":
        image.add_css_class("status-warning")
        image.set_halign(Gtk.Align.CENTER)
        return image
    circle = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    circle.add_css_class("status-circle")
    circle.add_css_class(kind)
    image.set_hexpand(True)
    image.set_vexpand(True)
    circle.append(image)
    return circle


def banner(kind: str, text: str, icon: str | None = None) -> Gtk.Widget:
    icons = {"info": "dialog-information-symbolic", "warning": "dialog-warning-symbolic", "error": "dialog-error-symbolic"}
    box = Gtk.Box(spacing=12)
    box.add_css_class("banner")
    box.add_css_class(f"banner-{kind}")
    box.append(Gtk.Image(icon_name=icon or icons[kind], valign=Gtk.Align.START))
    box.append(label(text, xalign=0.0, wrap=True))
    return box


def badge(text: str) -> Gtk.Label:
    widget = Gtk.Label(label=text, halign=Gtk.Align.START)
    widget.add_css_class("badge")
    return widget


def file_icon(path: Path) -> Gio.Icon:
    """Icon of the real file type (RPM, AppImage, archive…), never the .deb icon by default."""
    try:
        info = Gio.File.new_for_path(str(path)).query_info("standard::icon", Gio.FileQueryInfoFlags.NONE, None)
        icon = info.get_icon()
        if icon is not None:
            return icon
    except GLib.Error:
        pass
    return Gio.ThemedIcon.new("text-x-generic")


def file_status_icon(path: Path) -> Gtk.Widget:
    """Big icon of the file's real type (RPM, AppImage…) with a red error badge."""
    overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
    overlay.set_child(Gtk.Image(gicon=file_icon(path), pixel_size=112))
    badge_box = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
    badge_box.add_css_class("status-circle")
    badge_box.add_css_class("error")
    badge_box.add_css_class("status-badge")
    badge_box.append(Gtk.Image(icon_name=CROSS_ICON, pixel_size=16, hexpand=True, vexpand=True))
    overlay.add_overlay(badge_box)
    return overlay


def file_card(path: Path) -> Gtk.Widget:
    try:
        size = GLib.format_size(path.stat().st_size)
    except OSError:
        size = ""
    row = Adw.ActionRow(title=path.name, subtitle=size, use_markup=False)
    row.add_prefix(Gtk.Image(gicon=file_icon(path), pixel_size=40))
    group = Adw.PreferencesGroup()
    group.add(row)
    return group


def property_row(key: str, value: str, on_activate=None, suffix_icon: str | None = None) -> Gtk.Widget:
    row = Adw.ActionRow(use_markup=False, activatable=on_activate is not None)
    box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
    key_label = label(key, "prop-key", xalign=0.0, wrap=False)
    key_label.set_size_request(150, -1)
    value_label = label(value or "—", xalign=0.0, selectable=True)
    value_label.set_hexpand(True)
    box.append(key_label)
    box.append(value_label)
    if suffix_icon:
        box.append(Gtk.Image(icon_name=suffix_icon, valign=Gtk.Align.CENTER))
    row.set_child(box)
    if on_activate is not None:
        row.connect("activated", lambda *_a: on_activate())
    return row


def page_header(title: str, subtitle: str, on_back=None, icon: str | None = None) -> Gtk.Widget:
    box = Gtk.Box(spacing=12, margin_bottom=6)
    if on_back is not None:
        back = Gtk.Button(icon_name="go-previous-symbolic", valign=Gtk.Align.CENTER, tooltip_text=None)
        back.add_css_class("flat")
        back.add_css_class("circular")
        back.connect("clicked", lambda *_a: on_back())
        box.append(back)
    elif icon:
        image = Gtk.Image(icon_name=icon, pixel_size=22, valign=Gtk.Align.START, margin_top=4)
        image.add_css_class("download-icon")
        box.append(image)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    texts.append(label(title, "title-3", xalign=0.0))
    texts.append(label(subtitle, "dim-label", xalign=0.0))
    box.append(texts)
    return box


def code_box(text: str, on_copy=None, min_height: int = 120) -> Gtk.Widget:
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
    for side in ("top", "bottom", "left", "right"):
        getattr(view, f"set_{side}_margin")(10)
    view.get_buffer().set_text(text)
    scroller = Gtk.ScrolledWindow(child=view, min_content_height=min_height, max_content_height=max(320, min_height),
                                  propagate_natural_height=True)
    scroller.add_css_class("code-box")
    if on_copy is None:
        return scroller
    overlay = Gtk.Overlay(child=scroller)
    copy = Gtk.Button(icon_name="edit-copy-symbolic", halign=Gtk.Align.END, valign=Gtk.Align.START,
                      margin_top=6, margin_end=6)
    copy.add_css_class("flat")
    copy.add_css_class("circular")
    copy.connect("clicked", lambda *_a: on_copy())
    overlay.add_overlay(copy)
    return overlay


def action_bar(*buttons: Gtk.Button) -> Gtk.Widget:
    box = Gtk.Box(spacing=12, homogeneous=True)
    box.add_css_class("action-bar")
    for button in buttons:
        box.append(button)
    return box


def button(text: str, icon: str | None = None, suggested: bool = False, icon_end: bool = False) -> Gtk.Button:
    content = Adw.ButtonContent(label=text, use_underline=True)
    if icon:
        content.set_icon_name(icon)
    widget = Gtk.Button()
    if icon and icon_end:
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        box.append(Gtk.Label(label=text))
        box.append(Gtk.Image(icon_name=icon))
        widget.set_child(box)
    elif icon:
        widget.set_child(content)
    else:
        widget.set_label(text)
    if suggested:
        widget.add_css_class("suggested-action")
    return widget


def scrolled_page(child: Gtk.Widget, bottom: Gtk.Widget | None = None, valign_center: bool = False) -> Gtk.Widget:
    if valign_center:
        child.set_valign(Gtk.Align.CENTER)
    clamp = Adw.Clamp(maximum_size=620, child=child, margin_top=18, margin_bottom=12, margin_start=18, margin_end=18)
    scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    if bottom is None:
        return scroller
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(scroller)
    box.append(Adw.Clamp(maximum_size=620, child=bottom))
    return box


class StepList(Gtk.Box):
    """Vertical checklist: done (green check), current (blue ring), pending."""

    def __init__(self, labels: list[str]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.rows = []
        for text in labels:
            row = Gtk.Box(spacing=12)
            dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, hexpand=False, vexpand=False)
            dot.set_size_request(18, 18)
            dot.add_css_class("step-dot")
            text_label = label(text, "step-label", xalign=0.0, hexpand=True)
            row.append(dot)
            row.append(text_label)
            self.append(row)
            self.rows.append((dot, text_label))
        self.set_current(0)

    def set_label(self, index: int, text: str) -> None:
        self.rows[index][1].set_label(text)

    def set_current(self, index: int) -> None:
        for i, (dot, text_label) in enumerate(self.rows):
            for cls in ("done", "current", "pending"):
                dot.remove_css_class(cls)
                text_label.remove_css_class(cls)
            while (child := dot.get_first_child()) is not None:
                dot.remove(child)
            state = "done" if i < index else ("current" if i == index else "pending")
            dot.add_css_class(state)
            text_label.add_css_class(state)
            if state == "done":
                dot.append(Gtk.Image(icon_name=CHECK_ICON, pixel_size=10, hexpand=True, vexpand=True,
                                     halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER))
            elif state == "current":
                # the dot has hexpand set explicitly, so this cannot stretch it
                inner = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=True, vexpand=True)
                inner.set_size_request(8, 8)
                dot.append(inner)
            dot.set_hexpand(False)
