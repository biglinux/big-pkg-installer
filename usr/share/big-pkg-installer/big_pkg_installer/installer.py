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
    if not interactive_tty:
        # one line per event, easier to follow in the GUI
        cmd.append("--noprogressbar")
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
    on_start: Callable[[subprocess.Popen], None] | None = None,
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
    if on_start:
        on_start(proc)
    for line in proc.stdout:
        if log:
            log(line.rstrip("\n"))
    return proc.wait()


def is_installed(name: str, version: str) -> bool:
    return pacman.installed_version(name) == version


@dataclass
class PlannedPackage:
    name: str
    version: str
    size: int
    repo: str

    @property
    def local(self) -> bool:
        return self.repo == "local"


@dataclass
class TransactionPreview:
    packages: list[PlannedPackage]
    error: str = ""

    @property
    def download_size(self) -> int:
        return sum(p.size for p in self.packages if not p.local)

    @property
    def total_size(self) -> int:
        return sum(p.size for p in self.packages)


def preview_transaction(pkgfile: Path) -> TransactionPreview:
    """What "pacman -U" would install, computed without root (dry run)."""
    result = subprocess.run(
        ["pacman", "-Up", "--print-format", "%n|%v|%s|%r", str(pkgfile)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    packages = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            name, version, size, repo = parts
            packages.append(PlannedPackage(name, version, int(size) if size.isdigit() else 0, repo))
    if result.returncode != 0:
        error = "\n".join(line for line in (result.stderr + result.stdout).splitlines() if line.strip())
        return TransactionPreview(packages=packages, error=error or f"pacman exited with {result.returncode}")
    # the package being installed first, then its dependencies
    packages.sort(key=lambda p: (not p.local, p.name))
    return TransactionPreview(packages=packages)


INSTALL_STEPS = ("prepare", "download", "install", "configure", "finish")


class InstallProgress:
    """Follow "pacman -U --noprogressbar" output (LC_ALL=C) step by step."""

    def __init__(self, downloads: int):
        self.downloads = downloads
        self.downloaded = 0
        self.step = "prepare"

    def feed(self, line: str) -> bool:
        """Return True when the step or the download counter changed."""
        text = line.strip()
        previous = (self.step, self.downloaded)
        if text.startswith(":: Retrieving packages") and self.step == "prepare":
            self.step = "download"
        elif text.startswith("downloading ") or text.endswith(" downloading..."):
            self.step = "download"
            self.downloaded = min(self.downloaded + 1, max(self.downloads, 1))
        elif text.startswith(":: Processing package changes") or text.startswith(
            ("installing ", "upgrading ", "reinstalling ")
        ):
            self.step = "install"
        elif text.startswith(":: Running post-transaction hooks") or (
            self.step == "configure" and text.startswith("(")
        ):
            self.step = "configure"
        return (self.step, self.downloaded) != previous

    @property
    def fraction(self) -> float:
        base = {"prepare": 0.05, "download": 0.1, "install": 0.6, "configure": 0.85, "finish": 1.0}[self.step]
        if self.step == "download" and self.downloads:
            return 0.1 + 0.5 * self.downloaded / self.downloads
        return base
