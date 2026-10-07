"""Per-package adjustments loaded from data/quirks.toml."""

import fnmatch
import glob
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .deps import DATA_DIR
from .i18n import _

SCRIPT_NAMES = ("preinst", "postinst", "prerm", "postrm")


# Notes used in data/quirks.toml, listed here so xgettext extracts them;
# they are translated again with _() when shown.
_NOTES = (
    _("The Debian scripts only register an APT repository and an AppArmor profile for Ubuntu; they are not needed here."),
)


@dataclass
class Quirk:
    match: str = "*"
    name: str | None = None
    elf_deps: bool = True
    clear_depends: bool = False
    add_depends: list[str] = field(default_factory=list)
    remove_depends: list[str] = field(default_factory=list)
    skip_scripts: list[str] = field(default_factory=list)
    remove_files: list[str] = field(default_factory=list)
    post_install: str = ""
    note: str = ""
    add_file: list[dict] = field(default_factory=list)
    replace: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Quirk":
        data = dict(data)
        skip = data.pop("skip_scripts", [])
        quirk = cls(**data)
        quirk.skip_scripts = list(SCRIPT_NAMES) if skip is True else list(skip or [])
        return quirk

    def filter_depends(self, depends: list[str]) -> list[str]:
        result = [] if self.clear_depends else list(depends)
        result = [d for d in result if not any(fnmatch.fnmatch(d, p) for p in self.remove_depends)]
        for dep in self.add_depends:
            if dep not in result:
                result.append(dep)
        return sorted(result)

    def apply_files(self, root: Path) -> list[str]:
        """Apply file edits inside the package tree; return a change log."""
        changes = []
        for pattern in self.remove_files:
            for path in glob.glob(os.path.join(root, pattern)):
                if os.path.isdir(path) and not os.path.islink(path):
                    continue
                os.unlink(path)
                changes.append(f"removed {os.path.relpath(path, root)}")
                parent = os.path.dirname(path)
                while parent != str(root) and not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
        for entry in self.add_file:
            path = root / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["content"])
            path.chmod(int(entry.get("mode", 0o644)))
            changes.append(f"added {entry['path']}")
        for entry in self.replace:
            path = root / entry["path"]
            if path.is_file() and not path.is_symlink():
                text = path.read_text(errors="replace")
                if entry["search"] in text:
                    path.write_text(text.replace(entry["search"], entry["replace"]))
                    changes.append(f"edited {entry['path']}")
        return changes


def load_quirks(path: Path | None = None) -> list[Quirk]:
    with open(path or DATA_DIR / "quirks.toml", "rb") as f:
        data = tomllib.load(f)
    return [Quirk.from_dict(item) for item in data.get("quirk", [])]


def quirk_for(debname: str, quirks: list[Quirk] | None = None) -> Quirk:
    for quirk in quirks if quirks is not None else load_quirks():
        if fnmatch.fnmatch(debname, quirk.match):
            return quirk
    return Quirk()
