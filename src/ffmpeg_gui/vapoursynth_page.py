from __future__ import annotations

from dataclasses import dataclass
import json
import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ffmpeg_gui.ffmpeg import EncoderInfo, HardwareInfo
from ffmpeg_gui.i18n import _
from ffmpeg_gui.ui import bind_objects, compact_widget, load_builder, require_object
from ffmpeg_gui.vs_stack import (
    default_vs_plugin_dirs,
    managed_vs_plugin_dir,
    managed_vs_root,
    managed_vs_script_dir,
    shell_home_path,
)


SUPPORTED_CONTAINERS = ["mkv", "mp4", "mov"]
COMMON_CPU_VIDEO_ENCODERS = [
    "ffv1",
    "libx264",
    "libx265",
    "libaom-av1",
    "libsvtav1",
    "mpeg2video",
    "prores_ks",
    "mjpeg",
    "utvideo",
    "libxvid",
    "rawvideo",
]
COMMON_GPU_VIDEO_ENCODERS = [
    "h264_nvenc",
    "hevc_nvenc",
    "av1_nvenc",
    "h264_vaapi",
    "hevc_vaapi",
    "av1_vaapi",
    "h264_qsv",
    "hevc_qsv",
    "av1_qsv",
    "h264_amf",
    "hevc_amf",
    "av1_amf",
    "h264_vulkan",
    "hevc_vulkan",
    "av1_vulkan",
]
COMMON_AUDIO_ENCODERS = [
    "aac",
    "libopus",
    "flac",
    "pcm_s16le",
    "ac3",
    "mp2",
    "libmp3lame",
]


@dataclass(frozen=True)
class VSPreset:
    preset_id: str
    label: str
    description: str
    install_roots: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class VSPackage:
    file_stem: str
    name: str
    package_type: str
    description: str
    identifier: str
    namespace: str
    modulename: str
    dependencies: tuple[str, ...]
    devices: tuple[str, ...]
    linux_supported: bool
    payload_files: tuple[str, ...]

    @property
    def install_token(self) -> str:
        return self.identifier or self.modulename or self.namespace or self.name or self.file_stem

    @property
    def display_name(self) -> str:
        return self.identifier or self.name or self.file_stem


@dataclass(frozen=True)
class PackageStatus:
    requested: str
    package: VSPackage | None
    installed: bool
    available: bool
    reason: str | None


CURATED_PRESETS: tuple[VSPreset, ...] = (
    VSPreset(
        preset_id="qtgmc",
        label=_("QTGMC (havsfunc)"),
        description=_(
            "High-quality VHS deinterlacing via HAVSfunc/QTGMC. Best for interlaced tape captures."
        ),
        install_roots=("havsfunc", "com.vapoursynth.ffms2"),
        note=_(
            "vsrepo resolves HAVSfunc dependencies automatically. This is the main path if you want QTGMC."
        ),
    ),
    VSPreset(
        preset_id="bwdif",
        label=_("Bwdif (fast deinterlace)"),
        description=_("Simpler deinterlacing path with fewer dependencies and lower setup cost."),
        install_roots=("com.holywu.bwdif", "com.vapoursynth.ffms2"),
        note=_("Good fallback when you want deinterlacing without the full QTGMC chain."),
    ),
    VSPreset(
        preset_id="bm3d",
        label=_("BM3D (denoise)"),
        description=_("CPU denoising path for cleanup after deinterlacing or restoration work."),
        install_roots=("com.vapoursynth.bm3d", "com.vapoursynth.ffms2"),
        note=_("Useful for grain/noise cleanup on preserved captures."),
    ),
    VSPreset(
        preset_id="knlmeanscl",
        label=_("KNLMeansCL (OpenCL denoise)"),
        description=_("OpenCL denoising path when you want GPU-assisted cleanup."),
        install_roots=("com.Khanattila.KNLMeansCL", "com.vapoursynth.ffms2"),
        note=_("Requires OpenCL-capable hardware and working drivers on the host."),
    ),
    VSPreset(
        preset_id="deblock",
        label=_("Deblock"),
        description=_("Targeted block cleanup for damaged or low-bitrate analog transfers."),
        install_roots=("com.holywu.deblock", "com.vapoursynth.ffms2"),
        note=_("Useful as a follow-up filter, not a full restoration chain on its own."),
    ),
    VSPreset(
        preset_id="descratch",
        label=_("DeScratch"),
        description=_("Scratch/dropout repair candidate. Current vsrepo metadata does not expose Linux builds."),
        install_roots=("com.vapoursynth.descratch", "com.vapoursynth.ffms2"),
        note=_("Shown for completeness. Current local vsrepo metadata marks this as unavailable on Linux."),
    ),
)


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


def _find_executable(name: str) -> str | None:
    rc, out, _ = _run_command(["sh", "-lc", f"command -v {shlex.quote(name)}"])
    if rc == 0 and out:
        return out.splitlines()[0].strip()
    return None


def _candidate_vsrepo_dirs() -> list[Path]:
    candidates: list[Path] = []
    env_dir = os.environ.get("VSREPO_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    candidates.extend(
        [
            Path("/mnt/p3-raidz2/linux-projects/vsrepo"),
            Path.home() / "linux-projects" / "vsrepo",
            Path.cwd() / "vsrepo",
            Path.cwd().parent / "vsrepo",
        ]
    )

    for start in (Path.cwd(), Path(__file__).resolve()):
        for parent in [start, *start.parents]:
            candidates.append(parent / "vsrepo")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _find_vsrepo_dir() -> Path | None:
    for candidate in _candidate_vsrepo_dirs():
        if (candidate / "vsrepo.py").is_file() and (candidate / "local").is_dir():
            return candidate
    return None


def _linux_supported(releases: list[dict[str, Any]], package_type: str) -> bool:
    if package_type == "PyScript":
        return True
    for release in releases:
        for key, value in release.items():
            if not key.startswith("linux"):
                continue
            if not isinstance(value, dict):
                continue
            files = value.get("files")
            if isinstance(files, dict) and files:
                return True
    return False


def _collect_payload_files(releases: list[dict[str, Any]]) -> tuple[str, ...]:
    names: set[str] = set()
    for release in releases:
        for value in release.values():
            if not isinstance(value, dict):
                continue
            files = value.get("files")
            if not isinstance(files, dict):
                continue
            for filename in files:
                text = str(filename).strip()
                if text:
                    names.add(text)
    return tuple(sorted(names))


def _load_vsrepo_packages(vsrepo_dir: Path) -> tuple[dict[str, VSPackage], dict[str, VSPackage]]:
    packages: dict[str, VSPackage] = {}
    lookup: dict[str, VSPackage] = {}

    for path in sorted((vsrepo_dir / "local").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        package = VSPackage(
            file_stem=path.stem,
            name=str(data.get("name") or path.stem),
            package_type=str(data.get("type") or "Other"),
            description=str(data.get("description") or ""),
            identifier=str(data.get("identifier") or ""),
            namespace=str(data.get("namespace") or ""),
            modulename=str(data.get("modulename") or ""),
            dependencies=tuple(str(dep) for dep in data.get("dependencies") or []),
            devices=tuple(str(dev) for dev in data.get("device") or []),
            linux_supported=_linux_supported(data.get("releases") or [], str(data.get("type") or "Other")),
            payload_files=_collect_payload_files(data.get("releases") or []),
        )
        packages[path.stem] = package

        for key in (
            package.file_stem,
            package.name,
            package.identifier,
            package.namespace,
            package.modulename,
            package.install_token,
        ):
            text = key.strip().lower()
            if text and text not in lookup:
                lookup[text] = package

    return packages, lookup


def _resolve_package(token: str, lookup: dict[str, VSPackage]) -> VSPackage | None:
    return lookup.get(token.strip().lower())


def _resolve_dependencies(root_tokens: tuple[str, ...], lookup: dict[str, VSPackage]) -> list[tuple[str, VSPackage | None]]:
    resolved: list[tuple[str, VSPackage | None]] = []
    seen: set[str] = set()

    def visit(token: str) -> None:
        key = token.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        package = _resolve_package(token, lookup)
        resolved.append((token, package))
        if package is None:
            return
        for dependency in package.dependencies:
            visit(dependency)

    for root in root_tokens:
        visit(root)
    return resolved


def _detect_host_vapoursynth(modules: list[str], plugin_dirs: list[str]) -> dict[str, Any]:
    script = """
import importlib.util
import json
import os
import sys

mods = json.loads(sys.argv[1])
plugin_dirs = json.loads(sys.argv[2])
payload = {
    'available': False,
    'python': sys.executable,
    'python_version': sys.version.split()[0],
    'modules': {},
    'plugins': [],
    'plugin_dirs': [],
}

for mod in mods:
    try:
        payload['modules'][mod] = importlib.util.find_spec(mod) is not None
    except Exception:
        payload['modules'][mod] = False

try:
    import vapoursynth as vs
    payload['available'] = True
    payload['vapoursynth_version'] = getattr(vs, '__version__', None)
    api = getattr(vs, '__api_version__', None)
    if api is not None:
        payload['api'] = f"R{getattr(api, 'api_major', getattr(api, 'major', '?'))}.{getattr(api, 'api_minor', getattr(api, 'minor', '?'))}"
    core = vs.core
    # Try loading plugins from user/system plugin folders so host probe sees
    # vsrepo-installed filters even when global autoload paths are not configured.
    for folder in plugin_dirs:
        if not folder or not os.path.isdir(folder):
            continue
        try:
            core.std.LoadAllPlugins(path=folder)
            payload['plugin_dirs'].append(folder)
        except Exception:
            # Ignore duplicate-load and incompatible plugin errors in probe mode.
            pass

    core_version = getattr(core, 'version_number', None)
    if callable(core_version):
        core_version = core_version()
    payload['core_version'] = core_version
    try:
        for plugin in core.plugins():
            payload['plugins'].append({
                'identifier': getattr(plugin, 'identifier', ''),
                'namespace': getattr(plugin, 'namespace', ''),
                'name': getattr(plugin, 'name', ''),
            })
    except Exception as exc:
        payload['plugins_error'] = str(exc)
except Exception as exc:
    payload['error'] = str(exc)

print(json.dumps(payload))
"""
    rc, out, err = _run_command(
        ["python3", "-c", script, json.dumps(modules), json.dumps(plugin_dirs)],
        timeout=20.0,
    )
    if rc != 0 or not out:
        return {
            "available": False,
            "error": err or _("Could not query host VapourSynth."),
            "modules": {},
            "plugins": [],
        }
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {
            "available": False,
            "error": _("Host VapourSynth probe returned invalid JSON."),
            "modules": {},
            "plugins": [],
        }


def _detect_vsrepo_binary_path(vsrepo_dir: Path) -> str | None:
    rc, out, _ = _run_command(["python3", str(vsrepo_dir / "vsrepo.py"), "paths"])
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        if line.startswith("Binaries:"):
            path = line.split(":", 1)[1].strip()
            if path:
                return path
    return None


class VapourSynthPage(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        self.set_margin_start(5)
        self.set_margin_end(5)

        self._vsrepo_dir: Path | None = None
        self._package_lookup: dict[str, VSPackage] = {}
        self._encoders: list[EncoderInfo] = []
        self._hardware_info: HardwareInfo | None = None
        self._ffmpeg_command: list[str] | None = None
        self._host_vs_state: dict[str, Any] = {}
        self._probe_plugin_dirs: list[str] = []

        self._build_ui()
        self._populate_presets()
        self.refresh_environment()

    def _build_ui(self) -> None:
        builder = load_builder("vapoursynth_page.ui")
        bind_objects(
            self,
            builder,
            [
                "vs_status_label",
                "vs_paths_label",
                "vs_plugins_label",
                "preset_combo",
                "preset_description_label",
                "preset_summary_label",
                "filters_view",
                "install_command_view",
                "install_notes_label",
                "codec_support_view",
            ],
        )
        self.append(require_object(builder, "vapoursynth_page_root"))

        refresh_button = require_object(builder, "vs_refresh_button")
        refresh_button.connect("clicked", self.on_refresh_clicked)
        self.preset_combo.connect("changed", self.on_preset_changed)

        compact_widget(self.preset_combo, 260)

        self.filters_buffer = self.filters_view.get_buffer()
        self.install_command_buffer = self.install_command_view.get_buffer()
        self.codec_support_buffer = self.codec_support_view.get_buffer()

    def _populate_presets(self) -> None:
        self.preset_combo.remove_all()
        for preset in CURATED_PRESETS:
            self.preset_combo.append(preset.preset_id, preset.label)
        self.preset_combo.set_active_id(CURATED_PRESETS[0].preset_id)

    def sync_capabilities(
        self,
        ffmpeg_command: list[str] | None,
        encoders: list[EncoderInfo],
        hardware_info: HardwareInfo | None,
    ) -> None:
        self._ffmpeg_command = ffmpeg_command
        self._encoders = list(encoders)
        self._hardware_info = hardware_info
        self._update_codec_support()

    def on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self.refresh_environment()

    def on_preset_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self._refresh_preset_view()

    def refresh_environment(self) -> None:
        self._vsrepo_dir = _find_vsrepo_dir()
        self._package_lookup = {}

        python_path = _find_executable("python3")
        vspipe_path = _find_executable("vspipe")

        if self._vsrepo_dir is not None:
            _packages, self._package_lookup = _load_vsrepo_packages(self._vsrepo_dir)

        probe_modules = sorted(
            {
                package.modulename
                for package in self._package_lookup.values()
                if package.modulename
                and package.install_token
                in {
                    "havsfunc",
                    "mvsfunc",
                    "adjust",
                    "nnedi3_resample",
                }
            }
        )
        plugin_dirs: list[str] = list(default_vs_plugin_dirs())
        if self._vsrepo_dir is not None:
            binary_path = _detect_vsrepo_binary_path(self._vsrepo_dir)
            if binary_path and binary_path not in plugin_dirs:
                plugin_dirs.append(binary_path)
        # Preserve order while removing duplicates.
        seen_dirs: set[str] = set()
        dedup_plugin_dirs: list[str] = []
        for folder in plugin_dirs:
            key = folder.strip()
            if not key or key in seen_dirs:
                continue
            seen_dirs.add(key)
            dedup_plugin_dirs.append(key)

        self._host_vs_state = _detect_host_vapoursynth(probe_modules, dedup_plugin_dirs)
        self._probe_plugin_dirs = dedup_plugin_dirs

        if self._host_vs_state.get("available"):
            details: list[str] = [_("VapourSynth detected on host.")]
            core_version = self._host_vs_state.get("core_version")
            api = self._host_vs_state.get("api")
            if core_version is not None:
                details.append(_("Core: ") + str(core_version))
            if api:
                details.append(_("API: ") + str(api))
            self.vs_status_label.set_text(" | ".join(details))
        else:
            self.vs_status_label.set_text(
                _("Host VapourSynth not detected: ") + str(self._host_vs_state.get("error") or _("unknown error"))
            )

        path_lines = [
            _("python3: ") + (python_path or _("not found")),
            _("vspipe: ") + (vspipe_path or _("not found")),
            _("vsrepo: ") + (str(self._vsrepo_dir / "vsrepo.py") if self._vsrepo_dir else _("not found")),
            _("managed root: ") + str(managed_vs_root()),
            _("managed plugins: ") + str(managed_vs_plugin_dir()),
            _("managed scripts: ") + str(managed_vs_script_dir()),
        ]
        loaded_plugin_dirs = self._host_vs_state.get("plugin_dirs") or []
        if loaded_plugin_dirs:
            path_lines.append(_("plugin dirs: ") + ", ".join(str(item) for item in loaded_plugin_dirs))
        self.vs_paths_label.set_text("\n".join(path_lines))

        plugins = self._host_vs_state.get("plugins") or []
        plugin_names = [
            str(item.get("namespace") or item.get("identifier") or item.get("name") or "").strip()
            for item in plugins
        ]
        plugin_names = [name for name in plugin_names if name]
        if plugin_names:
            preview = ", ".join(plugin_names[:12])
            if len(plugin_names) > 12:
                preview += ", ..."
            self.vs_plugins_label.set_text(
                _("Loaded plugin namespaces: ") + preview + f" ({len(plugin_names)})"
            )
        else:
            self.vs_plugins_label.set_text(_("Loaded plugin namespaces: std, resize, text only or none detected."))

        self._refresh_preset_view()
        self._update_codec_support()

    def _refresh_preset_view(self) -> None:
        preset = self._current_preset()
        if preset is None:
            self.preset_description_label.set_text("")
            self.preset_summary_label.set_text("")
            self.filters_buffer.set_text("")
            self.install_command_buffer.set_text("")
            self.install_notes_label.set_text("")
            return

        self.preset_description_label.set_text(preset.description)

        if self._vsrepo_dir is None:
            self.preset_summary_label.set_text(
                _("Local vsrepo checkout not found. Set VSREPO_DIR or keep vsrepo as a sibling folder.")
            )
            self.filters_buffer.set_text(_("Cannot resolve dependencies because vsrepo metadata was not found."))
            self.install_command_buffer.set_text(
                _("Place vsrepo at ../vsrepo relative to this project, or set VSREPO_DIR to the checkout path.")
            )
            self.install_notes_label.set_text(preset.note)
            return

        resolved = _resolve_dependencies(preset.install_roots, self._package_lookup)
        statuses = self._package_statuses(resolved)
        installed_count = sum(1 for status in statuses if status.installed)
        missing_count = sum(1 for status in statuses if (not status.installed and status.available))
        unavailable_count = sum(1 for status in statuses if status.package is not None and not status.available)
        unknown_count = sum(1 for status in statuses if status.package is None)

        summary = [
            _("Preset roots: ") + ", ".join(preset.install_roots),
            _("Installed: ") + str(installed_count),
            _("Missing: ") + str(missing_count),
        ]
        if unavailable_count:
            summary.append(_("No Linux build: ") + str(unavailable_count))
        if unknown_count:
            summary.append(_("Unknown metadata: ") + str(unknown_count))
        self.preset_summary_label.set_text(" | ".join(summary))

        self.filters_buffer.set_text(self._format_filter_status(statuses))
        self.install_command_buffer.set_text(self._build_install_command(preset, statuses))

        notes = [
            preset.note,
            _("vsrepo installs dependencies for the root packages automatically after update."),
            _(
                "If install ends with a vapoursynth-stubs permission error, binaries may still be installed; verify in this tab and only escalate privileges if needed."
            ),
        ]
        if unavailable_count:
            notes.append(_("Some packages in this chain have no Linux build in current vsrepo metadata."))
        self.install_notes_label.set_text(" ".join(note for note in notes if note))

    def _current_preset(self) -> VSPreset | None:
        preset_id = self.preset_combo.get_active_id()
        for preset in CURATED_PRESETS:
            if preset.preset_id == preset_id:
                return preset
        return CURATED_PRESETS[0] if CURATED_PRESETS else None

    def _package_statuses(self, resolved: list[tuple[str, VSPackage | None]]) -> list[PackageStatus]:
        plugins = self._host_vs_state.get("plugins") or []
        module_state = {str(key).lower(): bool(value) for key, value in (self._host_vs_state.get("modules") or {}).items()}
        plugin_identifiers = {str(item.get("identifier") or "").lower() for item in plugins}
        plugin_namespaces = {str(item.get("namespace") or "").lower() for item in plugins}
        search_dirs = [Path(item) for item in (self._host_vs_state.get("plugin_dirs") or self._probe_plugin_dirs)]
        statuses: list[PackageStatus] = []

        for requested, package in resolved:
            if package is None:
                statuses.append(
                    PackageStatus(
                        requested=requested,
                        package=None,
                        installed=False,
                        available=False,
                        reason=_("Dependency not found in local vsrepo metadata."),
                    )
                )
                continue

            installed = False
            if package.package_type == "PyScript":
                if package.modulename:
                    installed = module_state.get(package.modulename.lower(), False)
            else:
                if package.identifier:
                    installed = package.identifier.lower() in plugin_identifiers
                if not installed and package.namespace:
                    installed = package.namespace.lower() in plugin_namespaces
                if not installed and package.payload_files and search_dirs:
                    installed = any((folder / filename).is_file() for folder in search_dirs for filename in package.payload_files)

            reason = None
            if not package.linux_supported:
                reason = _("No Linux build in current vsrepo metadata.")

            statuses.append(
                PackageStatus(
                    requested=requested,
                    package=package,
                    installed=installed,
                    available=package.linux_supported,
                    reason=reason,
                )
            )

        return statuses

    def _format_filter_status(self, statuses: list[PackageStatus]) -> str:
        lines = []
        for status in statuses:
            if status.package is None:
                lines.append(f"UNKNOWN    {status.requested}  ({status.reason or ''})")
                continue

            state = "OK"
            if not status.installed and status.available:
                state = "MISSING"
            elif not status.available:
                state = "NO-LINUX"

            device_text = ", ".join(status.package.devices) if status.package.devices else "cpu"
            package_type = status.package.package_type
            reason = f" | {status.reason}" if status.reason else ""
            lines.append(
                f"{state:<10} {status.package.display_name:<34} {package_type:<8} [{device_text}]{reason}"
            )
        return "\n".join(lines)

    def _build_install_command(self, preset: VSPreset, statuses: list[PackageStatus]) -> str:
        if self._vsrepo_dir is None:
            return _("vsrepo checkout not found. Run commands from your vsrepo directory.")

        installable_roots: list[str] = []
        unavailable_roots: list[str] = []
        for root in preset.install_roots:
            package = _resolve_package(root, self._package_lookup)
            if package is None:
                unavailable_roots.append(root)
                continue
            if package.linux_supported:
                installable_roots.append(package.install_token)
            else:
                unavailable_roots.append(package.install_token)

        if not installable_roots and unavailable_roots:
            return _("This preset is not installable on the current Linux host from current vsrepo metadata.")

        managed_plugin_path = shell_home_path(managed_vs_plugin_dir())
        managed_script_path = shell_home_path(managed_vs_script_dir())

        command_lines = [
            "cd <vsrepo-dir>",
            "python3 vsrepo.py update",
            "python3 vsrepo.py -b "
            + shlex.quote(managed_plugin_path)
            + " -s "
            + shlex.quote(managed_script_path)
            + " install "
            + " ".join(shlex.quote(token) for token in installable_roots),
        ]
        if unavailable_roots:
            command_lines.append("")
            command_lines.append(
                _("Unavailable on this host from current metadata: ") + ", ".join(unavailable_roots)
            )
        return "\n".join(command_lines)

    def _update_codec_support(self) -> None:
        if not self._encoders:
            self.codec_support_buffer.set_text(_("FFmpeg capability scan has not populated encoder data yet."))
            return

        encoder_names = {encoder.name for encoder in self._encoders}
        cpu_video = [name for name in COMMON_CPU_VIDEO_ENCODERS if name in encoder_names]
        gpu_video = [name for name in COMMON_GPU_VIDEO_ENCODERS if name in encoder_names]
        audio = [name for name in COMMON_AUDIO_ENCODERS if name in encoder_names]

        lines = [
            _("Containers: ") + ", ".join(SUPPORTED_CONTAINERS),
            _("CPU video encoders: ") + (", ".join(cpu_video) if cpu_video else _("none detected")),
            _("GPU video encoders: ") + (", ".join(gpu_video) if gpu_video else _("none detected")),
            _("Audio encoders: ") + (", ".join(audio) if audio else _("none detected")),
        ]

        if self._ffmpeg_command:
            lines.append(_("FFmpeg command: ") + " ".join(self._ffmpeg_command))
        if self._hardware_info is not None:
            if self._hardware_info.gpu_lines:
                lines.append(_("Detected GPU: ") + "; ".join(self._hardware_info.gpu_lines))
            elif self._hardware_info.known:
                lines.append(_("Detected GPU: none"))
            else:
                lines.append(_("Detected GPU: unavailable"))

        self.codec_support_buffer.set_text("\n".join(lines))
