from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shutil
import subprocess
from typing import Iterable


@dataclass(frozen=True)
class FFmpegInfo:
    command: list[str] | None
    version_line: str | None
    hwaccels: set[str]
    encoders: set[str]
    error: str | None


@dataclass(frozen=True)
class RendererStatus:
    label: str
    ffmpeg_supported: bool
    hardware_available: bool | None
    usable: bool
    matched: list[str]
    hardware_note: str | None


@dataclass(frozen=True)
class HardwareInfo:
    vendors: set[str]
    gpu_lines: list[str]
    known: bool
    error: str | None


@dataclass(frozen=True)
class EncoderInfo:
    name: str
    flags: str
    description: str
    kind: str


@dataclass(frozen=True)
class PixelFormat:
    name: str
    flags: str


def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID") or os.environ.get("FLATPAK_SANDBOX_DIR"))


def _candidate_ffmpeg_commands() -> list[list[str]]:
    commands: list[list[str]] = []

    if _is_flatpak():
        # Try host FFmpeg if we're in a Flatpak sandbox.
        commands.append(["flatpak-spawn", "--host", "ffmpeg"])

    if shutil.which("ffmpeg"):
        commands.append(["ffmpeg"])

    return commands


def _run_ffmpeg(args: list[str]) -> tuple[list[str] | None, str | None, int | None]:
    last_error: str | None = None
    for base in _candidate_ffmpeg_commands():
        cmd = base + args
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return cmd, output, result.returncode
        except FileNotFoundError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - unexpected runtime error
            last_error = str(exc)
            continue

    return None, last_error, None


def _run_system_command(args: list[str]) -> tuple[list[str] | None, str | None, int | None]:
    cmd = list(args)
    if _is_flatpak():
        cmd = ["flatpak-spawn", "--host"] + cmd
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return cmd, output, result.returncode
    except FileNotFoundError as exc:
        return None, str(exc), None
    except Exception as exc:  # pragma: no cover
        return None, str(exc), None


def _parse_hwaccels(output: str) -> set[str]:
    accels: set[str] = set()
    seen_header = False
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("hardware acceleration methods"):
            seen_header = True
            continue
        if not seen_header:
            continue
        accels.add(stripped)
    return accels


def _encoder_kind(flags: str) -> str:
    if not flags:
        return "unknown"
    kind = flags[0].upper()
    if kind == "V":
        return "video"
    if kind == "A":
        return "audio"
    if kind == "S":
        return "subtitle"
    if kind == "D":
        return "data"
    return "unknown"


def _parse_encoders_info(output: str) -> list[EncoderInfo]:
    encoders: list[EncoderInfo] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("encoders"):
            continue
        if stripped.startswith("--"):
            continue
        parts = stripped.split(None, 2)
        if len(parts) >= 2 and len(parts[0]) == 6:
            flags = parts[0]
            name = parts[1]
            description = parts[2] if len(parts) > 2 else ""
            encoders.append(
                EncoderInfo(
                    name=name,
                    flags=flags,
                    description=description,
                    kind=_encoder_kind(flags),
                )
            )
    return encoders


def _parse_encoders(output: str) -> set[str]:
    return {encoder.name for encoder in _parse_encoders_info(output)}


def _parse_pixel_formats(output: str) -> list[PixelFormat]:
    formats: list[PixelFormat] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("pixel formats"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and len(parts[0]) == 4:
            formats.append(PixelFormat(name=parts[1], flags=parts[0]))
    return formats


def _first_line(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def detect_ffmpeg() -> FFmpegInfo:
    cmd, version_output, rc = _run_ffmpeg(["-version"])
    if cmd is None:
        return FFmpegInfo(command=None, version_line=None, hwaccels=set(), encoders=set(), error=version_output)

    version_line = _first_line(version_output or "")
    if cmd and cmd[-1] == "-version":
        cmd = cmd[:-1]

    _, hw_output, _ = _run_ffmpeg(["-hide_banner", "-hwaccels"])
    _, enc_output, _ = _run_ffmpeg(["-hide_banner", "-encoders"])

    hwaccels = _parse_hwaccels(hw_output or "")
    encoders = _parse_encoders(enc_output or "")

    error = None
    if rc not in (0, None):
        error = "ffmpeg returned non-zero status"

    return FFmpegInfo(
        command=cmd,
        version_line=version_line,
        hwaccels=hwaccels,
        encoders=encoders,
        error=error,
    )


def detect_hardware() -> HardwareInfo:
    cmd, output, _ = _run_system_command(["lspci"])
    if cmd is None or not output:
        return HardwareInfo(vendors=set(), gpu_lines=[], known=False, error=output)

    gpu_lines: list[str] = []
    vendors: set[str] = set()
    gpu_pattern = re.compile(r"(VGA compatible controller|3D controller|Display controller)", re.IGNORECASE)

    for line in output.splitlines():
        if not gpu_pattern.search(line):
            continue
        gpu_lines.append(line.strip())
        lower = line.lower()
        if "nvidia" in lower:
            vendors.add("nvidia")
        if "advanced micro devices" in lower or "amd" in lower or "ati" in lower:
            vendors.add("amd")
        if "intel" in lower:
            vendors.add("intel")

    return HardwareInfo(vendors=vendors, gpu_lines=gpu_lines, known=True, error=None)


def list_encoders() -> list[EncoderInfo]:
    _, enc_output, _ = _run_ffmpeg(["-hide_banner", "-encoders"])
    if not enc_output:
        return []
    return _parse_encoders_info(enc_output)


def list_pixel_formats() -> list[PixelFormat]:
    _, fmt_output, _ = _run_ffmpeg(["-hide_banner", "-pix_fmts"])
    if not fmt_output:
        return []
    return _parse_pixel_formats(fmt_output)


def _matches(tokens: Iterable[str], names: Iterable[str]) -> list[str]:
    matches: list[str] = []
    for name in names:
        for token in tokens:
            if name in token:
                matches.append(token)
    return sorted(set(matches))


def detect_renderers(info: FFmpegInfo, hardware: HardwareInfo | None = None) -> tuple[list[RendererStatus], HardwareInfo]:
    if hardware is None:
        hardware = detect_hardware()
    tokens = set(info.hwaccels) | set(info.encoders)

    rules = [
        ("NVIDIA (CUDA/NVENC)", ["cuda", "nvenc", "nvdec", "cuvid"]),
        ("Intel (QSV)", ["qsv"]),
        ("AMD (AMF)", ["amf"]),
        ("VAAPI (Intel/AMD)", ["vaapi"]),
        ("VDPAU (NVIDIA/AMD)", ["vdpau"]),
        ("Vulkan", ["vulkan"]),
        ("OpenCL", ["opencl"]),
    ]

    results: list[RendererStatus] = []
    for label, keys in rules:
        matched = _matches(tokens, keys)
        ffmpeg_supported = bool(matched)
        hardware_available, note = _hardware_available(label, hardware)
        usable = ffmpeg_supported and hardware_available is True
        results.append(
            RendererStatus(
                label=label,
                ffmpeg_supported=ffmpeg_supported,
                hardware_available=hardware_available,
                usable=usable,
                matched=matched,
                hardware_note=note,
            )
        )

    return results, hardware


def _hardware_available(label: str, hardware: HardwareInfo) -> tuple[bool | None, str | None]:
    if not hardware.known:
        return None, "Hardware detection unavailable"

    vendors = hardware.vendors
    any_gpu = bool(vendors)

    if label.startswith("NVIDIA"):
        return ("nvidia" in vendors), _vendor_note(hardware)
    if label.startswith("Intel"):
        return ("intel" in vendors), _vendor_note(hardware)
    if label.startswith("AMD"):
        return ("amd" in vendors), _vendor_note(hardware)
    if label.startswith("VAAPI"):
        return (("intel" in vendors) or ("amd" in vendors)), _vendor_note(hardware)
    if label.startswith("VDPAU"):
        return (("nvidia" in vendors) or ("amd" in vendors)), _vendor_note(hardware)
    if label.startswith("Vulkan"):
        return (any_gpu if vendors else False), _vendor_note(hardware)
    if label.startswith("OpenCL"):
        return (any_gpu if vendors else False), _vendor_note(hardware)

    return None, None


def _vendor_note(hardware: HardwareInfo) -> str | None:
    if not hardware.known:
        return "Hardware detection unavailable"
    if not hardware.vendors:
        return "No GPU detected"
    return "Detected GPU: " + ", ".join(sorted(hardware.vendors))
