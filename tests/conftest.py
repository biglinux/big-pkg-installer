import io
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "usr/share/debtap-mod"
sys.path.insert(0, str(APP_DIR))

from compression import zstd  # noqa: E402

CONTROL = """\
Package: hello-test
Version: 1:2.4~rc1-3ubuntu1
Architecture: {arch}
Maintainer: Test <test@example.com>
Installed-Size: 12
Depends: libc6 (>= 2.30), xdg-utils, libglib2.0-bin | kde-cli-tools, nonexistent-tool-xyz
Recommends: git
Homepage: https://example.com/hello
Description: Hello <test> package
 A longer description.
 .
 Second paragraph.
"""


def _tar_bytes(entries, compression: str) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry["name"])
            info.mtime = int(time.time())
            info.uname = entry.get("uname", "root")
            info.gname = entry.get("gname", "root")
            info.uid = entry.get("uid", 0)
            info.gid = entry.get("gid", 0)
            kind = entry.get("type", "file")
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = entry.get("mode", 0o755)
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry["target"]
                info.mode = 0o777
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = entry["target"]
                tar.addfile(info)
            else:
                data = entry.get("data", b"")
                if isinstance(data, str):
                    data = data.encode()
                info.size = len(data)
                info.mode = entry.get("mode", 0o644)
                tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    if compression == "gz":
        import gzip

        return gzip.compress(data)
    if compression == "xz":
        import lzma

        return lzma.compress(data)
    if compression == "bz2":
        import bz2

        return bz2.compress(data)
    if compression == "zst":
        return zstd.compress(data)
    return data


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        out.write(header)
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def build_deb(
    path: Path,
    data_entries,
    control: str | None = None,
    scripts: dict[str, str] | None = None,
    compression: str = "xz",
    control_compression: str | None = None,
    arch: str = "amd64",
    conffiles: list[str] | None = None,
) -> Path:
    control_entries = [{"name": "./control", "data": control or CONTROL.format(arch=arch)}]
    for name, body in (scripts or {}).items():
        control_entries.append({"name": f"./{name}", "data": body, "mode": 0o755})
    if conffiles:
        control_entries.append({"name": "./conffiles", "data": "\n".join(conffiles) + "\n"})
    ccomp = control_compression or compression
    csuffix = "" if ccomp == "none" else f".{ccomp}"
    dsuffix = "" if compression == "none" else f".{compression}"
    path.write_bytes(
        _ar(
            [
                ("debian-binary", b"2.0\n"),
                (f"control.tar{csuffix}", _tar_bytes(control_entries, ccomp)),
                (f"data.tar{dsuffix}", _tar_bytes(data_entries, compression)),
            ]
        )
    )
    return path


def default_entries():
    ls = Path("/usr/bin/ls").read_bytes()
    return [
        {"name": "./", "type": "dir"},
        {"name": "./usr/", "type": "dir"},
        {"name": "./usr/bin/", "type": "dir"},
        {"name": "./usr/bin/hello-test", "data": ls, "mode": 0o755},
        {"name": "./usr/share/applications/hello-test.desktop", "data": "[Desktop Entry]\nType=Application\nName=Hello Test\nExec=hello-test\nIcon=hello-test\n"},
        {"name": "./opt/hello/chrome-sandbox", "data": b"sandbox", "mode": 0o4755},
        {"name": "./etc/hello.conf", "data": "x=1\n"},
        {"name": "./usr/share/lintian/overrides/hello-test", "data": "\n"},
    ]


@pytest.fixture
def make_deb(tmp_path):
    def factory(name="hello.deb", entries=None, **kwargs):
        return build_deb(tmp_path / name, entries if entries is not None else default_entries(), **kwargs)

    return factory


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    yield


def has_makepkg() -> bool:
    return all(os.access(f"/usr/bin/{tool}", os.X_OK) for tool in ("makepkg", "fakeroot", "pacman"))
