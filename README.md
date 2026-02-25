# FFmpeg GUI (GTK4)

A lightweight GTK4 GUI for FFmpeg with hardware-acceleration detection.

## Features

- Paste paths, pick files/folders, or drag-and-drop images.
- Output defaults to the same folder as your images.
- Choose codec, preset, quality (CRF/CQ), pixel format, FPS, tune, and extra FFmpeg args.
- Supports common image formats plus RAW (if your FFmpeg build supports it).
- Dedicated **Capture** tab for VHS/USB/PCI capture workflows.
- Live video + live audio monitoring in-app with independent mute/volume controls.
- Capture profiles (archive/delivery/proxy), source format selection, and FFmpeg command preview.
- Analog source input selector (for devices that expose it), e.g. `Composite` / `S-Video`.
- Live-during-capture policy (`Stop`, `Keep`, `Auto-fallback`) with watchdog-based audio recovery.
- Audio cleanup presets (hum filter/cleanup) and gain controls for noisy analog captures.
- `Keep`/`Auto-fallback` now use a single-device monitor stream during capture (preview comes from the same FFmpeg process instead of opening `/dev/video*` twice).

## VHS Capture Notes (Magix / em28xx)

- For best stability on em28xx devices, use `Live during capture = Stop live view`.
- If you want simultaneous monitoring and capture, use `Auto-fallback`; the app will fall back to video-only preview if live audio fails.
- In `Keep`/`Auto-fallback`, the app starts capture first and then previews from an internal local UDP monitor stream, reducing V4L2 contention.
- The app keeps default capture as raw stereo (`Channels = 2`, `Audio cleanup = Off`, `Gain = 0.0 dB`).
- If you have analog hum/noise, enable one of the cleanup presets (`Hum 50 Hz + cleanup` is usually correct in Sweden/Europe).
- ALSA card indices (`hw:1,0`, `hw:2,0`) can change between boots; prefer stable source names shown in the UI (`plughw:CARD=...,DEV=...`).

Suggested VHS archive baseline:

- Profile: `VHS Archive (FFV1 + PCM)`
- Container: `MKV`
- Video codec: `ffv1`
- Audio codec: `pcm_s16le`
- TV standard: `PAL`
- Input format: `YUYV` (mapped to FFmpeg `yuyv422`)

Approximate profile size rates (shown in the Capture UI when selecting a profile):

- `VHS Archive (FFV1 + PCM)`: about `~19.4 GiB/h` (`~19893 MiB/h`) on current sample setup (content-dependent).
- `VHS Delivery (H.264 + AAC)`: about `~2.6 GiB/h` (`~2657 MiB/h`) with `6M + 192k`.
- `VHS Proxy (MJPEG + PCM)`: about `~9.0 GiB/h` (`~9242 MiB/h`) using typical MJPEG assumptions.

### Magix S-Video fix (em28xx `card=105`)

Some Linux setups auto-detect Magix USB Videowandler with a board profile where `S-Video` does not route correctly.
If `Composite` works but `S-Video` is black, force em28xx board profile `105`.

Temporary test (until reboot):

```bash
sudo modprobe -r em28xx_v4l em28xx_alsa em28xx
sudo modprobe em28xx card=105 usb_xfer_mode=1
sudo modprobe em28xx_v4l
```

Permanent setup:

```bash
sudo tee /etc/modprobe.d/em28xx-magix.conf >/dev/null <<'EOF'
options em28xx card=105 usb_xfer_mode=1
EOF
```

Then reconnect the USB device or reload modules:

```bash
sudo modprobe -r em28xx_v4l em28xx_alsa em28xx
sudo modprobe em28xx
sudo modprobe em28xx_v4l
```

## Run locally (Fedora)

Install dependencies (package names may vary by Fedora version):

- `python3`
- `python3-gobject`
- `gtk4`
- `ffmpeg`
- `v4l-utils`
- `pipewire-utils` (or PulseAudio tooling)
- `gstreamer1`
- `gstreamer1-plugins-good`
- `gstreamer1-plugins-bad-free`
- `gstreamer1-plugins-bad-free-gtk4` (for embedded live video preview)

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
