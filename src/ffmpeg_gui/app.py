from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ffmpeg_gui.gui import MainWindow
from ffmpeg_gui.i18n import setup_gettext


def _on_activate(app: Gtk.Application) -> None:
    window = MainWindow(app)
    window.present()


def main() -> None:
    # NVIDIA + GTK4 Vulkan can be unstable in long-running live preview/capture sessions.
    # Respect user override if already set.
    os.environ.setdefault("GSK_RENDERER", "ngl")

    setup_gettext()
    app = Gtk.Application(application_id="io.github.slashmad.SlashmadFFmpegGUI")
    app.connect("activate", _on_activate)
    app.run()


if __name__ == "__main__":
    main()
