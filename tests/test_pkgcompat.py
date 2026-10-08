import re
import shutil

import pytest

from big_pkg_installer import pkgcompat
from big_pkg_installer.pkgcompat import Style, render_help, render_plan, translate

needs_pacman = pytest.mark.skipif(not shutil.which("pacman"), reason="pacman not available")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def user_with_pamac(monkeypatch):
    monkeypatch.setattr(pkgcompat, "_is_root", lambda: False)
    monkeypatch.setattr(pkgcompat, "_have", lambda cmd: True)
    monkeypatch.setattr(pkgcompat, "_installed", lambda name: True)
    monkeypatch.setattr(pkgcompat, "_orphans", lambda: ["old-lib"])


@pytest.mark.parametrize(
    "tool, args, pacman, run",
    [
        ("apt", ["update"], "checkupdates", ["pamac", "checkupdates"]),
        ("apt", ["upgrade"], "sudo pacman -Syu", ["pamac", "upgrade"]),
        ("apt", ["full-upgrade", "-y"], "sudo pacman -Syu", ["pamac", "upgrade", "--no-confirm"]),
        ("apt-get", ["dist-upgrade"], "sudo pacman -Syu", ["pamac", "upgrade"]),
        ("apt", ["install", "vlc", "gimp"], "sudo pacman -S vlc gimp", ["pamac", "install", "vlc", "gimp"]),
        ("apt", ["install", "--reinstall", "vlc"], "sudo pacman -S vlc", ["pamac", "reinstall", "vlc"]),
        ("apt", ["remove", "vlc"], "sudo pacman -R vlc", ["pamac", "remove", "vlc"]),
        ("apt", ["remove", "--purge", "vlc"], "sudo pacman -Rns vlc", ["sudo", "pacman", "-Rns", "vlc"]),
        ("apt", ["purge", "vlc"], "sudo pacman -Rns vlc", ["sudo", "pacman", "-Rns", "vlc"]),
        ("apt", ["autoremove", "vlc"], "sudo pacman -Rs vlc", ["pamac", "remove", "vlc", "--orphans"]),
        ("apt", ["autoremove"], "sudo pacman -Rns $(pacman -Qdtq)", ["sudo", "pacman", "-Rns", "old-lib"]),
        ("apt", ["search", "vlc"], "pacman -Ss vlc", ["pamac", "search", "vlc"]),
        ("apt", ["show", "bash"], "pacman -Qi bash", ["pamac", "info", "bash"]),
        ("apt", ["list"], "pacman -Sl", ["pacman", "-Sl"]),
        ("apt", ["list", "--installed"], "pacman -Q", ["pacman", "-Q"]),
        ("apt", ["list", "--installed", "vlc"], "pacman -Qs vlc", ["pacman", "-Qs", "vlc"]),
        ("apt", ["list", "--upgradable"], "pacman -Qu", ["pamac", "checkupdates"]),
        ("apt", ["clean"], "sudo pacman -Sc", ["pamac", "clean"]),
        ("apt-cache", ["rdepends", "glib2"], "pactree -r glib2", ["pactree", "-r", "glib2"]),
        ("apt-mark", ["showmanual"], "pacman -Qe", ["pacman", "-Qe"]),
        ("dpkg", ["-L", "bash"], "pacman -Ql bash", ["pacman", "-Ql", "bash"]),
        ("dpkg", ["-l"], "pacman -Q", ["pacman", "-Q"]),
        ("dpkg", ["-s", "bash"], "pacman -Qi bash", ["pamac", "info", "bash"]),
        ("dpkg", ["-r", "vlc"], "sudo pacman -R vlc", ["pamac", "remove", "vlc"]),
        ("dnf", ["check-update"], "checkupdates", ["pamac", "checkupdates"]),
        ("dnf", ["upgrade"], "sudo pacman -Syu", ["pamac", "upgrade"]),
        ("dnf", ["install", "-y", "vlc"], "sudo pacman -S vlc", ["pamac", "install", "vlc", "--no-confirm"]),
        ("dnf", ["erase", "vlc"], "sudo pacman -R vlc", ["pamac", "remove", "vlc"]),
        ("dnf", ["list", "installed"], "pacman -Q", ["pacman", "-Q"]),
        ("dnf", ["repoquery", "-l", "bash"], "pacman -Ql bash", ["pacman", "-Ql", "bash"]),
        ("dnf", ["history", "userinstalled"], "pacman -Qe", ["pacman", "-Qe"]),
        ("dnf", ["group", "list"], "pacman -Sg", ["pacman", "-Sg"]),
        ("yum", ["update"], "sudo pacman -Syu", ["pamac", "upgrade"]),
    ],
)
def test_translations(user_with_pamac, tool, args, pacman, run):
    plan = translate(tool, args)
    assert plan.pacman == pacman
    assert plan.run == run


def test_root_uses_pacman_directly(monkeypatch):
    monkeypatch.setattr(pkgcompat, "_is_root", lambda: True)
    monkeypatch.setattr(pkgcompat, "_have", lambda cmd: True)
    plan = translate("apt", ["install", "-y", "vlc"])
    assert plan.run == ["pacman", "-S", "--needed", "vlc", "--noconfirm"]


def test_without_pamac_uses_sudo_pacman(monkeypatch):
    monkeypatch.setattr(pkgcompat, "_is_root", lambda: False)
    monkeypatch.setattr(pkgcompat, "_have", lambda cmd: cmd != "pamac")
    assert translate("apt", ["install", "vlc"]).run == ["sudo", "pacman", "-S", "--needed", "vlc"]


def test_deb_files_go_to_installer(user_with_pamac, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_bytes(b"x")
    for tool, args in (("apt", ["install", str(deb)]), ("dpkg", ["-i", str(deb)])):
        plan = translate(tool, args)
        assert plan.action == "install-deb"
        assert plan.run == ["big-pkg-installer", str(deb)]


def test_deb_as_root_builds_as_sudo_user(monkeypatch, tmp_path):
    monkeypatch.setattr(pkgcompat, "_is_root", lambda: True)
    monkeypatch.setenv("SUDO_USER", "alice")
    deb = tmp_path / "app.deb"
    deb.write_bytes(b"x")
    assert translate("apt", ["install", str(deb)]).run == ["sudo", "-u", "alice", "big-pkg-installer", str(deb)]


def test_missing_deb_is_an_error(user_with_pamac):
    plan = translate("apt", ["install", "./missing.deb"])
    assert plan.action == "usage" and plan.run is None


def test_rpm_files_go_to_installer(user_with_pamac, tmp_path):
    rpm = tmp_path / "app.rpm"
    rpm.write_bytes(b"x")
    for tool, args in (("dnf", ["install", str(rpm)]), ("yum", ["localinstall", str(rpm)]), ("apt", ["install", str(rpm)])):
        plan = translate(tool, args)
        assert plan.action == "install-deb" and plan.run == ["big-pkg-installer", str(rpm)]
    assert translate("dnf", ["install", "missing.rpm"]).action == "usage"


def test_unknown_and_empty(user_with_pamac):
    assert translate("apt", ["moo"]).action == "unsupported"
    assert translate("apt", ["install"]).action == "usage"
    assert translate("apt", []).show_help
    assert translate("dnf", ["--help"]).show_help
    assert translate("apt", ["help", "install"]).help_topic == "install"


def test_ppa_explains_aur(user_with_pamac):
    plan = translate("dnf", ["copr", "enable", "x/y"])
    assert plan.action == "repositories" and plan.run is None


@pytest.mark.parametrize("columns", [160, 110, 60])
def test_help_layouts(columns):
    text = render_help("apt", "", Style(False), columns=columns)
    assert "sudo pacman -Syu" in text
    assert "big-pkg-installer app.deb" in text
    assert max(len(line) for line in text.splitlines()) <= max(columns, 120)
    if columns >= 160:
        assert "pamac upgrade" in text  # full table includes the pamac column


def test_help_topic_filters():
    text = render_help("apt", "purge", Style(False), columns=160)
    assert "-Rns vlc" in text
    assert "pacman -Ss" not in text


def test_help_unknown_topic():
    assert "xyz" in render_help("apt", "xyz", Style(False), columns=160)


def test_dnf_help_shows_dnf_column():
    text = render_help("dnf", "", Style(False), columns=160)
    assert "dnf check-update" in text and "apt list --installed" not in text


def test_colors_only_when_enabled(user_with_pamac):
    plan = translate("apt", ["install", "vlc"])
    assert not ANSI.search(render_plan(plan, Style(False)))
    assert ANSI.search(render_plan(plan, Style(True)))


def test_no_color_env(monkeypatch):
    import io

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert pkgcompat._color_enabled(Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not pkgcompat._color_enabled(Tty())


@needs_pacman
@pytest.mark.parametrize(
    "args, code",
    [
        (["--compare-versions", "1.0", "lt", "2.0"], 0),
        (["--compare-versions", "2.0", "lt", "1.0"], 1),
        (["--compare-versions", "1:1.0", "gt", "2.0"], 0),
        (["--compare-versions", "1.0~rc1", "lt", "1.0"], 0),
        (["--compare-versions", "1.10", "gt", "1.9"], 0),
        (["--compare-versions", "1.0"], 2),
    ],
)
def test_dpkg_compare_versions(args, code):
    assert pkgcompat.dpkg_silent(args) == code


def test_dpkg_print_architecture(capsys):
    assert pkgcompat.dpkg_silent(["--print-architecture"]) == 0
    assert capsys.readouterr().out.strip() in ("amd64", "arm64", "armhf", "i386", "riscv64")


def test_main_dry_run(monkeypatch, capsys, user_with_pamac):
    monkeypatch.setenv("BIG_PKG_COMPAT_DRY_RUN", "1")
    monkeypatch.setattr(pkgcompat, "_real_binary", lambda tool: None)
    assert pkgcompat.main(["/usr/local/bin/apt", "install", "vlc"]) == 0
    err = capsys.readouterr().err
    assert "sudo pacman -S vlc" in err and "pamac install vlc" in err


def test_main_module_invocation(monkeypatch, capsys):
    monkeypatch.setattr(pkgcompat, "_real_binary", lambda tool: None)
    assert pkgcompat.main(["pkgcompat.py", "dnf", "help"]) == 0
    assert "dnf × pacman" in capsys.readouterr().out


def test_real_binary_takes_precedence(monkeypatch):
    calls = []
    monkeypatch.setattr(pkgcompat, "_real_binary", lambda tool: "/usr/bin/apt")
    monkeypatch.setattr(pkgcompat.os, "execv", lambda path, argv: calls.append(argv))
    monkeypatch.setattr(pkgcompat, "translate", lambda *a: (_ for _ in ()).throw(AssertionError("translated")))
    monkeypatch.delenv("BIG_PKG_COMPAT_FORCE", raising=False)
    try:
        pkgcompat.main(["/usr/local/bin/apt", "update"])
    except AssertionError:
        pass
    assert calls == [["/usr/bin/apt", "update"]]
