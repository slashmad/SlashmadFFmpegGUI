from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gio, Gtk

from ffmpeg_gui.ffmpeg import EncoderInfo, PixelFormat
from ffmpeg_gui.i18n import _
from ffmpeg_gui.runner import FFmpegRunner

GST_AVAILABLE = False
GST_GTK4PAINTABLE_AVAILABLE = False
Gst = None  # type: ignore[assignment]

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst as _Gst  # type: ignore

    Gst = _Gst
    Gst.init(None)
    GST_AVAILABLE = True
    GST_GTK4PAINTABLE_AVAILABLE = Gst.ElementFactory.find("gtk4paintablesink") is not None
except Exception:
    GST_AVAILABLE = False
    GST_GTK4PAINTABLE_AVAILABLE = False


PRESETS_X264 = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
]

PRESETS_NVENC = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "fast", "medium", "slow"]

CONTAINERS = ["mkv", "mp4", "mov", "avi"]

LIVE_DURING_CAPTURE_POLICIES: list[tuple[str, str]] = [
    ("stop", _("Stop live view")),
    ("keep", _("Keep live view")),
    ("auto", _("Auto-fallback")),
]

AUDIO_FILTER_PRESETS: list[tuple[str, str]] = [
    ("off", _("Off")),
    ("cleanup_mild", _("Mild cleanup")),
    ("hum50", _("Hum filter 50 Hz")),
    ("hum50_cleanup", _("Hum 50 Hz + cleanup")),
    ("hum60", _("Hum filter 60 Hz")),
    ("hum60_cleanup", _("Hum 60 Hz + cleanup")),
]

VHS_PROFILES: list[dict[str, Any]] = [
    {
        "id": "vhs_archive_ffv1",
        "name": _("VHS Archive (FFV1 + PCM)"),
        "description": _("Preservation profile. Large files, minimal generational loss."),
        "values": {
            "container": "mkv",
            "video_codec": "ffv1",
            "audio_codec": "pcm_s16le",
            "video_bitrate": "",
            "audio_bitrate": "",
            "video_size": "720x576",
            "video_input_format": "YUYV",
            "pixel_format": "yuv422p",
            "sample_rate": 48000,
            "channels": "2",
            "match_source_fps": True,
            "output_fps": 25.0,
            "deinterlace": False,
            "video_preset": "auto",
            "video_tune": "",
            "video_standard": "pal",
            "video_source_input": "0",
            "audio_filter_preset": "off",
            "audio_gain_db": 0.0,
        },
    },
    {
        "id": "vhs_delivery_h264",
        "name": _("VHS Delivery (H.264 + AAC)"),
        "description": _("Balanced profile for playback and sharing (capture-safe defaults)."),
        "values": {
            "container": "mp4",
            "video_codec": "libx264",
            "audio_codec": "aac",
            "video_bitrate": "6M",
            "audio_bitrate": "192k",
            "video_size": "720x576",
            "video_input_format": "YUYV",
            "pixel_format": "yuv420p",
            "sample_rate": 48000,
            "channels": "2",
            "match_source_fps": True,
            "output_fps": 25.0,
            "deinterlace": True,
            "video_preset": "ultrafast",
            "video_tune": "",
            "video_standard": "pal",
            "video_source_input": "0",
            "audio_filter_preset": "off",
            "audio_gain_db": 0.0,
        },
    },
    {
        "id": "vhs_proxy_mjpeg",
        "name": _("VHS Proxy (MJPEG + PCM)"),
        "description": _("Lighter realtime profile for weaker systems."),
        "values": {
            "container": "avi",
            "video_codec": "mjpeg",
            "audio_codec": "pcm_s16le",
            "video_bitrate": "",
            "audio_bitrate": "",
            "video_size": "640x480",
            "video_input_format": "YUYV",
            "pixel_format": "yuv422p",
            "sample_rate": 48000,
            "channels": "2",
            "match_source_fps": True,
            "output_fps": 25.0,
            "deinterlace": False,
            "video_preset": "auto",
            "video_tune": "",
            "video_standard": "auto",
            "video_source_input": "0",
            "audio_filter_preset": "off",
            "audio_gain_db": 0.0,
        },
    },
]

V4L2_TO_FFMPEG_INPUT_FORMAT: dict[str, str] = {
    "YUYV": "yuyv422",
    "UYVY": "uyvy422",
    "YVYU": "yvyu422",
    "MJPG": "mjpeg",
    "JPEG": "mjpeg",
    "YU12": "yuv420p",
    "YV12": "yuv420p",
    "NV12": "nv12",
    "NV21": "nv21",
    "RGB3": "rgb24",
    "BGR3": "bgr24",
    "GREY": "gray",
    "H264": "h264",
    "HEVC": "hevc",
}

V4L2_TO_GST_RAW_FORMAT: dict[str, str] = {
    "YUYV": "YUY2",
    "UYVY": "UYVY",
    "YVYU": "YVYU",
    "YU12": "I420",
    "YV12": "YV12",
    "NV12": "NV12",
    "NV21": "NV21",
    "RGB3": "RGB",
    "BGR3": "BGR",
    "GREY": "GRAY8",
}

MAGIX_USB_VENDOR_ID = "1b80"
MAGIX_USB_PRODUCT_ID = "e349"


def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID") or os.environ.get("FLATPAK_SANDBOX_DIR"))


def _run_command(args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    cmd = list(args)
    if _is_flatpak():
        cmd = ["flatpak-spawn", "--host"] + cmd
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", _("Command not found: ") + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", _("Command timed out")


def _pick_free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _parse_bitrate_text(value: str) -> int | None:
    text = value.strip().lower()
    if not text:
        return None

    text = text.replace("bit/s", "").replace("bits/s", "").replace("bps", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg])?", text)
    if not match:
        return None

    amount = float(match.group(1))
    suffix = match.group(2) or ""
    scale = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0, "g": 1_000_000_000.0}
    return int(amount * scale[suffix])


def _format_rate_per_hour(bits_per_second: int) -> str:
    bytes_per_hour = (bits_per_second / 8.0) * 3600.0
    mib_per_hour = bytes_per_hour / (1024.0 * 1024.0)
    gib_per_hour = bytes_per_hour / (1024.0 * 1024.0 * 1024.0)
    return f"~{gib_per_hour:.1f} GiB/h ({mib_per_hour:.0f} MiB/h)"


def _ffprobe_bitrate(path: Path) -> int | None:
    rc, out, _ = _run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=12.0,
    )
    if rc != 0 or not out:
        return None
    try:
        return int(float(out.strip()))
    except ValueError:
        return None


def _detect_ffv1_reference_bitrate() -> tuple[int | None, str]:
    candidates = [
        Path.cwd() / "capture-band1.mkv",
        Path(__file__).resolve().parents[2] / "capture-band1.mkv",
    ]

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue
        bitrate = _ffprobe_bitrate(candidate)
        if bitrate and bitrate > 0:
            return bitrate, str(candidate)

    return None, ""


def _profile_total_bitrate_estimate(
    values: dict[str, Any],
    ffv1_reference_bitrate: int | None = None,
) -> tuple[int | None, str]:
    video_codec = str(values.get("video_codec", "")).strip().lower()
    audio_codec = str(values.get("audio_codec", "")).strip().lower()

    width = 720
    height = 576
    size_match = re.match(r"^(\d+)x(\d+)$", str(values.get("video_size", "")).strip())
    if size_match:
        width = int(size_match.group(1))
        height = int(size_match.group(2))

    pixels = max(1, width * height)
    pal_pixels = 720 * 576
    scale = pixels / pal_pixels

    video_bitrate = _parse_bitrate_text(str(values.get("video_bitrate", "")))
    note = ""

    if video_bitrate is None:
        if video_codec == "ffv1":
            if ffv1_reference_bitrate:
                video_bitrate = int(ffv1_reference_bitrate * scale)
                note = _("estimated from local FFV1 sample")
            else:
                video_bitrate = int(45_000_000 * scale)
                note = _("content-dependent FFV1 estimate")
        elif video_codec == "mjpeg":
            video_bitrate = int(20_000_000 * scale)
            note = _("typical MJPEG estimate")
        elif video_codec in {"libx264", "h264"}:
            video_bitrate = int(6_000_000 * scale)
            note = _("typical H.264 estimate")
        else:
            video_bitrate = None

    audio_bitrate = None
    if audio_codec == "pcm_s16le":
        sample_rate = int(values.get("sample_rate", 48000) or 48000)
        channels = int(values.get("channels", "2") or 2)
        audio_bitrate = sample_rate * channels * 16
    elif audio_codec in {"aac", "libmp3lame", "mp3", "ac3", "opus", "vorbis"}:
        audio_bitrate = _parse_bitrate_text(str(values.get("audio_bitrate", "")))
        if audio_bitrate is None:
            audio_bitrate = 192_000
    elif audio_codec in {"none", ""}:
        audio_bitrate = 0

    if video_bitrate is None and audio_bitrate is None:
        return None, note

    total = int((video_bitrate or 0) + (audio_bitrate or 0))
    return total if total > 0 else None, note


def _parse_v4l2_devices(text: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current_name = ""

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            continue

        if not line.startswith((" ", "\t")) and stripped.endswith(":"):
            current_name = stripped[:-1]
            continue

        if stripped.startswith("/dev/video"):
            devices.append({"name": current_name or stripped, "path": stripped})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in devices:
        path = item["path"]
        if path in seen:
            continue
        deduped.append(item)
        seen.add(path)

    return deduped


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _video_device_usb_ids(video_device: str) -> tuple[str, str] | None:
    if not video_device.startswith("/dev/video"):
        return None

    node = Path(video_device).name
    sys_video = Path("/sys/class/video4linux") / node
    sys_device = sys_video / "device"
    if not sys_device.exists():
        return None

    try:
        resolved = sys_device.resolve()
    except OSError:
        return None

    for path in (resolved, *resolved.parents):
        vendor = _read_text_file(path / "idVendor").lower()
        product = _read_text_file(path / "idProduct").lower()
        if vendor and product:
            return vendor, product

    return None


def _em28xx_card_parameter_values() -> list[int]:
    raw = _read_text_file(Path("/sys/module/em28xx/parameters/card"))
    if not raw:
        return []

    values: list[int] = []
    for token in re.findall(r"-?\d+", raw):
        try:
            values.append(int(token))
        except ValueError:
            continue
    return values


def _magix_em28xx_hint(video_device: str) -> str:
    usb_ids = _video_device_usb_ids(video_device)
    if usb_ids != (MAGIX_USB_VENDOR_ID, MAGIX_USB_PRODUCT_ID):
        return ""

    card_values = _em28xx_card_parameter_values()
    if 105 in card_values:
        return ""

    if not card_values:
        return _(
            "Magix USB Videowandler detected. If S-Video is missing or black, set em28xx option card=105."
        )

    return _(
        "Magix USB Videowandler detected. em28xx card=105 is not active, S-Video may fail. See README for permanent fix."
    )


def list_video_devices() -> list[dict[str, str]]:
    rc, out, _ = _run_command(["v4l2-ctl", "--list-devices"], timeout=8)
    if rc == 0 and out:
        parsed = _parse_v4l2_devices(out)
        if parsed:
            return parsed

    return [{"name": dev.name, "path": str(dev)} for dev in sorted(Path("/dev").glob("video*"))]


def list_video_inputs(device: str) -> list[dict[str, str]]:
    rc, out, _ = _run_command(["v4l2-ctl", "-d", device, "--list-inputs"], timeout=8)
    if rc != 0 or not out:
        return []

    inputs: list[dict[str, str]] = []
    current_id: str | None = None

    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue

        single_line = re.match(r"^(\d+)\s*:\s*(.+)$", line)
        if single_line:
            idx, name = single_line.groups()
            inputs.append({"id": idx, "name": name.strip()})
            current_id = None
            continue

        input_match = re.search(r"Input\s*:?\s*(\d+)", line)
        if input_match:
            current_id = input_match.group(1)
            continue

        name_match = re.search(r"Name\s*:?\s*(.+)$", line)
        if name_match and current_id is not None:
            inputs.append({"id": current_id, "name": name_match.group(1).strip()})
            current_id = None

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in inputs:
        idx = item["id"]
        if idx in seen:
            continue
        deduped.append(item)
        seen.add(idx)

    return deduped


def list_audio_sources() -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    rc, out, _ = _run_command(["pactl", "list", "short", "sources"], timeout=8)
    if rc == 0 and out:
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            source_id = parts[1]
            if ".monitor" in source_id:
                continue
            if source_id in seen_ids:
                continue
            state = parts[4] if len(parts) > 4 else ""
            display = f"{source_id} [Pulse] ({state})" if state else f"{source_id} [Pulse]"
            sources.append(
                {
                    "id": source_id,
                    "display": display,
                    "backend": "pulse",
                }
            )
            seen_ids.add(source_id)

    rc, out, _ = _run_command(["arecord", "-l"], timeout=8)
    if rc == 0 and out:
        card_re = re.compile(
            r"card\s+(\d+):\s+([^\s\[]+)(?:\s+\[([^\]]+)\])?,\s+device\s+(\d+):\s+([^\[]+)"
        )
        for line in out.splitlines():
            match = card_re.search(line)
            if not match:
                continue
            card, card_token, card_label, dev, dev_name = match.groups()
            source_id = f"plughw:CARD={card_token},DEV={dev}"
            if source_id in seen_ids:
                continue
            display_name = (card_label or card_token).strip()
            display = f"{source_id} [ALSA] ({display_name} - {dev_name.strip()}, hw:{card},{dev})"
            sources.append({"id": source_id, "display": display, "backend": "alsa"})
            seen_ids.add(source_id)

    return sources


def list_video_formats(device: str) -> list[dict[str, Any]]:
    rc, out, _ = _run_command(["v4l2-ctl", "-d", device, "--list-formats-ext"], timeout=8)
    if rc != 0 or not out:
        return []

    formats: list[dict[str, Any]] = []
    current_fmt: dict[str, Any] | None = None

    fmt_re = re.compile(r"\[(\d+)\]:\s+'([^']+)'\s+\((.+)\)")
    size_re = re.compile(r"Size:\s+Discrete\s+(\d+x\d+)")
    fps_re = re.compile(r"\((\d+(?:\.\d+)?)\s+fps\)")

    current_size: dict[str, Any] | None = None

    for raw in out.splitlines():
        line = raw.strip()
        fmt_match = fmt_re.search(line)
        if fmt_match:
            _, fourcc, label = fmt_match.groups()
            current_fmt = {"fourcc": fourcc, "label": label, "sizes": []}
            formats.append(current_fmt)
            current_size = None
            continue

        if current_fmt is None:
            continue

        size_match = size_re.search(line)
        if size_match:
            current_size = {"value": size_match.group(1), "fps": []}
            current_fmt["sizes"].append(current_size)
            continue

        if current_size is None:
            continue

        fps_match = fps_re.search(line)
        if fps_match:
            try:
                current_size["fps"].append(float(fps_match.group(1)))
            except ValueError:
                pass

    return formats


def _shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _parse_size(size: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)x(\d+)$", size)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _normalize_input_format(value: str) -> str:
    fmt = value.strip()
    if not fmt:
        return ""

    upper = fmt.upper()
    mapped = V4L2_TO_FFMPEG_INPUT_FORMAT.get(upper)
    if mapped:
        return mapped

    # For unknown FOURCC-like values, ffmpeg usually expects lowercase.
    if re.fullmatch(r"[A-Z0-9]{4}", upper):
        return upper.lower()

    return fmt


def _normalize_gst_raw_format(value: str) -> str:
    fmt = value.strip()
    if not fmt:
        return ""

    upper = fmt.upper()
    mapped = V4L2_TO_GST_RAW_FORMAT.get(upper)
    if mapped:
        return mapped

    return ""


def _prepare_v4l2_audio_capture(video_device: str) -> list[str]:
    warnings: list[str] = []
    if not video_device:
        return warnings

    steps = [
        (["v4l2-ctl", "-d", video_device, "--set-audio-input=0"], _("Could not set V4L2 audio input: ")),
        (["v4l2-ctl", "-d", video_device, "--set-ctrl", "mute=0"], _("Could not clear V4L2 mute: ")),
    ]
    for args, prefix in steps:
        rc, _stdout, err = _run_command(args, timeout=4.0)
        if rc == 0:
            continue

        low = err.lower()
        if "unknown control" in low or "inappropriate ioctl" in low or "invalid argument" in low:
            continue

        warnings.append(prefix + (err or "unknown error"))

    return warnings


def _prepare_v4l2_video_input(video_device: str, input_id: str) -> list[str]:
    warnings: list[str] = []
    if not video_device or not input_id:
        return warnings

    rc, _stdout, err = _run_command(
        ["v4l2-ctl", "-d", video_device, f"--set-input={input_id}"],
        timeout=4.0,
    )
    if rc == 0:
        return warnings

    low = err.lower()
    if "inappropriate ioctl" in low or "unknown" in low:
        warnings.append(_("Could not set video input on this device."))
        return warnings

    warnings.append(_("Could not set video input: ") + (err or "unknown error"))
    return warnings


class CapturePage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._ffmpeg_command: list[str] | None = None
        self._encoders: list[EncoderInfo] = []
        self._pixel_formats: list[PixelFormat] = []
        self._hardware_info: Any = None
        self._codec_description: dict[str, str] = {}
        self._ffv1_reference_bitrate, self._ffv1_reference_path = _detect_ffv1_reference_bitrate()

        self._audio_source_backends: dict[str, str] = {}

        self._preview_pipeline = None
        self._preview_bus = None
        self._preview_bus_handler_id: int | None = None
        self._preview_volume_element = None
        self._preview_running = False
        self._preview_include_audio = False
        self._preview_audio_watchdog_id: int | None = None
        self._preview_audio_last_level_monotonic = 0.0
        self._preview_audio_restart_count = 0
        self._preview_pending_watchdog_action = False
        self._preview_audio_fallback_reason = ""
        self._preview_source_mode = "device"
        self._preview_source_uri = ""
        self._capture_audio_open_error_reported = False
        self._capture_xrun_reported = False
        self._capture_monitor_uri = ""

        self.capture_runner = FFmpegRunner(self._on_capture_output, self._on_capture_exit)

        self._build_ui()
        self._populate_profiles()
        self._set_default_output_path()

    def _build_ui(self) -> None:
        info_frame = Gtk.Frame(label=_("Capture Status"))
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_margin_top(8)
        info_box.set_margin_bottom(8)
        info_box.set_margin_start(8)
        info_box.set_margin_end(8)
        info_frame.set_child(info_box)
        self.append(info_frame)

        self.capture_info_label = Gtk.Label(label=_("Waiting for FFmpeg capability scan..."))
        self.capture_info_label.set_xalign(0)
        self.capture_info_label.add_css_class("dim-label")
        info_box.append(self.capture_info_label)

        io_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        io_row.set_hexpand(True)
        self.append(io_row)

        input_frame = Gtk.Frame(label=_("Input & Source"))
        input_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        input_box.set_margin_top(8)
        input_box.set_margin_bottom(8)
        input_box.set_margin_start(8)
        input_box.set_margin_end(8)
        input_frame.set_child(input_box)
        input_frame.set_hexpand(True)
        io_row.append(input_frame)

        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(profile_row)

        profile_label = Gtk.Label(label=_("Profile"))
        profile_label.set_size_request(120, -1)
        profile_label.set_xalign(0)
        profile_row.append(profile_label)

        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.set_hexpand(True)
        profile_row.append(self.profile_combo)

        apply_profile_button = Gtk.Button(label=_("Apply"))
        apply_profile_button.connect("clicked", self.on_apply_profile_clicked)
        profile_row.append(apply_profile_button)

        self.profile_info_label = Gtk.Label(label="")
        self.profile_info_label.set_xalign(0)
        self.profile_info_label.set_wrap(True)
        self.profile_info_label.add_css_class("dim-label")
        input_box.append(self.profile_info_label)

        video_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(video_row)

        video_label = Gtk.Label(label=_("Video device"))
        video_label.set_size_request(120, -1)
        video_label.set_xalign(0)
        video_row.append(video_label)

        self.video_device_combo = Gtk.ComboBoxText()
        self.video_device_combo.set_hexpand(True)
        self.video_device_combo.connect("changed", self.on_video_device_changed)
        video_row.append(self.video_device_combo)

        refresh_devices_button = Gtk.Button(label=_("Refresh"))
        refresh_devices_button.set_tooltip_text(_("Rescan video and audio devices"))
        refresh_devices_button.connect("clicked", self.on_refresh_devices_clicked)
        video_row.append(refresh_devices_button)

        source_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(source_row)

        source_label = Gtk.Label(label=_("Video input"))
        source_label.set_size_request(120, -1)
        source_label.set_xalign(0)
        source_row.append(source_label)

        self.video_input_combo = Gtk.ComboBoxText()
        self.video_input_combo.set_hexpand(True)
        self.video_input_combo.connect("changed", self.on_capture_settings_changed)
        source_row.append(self.video_input_combo)

        self.device_hint_label = Gtk.Label(label="")
        self.device_hint_label.set_xalign(0)
        self.device_hint_label.set_wrap(True)
        self.device_hint_label.add_css_class("dim-label")
        input_box.append(self.device_hint_label)

        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(audio_row)

        audio_label = Gtk.Label(label=_("Audio source"))
        audio_label.set_size_request(120, -1)
        audio_label.set_xalign(0)
        audio_row.append(audio_label)

        self.audio_source_combo = Gtk.ComboBoxText()
        self.audio_source_combo.set_hexpand(True)
        self.audio_source_combo.connect("changed", self.on_audio_source_changed)
        audio_row.append(self.audio_source_combo)

        self.audio_backend_combo = Gtk.ComboBoxText()
        self.audio_backend_combo.append("pulse", "Pulse/PipeWire")
        self.audio_backend_combo.append("alsa", "ALSA")
        self.audio_backend_combo.set_active_id("pulse")
        self.audio_backend_combo.connect("changed", self.on_capture_settings_changed)
        audio_row.append(self.audio_backend_combo)

        format_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(format_row)

        format_label = Gtk.Label(label=_("Input format"))
        format_label.set_size_request(120, -1)
        format_label.set_xalign(0)
        format_row.append(format_label)

        self.video_format_combo = Gtk.ComboBoxText()
        self.video_format_combo.set_hexpand(True)
        self.video_format_combo.connect("changed", self.on_capture_settings_changed)
        format_row.append(self.video_format_combo)

        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(size_row)

        size_label = Gtk.Label(label=_("Video size"))
        size_label.set_size_request(120, -1)
        size_label.set_xalign(0)
        size_row.append(size_label)

        self.video_size_combo = Gtk.ComboBoxText()
        self.video_size_combo.set_hexpand(True)
        self.video_size_combo.connect("changed", self.on_capture_settings_changed)
        size_row.append(self.video_size_combo)

        self.video_size_entry = Gtk.Entry()
        self.video_size_entry.set_placeholder_text(_("Custom, e.g. 720x576"))
        self.video_size_entry.set_hexpand(True)
        self.video_size_entry.connect("changed", self.on_capture_settings_changed)
        size_row.append(self.video_size_entry)

        standard_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(standard_row)

        standard_label = Gtk.Label(label=_("TV standard"))
        standard_label.set_size_request(120, -1)
        standard_label.set_xalign(0)
        standard_row.append(standard_label)

        self.video_standard_combo = Gtk.ComboBoxText()
        self.video_standard_combo.append("auto", _("Auto"))
        self.video_standard_combo.append("pal", "PAL")
        self.video_standard_combo.append("ntsc", "NTSC")
        self.video_standard_combo.append("secam", "SECAM")
        self.video_standard_combo.set_active_id("auto")
        self.video_standard_combo.connect("changed", self.on_capture_settings_changed)
        standard_row.append(self.video_standard_combo)

        self.use_libv4l2_check = Gtk.CheckButton(label=_("Use libv4l2"))
        self.use_libv4l2_check.set_active(True)
        self.use_libv4l2_check.connect("toggled", self.on_capture_settings_changed)
        input_box.append(self.use_libv4l2_check)

        preview_frame = Gtk.Frame(label=_("Live View & Live Audio"))
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_box.set_margin_top(8)
        preview_box.set_margin_bottom(8)
        preview_box.set_margin_start(8)
        preview_box.set_margin_end(8)
        preview_frame.set_child(preview_box)
        preview_frame.set_hexpand(True)
        io_row.append(preview_frame)

        self.preview_picture = Gtk.Picture()
        self.preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview_picture.set_size_request(420, 260)

        preview_picture_frame = Gtk.Frame()
        preview_picture_frame.set_child(self.preview_picture)
        preview_box.append(preview_picture_frame)

        monitor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.append(monitor_row)

        self.preview_start_button = Gtk.Button(label=_("Start live view"))
        self.preview_start_button.connect("clicked", self.on_preview_start_clicked)
        monitor_row.append(self.preview_start_button)

        self.preview_stop_button = Gtk.Button(label=_("Stop live view"))
        self.preview_stop_button.set_sensitive(False)
        self.preview_stop_button.connect("clicked", self.on_preview_stop_clicked)
        monitor_row.append(self.preview_stop_button)

        self.preview_mute_check = Gtk.CheckButton(label=_("Mute live audio"))
        self.preview_mute_check.set_active(False)
        self.preview_mute_check.connect("toggled", self.on_preview_audio_control_changed)
        monitor_row.append(self.preview_mute_check)

        vol_label = Gtk.Label(label=_("Volume"))
        monitor_row.append(vol_label)

        self.preview_volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.01)
        self.preview_volume_scale.set_value(1.0)
        self.preview_volume_scale.set_digits(2)
        self.preview_volume_scale.set_hexpand(True)
        self.preview_volume_scale.connect("value-changed", self.on_preview_audio_control_changed)
        monitor_row.append(self.preview_volume_scale)

        policy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.append(policy_row)

        policy_label = Gtk.Label(label=_("Live during capture"))
        policy_label.set_xalign(0)
        policy_row.append(policy_label)

        self.live_during_capture_combo = Gtk.ComboBoxText()
        for policy_id, policy_label_text in LIVE_DURING_CAPTURE_POLICIES:
            self.live_during_capture_combo.append(policy_id, policy_label_text)
        self.live_during_capture_combo.set_active_id("auto")
        policy_row.append(self.live_during_capture_combo)

        self.preview_level_bar = Gtk.ProgressBar()
        self.preview_level_bar.set_show_text(True)
        self.preview_level_bar.set_text(_("Audio level"))
        self.preview_level_bar.set_fraction(0.0)
        preview_box.append(self.preview_level_bar)

        self.preview_status_label = Gtk.Label(label="")
        self.preview_status_label.set_xalign(0)
        self.preview_status_label.add_css_class("dim-label")
        preview_box.append(self.preview_status_label)

        output_frame = Gtk.Frame(label=_("Capture Output"))
        output_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        output_box.set_margin_top(8)
        output_box.set_margin_bottom(8)
        output_box.set_margin_start(8)
        output_box.set_margin_end(8)
        output_frame.set_child(output_box)
        self.append(output_frame)

        output_path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(output_path_row)

        output_path_label = Gtk.Label(label=_("Output file"))
        output_path_label.set_size_request(140, -1)
        output_path_label.set_xalign(0)
        output_path_row.append(output_path_label)

        self.output_entry = Gtk.Entry()
        self.output_entry.set_hexpand(True)
        self.output_entry.connect("changed", self.on_capture_settings_changed)
        output_path_row.append(self.output_entry)

        output_browse = Gtk.Button(label=_("Choose"))
        output_browse.connect("clicked", self.on_choose_output_clicked)
        output_path_row.append(output_browse)

        codec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(codec_row)

        container_label = Gtk.Label(label=_("Container"))
        container_label.set_size_request(140, -1)
        container_label.set_xalign(0)
        codec_row.append(container_label)

        self.container_combo = Gtk.ComboBoxText()
        for cont in CONTAINERS:
            self.container_combo.append(cont, cont.upper())
        self.container_combo.set_active_id("mkv")
        self.container_combo.connect("changed", self.on_container_changed)
        codec_row.append(self.container_combo)

        self.show_all_capture_codecs = Gtk.CheckButton(label=_("Show unusable codecs"))
        self.show_all_capture_codecs.connect("toggled", self.on_capture_codec_filter_changed)
        codec_row.append(self.show_all_capture_codecs)

        video_codec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(video_codec_row)

        video_codec_label = Gtk.Label(label=_("Video codec"))
        video_codec_label.set_size_request(140, -1)
        video_codec_label.set_xalign(0)
        video_codec_row.append(video_codec_label)

        self.capture_video_codec_combo = Gtk.ComboBoxText()
        self.capture_video_codec_combo.set_hexpand(True)
        self.capture_video_codec_combo.connect("changed", self.on_capture_video_codec_changed)
        video_codec_row.append(self.capture_video_codec_combo)

        self.capture_video_bitrate_entry = Gtk.Entry()
        self.capture_video_bitrate_entry.set_placeholder_text(_("Bitrate, e.g. 6M"))
        self.capture_video_bitrate_entry.set_text("6M")
        self.capture_video_bitrate_entry.connect("changed", self.on_capture_settings_changed)
        video_codec_row.append(self.capture_video_bitrate_entry)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(preset_row)

        preset_label = Gtk.Label(label=_("Preset"))
        preset_label.set_size_request(140, -1)
        preset_label.set_xalign(0)
        preset_row.append(preset_label)

        self.capture_preset_combo = Gtk.ComboBoxText()
        self.capture_preset_combo.connect("changed", self.on_capture_settings_changed)
        preset_row.append(self.capture_preset_combo)

        self.capture_tune_entry = Gtk.Entry()
        self.capture_tune_entry.set_placeholder_text(_("Optional tune, e.g. film, grain"))
        self.capture_tune_entry.set_hexpand(True)
        self.capture_tune_entry.connect("changed", self.on_capture_settings_changed)
        preset_row.append(self.capture_tune_entry)

        audio_codec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(audio_codec_row)

        audio_codec_label = Gtk.Label(label=_("Audio codec"))
        audio_codec_label.set_size_request(140, -1)
        audio_codec_label.set_xalign(0)
        audio_codec_row.append(audio_codec_label)

        self.capture_audio_codec_combo = Gtk.ComboBoxText()
        self.capture_audio_codec_combo.set_hexpand(True)
        self.capture_audio_codec_combo.connect("changed", self.on_capture_settings_changed)
        audio_codec_row.append(self.capture_audio_codec_combo)

        self.capture_audio_bitrate_entry = Gtk.Entry()
        self.capture_audio_bitrate_entry.set_placeholder_text(_("Bitrate, e.g. 192k"))
        self.capture_audio_bitrate_entry.set_text("192k")
        self.capture_audio_bitrate_entry.connect("changed", self.on_capture_settings_changed)
        audio_codec_row.append(self.capture_audio_bitrate_entry)

        audio_meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(audio_meta_row)

        sample_label = Gtk.Label(label=_("Sample rate"))
        sample_label.set_size_request(140, -1)
        sample_label.set_xalign(0)
        audio_meta_row.append(sample_label)

        self.sample_rate_spin = Gtk.SpinButton.new_with_range(8000, 192000, 1000)
        self.sample_rate_spin.set_value(48000)
        self.sample_rate_spin.connect("value-changed", self.on_capture_settings_changed)
        audio_meta_row.append(self.sample_rate_spin)

        channels_label = Gtk.Label(label=_("Channels"))
        audio_meta_row.append(channels_label)

        self.channels_combo = Gtk.ComboBoxText()
        self.channels_combo.append("1", "1")
        self.channels_combo.append("2", "2")
        self.channels_combo.set_active_id("2")
        self.channels_combo.connect("changed", self.on_capture_settings_changed)
        audio_meta_row.append(self.channels_combo)

        audio_tune_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(audio_tune_row)

        audio_filter_label = Gtk.Label(label=_("Audio cleanup"))
        audio_filter_label.set_size_request(140, -1)
        audio_filter_label.set_xalign(0)
        audio_tune_row.append(audio_filter_label)

        self.audio_filter_combo = Gtk.ComboBoxText()
        for preset_id, preset_label in AUDIO_FILTER_PRESETS:
            self.audio_filter_combo.append(preset_id, preset_label)
        self.audio_filter_combo.set_active_id("off")
        self.audio_filter_combo.set_hexpand(True)
        self.audio_filter_combo.connect("changed", self.on_capture_settings_changed)
        audio_tune_row.append(self.audio_filter_combo)

        audio_gain_label = Gtk.Label(label=_("Gain (dB)"))
        audio_tune_row.append(audio_gain_label)

        self.audio_gain_spin = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.5)
        self.audio_gain_spin.set_value(0.0)
        self.audio_gain_spin.set_digits(1)
        self.audio_gain_spin.connect("value-changed", self.on_capture_settings_changed)
        audio_tune_row.append(self.audio_gain_spin)

        pixfps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(pixfps_row)

        pix_label = Gtk.Label(label=_("Pixel format"))
        pix_label.set_size_request(140, -1)
        pix_label.set_xalign(0)
        pixfps_row.append(pix_label)

        self.capture_pixfmt_combo = Gtk.ComboBoxText()
        self.capture_pixfmt_combo.set_hexpand(True)
        self.capture_pixfmt_combo.connect("changed", self.on_capture_settings_changed)
        pixfps_row.append(self.capture_pixfmt_combo)

        self.match_fps_check = Gtk.CheckButton(label=_("Match source FPS"))
        self.match_fps_check.set_active(True)
        self.match_fps_check.connect("toggled", self.on_match_fps_toggled)
        pixfps_row.append(self.match_fps_check)

        self.output_fps_spin = Gtk.SpinButton.new_with_range(1, 120, 0.01)
        self.output_fps_spin.set_value(25.0)
        self.output_fps_spin.set_digits(3)
        self.output_fps_spin.set_sensitive(False)
        self.output_fps_spin.connect("value-changed", self.on_capture_settings_changed)
        pixfps_row.append(self.output_fps_spin)

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(filter_row)

        self.deinterlace_check = Gtk.CheckButton(label=_("Deinterlace (yadif)"))
        self.deinterlace_check.set_active(False)
        self.deinterlace_check.connect("toggled", self.on_capture_settings_changed)
        filter_row.append(self.deinterlace_check)

        extra_label = Gtk.Label(label=_("Extra args"))
        filter_row.append(extra_label)

        self.capture_extra_entry = Gtk.Entry()
        self.capture_extra_entry.set_hexpand(True)
        self.capture_extra_entry.set_placeholder_text(_("Optional FFmpeg args"))
        self.capture_extra_entry.connect("changed", self.on_capture_settings_changed)
        filter_row.append(self.capture_extra_entry)

        cmd_frame = Gtk.Frame(label=_("Capture command preview"))
        cmd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cmd_box.set_margin_top(8)
        cmd_box.set_margin_bottom(8)
        cmd_box.set_margin_start(8)
        cmd_box.set_margin_end(8)
        cmd_frame.set_child(cmd_box)
        self.append(cmd_frame)

        self.capture_command_buffer = Gtk.TextBuffer()
        self.capture_command_view = Gtk.TextView(buffer=self.capture_command_buffer)
        self.capture_command_view.set_editable(False)
        self.capture_command_view.set_monospace(True)

        cmd_scroller = Gtk.ScrolledWindow()
        cmd_scroller.set_min_content_height(80)
        cmd_scroller.set_child(self.capture_command_view)
        cmd_box.append(cmd_scroller)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cmd_box.append(action_row)

        self.capture_start_button = Gtk.Button(label=_("Start capture"))
        self.capture_start_button.connect("clicked", self.on_capture_start_clicked)
        action_row.append(self.capture_start_button)

        self.capture_stop_button = Gtk.Button(label=_("Stop capture"))
        self.capture_stop_button.set_sensitive(False)
        self.capture_stop_button.connect("clicked", self.on_capture_stop_clicked)
        action_row.append(self.capture_stop_button)

        self.capture_status_label = Gtk.Label(label="")
        self.capture_status_label.set_xalign(0)
        cmd_box.append(self.capture_status_label)

        log_frame = Gtk.Frame(label=_("Capture log"))
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_box.set_margin_top(8)
        log_box.set_margin_bottom(8)
        log_box.set_margin_start(8)
        log_box.set_margin_end(8)
        log_frame.set_child(log_box)
        self.append(log_frame)

        self.capture_log_buffer = Gtk.TextBuffer()
        self.capture_log_view = Gtk.TextView(buffer=self.capture_log_buffer)
        self.capture_log_view.set_editable(False)
        self.capture_log_view.set_monospace(True)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_vexpand(True)
        log_scroller.set_min_content_height(180)
        log_scroller.set_child(self.capture_log_view)
        log_box.append(log_scroller)

    def sync_capabilities(
        self,
        ffmpeg_command: list[str] | None,
        encoders: list[EncoderInfo],
        pixel_formats: list[PixelFormat],
        hardware_info: Any,
    ) -> None:
        self._ffmpeg_command = ffmpeg_command
        self._encoders = encoders
        self._pixel_formats = pixel_formats
        self._hardware_info = hardware_info

        if ffmpeg_command:
            cmd_txt = " ".join(ffmpeg_command)
            self.capture_info_label.set_text(_("Capture uses FFmpeg command: ") + cmd_txt)
        else:
            self.capture_info_label.set_text(_("FFmpeg not available."))

        self._populate_capture_codec_combos()
        self._populate_capture_pixfmt_combo()
        self._populate_capture_preset_combo(self.capture_video_codec_combo.get_active_id())
        self.refresh_devices()
        self.update_capture_command_preview()

    def refresh_devices(self) -> None:
        current_video = self.video_device_combo.get_active_id() or ""
        current_audio = self.audio_source_combo.get_active_id() or ""

        video_devices = list_video_devices()
        self.video_device_combo.remove_all()
        for dev in video_devices:
            self.video_device_combo.append(dev["path"], f"{dev['name']} -> {dev['path']}")

        if video_devices:
            if current_video and self.video_device_combo.set_active_id(current_video):
                pass
            else:
                self.video_device_combo.set_active_id(video_devices[0]["path"])

        self._audio_source_backends.clear()
        audio_sources = list_audio_sources()

        self.audio_source_combo.remove_all()
        self.audio_source_combo.append("", _("None"))
        for src in audio_sources:
            src_id = src["id"]
            self.audio_source_combo.append(src_id, src["display"])
            self._audio_source_backends[src_id] = src.get("backend", "pulse")

        if current_audio and self.audio_source_combo.set_active_id(current_audio):
            backend = self._audio_source_backends.get(current_audio)
            if backend:
                self.audio_backend_combo.set_active_id(backend)
        else:
            self.audio_source_combo.set_active_id("")

        self._refresh_video_input_options()
        self._refresh_video_format_options()
        self._update_device_hints()

    def _populate_profiles(self) -> None:
        self.profile_combo.remove_all()
        self.profile_combo.append("", _("Manual"))
        for profile in VHS_PROFILES:
            self.profile_combo.append(profile["id"], profile["name"])
        self.profile_combo.set_active_id("")
        self.profile_info_label.set_text("")

    def _profile_info_text(self, profile: dict[str, Any]) -> str:
        description = str(profile.get("description", "")).strip()
        values = profile.get("values", {})
        total_bitrate, note = _profile_total_bitrate_estimate(values, self._ffv1_reference_bitrate)

        estimate = ""
        if total_bitrate:
            estimate = _("Estimated size: ") + _format_rate_per_hour(total_bitrate)
            if note:
                estimate += f" ({note})"
        elif note:
            estimate = _("Estimated size: unknown") + f" ({note})"

        if description and estimate:
            return description + "\n" + estimate
        if estimate:
            return estimate
        return description

    def on_apply_profile_clicked(self, _button: Gtk.Button) -> None:
        profile_id = self.profile_combo.get_active_id()
        if not profile_id:
            self.profile_info_label.set_text(_("Manual mode active."))
            return

        profile = next((x for x in VHS_PROFILES if x["id"] == profile_id), None)
        if not profile:
            return

        values = profile.get("values", {})

        container = values.get("container")
        if container and self.container_combo.get_model() is not None:
            self.container_combo.set_active_id(container)

        vcodec = values.get("video_codec")
        if vcodec:
            self.capture_video_codec_combo.set_active_id(vcodec)

        self._populate_capture_preset_combo(self.capture_video_codec_combo.get_active_id())

        preset = values.get("video_preset")
        if preset:
            self.capture_preset_combo.set_active_id(preset)

        acodec = values.get("audio_codec")
        if acodec:
            self.capture_audio_codec_combo.set_active_id(acodec)

        pix = values.get("pixel_format")
        if pix:
            self.capture_pixfmt_combo.set_active_id(pix)

        self.capture_video_bitrate_entry.set_text(values.get("video_bitrate", ""))
        self.capture_audio_bitrate_entry.set_text(values.get("audio_bitrate", ""))
        self.capture_tune_entry.set_text(values.get("video_tune", ""))
        self.audio_filter_combo.set_active_id(values.get("audio_filter_preset", "off"))
        self.audio_gain_spin.set_value(float(values.get("audio_gain_db", 0.0)))

        self.sample_rate_spin.set_value(float(values.get("sample_rate", 48000)))
        self.channels_combo.set_active_id(values.get("channels", "2"))

        self.match_fps_check.set_active(bool(values.get("match_source_fps", True)))
        self.output_fps_spin.set_value(float(values.get("output_fps", 25.0)))
        self.output_fps_spin.set_sensitive(not self.match_fps_check.get_active())

        self.deinterlace_check.set_active(bool(values.get("deinterlace", False)))

        standard = values.get("video_standard")
        if standard:
            self.video_standard_combo.set_active_id(standard)

        self.use_libv4l2_check.set_active(bool(values.get("use_libv4l2", True)))

        size = values.get("video_size", "")
        if size and self.video_size_combo.get_model() is not None:
            if self.video_size_combo.set_active_id(size):
                self.video_size_entry.set_text("")
            else:
                self.video_size_entry.set_text(size)

        input_format = values.get("video_input_format", "")
        if input_format:
            self.video_format_combo.set_active_id(input_format)

        video_source_input = values.get("video_source_input")
        if video_source_input is not None:
            self.video_input_combo.set_active_id(str(video_source_input))

        self.profile_info_label.set_text(self._profile_info_text(profile))
        self.update_capture_command_preview()

    def _populate_capture_codec_combos(self) -> None:
        selected_video = self.capture_video_codec_combo.get_active_id()
        selected_audio = self.capture_audio_codec_combo.get_active_id()

        show_all = self.show_all_capture_codecs.get_active()

        video_encoders = [enc for enc in self._encoders if enc.kind == "video"]
        audio_encoders = [enc for enc in self._encoders if enc.kind == "audio"]

        if not show_all:
            video_encoders = [enc for enc in video_encoders if self._encoder_is_usable(enc.name)]

        self.capture_video_codec_combo.remove_all()
        self.capture_video_codec_combo.append("copy", "copy")

        self._codec_description = {}

        seen_video: set[str] = set()
        for enc in sorted(video_encoders, key=lambda x: x.name.casefold()):
            if enc.name in seen_video:
                continue
            seen_video.add(enc.name)
            label = enc.name
            if enc.description:
                label = f"{enc.name} - {enc.description}"
            self.capture_video_codec_combo.append(enc.name, label)
            self._codec_description[enc.name] = enc.description

        if selected_video and self.capture_video_codec_combo.set_active_id(selected_video):
            pass
        elif self.capture_video_codec_combo.set_active_id("libx264"):
            pass
        else:
            self.capture_video_codec_combo.set_active(0)

        self.capture_audio_codec_combo.remove_all()
        self.capture_audio_codec_combo.append("none", _("none"))
        self.capture_audio_codec_combo.append("copy", "copy")

        seen_audio: set[str] = set()
        for enc in sorted(audio_encoders, key=lambda x: x.name.casefold()):
            if enc.name in seen_audio:
                continue
            seen_audio.add(enc.name)
            label = enc.name
            if enc.description:
                label = f"{enc.name} - {enc.description}"
            self.capture_audio_codec_combo.append(enc.name, label)

        if selected_audio and self.capture_audio_codec_combo.set_active_id(selected_audio):
            pass
        elif self.capture_audio_codec_combo.set_active_id("aac"):
            pass
        else:
            self.capture_audio_codec_combo.set_active(0)

    def _populate_capture_pixfmt_combo(self) -> None:
        selected = self.capture_pixfmt_combo.get_active_id()

        self.capture_pixfmt_combo.remove_all()
        self.capture_pixfmt_combo.append("auto", _("auto"))

        names = sorted({fmt.name for fmt in self._pixel_formats}, key=str.casefold)
        for name in names:
            self.capture_pixfmt_combo.append(name, name)

        if selected and self.capture_pixfmt_combo.set_active_id(selected):
            return
        if self.capture_pixfmt_combo.set_active_id("yuv420p"):
            return
        self.capture_pixfmt_combo.set_active_id("auto")

    def _populate_capture_preset_combo(self, codec: str | None) -> None:
        selected = self.capture_preset_combo.get_active_id()
        self.capture_preset_combo.remove_all()

        self.capture_preset_combo.append("auto", _("auto"))

        presets = PRESETS_X264
        if codec and "nvenc" in codec.lower():
            presets = PRESETS_NVENC

        for preset in presets:
            self.capture_preset_combo.append(preset, preset)

        if selected and self.capture_preset_combo.set_active_id(selected):
            return
        if codec and "nvenc" in codec.lower() and self.capture_preset_combo.set_active_id("p4"):
            return
        if self.capture_preset_combo.set_active_id("veryfast"):
            return
        self.capture_preset_combo.set_active_id("auto")

    def _encoder_requirement(self, name: str) -> set[str] | None:
        lower = name.lower()
        if "_nvenc" in lower:
            return {"nvidia"}
        if "_qsv" in lower:
            return {"intel"}
        if "_amf" in lower:
            return {"amd"}
        if "_vaapi" in lower:
            return {"intel", "amd"}
        if "_vdpau" in lower:
            return {"nvidia", "amd"}
        if "_vulkan" in lower or "_opencl" in lower:
            return {"any"}
        return None

    def _encoder_is_usable(self, name: str) -> bool:
        if self._hardware_info is None or not getattr(self._hardware_info, "known", False):
            return True

        req = self._encoder_requirement(name)
        if req is None:
            return True

        vendors = getattr(self._hardware_info, "vendors", set())
        if "any" in req:
            return bool(vendors)
        return bool(vendors.intersection(req))

    def on_capture_codec_filter_changed(self, _button: Gtk.CheckButton) -> None:
        self._populate_capture_codec_combos()
        self._populate_capture_preset_combo(self.capture_video_codec_combo.get_active_id())
        self.update_capture_command_preview()

    def on_capture_video_codec_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._populate_capture_preset_combo(self.capture_video_codec_combo.get_active_id())
        self.update_capture_command_preview()

    def on_refresh_devices_clicked(self, _button: Gtk.Button) -> None:
        self.refresh_devices()
        self.capture_status_label.set_text(_("Devices refreshed."))

    def on_video_device_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._refresh_video_input_options()
        self._refresh_video_format_options()
        self._update_device_hints()
        self.update_capture_command_preview()

    def on_audio_source_changed(self, _combo: Gtk.ComboBoxText) -> None:
        src = self.audio_source_combo.get_active_id() or ""
        backend = self._audio_source_backends.get(src)
        if backend:
            self.audio_backend_combo.set_active_id(backend)
        self.update_capture_command_preview()

    def _refresh_video_format_options(self) -> None:
        device = self.video_device_combo.get_active_id() or ""

        self.video_format_combo.remove_all()
        self.video_size_combo.remove_all()

        self.video_format_combo.append("", _("auto"))
        self.video_size_combo.append("", _("auto"))

        if not device:
            self.video_format_combo.set_active_id("")
            self.video_size_combo.set_active_id("")
            return

        formats = list_video_formats(device)

        size_values: set[str] = set()
        for fmt in formats:
            fourcc = fmt.get("fourcc") or ""
            label = fmt.get("label") or ""
            if fourcc:
                self.video_format_combo.append(fourcc, f"{fourcc} ({label})")
            for size in fmt.get("sizes", []):
                value = size.get("value")
                if value:
                    size_values.add(value)

        for size in sorted(size_values):
            self.video_size_combo.append(size, size)

        self.video_format_combo.set_active_id("")
        self.video_size_combo.set_active_id("720x576" if "720x576" in size_values else "")

    def _refresh_video_input_options(self) -> None:
        device = self.video_device_combo.get_active_id() or ""
        current = self.video_input_combo.get_active_id() or ""

        self.video_input_combo.remove_all()
        self.video_input_combo.append("", _("auto"))

        if not device:
            self.video_input_combo.set_active_id("")
            return

        inputs = list_video_inputs(device)
        for item in inputs:
            idx = item.get("id", "")
            name = item.get("name", "")
            if idx:
                label = f"{idx} - {name}" if name else idx
                self.video_input_combo.append(idx, label)

        if current and self.video_input_combo.set_active_id(current):
            return
        if self.video_input_combo.set_active_id("0"):
            return
        if inputs:
            self.video_input_combo.set_active_id(inputs[0].get("id", ""))
            return
        self.video_input_combo.set_active_id("")

    def _update_device_hints(self) -> None:
        video_device = self.video_device_combo.get_active_id() or ""
        hint = _magix_em28xx_hint(video_device)
        self.device_hint_label.set_text(hint)

    def on_match_fps_toggled(self, _button: Gtk.CheckButton) -> None:
        self.output_fps_spin.set_sensitive(not self.match_fps_check.get_active())
        self.update_capture_command_preview()

    def on_container_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._ensure_output_extension()
        self.update_capture_command_preview()

    def on_capture_settings_changed(self, _widget) -> None:
        self.update_capture_command_preview()

    def _set_default_output_path(self) -> None:
        videos_dir = os.path.join(os.path.expanduser("~"), "Videos")
        if not os.path.isdir(videos_dir):
            videos_dir = os.getcwd()

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        default_path = os.path.join(videos_dir, f"capture-{timestamp}.mkv")
        self.output_entry.set_text(default_path)

    def _ensure_output_extension(self) -> None:
        path = self.output_entry.get_text().strip()
        if not path:
            return

        container = self.container_combo.get_active_id() or "mkv"
        root, ext = os.path.splitext(path)
        if ext.lower() == f".{container}":
            return
        if ext:
            path = root
        self.output_entry.set_text(path + f".{container}")

    def on_choose_output_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select capture output file"))
        dialog.set_modal(True)
        parent = self.get_root()
        if not isinstance(parent, Gtk.Window):
            parent = None
        dialog.save(parent, None, self._on_output_selected)

    def _on_output_selected(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return

        if file is None:
            return

        path = file.get_path()
        if path:
            self.output_entry.set_text(path)
            self._ensure_output_extension()
            self.update_capture_command_preview()

    def _selected_video_size(self) -> str:
        custom = self.video_size_entry.get_text().strip()
        if custom:
            return custom
        return self.video_size_combo.get_active_id() or ""

    def _resolve_container_compatibility(
        self,
        container: str,
        video_codec: str,
        audio_codec: str,
        has_audio: bool,
        auto_fix: bool,
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []

        if container != "mp4":
            return container, warnings

        ffv1_in_mp4 = video_codec == "ffv1"
        pcm_in_mp4 = has_audio and audio_codec.startswith("pcm_")
        if not ffv1_in_mp4 and not pcm_in_mp4:
            return container, warnings

        if auto_fix:
            container = "mkv"
            self.container_combo.set_active_id(container)
            self._ensure_output_extension()
            warnings.append(
                _("Container auto-switched to MKV because MP4 does not support this codec combination.")
            )
            return container, warnings

        if ffv1_in_mp4:
            warnings.append(_("FFV1 is not supported in MP4."))
        if pcm_in_mp4:
            warnings.append(_("PCM audio is not supported in MP4."))
        warnings.append(_("Use MKV (recommended) or MOV for this capture profile."))
        return container, warnings

    def _build_capture_command(
        self,
        auto_fix_compatibility: bool = False,
        monitor_udp_port: int | None = None,
    ) -> tuple[list[str], list[str]]:
        warnings: list[str] = []

        if not self._ffmpeg_command:
            raise RuntimeError(_("FFmpeg is not available."))

        video_device = self.video_device_combo.get_active_id() or ""
        if not video_device:
            raise RuntimeError(_("Choose a video device."))

        output_path = self.output_entry.get_text().strip()
        if not output_path:
            raise RuntimeError(_("Choose an output file."))

        self._ensure_output_extension()
        output_path = self.output_entry.get_text().strip()

        cmd = list(self._ffmpeg_command)
        cmd += ["-hide_banner", "-y"]

        cmd += ["-thread_queue_size", "8192", "-f", "v4l2"]

        if self.use_libv4l2_check.get_active():
            cmd += ["-use_libv4l2", "1"]

        input_format_ui = self.video_format_combo.get_active_id() or ""
        input_format = _normalize_input_format(input_format_ui)
        if input_format:
            cmd += ["-input_format", input_format]

        standard = self.video_standard_combo.get_active_id() or "auto"
        if standard != "auto":
            cmd += ["-standard", standard]

        video_size = self._selected_video_size()
        if video_size:
            cmd += ["-video_size", video_size]

        cmd += ["-i", video_device]

        audio_source = self.audio_source_combo.get_active_id() or ""
        audio_codec = self.capture_audio_codec_combo.get_active_id() or "none"
        has_audio = bool(audio_source and audio_codec != "none")
        video_codec = self.capture_video_codec_combo.get_active_id() or "libx264"

        container = self.container_combo.get_active_id() or "mkv"
        container, compat_warnings = self._resolve_container_compatibility(
            container=container,
            video_codec=video_codec,
            audio_codec=audio_codec,
            has_audio=has_audio,
            auto_fix=auto_fix_compatibility,
        )
        warnings.extend(compat_warnings)
        if auto_fix_compatibility:
            output_path = self.output_entry.get_text().strip()

        if has_audio:
            backend = self.audio_backend_combo.get_active_id() or "pulse"
            cmd += ["-thread_queue_size", "8192", "-f", backend, "-i", audio_source]

        cmd += ["-map", "0:v:0"]
        if has_audio:
            cmd += ["-map", "1:a:0"]

        if not self.match_fps_check.get_active():
            out_fps = self.output_fps_spin.get_value()
            cmd += ["-r", f"{out_fps:g}"]

        vf_filters: list[str] = []
        if self.deinterlace_check.get_active():
            vf_filters.append("yadif=0:-1:0")

        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        cmd += ["-c:v", video_codec]

        video_bitrate = self.capture_video_bitrate_entry.get_text().strip()
        if video_bitrate and video_codec not in {"copy", "mjpeg", "ffv1"}:
            cmd += ["-b:v", video_bitrate]

        preset = self.capture_preset_combo.get_active_id() or "auto"
        if preset not in {"auto", ""}:
            cmd += ["-preset", preset]

        tune = self.capture_tune_entry.get_text().strip()
        if tune:
            cmd += ["-tune", tune]

        pixfmt = self.capture_pixfmt_combo.get_active_id() or "auto"
        if pixfmt != "auto" and video_codec != "copy":
            cmd += ["-pix_fmt", pixfmt]

        if has_audio:
            cmd += ["-c:a", audio_codec]
            cmd += ["-ar", str(int(self.sample_rate_spin.get_value()))]
            cmd += ["-ac", self.channels_combo.get_active_id() or "2"]

            audio_bitrate = self.capture_audio_bitrate_entry.get_text().strip()
            if audio_bitrate and audio_codec not in {"copy", "pcm_s16le", "flac"}:
                cmd += ["-b:a", audio_bitrate]

            af_filters: list[str] = []
            audio_preset = self.audio_filter_combo.get_active_id() or "off"
            if audio_preset == "cleanup_mild":
                af_filters += ["highpass=f=80", "lowpass=f=12000", "afftdn=nf=-25"]
            elif audio_preset == "hum50":
                af_filters += ["equalizer=f=50:t=q:w=1:g=-14", "equalizer=f=100:t=q:w=1:g=-10"]
            elif audio_preset == "hum50_cleanup":
                af_filters += [
                    "equalizer=f=50:t=q:w=1:g=-14",
                    "equalizer=f=100:t=q:w=1:g=-10",
                    "highpass=f=80",
                    "lowpass=f=12000",
                    "afftdn=nf=-25",
                ]
            elif audio_preset == "hum60":
                af_filters += ["equalizer=f=60:t=q:w=1:g=-14", "equalizer=f=120:t=q:w=1:g=-10"]
            elif audio_preset == "hum60_cleanup":
                af_filters += [
                    "equalizer=f=60:t=q:w=1:g=-14",
                    "equalizer=f=120:t=q:w=1:g=-10",
                    "highpass=f=80",
                    "lowpass=f=12000",
                    "afftdn=nf=-25",
                ]

            gain_db = float(self.audio_gain_spin.get_value())
            if abs(gain_db) >= 0.01:
                af_filters.append(f"volume={gain_db:+.1f}dB")

            if af_filters:
                af_filters.append("aresample=async=1:first_pts=0")
                cmd += ["-af", ",".join(af_filters)]
        else:
            cmd += ["-an"]

        extra = self.capture_extra_entry.get_text().strip()
        if extra:
            if re.search(r"(^|\\s)-af(\\s|$)", extra):
                warnings.append(_("Extra args contain -af and may override audio cleanup settings."))
            try:
                cmd += shlex.split(extra)
            except ValueError as exc:
                warnings.append(_("Could not parse extra args: ") + str(exc))

        cmd += [output_path]

        if monitor_udp_port is not None:
            warnings.append(_("Live monitor stream enabled (single-device mode)."))
            cmd += ["-map", "0:v:0"]
            if has_audio:
                cmd += ["-map", "1:a:0"]

            cmd += [
                "-vf",
                "scale=640:480:flags=fast_bilinear,format=yuv420p",
                "-r",
                "25",
                "-c:v",
                "mpeg2video",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                "2M",
                "-g",
                "25",
                "-bf",
                "0",
            ]

            if has_audio:
                cmd += ["-c:a", "mp2", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
            else:
                cmd += ["-an"]

            cmd += [
                "-f",
                "mpegts",
                f"udp://127.0.0.1:{monitor_udp_port}?pkt_size=1316&overrun_nonfatal=1&fifo_size=1000000",
            ]

        return cmd, warnings

    def update_capture_command_preview(self) -> None:
        try:
            cmd, warnings = self._build_capture_command()
        except RuntimeError as exc:
            self.capture_command_buffer.set_text(str(exc))
            return

        self.capture_command_buffer.set_text(_shell_preview(cmd))
        if warnings:
            self.capture_status_label.set_text("\n".join(warnings))
        elif not self.capture_runner.running:
            self.capture_status_label.set_text("")

    def on_capture_start_clicked(self, _button: Gtk.Button) -> None:
        if self.capture_runner.running:
            return

        preview_was_running = self._preview_running
        live_policy = self._live_during_capture_policy()
        # Keep single-device monitor only when preview is active at capture start.
        # If preview is off, avoid paying the extra encode cost.
        monitor_stream_enabled = preview_was_running and live_policy in {"keep", "auto"}

        if preview_was_running:
            self.stop_preview()

        video_device = self.video_device_combo.get_active_id() or ""
        video_input_id = self.video_input_combo.get_active_id() or ""
        for warning in _prepare_v4l2_video_input(video_device, video_input_id):
            self._append_capture_status_warning(warning)

        for warning in _prepare_v4l2_audio_capture(video_device):
            self._append_capture_status_warning(warning)

        monitor_port: int | None = None
        monitor_uri = ""
        if monitor_stream_enabled:
            monitor_port = _pick_free_udp_port()
            monitor_uri = f"udp://127.0.0.1:{monitor_port}"

        try:
            cmd, warnings = self._build_capture_command(
                auto_fix_compatibility=True,
                monitor_udp_port=monitor_port,
            )
        except RuntimeError as exc:
            self.capture_status_label.set_text(str(exc))
            return

        self._clear_capture_log()
        self._capture_audio_open_error_reported = False
        self._capture_xrun_reported = False
        self._append_capture_log(f"[capture] Live policy: {live_policy}")
        audio_source = self.audio_source_combo.get_active_id() or ""
        if video_input_id:
            self._append_capture_log(f"[capture] Video input: {video_input_id}")
        if audio_source:
            backend = self.audio_backend_combo.get_active_id() or "pulse"
            self._append_capture_log(f"[capture] Audio input: {backend}:{audio_source}")
        if monitor_uri:
            self._append_capture_log(f"[capture] Live monitor stream: {monitor_uri}")
        self._append_capture_log(_("Running:") + " " + _shell_preview(cmd))
        if warnings:
            self.capture_status_label.set_text("\n".join(warnings))
        if preview_was_running and live_policy == "stop":
            self._append_capture_status_warning(_("Live view was stopped automatically during capture."))
        if monitor_stream_enabled and preview_was_running:
            self._append_capture_status_warning(
                _("Live view switched to capture monitor stream (single-device mode).")
            )
        if monitor_stream_enabled and live_policy == "auto":
            self._append_capture_status_warning(_("Auto-fallback enabled for live preview during capture."))

        try:
            self.capture_runner.start(cmd)
        except Exception as exc:
            self.capture_status_label.set_text(str(exc))
            return

        self._capture_monitor_uri = monitor_uri
        if preview_was_running and monitor_uri:
            self.start_preview_from_uri(monitor_uri, with_audio=bool(audio_source), preserve_watchdog_state=True)
            if not self._preview_running:
                self._append_capture_status_warning(_("Capture is running, but live monitor could not be started."))

        self.capture_start_button.set_sensitive(False)
        self.capture_stop_button.set_sensitive(True)

    def on_capture_stop_clicked(self, _button: Gtk.Button) -> None:
        self.capture_runner.stop()

    def _on_capture_output(self, line: str) -> None:
        GLib.idle_add(self._append_capture_log, line)
        GLib.idle_add(self._capture_status_hint_from_line, line)

    def _capture_status_hint_from_line(self, line: str) -> bool:
        low = line.lower()

        if (
            not self._capture_audio_open_error_reported
            and (
                "cannot open audio device" in low
                or "error opening input file" in low
                or "no such device" in low
            )
            and ("plughw" in low or "hw:" in low or "alsa_input" in low or "pulse" in low)
        ):
            self.capture_status_label.set_text(
                _("Audio source could not be opened. Verify selected source and backend.")
            )
            self._capture_audio_open_error_reported = True

        if not self._capture_xrun_reported and "alsa buffer xrun" in low:
            self._append_capture_status_warning(
                _("ALSA XRUN detected. Try Channels=1 or reduce live audio load.")
            )
            self._capture_xrun_reported = True

        return False

    def _on_capture_exit(self, rc: int) -> None:
        GLib.idle_add(self._handle_capture_exit, rc)

    def _handle_capture_exit(self, rc: int) -> None:
        if self._capture_monitor_uri:
            if self._preview_running and self._preview_source_mode == "uri":
                self.stop_preview()
            self._capture_monitor_uri = ""

        self.capture_start_button.set_sensitive(True)
        self.capture_stop_button.set_sensitive(False)
        self.capture_status_label.set_text(_("Capture finished with code ") + str(rc))

    def _clear_capture_log(self) -> None:
        self.capture_log_buffer.set_text("")

    def _append_capture_log(self, line: str) -> bool:
        start_iter = self.capture_log_buffer.get_start_iter()
        self.capture_log_buffer.insert(start_iter, line + "\n")
        return False

    def on_preview_start_clicked(self, _button: Gtk.Button) -> None:
        if self._capture_monitor_uri:
            audio_source = self.audio_source_combo.get_active_id() or ""
            self.start_preview_from_uri(
                self._capture_monitor_uri,
                with_audio=bool(audio_source),
                preserve_watchdog_state=True,
            )
            return

        if self.capture_runner.running:
            self._set_preview_status(
                _("Live view during capture requires policy Keep or Auto-fallback before capture start.")
            )
            return

        self.start_preview()

    def on_preview_stop_clicked(self, _button: Gtk.Button) -> None:
        self.stop_preview()

    def _set_preview_status(self, text: str) -> None:
        self.preview_status_label.set_text(text)

    def _set_audio_level(self, level_fraction: float, db_label: str | None = None) -> None:
        level = min(max(level_fraction, 0.0), 1.0)
        self.preview_level_bar.set_fraction(level)
        if db_label:
            self.preview_level_bar.set_text(db_label)
        else:
            self.preview_level_bar.set_text(_("Audio level"))

    def on_preview_audio_control_changed(self, _widget) -> None:
        if self._preview_volume_element is None:
            return

        try:
            volume = float(self.preview_volume_scale.get_value())
            muted = bool(self.preview_mute_check.get_active())
            self._preview_volume_element.set_property("volume", volume)
            self._preview_volume_element.set_property("mute", muted)
        except Exception:
            return

    def _live_during_capture_policy(self) -> str:
        return self.live_during_capture_combo.get_active_id() or "auto"

    def _auto_fallback_enabled(self) -> bool:
        return self._live_during_capture_policy() == "auto"

    def _start_preview_audio_watchdog(self) -> None:
        self._stop_preview_audio_watchdog()
        if not self._preview_include_audio:
            return
        self._preview_audio_last_level_monotonic = time.monotonic()
        self._preview_pending_watchdog_action = False
        self._preview_audio_watchdog_id = GLib.timeout_add(500, self._on_preview_audio_watchdog)

    def _stop_preview_audio_watchdog(self) -> None:
        if self._preview_audio_watchdog_id is not None:
            try:
                GLib.source_remove(self._preview_audio_watchdog_id)
            except Exception:
                pass
        self._preview_audio_watchdog_id = None
        self._preview_pending_watchdog_action = False

    def _on_preview_audio_watchdog(self) -> bool:
        if not self._preview_running:
            self._preview_audio_watchdog_id = None
            return False

        if not self._preview_include_audio:
            self._preview_audio_watchdog_id = None
            return False

        if self._preview_pending_watchdog_action:
            return True

        elapsed = time.monotonic() - self._preview_audio_last_level_monotonic
        if elapsed < 1.8:
            return True

        reason = _("No live audio level updates received.")

        if self._preview_audio_restart_count < 1:
            self._preview_audio_restart_count += 1
            self._preview_pending_watchdog_action = True
            self._append_capture_status_warning(_("Live audio stalled; restarting preview audio."))
            self._append_capture_log("[preview] Live audio stalled; restarting preview audio.")
            GLib.idle_add(self._recover_preview_audio, "restart", reason)
            return True

        if self._auto_fallback_enabled():
            self._preview_pending_watchdog_action = True
            self._append_capture_status_warning(_("Live audio fallback activated."))
            self._append_capture_log("[preview] Live audio fallback activated (watchdog).")
            GLib.idle_add(self._recover_preview_audio, "fallback", reason)
            return True

        self._set_preview_status(_("Live audio stalled. Stop/start live view to recover."))
        return True

    def _recover_preview_audio(self, mode: str, reason: str) -> bool:
        self._preview_pending_watchdog_action = False
        if not self._preview_running:
            return False

        if mode == "restart":
            self._restart_preview_source(with_audio=True, preserve_watchdog_state=True)
            return False

        self._preview_audio_fallback_reason = reason
        self._restart_preview_source(with_audio=False, preserve_watchdog_state=True)
        return False

    def _restart_preview_source(self, with_audio: bool, preserve_watchdog_state: bool = False) -> None:
        if self._preview_source_mode == "uri" and self._preview_source_uri:
            self.start_preview_from_uri(
                self._preview_source_uri,
                with_audio=with_audio,
                preserve_watchdog_state=preserve_watchdog_state,
            )
            return

        self.start_preview(with_audio=with_audio, preserve_watchdog_state=preserve_watchdog_state)

    def _preview_start_error_detail(self, bus) -> str:
        if bus is None:
            return ""
        try:
            msg = bus.timed_pop_filtered(250 * Gst.MSECOND, Gst.MessageType.ERROR)
        except Exception:
            return ""
        if msg is None:
            return ""
        try:
            err, dbg = msg.parse_error()
        except Exception:
            return ""
        detail = str(err) if err is not None else ""
        if dbg:
            detail = f"{detail} ({dbg})" if detail else str(dbg)
        return detail.strip()

    def start_preview(self, with_audio: bool = True, preserve_watchdog_state: bool = False) -> None:
        if self._capture_monitor_uri:
            audio_source = self.audio_source_combo.get_active_id() or ""
            self.start_preview_from_uri(
                self._capture_monitor_uri,
                with_audio=bool(audio_source),
                preserve_watchdog_state=preserve_watchdog_state,
            )
            return

        if self.capture_runner.running:
            self._set_preview_status(
                _("Live view during capture requires policy Keep or Auto-fallback before capture start.")
            )
            return

        self.stop_preview()
        if not preserve_watchdog_state:
            self._preview_audio_restart_count = 0
            self._preview_audio_fallback_reason = ""

        if not GST_AVAILABLE or not GST_GTK4PAINTABLE_AVAILABLE:
            self._set_preview_status(
                _("Live view requires GStreamer with gtk4paintablesink. Install gstreamer1-plugins-bad-free-gtk4.")
            )
            return

        video_device = self.video_device_combo.get_active_id() or ""
        if not video_device:
            self._set_preview_status(_("Choose a video device first."))
            return

        video_input_id = self.video_input_combo.get_active_id() or ""
        for warning in _prepare_v4l2_video_input(video_device, video_input_id):
            self._append_capture_status_warning(warning)

        for warning in _prepare_v4l2_audio_capture(video_device):
            self._append_capture_status_warning(warning)

        pipeline = Gst.Pipeline.new("capture-preview")
        if pipeline is None:
            self._set_preview_status(_("Could not create preview pipeline."))
            return

        vsrc = Gst.ElementFactory.make("v4l2src", "vsrc")
        vqueue = Gst.ElementFactory.make("queue", "vqueue")
        vconvert = Gst.ElementFactory.make("videoconvert", "vconvert")
        vsink = Gst.ElementFactory.make("gtk4paintablesink", "vsink")

        if not all([vsrc, vqueue, vconvert, vsink]):
            self._set_preview_status(_("Missing GStreamer elements for video preview."))
            return

        vsrc.set_property("device", video_device)
        if vqueue.find_property("max-size-buffers") is not None:
            vqueue.set_property("max-size-buffers", 4)
        if vqueue.find_property("max-size-bytes") is not None:
            vqueue.set_property("max-size-bytes", 0)
        if vqueue.find_property("max-size-time") is not None:
            vqueue.set_property("max-size-time", 0)
        if vqueue.find_property("leaky") is not None:
            vqueue.set_property("leaky", 2)

        input_fmt = self.video_format_combo.get_active_id() or ""
        gst_raw_fmt = _normalize_gst_raw_format(input_fmt)

        vcaps = None
        if gst_raw_fmt:
            vcaps = Gst.ElementFactory.make("capsfilter", "vcaps")
            if vcaps is None:
                self._set_preview_status(_("Missing GStreamer capsfilter for preview."))
                return
            caps = Gst.Caps.from_string(f"video/x-raw,format={gst_raw_fmt}")
            vcaps.set_property("caps", caps)

        if input_fmt and not gst_raw_fmt:
            self._append_capture_status_warning(
                _("Live view uses device default pixel format for this source format.")
            )

        pipeline.add(vsrc)
        pipeline.add(vqueue)
        pipeline.add(vconvert)
        if vcaps is not None:
            pipeline.add(vcaps)
        pipeline.add(vsink)

        if not vsrc.link(vqueue) or not vqueue.link(vconvert):
            self._set_preview_status(_("Could not link video preview pipeline."))
            pipeline.set_state(Gst.State.NULL)
            return

        if vcaps is not None:
            video_link_ok = vconvert.link(vcaps) and vcaps.link(vsink)
        else:
            video_link_ok = vconvert.link(vsink)

        if not video_link_ok:
            self._set_preview_status(_("Could not link video preview pipeline."))
            pipeline.set_state(Gst.State.NULL)
            return

        self._preview_volume_element = None

        audio_source = self.audio_source_combo.get_active_id() or ""
        include_audio_preview = with_audio and bool(audio_source)
        if include_audio_preview:
            backend = self.audio_backend_combo.get_active_id() or "pulse"
            audio_branch_issue = _("Audio preview pipeline could not be started.")
            if backend == "alsa":
                asrc = Gst.ElementFactory.make("alsasrc", "asrc")
                if asrc is not None:
                    asrc.set_property("device", audio_source)
            else:
                asrc = Gst.ElementFactory.make("pulsesrc", "asrc")
                if asrc is not None and audio_source:
                    asrc.set_property("device", audio_source)

            aqueue = Gst.ElementFactory.make("queue", "aqueue")
            aconvert = Gst.ElementFactory.make("audioconvert", "aconvert")
            aresample = Gst.ElementFactory.make("audioresample", "aresample")
            volume = Gst.ElementFactory.make("volume", "avolume")
            level = Gst.ElementFactory.make("level", "alevel")
            asink = Gst.ElementFactory.make("autoaudiosink", "asink")

            if all([asrc, aqueue, aconvert, aresample, volume, level, asink]):
                if asrc.find_property("do-timestamp") is not None:
                    asrc.set_property("do-timestamp", True)
                if asrc.find_property("buffer-time") is not None:
                    asrc.set_property("buffer-time", 400000)
                if asrc.find_property("latency-time") is not None:
                    asrc.set_property("latency-time", 50000)

                if aqueue.find_property("max-size-buffers") is not None:
                    aqueue.set_property("max-size-buffers", 16)
                if aqueue.find_property("max-size-bytes") is not None:
                    aqueue.set_property("max-size-bytes", 0)
                if aqueue.find_property("max-size-time") is not None:
                    aqueue.set_property("max-size-time", 0)
                if aqueue.find_property("leaky") is not None:
                    aqueue.set_property("leaky", 2)

                if asink.find_property("sync") is not None:
                    asink.set_property("sync", False)
                if asink.find_property("async") is not None:
                    asink.set_property("async", False)

                level.set_property("interval", 100_000_000)
                level.set_property("post-messages", True)

                pipeline.add(asrc)
                pipeline.add(aqueue)
                pipeline.add(aconvert)
                pipeline.add(aresample)
                pipeline.add(volume)
                pipeline.add(level)
                pipeline.add(asink)

                linked_ok = (
                    asrc.link(aqueue)
                    and aqueue.link(aconvert)
                    and aconvert.link(aresample)
                    and aresample.link(volume)
                    and volume.link(level)
                    and level.link(asink)
                )
                if linked_ok:
                    self._preview_volume_element = volume
                else:
                    audio_branch_issue = _("Audio preview pipeline could not be linked.")
            else:
                audio_branch_issue = _("Missing GStreamer elements for audio preview.")

            if self._preview_volume_element is None:
                if self._auto_fallback_enabled():
                    self._append_capture_status_warning(_("Live audio fallback activated."))
                    self._append_capture_log("[preview] Live audio fallback activated (audio branch setup).")
                    self._preview_audio_fallback_reason = audio_branch_issue
                    try:
                        pipeline.set_state(Gst.State.NULL)
                    except Exception:
                        pass
                    self.start_preview(with_audio=False, preserve_watchdog_state=True)
                    return
                self._set_preview_status(audio_branch_issue)

        paintable = vsink.get_property("paintable")
        self.preview_picture.set_paintable(paintable)

        bus = pipeline.get_bus()
        if bus is None:
            self._set_preview_status(_("Could not attach GStreamer bus."))
            pipeline.set_state(Gst.State.NULL)
            return

        bus.add_signal_watch()
        handler_id = bus.connect("message", self._on_preview_bus_message)

        state_ret = pipeline.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            detail = self._preview_start_error_detail(bus)
            if handler_id:
                bus.disconnect(handler_id)
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
            if include_audio_preview and self._auto_fallback_enabled():
                self._append_capture_status_warning(_("Live audio fallback activated."))
                reason = detail or _("Audio preview could not start.")
                self._append_capture_log("[preview] Live audio fallback activated (start failure). " + reason)
                self._preview_audio_fallback_reason = reason
                self.start_preview(with_audio=False, preserve_watchdog_state=True)
                return
            if detail:
                self._set_preview_status(_("Could not start live preview: ") + detail)
            else:
                self._set_preview_status(_("Could not start live preview."))
            return

        self._preview_pipeline = pipeline
        self._preview_bus = bus
        self._preview_bus_handler_id = handler_id
        self._preview_running = True
        self._preview_include_audio = include_audio_preview and self._preview_volume_element is not None
        self._preview_source_mode = "device"
        self._preview_source_uri = ""

        self.preview_start_button.set_sensitive(False)
        self.preview_stop_button.set_sensitive(True)

        if self._preview_include_audio:
            self._start_preview_audio_watchdog()
            self._set_preview_status(_("Live preview running."))
        else:
            self._stop_preview_audio_watchdog()
            if self._preview_audio_fallback_reason:
                self._set_preview_status(_("Live audio fallback activated (video only)."))
            else:
                self._set_preview_status(_("Live preview running (video only)."))
        self._set_audio_level(0.0)

        self.on_preview_audio_control_changed(self.preview_mute_check)

    def start_preview_from_uri(
        self,
        uri: str,
        with_audio: bool = True,
        preserve_watchdog_state: bool = False,
    ) -> None:
        self.stop_preview()
        if not preserve_watchdog_state:
            self._preview_audio_restart_count = 0
            self._preview_audio_fallback_reason = ""

        if not GST_AVAILABLE or not GST_GTK4PAINTABLE_AVAILABLE:
            self._set_preview_status(
                _("Live view requires GStreamer with gtk4paintablesink. Install gstreamer1-plugins-bad-free-gtk4.")
            )
            return

        if not uri:
            self._set_preview_status(_("Could not start live monitor preview."))
            return

        pipeline = Gst.ElementFactory.make("playbin", "capture-monitor-preview")
        vsink = Gst.ElementFactory.make("gtk4paintablesink", "vsink")
        if pipeline is None or vsink is None:
            self._set_preview_status(_("Missing GStreamer elements for video preview."))
            return

        pipeline.set_property("uri", uri)
        pipeline.set_property("video-sink", vsink)

        include_audio_preview = bool(with_audio)
        self._preview_volume_element = None

        if include_audio_preview:
            audio_branch_issue = _("Audio preview pipeline could not be started.")
            audio_bin = None
            try:
                audio_bin = Gst.parse_bin_from_description(
                    "volume name=avolume ! level name=alevel interval=100000000 post-messages=true ! "
                    "autoaudiosink sync=false async=false",
                    True,
                )
            except Exception:
                audio_bin = None

            if audio_bin is not None:
                try:
                    pipeline.set_property("audio-sink", audio_bin)
                except Exception:
                    audio_bin = None

            if audio_bin is not None:
                volume = audio_bin.get_by_name("avolume")
                if volume is not None:
                    self._preview_volume_element = volume
                else:
                    audio_branch_issue = _("Audio preview pipeline could not be linked.")
            else:
                audio_branch_issue = _("Missing GStreamer elements for audio preview.")

            if self._preview_volume_element is None:
                if self._auto_fallback_enabled():
                    self._append_capture_status_warning(_("Live audio fallback activated."))
                    self._append_capture_log("[preview] Live audio fallback activated (capture monitor setup).")
                    self._preview_audio_fallback_reason = audio_branch_issue
                    try:
                        pipeline.set_state(Gst.State.NULL)
                    except Exception:
                        pass
                    self.start_preview_from_uri(uri, with_audio=False, preserve_watchdog_state=True)
                    return
                self._set_preview_status(audio_branch_issue)

        if not include_audio_preview and pipeline.find_property("mute") is not None:
            pipeline.set_property("mute", True)

        paintable = vsink.get_property("paintable")
        self.preview_picture.set_paintable(paintable)

        bus = pipeline.get_bus()
        if bus is None:
            self._set_preview_status(_("Could not attach GStreamer bus."))
            pipeline.set_state(Gst.State.NULL)
            return

        bus.add_signal_watch()
        handler_id = bus.connect("message", self._on_preview_bus_message)

        state_ret = pipeline.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            detail = self._preview_start_error_detail(bus)
            if handler_id:
                bus.disconnect(handler_id)
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
            if include_audio_preview and self._auto_fallback_enabled():
                self._append_capture_status_warning(_("Live audio fallback activated."))
                reason = detail or _("Audio preview could not start.")
                self._append_capture_log("[preview] Live audio fallback activated (capture monitor start). " + reason)
                self._preview_audio_fallback_reason = reason
                self.start_preview_from_uri(uri, with_audio=False, preserve_watchdog_state=True)
                return
            if detail:
                self._set_preview_status(_("Could not start live preview: ") + detail)
            else:
                self._set_preview_status(_("Could not start live preview."))
            return

        self._preview_pipeline = pipeline
        self._preview_bus = bus
        self._preview_bus_handler_id = handler_id
        self._preview_running = True
        self._preview_include_audio = include_audio_preview and self._preview_volume_element is not None
        self._preview_source_mode = "uri"
        self._preview_source_uri = uri

        self.preview_start_button.set_sensitive(False)
        self.preview_stop_button.set_sensitive(True)

        if self._preview_include_audio:
            self._start_preview_audio_watchdog()
            self._set_preview_status(_("Live preview running (capture stream)."))
        else:
            self._stop_preview_audio_watchdog()
            if self._preview_audio_fallback_reason:
                self._set_preview_status(_("Live audio fallback activated (video only)."))
            else:
                self._set_preview_status(_("Live preview running (video only)."))
        self._set_audio_level(0.0)

        self.on_preview_audio_control_changed(self.preview_mute_check)

    def _on_preview_bus_message(self, _bus, message) -> None:
        mtype = message.type

        if mtype == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            dbg_part = f" ({dbg})" if dbg else ""
            error_text = str(err) + dbg_part
            if self._preview_include_audio and self._auto_fallback_enabled():
                self._append_capture_status_warning(_("Live audio fallback activated."))
                self._append_capture_log("[preview] Live audio fallback activated (runtime error). " + error_text)
                self._preview_audio_fallback_reason = error_text
                self._restart_preview_source(with_audio=False, preserve_watchdog_state=True)
            else:
                self._set_preview_status(_("Preview error: ") + error_text)
                self.stop_preview()
            return

        if mtype == Gst.MessageType.EOS:
            self._set_preview_status(_("Preview stopped (EOS)."))
            self.stop_preview()
            return

        if mtype == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is None:
                return
            if structure.get_name() != "level":
                return

            rms = structure.get_value("rms")
            if not rms:
                return

            values = [float(v) for v in rms if isinstance(v, (float, int))]
            if not values:
                return

            self._preview_audio_last_level_monotonic = time.monotonic()
            db = max(values)
            if db < -120.0:
                level = 0.0
            else:
                level = max(0.0, min(1.0, (db + 60.0) / 60.0))

            self._set_audio_level(level, f"{db:.1f} dB")

    def stop_preview(self) -> None:
        if not self._preview_running:
            self._stop_preview_audio_watchdog()
            self._preview_include_audio = False
            self._preview_source_mode = "device"
            self._preview_source_uri = ""
            self._set_audio_level(0.0)
            self.preview_picture.set_paintable(None)
            return

        pipeline = self._preview_pipeline
        bus = self._preview_bus
        handler_id = self._preview_bus_handler_id

        self._preview_pipeline = None
        self._preview_bus = None
        self._preview_bus_handler_id = None
        self._preview_volume_element = None
        self._preview_include_audio = False
        self._preview_running = False
        self._preview_source_mode = "device"
        self._preview_source_uri = ""
        self._stop_preview_audio_watchdog()

        if bus is not None:
            if handler_id is not None:
                try:
                    bus.disconnect(handler_id)
                except Exception:
                    pass
            try:
                bus.remove_signal_watch()
            except Exception:
                pass

        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        self.preview_picture.set_paintable(None)
        self._set_audio_level(0.0)

        self.preview_start_button.set_sensitive(True)
        self.preview_stop_button.set_sensitive(False)

        self._set_preview_status(_("Live preview stopped."))

    def shutdown(self) -> None:
        self.stop_preview()
        self.capture_runner.stop()

    def _append_capture_status_warning(self, text: str) -> None:
        existing = self.capture_status_label.get_text().strip()
        if not existing:
            self.capture_status_label.set_text(text)
            return
        self.capture_status_label.set_text(existing + "\n" + text)
