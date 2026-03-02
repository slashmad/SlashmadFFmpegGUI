from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gio, Gtk

from ffmpeg_gui.ffmpeg import EncoderInfo, PixelFormat
from ffmpeg_gui.i18n import _
from ffmpeg_gui.runner import FFmpegRunner
from ffmpeg_gui.ui import bind_objects, compact_widget, load_builder, require_object

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


EDIT_CONTAINERS = ["mkv", "mp4", "mov"]
DEFAULT_VIDEO_CODECS = ["copy", "libx264", "ffv1"]
DEFAULT_AUDIO_CODECS = ["none", "copy", "aac", "pcm_s16le"]
EDIT_OUTPUT_MODES: list[tuple[str, str]] = [
    ("copy", _("Keep source streams")),
    ("reencode", _("Re-encode")),
]


def _shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID") or os.environ.get("FLATPAK_SANDBOX_DIR"))


def _run_command(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
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


def _format_time_label(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def _parse_fps_value(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text or text == "0/0":
        return None
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            num = float(left)
            den = float(right)
            if den == 0.0:
                return None
            return num / den
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _probe_media(path: str) -> dict[str, Any]:
    rc, out, _ = _run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,avg_frame_rate",
            path,
        ],
        timeout=15.0,
    )
    if rc != 0 or not out:
        return {}

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


class TrimRangeBar(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(520)
        self.set_content_height(28)
        self.set_hexpand(True)
        self.set_focusable(True)

        self._min_value = 0.0
        self._max_value = 1.0
        self._start_value = 0.0
        self._end_value = 1.0
        self._min_gap = 0.04
        self._active_handle: str | None = None
        self._selected_handle = "start"
        self._drag_origin_x = 0.0
        self._on_changed = None

        self.set_draw_func(self._on_draw)

        click = Gtk.GestureClick.new()
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def set_changed_callback(self, callback) -> None:
        self._on_changed = callback

    def set_limits(self, min_value: float, max_value: float) -> None:
        self._min_value = float(min_value)
        self._max_value = max(float(max_value), self._min_value + self._min_gap)
        self._start_value = max(self._min_value, min(self._start_value, self._max_value))
        self._end_value = max(self._start_value + self._min_gap, min(self._end_value, self._max_value))
        self.queue_draw()

    def set_values(self, start_value: float, end_value: float) -> None:
        self._start_value = max(self._min_value, min(float(start_value), self._max_value))
        self._end_value = max(self._start_value + self._min_gap, min(float(end_value), self._max_value))
        self.queue_draw()

    def set_start_value(self, value: float) -> None:
        self.set_values(value, self._end_value)

    def set_end_value(self, value: float) -> None:
        self.set_values(self._start_value, value)

    def get_start_value(self) -> float:
        return self._start_value

    def get_end_value(self) -> float:
        return self._end_value

    def get_selected_handle(self) -> str:
        return self._selected_handle

    def has_selected_handle(self) -> bool:
        return self._selected_handle in {"start", "end"}

    def nudge_selected_handle(self, delta: float) -> str:
        handle = self._selected_handle if self._selected_handle in {"start", "end"} else "start"
        if handle == "start":
            self._start_value = min(
                max(self._min_value, self._start_value + delta),
                self._end_value - self._min_gap,
            )
        else:
            self._end_value = max(
                min(self._max_value, self._end_value + delta),
                self._start_value + self._min_gap,
            )
        self.queue_draw()
        return handle

    def _track_geometry(self) -> tuple[float, float, float]:
        width = float(max(self.get_allocated_width(), 1))
        left = 10.0
        right = width - 10.0
        center_y = float(self.get_allocated_height()) / 2.0
        return left, right, center_y

    def _value_to_x(self, value: float) -> float:
        left, right, _ = self._track_geometry()
        span = max(self._max_value - self._min_value, 0.0001)
        ratio = (value - self._min_value) / span
        return left + max(0.0, min(1.0, ratio)) * (right - left)

    def _x_to_value(self, x: float) -> float:
        left, right, _ = self._track_geometry()
        if right <= left:
            return self._min_value
        ratio = (x - left) / (right - left)
        return self._min_value + max(0.0, min(1.0, ratio)) * (self._max_value - self._min_value)

    def _emit_changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed(self, self._active_handle or self._selected_handle or "")

    def _update_handle_from_x(self, handle: str, x: float) -> None:
        value = self._x_to_value(x)
        if handle == "start":
            self._start_value = min(max(self._min_value, value), self._end_value - self._min_gap)
        else:
            self._end_value = max(min(self._max_value, value), self._start_value + self._min_gap)
        self.queue_draw()
        self._emit_changed()

    def _pick_handle(self, x: float) -> str:
        start_x = self._value_to_x(self._start_value)
        end_x = self._value_to_x(self._end_value)
        return "start" if abs(x - start_x) <= abs(x - end_x) else "end"

    def _on_pressed(self, _gesture, _n_press, x: float, _y: float) -> None:
        if not self.get_sensitive():
            return
        self._active_handle = self._pick_handle(x)
        self._selected_handle = self._active_handle
        self._drag_origin_x = x
        self.grab_focus()
        self._update_handle_from_x(self._active_handle, x)

    def _on_released(self, *_args) -> None:
        self._active_handle = None

    def _on_drag_update(self, _gesture, offset_x: float, _offset_y: float) -> None:
        if not self.get_sensitive() or self._active_handle is None:
            return
        self._update_handle_from_x(self._active_handle, self._drag_origin_x + offset_x)

    def _on_drag_end(self, *_args) -> None:
        self._active_handle = None

    def _on_draw(self, _area, cr, width: int, height: int) -> None:
        left, right, center_y = self._track_geometry()
        start_x = self._value_to_x(self._start_value)
        end_x = self._value_to_x(self._end_value)
        track_h = 4.0
        radius = 8.0

        cr.set_source_rgba(0.35, 0.35, 0.35, 1.0)
        cr.rectangle(left, center_y - track_h / 2.0, right - left, track_h)
        cr.fill()

        cr.set_source_rgba(0.14, 0.47, 0.86, 1.0)
        cr.rectangle(start_x, center_y - track_h / 2.0, max(end_x - start_x, 1.0), track_h)
        cr.fill()

        for handle_name, handle_x in (("start", start_x), ("end", end_x)):
            cr.set_source_rgba(0.14, 0.47, 0.86, 1.0)
            cr.arc(handle_x, center_y, radius, 0.0, math.tau)
            cr.fill()
            cr.set_source_rgba(0.85, 0.88, 0.92, 1.0)
            cr.arc(handle_x, center_y, radius - 3.0, 0.0, math.tau)
            cr.fill()
            if self.has_focus() and self._selected_handle == handle_name:
                cr.set_line_width(2.0)
                cr.set_source_rgba(0.95, 0.97, 1.0, 1.0)
                cr.arc(handle_x, center_y, radius + 1.5, 0.0, math.tau)
                cr.stroke()


class EditPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_focusable(True)

        page_keys = Gtk.EventControllerKey()
        page_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        page_keys.connect("key-pressed", self.on_preview_key_pressed)
        self.add_controller(page_keys)

        self._ffmpeg_command: list[str] | None = None
        self._encoders: list[EncoderInfo] = []
        self._pixel_formats: list[PixelFormat] = []

        self._preview_pipeline = None
        self._preview_bus = None
        self._preview_bus_handler_id: int | None = None
        self._preview_volume_element = None
        self._preview_video_balance = None
        self._preview_running = False
        self._preview_paused = False
        self._preview_source_uri = ""
        self._preview_trim_watch_id: int | None = None
        self._preview_seek_watch_id: int | None = None

        self._source_duration_seconds = 0.0
        self._source_container = ""
        self._source_video_codec = ""
        self._source_audio_codec = ""
        self._source_fps = 0.0
        self._updating_trim_controls = False
        self._updating_seek_scale = False

        self.runner = FFmpegRunner(self._on_runner_output, self._on_runner_exit)

        self._build_ui()
        self._set_default_output_path()

    def _build_ui(self) -> None:
        builder = load_builder("edit_page.ui")
        bind_objects(
            self,
            builder,
            [
                "info_label",
                "source_entry",
                "source_meta_label",
                "trim_summary_label",
                "trim_start_label",
                "trim_end_label",
                "trim_set_start_button",
                "trim_set_end_button",
                "trim_start_prev_frame_button",
                "trim_start_next_frame_button",
                "trim_end_prev_frame_button",
                "trim_end_next_frame_button",
                "preview_picture",
                "preview_start_button",
                "preview_pause_button",
                "preview_stop_button",
                "preview_mute_check",
                "preview_volume_scale",
                "preview_position_label",
                "preview_seek_scale",
                "preview_duration_label",
                "preview_status_label",
                "audio_delay_spin",
                "deinterlace_check",
                "denoise_check",
                "denoise_strength_spin",
                "brightness_scale",
                "contrast_scale",
                "saturation_scale",
                "audio_gain_spin",
                "highpass_spin",
                "lowpass_spin",
                "output_entry",
                "output_mode_combo",
                "container_combo",
                "video_codec_combo",
                "video_bitrate_entry",
                "audio_codec_combo",
                "audio_bitrate_entry",
                "sample_rate_spin",
                "channels_combo",
                "match_fps_check",
                "output_fps_spin",
                "pix_fmt_combo",
                "start_button",
                "stop_button",
                "status_label",
                "log_view",
            ],
        )

        self.append(require_object(builder, "edit_page_root"))

        source_pick_button = require_object(builder, "source_pick_button")
        trim_range_placeholder = require_object(builder, "trim_range_placeholder")
        out_pick_button = require_object(builder, "out_pick_button")

        self.trim_range_bar = TrimRangeBar()
        self.trim_range_bar.set_sensitive(False)
        self.trim_range_bar.set_changed_callback(self.on_trim_changed)
        trim_range_placeholder.append(self.trim_range_bar)

        preview_keys = Gtk.EventControllerKey()
        preview_keys.connect("key-pressed", self.on_preview_key_pressed)
        self.preview_picture.add_controller(preview_keys)

        self.command_buffer = Gtk.TextBuffer()
        self.log_buffer = self.log_view.get_buffer()

        self.source_entry.connect("changed", self.on_source_changed)
        source_pick_button.connect("clicked", self.on_choose_source_clicked)

        self.trim_set_start_button.connect("clicked", self.on_trim_set_start_clicked)
        self.trim_set_end_button.connect("clicked", self.on_trim_set_end_clicked)
        self.trim_start_prev_frame_button.connect("clicked", self.on_trim_start_prev_frame_clicked)
        self.trim_start_next_frame_button.connect("clicked", self.on_trim_start_next_frame_clicked)
        self.trim_end_prev_frame_button.connect("clicked", self.on_trim_end_prev_frame_clicked)
        self.trim_end_next_frame_button.connect("clicked", self.on_trim_end_next_frame_clicked)

        self.preview_start_button.connect("clicked", self.on_preview_start_clicked)
        self.preview_pause_button.connect("clicked", self.on_preview_pause_clicked)
        self.preview_stop_button.connect("clicked", self.on_preview_stop_clicked)
        self.preview_mute_check.connect("toggled", self.on_preview_audio_changed)
        self.preview_volume_scale.set_value(1.0)
        self.preview_volume_scale.connect("value-changed", self.on_preview_audio_changed)
        self.preview_seek_scale.connect("value-changed", self.on_preview_seek_changed)

        self.audio_delay_spin.set_value(0)
        self.audio_delay_spin.connect("value-changed", self.on_audio_delay_changed)
        self.deinterlace_check.connect("toggled", self.on_preview_rebuild_setting_changed)
        self.denoise_check.connect("toggled", self.on_preview_rebuild_setting_changed)
        self.denoise_strength_spin.set_value(1.0)
        self.denoise_strength_spin.set_digits(1)
        self.denoise_strength_spin.connect("value-changed", self.on_preview_rebuild_setting_changed)
        self.brightness_scale.connect("value-changed", self.on_video_balance_changed)
        self.contrast_scale.connect("value-changed", self.on_video_balance_changed)
        self.saturation_scale.connect("value-changed", self.on_video_balance_changed)
        self.audio_gain_spin.set_value(0.0)
        self.audio_gain_spin.set_digits(1)
        self.audio_gain_spin.connect("value-changed", self.on_audio_filter_changed)
        self.highpass_spin.connect("value-changed", self.on_audio_filter_changed)
        self.lowpass_spin.connect("value-changed", self.on_audio_filter_changed)

        self.output_entry.connect("changed", self.on_settings_changed)
        out_pick_button.connect("clicked", self.on_choose_output_clicked)

        self.output_mode_combo.remove_all()
        for mode_id, label in EDIT_OUTPUT_MODES:
            self.output_mode_combo.append(mode_id, label)
        self.output_mode_combo.set_active_id("copy")
        self.output_mode_combo.connect("changed", self.on_output_mode_changed)

        self.container_combo.remove_all()
        for container in EDIT_CONTAINERS:
            self.container_combo.append(container, container.upper())
        self.container_combo.set_active_id("mkv")
        self.container_combo.connect("changed", self.on_container_changed)

        self.video_codec_combo.connect("changed", self.on_settings_changed)
        self.video_bitrate_entry.set_text("6M")
        self.video_bitrate_entry.connect("changed", self.on_settings_changed)
        self.audio_codec_combo.connect("changed", self.on_settings_changed)
        self.audio_bitrate_entry.set_text("192k")
        self.audio_bitrate_entry.connect("changed", self.on_settings_changed)
        self.sample_rate_spin.set_value(48000)
        self.sample_rate_spin.connect("value-changed", self.on_settings_changed)

        self.channels_combo.remove_all()
        self.channels_combo.append("1", "1")
        self.channels_combo.append("2", "2")
        self.channels_combo.set_active_id("2")
        self.channels_combo.connect("changed", self.on_settings_changed)

        self.match_fps_check.set_active(True)
        self.match_fps_check.connect("toggled", self.on_match_fps_toggled)
        self.output_fps_spin.set_value(25.0)
        self.output_fps_spin.set_digits(3)
        self.output_fps_spin.set_sensitive(False)
        self.output_fps_spin.connect("value-changed", self.on_settings_changed)
        self.pix_fmt_combo.connect("changed", self.on_settings_changed)

        for widget, width in (
            (self.output_mode_combo, 150),
            (self.container_combo, 100),
            (self.video_codec_combo, 160),
            (self.audio_codec_combo, 150),
            (self.channels_combo, 75),
            (self.pix_fmt_combo, 150),
        ):
            compact_widget(widget, width)
        self.start_button.connect("clicked", self.on_start_clicked)
        self.stop_button.connect("clicked", self.on_stop_clicked)

        self._populate_codec_combos()
        self._populate_pix_fmt_combo()
        self._sync_output_mode_widgets()
        self._set_preview_state_buttons()
        self.update_command_preview()

    def _add_setting_row(self, parent: Gtk.Box, label_text: str, widget: Gtk.Widget, hint_text: str) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        parent.append(row)

        label = Gtk.Label(label=label_text)
        label.set_size_request(150, -1)
        label.set_xalign(0)
        row.append(label)

        widget.set_hexpand(True)
        row.append(widget)

        hint = Gtk.Label(label=hint_text)
        hint.set_xalign(0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

    def _labeled_widget(self, widget: Gtk.Widget, value_label: Gtk.Label) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_hexpand(True)
        box.append(widget)
        value_label.set_xalign(1.0)
        value_label.set_width_chars(12)
        box.append(value_label)
        return box

    def _output_mode(self) -> str:
        return self.output_mode_combo.get_active_id() or "copy"

    def _trim_start_seconds(self) -> float:
        if self._source_duration_seconds <= 0.0:
            return 0.0
        return min(max(float(self.trim_range_bar.get_start_value()), 0.0), self._source_duration_seconds)

    def _trim_end_seconds(self) -> float:
        if self._source_duration_seconds <= 0.0:
            return 0.0
        return min(max(float(self.trim_range_bar.get_end_value()), 0.0), self._source_duration_seconds)

    def _trim_duration_seconds(self) -> float:
        return max(0.0, self._trim_end_seconds() - self._trim_start_seconds())

    def _set_trim_enabled(self, enabled: bool) -> None:
        self.trim_range_bar.set_sensitive(enabled)
        self.trim_set_start_button.set_sensitive(enabled)
        self.trim_set_end_button.set_sensitive(enabled)
        self.trim_start_prev_frame_button.set_sensitive(enabled)
        self.trim_start_next_frame_button.set_sensitive(enabled)
        self.trim_end_prev_frame_button.set_sensitive(enabled)
        self.trim_end_next_frame_button.set_sensitive(enabled)

    def _sync_source_metadata(self) -> None:
        source_path = self.source_entry.get_text().strip()
        self._source_duration_seconds = 0.0
        self._source_container = ""
        self._source_video_codec = ""
        self._source_audio_codec = ""
        self._source_fps = 0.0

        if not source_path or not os.path.isfile(source_path):
            self.source_meta_label.set_text(_("No source loaded."))
            self.trim_summary_label.set_text(_("Load a file to set clip start and end."))
            self._set_trim_enabled(False)
            self._set_trim_range(0.0)
            return

        probe = _probe_media(source_path)
        fmt = probe.get("format") or {}
        streams = probe.get("streams") or []

        try:
            self._source_duration_seconds = max(0.0, float(fmt.get("duration") or 0.0))
        except (TypeError, ValueError):
            self._source_duration_seconds = 0.0

        self._source_container = str(fmt.get("format_name") or "").split(",", 1)[0]

        for stream in streams:
            codec_type = stream.get("codec_type")
            codec_name = str(stream.get("codec_name") or "")
            if codec_type == "video" and not self._source_video_codec:
                self._source_video_codec = codec_name
                self._source_fps = _parse_fps_value(stream.get("avg_frame_rate")) or 0.0
            elif codec_type == "audio" and not self._source_audio_codec:
                self._source_audio_codec = codec_name

        parts: list[str] = []
        if self._source_container:
            parts.append(self._source_container.upper())
        if self._source_video_codec:
            video_text = self._source_video_codec
            if self._source_fps > 0.0:
                video_text += f" @ {self._source_fps:.3f} fps"
            parts.append(_("Video: ") + video_text)
        if self._source_audio_codec:
            parts.append(_("Audio: ") + self._source_audio_codec)
        if self._source_duration_seconds > 0.0:
            parts.append(_("Duration: ") + _format_time_label(self._source_duration_seconds))

        self.source_meta_label.set_text(" | ".join(parts) if parts else _("Source loaded."))

        if self._source_container in EDIT_CONTAINERS:
            self.container_combo.set_active_id(self._source_container)

        self._set_trim_range(self._source_duration_seconds)
        self._set_trim_enabled(self._source_duration_seconds > 0.0)
        self._update_trim_summary()

    def _set_trim_range(self, duration: float) -> None:
        self._updating_trim_controls = True
        try:
            upper = max(duration, 1.0)
            self.trim_range_bar.set_limits(0.0, upper)
            self.preview_seek_scale.set_range(0.0, upper)
            self.preview_duration_label.set_text(_format_time_label(duration if duration > 0.0 else 0.0))
            if duration > 0.0:
                self.trim_range_bar.set_values(0.0, duration)
                self.trim_start_label.set_text(_format_time_label(0.0))
                self.trim_end_label.set_text(_format_time_label(duration))
            else:
                self.trim_range_bar.set_values(0.0, 0.0)
                self.trim_start_label.set_text("00:00.000")
                self.trim_end_label.set_text("00:00.000")
                self.preview_position_label.set_text("00:00.000")
                self.preview_duration_label.set_text("00:00.000")
                self.preview_seek_scale.set_value(0.0)
        finally:
            self._updating_trim_controls = False

    def _update_trim_summary(self) -> None:
        start = self._trim_start_seconds()
        end = self._trim_end_seconds()
        duration = self._trim_duration_seconds()
        self.trim_start_label.set_text(_format_time_label(start))
        self.trim_end_label.set_text(_format_time_label(end))

        if self._source_duration_seconds <= 0.0:
            self.trim_summary_label.set_text(_("Load a file to set clip start and end."))
            return

        self.trim_summary_label.set_text(
            _("Clip: ")
            + f"{_format_time_label(start)} -> {_format_time_label(end)}"
            + " | "
            + _("Length: ")
            + _format_time_label(duration)
        )

    def _sync_output_mode_widgets(self) -> None:
        reencode = self._output_mode() == "reencode"
        for widget in (
            self.video_codec_combo,
            self.video_bitrate_entry,
            self.audio_codec_combo,
            self.audio_bitrate_entry,
            self.sample_rate_spin,
            self.channels_combo,
            self.match_fps_check,
            self.output_fps_spin,
            self.pix_fmt_combo,
            self.audio_delay_spin,
            self.deinterlace_check,
            self.denoise_check,
            self.denoise_strength_spin,
            self.brightness_scale,
            self.contrast_scale,
            self.saturation_scale,
            self.audio_gain_spin,
            self.highpass_spin,
            self.lowpass_spin,
        ):
            widget.set_sensitive(reencode)
        self.output_fps_spin.set_sensitive(reencode and (not self.match_fps_check.get_active()))

    def sync_capabilities(
        self,
        ffmpeg_command: list[str] | None,
        encoders: list[EncoderInfo],
        pixel_formats: list[PixelFormat],
        hardware_info: Any = None,
    ) -> None:
        del hardware_info
        self._ffmpeg_command = ffmpeg_command
        self._encoders = encoders
        self._pixel_formats = pixel_formats

        if ffmpeg_command:
            self.info_label.set_text(_("Edit uses FFmpeg command: ") + " ".join(ffmpeg_command))
        else:
            self.info_label.set_text(_("FFmpeg not available."))

        self._populate_codec_combos()
        self._populate_pix_fmt_combo()
        self._sync_output_mode_widgets()
        self.update_command_preview()

    def _populate_codec_combos(self) -> None:
        selected_v = self.video_codec_combo.get_active_id() or "libx264"
        selected_a = self.audio_codec_combo.get_active_id() or "aac"

        video_names = {enc.name for enc in self._encoders if enc.kind == "video"}
        audio_names = {enc.name for enc in self._encoders if enc.kind == "audio"}

        self.video_codec_combo.remove_all()
        for name in DEFAULT_VIDEO_CODECS:
            if name == "copy" or name in video_names:
                self.video_codec_combo.append(name, name)
        if self.video_codec_combo.get_model() is not None and self.video_codec_combo.set_active_id(selected_v):
            pass
        elif self.video_codec_combo.set_active_id("libx264"):
            pass
        else:
            self.video_codec_combo.set_active(0)

        self.audio_codec_combo.remove_all()
        for name in DEFAULT_AUDIO_CODECS:
            if name in {"none", "copy"} or name in audio_names:
                self.audio_codec_combo.append(name, name)
        if self.audio_codec_combo.get_model() is not None and self.audio_codec_combo.set_active_id(selected_a):
            pass
        elif self.audio_codec_combo.set_active_id("aac"):
            pass
        else:
            self.audio_codec_combo.set_active(0)

    def _populate_pix_fmt_combo(self) -> None:
        selected = self.pix_fmt_combo.get_active_id() or "auto"

        self.pix_fmt_combo.remove_all()
        self.pix_fmt_combo.append("auto", _("auto"))
        for name in sorted({fmt.name for fmt in self._pixel_formats}, key=str.casefold):
            self.pix_fmt_combo.append(name, name)

        if not self.pix_fmt_combo.set_active_id(selected):
            self.pix_fmt_combo.set_active_id("auto")

    def _set_default_output_path(self) -> None:
        source_path = self.source_entry.get_text().strip()
        container = self.container_combo.get_active_id() or "mkv"
        if source_path:
            source_dir = os.path.dirname(source_path) or os.getcwd()
            stem = os.path.splitext(os.path.basename(source_path))[0]
            self.output_entry.set_text(os.path.join(source_dir, f"{stem}-trim.{container}"))
            return

        videos_dir = os.path.join(os.path.expanduser("~"), "Videos")
        if not os.path.isdir(videos_dir):
            videos_dir = os.getcwd()
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.output_entry.set_text(os.path.join(videos_dir, f"edit-{ts}.mkv"))

    def _ensure_output_extension(self) -> None:
        output = self.output_entry.get_text().strip()
        if not output:
            return
        container = self.container_combo.get_active_id() or "mkv"
        root, ext = os.path.splitext(output)
        if ext.lower() == f".{container}":
            return
        if ext:
            output = root
        self.output_entry.set_text(output + f".{container}")

    def on_choose_source_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select source media file"))
        dialog.set_modal(True)
        parent = self.get_root()
        if not isinstance(parent, Gtk.Window):
            parent = None
        dialog.open(parent, None, self._on_source_selected)

    def _on_source_selected(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is None:
            return
        path = file.get_path()
        if path:
            self.source_entry.set_text(path)
            if not self.output_entry.get_text().strip():
                self._set_default_output_path()
            self.update_command_preview()

    def on_choose_output_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select export output file"))
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
            self.update_command_preview()

    def on_source_changed(self, _entry: Gtk.Entry) -> None:
        source = self.source_entry.get_text().strip()
        if source and not self.output_entry.get_text().strip():
            self._set_default_output_path()
        self._sync_source_metadata()
        self.update_command_preview()
        if self._preview_running:
            self.start_preview()

    def on_container_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._ensure_output_extension()
        self.update_command_preview()

    def on_output_mode_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._sync_output_mode_widgets()
        self.update_command_preview()

    def on_match_fps_toggled(self, _button: Gtk.CheckButton) -> None:
        self.output_fps_spin.set_sensitive(self._output_mode() == "reencode" and (not self.match_fps_check.get_active()))
        self.update_command_preview()

    def on_settings_changed(self, _widget) -> None:
        self.update_command_preview()

    def on_trim_changed(self, _widget, handle: str = "") -> None:
        if self._updating_trim_controls or self._source_duration_seconds <= 0.0:
            return

        start = self._trim_start_seconds()
        end = self._trim_end_seconds()
        min_gap = 0.04

        self._updating_trim_controls = True
        try:
            if start > end - min_gap:
                if handle == "start":
                    self.trim_range_bar.set_end_value(min(self._source_duration_seconds, start + min_gap))
                else:
                    self.trim_range_bar.set_start_value(max(0.0, end - min_gap))
        finally:
            self._updating_trim_controls = False

        self._update_trim_summary()
        self.update_command_preview()
        if self._preview_running:
            self.pause_preview()
            if handle == "end":
                self._seek_preview(self._trim_end_seconds())
            else:
                self._seek_preview(self._trim_start_seconds())
        else:
            self._clamp_preview_to_trim()

    def on_trim_set_start_clicked(self, _button: Gtk.Button) -> None:
        current = self._current_preview_position_seconds()
        self.trim_range_bar.set_start_value(current)
        self.on_trim_changed(self.trim_range_bar, "start")

    def on_trim_set_end_clicked(self, _button: Gtk.Button) -> None:
        current = self._current_preview_position_seconds()
        self.trim_range_bar.set_end_value(current)
        self.on_trim_changed(self.trim_range_bar, "end")

    def _nudge_trim_handle(self, handle: str, direction: int, seconds: float | None = None) -> bool:
        if self._source_duration_seconds <= 0.0:
            return False
        step = seconds if seconds is not None else self._frame_step_seconds()
        delta = float(direction) * step
        if handle == "end":
            self.trim_range_bar.set_end_value(self._trim_end_seconds() + delta)
            self.on_trim_changed(self.trim_range_bar, "end")
        else:
            self.trim_range_bar.set_start_value(self._trim_start_seconds() + delta)
            self.on_trim_changed(self.trim_range_bar, "start")
        return True

    def on_trim_start_prev_frame_clicked(self, _button: Gtk.Button) -> None:
        self._nudge_trim_handle("start", -1)

    def on_trim_start_next_frame_clicked(self, _button: Gtk.Button) -> None:
        self._nudge_trim_handle("start", 1)

    def on_trim_end_prev_frame_clicked(self, _button: Gtk.Button) -> None:
        self._nudge_trim_handle("end", -1)

    def on_trim_end_next_frame_clicked(self, _button: Gtk.Button) -> None:
        self._nudge_trim_handle("end", 1)

    def on_preview_rebuild_setting_changed(self, _widget) -> None:
        self.update_command_preview()
        if self._preview_running:
            self.start_preview()

    def on_video_balance_changed(self, _widget) -> None:
        self.update_command_preview()

        if self._preview_video_balance is None:
            return
        try:
            self._preview_video_balance.set_property("brightness", float(self.brightness_scale.get_value()))
            self._preview_video_balance.set_property("contrast", float(self.contrast_scale.get_value()))
            self._preview_video_balance.set_property("saturation", float(self.saturation_scale.get_value()))
        except Exception:
            return

    def on_audio_filter_changed(self, _widget) -> None:
        self.update_command_preview()
        self.on_preview_audio_changed(self.preview_volume_scale)

    def on_audio_delay_changed(self, _spin: Gtk.SpinButton) -> None:
        self.update_command_preview()
        self._apply_preview_av_offset()

    def _apply_preview_av_offset(self) -> None:
        pipeline = self._preview_pipeline
        if pipeline is None:
            return
        if pipeline.find_property("av-offset") is None:
            return
        delay_ms = int(self.audio_delay_spin.get_value())
        try:
            pipeline.set_property("av-offset", int(delay_ms * 1_000_000))
        except Exception:
            return

    def _current_preview_position_seconds(self) -> float:
        pipeline = self._preview_pipeline
        if pipeline is None or Gst is None:
            return self._trim_start_seconds()
        try:
            ok, pos = pipeline.query_position(Gst.Format.TIME)
        except Exception:
            return self._trim_start_seconds()
        if not ok:
            return self._trim_start_seconds()
        return max(0.0, float(pos) / float(Gst.SECOND))

    def _seek_preview(self, seconds: float) -> bool:
        pipeline = self._preview_pipeline
        if pipeline is None or Gst is None:
            return False
        target = max(0.0, min(seconds, self._source_duration_seconds or seconds))
        try:
            pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                int(target * Gst.SECOND),
            )
            return True
        except Exception:
            return False

    def _frame_step_seconds(self) -> float:
        if self._source_fps > 0.001:
            return max(1.0 / self._source_fps, 0.001)
        return 1.0 / 25.0

    def _step_preview(self, direction: int, seconds: float | None = None) -> bool:
        if direction == 0 or self._preview_pipeline is None:
            return False

        step = seconds if seconds is not None else self._frame_step_seconds()
        current = self._current_preview_position_seconds()
        min_pos = self._trim_start_seconds()
        max_pos = self._trim_end_seconds() if self._trim_end_seconds() > min_pos else self._source_duration_seconds
        target = max(min_pos, min(max_pos, current + (step * direction)))

        if Gst is not None and not self._preview_paused:
            try:
                self._preview_pipeline.set_state(Gst.State.PAUSED)
                self._preview_paused = True
                self._set_preview_state_buttons()
                self._stop_preview_trim_watch()
                self._set_preview_status(_("Preview paused."))
            except Exception:
                pass

        if not self._seek_preview(target):
            return False

        self._updating_seek_scale = True
        try:
            self.preview_seek_scale.set_value(target)
            self.preview_position_label.set_text(_format_time_label(target))
        finally:
            self._updating_seek_scale = False
        return True

    def _clamp_preview_to_trim(self) -> None:
        if not self._preview_running:
            return
        current = self._current_preview_position_seconds()
        start = self._trim_start_seconds()
        end = self._trim_end_seconds()
        if current < start or current > end:
            self._seek_preview(start)

    def _set_preview_state_buttons(self) -> None:
        self.preview_start_button.set_sensitive(not self._preview_running or self._preview_paused)
        self.preview_pause_button.set_sensitive(self._preview_running and (not self._preview_paused))
        self.preview_stop_button.set_sensitive(self._preview_running)
        self.preview_seek_scale.set_sensitive(self._preview_running)

    def _start_preview_seek_watch(self) -> None:
        self._stop_preview_seek_watch()
        if not self._preview_running:
            return
        self._preview_seek_watch_id = GLib.timeout_add(100, self._on_preview_seek_watch)

    def _stop_preview_seek_watch(self) -> None:
        if self._preview_seek_watch_id is not None:
            try:
                GLib.source_remove(self._preview_seek_watch_id)
            except Exception:
                pass
        self._preview_seek_watch_id = None

    def _on_preview_seek_watch(self) -> bool:
        if not self._preview_running:
            self._preview_seek_watch_id = None
            return False

        pos = self._current_preview_position_seconds()
        self._updating_seek_scale = True
        try:
            self.preview_seek_scale.set_value(pos)
            self.preview_position_label.set_text(_format_time_label(pos))
        finally:
            self._updating_seek_scale = False
        return True

    def on_preview_seek_changed(self, _scale: Gtk.Scale) -> None:
        if self._updating_seek_scale or not self._preview_running:
            return
        target = float(self.preview_seek_scale.get_value())
        if not self._preview_paused:
            self.pause_preview()
        self._seek_preview(target)
        self.preview_position_label.set_text(_format_time_label(target))

    def on_preview_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if not self._preview_running:
            return False

        if keyval == Gdk.KEY_space:
            if self._preview_paused:
                self.start_preview()
            else:
                self.pause_preview()
            return True
        return False

    def _seek_preview_to_trim_start(self) -> bool:
        if self._preview_pipeline is None or not self._preview_running or Gst is None:
            return False

        start = self._trim_start_seconds()
        if start <= 0.0:
            return False

        self._seek_preview(start)
        return False

    def _start_preview_trim_watch(self) -> None:
        self._stop_preview_trim_watch()
        if self._trim_duration_seconds() <= 0.0:
            return
        self._preview_trim_watch_id = GLib.timeout_add(200, self._on_preview_trim_watch)

    def _stop_preview_trim_watch(self) -> None:
        if self._preview_trim_watch_id is not None:
            try:
                GLib.source_remove(self._preview_trim_watch_id)
            except Exception:
                pass
        self._preview_trim_watch_id = None

    def _on_preview_trim_watch(self) -> bool:
        pipeline = self._preview_pipeline
        if pipeline is None or not self._preview_running or Gst is None:
            self._preview_trim_watch_id = None
            return False

        end = self._trim_end_seconds()
        if end <= 0.0:
            return True

        try:
            ok, pos = pipeline.query_position(Gst.Format.TIME)
        except Exception:
            return True
        if not ok:
            return True

        if (pos / Gst.SECOND) >= max(0.0, end - 0.02):
            self._set_preview_status(_("Preview reached trim end."))
            self.pause_preview(seek_to_end=True)
            return False
        return True

    def _preview_effective_volume(self) -> float:
        monitor_volume = float(self.preview_volume_scale.get_value())
        gain_db = float(self.audio_gain_spin.get_value())
        gain_linear = math.pow(10.0, gain_db / 20.0)
        return monitor_volume * gain_linear

    def on_preview_audio_changed(self, _widget) -> None:
        if self._preview_volume_element is None:
            return
        try:
            muted = bool(self.preview_mute_check.get_active())
            self._preview_volume_element.set_property("mute", muted)
            self._preview_volume_element.set_property("volume", self._preview_effective_volume())
        except Exception:
            return

    def _build_preview_video_filter_bin(self):
        if not GST_AVAILABLE or Gst is None:
            return None

        parts: list[str] = []
        if self.deinterlace_check.get_active():
            parts.append("deinterlace")

        if self.denoise_check.get_active():
            if Gst.ElementFactory.find("videodenoise") is not None:
                parts.append("videodenoise name=edit_denoise")
            else:
                self._append_status_warning(_("GStreamer videodenoise not available; preview denoise disabled."))

        parts.append("videoconvert")
        parts.append(
            "videobalance name=edit_vbal "
            f"brightness={float(self.brightness_scale.get_value()):.3f} "
            f"contrast={float(self.contrast_scale.get_value()):.3f} "
            f"saturation={float(self.saturation_scale.get_value()):.3f}"
        )

        desc = " ! ".join(parts)
        try:
            bin_obj = Gst.parse_bin_from_description(desc, True)
        except Exception:
            self._append_status_warning(_("Could not apply preview filters; using source view."))
            return None

        denoise = bin_obj.get_by_name("edit_denoise")
        if denoise is not None:
            strength = float(self.denoise_strength_spin.get_value())
            if denoise.find_property("sigma") is not None:
                try:
                    denoise.set_property("sigma", strength)
                except Exception:
                    pass

        self._preview_video_balance = bin_obj.get_by_name("edit_vbal")
        return bin_obj

    def on_preview_start_clicked(self, _button: Gtk.Button) -> None:
        self.start_preview()

    def on_preview_pause_clicked(self, _button: Gtk.Button) -> None:
        self.pause_preview()

    def on_preview_stop_clicked(self, _button: Gtk.Button) -> None:
        self.stop_preview()

    def start_preview(self) -> None:
        if self._preview_running and self._preview_pipeline is not None and Gst is not None:
            try:
                self._preview_pipeline.set_state(Gst.State.PLAYING)
                self._preview_paused = False
                self._set_preview_state_buttons()
                self._set_preview_status(_("Preview running."))
                self._start_preview_trim_watch()
                self._start_preview_seek_watch()
                self.preview_picture.grab_focus()
            except Exception:
                pass
            return

        self.stop_preview()

        if not GST_AVAILABLE or not GST_GTK4PAINTABLE_AVAILABLE:
            self._set_preview_status(
                _("Live preview requires GStreamer with gtk4paintablesink.")
            )
            return

        source_path = self.source_entry.get_text().strip()
        if not source_path:
            self._set_preview_status(_("Choose a source file first."))
            return
        if not os.path.isfile(source_path):
            self._set_preview_status(_("Source file not found."))
            return

        playbin = Gst.ElementFactory.make("playbin", "edit-preview")
        vsink = Gst.ElementFactory.make("gtk4paintablesink", "vsink")
        if playbin is None or vsink is None:
            self._set_preview_status(_("Missing GStreamer elements for preview."))
            return

        playbin.set_property("uri", Gio.File.new_for_path(source_path).get_uri())
        playbin.set_property("video-sink", vsink)

        audio_bin = None
        try:
            audio_bin = Gst.parse_bin_from_description(
                "volume name=edit_avolume ! autoaudiosink sync=false async=false",
                True,
            )
        except Exception:
            audio_bin = None

        if audio_bin is not None:
            try:
                playbin.set_property("audio-sink", audio_bin)
            except Exception:
                audio_bin = None

        if audio_bin is None:
            self._append_status_warning(_("Audio monitor sink unavailable; previewing video only."))
            self._preview_volume_element = None
        else:
            self._preview_volume_element = audio_bin.get_by_name("edit_avolume")

        video_filter = self._build_preview_video_filter_bin()
        if video_filter is not None and playbin.find_property("video-filter") is not None:
            try:
                playbin.set_property("video-filter", video_filter)
            except Exception:
                pass

        paintable = vsink.get_property("paintable")
        self.preview_picture.set_paintable(paintable)

        bus = playbin.get_bus()
        if bus is None:
            playbin.set_state(Gst.State.NULL)
            self._set_preview_status(_("Could not start preview bus."))
            return

        bus.add_signal_watch()
        handler_id = bus.connect("message", self._on_preview_bus_message)

        state_ret = playbin.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            bus.disconnect(handler_id)
            bus.remove_signal_watch()
            playbin.set_state(Gst.State.NULL)
            self._set_preview_status(_("Could not start preview playback."))
            return

        self._preview_pipeline = playbin
        self._preview_bus = bus
        self._preview_bus_handler_id = handler_id
        self._preview_running = True
        self._preview_paused = False
        self._preview_source_uri = Gio.File.new_for_path(source_path).get_uri()

        self._set_preview_state_buttons()
        self._set_preview_status(_("Preview running."))

        self.on_video_balance_changed(self.brightness_scale)
        self.on_preview_audio_changed(self.preview_volume_scale)
        self._apply_preview_av_offset()
        GLib.timeout_add(120, self._seek_preview_to_trim_start)
        self._start_preview_trim_watch()
        self._start_preview_seek_watch()
        self.preview_picture.grab_focus()

    def pause_preview(self, seek_to_end: bool = False) -> None:
        pipeline = self._preview_pipeline
        if pipeline is None or not self._preview_running or Gst is None:
            return
        try:
            if seek_to_end:
                self._seek_preview(self._trim_end_seconds())
            pipeline.set_state(Gst.State.PAUSED)
            self._preview_paused = True
            self._set_preview_state_buttons()
            self._stop_preview_trim_watch()
            self._set_preview_status(_("Preview paused."))
        except Exception:
            return

    def _on_preview_bus_message(self, _bus, message) -> None:
        mtype = message.type
        if mtype == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            detail = str(err)
            if dbg:
                detail += f" ({dbg})"
            self._set_preview_status(_("Preview error: ") + detail)
            self.stop_preview()
            return

        if mtype == Gst.MessageType.EOS:
            self._set_preview_status(_("Preview reached end of file."))
            self.stop_preview()
            return

    def stop_preview(self) -> None:
        if not self._preview_running:
            self._stop_preview_trim_watch()
            self._stop_preview_seek_watch()
            self.preview_picture.set_paintable(None)
            self._set_preview_state_buttons()
            return

        pipeline = self._preview_pipeline
        bus = self._preview_bus
        handler_id = self._preview_bus_handler_id

        self._preview_pipeline = None
        self._preview_bus = None
        self._preview_bus_handler_id = None
        self._preview_volume_element = None
        self._preview_video_balance = None
        self._preview_running = False
        self._preview_paused = False
        self._preview_source_uri = ""
        self._stop_preview_trim_watch()
        self._stop_preview_seek_watch()

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
        self._set_preview_state_buttons()
        self._set_preview_status(_("Preview stopped."))

    def _build_export_command(self, auto_fix_compatibility: bool = False) -> tuple[list[str], list[str]]:
        warnings: list[str] = []

        if not self._ffmpeg_command:
            raise RuntimeError(_("FFmpeg is not available."))

        source_path = self.source_entry.get_text().strip()
        if not source_path:
            raise RuntimeError(_("Choose a source file."))
        if not os.path.isfile(source_path):
            raise RuntimeError(_("Source file not found."))

        output_path = self.output_entry.get_text().strip()
        if not output_path:
            raise RuntimeError(_("Choose an output file."))

        self._ensure_output_extension()
        output_path = self.output_entry.get_text().strip()

        container = self.container_combo.get_active_id() or "mkv"
        vcodec = self.video_codec_combo.get_active_id() or "libx264"
        acodec = self.audio_codec_combo.get_active_id() or "aac"
        output_mode = self._output_mode()

        if auto_fix_compatibility and container == "mp4":
            if vcodec == "ffv1" or acodec.startswith("pcm_"):
                container = "mkv"
                self.container_combo.set_active_id("mkv")
                self._ensure_output_extension()
                output_path = self.output_entry.get_text().strip()
                warnings.append(_("Container auto-switched to MKV for selected codec combination."))

        delay_ms = int(self.audio_delay_spin.get_value())
        trim_start = self._trim_start_seconds()
        trim_end = self._trim_end_seconds()
        trim_active = self._source_duration_seconds > 0.0 and (
            trim_start >= 0.001 or trim_end <= (self._source_duration_seconds - 0.001)
        )
        trim_duration = max(0.0, trim_end - trim_start)

        if trim_active and trim_duration <= 0.0:
            raise RuntimeError(_("Trim end must be after trim start."))

        cmd = list(self._ffmpeg_command)
        cmd += ["-hide_banner", "-y"]

        def append_trimmed_input(target: list[str]) -> None:
            if trim_active:
                if trim_start >= 0.001:
                    target += ["-ss", f"{trim_start:.3f}"]
                if trim_end <= (self._source_duration_seconds - 0.001):
                    target += ["-to", f"{trim_end:.3f}"]
            target += ["-i", source_path]

        if output_mode == "copy":
            has_audio = bool(self._source_audio_codec)
            if self._source_container and container != self._source_container:
                warnings.append(
                    _("Changing container in Keep source streams mode may fail for some source codecs.")
                )
            if delay_ms != 0:
                warnings.append(_("Audio delay is ignored in Keep source streams mode. Choose Re-encode to apply it."))
            if self.deinterlace_check.get_active() or self.denoise_check.get_active():
                warnings.append(_("Video cleanup is ignored in Keep source streams mode. Choose Re-encode to apply it."))
            if (
                abs(float(self.brightness_scale.get_value())) >= 0.001
                or abs(float(self.contrast_scale.get_value()) - 1.0) >= 0.001
                or abs(float(self.saturation_scale.get_value()) - 1.0) >= 0.001
            ):
                warnings.append(_("Video color corrections are ignored in Keep source streams mode."))
            if (
                abs(float(self.audio_gain_spin.get_value())) >= 0.01
                or int(self.highpass_spin.get_value()) > 0
                or int(self.lowpass_spin.get_value()) > 0
            ):
                warnings.append(_("Audio filters are ignored in Keep source streams mode."))

            append_trimmed_input(cmd)
            cmd += ["-map", "0:v:0?"]
            if has_audio:
                cmd += ["-map", "0:a:0?"]
            cmd += ["-c:v", "copy"]
            if has_audio:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-an"]

            if trim_active:
                warnings.append(_("Trim uses stream copy mode. Cuts may snap to the nearest keyframe."))

            cmd += [output_path]
            return cmd, warnings

        has_audio = acodec != "none"

        if has_audio and delay_ms != 0:
            append_trimmed_input(cmd)
            cmd += ["-itsoffset", f"{delay_ms / 1000.0:+.3f}"]
            append_trimmed_input(cmd)
            video_input_idx = 0
            audio_input_idx = 1
            warnings.append(_("Audio delay uses dual-input remap mode."))
        else:
            append_trimmed_input(cmd)
            video_input_idx = 0
            audio_input_idx = 0

        cmd += ["-map", f"{video_input_idx}:v:0?"]
        if has_audio:
            cmd += ["-map", f"{audio_input_idx}:a:0?"]

        vf_filters: list[str] = []
        if self.deinterlace_check.get_active():
            vf_filters.append("yadif=0:-1:0")

        if self.denoise_check.get_active():
            strength = float(self.denoise_strength_spin.get_value())
            luma = max(1.0, 4.0 * strength)
            chroma = max(1.0, 3.0 * strength)
            vf_filters.append(f"hqdn3d={luma:.1f}:{chroma:.1f}:6:6")

        brightness = float(self.brightness_scale.get_value())
        contrast = float(self.contrast_scale.get_value())
        saturation = float(self.saturation_scale.get_value())
        if abs(brightness) >= 0.001 or abs(contrast - 1.0) >= 0.001 or abs(saturation - 1.0) >= 0.001:
            vf_filters.append(
                f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f}"
            )

        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        if not self.match_fps_check.get_active():
            out_fps = float(self.output_fps_spin.get_value())
            cmd += ["-r", f"{out_fps:g}"]

        cmd += ["-c:v", vcodec]
        if vcodec != "copy":
            vbitrate = self.video_bitrate_entry.get_text().strip()
            if vbitrate and vcodec not in {"ffv1"}:
                cmd += ["-b:v", vbitrate]

            pix_fmt = self.pix_fmt_combo.get_active_id() or "auto"
            if pix_fmt != "auto":
                cmd += ["-pix_fmt", pix_fmt]

        if has_audio:
            cmd += ["-c:a", acodec]
            if acodec != "copy":
                cmd += ["-ar", str(int(self.sample_rate_spin.get_value()))]
                cmd += ["-ac", self.channels_combo.get_active_id() or "2"]

                abitrate = self.audio_bitrate_entry.get_text().strip()
                if abitrate and acodec not in {"pcm_s16le"}:
                    cmd += ["-b:a", abitrate]

                af_filters: list[str] = []
                gain_db = float(self.audio_gain_spin.get_value())
                if abs(gain_db) >= 0.01:
                    af_filters.append(f"volume={gain_db:+.1f}dB")

                hp = int(self.highpass_spin.get_value())
                lp = int(self.lowpass_spin.get_value())
                if hp > 0:
                    af_filters.append(f"highpass=f={hp}")
                if lp > 0:
                    af_filters.append(f"lowpass=f={lp}")

                if af_filters:
                    cmd += ["-af", ",".join(af_filters)]
        else:
            cmd += ["-an"]

        cmd += [output_path]
        return cmd, warnings

    def update_command_preview(self) -> None:
        try:
            cmd, warnings = self._build_export_command()
        except RuntimeError as exc:
            self.command_buffer.set_text(str(exc))
            if not self.runner.running:
                self._set_status_text("")
            return

        self.command_buffer.set_text(_shell_preview(cmd))
        if warnings:
            self._set_status_text("\n".join(warnings))
        elif not self.runner.running:
            self._set_status_text("")

    def on_start_clicked(self, _button: Gtk.Button) -> None:
        if self.runner.running:
            return

        try:
            cmd, warnings = self._build_export_command(auto_fix_compatibility=True)
        except RuntimeError as exc:
            self._set_status_text(str(exc))
            return

        self._clear_log()
        self._append_log(_("Running:") + " " + _shell_preview(cmd))
        if warnings:
            self._set_status_text("\n".join(warnings))

        try:
            self.runner.start(cmd)
        except Exception as exc:
            self._set_status_text(str(exc))
            return

        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        self.update_command_preview()

    def on_stop_clicked(self, _button: Gtk.Button) -> None:
        self.runner.stop()

    def _on_runner_output(self, line: str) -> None:
        GLib.idle_add(self._append_log, line)

    def _on_runner_exit(self, rc: int) -> None:
        GLib.idle_add(self._handle_runner_exit, rc)

    def _handle_runner_exit(self, rc: int) -> None:
        self.start_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self._set_status_text(_("Export finished with code ") + str(rc))
        self.update_command_preview()

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")

    def _append_log(self, line: str) -> bool:
        start_iter = self.log_buffer.get_start_iter()
        self.log_buffer.insert(start_iter, line + "\n")
        return False

    def _set_preview_status(self, text: str) -> None:
        self.preview_status_label.set_text(text)

    def _append_status_warning(self, text: str) -> None:
        existing = self.status_label.get_text().strip()
        if not existing:
            self._set_status_text(text)
            return
        self._set_status_text(existing + "\n" + text)

    def _set_status_text(self, text: str) -> None:
        self.status_label.set_text(text)
        self.status_label.set_visible(bool(text.strip()))

    def shutdown(self) -> None:
        self.stop_preview()
        self.runner.stop()
