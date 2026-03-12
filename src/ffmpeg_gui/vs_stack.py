from __future__ import annotations

import os
from pathlib import Path


def managed_vs_root() -> Path:
    env = os.environ.get("SLASHMAD_VS_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "SlashmadFFmpegGUI" / "vs"


def managed_vs_plugin_dir() -> Path:
    return managed_vs_root() / "plugins"


def managed_vs_script_dir() -> Path:
    return managed_vs_root() / "scripts"


def default_vs_plugin_dirs() -> list[str]:
    candidates = [
        str(managed_vs_plugin_dir()),
        str(Path.home() / ".local" / "lib" / "vapoursynth"),
        "/usr/local/lib/vapoursynth",
        "/usr/lib64/vapoursynth",
        "/usr/lib/vapoursynth",
    ]
    dedup: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return dedup


def default_vs_script_dirs() -> list[str]:
    candidates = [
        str(managed_vs_script_dir()),
    ]
    dedup: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return dedup


def _shell_escape_dquote(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def vs_plugin_path_prefix_shell() -> str:
    static_paths = _shell_escape_dquote(":".join(default_vs_plugin_dirs()))
    return f'VAPOURSYNTH_PLUGIN_PATH="{static_paths}:${{VAPOURSYNTH_PLUGIN_PATH:-}}" '


def vs_pythonpath_prefix_shell() -> str:
    static_paths = _shell_escape_dquote(":".join(default_vs_script_dirs()))
    return f'PYTHONPATH="{static_paths}:${{PYTHONPATH:-}}" '


def vs_runtime_prefix_shell() -> str:
    return vs_plugin_path_prefix_shell() + vs_pythonpath_prefix_shell()


def shell_home_path(path: Path) -> str:
    text = str(path.expanduser())
    home = str(Path.home())
    if text == home:
        return "$HOME"
    if text.startswith(home + os.sep):
        return "$HOME/" + text[len(home + os.sep) :]
    return text
