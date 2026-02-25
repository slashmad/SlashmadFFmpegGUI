from __future__ import annotations

import math
import os
import shlex
import time
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


EDIT_CONTAINERS = ["mkv", "mp4", "mov"]
DEFAULT_VIDEO_CODECS = ["copy", "libx264", "ffv1"]
DEFAULT_AUDIO_CODECS = ["none", "copy", "aac", "pcm_s16le"]


def _shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


class EditPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._ffmpeg_command: list[str] | None = None
        self._encoders: list[EncoderInfo] = []
        self._pixel_formats: list[PixelFormat] = []

        self._preview_pipeline = None
        self._preview_bus = None
        self._preview_bus_handler_id: int | None = None
        self._preview_volume_element = None
        self._preview_video_balance = None
        self._preview_running = False
        self._preview_source_uri = ""

        self.runner = FFmpegRunner(self._on_runner_output, self._on_runner_exit)

        self._build_ui()
        self._set_default_output_path()

    def _build_ui(self) -> None:
        info_frame = Gtk.Frame(label=_("Edit Status"))
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_margin_top(8)
        info_box.set_margin_bottom(8)
        info_box.set_margin_start(8)
        info_box.set_margin_end(8)
        info_frame.set_child(info_box)
        self.append(info_frame)

        self.info_label = Gtk.Label(label=_("Load a captured file to preview and export corrections."))
        self.info_label.set_xalign(0)
        self.info_label.add_css_class("dim-label")
        info_box.append(self.info_label)

        source_frame = Gtk.Frame(label=_("Source"))
        source_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        source_box.set_margin_top(8)
        source_box.set_margin_bottom(8)
        source_box.set_margin_start(8)
        source_box.set_margin_end(8)
        source_frame.set_child(source_box)
        self.append(source_frame)

        source_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        source_box.append(source_row)

        self.source_entry = Gtk.Entry()
        self.source_entry.set_placeholder_text(_("Input media file"))
        self.source_entry.set_hexpand(True)
        self.source_entry.connect("changed", self.on_source_changed)
        source_row.append(self.source_entry)

        source_pick_button = Gtk.Button(label=_("Choose"))
        source_pick_button.connect("clicked", self.on_choose_source_clicked)
        source_row.append(source_pick_button)

        source_hint = Gtk.Label(
            label=_("Use this tab for post-capture fixes: sync, picture corrections, denoise and export.")
        )
        source_hint.set_xalign(0)
        source_hint.set_wrap(True)
        source_hint.add_css_class("dim-label")
        source_box.append(source_hint)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.append(top_row)

        preview_frame = Gtk.Frame(label=_("Live Preview"))
        preview_frame.set_hexpand(True)
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_box.set_margin_top(8)
        preview_box.set_margin_bottom(8)
        preview_box.set_margin_start(8)
        preview_box.set_margin_end(8)
        preview_frame.set_child(preview_box)
        top_row.append(preview_frame)

        self.preview_picture = Gtk.Picture()
        self.preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview_picture.set_size_request(520, 300)
        preview_picture_frame = Gtk.Frame()
        preview_picture_frame.set_child(self.preview_picture)
        preview_box.append(preview_picture_frame)

        preview_ctl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.append(preview_ctl_row)

        self.preview_start_button = Gtk.Button(label=_("Start preview"))
        self.preview_start_button.connect("clicked", self.on_preview_start_clicked)
        preview_ctl_row.append(self.preview_start_button)

        self.preview_stop_button = Gtk.Button(label=_("Stop preview"))
        self.preview_stop_button.set_sensitive(False)
        self.preview_stop_button.connect("clicked", self.on_preview_stop_clicked)
        preview_ctl_row.append(self.preview_stop_button)

        self.preview_mute_check = Gtk.CheckButton(label=_("Mute"))
        self.preview_mute_check.connect("toggled", self.on_preview_audio_changed)
        preview_ctl_row.append(self.preview_mute_check)

        volume_label = Gtk.Label(label=_("Volume"))
        preview_ctl_row.append(volume_label)

        self.preview_volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.01)
        self.preview_volume_scale.set_value(1.0)
        self.preview_volume_scale.set_digits(2)
        self.preview_volume_scale.set_hexpand(True)
        self.preview_volume_scale.connect("value-changed", self.on_preview_audio_changed)
        preview_ctl_row.append(self.preview_volume_scale)

        self.preview_status_label = Gtk.Label(label="")
        self.preview_status_label.set_xalign(0)
        self.preview_status_label.add_css_class("dim-label")
        preview_box.append(self.preview_status_label)

        adjust_frame = Gtk.Frame(label=_("Corrections"))
        adjust_frame.set_hexpand(True)
        adjust_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        adjust_box.set_margin_top(8)
        adjust_box.set_margin_bottom(8)
        adjust_box.set_margin_start(8)
        adjust_box.set_margin_end(8)
        adjust_frame.set_child(adjust_box)
        top_row.append(adjust_frame)

        self.audio_delay_spin = Gtk.SpinButton.new_with_range(-2000, 2000, 10)
        self.audio_delay_spin.set_value(0)
        self.audio_delay_spin.connect("value-changed", self.on_audio_delay_changed)
        self._add_setting_row(
            adjust_box,
            _("Audio delay (ms)"),
            self.audio_delay_spin,
            _("Positive values delay audio, negative values advance audio."),
        )

        self.deinterlace_check = Gtk.CheckButton(label=_("Enable"))
        self.deinterlace_check.connect("toggled", self.on_preview_rebuild_setting_changed)
        self._add_setting_row(
            adjust_box,
            _("Deinterlace"),
            self.deinterlace_check,
            _("Removes combing artifacts from interlaced VHS fields."),
        )

        denoise_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.denoise_check = Gtk.CheckButton(label=_("Enable"))
        self.denoise_check.connect("toggled", self.on_preview_rebuild_setting_changed)
        denoise_row.append(self.denoise_check)

        self.denoise_strength_spin = Gtk.SpinButton.new_with_range(0.5, 3.0, 0.1)
        self.denoise_strength_spin.set_value(1.0)
        self.denoise_strength_spin.set_digits(1)
        self.denoise_strength_spin.connect("value-changed", self.on_preview_rebuild_setting_changed)
        denoise_row.append(self.denoise_strength_spin)
        self._add_setting_row(
            adjust_box,
            _("Denoise"),
            denoise_row,
            _("Temporal-spatial cleanup (hqdn3d). Higher values remove more noise but can blur detail."),
        )

        self.brightness_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -0.20, 0.20, 0.01)
        self.brightness_scale.set_value(0.0)
        self.brightness_scale.set_digits(2)
        self.brightness_scale.set_hexpand(True)
        self.brightness_scale.connect("value-changed", self.on_video_balance_changed)
        self._add_setting_row(
            adjust_box,
            _("Brightness"),
            self.brightness_scale,
            _("Lifts or lowers luma. Keep low to avoid clipping highlights/shadows."),
        )

        self.contrast_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.50, 2.00, 0.01)
        self.contrast_scale.set_value(1.0)
        self.contrast_scale.set_digits(2)
        self.contrast_scale.set_hexpand(True)
        self.contrast_scale.connect("value-changed", self.on_video_balance_changed)
        self._add_setting_row(
            adjust_box,
            _("Contrast"),
            self.contrast_scale,
            _("Adjusts separation between dark and bright parts of the image."),
        )

        self.saturation_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.01)
        self.saturation_scale.set_value(1.0)
        self.saturation_scale.set_digits(2)
        self.saturation_scale.set_hexpand(True)
        self.saturation_scale.connect("value-changed", self.on_video_balance_changed)
        self._add_setting_row(
            adjust_box,
            _("Saturation"),
            self.saturation_scale,
            _("Controls color intensity. Lower values reduce color noise, higher values boost color."),
        )

        self.audio_gain_spin = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.5)
        self.audio_gain_spin.set_value(0.0)
        self.audio_gain_spin.set_digits(1)
        self.audio_gain_spin.connect("value-changed", self.on_audio_filter_changed)
        self._add_setting_row(
            adjust_box,
            _("Audio gain (dB)"),
            self.audio_gain_spin,
            _("Linear level adjustment. Use small steps to avoid clipping."),
        )

        hp_lp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.highpass_spin = Gtk.SpinButton.new_with_range(0, 500, 5)
        self.highpass_spin.set_value(0)
        self.highpass_spin.connect("value-changed", self.on_audio_filter_changed)
        hp_lp_row.append(Gtk.Label(label=_("HP")))
        hp_lp_row.append(self.highpass_spin)

        self.lowpass_spin = Gtk.SpinButton.new_with_range(0, 20000, 100)
        self.lowpass_spin.set_value(0)
        self.lowpass_spin.connect("value-changed", self.on_audio_filter_changed)
        hp_lp_row.append(Gtk.Label(label=_("LP")))
        hp_lp_row.append(self.lowpass_spin)
        self._add_setting_row(
            adjust_box,
            _("Audio filters (Hz)"),
            hp_lp_row,
            _("HP removes low rumble, LP removes high hiss. 0 disables each filter."),
        )

        output_frame = Gtk.Frame(label=_("Export Output"))
        output_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        output_box.set_margin_top(8)
        output_box.set_margin_bottom(8)
        output_box.set_margin_start(8)
        output_box.set_margin_end(8)
        output_frame.set_child(output_box)
        self.append(output_frame)

        out_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(out_row)

        self.output_entry = Gtk.Entry()
        self.output_entry.set_hexpand(True)
        self.output_entry.set_placeholder_text(_("Output file"))
        self.output_entry.connect("changed", self.on_settings_changed)
        out_row.append(self.output_entry)

        out_pick_button = Gtk.Button(label=_("Choose"))
        out_pick_button.connect("clicked", self.on_choose_output_clicked)
        out_row.append(out_pick_button)

        codec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(codec_row)

        codec_row.append(Gtk.Label(label=_("Container")))
        self.container_combo = Gtk.ComboBoxText()
        for container in EDIT_CONTAINERS:
            self.container_combo.append(container, container.upper())
        self.container_combo.set_active_id("mkv")
        self.container_combo.connect("changed", self.on_container_changed)
        codec_row.append(self.container_combo)

        codec_row.append(Gtk.Label(label=_("Video codec")))
        self.video_codec_combo = Gtk.ComboBoxText()
        self.video_codec_combo.connect("changed", self.on_settings_changed)
        codec_row.append(self.video_codec_combo)

        self.video_bitrate_entry = Gtk.Entry()
        self.video_bitrate_entry.set_placeholder_text(_("Video bitrate, e.g. 6M"))
        self.video_bitrate_entry.set_text("6M")
        self.video_bitrate_entry.connect("changed", self.on_settings_changed)
        codec_row.append(self.video_bitrate_entry)

        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(audio_row)

        audio_row.append(Gtk.Label(label=_("Audio codec")))
        self.audio_codec_combo = Gtk.ComboBoxText()
        self.audio_codec_combo.connect("changed", self.on_settings_changed)
        audio_row.append(self.audio_codec_combo)

        self.audio_bitrate_entry = Gtk.Entry()
        self.audio_bitrate_entry.set_placeholder_text(_("Audio bitrate, e.g. 192k"))
        self.audio_bitrate_entry.set_text("192k")
        self.audio_bitrate_entry.connect("changed", self.on_settings_changed)
        audio_row.append(self.audio_bitrate_entry)

        audio_row.append(Gtk.Label(label=_("Sample rate")))
        self.sample_rate_spin = Gtk.SpinButton.new_with_range(8000, 192000, 1000)
        self.sample_rate_spin.set_value(48000)
        self.sample_rate_spin.connect("value-changed", self.on_settings_changed)
        audio_row.append(self.sample_rate_spin)

        audio_row.append(Gtk.Label(label=_("Channels")))
        self.channels_combo = Gtk.ComboBoxText()
        self.channels_combo.append("1", "1")
        self.channels_combo.append("2", "2")
        self.channels_combo.set_active_id("2")
        self.channels_combo.connect("changed", self.on_settings_changed)
        audio_row.append(self.channels_combo)

        fps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(fps_row)

        self.match_fps_check = Gtk.CheckButton(label=_("Match source FPS"))
        self.match_fps_check.set_active(True)
        self.match_fps_check.connect("toggled", self.on_match_fps_toggled)
        fps_row.append(self.match_fps_check)

        self.output_fps_spin = Gtk.SpinButton.new_with_range(1, 120, 0.01)
        self.output_fps_spin.set_value(25.0)
        self.output_fps_spin.set_digits(3)
        self.output_fps_spin.set_sensitive(False)
        self.output_fps_spin.connect("value-changed", self.on_settings_changed)
        fps_row.append(self.output_fps_spin)

        fps_row.append(Gtk.Label(label=_("Pixel format")))
        self.pix_fmt_combo = Gtk.ComboBoxText()
        self.pix_fmt_combo.connect("changed", self.on_settings_changed)
        fps_row.append(self.pix_fmt_combo)

        cmd_frame = Gtk.Frame(label=_("Export command preview"))
        cmd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cmd_box.set_margin_top(8)
        cmd_box.set_margin_bottom(8)
        cmd_box.set_margin_start(8)
        cmd_box.set_margin_end(8)
        cmd_frame.set_child(cmd_box)
        self.append(cmd_frame)

        self.command_buffer = Gtk.TextBuffer()
        self.command_view = Gtk.TextView(buffer=self.command_buffer)
        self.command_view.set_editable(False)
        self.command_view.set_monospace(True)
        cmd_scroller = Gtk.ScrolledWindow()
        cmd_scroller.set_min_content_height(80)
        cmd_scroller.set_child(self.command_view)
        cmd_box.append(cmd_scroller)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cmd_box.append(action_row)

        self.start_button = Gtk.Button(label=_("Start export"))
        self.start_button.connect("clicked", self.on_start_clicked)
        action_row.append(self.start_button)

        self.stop_button = Gtk.Button(label=_("Stop export"))
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self.on_stop_clicked)
        action_row.append(self.stop_button)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0)
        cmd_box.append(self.status_label)

        log_frame = Gtk.Frame(label=_("Export log"))
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_box.set_margin_top(8)
        log_box.set_margin_bottom(8)
        log_box.set_margin_start(8)
        log_box.set_margin_end(8)
        log_frame.set_child(log_box)
        self.append(log_frame)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_vexpand(True)
        log_scroller.set_min_content_height(180)
        log_scroller.set_child(self.log_view)
        log_box.append(log_scroller)

        self._populate_codec_combos()
        self._populate_pix_fmt_combo()
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
        self.update_command_preview()
        if self._preview_running:
            self.start_preview()

    def on_container_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._ensure_output_extension()
        self.update_command_preview()

    def on_match_fps_toggled(self, _button: Gtk.CheckButton) -> None:
        self.output_fps_spin.set_sensitive(not self.match_fps_check.get_active())
        self.update_command_preview()

    def on_settings_changed(self, _widget) -> None:
        self.update_command_preview()

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

    def on_preview_stop_clicked(self, _button: Gtk.Button) -> None:
        self.stop_preview()

    def start_preview(self) -> None:
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
        self._preview_source_uri = Gio.File.new_for_path(source_path).get_uri()

        self.preview_start_button.set_sensitive(False)
        self.preview_stop_button.set_sensitive(True)
        self._set_preview_status(_("Preview running."))

        self.on_video_balance_changed(self.brightness_scale)
        self.on_preview_audio_changed(self.preview_volume_scale)
        self._apply_preview_av_offset()

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
            self.preview_picture.set_paintable(None)
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
        self._preview_source_uri = ""

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
        self.preview_start_button.set_sensitive(True)
        self.preview_stop_button.set_sensitive(False)
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

        if auto_fix_compatibility and container == "mp4":
            if vcodec == "ffv1" or acodec.startswith("pcm_"):
                container = "mkv"
                self.container_combo.set_active_id("mkv")
                self._ensure_output_extension()
                output_path = self.output_entry.get_text().strip()
                warnings.append(_("Container auto-switched to MKV for selected codec combination."))

        has_audio = acodec != "none"
        delay_ms = int(self.audio_delay_spin.get_value())

        cmd = list(self._ffmpeg_command)
        cmd += ["-hide_banner", "-y"]

        if has_audio and delay_ms != 0:
            cmd += ["-i", source_path, "-itsoffset", f"{delay_ms / 1000.0:+.3f}", "-i", source_path]
            video_input_idx = 0
            audio_input_idx = 1
            warnings.append(_("Audio delay uses dual-input remap mode."))
        else:
            cmd += ["-i", source_path]
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
                self.status_label.set_text("")
            return

        self.command_buffer.set_text(_shell_preview(cmd))
        if warnings:
            self.status_label.set_text("\n".join(warnings))
        elif not self.runner.running:
            self.status_label.set_text("")

    def on_start_clicked(self, _button: Gtk.Button) -> None:
        if self.runner.running:
            return

        try:
            cmd, warnings = self._build_export_command(auto_fix_compatibility=True)
        except RuntimeError as exc:
            self.status_label.set_text(str(exc))
            return

        self._clear_log()
        self._append_log(_("Running:") + " " + _shell_preview(cmd))
        if warnings:
            self.status_label.set_text("\n".join(warnings))

        try:
            self.runner.start(cmd)
        except Exception as exc:
            self.status_label.set_text(str(exc))
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
        self.status_label.set_text(_("Export finished with code ") + str(rc))
        self.update_command_preview()

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")

    def _append_log(self, line: str) -> bool:
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, line + "\n")
        self.log_view.scroll_to_iter(end_iter, 0.0, False, 0.0, 1.0)
        return False

    def _set_preview_status(self, text: str) -> None:
        self.preview_status_label.set_text(text)

    def _append_status_warning(self, text: str) -> None:
        existing = self.status_label.get_text().strip()
        if not existing:
            self.status_label.set_text(text)
            return
        self.status_label.set_text(existing + "\n" + text)

    def shutdown(self) -> None:
        self.stop_preview()
        self.runner.stop()

