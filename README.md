# SlashmadFFmpegGUI

GTK4 frontend for FFmpeg maintained by `slashmad`, focused on capture, review, trimming, cleanup, and export workflows.

Repository:
`https://github.com/slashmad/SlashmadFFmpegGUI`

License:
`GPL-3.0-or-later`

## Features

- FFmpeg encode workflow with hardware-acceleration detection.
- Dedicated `Capture` tab for VHS, USB, PCI, and PCIe capture devices.
- Dedicated `Edit` tab with in-app playback, trim controls, frame stepping, and export.
- Live video and live audio monitoring with independent mute and volume control.
- Capture profiles for archive, delivery, and proxy outputs.
- Analog source input selection for devices exposing `Composite` / `S-Video`.
- Explicit FFmpeg command preview for capture and export jobs.
- Flatpak support with host-device discovery via `flatpak-spawn --host`.

## Search Terms

This project is intended to be discoverable for common Linux video digitizing searches, including:

- `Magix capture Linux`
- `Magix USB Videowandler Linux`
- `USB to VHS capture Linux`
- `VHS capture Linux`
- `VHS digitizing Linux`
- `S-Video capture Linux`
- `Composite capture Linux`
- `analog video capture Linux`
- `FFmpeg VHS capture GUI`
- `Linux USB video capture GUI`

## Edit Workflow Notes

- Load a captured file in `Edit`.
- Use the built-in player transport (`Play`, `Pause`, seek timeline) to inspect the source.
- `Left` / `Right` steps backward or forward by one frame in preview.
- `Shift+Left` / `Shift+Right` jumps one second.
- The trim bar uses one combined range with `start` and `end` handles.
- Click a trim handle and use arrow keys to nudge that specific handle.
- Use the dedicated `-1f` / `+1f` buttons under the trim bar for manual frame-accurate start/end adjustment.
- Default export mode is `Keep source streams`, which trims/remuxes without re-encoding.
- Switch to `Re-encode` when applying denoise, deinterlace, color correction, sync changes, or new codecs.

## VHS Capture Notes

- For em28xx-based hardware, `Live during capture = Stop live view` is still the most stable path.
- `Auto-fallback` can be used if you want monitoring during capture and accept fallback to lighter preview behavior when needed.
- The app keeps default archive capture as raw stereo with no tonal cleanup enabled by default.
- Prefer stable ALSA identifiers shown in the UI (`plughw:CARD=...,DEV=...`) over boot-dependent `hw:X,Y` numbers.
- Typical use case: digitizing VHS on Linux from a Magix USB capture device or similar USB analog-video hardware.

Suggested archive baseline:

- Profile: `VHS Archive (FFV1 + PCM)`
- Container: `MKV`
- Video codec: `ffv1`
- Audio codec: `pcm_s16le`
- TV standard: `PAL`
- Input format: `YUYV` mapped to FFmpeg `yuyv422`

Approximate size rates shown in the UI:

- `VHS Archive (FFV1 + PCM)`: about `~19.4 GiB/h`
- `VHS Delivery (H.264 + AAC)`: about `~2.6 GiB/h`
- `VHS Proxy (MJPEG + PCM)`: about `~9.0 GiB/h`

## Magix S-Video Fix (em28xx `card=105`)

If `Composite` works but `S-Video` is black, force em28xx board profile `105`.

Temporary test:

```bash
sudo modprobe -r em28xx_v4l em28xx_alsa em28xx
sudo modprobe em28xx card=105 usb_xfer_mode=1
sudo modprobe em28xx_v4l
```

Permanent setup:

```bash
sudo tee /etc/modprobe.d/em28xx-magix.conf >/dev/null <<'EOF2'
options em28xx card=105 usb_xfer_mode=1
EOF2
```

Reload modules:

```bash
sudo modprobe -r em28xx_v4l em28xx_alsa em28xx
sudo modprobe em28xx
sudo modprobe em28xx_v4l
```

## Run Locally (Fedora)

Typical dependencies:

- `python3`
- `python3-gobject`
- `gtk4`
- `ffmpeg`
- `v4l-utils`
- `pipewire-utils` or PulseAudio tooling
- `gstreamer1`
- `gstreamer1-plugins-good`
- `gstreamer1-plugins-bad-free`
- `gstreamer1-plugins-bad-free-gtk4`

Run in development mode:

```bash
./dev_run.sh
```

Or install locally and run:

```bash
python3 -m pip install -e .
slashmad-ffmpeg-gui
```

## Flatpak

Build and run with Flatpak Builder:

```bash
flatpak-builder --force-clean --install-deps-from=flathub --user build flatpak/com.slashmad.SlashmadFFmpegGUI.yml
flatpak-builder --run build flatpak/com.slashmad.SlashmadFFmpegGUI.yml slashmad-ffmpeg-gui
```

Notes:

- Inside Flatpak, hardware probing uses host commands via `flatpak-spawn --host`.
- Capture discovery for `v4l2-ctl`, `pactl`, and `arecord` is executed on host.
- The included Flatpak manifest grants the permissions needed for capture workflows:
  - `--share=network`
  - `--share=ipc`
  - `--device=all`
  - `--socket=pulseaudio`
  - `--filesystem=/run/udev:ro`
  - `--filesystem=/mnt`
  - `--filesystem=/media`
  - `--filesystem=/run/media`
  - `--filesystem=xdg-download`
  - `--filesystem=xdg-videos`

## Flatpak Theme Override

Force Adwaita dark if your desktop theme is unavailable in the runtime:

```bash
flatpak override --user --env=GTK_THEME=Adwaita:dark com.slashmad.SlashmadFFmpegGUI
```

Reset the override:

```bash
flatpak override --user --reset com.slashmad.SlashmadFFmpegGUI
```
