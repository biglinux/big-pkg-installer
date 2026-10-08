from debtap_mod.quirks import Quirk, load_quirks, quirk_for


def test_bundled_quirks_load():
    quirks = load_quirks()
    names = {q.match for q in quirks}
    assert {"warsaw", "chatgpt", "pje-office", "bricscad*"} <= names


def test_quirk_matching():
    assert quirk_for("warsaw").name == "warsaw-deb"
    assert quirk_for("bricscad-v24").post_install
    assert quirk_for("something-else") == Quirk()


def test_filter_depends():
    quirk = Quirk(remove_depends=["java-*"], add_depends=["libcurl-gnutls"])
    assert quirk.filter_depends(["java-runtime", "gtk3"]) == ["gtk3", "libcurl-gnutls"]
    assert Quirk(clear_depends=True, add_depends=["a"]).filter_depends(["b"]) == ["a"]


def test_skip_scripts_true_means_all():
    skipped = quirk_for("chatgpt").skip_scripts
    assert {"preinst", "postinst", "prerm", "postrm", "pre", "post", "preun", "postun"} <= set(skipped)


def test_apply_files(tmp_path):
    (tmp_path / "etc/apparmor.d").mkdir(parents=True)
    (tmp_path / "etc/apparmor.d/chatgpt").write_text("profile")
    (tmp_path / "usr/share/shodo/shortcuts").mkdir(parents=True)
    (tmp_path / "usr/share/shodo/shortcuts/shodo.desktop").write_text("Exec=java -jar x")
    quirk_for("chatgpt").apply_files(tmp_path)
    assert not (tmp_path / "etc").exists()  # emptied directories are pruned
    quirk_for("shodo").apply_files(tmp_path)
    assert "java-8-openjdk" in (tmp_path / "usr/share/shodo/shortcuts/shodo.desktop").read_text()
    quirk_for("pje-office").apply_files(tmp_path)
    assert (tmp_path / "usr/bin/pjeOffice").stat().st_mode & 0o111
