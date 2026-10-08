"""Safe extraction shared by the .deb (tar) and .rpm (cpio) readers.

Entries escaping the destination through ``..`` or through a previously
extracted symlink are refused instead of being written outside. Modes are
kept exactly (setuid helpers such as chrome-sandbox included) and non-root
owners are recorded so the package can restore them under fakeroot.
"""

import os
import posixpath
import stat
from dataclasses import dataclass, field
from pathlib import Path


class Cancelled(Exception):
    """The user cancelled the operation."""


@dataclass
class ExtractResult:
    owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def normalize_member(name: str) -> str | None:
    """Relative path inside the package, "" for the root, None if unsafe."""
    norm = posixpath.normpath("/" + name).lstrip("/")
    if norm in ("", "."):
        return ""
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return None
    return norm


class SafeWriter:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = os.path.realpath(root)
        self.result = ExtractResult()
        self._dir_modes: list[tuple[str, int, float]] = []

    def _inside(self, path: str) -> bool:
        real = os.path.realpath(path)
        return real == self.root or real.startswith(self.root + os.sep)

    def _prepare(self, name: str, owner: tuple[str, str] | None) -> str | None:
        """Validate ``name`` and return the destination path (or None)."""
        rel = normalize_member(name)
        if rel is None:
            self.result.skipped.append(name)
            return None
        if rel == "":
            return None
        dest = os.path.join(self.root, rel)
        parent = os.path.dirname(dest)
        if not self._inside(parent):
            self.result.skipped.append(name)
            return None
        os.makedirs(parent, exist_ok=True)
        if owner and owner != ("root", "root"):
            self.result.owners[rel] = owner
        return dest

    @staticmethod
    def _clear(dest: str) -> None:
        if os.path.lexists(dest) and not (os.path.isdir(dest) and not os.path.islink(dest)):
            os.unlink(dest)

    def directory(self, name: str, mode: int, mtime: float, owner=None) -> None:
        dest = self._prepare(name, owner)
        if dest is None:
            return
        self._clear(dest)
        os.makedirs(dest, exist_ok=True)
        os.chmod(dest, 0o755)
        self._dir_modes.append((dest, mode, mtime))

    def file(self, name: str, mode: int, mtime: float, source, owner=None) -> None:
        dest = self._prepare(name, owner)
        if dest is None:
            return
        self._clear(dest)
        with open(dest, "wb") as out:
            while chunk := source.read(1 << 20):
                out.write(chunk)
        os.chmod(dest, stat.S_IMODE(mode))
        os.utime(dest, (mtime, mtime))

    def symlink(self, name: str, target: str, owner=None) -> None:
        dest = self._prepare(name, owner)
        if dest is None:
            return
        self._clear(dest)
        os.symlink(target, dest)

    def hardlink(self, name: str, target_name: str, owner=None) -> None:
        dest = self._prepare(name, owner)
        if dest is None:
            return
        target_rel = normalize_member(target_name)
        target = os.path.join(self.root, target_rel or "")
        if target_rel and os.path.isfile(target) and self._inside(target):
            self._clear(dest)
            os.link(target, dest)
        else:
            self.result.skipped.append(name)

    def skip(self, name: str) -> None:
        # device nodes and fifos make no sense inside a package
        self.result.skipped.append(name)

    def finish(self) -> ExtractResult:
        for path, mode, mtime in reversed(self._dir_modes):
            os.chmod(path, stat.S_IMODE(mode) | stat.S_IRWXU)
            os.utime(path, (mtime, mtime))
        return self.result
