import json
import subprocess

import pytest

from conftest import has_makepkg
from debtap_mod import installer
from debtap_mod.installer import InstallProgress, preview_transaction

PACMAN_LOG = """loading packages...
resolving dependencies...
looking for conflicting packages...
:: Retrieving packages...
 downloading gnome-chess-50.1-1-x86_64.pkg.tar.zst...
 downloading libfoo-1.0-1-x86_64.pkg.tar.zst...
checking keyring...
checking package integrity...
:: Processing package changes...
installing gnome-chess...
installing example-app-deb...
:: Running post-transaction hooks...
(1/2) Arming ConditionNeedsUpdate...
(2/2) Updating the desktop file MIME type cache..."""


def test_progress_follows_pacman_output():
    progress = InstallProgress(downloads=2)
    seen = []
    for line in PACMAN_LOG.splitlines():
        if progress.feed(line):
            seen.append((progress.step, progress.downloaded))
    assert seen == [("download", 0), ("download", 1), ("download", 2), ("install", 2), ("configure", 2)]
    assert progress.fraction == 0.85


def test_progress_without_downloads():
    progress = InstallProgress(downloads=0)
    for line in ("loading packages...", ":: Processing package changes...", "installing x..."):
        progress.feed(line)
    assert progress.step == "install"


def test_install_command_flags(monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    cmd = installer.install_command(installer.Path("/x.pkg.tar.zst"), ["/usr/bin/a*b"], interactive_tty=False)
    assert cmd[:2] == ["pkexec", "pacman"]
    assert "--noprogressbar" in cmd and "--ask=4" in cmd
    assert cmd[cmd.index("--overwrite") + 1] == "/usr/bin/a\\*b"


@pytest.mark.slow
@pytest.mark.skipif(not has_makepkg(), reason="makepkg not available")
def test_preview_lists_missing_dependencies(tmp_path):
    (tmp_path / "PKGBUILD").write_text(
        "pkgname=dtp-preview\npkgver=1\npkgrel=1\narch=(any)\ndepends=(glibc gnome-chess)\n"
        'package() { mkdir -p "$pkgdir/usr/share/dtp-preview"; }\n'
    )
    env = {"PATH": "/usr/bin", "HOME": str(tmp_path), "PKGDEST": str(tmp_path), "BUILDDIR": str(tmp_path / "b"),
           "PKGEXT": ".pkg.tar"}
    subprocess.run(["makepkg", "-f", "-d", "--noconfirm"], cwd=tmp_path, env=env, capture_output=True, check=True)
    preview = preview_transaction(next(tmp_path.glob("*.pkg.tar")))
    names = [p.name for p in preview.packages]
    assert not preview.error
    assert names[0] == "dtp-preview" and preview.packages[0].local
    assert "glibc" not in names  # already installed
    if subprocess.run(["pacman", "-Qq", "gnome-chess"], capture_output=True).returncode != 0:
        assert "gnome-chess" in names
    assert preview.total_size > 0


@pytest.mark.slow
@pytest.mark.skipif(not has_makepkg(), reason="makepkg not available")
def test_preview_reports_unknown_dependency(tmp_path):
    (tmp_path / "PKGBUILD").write_text(
        "pkgname=dtp-broken\npkgver=1\npkgrel=1\narch=(any)\ndepends=(definitely-not-a-package-xyz)\n"
        'package() { mkdir -p "$pkgdir/usr/share/x"; }\n'
    )
    env = {"PATH": "/usr/bin", "HOME": str(tmp_path), "PKGDEST": str(tmp_path), "BUILDDIR": str(tmp_path / "b"),
           "PKGEXT": ".pkg.tar"}
    subprocess.run(["makepkg", "-f", "-d", "--noconfirm"], cwd=tmp_path, env=env, capture_output=True, check=True)
    preview = preview_transaction(next(tmp_path.glob("*.pkg.tar")))
    assert "definitely-not-a-package-xyz" in preview.error


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from debtap_mod.settings import Settings

    assert Settings.load().save_copy is False
    Settings(save_copy=True, save_dir="/srv/pkgs").save()
    loaded = Settings.load()
    assert loaded.save_copy and str(loaded.save_path) == "/srv/pkgs"
    (tmp_path / "debtap-mod" / "settings.json").write_text("{broken")
    assert Settings.load() == Settings()
    assert json.loads(json.dumps({"ok": 1}))
