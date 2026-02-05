# FFmpeg GUI (GTK4)

A lightweight GTK4 GUI for FFmpeg with hardware-acceleration detection.

## Features

- Paste paths, pick files/folders, or drag-and-drop images.
- Output defaults to the same folder as your images.
- Choose codec, preset, quality (CRF/CQ), pixel format, FPS, tune, and extra FFmpeg args.
- Supports common image formats plus RAW (if your FFmpeg build supports it).

## Run locally (Fedora)

Install dependencies (package names may vary by Fedora version):

- `python3`
- `python3-gobject`
- `gtk4`
- `ffmpeg`

Run:

```
./dev_run.sh
```

Or install editable and run:

```
python3 -m pip install -e .
ffmpeg-gui
```

## Flatpak

Build and run with Flatpak Builder (adjust `runtime-version` to what you have installed):

```
flatpak-builder --force-clean --install-deps-from=flathub --user build flatpak/com.slashmad.TimelapseFFmpegGUI.yml
flatpak-builder --run build flatpak/com.slashmad.TimelapseFFmpegGUI.yml ffmpeg-gui
```

Notes:
- The app calls `ffmpeg` to detect available hardware acceleration. In Flatpak it tries to run host `ffmpeg` via `flatpak-spawn --host`.
- If you prefer bundling FFmpeg, update the manifest to include a module for it.

### Theme In Flatpak

Flatpak apps use the GNOME runtime theme (Adwaita) by default. To match your desktop theme, install
GTK theme extensions in Flatpak. Example for Breeze:

```
flatpak install --user flathub org.gtk.Gtk3theme.Breeze
flatpak install --user flathub org.gtk.Gtk4theme.Breeze
```

If no GTK4 theme is available, the app will fall back to Adwaita but still respect your system
dark/light preference via the portal.

### Flatpak Theme Override (Optional)

If your desktop theme is not available in Flatpak (e.g., no GTK4 Breeze), you can force Adwaita dark:

```
flatpak override --user --env=GTK_THEME=Adwaita:dark com.slashmad.TimelapseFFmpegGUI
```

To remove the override:

```
flatpak override --user --reset com.slashmad.TimelapseFFmpegGUI
```

### App Store Assets

To show screenshots in app stores (AppStream/Flathub), place images in `data/screenshots/`
and update `data/com.slashmad.TimelapseFFmpegGUI.metainfo.xml` with their public URLs. Example filenames:

- `data/screenshots/encode.png`
- `data/screenshots/hardware.png`

If you don't use GitHub, replace the screenshot URLs in the metainfo with your own hosted files.
