"""Install the built package with pacman and check the result."""

import configparser
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import pacman


@dataclass
class DesktopEntry:
    file_id: str
    name: str
    icon: str
    hidden: bool


def desktop_entries(root: Path) -> list[DesktopEntry]:
    entries = []
    apps = root / "usr/share/applications"
    if not apps.is_dir():
        return entries
    for path in sorted(apps.rglob("*.desktop")):
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            continue
        if not parser.has_section("Desktop Entry"):
            continue
        section = parser["Desktop Entry"]
        if section.get("Type", "Application") != "Application":
            continue
        hidden = section.get("NoDisplay", "false").lower() == "true" or section.get("Hidden", "false").lower() == "true"
        entries.append(
            DesktopEntry(
                file_id=str(path.relative_to(apps)).replace("/", "-"),
                name=section.get("Name", path.stem),
                icon=section.get("Icon", ""),
                hidden=hidden,
            )
        )
    return entries


@dataclass
class FileConflicts:
    owned: dict[str, str]  # path -> owning package (blocks the installation)
    unowned: list[str]  # files left behind by something else (safe to overwrite)


def check_conflicts(root: Path, own_names: set[str]) -> FileConflicts:
    existing = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            system_path = "/" + rel
            if os.path.lexists(system_path) and not (os.path.isdir(system_path) and not os.path.islink(system_path)):
                existing.append(system_path)
    owners = pacman.owners_of(existing)
    owned = {p: o for p, o in owners.items() if o not in own_names}
    unowned = [p for p in existing if p not in owners]
    return FileConflicts(owned=owned, unowned=unowned)


def install_command(pkgfile: Path, overwrite: list[str], interactive_tty: bool) -> list[str]:
    cmd = ["pacman", "-U", "--noconfirm", "--ask=4"]
    for path in overwrite:
        cmd += ["--overwrite", path.replace("*", "\\*").replace("?", "\\?")]
    cmd.append(str(pkgfile))
    if os.geteuid() == 0:
        return cmd
    if interactive_tty and shutil.which("sudo"):
        return ["sudo", *cmd]
    return ["pkexec", *cmd]


def run_install(
    pkgfile: Path,
    overwrite: list[str],
    log: Callable[[str], None] | None = None,
    interactive_tty: bool | None = None,
) -> int:
    if interactive_tty is None:
        interactive_tty = sys.stdin.isatty()
    cmd = install_command(pkgfile, overwrite, interactive_tty)
    if log:
        log("$ " + " ".join(cmd))
    env = {**os.environ, "LC_ALL": "C"}
    if interactive_tty:
        return subprocess.call(cmd, env=env)
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        if log:
            log(line.rstrip("\n"))
    return proc.wait()


def is_installed(name: str, version: str) -> bool:
    return pacman.installed_version(name) == version
