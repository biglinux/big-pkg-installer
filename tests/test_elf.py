import glob
import struct

import pytest

from big_pkg_installer.elf import read_elf


def test_reads_program():
    info = read_elf("/usr/bin/ls")
    assert info is not None
    assert info.is_program
    assert "libc.so.6" in info.needed


def test_reads_library_soname():
    libs = sorted(glob.glob("/usr/lib/libz.so.1*"))
    if not libs:
        pytest.skip("zlib not installed")
    info = read_elf(libs[0])
    assert info.soname == "libz.so.1"
    assert not info.is_program


def test_foreign_header_only(tmp_path):
    # 32-bit ARM ELF header without program headers
    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header += struct.pack("<HHIIIIIHHHHHH", 3, 40, 1, 0, 0, 0, 0, 52, 32, 0, 40, 0, 0)
    path = tmp_path / "arm.so"
    path.write_bytes(header)
    info = read_elf(str(path))
    assert info.machine == 40 and info.elfclass == 1


def test_not_elf(tmp_path):
    path = tmp_path / "text"
    path.write_text("#!/bin/sh\n")
    assert read_elf(str(path)) is None
