"""User preferences stored in ~/.config/big-pkg-installer/settings.json."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from gi.repository import GLib


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "big-pkg-installer" / "settings.json"


def _downloads_dir() -> str:
    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")


@dataclass
class Settings:
    save_copy: bool = False
    save_dir: str = ""

    @property
    def save_path(self) -> Path:
        return Path(self.save_dir or _downloads_dir())

    @classmethod
    def load(cls) -> "Settings":
        path = _config_file()
        legacy = path.parent.parent / "debtap-mod" / "settings.json"
        if not path.exists() and legacy.exists():
            # the program used to be called debtap-mod
            path = legacy
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        path = _config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
