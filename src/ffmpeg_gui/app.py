from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk

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
    app_id = os.environ.get("FFMPEG_GUI_APP_ID", "").strip() or "io.github.slashmad.SlashmadFFmpegGUI"
    # Allow parallel app instances (useful when running dev build beside installed Flatpak).
    app = Gtk.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", _on_activate)
    app.run()


if __name__ == "__main__":
    main()
