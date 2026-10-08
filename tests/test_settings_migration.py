import json

from big_pkg_installer.settings import Settings


def test_settings_from_debtap_mod_are_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "debtap-mod"
    legacy.mkdir()
    (legacy / "settings.json").write_text(json.dumps({"save_copy": True, "save_dir": "/srv/old"}))
    loaded = Settings.load()
    assert loaded.save_copy and loaded.save_dir == "/srv/old"
    loaded.save()
    assert (tmp_path / "big-pkg-installer" / "settings.json").exists()
