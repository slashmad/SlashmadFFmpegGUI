#!/bin/sh
set -eu

cd "$(dirname "$0")"
FFMPEG_GUI_APP_ID="${FFMPEG_GUI_APP_ID:-io.github.slashmad.SlashmadFFmpegGUI.dev}" \
PYTHONPATH=src python3 -m ffmpeg_gui.app
