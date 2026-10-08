import os
import stat
import subprocess

import pytest

from big_pkg_installer.converter import Callbacks, convert, inspect
from big_pkg_installer.debfile import DebError
from big_pkg_installer.deps import map_rpm_name, parse_rpm_requirement
from big_pkg_installer.package import open_package
from big_pkg_installer.rpmfile import open_rpm
from big_pkg_installer.scripts import build_install_rpm
from conftest import has_makepkg, has_rpmbuild

needs_rpmbuild = pytest.mark.skipif(not has_rpmbuild(), reason="rpmbuild not available")


def test_parse_rich_dependencies():
    assert parse_rpm_requirement("gtk3") == [["gtk3"]]
    assert parse_rpm_requirement("(git or mercurial)") == [["git", "mercurial"]]
    assert parse_rpm_requirement("(a and (b or c))") == [["a"], ["b", "c"]]
    assert parse_rpm_requirement("(foo if bar)") == [["foo"]]


def test_map_rpm_names():
    sync = frozenset({"libx11", "python-gobject", "gtk3", "perl-json"})
    assert map_rpm_name("libX11", sync) == "libx11"
    assert map_rpm_name("python3-gobject", sync) == "python-gobject"
    assert map_rpm_name("/bin/sh", sync) == "bash"
    assert map_rpm_name("redhat-lsb-core", sync) == ""
    assert map_rpm_name("totally-unknown", sync) is None


@needs_rpmbuild
def test_metadata(make_rpm):
    rpm = open_package(make_rpm())
    assert rpm.kind == "rpm"
    assert rpm.name == "hello-rpm"
    assert rpm.version == "1:2.4~rc1-3.fc42"
    assert str(rpm.pacman_version()) == "1:2.4rc1-3"
    assert rpm.pacman_arch == "x86_64"
    assert rpm.summary == "Hello RPM test package"
    assert rpm.homepage == "https://example.com/hello-rpm"
    assert rpm.conffiles == ["/etc/hello/hello.conf"]
    assert "gtk3" in rpm.requires() and not any(r.startswith("rpmlib(") for r in rpm.requires())
    assert set(rpm.scripts()) == {"pre", "post", "preun", "postun", "posttrans"}


@needs_rpmbuild
@pytest.mark.parametrize("payload", ["w19.zstdio", "w9.gzdio", "w6.xzdio", "w9.bzdio"])
def test_extract_every_compression(make_rpm, tmp_path, payload):
    root = tmp_path / f"root-{payload}"
    result = open_rpm(make_rpm(payload=payload)).extract(root)
    assert (root / "usr/bin/hello-rpm").is_file()
    assert os.stat(root / "opt/hello/chrome-sandbox").st_mode & stat.S_ISUID
    assert os.readlink(root / "opt/hello/run") == "../../usr/bin/hello-rpm"
    data, link = root / "usr/lib64/hello/data.txt", root / "usr/lib64/hello/data-link.txt"
    assert data.read_text() == "data\n" and os.stat(data).st_ino == os.stat(link).st_ino
    assert (root / "etc/hello/hello.conf").read_text() == "x=1\n"
    assert result.owners.get("var/lib/hello") == ("nobody", "nobody")
    assert not result.skipped


@needs_rpmbuild
def test_noarch_is_any(make_rpm):
    assert open_package(make_rpm(name="hello-noarch", arch="noarch")).pacman_arch == "any"


@needs_rpmbuild
def test_source_rpm_is_refused(make_rpm):
    with pytest.raises(DebError, match="source"):
        open_package(make_rpm(source=True))


@needs_rpmbuild
def test_truncated_rpm(make_rpm, tmp_path):
    good = make_rpm().read_bytes()
    bad = tmp_path / "broken.rpm"
    bad.write_bytes(good[:300])
    with pytest.raises(DebError):
        open_package(bad)


def test_rpm_scriptlets_run_with_rpm_arguments(tmp_path):
    from big_pkg_installer.rpmfile import RpmScript

    scripts = {
        "pre": RpmScript('echo "pre $1" >> "$BIG_PKG_TEST_LOG"', ["/bin/sh"]),
        "post": RpmScript('echo "post $1" >> "$BIG_PKG_TEST_LOG"', ["/bin/sh"]),
        "posttrans": RpmScript('echo posttrans >> "$BIG_PKG_TEST_LOG"', ["/bin/sh"]),
        "postun": RpmScript('echo "postun $1" >> "$BIG_PKG_TEST_LOG"', ["/bin/bash"]),
        "preun": RpmScript("", ["/usr/bin/true"]),
        "pretrans": RpmScript("print('hi')", ["<lua>"]),
    }
    result = build_install_rpm(scripts, "hello")
    assert result.notes and "pretrans" not in result.scripts
    install = tmp_path / "x.install"
    install.write_text(result.text)
    subprocess.run(["bash", "-n", str(install)], check=True)
    log = tmp_path / "log"

    def run(func, *args):
        log.write_text("")
        subprocess.run(["bash", "-c", f'. "{install}"; {func} ' + " ".join(args)], check=True,
                       env={"PATH": "/usr/bin:/bin", "BIG_PKG_TEST_LOG": str(log)})
        return log.read_text()

    assert run("post_install", "1.0-1") == "post 1\nposttrans\n"
    assert run("pre_upgrade", "2.0-1", "1.0-1") == "pre 2\n"
    assert run("post_upgrade", "2.0-1", "1.0-1") == "post 2\nposttrans\n"
    assert run("post_remove", "1.0-1") == "postun 0\n"
    assert run("pre_remove", "1.0-1") == ""


@pytest.mark.slow
@needs_rpmbuild
@pytest.mark.skipif(not has_makepkg(), reason="makepkg not available")
def test_full_rpm_conversion(make_rpm):
    conversion = inspect(make_rpm())
    assert conversion.pkgname == "hello-rpm-rpm"
    try:
        convert(conversion, Callbacks())
        pkg = conversion.pkgfile
        assert pkg.name == "hello-rpm-rpm-1:2.4rc1-3-x86_64.pkg.tar.zst"
        listing = subprocess.run(["bsdtar", "-tvf", str(pkg)], capture_output=True, text=True, check=True).stdout
        assert "usr/lib/hello/data.txt" in listing and "usr/lib64" not in listing
        assert ".build-id" not in listing
        sandbox = next(line for line in listing.splitlines() if line.endswith("opt/hello/chrome-sandbox"))
        assert sandbox.startswith("-rwsr-xr-x")
        info = subprocess.run(["bsdtar", "-xOf", str(pkg), ".PKGINFO"], capture_output=True, text=True).stdout
        for line in ("depend = gtk3", "depend = libx11", "depend = xdg-utils", "depend = git", "depend = bash",
                     "backup = etc/hello/hello.conf", "conflict = hello-rpm",
                     "optdepend = lsb-release: recommended by the RPM package"):
            assert line in info, line
        assert any("nonexistent-tool-xyz" in w for w in conversion.warnings)
        install = subprocess.run(["bsdtar", "-xOf", str(pkg), ".INSTALL"], capture_output=True, text=True).stdout
        subprocess.run(["bash", "-n"], input=install, text=True, check=True)
        assert "_bigpkg_run post 1" in install and "_bigpkg_run postun 0" in install
        assert [a.name for a in conversion.apps] == ["Hello RPM"]
    finally:
        conversion.cleanup()
