import os
import stat

import pytest

from big_pkg_installer.debfile import DebError, extract_data, open_deb, parse_control


def test_parse_control_multiline():
    fields = parse_control("Package: a\nDescription: short\n line one\n .\n line two\nVersion: 1\n")
    assert fields["package"] == "a"
    assert fields["description"] == "short\nline one\n\nline two"
    assert fields["version"] == "1"


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst", "none"])
def test_open_and_extract_every_compression(make_deb, tmp_path, compression):
    deb = open_deb(make_deb(compression=compression))
    assert deb.name == "hello-test"
    assert deb.summary == "Hello <test> package"
    assert deb.installed_size == 12 * 1024
    root = tmp_path / "root"
    extract_data(deb, root)
    assert (root / "usr/bin/hello-test").is_file()


def test_uncompressed_control_tar(make_deb):
    # dpkg accepts a plain control.tar; the bash version silently failed on it
    deb = open_deb(make_deb(control_compression="none"))
    assert deb.version == "1:2.4~rc1-3ubuntu1"


def test_setuid_and_owners_are_kept(make_deb, tmp_path):
    entries = [
        {"name": "./opt/app/chrome-sandbox", "data": b"x", "mode": 0o4755},
        {"name": "./var/lib/app/", "type": "dir", "uname": "daemon", "gname": "daemon", "uid": 2, "gid": 2},
    ]
    deb = open_deb(make_deb(entries=entries))
    root = tmp_path / "root"
    result = extract_data(deb, root)
    mode = os.stat(root / "opt/app/chrome-sandbox").st_mode
    assert mode & stat.S_ISUID
    assert result.owners == {"var/lib/app": ("daemon", "daemon")}


def test_path_traversal_is_refused(make_deb, tmp_path):
    entries = [
        {"name": "../evil", "data": b"x"},
        {"name": "./link", "type": "symlink", "target": "/tmp"},
        {"name": "./link/escaped", "data": b"x"},
        {"name": "./ok", "data": b"fine"},
    ]
    deb = open_deb(make_deb(entries=entries))
    root = tmp_path / "root"
    result = extract_data(deb, root)
    assert not (tmp_path / "evil").exists()
    assert not os.path.exists("/tmp/escaped")
    assert (root / "ok").read_bytes() == b"fine"
    assert "./link/escaped" in result.skipped


def test_hardlinks(make_deb, tmp_path):
    entries = [{"name": "./a", "data": b"same"}, {"name": "./b", "type": "hardlink", "target": "./a"}]
    root = tmp_path / "root"
    extract_data(open_deb(make_deb(entries=entries)), root)
    assert os.stat(root / "a").st_ino == os.stat(root / "b").st_ino


def test_conffiles(make_deb):
    deb = open_deb(make_deb(conffiles=["/etc/hello.conf", "remove-on-upgrade /etc/old.conf"]))
    assert deb.conffiles == ["/etc/hello.conf"]


def test_not_a_deb(tmp_path):
    bad = tmp_path / "bad.deb"
    bad.write_text("hello")
    with pytest.raises(DebError):
        open_deb(bad)


def test_truncated_deb(make_deb, tmp_path):
    good = make_deb().read_bytes()
    bad = tmp_path / "truncated.deb"
    bad.write_bytes(good[:70])
    with pytest.raises(DebError):
        open_deb(bad)
