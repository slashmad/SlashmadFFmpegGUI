from __future__ import annotations

from importlib.resources import as_file, files

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango


_UI_PACKAGE = "ffmpeg_gui"


def load_builder(filename: str) -> Gtk.Builder:
    builder = Gtk.Builder()
    resource = files(_UI_PACKAGE).joinpath("ui", filename)
    with as_file(resource) as path:
        builder.add_from_file(str(path))
    return builder


def require_object(builder: Gtk.Builder, object_id: str):
    obj = builder.get_object(object_id)
    if obj is None:
        raise RuntimeError(f"Missing GtkBuilder object: {object_id}")
    return obj


def bind_objects(target: object, builder: Gtk.Builder, object_ids: list[str]) -> None:
    for object_id in object_ids:
        setattr(target, object_id, require_object(builder, object_id))


def compact_widget(widget: Gtk.Widget, width: int, *, expand: bool = False) -> None:
    widget.set_size_request(width, -1)
    widget.set_hexpand(expand)
    if not expand:
        widget.set_halign(Gtk.Align.START)
    if isinstance(widget, Gtk.ComboBox):
        for cell in widget.get_cells():
            if isinstance(cell, Gtk.CellRendererText):
                cell.set_property("ellipsize", Pango.EllipsizeMode.END)


def present_text_buffer_window(
    parent: Gtk.Widget,
    title: str,
    buffer: Gtk.TextBuffer,
    *,
    monospace: bool = True,
    wrap_mode: Gtk.WrapMode = Gtk.WrapMode.NONE,
    default_width: int = 900,
    default_height: int = 420,
    window: Gtk.Window | None = None,
) -> Gtk.Window:
    if window is not None:
        window.present()
        return window

    root = parent.get_root()
    transient = root if isinstance(root, Gtk.Window) else None

    text_view = Gtk.TextView()
    text_view.set_buffer(buffer)
    text_view.set_editable(False)
    text_view.set_cursor_visible(False)
    text_view.set_monospace(monospace)
    text_view.set_wrap_mode(wrap_mode)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_hexpand(True)
    scroller.set_child(text_view)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    content.set_margin_top(5)
    content.set_margin_bottom(5)
    content.set_margin_start(5)
    content.set_margin_end(5)
    content.append(scroller)

    log_window = Gtk.Window(title=title, transient_for=transient, modal=False)
    log_window.set_default_size(default_width, default_height)
    log_window.set_hide_on_close(True)
    log_window.set_child(content)
    log_window.present()
    return log_window
