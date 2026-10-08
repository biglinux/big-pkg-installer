"""Debian version strings -> pacman epoch/pkgver/pkgrel."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PacmanVersion:
    epoch: int
    pkgver: str
    pkgrel: str

    def __str__(self) -> str:
        prefix = f"{self.epoch}:" if self.epoch else ""
        return f"{prefix}{self.pkgver}-{self.pkgrel}"


def split_debian_version(version: str) -> tuple[int, str, str]:
    """Split ``[epoch:]upstream[-revision]`` following Debian policy 5.6.12."""
    version = version.strip()
    epoch = 0
    if ":" in version:
        head, rest = version.split(":", 1)
        if head.isdigit():
            epoch = int(head)
            version = rest
    upstream, revision = version, ""
    if "-" in version:
        upstream, revision = version.rsplit("-", 1)
    return epoch, upstream, revision


def _sanitize_pkgver(upstream: str) -> str:
    # A tilde sorts before everything in dpkg; pacman sorts a trailing alpha
    # block before the bare release, so "1.0~rc1" -> "1.0rc1" keeps the order.
    upstream = re.sub(r"~(?=[A-Za-z])", "", upstream)
    upstream = upstream.replace("~", ".").replace("-", "_").replace(":", ".")
    upstream = re.sub(r"[^A-Za-z0-9._+]", "", upstream)
    upstream = re.sub(r"\.{2,}", ".", upstream).strip("._+")
    return upstream or "0"


def to_pacman_version(version: str) -> PacmanVersion:
    epoch, upstream, revision = split_debian_version(version)
    match = re.match(r"(\d+)(?:\.(\d+))?", revision)
    if match:
        pkgrel = match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}"
    else:
        pkgrel = "1"
    return PacmanVersion(epoch=epoch, pkgver=_sanitize_pkgver(upstream), pkgrel=pkgrel)


_PKGNAME_INVALID = re.compile(r"[^a-z0-9@._+-]")


def to_pacman_name(name: str) -> str:
    name = _PKGNAME_INVALID.sub("-", name.strip().lower())
    return name.lstrip("-.") or "converted-package"
