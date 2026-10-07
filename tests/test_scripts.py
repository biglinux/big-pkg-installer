import subprocess

from debtap_mod.scripts import SCRIPTS_MARKER, build_install

POSTINST = """#!/bin/sh
set -e
. /usr/share/debconf/confmodule
db_get hello/question
if dpkg --compare-versions "$2" lt 2.0; then echo upgraded-from-old >> "$DEBTAP_TEST_LOG"; fi
update-alternatives --install /usr/bin/x-www-browser x /opt/x 50
deb-systemd-helper enable hello.service
echo "postinst $1 [$2] arch=$(dpkg --print-architecture)" >> "$DEBTAP_TEST_LOG"
"""

PRERM = """#!/bin/bash
echo "prerm $1" >> "$DEBTAP_TEST_LOG"
"""


def _run(install_text: str, function: str, *args: str, tmp_path) -> str:
    log = tmp_path / "log"
    log.write_text("")
    script = tmp_path / "pkg.install"
    script.write_text(install_text)
    cmd = f'. "{script}"; {function} ' + " ".join(f"'{a}'" for a in args)
    result = subprocess.run(
        ["bash", "-c", cmd], env={"PATH": "/usr/bin:/bin", "DEBTAP_TEST_LOG": str(log)}, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return log.read_text()


def test_no_scripts_no_install():
    assert build_install({}, "x", "amd64").text is None


def test_install_is_valid_bash(tmp_path):
    install = build_install({"postinst": POSTINST, "prerm": PRERM}, "hello", "amd64").text
    path = tmp_path / "x.install"
    path.write_text(install)
    subprocess.run(["bash", "-n", str(path)], check=True)
    assert SCRIPTS_MARKER in install
    assert "pre_install" not in install


def test_postinst_runs_with_debian_arguments(tmp_path):
    install = build_install({"postinst": POSTINST, "prerm": PRERM}, "hello", "amd64").text
    assert "postinst configure [] arch=" in _run(install, "post_install", "1.0-1", tmp_path=tmp_path)
    upgrade = _run(install, "post_upgrade", "2.1-1", "1.5-1", tmp_path=tmp_path)
    assert "upgraded-from-old" in upgrade
    assert "postinst configure [1.5-1]" in upgrade
    assert _run(install, "pre_remove", "1.0-1", tmp_path=tmp_path) == "prerm remove\n"


def test_failing_script_does_not_break_transaction(tmp_path):
    install = build_install({"postinst": "#!/bin/sh\nset -e\nfalse\n"}, "x", "amd64").text
    _run(install, "post_install", "1", tmp_path=tmp_path)


def test_heredoc_delimiter_cannot_collide(tmp_path):
    body = "#!/bin/sh\necho DEBTAP_EOF_ >> \"$DEBTAP_TEST_LOG\"\n"
    install = build_install({"postinst": body}, "x", "amd64").text
    assert _run(install, "post_install", "1", tmp_path=tmp_path) == "DEBTAP_EOF_\n"


def test_dash_shebang_and_apt_note():
    result = build_install({"postinst": "#!/bin/dash\necho > /etc/apt/sources.list.d/x.list\n"}, "x", "amd64")
    assert "#!/bin/sh" in result.text
    assert result.notes


def test_extra_post_install():
    result = build_install({}, "bricscad", "amd64", "chmod -R 777 /var/bricsys")
    assert "post_install() {\n    chmod -R 777 /var/bricsys" in result.text
