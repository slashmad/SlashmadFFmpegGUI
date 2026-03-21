from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import shutil
import tempfile
from typing import Iterable


COMMON_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".ppm",
    ".pgm",
    ".pnm",
    ".exr",
    ".hdr",
    ".heic",
    ".heif",
    ".avif",
}

RAW_EXTENSIONS = {
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".raf",
    ".orf",
    ".rw2",
    ".pef",
    ".srw",
    ".3fr",
    ".erf",
    ".kdc",
    ".mrw",
    ".raw",
    ".rwl",
    ".sr2",
    ".srf",
}

SUPPORTED_IMAGE_EXTENSIONS = COMMON_IMAGE_EXTENSIONS | RAW_EXTENSIONS


@dataclass(frozen=True)
class InputCollection:
    paths: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class PreparedInputs:
    paths: list[str]
    warnings: list[str]
    temp_dir: str | None


def _is_supported(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in SUPPORTED_IMAGE_EXTENSIONS


def collect_inputs(items: Iterable[str]) -> InputCollection:
    warnings: list[str] = []
    files: list[str] = []
    seen: set[str] = set()
    saw_raw = False

    for item in items:
        if not item:
            continue
        path = os.path.abspath(os.path.expanduser(item))
        if not os.path.exists(path):
            warnings.append(f"Path not found: {path}")
            continue
        if os.path.isdir(path):
            try:
                entries = sorted(os.listdir(path), key=str.casefold)
            except OSError:
                warnings.append(f"Cannot read folder: {path}")
                continue
            count_before = len(files)
            for entry in entries:
                candidate = os.path.join(path, entry)
                if os.path.isfile(candidate) and _is_supported(candidate):
                    if candidate not in seen:
                        files.append(candidate)
                        seen.add(candidate)
                        if os.path.splitext(candidate)[1].lower() in RAW_EXTENSIONS:
                            saw_raw = True
            if len(files) == count_before:
                warnings.append(f"No supported images in folder: {path}")
            continue

        if os.path.isfile(path):
            if _is_supported(path):
                if path not in seen:
                    files.append(path)
                    seen.add(path)
                    if os.path.splitext(path)[1].lower() in RAW_EXTENSIONS:
                        saw_raw = True
            else:
                warnings.append(f"Unsupported file: {path}")

    if saw_raw:
        warnings.append(
            "RAW files detected. Embedded JPEG previews will be used for timelapse rendering."
        )

    return InputCollection(paths=files, warnings=warnings)


def _largest_embedded_jpeg(raw_path: str) -> bytes | None:
    with open(raw_path, "rb") as handle:
        data = handle.read()

    largest: bytes | None = None
    pos = 0
    while True:
        start = data.find(b"\xff\xd8\xff", pos)
        if start == -1:
            break
        end = data.find(b"\xff\xd9", start + 2)
        if end == -1:
            break
        segment = data[start : end + 2]
        if largest is None or len(segment) > len(largest):
            largest = segment
        pos = start + 3

    return largest


def prepare_inputs_for_timelapse(image_paths: list[str]) -> PreparedInputs:
    raw_paths = [path for path in image_paths if os.path.splitext(path)[1].lower() in RAW_EXTENSIONS]
    if not raw_paths:
        return PreparedInputs(paths=list(image_paths), warnings=[], temp_dir=None)

    prepared_paths: list[str] = []
    temp_dir = tempfile.mkdtemp(prefix="slashmad-raw-previews-")
    used_names: set[str] = set()

    try:
        for path in image_paths:
            ext = os.path.splitext(path)[1].lower()
            if ext not in RAW_EXTENSIONS:
                prepared_paths.append(path)
                continue

            preview = _largest_embedded_jpeg(path)
            if preview is None:
                raise RuntimeError(
                    f"No embedded JPEG preview found in RAW file: {path}. "
                    "Install darktable-cli or a RAW-capable FFmpeg/libraw setup for full RAW timelapse support."
                )

            base = os.path.splitext(os.path.basename(path))[0] + ".jpg"
            candidate = base
            suffix = 1
            while candidate in used_names:
                stem = os.path.splitext(base)[0]
                candidate = f"{stem}-{suffix}.jpg"
                suffix += 1
            used_names.add(candidate)

            preview_path = os.path.join(temp_dir, candidate)
            with open(preview_path, "wb") as handle:
                handle.write(preview)
            prepared_paths.append(preview_path)

        return PreparedInputs(paths=prepared_paths, warnings=[], temp_dir=temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def write_concat_file(image_paths: list[str], fps: float | None) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ffconcat")
    try:
        handle.write("ffconcat version 1.0\n")
        duration = None
        if fps and fps > 0:
            duration = 1.0 / fps
        last_path = None
        for path in image_paths:
            safe_path = path.replace("'", "'\\''")
            handle.write(f"file '{safe_path}'\n")
            if duration is not None:
                handle.write(f"duration {duration:.6f}\n")
            last_path = path
        if last_path and duration is not None:
            safe_path = last_path.replace("'", "'\\''")
            handle.write(f"file '{safe_path}'\n")
    finally:
        handle.close()
    return handle.name


def build_ffmpeg_command(
    ffmpeg_cmd: list[str],
    list_file: str,
    output_file: str,
    fps: float | None,
    codec: str | None,
    quality: int | None,
    preset: str | None,
    tune: str | None,
    pix_fmt: str | None,
    extra_args: str | None,
) -> list[str]:
    cmd = list(ffmpeg_cmd)
    cmd += ["-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", list_file]

    if fps and fps > 0:
        cmd += ["-r", str(fps), "-fps_mode", "cfr"]

    if codec:
        cmd += ["-c:v", codec]

    if preset:
        cmd += ["-preset", preset]

    if tune:
        cmd += ["-tune", tune]

    if quality is not None:
        flag = quality_flag_for_codec(codec)
        if flag:
            cmd += [flag, str(quality)]

    if pix_fmt:
        cmd += ["-pix_fmt", pix_fmt]

    if extra_args:
        cmd += shlex.split(extra_args)

    cmd.append(output_file)
    return cmd


def build_command_preview(
    ffmpeg_cmd: list[str],
    output_file: str,
    fps: float | None,
    codec: str | None,
    quality: int | None,
    preset: str | None,
    tune: str | None,
    pix_fmt: str | None,
    extra_args: str | None,
) -> str:
    placeholder = "/path/to/list.ffconcat"
    cmd = build_ffmpeg_command(
        ffmpeg_cmd=ffmpeg_cmd,
        list_file=placeholder,
        output_file=output_file,
        fps=fps,
        codec=codec,
        quality=quality,
        preset=preset,
        tune=tune,
        pix_fmt=pix_fmt,
        extra_args=extra_args,
    )
    return " ".join(shlex.quote(part) for part in cmd)


def quality_flag_for_codec(codec: str | None) -> str | None:
    if not codec:
        return None
    name = codec.lower()
    if "nvenc" in name:
        return "-cq"
    if "x264" in name or "x265" in name:
        return "-crf"
    if name in {"libvpx-vp9", "libaom-av1", "libsvtav1"}:
        return "-crf"
    return None
