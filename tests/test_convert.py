"""End to end: .deb -> makepkg -> .pkg.tar.zst (nothing is installed)."""

import subprocess

import pytest

from conftest import has_makepkg
from debtap_mod import cli
from debtap_mod.converter import Callbacks, ConversionError, convert, inspect
from debtap_mod.debfile import Cancelled

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not has_makepkg(), reason="makepkg not available")]

SCRIPTS = {"postinst": "#!/bin/sh\nset -e\necho configured\n", "postrm": "#!/bin/sh\necho removed\n"}


def _listing(pkgfile):
    out = subprocess.run(["bsdtar", "-tvf", str(pkgfile)], capture_output=True, text=True, check=True).stdout
    return {line.split()[-1] if "->" not in line else line.split()[-3]: line for line in out.splitlines()}


def _pkginfo(pkgfile) -> str:
    return subprocess.run(["bsdtar", "-xOf", str(pkgfile), ".PKGINFO"], capture_output=True, text=True, check=True).stdout


def test_full_conversion(make_deb, tmp_path):
    deb = make_deb(
        scripts=SCRIPTS,
        conffiles=["/etc/hello.conf"],
        entries=[
            *__import__("conftest").default_entries(),
            {"name": "./sbin/hello-daemon", "data": b"#!/bin/sh\n", "mode": 0o755},
        ],
    )
    steps = []
    conversion = inspect(deb)
    assert conversion.pkgname == "hello-test-deb"
    try:
        convert(conversion, Callbacks(step=steps.append))
        assert steps == ["extract", "layout", "deps", "scripts", "build"]
        assert conversion.pkgfile.name == "hello-test-deb-1:2.4rc1-3-x86_64.pkg.tar.zst"
        files = _listing(conversion.pkgfile)
        # ownership is root and setuid survives
        assert " root " in files["opt/hello/chrome-sandbox"] or "root/root" in files["opt/hello/chrome-sandbox"]
        assert files["opt/hello/chrome-sandbox"].startswith("-rwsr-xr-x")
        assert "usr/bin/hello-daemon" in files
        assert "sbin/hello-daemon" not in files
        assert not any(name.startswith("usr/share/lintian") for name in files)
        assert ".INSTALL" in files
        info = _pkginfo(conversion.pkgfile)
        assert "depend = glibc" in info
        assert "depend = xdg-utils" in info
        assert "backup = etc/hello.conf" in info
        assert "conflict = hello-test" in info
        assert "provides = hello-test=2.4rc1" in info
        assert "optdepend = git: recommended by the Debian package" in info
        assert [a.name for a in conversion.apps] == ["Hello Test"]
        install = subprocess.run(["bsdtar", "-xOf", str(conversion.pkgfile), ".INSTALL"], capture_output=True, text=True).stdout
        subprocess.run(["bash", "-n"], input=install, text=True, check=True)
        assert "echo configured" in install
    finally:
        workdir = conversion.workdir
        conversion.cleanup()
    assert workdir is None or not workdir.exists()


def test_cancel_cleans_up(make_deb):
    conversion = inspect(make_deb())
    callbacks = Callbacks()
    callbacks.cancel.set()
    with pytest.raises(Cancelled):
        convert(conversion, callbacks)
    assert conversion.workdir is None


def test_wrong_architecture_is_refused(make_deb):
    conversion = inspect(make_deb(arch="arm64"))
    assert conversion.warnings
    with pytest.raises(ConversionError):
        convert(conversion)


def test_cli_build_only(make_deb, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.main(["-n", "-o", str(out), str(make_deb())]) == 0
    assert list(out.glob("hello-test-deb-*.pkg.tar.zst"))
    assert "Hello Test" in capsys.readouterr().out


def test_cli_pkgbuild_only(make_deb, tmp_path):
    deb = make_deb()
    assert cli.main(["-P", str(deb)]) == 0
    pkgbuild = deb.parent / "hello-test-deb" / "PKGBUILD"
    text = pkgbuild.read_text()
    assert "source=(hello.deb)" in text
    subprocess.run(["bash", "-n", str(pkgbuild)], check=True)
    # the portable PKGBUILD really builds
    env_dir = tmp_path / "build"
    result = subprocess.run(
        ["makepkg", "-f", "-d", "--noconfirm", "--nocolor"],
        cwd=pkgbuild.parent,
        env={"PATH": "/usr/bin", "HOME": str(tmp_path), "BUILDDIR": str(env_dir), "PKGDEST": str(env_dir), "PKGEXT": ".pkg.tar"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    files = _listing(next(env_dir.glob("*.pkg.tar")))
    assert "usr/bin/hello-test" in files


def test_cli_rejects_garbage(tmp_path, capsys):
    bad = tmp_path / "x.deb"
    bad.write_text("nope")
    assert cli.main([str(bad)]) == 1
    assert cli.main([]) == 2
