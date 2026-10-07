"""Gettext setup shared by the CLI and the GUI."""

import gettext
from pathlib import Path

DOMAIN = "debtap-mod"


def _locale_dir() -> str:
    # usr/share/debtap-mod/debtap_mod/i18n.py -> usr/share/locale
    # Works both from the git checkout and from the installed package.
    candidate = Path(__file__).resolve().parents[2] / "locale"
    if candidate.is_dir():
        return str(candidate)
    return "/usr/share/locale"


LOCALE_DIR = _locale_dir()
_translation = gettext.translation(DOMAIN, LOCALE_DIR, fallback=True)


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
