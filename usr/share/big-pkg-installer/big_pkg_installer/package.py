"""Open a .deb or .rpm file behind one interface."""

import os

from .debfile import DebError, DebPackage, open_deb
from .i18n import _
from .rpmfile import RpmPackage, is_rpm, open_rpm

Package = DebPackage | RpmPackage


def open_package(path: str | os.PathLike) -> Package:
    if is_rpm(path):
        return open_rpm(path)
    try:
        return open_deb(path)
    except DebError as exc:
        if str(path).lower().endswith(".rpm"):
            raise DebError(_("The RPM package is truncated or corrupted.")) from exc
        raise
