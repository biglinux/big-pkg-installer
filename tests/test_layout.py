import os

from debtap_mod.layout import normalize_layout


def make(root, rel, data="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def test_bin_sbin_lib_are_merged(tmp_path):
    make(tmp_path, "bin/a")
    make(tmp_path, "sbin/b")
    make(tmp_path, "usr/sbin/c")
    make(tmp_path, "lib/systemd/system/x.service")
    make(tmp_path, "usr/lib64/libq.so")
    report, mapping = normalize_layout(tmp_path, "x86_64")
    for rel in ("usr/bin/a", "usr/bin/b", "usr/bin/c", "usr/lib/systemd/system/x.service", "usr/lib/libq.so"):
        assert (tmp_path / rel).is_file(), rel
    for rel in ("bin", "sbin", "usr/sbin", "lib", "usr/lib64"):
        assert not os.path.lexists(tmp_path / rel), rel
    assert mapping["sbin/b"] == "usr/bin/b"


def test_relative_symlinks_are_retargeted(tmp_path):
    make(tmp_path, "usr/lib/app/run")
    (tmp_path / "bin").mkdir()
    os.symlink("../usr/lib/app/run", tmp_path / "bin/app")
    normalize_layout(tmp_path, "x86_64")
    link = tmp_path / "usr/bin/app"
    assert os.readlink(link) == "../lib/app/run"
    assert link.resolve() == (tmp_path / "usr/lib/app/run").resolve()


def test_real_file_wins_over_symlink(tmp_path):
    make(tmp_path, "bin/tool", "real")
    (tmp_path / "usr/bin").mkdir(parents=True)
    os.symlink("/bin/tool", tmp_path / "usr/bin/tool")
    normalize_layout(tmp_path, "x86_64")
    assert not os.path.islink(tmp_path / "usr/bin/tool")
    assert (tmp_path / "usr/bin/tool").read_text() == "real"


def test_multiarch_dir_merged_with_compat_links(tmp_path):
    make(tmp_path, "usr/lib/x86_64-linux-gnu/libfoo.so.1")
    make(tmp_path, "usr/lib/x86_64-linux-gnu/qt5/plugins/p.so")
    report, _ = normalize_layout(tmp_path, "x86_64")
    assert (tmp_path / "usr/lib/libfoo.so.1").is_file()
    assert (tmp_path / "usr/lib/qt5/plugins/p.so").is_file()
    assert os.readlink(tmp_path / "usr/lib/x86_64-linux-gnu/libfoo.so.1") == "../libfoo.so.1"
    assert (tmp_path / "usr/lib/x86_64-linux-gnu/libfoo.so.1").is_file()


def test_debian_only_paths_removed(tmp_path):
    make(tmp_path, "usr/share/lintian/overrides/x")
    make(tmp_path, "usr/share/doc/x/copyright")
    report, _ = normalize_layout(tmp_path, "x86_64")
    assert not (tmp_path / "usr/share/lintian").exists()
    assert (tmp_path / "usr/share/doc/x/copyright").exists()
    assert "usr/share/lintian" in report.removed


def test_conflicting_files_keep_usr_version(tmp_path):
    make(tmp_path, "bin/x", "old")
    make(tmp_path, "usr/bin/x", "new")
    report, _ = normalize_layout(tmp_path, "x86_64")
    assert (tmp_path / "usr/bin/x").read_text() == "new"
    assert report.conflicts == ["usr/bin/x"]
