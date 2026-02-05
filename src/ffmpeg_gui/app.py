from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ffmpeg_gui.gui import MainWindow
from ffmpeg_gui.i18n import setup_gettext


def _on_activate(app: Gtk.Application) -> None:
    window = MainWindow(app)
    window.present()


def main() -> None:
    setup_gettext()
    app = Gtk.Application(application_id="com.slashmad.TimelapseFFmpegGUI")
    app.connect("activate", _on_activate)
    app.run()


if __name__ == "__main__":
    main()
