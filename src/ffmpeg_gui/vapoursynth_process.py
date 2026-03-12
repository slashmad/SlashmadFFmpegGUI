from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gio, Gtk

from ffmpeg_gui.ffmpeg import EncoderInfo, PixelFormat
from ffmpeg_gui.i18n import _
from ffmpeg_gui.runner import FFmpegRunner
from ffmpeg_gui.ui import bind_objects, compact_widget, load_builder, require_object
from ffmpeg_gui.vs_stack import (
    default_vs_plugin_dirs,
    managed_vs_plugin_dir,
    vs_runtime_prefix_shell,
)


PROCESS_CONTAINERS = ["mkv", "mp4", "mov"]
PROCESS_VIDEO_CODECS = ["libx264", "libx265", "ffv1", "h264_nvenc", "hevc_nvenc", "h264_vaapi", "hevc_vaapi"]
PROCESS_X26X_PRESETS: list[tuple[str, str]] = [
    ("auto", _("auto")),
    ("ultrafast", "ultrafast"),
    ("superfast", "superfast"),
    ("veryfast", "veryfast"),
    ("faster", "faster"),
    ("fast", "fast"),
    ("medium", "medium"),
    ("slow", "slow"),
    ("slower", "slower"),
    ("veryslow", "veryslow"),
    ("placebo", "placebo"),
]
PROCESS_AUDIO_CODECS = ["none", "copy", "aac", "pcm_s16le", "libopus", "ac3", "mp2"]
PROCESS_OUTPUT_MODES: list[tuple[str, str]] = [
    ("source", _("Keep source (container + audio)")),
    ("custom", _("Custom export")),
]
PROCESS_VIDEO_RATE_MODES: list[tuple[str, str]] = [
    ("bitrate", _("Bitrate")),
    ("crf", _("CRF")),
]
PROCESS_DEINTERLACE_MODES: list[tuple[str, str]] = [
    ("off", _("Off")),
    ("bwdif", _("BWDIF (double-rate)")),
    ("qtgmc_placebo", _("QTGMC Placebo")),
    ("qtgmc_very_slow", _("QTGMC Very Slow")),
    ("qtgmc_slower", _("QTGMC Slower")),
    ("qtgmc_slow", _("QTGMC Slow")),
    ("qtgmc_medium", _("QTGMC Medium")),
    ("qtgmc_fast", _("QTGMC Fast")),
    ("qtgmc_faster", _("QTGMC Faster")),
    ("qtgmc_very_fast", _("QTGMC Very Fast")),
    ("qtgmc_super_fast", _("QTGMC Super Fast")),
    ("qtgmc_ultra_fast", _("QTGMC Ultra Fast")),
    ("qtgmc_draft", _("QTGMC Draft")),
]
PROCESS_FIELD_ORDERS: list[tuple[str, str]] = [
    ("auto", _("Auto")),
    ("tff", _("TFF")),
    ("bff", _("BFF")),
]
PROCESS_VHS_CLEANUP_MODES: list[tuple[str, str]] = [
    ("off", _("Off")),
    ("light", _("Light")),
    ("medium", _("Medium")),
    ("advanced", _("Advanced")),
]
QTGMC_PRESET_BY_MODE: dict[str, str] = {
    "qtgmc_placebo": "Placebo",
    "qtgmc_very_slow": "Very Slow",
    "qtgmc_slower": "Slower",
    "qtgmc_slow": "Slow",
    "qtgmc_medium": "Medium",
    "qtgmc_fast": "Fast",
    "qtgmc_faster": "Faster",
    "qtgmc_very_fast": "Very Fast",
    "qtgmc_super_fast": "Super Fast",
    "qtgmc_ultra_fast": "Ultra Fast",
    "qtgmc_draft": "Draft",
}
QTGMC_MATCH_PRESET_VALUES: list[str] = [
    "Placebo",
    "Very Slow",
    "Slower",
    "Slow",
    "Medium",
    "Fast",
    "Faster",
    "Very Fast",
    "Super Fast",
    "Ultra Fast",
]

_VSPIPE_Y4M_ARGS_CACHE: dict[str, list[str]] = {}


@dataclass(frozen=True)
class VSPipelinePreset:
    preset_id: str
    label: str
    description: str
    deinterlace: str
    field_order: str
    cleanup: str
    output_mode: str
    container: str
    video_codec: str
    video_preset: str
    video_rate_mode: str
    video_rate_value: str
    audio_codec: str
    audio_bitrate: str
    match_source_fps: bool
    pixel_format: str


PIPELINE_PRESETS: tuple[VSPipelinePreset, ...] = (
    VSPipelinePreset(
        preset_id="manual",
        label=_("Manual"),
        description=_("Do not auto-change settings. Keep full manual control."),
        deinterlace="off",
        field_order="auto",
        cleanup="off",
        output_mode="custom",
        container="mkv",
        video_codec="libx264",
        video_preset="medium",
        video_rate_mode="bitrate",
        video_rate_value="6M",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="auto",
    ),
    VSPipelinePreset(
        preset_id="vhs_qtgmc_balanced_auto",
        label=_("VHS QTGMC Balanced (Auto field order)"),
        description=_(
            "Recommended start for VHS delivery. QTGMC Slow + balanced motion settings. "
            "Auto-detects field order and outputs double-rate FPS."
        ),
        deinterlace="qtgmc_slow",
        field_order="auto",
        cleanup="off",
        output_mode="custom",
        container="mp4",
        video_codec="libx265",
        video_preset="slow",
        video_rate_mode="crf",
        video_rate_value="16",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_qtgmc_balanced_bff",
        label=_("VHS QTGMC Balanced (BFF / 50p)"),
        description=_(
            "Balanced VHS profile locked to BFF. Use when your capture chain is known BFF."
        ),
        deinterlace="qtgmc_slow",
        field_order="bff",
        cleanup="off",
        output_mode="custom",
        container="mp4",
        video_codec="libx265",
        video_preset="slow",
        video_rate_mode="crf",
        video_rate_value="16",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_qtgmc_balanced_tff",
        label=_("VHS QTGMC Balanced (TFF / 50p)"),
        description=_(
            "Balanced VHS profile locked to TFF. Use when your capture chain is known TFF."
        ),
        deinterlace="qtgmc_slow",
        field_order="tff",
        cleanup="off",
        output_mode="custom",
        container="mp4",
        video_codec="libx265",
        video_preset="slow",
        video_rate_mode="crf",
        video_rate_value="16",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_qtgmc_stable_auto",
        label=_("VHS QTGMC Stable (less shimmer)"),
        description=_(
            "Stability-focused VHS profile for difficult motion edges. Slightly stronger "
            "temporal stabilization and softer sharpening."
        ),
        deinterlace="qtgmc_slower",
        field_order="auto",
        cleanup="light",
        output_mode="custom",
        container="mp4",
        video_codec="libx265",
        video_preset="slower",
        video_rate_mode="crf",
        video_rate_value="16",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_qtgmc_clean_test",
        label=_("VHS QTGMC Clean Test (minimal)"),
        description=_(
            "Minimal QTGMC chain for A/B testing when advanced restoration looks overprocessed."
        ),
        deinterlace="qtgmc_slow",
        field_order="auto",
        cleanup="off",
        output_mode="custom",
        container="mp4",
        video_codec="libx265",
        video_preset="slow",
        video_rate_mode="crf",
        video_rate_value="18",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_bwdif_fast",
        label=_("VHS BWDIF Fast (quick preview/export)"),
        description=_(
            "Fast bob deinterlace profile for quick passes. Lower quality than QTGMC but much faster."
        ),
        deinterlace="bwdif",
        field_order="auto",
        cleanup="off",
        output_mode="custom",
        container="mp4",
        video_codec="libx264",
        video_preset="fast",
        video_rate_mode="crf",
        video_rate_value="18",
        audio_codec="aac",
        audio_bitrate="192k",
        match_source_fps=True,
        pixel_format="yuv420p",
    ),
    VSPipelinePreset(
        preset_id="vhs_backup_export",
        label=_("VHS Backup Export"),
        description=_(
            "Archival export: keep source FPS, no forced deinterlace, FFV1 + PCM in MKV."
        ),
        deinterlace="off",
        field_order="auto",
        cleanup="off",
        output_mode="custom",
        container="mkv",
        video_codec="ffv1",
        video_preset="auto",
        video_rate_mode="bitrate",
        video_rate_value="",
        audio_codec="pcm_s16le",
        audio_bitrate="",
        match_source_fps=True,
        pixel_format="yuv422p",
    ),
)


def _shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID") or os.environ.get("FLATPAK_SANDBOX_DIR"))


def _run_command(args: list[str], timeout: float = 12.0) -> tuple[int, str, str]:
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


def _find_executable(name: str) -> str | None:
    rc, out, _ = _run_command(["sh", "-lc", f"command -v {shlex.quote(name)}"])
    if rc == 0 and out:
        return out.splitlines()[0].strip()
    return None


def _resolve_vspipe_y4m_args(vspipe_path: str) -> list[str]:
    cached = _VSPIPE_Y4M_ARGS_CACHE.get(vspipe_path)
    if cached is not None:
        return cached

    rc, out, err = _run_command([vspipe_path, "--help"], timeout=6.0)
    help_text = (out + "\n" + err).lower() if rc in {0, 1, 2} else ""

    # Newer builds commonly use -c/--container y4m.
    if "--container" in help_text or re.search(r"(^|\\s)-c(\\s|,)", help_text):
        args = ["-c", "y4m"]
    elif "--y4m" in help_text:
        args = ["--y4m"]
    else:
        # Safe default for current Linux distros; avoids failing on hosts
        # where --y4m was removed.
        args = ["-c", "y4m"]

    _VSPIPE_Y4M_ARGS_CACHE[vspipe_path] = args
    return args


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


def _detect_field_order_with_idet(path: str) -> tuple[str | None, str]:
    rc, out, err = _run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-v",
            "info",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-frames:v",
            "600",
            "-vf",
            "idet",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=30.0,
    )
    text = (out + "\n" + err) if (out or err) else ""
    if rc not in {0, 1} or not text:
        return None, ""

    match = re.search(
        r"Multi frame detection:\s*TFF:(\d+)\s+BFF:(\d+)\s+Progressive:(\d+)\s+Undetermined:(\d+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, ""

    tff = int(match.group(1))
    bff = int(match.group(2))
    prog = int(match.group(3))
    und = int(match.group(4))
    detail = f"TFF:{tff} BFF:{bff} Progressive:{prog} Und:{und}"

    # Need enough interlaced evidence for confident choice.
    if (tff + bff) < 20:
        return None, detail
    if tff > bff * 1.15:
        return "tff", detail
    if bff > tff * 1.15:
        return "bff", detail
    return None, detail


def _python_literal(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


class VapourSynthProcessPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        self.set_margin_start(5)
        self.set_margin_end(5)

        self._ffmpeg_command: list[str] | None = None
        self._encoders: list[EncoderInfo] = []
        self._pixel_formats: list[PixelFormat] = []
        self._source_info: dict[str, Any] = {}
        self._source_container = ""
        self._source_fps = 25.0
        self._source_has_audio = False
        self._active_script_path: str | None = None
        self._source_probe_seq = 0
        self._source_probe_timeout_id = 0
        self._auto_detected_field_order: str | None = None
        self._auto_field_order_detail: str = ""

        self.runner = FFmpegRunner(self._on_runner_output, self._on_runner_exit)

        self._build_ui()
        self._update_pipeline_preset_description()
        self._set_default_script()
        self._set_default_output_path()
        self._update_mode_widgets()
        self._update_qtgmc_advanced_state()
        self.update_command_preview()

    def _build_ui(self) -> None:
        builder = load_builder("vapoursynth_process_page.ui")
        bind_objects(
            self,
            builder,
            [
                "vs_status_label",
                "source_entry",
                "source_meta_label",
                "source_probe_box",
                "source_probe_spinner",
                "source_probe_label",
                "output_entry",
                "output_mode_combo",
                "container_combo",
                "video_codec_combo",
                "video_preset_combo",
                "video_rate_mode_combo",
                "video_rate_value_entry",
                "audio_codec_combo",
                "audio_bitrate_entry",
                "sample_rate_spin",
                "channels_combo",
                "match_fps_check",
                "output_fps_spin",
                "pix_fmt_combo",
                "pipeline_preset_combo",
                "pipeline_apply_button",
                "pipeline_preset_desc_label",
                "deinterlace_combo",
                "field_order_combo",
                "cleanup_combo",
                "qtgmc_advanced_frame",
                "source_match_combo",
                "match_preset_combo",
                "match_preset2_combo",
                "match_tr2_combo",
                "lossless_combo",
                "tr0_combo",
                "tr1_combo",
                "tr2_combo",
                "sharpness_entry",
                "ez_denoise_entry",
                "ez_keep_grain_entry",
                "input_type_combo",
                "fps_divisor_spin",
                "qtgmc_tuning_combo",
                "qtgmc_show_settings_check",
                "qtgmc_extra_args_entry",
                "use_custom_script_check",
                "custom_script_view",
                "script_note_label",
                "status_label",
                "command_view",
                "start_button",
                "stop_button",
                "log_expander",
                "log_view",
            ],
        )
        self.append(require_object(builder, "vapoursynth_process_page_root"))

        source_pick_button = require_object(builder, "source_pick_button")
        out_pick_button = require_object(builder, "out_pick_button")

        self.command_buffer = self.command_view.get_buffer()
        self.log_buffer = self.log_view.get_buffer()
        self.custom_script_buffer = self.custom_script_view.get_buffer()

        self.source_entry.connect("changed", self.on_source_changed)
        source_pick_button.connect("clicked", self.on_choose_source_clicked)
        self.output_entry.connect("changed", self.on_settings_changed)
        out_pick_button.connect("clicked", self.on_choose_output_clicked)

        self.output_mode_combo.remove_all()
        for mode_id, label in PROCESS_OUTPUT_MODES:
            self.output_mode_combo.append(mode_id, label)
        self.output_mode_combo.set_active_id("source")
        self.output_mode_combo.connect("changed", self.on_output_mode_changed)

        self.container_combo.remove_all()
        for name in PROCESS_CONTAINERS:
            self.container_combo.append(name, name.upper())
        self.container_combo.set_active_id("mkv")
        self.container_combo.connect("changed", self.on_settings_changed)

        self.video_codec_combo.connect("changed", self.on_video_codec_changed)
        self.video_preset_combo.remove_all()
        for preset_id, preset_label in PROCESS_X26X_PRESETS:
            self.video_preset_combo.append(preset_id, preset_label)
        self.video_preset_combo.set_active_id("medium")
        self.video_preset_combo.connect("changed", self.on_settings_changed)
        self.video_rate_mode_combo.remove_all()
        for mode_id, label in PROCESS_VIDEO_RATE_MODES:
            self.video_rate_mode_combo.append(mode_id, label)
        self.video_rate_mode_combo.set_active_id("bitrate")
        self.video_rate_mode_combo.connect("changed", self.on_video_rate_mode_changed)
        self.video_rate_value_entry.set_text("6M")
        self.video_rate_value_entry.connect("changed", self.on_settings_changed)
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

        self.pipeline_preset_combo.remove_all()
        for preset in PIPELINE_PRESETS:
            self.pipeline_preset_combo.append(preset.preset_id, preset.label)
        self.pipeline_preset_combo.set_active_id("manual")
        self.pipeline_preset_combo.connect("changed", self.on_pipeline_preset_changed)
        self.pipeline_apply_button.connect("clicked", self.on_pipeline_apply_clicked)

        self.deinterlace_combo.remove_all()
        for mode_id, label in PROCESS_DEINTERLACE_MODES:
            self.deinterlace_combo.append(mode_id, label)
        self.deinterlace_combo.set_active_id("off")
        self.deinterlace_combo.connect("changed", self.on_processing_option_changed)

        self.field_order_combo.remove_all()
        for order_id, label in PROCESS_FIELD_ORDERS:
            self.field_order_combo.append(order_id, label)
        self.field_order_combo.set_active_id("auto")
        self.field_order_combo.connect("changed", self.on_processing_option_changed)

        self.cleanup_combo.remove_all()
        for mode_id, label in PROCESS_VHS_CLEANUP_MODES:
            self.cleanup_combo.append(mode_id, label)
        self.cleanup_combo.set_active_id("off")
        self.cleanup_combo.connect("changed", self.on_processing_option_changed)

        self.source_match_combo.remove_all()
        for value in ("auto", "0", "1", "2", "3"):
            label = _("auto") if value == "auto" else value
            self.source_match_combo.append(value, label)
        self.source_match_combo.set_active_id("auto")
        self.source_match_combo.connect("changed", self.on_processing_option_changed)

        self.match_preset_combo.remove_all()
        self.match_preset_combo.append("auto", _("auto"))
        for value in QTGMC_MATCH_PRESET_VALUES:
            self.match_preset_combo.append(value, value)
        self.match_preset_combo.set_active_id("auto")
        self.match_preset_combo.connect("changed", self.on_processing_option_changed)

        self.match_preset2_combo.remove_all()
        self.match_preset2_combo.append("auto", _("auto"))
        for value in QTGMC_MATCH_PRESET_VALUES:
            self.match_preset2_combo.append(value, value)
        self.match_preset2_combo.set_active_id("auto")
        self.match_preset2_combo.connect("changed", self.on_processing_option_changed)

        self.match_tr2_combo.remove_all()
        for value in ("auto", "0", "1", "2"):
            label = _("auto") if value == "auto" else value
            self.match_tr2_combo.append(value, label)
        self.match_tr2_combo.set_active_id("auto")
        self.match_tr2_combo.connect("changed", self.on_processing_option_changed)

        self.lossless_combo.remove_all()
        for value in ("auto", "0", "1", "2"):
            label = _("auto") if value == "auto" else value
            self.lossless_combo.append(value, label)
        self.lossless_combo.set_active_id("auto")
        self.lossless_combo.connect("changed", self.on_processing_option_changed)

        self.tr0_combo.remove_all()
        self.tr1_combo.remove_all()
        self.tr2_combo.remove_all()
        for value in ("auto", "0", "1", "2", "3"):
            label = _("auto") if value == "auto" else value
            self.tr0_combo.append(value, label)
            self.tr1_combo.append(value, label)
            self.tr2_combo.append(value, label)
        self.tr0_combo.set_active_id("auto")
        self.tr1_combo.set_active_id("auto")
        self.tr2_combo.set_active_id("auto")
        self.tr0_combo.connect("changed", self.on_processing_option_changed)
        self.tr1_combo.connect("changed", self.on_processing_option_changed)
        self.tr2_combo.connect("changed", self.on_processing_option_changed)

        self.sharpness_entry.connect("changed", self.on_processing_option_changed)
        self.ez_denoise_entry.connect("changed", self.on_processing_option_changed)
        self.ez_keep_grain_entry.connect("changed", self.on_processing_option_changed)

        self.input_type_combo.remove_all()
        for value in ("auto", "0", "1", "2", "3"):
            label = _("auto") if value == "auto" else value
            self.input_type_combo.append(value, label)
        self.input_type_combo.set_active_id("auto")
        self.input_type_combo.connect("changed", self.on_processing_option_changed)

        self.fps_divisor_spin.set_value(1.0)
        self.fps_divisor_spin.connect("value-changed", self.on_processing_option_changed)

        self.qtgmc_tuning_combo.remove_all()
        self.qtgmc_tuning_combo.append("auto", _("auto"))
        self.qtgmc_tuning_combo.append("None", "None")
        self.qtgmc_tuning_combo.append("DV-SD", "DV-SD")
        self.qtgmc_tuning_combo.append("DV-HD", "DV-HD")
        self.qtgmc_tuning_combo.set_active_id("auto")
        self.qtgmc_tuning_combo.connect("changed", self.on_processing_option_changed)

        self.qtgmc_show_settings_check.set_active(False)
        self.qtgmc_show_settings_check.connect("toggled", self.on_processing_option_changed)
        self.qtgmc_extra_args_entry.connect("changed", self.on_processing_option_changed)

        self.use_custom_script_check.connect("toggled", self.on_custom_script_toggled)
        self.custom_script_buffer.connect("changed", self.on_settings_changed)

        self.start_button.connect("clicked", self.on_start_clicked)
        self.stop_button.connect("clicked", self.on_stop_clicked)

        self.vs_status_label.set_tooltip_text(
            _("Detected host tools for this tab (FFmpeg and vspipe).")
        )
        self.source_entry.set_tooltip_text(
            _("Input media file for VapourSynth processing.")
        )
        source_pick_button.set_tooltip_text(
            _("Choose source media file from disk.")
        )
        self.source_meta_label.set_tooltip_text(
            _("Detected source metadata: container, codecs, FPS, and duration.")
        )
        self.source_probe_box.set_tooltip_text(
            _("Background metadata import status for large source files.")
        )

        self.output_entry.set_tooltip_text(
            _("Output file path for the processed export.")
        )
        out_pick_button.set_tooltip_text(
            _("Choose output file and destination.")
        )
        self.output_mode_combo.set_tooltip_text(
            _("Keep source mode preserves source container/audio where possible. Custom export unlocks manual codec settings.")
        )
        self.container_combo.set_tooltip_text(
            _("Output container format.")
        )
        self.video_codec_combo.set_tooltip_text(
            _("Video encoder used after VapourSynth processing.")
        )
        self.video_preset_combo.set_tooltip_text(
            _("Encoder speed/quality preset for libx264/libx265. Slower = better compression, slower encode.")
        )
        self.video_rate_mode_combo.set_tooltip_text(
            _("Video rate control mode: bitrate target or quality-based CRF/CQ/QP.")
        )
        self.video_rate_value_entry.set_tooltip_text(
            _("Rate value. Examples: 6M (bitrate) or 18 (CRF).")
        )
        self.audio_codec_combo.set_tooltip_text(
            _("Audio encoder. Use copy to keep source audio, or none to drop audio.")
        )
        self.audio_bitrate_entry.set_tooltip_text(
            _("Audio bitrate for lossy audio encoders, e.g. 192k.")
        )
        self.sample_rate_spin.set_tooltip_text(
            _("Output audio sample rate (Hz).")
        )
        self.channels_combo.set_tooltip_text(
            _("Output audio channel count.")
        )
        self.match_fps_check.set_tooltip_text(
            _(
                "When enabled, output FPS follows VapourSynth output automatically. "
                "Recommended for QTGMC/BWDIF."
            )
        )
        self.output_fps_spin.set_tooltip_text(
            _("Manual output FPS when Auto FPS is disabled.")
        )
        self.pix_fmt_combo.set_tooltip_text(
            _("Force output pixel format, or keep auto.")
        )

        self.pipeline_preset_combo.set_tooltip_text(
            _("Preconfigured VHS workflows. Use Apply to write settings into this page.")
        )
        self.pipeline_apply_button.set_tooltip_text(
            _("Apply selected pipeline preset to export and processing settings.")
        )
        self.pipeline_preset_desc_label.set_tooltip_text(
            _("Description of selected pipeline preset.")
        )
        self.deinterlace_combo.set_tooltip_text(
            _("Deinterlacing method. QTGMC gives highest quality but is slower.")
        )
        self.field_order_combo.set_tooltip_text(
            _("Field order for interlaced content: Auto, TFF, or BFF.")
        )
        self.cleanup_combo.set_tooltip_text(
            _("VHS cleanup strength (denoise/deblock chain).")
        )
        self.qtgmc_advanced_frame.set_tooltip_text(
            _("Advanced QTGMC controls. These apply only when a QTGMC deinterlace mode is selected.")
        )
        self.source_match_combo.set_tooltip_text(
            _("QTGMC SourceMatch level: 0=off, 1=basic, 2=refined, 3=twice refined.")
        )
        self.match_preset_combo.set_tooltip_text(
            _("QTGMC MatchPreset speed/quality for source-match stage 1. Auto uses QTGMC defaults.")
        )
        self.match_preset2_combo.set_tooltip_text(
            _("QTGMC MatchPreset2 speed/quality for source-match stage 2. Auto uses QTGMC defaults.")
        )
        self.match_tr2_combo.set_tooltip_text(
            _("QTGMC MatchTR2 temporal radius for refined source-match. Auto uses QTGMC defaults.")
        )
        self.lossless_combo.set_tooltip_text(
            _("QTGMC Lossless mode: 0=off, 1=after final smooth, 2=before resharpening.")
        )
        self.tr0_combo.set_tooltip_text(
            _("QTGMC TR0 temporal radius for motion search smoothing. Auto uses preset defaults.")
        )
        self.tr1_combo.set_tooltip_text(
            _("QTGMC TR1 temporal radius for initial output smoothing. Auto uses preset defaults.")
        )
        self.tr2_combo.set_tooltip_text(
            _("QTGMC TR2 temporal radius for final stabilization/denoise. Auto uses preset defaults.")
        )
        self.sharpness_entry.set_tooltip_text(
            _("QTGMC Sharpness override. Leave empty for auto.")
        )
        self.ez_denoise_entry.set_tooltip_text(
            _("QTGMC EZDenoise value (>0 enables). Leave empty for auto/off. Mutually exclusive with EZKeepGrain.")
        )
        self.ez_keep_grain_entry.set_tooltip_text(
            _("QTGMC EZKeepGrain value (>0 enables). Leave empty for auto/off. Mutually exclusive with EZDenoise.")
        )
        self.input_type_combo.set_tooltip_text(
            _("QTGMC InputType. 0=interlaced default, 1/2/3 for progressive repair/deshimmer workflows.")
        )
        self.fps_divisor_spin.set_tooltip_text(
            _("QTGMC FPSDivisor. 1 keeps bob double-rate output, 2 outputs single-rate, 3+ further decimates.")
        )
        self.qtgmc_tuning_combo.set_tooltip_text(
            _("QTGMC tuning preset: None, DV-SD, or DV-HD. Auto uses QTGMC default.")
        )
        self.qtgmc_show_settings_check.set_tooltip_text(
            _("Enable QTGMC ShowSettings to print resolved parameters in log output.")
        )
        self.qtgmc_extra_args_entry.set_tooltip_text(
            _(
                "Additional raw QTGMC keyword arguments appended as-is, e.g. "
                "NoiseProcess=1, Denoiser=\"dfttest\", ChromaNoise=True. "
                "Avoid duplicating options already set above."
            )
        )
        self.use_custom_script_check.set_tooltip_text(
            _("Enable to edit and run your own .vpy script. Disables preset generator.")
        )
        self.custom_script_view.set_tooltip_text(
            _("Generated or custom VapourSynth script. Supports {{SOURCE}} placeholder.")
        )
        self.script_note_label.set_tooltip_text(
            _("Script generation notes and active processing summary.")
        )

        self.status_label.set_tooltip_text(
            _("Warnings and runtime status for export.")
        )
        self.command_view.set_tooltip_text(
            _("Final command pipeline preview: vspipe output piped into ffmpeg.")
        )
        self.start_button.set_tooltip_text(
            _("Start VapourSynth processing export.")
        )
        self.stop_button.set_tooltip_text(
            _("Stop current VapourSynth export process.")
        )
        self.log_expander.set_tooltip_text(
            _("Show or hide VapourSynth export log.")
        )
        self.log_view.set_tooltip_text(
            _("Runtime log from vspipe/ffmpeg process.")
        )

        for widget, width in (
            (self.output_mode_combo, 160),
            (self.container_combo, 95),
            (self.video_codec_combo, 160),
            (self.video_preset_combo, 120),
            (self.video_rate_mode_combo, 105),
            (self.audio_codec_combo, 160),
            (self.channels_combo, 75),
            (self.pix_fmt_combo, 130),
            (self.pipeline_preset_combo, 260),
            (self.deinterlace_combo, 190),
            (self.field_order_combo, 95),
            (self.cleanup_combo, 130),
            (self.source_match_combo, 120),
            (self.match_preset_combo, 150),
            (self.match_preset2_combo, 150),
            (self.match_tr2_combo, 95),
            (self.lossless_combo, 120),
            (self.tr0_combo, 95),
            (self.tr1_combo, 95),
            (self.tr2_combo, 95),
            (self.input_type_combo, 95),
            (self.qtgmc_tuning_combo, 150),
        ):
            compact_widget(widget, width)

    def sync_capabilities(
        self,
        ffmpeg_command: list[str] | None,
        encoders: list[EncoderInfo],
        pixel_formats: list[PixelFormat],
    ) -> None:
        self._ffmpeg_command = ffmpeg_command
        self._encoders = list(encoders)
        self._pixel_formats = list(pixel_formats)

        vspipe = _find_executable("vspipe")
        if ffmpeg_command and vspipe:
            self.vs_status_label.set_text(
                _("VapourSynth export uses: ")
                + " ".join(ffmpeg_command)
                + " | vspipe: "
                + vspipe
                + " | VS path: "
                + str(managed_vs_plugin_dir())
            )
        elif ffmpeg_command:
            self.vs_status_label.set_text(_("FFmpeg found, but vspipe not found in PATH."))
        else:
            self.vs_status_label.set_text(_("FFmpeg not available."))

        self._populate_codec_combos()
        self._populate_pix_fmt_combo()
        self._update_mode_widgets()
        self.update_command_preview()

    def _populate_codec_combos(self) -> None:
        selected_v = self.video_codec_combo.get_active_id() or "libx264"
        selected_a = self.audio_codec_combo.get_active_id() or "aac"

        video_names = {enc.name for enc in self._encoders if enc.kind == "video"}
        audio_names = {enc.name for enc in self._encoders if enc.kind == "audio"}

        self.video_codec_combo.remove_all()
        for name in PROCESS_VIDEO_CODECS:
            if name in video_names:
                self.video_codec_combo.append(name, name)
        if not self.video_codec_combo.set_active_id(selected_v):
            if not self.video_codec_combo.set_active_id("libx264"):
                self.video_codec_combo.set_active(0)

        self.audio_codec_combo.remove_all()
        for name in PROCESS_AUDIO_CODECS:
            if name in {"none", "copy"} or name in audio_names:
                self.audio_codec_combo.append(name, name)
        if not self.audio_codec_combo.set_active_id(selected_a):
            if not self.audio_codec_combo.set_active_id("aac"):
                self.audio_codec_combo.set_active(0)

    def _populate_pix_fmt_combo(self) -> None:
        selected = self.pix_fmt_combo.get_active_id() or "auto"
        self.pix_fmt_combo.remove_all()
        self.pix_fmt_combo.append("auto", _("auto"))
        for name in sorted({fmt.name for fmt in self._pixel_formats}, key=str.casefold):
            self.pix_fmt_combo.append(name, name)
        if not self.pix_fmt_combo.set_active_id(selected):
            self.pix_fmt_combo.set_active_id("auto")

    def _current_pipeline_preset(self) -> VSPipelinePreset | None:
        active = self.pipeline_preset_combo.get_active_id()
        for preset in PIPELINE_PRESETS:
            if preset.preset_id == active:
                return preset
        return PIPELINE_PRESETS[0] if PIPELINE_PRESETS else None

    def _update_pipeline_preset_description(self) -> None:
        preset = self._current_pipeline_preset()
        if preset is None:
            self.pipeline_preset_desc_label.set_text("")
            return
        self.pipeline_preset_desc_label.set_text(preset.description)

    def _apply_pipeline_preset(self, preset: VSPipelinePreset) -> None:
        self.output_mode_combo.set_active_id(preset.output_mode)
        self.container_combo.set_active_id(preset.container)
        self.video_codec_combo.set_active_id(preset.video_codec)
        self.video_preset_combo.set_active_id(preset.video_preset)
        self.video_rate_mode_combo.set_active_id(preset.video_rate_mode)
        self.video_rate_value_entry.set_text(preset.video_rate_value)
        self.audio_codec_combo.set_active_id(preset.audio_codec)
        self.audio_bitrate_entry.set_text(preset.audio_bitrate)
        self.match_fps_check.set_active(preset.match_source_fps)
        self.pix_fmt_combo.set_active_id(preset.pixel_format)
        self.deinterlace_combo.set_active_id(preset.deinterlace)
        self.field_order_combo.set_active_id(preset.field_order)
        self.cleanup_combo.set_active_id(preset.cleanup)
        self._apply_qtgmc_advanced_defaults(preset.preset_id)
        if not preset.match_source_fps:
            self.output_fps_spin.set_value(self._source_fps or 25.0)

        self._update_pipeline_preset_description()
        self._update_mode_widgets()
        self._update_qtgmc_advanced_state()
        self._set_default_output_path()
        self._ensure_output_extension()
        if not self.use_custom_script_check.get_active():
            self._set_default_script()
        self.update_command_preview()

    def _apply_qtgmc_advanced_defaults(self, preset_id: str) -> None:
        # Reset baseline to auto/default first.
        self.source_match_combo.set_active_id("auto")
        self.match_preset_combo.set_active_id("auto")
        self.match_preset2_combo.set_active_id("auto")
        self.match_tr2_combo.set_active_id("auto")
        self.lossless_combo.set_active_id("auto")
        self.tr0_combo.set_active_id("auto")
        self.tr1_combo.set_active_id("auto")
        self.tr2_combo.set_active_id("auto")
        self.sharpness_entry.set_text("")
        self.ez_denoise_entry.set_text("")
        self.ez_keep_grain_entry.set_text("")
        self.input_type_combo.set_active_id("auto")
        self.fps_divisor_spin.set_value(1.0)
        self.qtgmc_tuning_combo.set_active_id("auto")
        self.qtgmc_show_settings_check.set_active(False)
        self.qtgmc_extra_args_entry.set_text("")

        if preset_id in {"vhs_qtgmc_balanced_auto", "vhs_qtgmc_balanced_bff", "vhs_qtgmc_balanced_tff"}:
            # Practical VHS baseline.
            self.source_match_combo.set_active_id("1")
            self.tr2_combo.set_active_id("2")
            self.lossless_combo.set_active_id("1")
            self.sharpness_entry.set_text("0.0")
            return

        if preset_id == "vhs_qtgmc_stable_auto":
            # More edge stability, slightly softer look.
            self.source_match_combo.set_active_id("1")
            self.tr2_combo.set_active_id("2")
            self.lossless_combo.set_active_id("0")
            self.sharpness_entry.set_text("-0.1")
            return

        if preset_id == "vhs_qtgmc_clean_test":
            # Minimal chain for A/B comparisons.
            self.source_match_combo.set_active_id("0")
            self.tr2_combo.set_active_id("1")
            self.lossless_combo.set_active_id("0")
            self.sharpness_entry.set_text("0.0")

    def on_source_changed(self, _entry: Gtk.Entry) -> None:
        self._source_probe_seq += 1
        seq = self._source_probe_seq
        path = self.source_entry.get_text().strip()
        self._set_default_output_path()
        if not self.use_custom_script_check.get_active():
            self._set_default_script()

        if self._source_probe_timeout_id:
            GLib.source_remove(self._source_probe_timeout_id)
            self._source_probe_timeout_id = 0

        self._source_info = {}
        self._source_container = ""
        self._source_fps = 25.0
        self._source_has_audio = False
        self._auto_detected_field_order = None
        self._auto_field_order_detail = ""

        if not path:
            self._set_source_probe_busy(False)
            self.source_meta_label.set_text(_("No source loaded."))
            self.update_command_preview()
            return
        if not os.path.isfile(path):
            self._set_source_probe_busy(False)
            self.source_meta_label.set_text(_("Source file not found."))
            self.update_command_preview()
            return

        self._set_source_probe_busy(True, _("Importing source metadata..."))
        self.source_meta_label.set_text(_("Loading source metadata..."))
        self._source_probe_timeout_id = GLib.timeout_add(120, self._run_source_probe_async, seq, path)
        self._set_default_output_path()
        self.update_command_preview()

    def on_settings_changed(self, *_args) -> None:
        self._update_mode_widgets()
        self.update_command_preview()

    def on_video_codec_changed(self, *_args) -> None:
        self._update_mode_widgets()
        self.update_command_preview()

    def on_processing_option_changed(self, *_args) -> None:
        self._update_qtgmc_advanced_state()
        if not self.use_custom_script_check.get_active():
            self._set_default_script()
        if not self.match_fps_check.get_active():
            self.output_fps_spin.set_value(self._recommended_output_fps())
        self.update_command_preview()

    def on_video_rate_mode_changed(self, *_args) -> None:
        mode = self.video_rate_mode_combo.get_active_id() or "bitrate"
        if mode == "crf":
            self.video_rate_value_entry.set_placeholder_text("18")
            value = self.video_rate_value_entry.get_text().strip()
            if not value or any(ch in value for ch in ("k", "K", "m", "M", "g", "G")):
                self.video_rate_value_entry.set_text("18")
        else:
            self.video_rate_value_entry.set_placeholder_text("6M")
            value = self.video_rate_value_entry.get_text().strip()
            if not value:
                self.video_rate_value_entry.set_text("6M")
        self.update_command_preview()

    def on_pipeline_preset_changed(self, *_args) -> None:
        self._update_pipeline_preset_description()

    def on_pipeline_apply_clicked(self, *_args) -> None:
        preset = self._current_pipeline_preset()
        if preset is None:
            return
        self._apply_pipeline_preset(preset)

    def on_custom_script_toggled(self, *_args) -> None:
        custom = self.use_custom_script_check.get_active()
        self.pipeline_preset_combo.set_sensitive(not custom)
        self.pipeline_apply_button.set_sensitive(not custom)
        self.deinterlace_combo.set_sensitive(not custom)
        self.field_order_combo.set_sensitive(not custom)
        self.cleanup_combo.set_sensitive(not custom)
        for widget in (
            self.source_match_combo,
            self.match_preset_combo,
            self.match_preset2_combo,
            self.match_tr2_combo,
            self.lossless_combo,
            self.tr0_combo,
            self.tr1_combo,
            self.tr2_combo,
            self.sharpness_entry,
            self.ez_denoise_entry,
            self.ez_keep_grain_entry,
            self.input_type_combo,
            self.fps_divisor_spin,
            self.qtgmc_tuning_combo,
            self.qtgmc_show_settings_check,
            self.qtgmc_extra_args_entry,
        ):
            widget.set_sensitive(not custom)
        self._update_qtgmc_advanced_state()
        if not custom:
            self._set_default_script()
        if not self.match_fps_check.get_active():
            self.output_fps_spin.set_value(self._recommended_output_fps())
        self.update_command_preview()

    def on_match_fps_toggled(self, _button: Gtk.CheckButton) -> None:
        self.output_fps_spin.set_sensitive(not self.match_fps_check.get_active())
        if not self.match_fps_check.get_active():
            self.output_fps_spin.set_value(self._recommended_output_fps())
        self.update_command_preview()

    def on_output_mode_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._update_mode_widgets()
        self._set_default_output_path()
        self.update_command_preview()

    def _update_mode_widgets(self) -> None:
        source_mode = self._output_mode() == "source"
        vcodec = self.video_codec_combo.get_active_id() or ""
        ffv1_codec = vcodec == "ffv1"
        x26x_codec = vcodec in {"libx264", "libx265"}
        for widget in (
            self.container_combo,
            self.video_codec_combo,
            self.audio_codec_combo,
            self.audio_bitrate_entry,
            self.sample_rate_spin,
            self.channels_combo,
            self.match_fps_check,
            self.output_fps_spin,
            self.pix_fmt_combo,
        ):
            widget.set_sensitive(not source_mode)
        self.video_preset_combo.set_sensitive((not source_mode) and x26x_codec)
        if not x26x_codec and (self.video_preset_combo.get_active_id() or "auto") != "auto":
            self.video_preset_combo.set_active_id("auto")
        self.video_rate_mode_combo.set_sensitive((not source_mode) and (not ffv1_codec))
        self.video_rate_value_entry.set_sensitive((not source_mode) and (not ffv1_codec))
        if ffv1_codec:
            self.video_rate_value_entry.set_placeholder_text(_("unused for ffv1"))
        else:
            rate_mode = self.video_rate_mode_combo.get_active_id() or "bitrate"
            self.video_rate_value_entry.set_placeholder_text("18" if rate_mode == "crf" else "6M")
        self.output_fps_spin.set_sensitive((not source_mode) and (not self.match_fps_check.get_active()))

    def _output_mode(self) -> str:
        return self.output_mode_combo.get_active_id() or "source"

    def _qtgmc_mode_active(self) -> bool:
        deint = self.deinterlace_combo.get_active_id() or "off"
        return deint.startswith("qtgmc_")

    def _update_qtgmc_advanced_state(self) -> None:
        active = self._qtgmc_mode_active() and (not self.use_custom_script_check.get_active())
        self.qtgmc_advanced_frame.set_visible(active)
        self.qtgmc_advanced_frame.set_sensitive(active)

    def _recommended_output_fps(self) -> float:
        base = self._source_fps if self._source_fps > 0 else 25.0

        if self.use_custom_script_check.get_active():
            start, end = self.custom_script_buffer.get_bounds()
            script_text = self.custom_script_buffer.get_text(start, end, True).lower()

            if "qtgmc(" in script_text:
                divisor = 1.0
                match = re.search(r"fpsdivisor\s*=\s*([0-9]+(?:\.[0-9]+)?)", script_text)
                if match:
                    try:
                        divisor = float(match.group(1))
                    except ValueError:
                        divisor = 1.0
                if divisor <= 0:
                    divisor = 1.0
                return base * 2.0 / divisor

            if "bwdif.bwdif(" in script_text:
                # BWDIF with field=2/3 is bob (double-rate); field=0/1 keeps rate.
                match = re.search(r"bwdif\.bwdif\([^)]*field\s*=\s*([0-3])", script_text)
                if match and match.group(1) in {"0", "1"}:
                    return base
                return base * 2.0

            return base

        deint = self.deinterlace_combo.get_active_id() or "off"
        if deint == "bwdif" or deint.startswith("qtgmc_"):
            return base * 2.0
        return base

    def _run_source_probe_async(self, seq: int, path: str) -> bool:
        self._source_probe_timeout_id = 0

        def worker() -> None:
            info = _probe_media(path)
            detected_field_order, detected_detail = _detect_field_order_with_idet(path)
            GLib.idle_add(
                self._apply_source_probe_result,
                seq,
                path,
                info,
                detected_field_order,
                detected_detail,
            )

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _apply_source_probe_result(
        self,
        seq: int,
        path: str,
        info: dict[str, Any],
        detected_field_order: str | None,
        detected_detail: str,
    ) -> bool:
        current_path = self.source_entry.get_text().strip()
        if seq != self._source_probe_seq or current_path != path:
            return False

        self._set_source_probe_busy(False)
        self._source_info = info
        self._auto_detected_field_order = detected_field_order
        self._auto_field_order_detail = detected_detail

        fmt = info.get("format") or {}
        streams = info.get("streams") or []
        self._source_container = str(fmt.get("format_name") or "").split(",", 1)[0].strip().lower()
        duration = float(fmt.get("duration") or 0.0)

        vcodec = ""
        acodec = ""
        for stream in streams:
            codec_type = str(stream.get("codec_type") or "")
            if codec_type == "video" and not vcodec:
                vcodec = str(stream.get("codec_name") or "")
                fps = _parse_fps_value(str(stream.get("avg_frame_rate") or ""))
                if fps and fps > 0:
                    self._source_fps = fps
            if codec_type == "audio" and not acodec:
                acodec = str(stream.get("codec_name") or "")
                self._source_has_audio = True

        parts = []
        if self._source_container:
            parts.append(self._source_container.upper())
        if vcodec:
            parts.append(_("Video: ") + vcodec)
        if self._source_fps > 0:
            parts.append(_("FPS: ") + f"{self._source_fps:.3f}")
        if acodec:
            parts.append(_("Audio: ") + acodec)
        if duration > 0:
            parts.append(_("Duration: ") + _format_time_label(duration))
        if self._auto_detected_field_order == "tff":
            parts.append(_("Field order (auto): TFF"))
        elif self._auto_detected_field_order == "bff":
            parts.append(_("Field order (auto): BFF"))

        self.source_meta_label.set_text(" | ".join(parts) if parts else _("Source loaded."))
        if not self.match_fps_check.get_active():
            self.output_fps_spin.set_value(self._recommended_output_fps())
        return False

    def _set_source_probe_busy(self, busy: bool, text: str = "") -> None:
        self.source_probe_box.set_visible(busy)
        if text:
            self.source_probe_label.set_text(text)
        if busy:
            self.source_probe_spinner.start()
        else:
            self.source_probe_spinner.stop()

    def _set_default_output_path(self) -> None:
        if self.output_entry.get_text().strip():
            return
        source_path = self.source_entry.get_text().strip()
        if not source_path:
            return
        base = os.path.splitext(os.path.basename(source_path))[0]
        suffix = "-vs"
        ext = ".mkv"
        if self._output_mode() == "source" and self._source_container:
            ext = "." + self._source_container
        elif (self.container_combo.get_active_id() or "mkv") in PROCESS_CONTAINERS:
            ext = "." + (self.container_combo.get_active_id() or "mkv")
        self.output_entry.set_text(os.path.join(os.path.dirname(source_path), f"{base}{suffix}{ext}"))

    def _ensure_output_extension(self) -> None:
        out = self.output_entry.get_text().strip()
        if not out:
            return

        if self._output_mode() == "source" and self._source_container:
            wanted = self._source_container
        else:
            wanted = self.container_combo.get_active_id() or "mkv"
        if not wanted:
            return

        root, _ext = os.path.splitext(out)
        fixed = f"{root}.{wanted.lower()}"
        if fixed != out:
            self.output_entry.set_text(fixed)

    def _combo_optional_int(self, combo: Gtk.ComboBoxText) -> int | None:
        value = combo.get_active_id() or "auto"
        if value == "auto":
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _entry_optional_float(self, entry: Gtk.Entry) -> float | None:
        text = entry.get_text().strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _qtgmc_advanced_args(self) -> tuple[list[str], list[str]]:
        args: list[str] = []
        notes: list[str] = []

        source_match = self._combo_optional_int(self.source_match_combo)
        if source_match is not None:
            args.append(f"SourceMatch={source_match}")

        match_preset = self.match_preset_combo.get_active_id() or "auto"
        if match_preset != "auto":
            args.append(f"MatchPreset={_python_literal(match_preset)}")

        match_preset2 = self.match_preset2_combo.get_active_id() or "auto"
        if match_preset2 != "auto":
            args.append(f"MatchPreset2={_python_literal(match_preset2)}")

        match_tr2 = self._combo_optional_int(self.match_tr2_combo)
        if match_tr2 is not None:
            args.append(f"MatchTR2={match_tr2}")

        lossless = self._combo_optional_int(self.lossless_combo)
        if lossless is not None:
            args.append(f"Lossless={lossless}")

        tr0 = self._combo_optional_int(self.tr0_combo)
        if tr0 is not None:
            args.append(f"TR0={tr0}")

        tr1 = self._combo_optional_int(self.tr1_combo)
        if tr1 is not None:
            args.append(f"TR1={tr1}")

        tr2 = self._combo_optional_int(self.tr2_combo)
        if tr2 is not None:
            args.append(f"TR2={tr2}")

        sharpness_text = self.sharpness_entry.get_text().strip()
        sharpness = self._entry_optional_float(self.sharpness_entry)
        if sharpness is not None:
            args.append(f"Sharpness={sharpness:g}")
        elif sharpness_text:
            notes.append(_("Invalid Sharpness value ignored: ") + sharpness_text)

        ez_denoise_text = self.ez_denoise_entry.get_text().strip()
        ez_keep_grain_text = self.ez_keep_grain_entry.get_text().strip()
        ez_denoise = self._entry_optional_float(self.ez_denoise_entry)
        ez_keep_grain = self._entry_optional_float(self.ez_keep_grain_entry)
        if ez_denoise is None and ez_denoise_text:
            notes.append(_("Invalid EZDenoise value ignored: ") + ez_denoise_text)
        if ez_keep_grain is None and ez_keep_grain_text:
            notes.append(_("Invalid EZKeepGrain value ignored: ") + ez_keep_grain_text)
        if ez_denoise is not None and ez_keep_grain is not None:
            notes.append(_("Both EZDenoise and EZKeepGrain set; EZKeepGrain was ignored."))
            ez_keep_grain = None
        if ez_denoise is not None:
            args.append(f"EZDenoise={ez_denoise:g}")
        elif ez_keep_grain is not None:
            args.append(f"EZKeepGrain={ez_keep_grain:g}")

        input_type = self._combo_optional_int(self.input_type_combo)
        if input_type is not None:
            args.append(f"InputType={input_type}")

        fps_divisor = max(1, int(round(self.fps_divisor_spin.get_value())))
        if fps_divisor != 1:
            args.append(f"FPSDivisor={fps_divisor}")

        tuning = self.qtgmc_tuning_combo.get_active_id() or "auto"
        if tuning != "auto":
            args.append(f"Tuning={_python_literal(tuning)}")

        if self.qtgmc_show_settings_check.get_active():
            args.append("ShowSettings=True")

        extra_args = self.qtgmc_extra_args_entry.get_text().strip()
        if extra_args:
            args.append(extra_args)

        return args, notes

    def _set_default_script(self) -> None:
        source = self.source_entry.get_text().strip()
        source_literal = _python_literal(source or "/path/to/input.mkv")
        deinterlace_mode = self.deinterlace_combo.get_active_id() or "off"
        field_order = self.field_order_combo.get_active_id() or "auto"
        cleanup_mode = self.cleanup_combo.get_active_id() or "off"
        plugin_dirs = default_vs_plugin_dirs()
        lines = [
            "import vapoursynth as vs",
            "import os",
            "core = vs.core",
            f"src_path = {source_literal}",
            "",
            "plugin_dirs = [",
        ]
        for folder in plugin_dirs:
            lines.append(f"    {_python_literal(folder)},")
        lines += [
            "]",
            "",
            "def _try_load_plugin(lib_name, namespace):",
            "    if hasattr(core, namespace):",
            "        return True",
            "    for d in plugin_dirs:",
            "        p = os.path.join(d, lib_name)",
            "        if not os.path.isfile(p):",
            "            continue",
            "        try:",
            "            core.std.LoadPlugin(path=p)",
            "            if hasattr(core, namespace):",
            "                print(f'[vs] loaded {namespace} from {p}')",
            "                return True",
            "        except Exception as _vs_exc:",
            "            print(f'[vs] failed loading {lib_name} from {p}: {_vs_exc}')",
            "    return hasattr(core, namespace)",
            "",
            "def _load_plugin_file(lib_name):",
            "    for d in plugin_dirs:",
            "        p = os.path.join(d, lib_name)",
            "        if not os.path.isfile(p):",
            "            continue",
            "        try:",
            "            core.std.LoadPlugin(path=p)",
            "            print(f'[vs] loaded {lib_name} from {p}')",
            "            return True",
            "        except Exception as _vs_exc:",
            "            print(f'[vs] failed loading {lib_name} from {p}: {_vs_exc}')",
            "    return False",
            "",
            "_try_load_plugin('libffms2.so', 'ffms2')",
            "_try_load_plugin('libvslsmashsource.so', 'lsmas')",
            "_try_load_plugin('libbestsource.so', 'bs')",
            "",
            "# Try to load common post-process dependencies used by VHS pipelines.",
            "for _lib in [",
            "    'libmiscfilters.so',",
            "    'libmvtools.so',",
            "    'libremovegrain.so',",
            "    'libfmtconv.so',",
            "    'libdfttest.so',",
            "    'libnnedi3.so',",
            "    'libvsznedi3.so',",
            "    'libnnedi3cl.so',",
            "    'libeedi3m.so',",
            "    'libeedi2.so',",
            "    'libsangnom.so',",
            "    'libbwdif.so',",
            "    'libhqdn3d.so',",
            "    'libdeblock.so',",
            "]:",
            "    _load_plugin_file(_lib)",
            "",
            "clip = None",
            "source_errors = []",
            "",
            "if hasattr(core, 'ffms2'):",
            "    try:",
            "        clip = core.ffms2.Source(src_path)",
            "    except Exception as _vs_exc:",
            "        source_errors.append('ffms2: ' + str(_vs_exc))",
            "",
            "if clip is None and hasattr(core, 'lsmas'):",
            "    try:",
            "        clip = core.lsmas.LWLibavSource(src_path)",
            "    except Exception as _vs_exc:",
            "        source_errors.append('lsmas: ' + str(_vs_exc))",
            "",
            "if clip is None and hasattr(core, 'bs'):",
            "    try:",
            "        clip = core.bs.VideoSource(src_path)",
            "    except Exception as _vs_exc:",
            "        source_errors.append('bestsource: ' + str(_vs_exc))",
            "",
            "if clip is None:",
            "    err = ' | '.join(source_errors) if source_errors else 'no source namespace loaded (ffms2/lsmas/bs)'",
            "    raise RuntimeError('No usable VapourSynth source plugin. Install FFMS2 or LSMASHSource/BestSource. ' + err)",
            "",
        ]

        def add_try(name: str, statement: str) -> None:
            lines.extend(
                [
                    "try:",
                    f"    {statement}",
                    "except Exception as _vs_exc:",
                    f"    print('[vs] {name} skipped:', _vs_exc)",
                    "",
                ]
            )

        def add_required(name: str, statement: str) -> None:
            lines.extend(
                [
                    "try:",
                    f"    {statement}",
                    "except Exception as _vs_exc:",
                    f"    raise RuntimeError('{name} failed: ' + str(_vs_exc))",
                    "",
                ]
            )

        auto_field_order_note = ""
        effective_tff: bool | None = None
        qtgmc_note_text = ""
        if field_order == "tff":
            # VapourSynth R71+ uses SetFieldBased instead of AssumeTFF/AssumeBFF.
            lines.append("clip = core.std.SetFieldBased(clip, 2)")
            lines.append("")
            effective_tff = True
        elif field_order == "bff":
            lines.append("clip = core.std.SetFieldBased(clip, 1)")
            lines.append("")
            effective_tff = False
        elif deinterlace_mode != "off":
            if self._auto_detected_field_order == "bff":
                lines.append("clip = core.std.SetFieldBased(clip, 1)")
                lines.append("")
                effective_tff = False
                auto_field_order_note = _("Auto field order detected: BFF")
            else:
                # Most analog capture chains are TFF. If detection is missing
                # or uncertain, keep safe TFF fallback.
                lines.append("clip = core.std.SetFieldBased(clip, 2)")
                lines.append("")
                effective_tff = True
                if self._auto_detected_field_order == "tff":
                    auto_field_order_note = _("Auto field order detected: TFF")
                else:
                    auto_field_order_note = _("Auto field order fallback used: TFF")

        if deinterlace_mode == "bwdif":
            # VapourSynth Bwdif requires explicit field mode.
            # 2 = bob (double-rate) TFF, 3 = bob (double-rate) BFF.
            bwdif_field = "2" if effective_tff is not False else "3"
            add_required("Bwdif", f"clip = core.bwdif.Bwdif(clip, field={bwdif_field})")
        elif deinterlace_mode.startswith("qtgmc_"):
            qtgmc_preset = QTGMC_PRESET_BY_MODE.get(deinterlace_mode, "Medium")
            qtgmc_args = [f"Preset={_python_literal(qtgmc_preset)}"]
            if effective_tff is True:
                qtgmc_args.append("TFF=True")
            elif effective_tff is False:
                qtgmc_args.append("TFF=False")
            extra_qtgmc_args, qtgmc_notes = self._qtgmc_advanced_args()
            qtgmc_args.extend(extra_qtgmc_args)
            add_required("QTGMC", "import havsfunc; clip = havsfunc.QTGMC(clip, " + ", ".join(qtgmc_args) + ")")
            if qtgmc_notes:
                lines.append("# QTGMC notes:")
                for note in qtgmc_notes:
                    lines.append(f"# - {note}")
                lines.append("")
            if extra_qtgmc_args:
                qtgmc_note_text = _("QTGMC extras: ") + ", ".join(extra_qtgmc_args)
            if qtgmc_notes:
                joined_notes = " | ".join(qtgmc_notes)
                qtgmc_note_text = (qtgmc_note_text + " | " if qtgmc_note_text else "") + joined_notes

        if cleanup_mode == "light":
            add_try("HQDN3D", "clip = core.hqdn3d.Hqdn3d(clip, 2.0, 1.5, 3.0, 2.0)")
        elif cleanup_mode == "medium":
            add_try("HQDN3D", "clip = core.hqdn3d.Hqdn3d(clip, 3.0, 2.0, 4.5, 3.0)")
            add_try("Deblock", "clip = core.deblock.Deblock(clip, quant=14)")
        elif cleanup_mode == "advanced":
            add_try("HQDN3D", "clip = core.hqdn3d.Hqdn3d(clip, 4.0, 3.0, 6.0, 4.5)")
            add_try("Deblock", "clip = core.deblock.Deblock(clip, quant=20)")
            add_try("KNLMeansCL", "clip = core.knlm.KNLMeansCL(clip, d=1, a=2, s=4, h=1.2)")

        lines.append("clip.set_output()")
        self.custom_script_buffer.set_text("\n".join(lines))
        deint_text = self.deinterlace_combo.get_active_text() or _("Off")
        field_text = self.field_order_combo.get_active_text() or _("Auto")
        cleanup_text = self.cleanup_combo.get_active_text() or _("Off")
        self.script_note_label.set_text(
            _(
                "Generated script from Deinterlace={deint}, Field order={field}, VHS cleanup={cleanup}. "
                "Enable custom script to edit manually."
            ).format(deint=deint_text, field=field_text, cleanup=cleanup_text)
        )
        if qtgmc_note_text:
            self.script_note_label.set_text(self.script_note_label.get_text() + " " + qtgmc_note_text)
        if auto_field_order_note:
            note = auto_field_order_note
            if self._auto_field_order_detail:
                note += " (" + self._auto_field_order_detail + ")"
            self.script_note_label.set_text(self.script_note_label.get_text() + " " + note)

    def _build_script_text(self, source_path: str) -> str:
        start, end = self.custom_script_buffer.get_bounds()
        text = self.custom_script_buffer.get_text(start, end, True)
        if "{{SOURCE}}" in text:
            text = text.replace("{{SOURCE}}", source_path)
        return text

    def _write_script_file(self, script_text: str) -> str:
        self._cleanup_script_file()
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".vpy",
            prefix="slashmad-vs-",
            delete=False,
        ) as handle:
            handle.write(script_text)
            handle.write("\n")
            self._active_script_path = handle.name
        return self._active_script_path

    def _cleanup_script_file(self) -> None:
        path = self._active_script_path
        self._active_script_path = None
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def _build_export_command(self, *, for_preview: bool = False) -> tuple[list[str], str, list[str]]:
        warnings: list[str] = []

        source_path = self.source_entry.get_text().strip()
        if not source_path:
            raise RuntimeError(_("Choose a source file."))
        if not os.path.isfile(source_path):
            raise RuntimeError(_("Source file not found."))

        output_path = self.output_entry.get_text().strip()
        if not output_path:
            raise RuntimeError(_("Choose an output file."))

        ffmpeg_base = list(self._ffmpeg_command or [])
        if not ffmpeg_base:
            ffmpeg_base = ["ffmpeg"]

        # If this app runs inside flatpak we run the whole shell pipeline on host,
        # so the wrapped ffmpeg base should be normalized back to host binary.
        if ffmpeg_base[:2] == ["flatpak-spawn", "--host"]:
            ffmpeg_base = ffmpeg_base[2:] or ["ffmpeg"]

        vspipe = _find_executable("vspipe")
        if not vspipe:
            raise RuntimeError(_("vspipe not found. Install VapourSynth tools on host."))
        vspipe_y4m_args = _resolve_vspipe_y4m_args(vspipe)

        self._ensure_output_extension()
        output_path = self.output_entry.get_text().strip()

        script_text = self._build_script_text(source_path)
        if not script_text.strip():
            raise RuntimeError(_("VapourSynth script is empty."))
        if for_preview:
            script_path = "<vapoursynth-script.vpy>"
        else:
            script_path = self._write_script_file(script_text)

        mode = self._output_mode()
        ffmpeg_args = list(ffmpeg_base)
        ffmpeg_args += ["-hide_banner", "-y", "-f", "yuv4mpegpipe", "-i", "pipe:0", "-i", source_path]
        ffmpeg_args += ["-map", "0:v:0"]

        if mode == "source":
            vcodec = "libx264"
            container = self._source_container or "mkv"
            if self._source_has_audio:
                ffmpeg_args += ["-map", "1:a:0?", "-c:a", "copy"]
            else:
                ffmpeg_args += ["-an"]
            ffmpeg_args += ["-c:v", vcodec]
            warnings.append(_("Source mode keeps source container/audio, but video is always re-encoded after VapourSynth."))
            # Make extension align with source-mode container.
            root, _ext = os.path.splitext(output_path)
            output_path = f"{root}.{container.lower()}"
            self.output_entry.set_text(output_path)
        else:
            vcodec = self.video_codec_combo.get_active_id() or "libx264"
            video_preset = self.video_preset_combo.get_active_id() or "auto"
            acodec = self.audio_codec_combo.get_active_id() or "aac"
            pix_fmt = self.pix_fmt_combo.get_active_id() or "auto"
            video_rate_mode = self.video_rate_mode_combo.get_active_id() or "bitrate"
            video_rate_value = self.video_rate_value_entry.get_text().strip()

            ffmpeg_args += ["-c:v", vcodec]
            if video_preset != "auto":
                if vcodec in {"libx264", "libx265"}:
                    ffmpeg_args += ["-preset", video_preset]
                else:
                    warnings.append(
                        _("Preset is only applied for libx264/libx265 (current codec: {codec}).").format(
                            codec=vcodec
                        )
                    )
            if vcodec != "ffv1":
                if video_rate_mode == "crf":
                    if video_rate_value:
                        if vcodec in {"libx264", "libx265"}:
                            ffmpeg_args += ["-crf", video_rate_value]
                        elif vcodec in {"h264_nvenc", "hevc_nvenc"}:
                            ffmpeg_args += ["-cq", video_rate_value]
                        elif vcodec in {"h264_vaapi", "hevc_vaapi"}:
                            ffmpeg_args += ["-qp", video_rate_value]
                        else:
                            warnings.append(
                                _("CRF mode is not supported for this codec. Falling back to bitrate.")
                            )
                            ffmpeg_args += ["-b:v", video_rate_value]
                    else:
                        warnings.append(_("Rate control is CRF but value is empty."))
                else:
                    if video_rate_value:
                        ffmpeg_args += ["-b:v", video_rate_value]
                    else:
                        warnings.append(_("Rate control is bitrate but value is empty."))
            if pix_fmt != "auto":
                ffmpeg_args += ["-pix_fmt", pix_fmt]
            if not self.match_fps_check.get_active():
                chosen_fps = float(self.output_fps_spin.get_value())
                ffmpeg_args += ["-r", f"{chosen_fps:g}"]
                recommended_fps = self._recommended_output_fps()
                if abs(chosen_fps - recommended_fps) > 0.25:
                    warnings.append(
                        _("Manual FPS ({chosen:.3f}) differs from recommended ({recommended:.3f}) for current deinterlace/script.")
                        .format(chosen=chosen_fps, recommended=recommended_fps)
                    )

            if acodec == "none":
                ffmpeg_args += ["-an"]
            elif acodec == "copy":
                ffmpeg_args += ["-map", "1:a:0?", "-c:a", "copy"]
            else:
                ffmpeg_args += ["-map", "1:a:0?", "-c:a", acodec]
                ffmpeg_args += ["-ar", str(int(self.sample_rate_spin.get_value()))]
                ffmpeg_args += ["-ac", self.channels_combo.get_active_id() or "2"]
                abitrate = self.audio_bitrate_entry.get_text().strip()
                if abitrate and acodec not in {"pcm_s16le"}:
                    ffmpeg_args += ["-b:a", abitrate]

        ffmpeg_args += [output_path]

        vspipe_args = [vspipe, *vspipe_y4m_args, script_path, "-"]
        env_prefix = vs_runtime_prefix_shell()
        pipeline = f"{env_prefix}{_shell_preview(vspipe_args)} | {_shell_preview(ffmpeg_args)}"
        if _is_flatpak():
            exec_cmd = ["flatpak-spawn", "--host", "sh", "-lc", pipeline]
        else:
            exec_cmd = ["sh", "-lc", pipeline]
        return exec_cmd, pipeline, warnings

    def update_command_preview(self) -> None:
        try:
            _cmd, preview, warnings = self._build_export_command(for_preview=True)
        except RuntimeError as exc:
            self.command_buffer.set_text(str(exc))
            if not self.runner.running:
                self._set_status_text("")
            return
        self.command_buffer.set_text(preview)
        if warnings:
            self._set_status_text("\n".join(warnings))
        elif not self.runner.running:
            self._set_status_text("")

    def on_start_clicked(self, _button: Gtk.Button) -> None:
        if self.runner.running:
            return
        try:
            cmd, preview, warnings = self._build_export_command(for_preview=False)
        except RuntimeError as exc:
            self._set_status_text(str(exc))
            return

        self._clear_log()
        self._append_log(_("Running:") + " " + preview)
        if warnings:
            self._set_status_text("\n".join(warnings))

        try:
            self.runner.start(cmd)
        except Exception as exc:
            self._set_status_text(str(exc))
            self._cleanup_script_file()
            return

        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)

    def on_stop_clicked(self, _button: Gtk.Button) -> None:
        self.runner.stop()

    def _on_runner_output(self, line: str) -> None:
        GLib.idle_add(self._append_log, line)

    def _on_runner_exit(self, rc: int) -> None:
        GLib.idle_add(self._handle_runner_exit, rc)

    def _handle_runner_exit(self, rc: int) -> None:
        self.start_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self._set_status_text(_("VapourSynth export finished with code ") + str(rc))
        self._cleanup_script_file()
        self.update_command_preview()

    def on_choose_source_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title=_("Choose source media"))
        parent = self.get_root()

        def on_done(_dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if not isinstance(file, Gio.File):
                return
            path = file.get_path()
            if path:
                self.source_entry.set_text(path)

        dialog.open(parent, None, on_done)

    def on_choose_output_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title=_("Choose output file"))
        parent = self.get_root()

        current = self.output_entry.get_text().strip()
        if current:
            dialog.set_initial_file(Gio.File.new_for_path(current))
        else:
            source_path = self.source_entry.get_text().strip()
            if source_path:
                dialog.set_initial_folder(Gio.File.new_for_path(str(Path(source_path).parent)))

        def on_done(_dialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            if not isinstance(file, Gio.File):
                return
            path = file.get_path()
            if path:
                self.output_entry.set_text(path)

        dialog.save(parent, None, on_done)

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")

    def _append_log(self, line: str) -> bool:
        start_iter = self.log_buffer.get_start_iter()
        self.log_buffer.insert(start_iter, line + "\n")
        return False

    def _set_status_text(self, text: str) -> None:
        self.status_label.set_text(text)
        self.status_label.set_visible(bool(text.strip()))

    def shutdown(self) -> None:
        if self._source_probe_timeout_id:
            GLib.source_remove(self._source_probe_timeout_id)
            self._source_probe_timeout_id = 0
        self.runner.stop()
        self._cleanup_script_file()
