"""Reading .rpm files: lead, signature and main headers, and the cpio payload."""

import bz2
import gzip
import io
import lzma
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from compression import zstd

from .debfile import DebError, _Slice
from .extract import Cancelled, ExtractResult, SafeWriter
from .i18n import _
from .version import PacmanVersion, _sanitize_pkgver

RPM_MAGIC = b"\xed\xab\xee\xdb"
HEADER_MAGIC = b"\x8e\xad\xe8\x01"

# header tags
NAME, VERSION, RELEASE, EPOCH, SUMMARY, DESCRIPTION = 1000, 1001, 1002, 1003, 1004, 1005
SIZE, VENDOR, PACKAGER, URL, ARCH = 1009, 1011, 1015, 1020, 1022
PREIN, POSTIN, PREUN, POSTUN = 1023, 1024, 1025, 1026
FILEFLAGS, FILEUSERNAME, FILEGROUPNAME, SOURCERPM = 1037, 1039, 1040, 1044
REQUIREFLAGS, REQUIRENAME = 1048, 1049
PREINPROG, POSTINPROG, PREUNPROG, POSTUNPROG = 1085, 1086, 1087, 1088
DIRINDEXES, BASENAMES, DIRNAMES = 1116, 1117, 1118
PAYLOADFORMAT, PAYLOADCOMPRESSOR = 1124, 1125
PRETRANS, POSTTRANS, PRETRANSPROG, POSTTRANSPROG = 1151, 1152, 1153, 1154
LONGSIZE, RECOMMENDNAME, SUGGESTNAME = 5009, 5046, 5049
FILESIZES, FILEMODES, FILEMTIMES, FILELINKTOS = 1028, 1030, 1034, 1036
FILEDEVICES, FILEINODES, LONGFILESIZES = 1095, 1096, 5008

RPMFILE_CONFIG, RPMFILE_GHOST = 1 << 0, 1 << 6
RPMSENSE_RPMLIB = 1 << 24

# scriptlet name -> (body tag, interpreter tag)
SCRIPTLETS = {
    "pretrans": (PRETRANS, PRETRANSPROG),
    "pre": (PREIN, PREINPROG),
    "post": (POSTIN, POSTINPROG),
    "preun": (PREUN, PREUNPROG),
    "postun": (POSTUN, POSTUNPROG),
    "posttrans": (POSTTRANS, POSTTRANSPROG),
}

RPM_TO_PACMAN_ARCH = {
    "x86_64": "x86_64", "amd64": "x86_64", "noarch": "any", "aarch64": "aarch64",
    "i386": "i686", "i486": "i686", "i586": "i686", "i686": "i686",
    "armv7hl": "armv7h", "armv7l": "armv7h", "riscv64": "riscv64",
}


def _read_header(f) -> dict[int, object]:
    intro = f.read(16)
    if len(intro) < 16 or intro[:4] != HEADER_MAGIC:
        raise DebError(_("The RPM package is truncated or corrupted."))
    nindex, hsize = struct.unpack(">II", intro[8:16])
    if nindex > 100_000 or hsize > 256 << 20:
        raise DebError(_("The RPM package is truncated or corrupted."))
    index = f.read(nindex * 16)
    store = f.read(hsize)
    if len(index) < nindex * 16 or len(store) < hsize:
        raise DebError(_("The RPM package is truncated or corrupted."))
    tags: dict[int, object] = {}
    for i in range(nindex):
        tag, kind, offset, count = struct.unpack_from(">iiii", index, i * 16)
        try:
            tags[tag] = _value(store, kind, offset, count)
        except (struct.error, IndexError, UnicodeDecodeError):
            continue
    return tags


def _strings(store: bytes, offset: int, count: int) -> list[str]:
    out = []
    for _i in range(count):
        end = store.index(b"\0", offset)
        out.append(store[offset:end].decode("utf-8", "replace"))
        offset = end + 1
    return out


def _value(store: bytes, kind: int, offset: int, count: int):
    if kind == 6:  # STRING
        return _strings(store, offset, 1)[0]
    if kind in (8, 9):  # STRING_ARRAY, I18NSTRING
        return _strings(store, offset, count)
    if kind == 4:
        return list(struct.unpack_from(f">{count}I", store, offset))
    if kind == 5:
        return list(struct.unpack_from(f">{count}Q", store, offset))
    if kind == 3:
        return list(struct.unpack_from(f">{count}H", store, offset))
    if kind in (1, 2):
        return list(store[offset:offset + count])
    if kind == 7:
        return store[offset:offset + count]
    return None


def _first(value, default=""):
    if isinstance(value, list):
        return value[0] if value else default
    return default if value is None else value


@dataclass
class RpmScript:
    body: str
    interpreter: list[str]

    @property
    def is_lua(self) -> bool:
        return bool(self.interpreter) and self.interpreter[0] == "<lua>"


@dataclass
class RpmPackage:
    path: Path
    tags: dict[int, object]
    payload_offset: int
    payload_size: int

    kind = "rpm"
    name_suffix = "-rpm"

    # ----------------------------------------------------------- metadata
    def _tag(self, tag: int, default=""):
        value = self.tags.get(tag, default)
        return _first(value, default) if isinstance(value, list) else value

    @property
    def name(self) -> str:
        return str(self._tag(NAME))

    @property
    def epoch(self) -> int:
        value = self.tags.get(EPOCH)
        return int(value[0]) if isinstance(value, list) and value else 0

    @property
    def version(self) -> str:
        release = self._tag(RELEASE)
        text = f"{self._tag(VERSION)}-{release}" if release else str(self._tag(VERSION))
        return f"{self.epoch}:{text}" if self.epoch else text

    @property
    def architecture(self) -> str:
        return str(self._tag(ARCH)) or "noarch"

    @property
    def pacman_arch(self) -> str:
        return RPM_TO_PACMAN_ARCH.get(self.architecture, self.architecture)

    @property
    def summary(self) -> str:
        return str(self._tag(SUMMARY)).strip()

    @property
    def long_description(self) -> str:
        return str(self._tag(DESCRIPTION)).strip()

    @property
    def installed_size(self) -> int:
        for tag in (LONGSIZE, SIZE):
            value = self.tags.get(tag)
            if isinstance(value, list) and value:
                return int(value[0])
        return 0

    @property
    def maintainer(self) -> str:
        return str(self._tag(PACKAGER) or self._tag(VENDOR))

    @property
    def homepage(self) -> str:
        return str(self._tag(URL)).strip()

    @property
    def control(self) -> dict[str, str]:
        """Debian-like view used by generic code (homepage, maintainer)."""
        return {"package": self.name, "version": self.version, "homepage": self.homepage,
                "maintainer": self.maintainer, "description": self.summary}

    def pacman_version(self) -> PacmanVersion:
        release = str(self._tag(RELEASE))
        match = re.match(r"(\d+)(?:\.(\d+))?", release)
        pkgrel = (match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}") if match else "1"
        upstream = str(self._tag(VERSION)).replace("^", ".")
        return PacmanVersion(epoch=self.epoch, pkgver=_sanitize_pkgver(upstream), pkgrel=pkgrel)

    # ------------------------------------------------------------- files
    def file_list(self) -> list[str]:
        dirs = self.tags.get(DIRNAMES) or []
        bases = self.tags.get(BASENAMES) or []
        indexes = self.tags.get(DIRINDEXES) or []
        return [dirs[i] + b for b, i in zip(bases, indexes, strict=False) if i < len(dirs)]

    def _file_attr(self, tag: int) -> list:
        value = self.tags.get(tag)
        return value if isinstance(value, list) else []

    @property
    def conffiles(self) -> list[str]:
        files, flags = self.file_list(), self._file_attr(FILEFLAGS)
        return [f for f, fl in zip(files, flags, strict=False) if fl & RPMFILE_CONFIG and not fl & RPMFILE_GHOST]

    def owners(self) -> dict[str, tuple[str, str]]:
        users, groups = self._file_attr(FILEUSERNAME), self._file_attr(FILEGROUPNAME)
        return {f: (u, g) for f, u, g in zip(self.file_list(), users, groups, strict=False)}

    # ----------------------------------------------------- dependencies
    def requires(self) -> list[str]:
        names = self._file_attr(REQUIRENAME)
        flags = self._file_attr(REQUIREFLAGS) or [0] * len(names)
        return [n for n, fl in zip(names, flags, strict=False) if not fl & RPMSENSE_RPMLIB]

    def declared_dependencies(self):
        from .deps import rpm_declared

        return rpm_declared(self.requires(), self._file_attr(RECOMMENDNAME), self._file_attr(SUGGESTNAME))

    # ---------------------------------------------------------- scripts
    def scripts(self) -> dict[str, RpmScript]:
        found = {}
        for name, (body_tag, prog_tag) in SCRIPTLETS.items():
            body = self.tags.get(body_tag)
            prog = self.tags.get(prog_tag)
            interpreter = prog if isinstance(prog, list) else ([prog] if prog else [])
            if body or interpreter:
                found[name] = RpmScript(body=body or "", interpreter=interpreter or ["/bin/sh"])
        return found

    def install_script(self, skip: list[str], extra_post_install: str = ""):
        from .scripts import build_install_rpm

        scripts = {n: s for n, s in self.scripts().items() if n not in skip}
        return build_install_rpm(scripts, self.name.lower(), extra_post_install)

    # -------------------------------------------------------- extraction
    def _payload_stream(self, raw):
        compressor = str(self._tag(PAYLOADCOMPRESSOR) or "gzip")
        if compressor == "gzip":
            return gzip.GzipFile(fileobj=raw)
        if compressor in ("xz", "lzma"):
            return lzma.LZMAFile(raw)
        if compressor == "bzip2":
            return bz2.BZ2File(raw)
        if compressor == "zstd":
            return zstd.ZstdFile(raw)
        if compressor in ("none", "identity"):
            return io.BufferedReader(raw, buffer_size=1 << 20)
        raise DebError(_("Unsupported compression in member {name}.").format(name=compressor))

    def extract(
        self,
        root: Path,
        progress: Callable[[float], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ExtractResult:
        owners = self.owners()
        writer = SafeWriter(root)
        with open(self.path, "rb") as f:
            raw = _Slice(f, self.payload_offset, self.payload_size)
            with self._payload_stream(raw) as stream:
                for count, entry in enumerate(_read_cpio(stream, self)):
                    if cancelled and count % 64 == 0 and cancelled():
                        raise Cancelled()
                    if progress and count % 32 == 0:
                        progress(raw.consumed / max(self.payload_size, 1))
                    name, mode, mtime, data, link_to = entry
                    absolute = name[1:] if name.startswith("./") else "/" + name.lstrip("/")
                    owner = owners.get(absolute)
                    kind = mode & 0o170000
                    if link_to:
                        writer.hardlink(name, link_to, owner)
                    elif kind == 0o040000:
                        writer.directory(name, mode, mtime, owner)
                    elif kind == 0o100000:
                        writer.file(name, mode, mtime, data, owner)
                    elif kind == 0o120000:
                        writer.symlink(name, data.read().decode("utf-8", "replace"), owner)
                    else:
                        writer.skip(name)
        if progress:
            progress(1.0)
        return writer.finish()


class _Limited(io.RawIOBase):
    """Read exactly ``size`` bytes from a stream."""

    def __init__(self, stream, size: int):
        self._stream = stream
        self.remaining = size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self.remaining <= 0:
            return 0
        chunk = self._stream.read(min(len(buffer), self.remaining))
        n = len(chunk)
        buffer[:n] = chunk
        self.remaining -= n
        return n

    def drain(self) -> None:
        while self.remaining > 0 and self.read(1 << 20):
            pass


def _skip(stream, n: int) -> None:
    while n > 0:
        chunk = stream.read(min(n, 1 << 20))
        if not chunk:
            break
        n -= len(chunk)


class _StrippedFiles:
    """File metadata from the header, used by the "stripped" cpio format."""

    def __init__(self, package: "RpmPackage"):
        self.names = ["." + f for f in package.file_list()]
        attr = package._file_attr
        self.modes = attr(FILEMODES)
        self.sizes = attr(LONGFILESIZES) or attr(FILESIZES)
        self.mtimes = attr(FILEMTIMES)
        self.links = attr(FILELINKTOS)
        devices, inodes = attr(FILEDEVICES), attr(FILEINODES)
        # in a hardlink set only the last file (in header order) carries the data
        groups: dict[tuple[int, int], list[int]] = {}
        for i, mode in enumerate(self.modes):
            if mode & 0o170000 == 0o100000 and i < len(inodes):
                groups.setdefault((devices[i] if i < len(devices) else 0, inodes[i]), []).append(i)
        self.carrier: dict[int, int] = {}
        for members in groups.values():
            if len(members) > 1:
                for i in members:
                    self.carrier[i] = members[-1]

    def entry(self, index: int):
        mode = self.modes[index]
        return self.names[index], mode, self.mtimes[index] if index < len(self.mtimes) else 0


def _read_stripped(stream, files: _StrippedFiles, first_header: bytes):
    header = first_header
    pending: dict[int, list[int]] = {}
    while True:
        if header[:6] in (b"070701", b"070702"):
            return  # the archive ends with a classic "TRAILER!!!" entry
        if len(header) < 6 or header[:6] != b"07070X":
            raise DebError(_("The RPM package is truncated or corrupted."))
        rest = stream.read(8)
        index = int(rest, 16)
        _skip(stream, 2)  # 14-byte header padded to 4
        if index >= len(files.names):
            # trailer entry
            return
        name, mode, mtime = files.entry(index)
        kind = mode & 0o170000
        carrier = files.carrier.get(index)
        if kind == 0o100000 and carrier is not None and carrier != index:
            pending.setdefault(carrier, []).append(index)
        elif kind == 0o100000:
            size = files.sizes[index]
            data = _Limited(stream, size)
            yield name, mode, mtime, data, None
            data.drain()
            _skip(stream, (-size) % 4)
            for other in pending.pop(index, []):
                oname, omode, omtime = files.entry(other)
                yield oname, omode, omtime, None, name
        elif kind == 0o120000:
            # the link target is stored as the entry's content
            size = files.sizes[index]
            target = stream.read(size)
            _skip(stream, (-size) % 4)
            if not target and index < len(files.links):
                target = files.links[index].encode()
            yield name, mode, mtime, io.BytesIO(target), None
        else:
            yield name, mode, mtime, io.BytesIO(b""), None
        header = stream.read(6)
        if not header:
            return


def _read_cpio(stream, package: "RpmPackage | None" = None):
    """Yield (name, mode, mtime, data_stream, hardlink_target) from the payload."""
    first_name_by_ino: dict[tuple[int, int], str] = {}
    pending_links: dict[tuple[int, int], list[str]] = {}
    while True:
        header = stream.read(6)
        if header == b"07070X":
            if package is None:
                raise DebError(_("Unsupported RPM payload format."))
            yield from _read_stripped(stream, _StrippedFiles(package), header)
            return
        header += stream.read(104)
        if len(header) < 110:
            raise DebError(_("The RPM package is truncated or corrupted."))
        magic = header[:6]
        if magic not in (b"070701", b"070702"):
            raise DebError(_("Unsupported RPM payload format."))
        fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        ino, mode, _uid, _gid, nlink, mtime, filesize = fields[:7]
        devmajor, namesize = fields[7], fields[11]
        name = stream.read(namesize)[:-1].decode("utf-8", "replace")
        _skip(stream, (-(110 + namesize)) % 4)
        if name == "TRAILER!!!":
            return
        key = (devmajor, ino)
        data = _Limited(stream, filesize)
        is_reg = mode & 0o170000 == 0o100000
        if is_reg and nlink > 1:
            if filesize == 0:
                # data comes with the last link of this inode
                if key in first_name_by_ino:
                    yield name, mode, mtime, None, first_name_by_ino[key]
                else:
                    pending_links.setdefault(key, []).append((name, mode, mtime))
                _skip(stream, (-filesize) % 4)
                continue
            first_name_by_ino[key] = name
            yield name, mode, mtime, data, None
            data.drain()
            _skip(stream, (-filesize) % 4)
            for other, omode, omtime in pending_links.pop(key, []):
                yield other, omode, omtime, None, name
            continue
        yield name, mode, mtime, data, None
        data.drain()
        _skip(stream, (-filesize) % 4)


def is_rpm(path: str | os.PathLike) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == RPM_MAGIC
    except OSError:
        return False


def open_rpm(path: str | os.PathLike) -> RpmPackage:
    path = Path(path)
    if not path.is_file():
        raise DebError(_("File not found: {path}").format(path=path))
    with open(path, "rb") as f:
        lead = f.read(96)
        if len(lead) < 96 or lead[:4] != RPM_MAGIC:
            raise DebError(_("The file is not an RPM package."))
        if struct.unpack(">H", lead[6:8])[0] == 1:
            raise DebError(_("This is a source RPM (.src.rpm): it contains source code, not a program to install."))
        signature_start = f.tell()
        _read_header(f)
        # the signature header is padded to a multiple of 8 bytes
        f.seek((-(f.tell() - signature_start)) % 8, os.SEEK_CUR)
        tags = _read_header(f)
        payload_offset = f.tell()
        size = os.fstat(f.fileno()).st_size
    if SOURCERPM not in tags:
        raise DebError(_("This is a source RPM (.src.rpm): it contains source code, not a program to install."))
    payload_format = tags.get(PAYLOADFORMAT, "cpio")
    if isinstance(payload_format, list):
        payload_format = _first(payload_format, "cpio")
    if payload_format not in ("cpio", ""):
        raise DebError(_("Unsupported RPM payload format."))
    package = RpmPackage(path=path, tags=tags, payload_offset=payload_offset, payload_size=size - payload_offset)
    if not package.name or not package._tag(VERSION):
        raise DebError(_("The RPM package is truncated or corrupted."))
    return package
