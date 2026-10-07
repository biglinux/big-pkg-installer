"""Reading .deb files: the ar container, the control archive and the data tree."""

import bz2
import gzip
import io
import lzma
import os
import posixpath
import stat
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from compression import zstd

from .i18n import _

MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm")


class DebError(Exception):
    """The file is not a usable Debian package."""


class Cancelled(Exception):
    """The user cancelled the operation."""


@dataclass
class ArMember:
    name: str
    offset: int
    size: int


class _Slice(io.RawIOBase):
    """Read-only window over a region of a file, counting consumed bytes."""

    def __init__(self, fileobj, offset: int, size: int):
        self._f = fileobj
        self._start = offset
        self._size = size
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._size - self.consumed
        if remaining <= 0:
            return 0
        self._f.seek(self._start + self.consumed)
        n = self._f.readinto(memoryview(buffer)[: min(len(buffer), remaining)])
        self.consumed += n
        return n


def read_ar_members(path: Path) -> list[ArMember]:
    members = []
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise DebError(_("The file is not a Debian package (missing ar header)."))
        while True:
            header = f.read(60)
            if not header:
                break
            if len(header) < 60 or header[58:60] != b"`\n":
                raise DebError(_("The Debian package is truncated or corrupted."))
            name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError as exc:
                raise DebError(_("The Debian package is truncated or corrupted.")) from exc
            members.append(ArMember(name=name, offset=f.tell(), size=size))
            f.seek(size + (size % 2), os.SEEK_CUR)
    return members


def _decompressor(name: str, raw):
    if name.endswith(".gz"):
        return gzip.GzipFile(fileobj=raw)
    if name.endswith(".xz") or name.endswith(".lzma"):
        return lzma.LZMAFile(raw)
    if name.endswith(".bz2"):
        return bz2.BZ2File(raw)
    if name.endswith(".zst"):
        return zstd.ZstdFile(raw)
    if name.endswith(".tar"):
        return io.BufferedReader(raw, buffer_size=1 << 20)
    raise DebError(_("Unsupported compression in member {name}.").format(name=name))


def parse_control(text: str) -> dict[str, str]:
    """Parse the first deb822 paragraph. Keys are lower-cased."""
    fields: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            if fields:
                break
            continue
        if line[0] in " \t":
            if current is None:
                continue
            value = line.strip()
            fields[current] += "\n" + ("" if value == "." else value)
        elif ":" in line:
            key, value = line.split(":", 1)
            current = key.strip().lower()
            fields[current] = value.strip()
    return fields


@dataclass
class DebPackage:
    path: Path
    control: dict[str, str]
    control_files: dict[str, bytes]
    data_member: ArMember

    @property
    def name(self) -> str:
        return self.control.get("package", "")

    @property
    def version(self) -> str:
        return self.control.get("version", "")

    @property
    def architecture(self) -> str:
        return self.control.get("architecture", "all")

    @property
    def summary(self) -> str:
        return self.control.get("description", "").split("\n", 1)[0].strip()

    @property
    def long_description(self) -> str:
        parts = self.control.get("description", "").split("\n", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def installed_size(self) -> int:
        """Installed size in bytes (the control field is in KiB)."""
        try:
            return int(self.control.get("installed-size", "0")) * 1024
        except ValueError:
            return 0

    def script(self, name: str) -> str | None:
        data = self.control_files.get(name)
        return data.decode("utf-8", "replace") if data is not None else None

    @property
    def conffiles(self) -> list[str]:
        text = self.control_files.get("conffiles", b"").decode("utf-8", "replace")
        result = []
        for line in text.splitlines():
            line = line.strip()
            # dpkg >= 1.20 allows a "remove-on-upgrade" flag before the path
            if line.startswith("remove-on-upgrade "):
                continue
            if line.startswith("/"):
                result.append(line)
        return result


def open_deb(path: str | os.PathLike) -> DebPackage:
    path = Path(path)
    if not path.is_file():
        raise DebError(_("File not found: {path}").format(path=path))
    members = read_ar_members(path)
    names = [m.name for m in members]
    if "debian-binary" not in names:
        raise DebError(_("The file is not a Debian package (debian-binary missing)."))
    control_member = next((m for m in members if m.name.startswith("control.tar")), None)
    data_member = next((m for m in members if m.name.startswith("data.tar")), None)
    if control_member is None or data_member is None:
        raise DebError(_("The Debian package has no control or data archive."))

    control_files: dict[str, bytes] = {}
    with open(path, "rb") as f:
        raw = _Slice(f, control_member.offset, control_member.size)
        with _decompressor(control_member.name, raw) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for info in tar:
                    if not info.isreg():
                        continue
                    name = posixpath.basename(info.name)
                    extracted = tar.extractfile(info)
                    if extracted is not None:
                        control_files[name] = extracted.read()

    if "control" not in control_files:
        raise DebError(_("The Debian package has no control file."))
    control = parse_control(control_files["control"].decode("utf-8", "replace"))
    if not control.get("package") or not control.get("version"):
        raise DebError(_("The control file has no Package or Version field."))
    return DebPackage(path=path, control=control, control_files=control_files, data_member=data_member)


@dataclass
class ExtractResult:
    owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _normalize_member(name: str) -> str | None:
    norm = posixpath.normpath("/" + name).lstrip("/")
    if norm in ("", "."):
        return ""
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return None
    return norm


def extract_data(
    deb: DebPackage,
    root: Path,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ExtractResult:
    """Extract data.tar into ``root`` safely, preserving modes (setuid too).

    Members escaping ``root`` through ``..`` or through a previously
    extracted symlink are refused instead of being written outside.
    """
    root.mkdir(parents=True, exist_ok=True)
    real_root = os.path.realpath(root)
    result = ExtractResult()
    dir_modes: list[tuple[str, int, float]] = []
    member = deb.data_member

    def inside_root(path: str) -> bool:
        real = os.path.realpath(path)
        return real == real_root or real.startswith(real_root + os.sep)

    with open(deb.path, "rb") as f:
        raw = _Slice(f, member.offset, member.size)
        with _decompressor(member.name, raw) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for count, info in enumerate(tar):
                    if cancelled and count % 64 == 0 and cancelled():
                        raise Cancelled()
                    if progress and count % 32 == 0:
                        progress(raw.consumed / max(member.size, 1))
                    rel = _normalize_member(info.name)
                    if rel is None:
                        result.skipped.append(info.name)
                        continue
                    if rel == "":
                        continue
                    dest = os.path.join(real_root, rel)
                    parent = os.path.dirname(dest)
                    if not inside_root(parent):
                        result.skipped.append(info.name)
                        continue
                    os.makedirs(parent, exist_ok=True)
                    uname = info.uname or ("root" if info.uid == 0 else str(info.uid))
                    gname = info.gname or ("root" if info.gid == 0 else str(info.gid))
                    if (uname, gname) != ("root", "root"):
                        result.owners[rel] = (uname, gname)

                    if info.isdir():
                        if os.path.lexists(dest) and not os.path.isdir(dest):
                            os.unlink(dest)
                        os.makedirs(dest, exist_ok=True)
                        os.chmod(dest, 0o755)
                        dir_modes.append((dest, info.mode, info.mtime))
                        continue
                    if os.path.lexists(dest) and not os.path.isdir(dest):
                        os.unlink(dest)
                    if info.isreg():
                        source = tar.extractfile(info)
                        with open(dest, "wb") as out:
                            while chunk := source.read(1 << 20):
                                out.write(chunk)
                        os.chmod(dest, stat.S_IMODE(info.mode))
                        os.utime(dest, (info.mtime, info.mtime))
                    elif info.issym():
                        os.symlink(info.linkname, dest)
                    elif info.islnk():
                        target_rel = _normalize_member(info.linkname)
                        target = os.path.join(real_root, target_rel or "")
                        if target_rel and os.path.isfile(target) and inside_root(target):
                            os.link(target, dest)
                        else:
                            result.skipped.append(info.name)
                    else:
                        # device nodes and fifos make no sense inside a package
                        result.skipped.append(info.name)

    for path, mode, mtime in reversed(dir_modes):
        os.chmod(path, stat.S_IMODE(mode) | stat.S_IRWXU)
        os.utime(path, (mtime, mtime))
    if progress:
        progress(1.0)
    return result
