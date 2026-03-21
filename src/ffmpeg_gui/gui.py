from __future__ import annotations

import os
import shutil

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ffmpeg_gui.encode import (
    SUPPORTED_IMAGE_EXTENSIONS,
    build_command_preview,
    build_ffmpeg_command,
    collect_inputs,
    prepare_inputs_for_timelapse,
    quality_flag_for_codec,
    write_concat_file,
)
from ffmpeg_gui.capture import CapturePage
from ffmpeg_gui.edit import EditPage
from ffmpeg_gui.ffmpeg import detect_ffmpeg, detect_renderers, list_encoders, list_pixel_formats
from ffmpeg_gui.i18n import _
from ffmpeg_gui.runner import FFmpegRunner
from ffmpeg_gui.ui import bind_objects, compact_widget, load_builder, require_object
from ffmpeg_gui.vapoursynth_process import VapourSynthProcessPage
from ffmpeg_gui.vapoursynth_page import VapourSynthPage


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
        self.set_title(_("Slashmad FFmpeg GUI"))
        self.set_default_size(1120, 760)
        self.set_resizable(True)
        self.set_decorated(True)
        self.add_css_class("ffmpeg-app-window")

        self.input_items: list[str] = []
        self.output_auto = True
        self._setting_output = False
        self._concat_list_path: str | None = None
        self._prepared_input_tempdir: str | None = None
        self.last_folder_path: str | None = None

        self._encoders = []
        self._pixel_formats = []
        self._encoder_details: dict[str, str] = {}
        self._hardware_info = None
        self._ffmpeg_command: list[str] | None = None
        self._css_provider: Gtk.CssProvider | None = None

        self.runner = FFmpegRunner(self._on_runner_output, self._on_runner_exit)

        header = Gtk.HeaderBar()
        header.add_css_class("ffmpeg-headerbar")
        header.set_title_widget(Gtk.Label(label=_("Slashmad FFmpeg GUI")))
        self.set_titlebar(header)

        refresh_button = Gtk.Button(label=_("Rescan"))
        refresh_button.add_css_class("toolbar-button")
        refresh_button.set_tooltip_text(_("Re-check FFmpeg capabilities and encoders."))
        refresh_button.connect("clicked", self.on_refresh_clicked)
        header.pack_end(refresh_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("ffmpeg-app")
        self.set_child(root)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.popup_enable()
        root.append(self.notebook)

        self._apply_css()

        self.encode_page = self._build_encode_page()
        self.encode_page.add_css_class("ffmpeg-page")
        self.capture_page = CapturePage()
        self.capture_page.add_css_class("ffmpeg-page")
        self.capture_page_scroller = Gtk.ScrolledWindow()
        self.capture_page_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.capture_page_scroller.set_child(self.capture_page)
        self.edit_page = EditPage()
        self.edit_page.add_css_class("ffmpeg-page")
        self.edit_page_scroller = Gtk.ScrolledWindow()
        self.edit_page_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.edit_page_scroller.set_child(self.edit_page)
        self.vapoursynth_process_page = VapourSynthProcessPage()
        self.vapoursynth_process_page.add_css_class("ffmpeg-page")
        self.vapoursynth_process_page_scroller = Gtk.ScrolledWindow()
        self.vapoursynth_process_page_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.vapoursynth_process_page_scroller.set_child(self.vapoursynth_process_page)
        self.vapoursynth_page = VapourSynthPage()
        self.vapoursynth_page.add_css_class("ffmpeg-page")
        self.vapoursynth_page_scroller = Gtk.ScrolledWindow()
        self.vapoursynth_page_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.vapoursynth_page_scroller.set_child(self.vapoursynth_page)
        self.hardware_page = self._build_hardware_page()
        self.hardware_page.add_css_class("ffmpeg-page")
        self.help_page = self._build_help_page()
        self.help_page.add_css_class("ffmpeg-page")

        self.notebook.append_page(self.encode_page, Gtk.Label(label=_("Timelapse")))
        self.notebook.append_page(self.capture_page_scroller, Gtk.Label(label=_("Capture")))
        self.notebook.append_page(self.edit_page_scroller, Gtk.Label(label=_("Trim")))
        self.notebook.append_page(self.vapoursynth_process_page_scroller, Gtk.Label(label=_("VapourSynth")))
        self.notebook.append_page(self.vapoursynth_page_scroller, Gtk.Label(label=_("VSRepo")))
        self.notebook.append_page(self.hardware_page, Gtk.Label(label=_("Hardware")))
        self.notebook.append_page(self.help_page, Gtk.Label(label=_("Help")))
        self._debug_print_tabs_if_enabled()

        self.connect("close-request", self.on_close_request)

        self.refresh()
        self.update_preview()

    def _debug_print_tabs_if_enabled(self) -> None:
        if os.environ.get("FFMPEG_GUI_DEBUG_TABS", "0") != "1":
            return
        try:
            pages = self.notebook.get_n_pages()
            print(f"[tabs] pages={pages}", flush=True)
            for index in range(pages):
                child = self.notebook.get_nth_page(index)
                tab = self.notebook.get_tab_label(child)
                text = tab.get_text() if isinstance(tab, Gtk.Label) else type(tab).__name__
                visible = tab.get_visible() if tab is not None else False
                print(f"[tabs] {index}: {text!r} visible={visible}", flush=True)
        except Exception as exc:
            print(f"[tabs] debug failed: {exc}", flush=True)

    def _build_help_page(self) -> Gtk.Widget:
        builder = load_builder("help_page.ui")
        help_page = require_object(builder, "help_page_root")
        self.help_view = require_object(builder, "help_view")
        help_buffer = self.help_view.get_buffer()
        help_buffer.set_text(self._load_readme_text())
        return help_page

    def _load_readme_text(self) -> str:
        candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "README.md")),
            "/app/share/doc/slashmad-ffmpeg-gui/README.md",
            "/usr/share/doc/slashmad-ffmpeg-gui/README.md",
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
        builder = load_builder("encode_page.ui")
        page = require_object(builder, "encode_page_root")
        bind_objects(
            self,
            builder,
            [
                "path_entry",
                "input_list",
                "input_status",
                "output_entry",
                "output_hint",
                "codec_combo",
                "show_all_check",
                "codec_info_label",
                "quality_spin",
                "quality_check",
                "preset_combo",
                "tune_entry",
                "pix_combo",
                "fps_spin",
                "extra_entry",
                "command_view",
                "start_button",
                "stop_button",
                "encode_status",
                "log_view",
            ],
        )

        add_path_button = require_object(builder, "add_path_button")
        add_files_button = require_object(builder, "add_files_button")
        add_folder_button = require_object(builder, "add_folder_button")
        clear_button = require_object(builder, "clear_button")
        output_button = require_object(builder, "choose_output_button")

        self.command_buffer = self.command_view.get_buffer()
        self.log_buffer = self.log_view.get_buffer()

        self.path_entry.set_tooltip_text(_("Paste an image file path or a folder path."))
        self.path_entry.connect("activate", self.on_add_path_clicked)
        add_path_button.set_tooltip_text(_("Add the pasted path to the input list."))
        add_path_button.connect("clicked", self.on_add_path_clicked)
        add_files_button.set_tooltip_text(_("Pick one or more image files."))
        add_files_button.connect("clicked", self.on_add_files_clicked)
        add_folder_button.set_tooltip_text(_("Pick a folder and load supported images inside it."))
        add_folder_button.connect("clicked", self.on_add_folder_clicked)
        clear_button.set_tooltip_text(_("Clear all inputs."))
        clear_button.connect("clicked", self.on_clear_inputs_clicked)

        self.input_list.set_tooltip_text(_("Drag and drop files or folders here."))
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self.on_drop)
        self.input_list.add_controller(drop_target)

        self.output_entry.set_tooltip_text(_("Where the rendered video will be saved."))
        self.output_entry.connect("changed", self.on_output_changed)
        output_button.set_tooltip_text(_("Choose the output file path."))
        output_button.connect("clicked", self.on_choose_output_clicked)

        self.codec_combo.set_tooltip_text(_("Choose a video encoder. Auto uses FFmpeg defaults."))
        self.codec_combo.connect("changed", self.on_codec_changed)
        self.show_all_check.set_tooltip_text(_("Show codecs that require hardware not found on this system."))
        self.show_all_check.connect("toggled", self.on_show_all_toggled)

        self.quality_spin.set_tooltip_text(_("CRF/CQ value. Lower = higher quality."))
        self.quality_spin.connect("value-changed", self.on_settings_changed)
        self.quality_check.set_tooltip_text(_("Enable CRF/CQ quality control."))
        self.quality_check.connect("toggled", self.on_quality_toggled)
        self.preset_combo.set_tooltip_text(_("Encoder speed/quality preset. Auto skips -preset."))
        self.preset_combo.connect("changed", self.on_settings_changed)
        self.tune_entry.set_tooltip_text(_("Optional -tune setting for the encoder."))
        self.tune_entry.connect("changed", self.on_settings_changed)
        self.pix_combo.set_tooltip_text(_("Pixel format (color format). Auto lets FFmpeg choose."))
        self.pix_combo.connect("changed", self.on_settings_changed)
        self.fps_spin.set_tooltip_text(_("Output frames per second for the timelapse."))
        self.fps_spin.connect("value-changed", self.on_settings_changed)
        self.extra_entry.set_tooltip_text(_("Extra FFmpeg arguments appended at the end."))
        self.extra_entry.connect("changed", self.on_settings_changed)

        self.command_view.set_tooltip_text(_("FFmpeg command preview."))
        self.start_button.set_tooltip_text(_("Start rendering with FFmpeg."))
        self.start_button.connect("clicked", self.on_start_clicked)
        self.stop_button.set_tooltip_text(_("Stop the running FFmpeg process."))
        self.stop_button.connect("clicked", self.on_stop_clicked)
        self.log_view.set_tooltip_text(_("FFmpeg output log."))

        compact_widget(self.codec_combo, 230)
        compact_widget(self.preset_combo, 160)
        compact_widget(self.pix_combo, 170)

        return page

    def _build_hardware_page(self) -> Gtk.Widget:
        builder = load_builder("hardware_page.ui")
        bind_objects(
            self,
            builder,
            [
                "command_label",
                "version_label",
                "error_label",
                "render_list",
                "status_label",
                "hardware_label",
            ],
        )
        return require_object(builder, "hardware_page_root")

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
        self.edit_page.sync_capabilities(
            ffmpeg_command=self._ffmpeg_command,
            encoders=self._encoders,
            pixel_formats=self._pixel_formats,
            hardware_info=self._hardware_info,
        )
        self.vapoursynth_process_page.sync_capabilities(
            ffmpeg_command=self._ffmpeg_command,
            encoders=self._encoders,
            pixel_formats=self._pixel_formats,
        )
        self.vapoursynth_page.sync_capabilities(
            ffmpeg_command=self._ffmpeg_command,
            encoders=self._encoders,
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

    def _apply_css(self) -> None:
        if self._css_provider is not None:
            return

        css = """
        window.ffmpeg-app-window,
        window.ffmpeg-app-window > box,
        window.ffmpeg-app-window viewport,
        window.ffmpeg-app-window notebook,
        window.ffmpeg-app-window stack,
        window.ffmpeg-app-window .ffmpeg-page {
            background-color: #14181d;
            color: #edf2f8;
        }

        headerbar.ffmpeg-headerbar {
            background: #181c22;
            border-bottom: 1px solid #2b3340;
            box-shadow: none;
            min-height: 20px;
            padding: 0 5px;
        }

        headerbar.ffmpeg-headerbar label {
            color: #f1f5fb;
            font-weight: 600;
        }

        headerbar.ffmpeg-headerbar button.toolbar-button {
            background: #222933;
            color: #edf2f8;
            border: 1px solid #364151;
            border-radius: 7px;
            box-shadow: none;
            min-height: 10px;
            min-width: 48px;
            padding: 1px 7px;
        }

        headerbar.ffmpeg-headerbar button.toolbar-button:hover {
            background: #2a3340;
        }

        headerbar.ffmpeg-headerbar button.toolbar-button:active {
            background: #313b49;
        }

        headerbar.ffmpeg-headerbar windowcontrols {
            border-spacing: 0;
            margin: 0;
            padding: 0;
        }

        headerbar.ffmpeg-headerbar windowcontrols > button,
        headerbar.ffmpeg-headerbar button.titlebutton {
            background: transparent;
            border: none;
            border-radius: 6px;
            min-width: 12px;
            min-height: 12px;
            margin: 0;
            padding: 0;
            box-shadow: none;
        }

        headerbar.ffmpeg-headerbar windowcontrols > button:hover,
        headerbar.ffmpeg-headerbar button.titlebutton:hover {
            background: rgba(110, 128, 150, 0.18);
        }

        headerbar.ffmpeg-headerbar windowcontrols > button:active,
        headerbar.ffmpeg-headerbar button.titlebutton:active {
            background: rgba(110, 128, 150, 0.28);
        }

        headerbar.ffmpeg-headerbar windowcontrols > button > image,
        headerbar.ffmpeg-headerbar button.titlebutton > image {
            -gtk-icon-size: 12px;
        }

        window.ffmpeg-app-window notebook > header {
            background: #10151c;
            border-bottom: 1px solid #27303c;
            padding: 0 6px;
        }

        window.ffmpeg-app-window notebook > header > tabs > tab {
            margin: 0 2px;
            padding: 3px 9px;
            border-radius: 10px 10px 0 0;
            color: rgba(237, 242, 248, 0.76);
        }

        window.ffmpeg-app-window notebook > header > tabs > tab:hover {
            background: rgba(84, 104, 128, 0.18);
            color: #f3f7fc;
        }

        window.ffmpeg-app-window notebook > header > tabs > tab:checked {
            background: #1d2530;
            color: #f8fbff;
            box-shadow: inset 0 -2px 0 #5a9cff;
        }

        window.ffmpeg-app-window frame > border {
            background: #141a22;
            border: none;
            border-radius: 10px;
            padding: 4px;
            box-shadow: none;
        }

        window.ffmpeg-app-window expander > title {
            background: #141a22;
            border: none;
            border-radius: 10px;
            padding: 4px 7px;
            box-shadow: none;
        }

        window.ffmpeg-app-window expander > title > arrow {
            color: #76adff;
        }

        window.ffmpeg-app-window label {
            color: #edf2f8;
        }

        window.ffmpeg-app-window .dim-label {
            color: rgba(237, 242, 248, 0.62);
        }

        window.ffmpeg-app-window .success {
            color: #8bdfab;
        }

        window.ffmpeg-app-window .warning {
            color: #ffcf7d;
        }

        window.ffmpeg-app-window .error {
            color: #ff9898;
        }

        window.ffmpeg-app-window entry,
        window.ffmpeg-app-window textview,
        window.ffmpeg-app-window textview text,
        window.ffmpeg-app-window spinbutton,
        window.ffmpeg-app-window spinbutton entry,
        window.ffmpeg-app-window listview,
        window.ffmpeg-app-window listbox {
            background: #0d1218;
            color: #f2f6fb;
            border: 1px solid #3d4a5c;
            border-radius: 8px;
            box-shadow: none;
        }

        window.ffmpeg-app-window entry,
        window.ffmpeg-app-window spinbutton entry {
            min-height: 14px;
            padding: 4px 7px;
            caret-color: #f2f6fb;
        }

        window.ffmpeg-app-window combobox {
            background: transparent;
            border: none;
            box-shadow: none;
            padding: 0;
        }

        window.ffmpeg-app-window spinbutton {
            padding: 0;
        }

        window.ffmpeg-app-window spinbutton entry {
            background: transparent;
            border: none;
            border-radius: 0;
            box-shadow: none;
        }

        window.ffmpeg-app-window textview text {
            padding: 6px;
        }

        window.ffmpeg-app-window entry:focus,
        window.ffmpeg-app-window spinbutton:focus-within,
        window.ffmpeg-app-window combobox button:focus,
        window.ffmpeg-app-window button:focus {
            border-color: #5d9fff;
            box-shadow: 0 0 0 1px rgba(93, 159, 255, 0.30);
        }

        window.ffmpeg-app-window button,
        window.ffmpeg-app-window combobox button {
            background: #242c36;
            color: #eef3f8;
            border: 1px solid #394454;
            border-radius: 8px;
            box-shadow: none;
            min-width: 58px;
            min-height: 14px;
            padding: 4px 8px;
        }

        window.ffmpeg-app-window button:hover,
        window.ffmpeg-app-window combobox button:hover {
            background: #2c3643;
        }

        window.ffmpeg-app-window button:active,
        window.ffmpeg-app-window combobox button:active {
            background: #34404e;
        }

        window.ffmpeg-app-window button:disabled,
        window.ffmpeg-app-window combobox button:disabled,
        window.ffmpeg-app-window entry:disabled,
        window.ffmpeg-app-window spinbutton:disabled {
            background: #1c222b;
            color: rgba(237, 242, 248, 0.42);
            border-color: #2b3440;
        }

        window.ffmpeg-app-window combobox arrow {
            color: #bdd6ff;
        }

        window.ffmpeg-app-window spinbutton button {
            padding: 4px 6px;
            min-width: 20px;
            min-height: 14px;
        }

        window.ffmpeg-app-window checkbutton > check {
            background: #0d1218;
            border: 1px solid #394454;
            border-radius: 5px;
            min-width: 16px;
            min-height: 16px;
        }

        window.ffmpeg-app-window checkbutton:checked > check {
            background: #4f96ff;
            border-color: #4f96ff;
        }

        window.ffmpeg-app-window progressbar trough {
            background: #0d1218;
            border: 1px solid #394454;
            border-radius: 999px;
            min-height: 10px;
        }

        window.ffmpeg-app-window progressbar progress {
            background: #5aa2ff;
            border-radius: 999px;
        }

        window.ffmpeg-app-window scale trough {
            background: #0d1218;
            border: 1px solid #394454;
            border-radius: 999px;
            min-height: 6px;
        }

        window.ffmpeg-app-window scale highlight {
            background: #5aa2ff;
            border-radius: 999px;
        }

        window.ffmpeg-app-window scale slider {
            background: #dce8ff;
            border: 2px solid #5aa2ff;
            border-radius: 999px;
            min-width: 14px;
            min-height: 14px;
            margin: -5px 0;
        }
        """
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_data(css.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

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

        self._cleanup_concat_file()
        self._cleanup_prepared_inputs()
        try:
            prepared = prepare_inputs_for_timelapse(collection.paths)
        except Exception as exc:
            self.encode_status.set_text(str(exc))
            return

        if prepared.warnings:
            warnings.extend(prepared.warnings)
        self._prepared_input_tempdir = prepared.temp_dir

        list_file = write_concat_file(prepared.paths, fps)
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
        if self._prepared_input_tempdir:
            self._append_log(_("RAW preview fallback active. Using embedded JPEG previews for timelapse input."))
        self._append_log(_("Running:") + " " + " ".join(cmd))

        try:
            self.runner.start(cmd)
        except Exception as exc:
            self._cleanup_concat_file()
            self._cleanup_prepared_inputs()
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
        self._cleanup_prepared_inputs()

    def _cleanup_concat_file(self) -> None:
        if self._concat_list_path and os.path.exists(self._concat_list_path):
            try:
                os.remove(self._concat_list_path)
            except OSError:
                pass
        self._concat_list_path = None

    def _cleanup_prepared_inputs(self) -> None:
        if self._prepared_input_tempdir and os.path.isdir(self._prepared_input_tempdir):
            shutil.rmtree(self._prepared_input_tempdir, ignore_errors=True)
        self._prepared_input_tempdir = None

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")

    def _append_log(self, line: str) -> bool:
        start_iter = self.log_buffer.get_start_iter()
        self.log_buffer.insert(start_iter, line + "\n")
        return False

    def on_close_request(self, _window: Gtk.ApplicationWindow) -> bool:
        self.runner.stop()
        self._cleanup_concat_file()
        self._cleanup_prepared_inputs()
        self.capture_page.shutdown()
        self.edit_page.shutdown()
        self.vapoursynth_process_page.shutdown()
        return False
