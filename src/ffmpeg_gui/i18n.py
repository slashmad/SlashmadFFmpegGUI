from __future__ import annotations

import gettext
import locale
import os


APP_ID = "com.slashmad.SlashmadFFmpegGUI"


def setup_gettext() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    localedir = os.path.join(os.path.dirname(__file__), "locale")
    gettext.bindtextdomain(APP_ID, localedir)
    gettext.textdomain(APP_ID)


def _(text: str) -> str:
    return gettext.gettext(text)
