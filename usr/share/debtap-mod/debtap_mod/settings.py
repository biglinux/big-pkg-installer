"""User preferences stored in ~/.config/debtap-mod/settings.json."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from gi.repository import GLib


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "debtap-mod" / "settings.json"


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
        try:
            data = json.loads(_config_file().read_text())
        except (OSError, ValueError):
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        path = _config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
