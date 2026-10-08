import shutil

import pytest

from big_pkg_installer import deps, pacman
from big_pkg_installer.deps import compute_dependencies, map_debian_name, parse_relationship

needs_pacman = pytest.mark.skipif(not shutil.which("pacman"), reason="pacman not available")


def test_parse_relationship():
    groups = parse_relationship("libc6 (>= 2.30), a | b:any (<< 3), c [amd64], python3:any")
    assert groups == [["libc6"], ["a", "b:any"], ["c"], ["python3:any"]]


def test_map_debian_name():
    sync = frozenset({"git", "libfoo"})
    assert map_debian_name("xz-utils", sync) == "xz"
    assert map_debian_name("libc6", sync) == ""
    assert map_debian_name("git", sync) == "git"
    assert map_debian_name("libfoo", sync) is None  # libraries come from the ELF scan
    assert map_debian_name("unknown-thing", sync) is None


@needs_pacman
def test_dependencies_from_elf_and_control(tmp_path):
    (tmp_path / "usr/bin").mkdir(parents=True)
    shutil.copy("/usr/bin/ls", tmp_path / "usr/bin/tool")
    control = {
        "depends": "libc6, xdg-utils, libglib2.0-bin | kde-cli-tools, nonexistent-tool-xyz",
        "recommends": "git",
    }
    report = compute_dependencies(tmp_path, control, "x86_64", {"tool-deb"})
    assert "glibc" in report.depends
    assert "xdg-utils" in report.depends
    assert "glib2" in report.depends
    assert "git" in report.optdepends
    assert report.unresolved_declared == ["nonexistent-tool-xyz"]


@needs_pacman
def test_foreign_binaries_are_ignored(tmp_path, monkeypatch):
    # an ARM build of a native module must not pull lib32-* packages
    import struct

    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header += struct.pack("<HHIIIIIHHHHHH", 3, 40, 1, 0, 0, 0, 0, 52, 32, 0, 40, 0, 0)
    (tmp_path / "prebuilds").mkdir()
    (tmp_path / "prebuilds/node.armv7.node").write_bytes(header)
    report = compute_dependencies(tmp_path, {}, "x86_64", set())
    assert report.foreign_binaries == 1
    assert not any(d.startswith("lib32-") for d in report.depends)


@needs_pacman
def test_plugins_become_optional(tmp_path, monkeypatch):
    # a library nobody links against (dlopen plugin) gets optional deps only
    from big_pkg_installer.elf import ElfInfo

    fake = [
        deps.ScannedElf("usr/bin/app", ElfInfo(62, 2, 3, interp="/lib64/ld-linux-x86-64.so.2", needed=["libc.so.6"])),
        deps.ScannedElf("usr/lib/app/libqt_shim.so", ElfInfo(62, 2, 3, needed=["libz.so.1"], soname="libqt_shim.so")),
    ]
    monkeypatch.setattr(deps, "scan_elves", lambda root: fake)
    report = compute_dependencies(tmp_path, {}, "x86_64", set())
    assert "glibc" in report.depends
    assert "zlib" not in report.depends
    assert "zlib" in report.optdepends


@needs_pacman
def test_files_db_used_for_missing_libraries():
    resolved, unresolved = deps.resolve_sonames({"libc.so.6", "libdefinitely-not-real.so.9"}, 62, 2)
    assert resolved["libc.so.6"] == "glibc"
    assert unresolved == {"libdefinitely-not-real.so.9"}


def test_virtual_packages_allowed():
    assert deps._package_usable("java-runtime", frozenset(), frozenset())


@needs_pacman
def test_repo_order_is_known():
    assert "core" in pacman.repo_order()
