from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ffmpeg_gui.encode import (
    SUPPORTED_IMAGE_EXTENSIONS,
    build_command_preview,
    build_ffmpeg_command,
    collect_inputs,
    quality_flag_for_codec,
    write_concat_file,
)
from ffmpeg_gui.capture import CapturePage
from ffmpeg_gui.ffmpeg import detect_ffmpeg, detect_renderers, list_encoders, list_pixel_formats
from ffmpeg_gui.i18n import _
from ffmpeg_gui.runner import FFmpegRunner


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

PRESETS_NVENC = [
    "p1",
    "p2",
    "p3",
    "p4",
    "p5",
    "p6",
    "p7",
    "fast",
    "medium",
    "slow",
]


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app)
        self.set_title(_("Timelapse FFmpeg GUI"))
        self.set_default_size(900, 720)
        self.set_resizable(True)
        self.set_decorated(True)

        self.input_items: list[str] = []
        self.output_auto = True
        self._setting_output = False
        self._concat_list_path: str | None = None
        self.last_folder_path: str | None = None

        self._encoders = []
        self._pixel_formats = []
        self._encoder_details: dict[str, str] = {}
        self._hardware_info = None
        self._ffmpeg_command: list[str] | None = None
        self._css_provider: Gtk.CssProvider | None = None

        self.runner = FFmpegRunner(self._on_runner_output, self._on_runner_exit)

        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Timelapse FFmpeg GUI")))
        self.set_titlebar(header)

        refresh_button = Gtk.Button(label=_("Rescan"))
        refresh_button.set_tooltip_text(_("Re-check FFmpeg capabilities and encoders."))
        refresh_button.connect("clicked", self.on_refresh_clicked)
        header.pack_end(refresh_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        self.notebook = Gtk.Notebook()
        root.append(self.notebook)

        self.encode_page = self._build_encode_page()
        self.capture_page = CapturePage()
        self.capture_page_scroller = Gtk.ScrolledWindow()
        self.capture_page_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.capture_page_scroller.set_child(self.capture_page)
        self.hardware_page = self._build_hardware_page()
        self.help_page = self._build_help_page()

        self.notebook.append_page(self.encode_page, Gtk.Label(label=_("Encode")))
        self.notebook.append_page(self.capture_page_scroller, Gtk.Label(label=_("Capture")))
        self.notebook.append_page(self.hardware_page, Gtk.Label(label=_("Hardware")))
        self.notebook.append_page(self.help_page, Gtk.Label(label=_("Help")))

        self.connect("close-request", self.on_close_request)

        self.refresh()
        self.update_preview()

    def _build_help_page(self) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)

        title = Gtk.Label(label=_("Help & README"))
        title.set_xalign(0)
        title.add_css_class("title-2")
        container.append(title)

        subtitle = Gtk.Label(
            label=_("Quick help, Flatpak notes, and project README content.")
        )
        subtitle.set_xalign(0)
        subtitle.add_css_class("dim-label")
        container.append(subtitle)

        help_buffer = Gtk.TextBuffer()
        help_buffer.set_text(self._load_readme_text())

        help_view = Gtk.TextView(buffer=help_buffer)
        help_view.set_editable(False)
        help_view.set_monospace(False)
        help_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(help_view)
        container.append(scroller)

        return container

    def _load_readme_text(self) -> str:
        candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "README.md")),
            "/app/share/doc/ffmpeg-gui/README.md",
            "/usr/share/doc/ffmpeg-gui/README.md",
            os.path.join(os.getcwd(), "README.md"),
        ]
        for path in candidates:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        return handle.read()
            except OSError:
                continue

        return _(
            "README not found. If you're running from source, open README.md in the project root."
        )

    def _build_encode_page(self) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(container)

        io_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        io_row.set_hexpand(True)
        container.append(io_row)

        input_frame = Gtk.Frame(label=_("Inputs"))
        input_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        input_box.set_margin_top(8)
        input_box.set_margin_bottom(8)
        input_box.set_margin_start(8)
        input_box.set_margin_end(8)
        input_frame.set_child(input_box)
        input_frame.set_hexpand(True)
        io_row.append(input_frame)

        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_box.append(path_row)

        self.path_entry = Gtk.Entry()
        self.path_entry.set_placeholder_text(_("Paste a file or folder path"))
        self.path_entry.set_tooltip_text(_("Paste an image file path or a folder path."))
        self.path_entry.set_hexpand(True)
        self.path_entry.connect("activate", self.on_add_path_clicked)
        path_row.append(self.path_entry)

        add_path_button = Gtk.Button(label=_("Add"))
        add_path_button.set_tooltip_text(_("Add the pasted path to the input list."))
        add_path_button.connect("clicked", self.on_add_path_clicked)
        path_row.append(add_path_button)

        add_files_button = Gtk.Button(label=_("Add Files"))
        add_files_button.set_tooltip_text(_("Pick one or more image files."))
        add_files_button.connect("clicked", self.on_add_files_clicked)
        path_row.append(add_files_button)

        add_folder_button = Gtk.Button(label=_("Add Folder"))
        add_folder_button.set_tooltip_text(_("Pick a folder and load supported images inside it."))
        add_folder_button.connect("clicked", self.on_add_folder_clicked)
        path_row.append(add_folder_button)

        clear_button = Gtk.Button(label=_("Clear"))
        clear_button.set_tooltip_text(_("Clear all inputs."))
        clear_button.connect("clicked", self.on_clear_inputs_clicked)
        path_row.append(clear_button)

        self.input_list = Gtk.ListBox()
        self.input_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.input_list.set_tooltip_text(_("Drag and drop files or folders here."))
        input_box.append(self.input_list)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self.on_drop)
        self.input_list.add_controller(drop_target)

        self.input_status = Gtk.Label(label=_("No inputs yet."))
        self.input_status.set_xalign(0)
        input_box.append(self.input_status)

        output_frame = Gtk.Frame(label=_("Output"))
        output_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        output_box.set_margin_top(8)
        output_box.set_margin_bottom(8)
        output_box.set_margin_start(8)
        output_box.set_margin_end(8)
        output_frame.set_child(output_box)
        output_frame.set_hexpand(True)
        io_row.append(output_frame)

        output_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.append(output_row)

        self.output_entry = Gtk.Entry()
        self.output_entry.set_placeholder_text(_("Output file path"))
        self.output_entry.set_tooltip_text(_("Where the rendered video will be saved."))
        self.output_entry.set_hexpand(True)
        self.output_entry.connect("changed", self.on_output_changed)
        output_row.append(self.output_entry)

        output_button = Gtk.Button(label=_("Choose Output"))
        output_button.set_tooltip_text(_("Choose the output file path."))
        output_button.connect("clicked", self.on_choose_output_clicked)
        output_row.append(output_button)

        self.output_hint = Gtk.Label(label=_("Default is the same folder as your images."))
        self.output_hint.set_xalign(0)
        output_box.append(self.output_hint)

        settings_frame = Gtk.Frame(label=_("Settings"))
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        settings_box.set_margin_top(8)
        settings_box.set_margin_bottom(8)
        settings_box.set_margin_start(8)
        settings_box.set_margin_end(8)
        settings_frame.set_child(settings_box)
        container.append(settings_frame)

        codec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(codec_row)

        codec_label = Gtk.Label(label=_("Video codec"))
        codec_label.set_xalign(0)
        codec_label.set_size_request(140, -1)
        codec_row.append(codec_label)

        self.codec_combo = Gtk.ComboBoxText()
        self.codec_combo.set_tooltip_text(_("Choose a video encoder. Auto uses FFmpeg defaults."))
        self.codec_combo.set_hexpand(True)
        self.codec_combo.connect("changed", self.on_codec_changed)
        codec_row.append(self.codec_combo)

        self.show_all_check = Gtk.CheckButton(label=_("Show unusable codecs"))
        self.show_all_check.set_tooltip_text(_("Show codecs that require hardware not found on this system."))
        self.show_all_check.connect("toggled", self.on_show_all_toggled)
        codec_row.append(self.show_all_check)

        self.codec_info_label = Gtk.Label(label=_("Select a codec to see details."))
        self.codec_info_label.set_xalign(0)
        self.codec_info_label.set_wrap(True)
        self.codec_info_label.add_css_class("dim-label")
        settings_box.append(self.codec_info_label)

        quality_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(quality_row)

        quality_label = Gtk.Label(label=_("Quality (CRF/CQ)"))
        quality_label.set_xalign(0)
        quality_label.set_size_request(140, -1)
        quality_row.append(quality_label)

        self.quality_spin = Gtk.SpinButton.new_with_range(0, 51, 1)
        self.quality_spin.set_value(18)
        self.quality_spin.set_tooltip_text(_("CRF/CQ value. Lower = higher quality."))
        self.quality_spin.connect("value-changed", self.on_settings_changed)
        quality_row.append(self.quality_spin)

        self.quality_check = Gtk.CheckButton(label=_("Enable"))
        self.quality_check.set_tooltip_text(_("Enable CRF/CQ quality control."))
        self.quality_check.set_active(True)
        self.quality_check.connect("toggled", self.on_quality_toggled)
        quality_row.append(self.quality_check)

        quality_hint = Gtk.Label(label=_("Uses -crf for x264/x265/AV1/VP9 and -cq for NVENC."))
        quality_hint.set_xalign(0)
        quality_hint.add_css_class("dim-label")
        settings_box.append(quality_hint)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(preset_row)

        preset_label = Gtk.Label(label=_("Preset"))
        preset_label.set_xalign(0)
        preset_label.set_size_request(140, -1)
        preset_row.append(preset_label)

        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.set_tooltip_text(_("Encoder speed/quality preset. Auto skips -preset."))
        self.preset_combo.set_hexpand(True)
        self.preset_combo.connect("changed", self.on_settings_changed)
        preset_row.append(self.preset_combo)

        tune_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(tune_row)

        tune_label = Gtk.Label(label=_("Tune"))
        tune_label.set_xalign(0)
        tune_label.set_size_request(140, -1)
        tune_row.append(tune_label)

        self.tune_entry = Gtk.Entry()
        self.tune_entry.set_placeholder_text(_("e.g. film, animation, grain, fastdecode"))
        self.tune_entry.set_tooltip_text(_("Optional -tune setting for the encoder."))
        self.tune_entry.set_hexpand(True)
        self.tune_entry.connect("changed", self.on_settings_changed)
        tune_row.append(self.tune_entry)

        pix_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(pix_row)

        pix_label = Gtk.Label(label=_("Pixel format"))
        pix_label.set_xalign(0)
        pix_label.set_size_request(140, -1)
        pix_row.append(pix_label)

        self.pix_combo = Gtk.ComboBoxText()
        self.pix_combo.set_tooltip_text(_("Pixel format (color format). Auto lets FFmpeg choose."))
        self.pix_combo.set_hexpand(True)
        self.pix_combo.connect("changed", self.on_settings_changed)
        pix_row.append(self.pix_combo)

        fps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(fps_row)

        fps_label = Gtk.Label(label=_("Output FPS"))
        fps_label.set_xalign(0)
        fps_label.set_size_request(140, -1)
        fps_row.append(fps_label)

        self.fps_spin = Gtk.SpinButton.new_with_range(1, 240, 1)
        self.fps_spin.set_value(25)
        self.fps_spin.set_tooltip_text(_("Output frames per second for the timelapse."))
        self.fps_spin.connect("value-changed", self.on_settings_changed)
        fps_row.append(self.fps_spin)

        fps_hint = Gtk.Label(label=_("Timelapse output uses the FPS you set here."))
        fps_hint.set_xalign(0)
        fps_hint.add_css_class("dim-label")
        settings_box.append(fps_hint)

        extra_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_box.append(extra_row)

        extra_label = Gtk.Label(label=_("Extra ffmpeg args"))
        extra_label.set_xalign(0)
        extra_label.set_size_request(140, -1)
        extra_row.append(extra_label)

        self.extra_entry = Gtk.Entry()
        self.extra_entry.set_placeholder_text(_("e.g. -crf 18 -preset slow"))
        self.extra_entry.set_tooltip_text(_("Extra FFmpeg arguments appended at the end."))
        self.extra_entry.set_hexpand(True)
        self.extra_entry.connect("changed", self.on_settings_changed)
        extra_row.append(self.extra_entry)

        preview_frame = Gtk.Frame(label=_("Command preview"))
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        preview_box.set_margin_top(8)
        preview_box.set_margin_bottom(8)
        preview_box.set_margin_start(8)
        preview_box.set_margin_end(8)
        preview_frame.set_child(preview_box)
        container.append(preview_frame)

        self.command_buffer = Gtk.TextBuffer()
        self.command_view = Gtk.TextView(buffer=self.command_buffer)
        self.command_view.set_editable(False)
        self.command_view.set_monospace(True)
        self.command_view.set_tooltip_text(_("FFmpeg command preview."))

        command_scroller = Gtk.ScrolledWindow()
        command_scroller.set_min_content_height(80)
        command_scroller.set_child(self.command_view)
        preview_box.append(command_scroller)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.append(action_row)

        self.start_button = Gtk.Button(label=_("Start"))
        self.start_button.set_tooltip_text(_("Start rendering with FFmpeg."))
        self.start_button.connect("clicked", self.on_start_clicked)
        action_row.append(self.start_button)

        self.stop_button = Gtk.Button(label=_("Stop"))
        self.stop_button.set_tooltip_text(_("Stop the running FFmpeg process."))
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self.on_stop_clicked)
        action_row.append(self.stop_button)

        self.encode_status = Gtk.Label(label="")
        self.encode_status.set_xalign(0)
        preview_box.append(self.encode_status)

        log_frame = Gtk.Frame(label=_("Log"))
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_box.set_margin_top(8)
        log_box.set_margin_bottom(8)
        log_box.set_margin_start(8)
        log_box.set_margin_end(8)
        log_frame.set_child(log_box)
        container.append(log_frame)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_tooltip_text(_("FFmpeg output log."))

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_vexpand(True)
        log_scroller.set_min_content_height(180)
        log_scroller.set_child(self.log_view)
        log_box.append(log_scroller)

        return scroller

    def _build_hardware_page(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)

        info_frame = Gtk.Frame(label=_("FFmpeg"))
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_margin_top(8)
        info_box.set_margin_bottom(8)
        info_box.set_margin_start(8)
        info_box.set_margin_end(8)
        info_frame.set_child(info_box)
        root.append(info_frame)

        self.command_label = Gtk.Label(label=_("Command: (detecting...)") )
        self.command_label.set_xalign(0)
        info_box.append(self.command_label)

        self.version_label = Gtk.Label(label=_("Version: (detecting...)") )
        self.version_label.set_xalign(0)
        info_box.append(self.version_label)

        self.error_label = Gtk.Label(label="")
        self.error_label.set_xalign(0)
        self.error_label.add_css_class("error")
        info_box.append(self.error_label)

        render_frame = Gtk.Frame(label=_("Hardware Acceleration"))
        render_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        render_box.set_margin_top(8)
        render_box.set_margin_bottom(8)
        render_box.set_margin_start(8)
        render_box.set_margin_end(8)
        render_frame.set_child(render_box)
        root.append(render_frame)

        self.render_list = Gtk.ListBox()
        self.render_list.set_selection_mode(Gtk.SelectionMode.NONE)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self.render_list)
        render_box.append(scroller)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0)
        render_box.append(self.status_label)

        self.hardware_label = Gtk.Label(label="")
        self.hardware_label.set_xalign(0)
        self.hardware_label.add_css_class("dim-label")
        render_box.append(self.hardware_label)

        return root

    def on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self.refresh()
        self.update_preview()

    def refresh(self) -> None:
        info = detect_ffmpeg()
        self._ffmpeg_command = info.command

        if info.command:
            self.command_label.set_text(_("Command: ") + " ".join(info.command))
        else:
            self.command_label.set_text(_("Command: (not found)"))

        self.version_label.set_text(_("Version: ") + (info.version_line or "(unknown)"))
        self.error_label.set_text(info.error or "")

        statuses, hardware = detect_renderers(info)
        self._hardware_info = hardware
        self._update_renderer_list(statuses)

        if info.command is None:
            self.status_label.set_text(_("FFmpeg not found. Install it or ensure it is in PATH."))
        else:
            self.status_label.set_text(_("Detected from ffmpeg -hwaccels and -encoders."))

        if not hardware.known:
            self.hardware_label.set_text(_("Hardware detection unavailable."))
        elif hardware.gpu_lines:
            self.hardware_label.set_text(_("Detected GPU: ") + "; ".join(hardware.gpu_lines))
        else:
            self.hardware_label.set_text(_("No GPU detected."))

        self._encoders = list_encoders()
        self._pixel_formats = list_pixel_formats()
        self._populate_codec_combo()
        self._populate_pix_fmt_combo()
        self._populate_preset_combo(self.codec_combo.get_active_id())
        self._update_codec_info()
        self.capture_page.sync_capabilities(
            ffmpeg_command=self._ffmpeg_command,
            encoders=self._encoders,
            pixel_formats=self._pixel_formats,
            hardware_info=self._hardware_info,
        )

    def _populate_codec_combo(self) -> None:
        selected = self.codec_combo.get_active_id()
        self.codec_combo.remove_all()

        self.codec_combo.append("auto", _("Auto"))

        encoders = [enc for enc in self._encoders if enc.kind == "video"]
        show_unusable = self.show_all_check.get_active()
        if not show_unusable:
            encoders = [enc for enc in encoders if self._encoder_is_usable(enc.name)]

        encoder_by_name = {}
        for encoder in encoders:
            if encoder.name not in encoder_by_name:
                encoder_by_name[encoder.name] = encoder

        self._encoder_details = {name: enc.description for name, enc in encoder_by_name.items()}

        names = sorted(encoder_by_name.keys(), key=str.casefold)
        for name in names:
            description = self._encoder_details.get(name, "")
            label = name
            if show_unusable and not self._encoder_is_usable(name):
                label = f"{name} ({_('HW missing')})"
            if description:
                label = f"{label} - {description}"
            self.codec_combo.append(name, label)

        if selected and selected in names:
            self.codec_combo.set_active_id(selected)
            return

        if "libx264" in names:
            self.codec_combo.set_active_id("libx264")
        elif names:
            self.codec_combo.set_active_id(names[0])
        else:
            self.codec_combo.set_active_id("auto")

    def _populate_preset_combo(self, codec: str | None) -> None:
        selected = self.preset_combo.get_active_id()
        self.preset_combo.remove_all()

        self.preset_combo.append("auto", _("Auto"))

        preset_list = PRESETS_X264
        if codec and codec not in ("auto", ""):
            if "nvenc" in codec.lower():
                preset_list = PRESETS_NVENC

        for preset in preset_list:
            self.preset_combo.append(preset, preset)

        if codec in (None, "auto", ""):
            self.preset_combo.set_active_id("auto")
            return

        if selected and selected in preset_list:
            self.preset_combo.set_active_id(selected)
        elif "p4" in preset_list:
            self.preset_combo.set_active_id("p4")
        elif "medium" in preset_list:
            self.preset_combo.set_active_id("medium")
        else:
            self.preset_combo.set_active_id("auto")
        self._update_preset_tooltip()

    def _update_codec_info(self) -> None:
        codec_id = self.codec_combo.get_active_id()
        if not codec_id or codec_id == "auto":
            self.codec_info_label.set_text(_("Auto uses the FFmpeg default encoder."))
            return
        description = self._encoder_details.get(codec_id, "")
        if description:
            self.codec_info_label.set_text(f"{codec_id} - {description}")
        else:
            self.codec_info_label.set_text(codec_id)
        self.codec_combo.set_tooltip_text(self.codec_info_label.get_text())

    def _encoder_is_usable(self, name: str) -> bool:
        if self._hardware_info is None or not getattr(self._hardware_info, "known", False):
            return True
        requirement = self._encoder_requirement(name)
        if requirement is None:
            return True
        vendors = getattr(self._hardware_info, "vendors", set())
        if "any" in requirement:
            return bool(vendors)
        return bool(vendors.intersection(requirement))

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
        if "_vulkan" in lower:
            return {"any"}
        if "_opencl" in lower:
            return {"any"}
        return None

    def _populate_pix_fmt_combo(self) -> None:
        selected = self.pix_combo.get_active_id()
        self.pix_combo.remove_all()

        self.pix_combo.append("auto", _("Auto"))

        names = sorted({fmt.name for fmt in self._pixel_formats}, key=str.casefold)
        for name in names:
            self.pix_combo.append(name, name)

        if selected and selected in names:
            self.pix_combo.set_active_id(selected)
            return

        if "yuv420p" in names:
            self.pix_combo.set_active_id("yuv420p")
        elif names:
            self.pix_combo.set_active_id(names[0])
        else:
            self.pix_combo.set_active_id("auto")
        self._update_pix_fmt_tooltip()

    def _update_pix_fmt_tooltip(self) -> None:
        pix_id = self.pix_combo.get_active_id()
        if not pix_id or pix_id == "auto":
            self.pix_combo.set_tooltip_text(_("Pixel format. Auto lets FFmpeg choose."))
        else:
            self.pix_combo.set_tooltip_text(_("Pixel format: ") + pix_id)

    def _update_preset_tooltip(self) -> None:
        preset_id = self.preset_combo.get_active_id()
        if not preset_id or preset_id == "auto":
            self.preset_combo.set_tooltip_text(_("Preset. Auto skips -preset."))
        else:
            self.preset_combo.set_tooltip_text(_("Preset: ") + preset_id)

    def _update_renderer_list(self, statuses) -> None:
        child = self.render_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.render_list.remove(child)
            child = next_child

        for status in statuses:
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(6)
            row_box.set_margin_end(6)

            name_label = Gtk.Label(label=status.label)
            name_label.set_xalign(0)
            name_label.set_hexpand(True)

            state = ""
            if not status.ffmpeg_supported:
                state = _("FFmpeg: no")
            else:
                if status.hardware_available is True:
                    state = _("FFmpeg: yes; Hardware: yes")
                elif status.hardware_available is False:
                    state = _("FFmpeg: yes; Hardware: no")
                else:
                    state = _("FFmpeg: yes; Hardware: unknown")

            detail_parts: list[str] = []
            if status.matched:
                detail_parts.append(", ".join(status.matched))
            if status.hardware_note:
                detail_parts.append(status.hardware_note)
            detail = ""
            if detail_parts:
                detail = " (" + " | ".join(detail_parts) + ")"

            state_label = Gtk.Label(label=state + detail)
            state_label.set_xalign(1)
            if status.usable:
                state_label.add_css_class("success")
            elif status.ffmpeg_supported and status.hardware_available is False:
                state_label.add_css_class("warning")
            else:
                state_label.add_css_class("dim-label")

            row_box.append(name_label)
            row_box.append(state_label)
            row.set_child(row_box)
            self.render_list.append(row)

    def on_add_path_clicked(self, _button: Gtk.Button | Gtk.Entry) -> None:
        path = self.path_entry.get_text().strip()
        if path:
            self.add_paths([path])
        self.path_entry.set_text("")

    def on_add_files_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select images"))
        dialog.set_modal(True)
        if self.last_folder_path:
            dialog.set_initial_folder(Gio.File.new_for_path(self.last_folder_path))

        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Supported images"))
        for ext in sorted(SUPPORTED_IMAGE_EXTENSIONS):
            image_filter.add_suffix(ext[1:])
        filters.append(image_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(image_filter)

        dialog.open_multiple(self, None, self._on_files_selected)

    def _on_files_selected(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return

        paths = self._extract_paths(files)
        self._remember_last_folder(paths)
        self.add_paths(paths)

    def on_add_folder_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select folder"))
        dialog.set_modal(True)
        if self.last_folder_path:
            dialog.set_initial_folder(Gio.File.new_for_path(self.last_folder_path))
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None:
            return
        path = folder.get_path()
        if path:
            self._remember_last_folder([path])
            self.add_paths([path])

    def on_clear_inputs_clicked(self, _button: Gtk.Button) -> None:
        self.input_items = []
        self._refresh_input_list()
        self.update_preview()

    def on_drop(self, _target: Gtk.DropTarget, value, _x: float, _y: float) -> bool:
        paths: list[str] = []
        if hasattr(value, "get_files"):
            paths = self._extract_paths(value.get_files())
        if paths:
            self.add_paths(paths)
            return True
        return False

    def _extract_paths(self, files) -> list[str]:
        paths: list[str] = []
        if files is None:
            return paths
        if hasattr(files, "get_n_items"):
            for index in range(files.get_n_items()):
                file = files.get_item(index)
                if file is None:
                    continue
                path = file.get_path()
                if path:
                    paths.append(path)
            return paths
        try:
            for file in files:
                path = file.get_path()
                if path:
                    paths.append(path)
        except TypeError:
            return []
        return paths

    def _remember_last_folder(self, paths: list[str]) -> None:
        for path in paths:
            if not path:
                continue
            if os.path.isdir(path):
                self.last_folder_path = path
                return
            if os.path.isfile(path):
                self.last_folder_path = os.path.dirname(path)
                return

    def add_paths(self, paths: list[str]) -> None:
        changed = False
        for path in paths:
            if path and path not in self.input_items:
                self.input_items.append(path)
                changed = True

        if changed:
            self._remember_last_folder(paths)
            self._refresh_input_list()
            self._update_default_output()
            self.update_preview()

    def _refresh_input_list(self) -> None:
        child = self.input_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.input_list.remove(child)
            child = next_child

        for path in self.input_items:
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)

            label_text = path
            if os.path.isdir(path):
                label_text = _("Folder: ") + path

            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            label.set_hexpand(True)

            remove_button = Gtk.Button(label=_("Remove"))
            remove_button.set_tooltip_text(_("Remove this input from the list."))
            remove_button.connect("clicked", self.on_remove_input_clicked, path)

            row_box.append(label)
            row_box.append(remove_button)
            row.set_child(row_box)
            self.input_list.append(row)

        if self.input_items:
            self.input_status.set_text(_("Inputs: ") + str(len(self.input_items)))
        else:
            self.input_status.set_text(_("No inputs yet."))

    def on_remove_input_clicked(self, _button: Gtk.Button, path: str) -> None:
        if path in self.input_items:
            self.input_items.remove(path)
            self._refresh_input_list()
            self._update_default_output()
            self.update_preview()

    def _update_default_output(self) -> None:
        if not self.output_auto:
            return
        if not self.input_items:
            return
        first = self.input_items[0]
        if os.path.isdir(first):
            base_dir = first
        else:
            base_dir = os.path.dirname(first)
        output_path = os.path.join(base_dir, "output.mp4")
        self._set_output_path(output_path, auto=True)

    def _set_output_path(self, path: str, auto: bool) -> None:
        self._setting_output = True
        self.output_entry.set_text(path)
        self._setting_output = False
        self.output_auto = auto

    def on_output_changed(self, _entry: Gtk.Entry) -> None:
        if not self._setting_output:
            self.output_auto = False
        self.update_preview()

    def on_choose_output_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select output file"))
        dialog.set_modal(True)
        if self.last_folder_path:
            dialog.set_initial_folder(Gio.File.new_for_path(self.last_folder_path))
        dialog.save(self, None, self._on_output_selected)

    def _on_output_selected(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        if file is None:
            return
        path = file.get_path()
        if path:
            self._remember_last_folder([path])
            self._set_output_path(path, auto=False)
            self.update_preview()

    def on_codec_changed(self, _widget) -> None:
        codec_id = self.codec_combo.get_active_id()
        self._populate_preset_combo(codec_id)
        self._update_codec_info()
        self.update_preview()

    def on_show_all_toggled(self, _button: Gtk.CheckButton) -> None:
        self._populate_codec_combo()
        self.on_codec_changed(self.codec_combo)

    def on_quality_toggled(self, _button: Gtk.CheckButton) -> None:
        self.quality_spin.set_sensitive(self.quality_check.get_active())
        self.update_preview()

    def on_settings_changed(self, _widget) -> None:
        self._update_preset_tooltip()
        self._update_pix_fmt_tooltip()
        self.update_preview()

    def _selected_preset(self) -> str | None:
        preset_id = self.preset_combo.get_active_id()
        if preset_id in (None, "auto"):
            return None
        return preset_id

    def update_preview(self) -> None:
        self.encode_status.set_text("")

        if not self._ffmpeg_command:
            self.command_buffer.set_text(_("FFmpeg not found."))
            return

        output_path = self.output_entry.get_text().strip()
        if not output_path:
            self.command_buffer.set_text(_("Choose an output file to see the command."))
            return

        collection = collect_inputs(self.input_items)
        warnings: list[str] = []
        if collection.warnings:
            warnings.extend(collection.warnings)
        if not collection.paths:
            self.command_buffer.set_text(_("Add images or folders to build the command."))
            return

        codec_id = self.codec_combo.get_active_id()
        codec = None if codec_id in (None, "auto") else codec_id

        quality = int(self.quality_spin.get_value()) if self.quality_check.get_active() else None
        preset = self._selected_preset()
        tune = self.tune_entry.get_text().strip() or None

        quality_flag = quality_flag_for_codec(codec) if quality is not None else None
        if quality is not None and codec is not None and quality_flag is None:
            warnings.append(_("Quality setting is ignored for this codec."))

        pix_id = self.pix_combo.get_active_id()
        pix_fmt = None if pix_id in (None, "auto") else pix_id
        if pix_fmt is None and codec and "nvenc" in codec.lower():
            pix_fmt = "yuv420p"
            warnings.append(_("Pixel format set to yuv420p for NVENC."))

        fps = self.fps_spin.get_value()
        extra_args = self.extra_entry.get_text().strip() or None

        preview = build_command_preview(
            ffmpeg_cmd=self._ffmpeg_command,
            output_file=output_path,
            fps=fps,
            codec=codec,
            quality=quality,
            preset=preset,
            tune=tune,
            pix_fmt=pix_fmt,
            extra_args=extra_args,
        )
        self.command_buffer.set_text(preview)
        if warnings:
            self.encode_status.set_text("\n".join(warnings))

    def on_start_clicked(self, _button: Gtk.Button) -> None:
        if self.runner.running:
            return

        info = detect_ffmpeg()
        if not info.command:
            self.encode_status.set_text(_("FFmpeg not found. Install it or ensure it is in PATH."))
            return

        output_path = self.output_entry.get_text().strip()
        if not output_path:
            self.encode_status.set_text(_("Please choose an output file."))
            return

        collection = collect_inputs(self.input_items)
        warnings: list[str] = []
        if collection.warnings:
            warnings.extend(collection.warnings)
        if not collection.paths:
            self.encode_status.set_text(_("No supported images found."))
            return

        codec_id = self.codec_combo.get_active_id()
        codec = None if codec_id in (None, "auto") else codec_id

        quality = int(self.quality_spin.get_value()) if self.quality_check.get_active() else None
        preset = self._selected_preset()
        tune = self.tune_entry.get_text().strip() or None

        pix_id = self.pix_combo.get_active_id()
        pix_fmt = None if pix_id in (None, "auto") else pix_id
        if pix_fmt is None and codec and "nvenc" in codec.lower():
            pix_fmt = "yuv420p"
            warnings.append(_("Pixel format set to yuv420p for NVENC."))

        fps = self.fps_spin.get_value()
        extra_args = self.extra_entry.get_text().strip() or None

        list_file = write_concat_file(collection.paths, fps)
        self._concat_list_path = list_file

        cmd = build_ffmpeg_command(
            ffmpeg_cmd=info.command,
            list_file=list_file,
            output_file=output_path,
            fps=fps,
            codec=codec,
            quality=quality,
            preset=preset,
            tune=tune,
            pix_fmt=pix_fmt,
            extra_args=extra_args,
        )

        if quality is not None and codec is not None and quality_flag_for_codec(codec) is None:
            warnings.append(_("Quality setting is ignored for this codec."))

        if warnings:
            self.encode_status.set_text("\n".join(warnings))

        self._clear_log()
        self._append_log(_("Running:") + " " + " ".join(cmd))

        try:
            self.runner.start(cmd)
        except Exception as exc:
            self.encode_status.set_text(str(exc))
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
        self.encode_status.set_text(_("FFmpeg finished with code ") + str(rc))
        self._cleanup_concat_file()

    def _cleanup_concat_file(self) -> None:
        if self._concat_list_path and os.path.exists(self._concat_list_path):
            try:
                os.remove(self._concat_list_path)
            except OSError:
                pass
        self._concat_list_path = None

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")

    def _append_log(self, line: str) -> None:
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, line + "\n")
        self.log_view.scroll_to_iter(end_iter, 0.0, False, 0.0, 1.0)
        return False

    def on_close_request(self, _window: Gtk.ApplicationWindow) -> bool:
        self.runner.stop()
        self.capture_page.shutdown()
        return False
