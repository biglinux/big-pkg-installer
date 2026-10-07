"""High level conversion pipeline used by both the CLI and the GUI."""

import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import pacman
from .debfile import Cancelled, DebPackage, extract_data, open_deb
from .deps import DependencyReport, compute_dependencies
from .elf import host_arch
from .i18n import _
from .installer import DesktopEntry, FileConflicts, check_conflicts, desktop_entries
from .layout import normalize_layout
from .pkgbuild import PackageSpec, run_makepkg, write_build_dir, write_portable
from .quirks import Quirk, quirk_for
from .scripts import SCRIPTS_MARKER, build_install
from .version import PacmanVersion, to_pacman_name, to_pacman_version

DEB_TO_PACMAN_ARCH = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "armhf": "armv7h",
    "i386": "i686",
    "riscv64": "riscv64",
    "all": "any",
}

STEPS = ("extract", "layout", "deps", "scripts", "build")


class ConversionError(Exception):
    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


STALE_WORKDIR_SECONDS = 6 * 3600


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = Path(base) / "debtap-mod"
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_stale_workdirs() -> None:
    """Delete work directories left behind by a crashed or killed run."""
    now = time.time()
    for entry in cache_dir().iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > STALE_WORKDIR_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


@dataclass
class Conversion:
    deb: DebPackage
    pkgname: str
    version: PacmanVersion
    arch: str
    quirk: Quirk
    workdir: Path | None = None
    spec: PackageSpec | None = None
    deps: DependencyReport | None = None
    pkgfile: Path | None = None
    apps: list[DesktopEntry] = field(default_factory=list)
    conflicts: FileConflicts | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)

    @property
    def full_version(self) -> str:
        return str(self.version)

    @property
    def install_script(self) -> str | None:
        return self.spec.install if self.spec else None

    @property
    def install_functions(self) -> str | None:
        """The part of the .install a human wants to review (no helpers)."""
        text = self.install_script
        if text and SCRIPTS_MARKER in text:
            return text.split(SCRIPTS_MARKER, 1)[1].strip() + "\n"
        return text

    def cleanup(self) -> None:
        if self.workdir and self.workdir.exists():
            shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir = None


def inspect(path: str | os.PathLike) -> Conversion:
    """Read the package metadata (fast, nothing is extracted)."""
    deb = open_deb(path)
    quirk = quirk_for(deb.name)
    pkgname = quirk.name or to_pacman_name(deb.name) + "-deb"
    arch = DEB_TO_PACMAN_ARCH.get(deb.architecture, deb.architecture)
    conversion = Conversion(deb=deb, pkgname=pkgname, version=to_pacman_version(deb.version), arch=arch, quirk=quirk)
    host = host_arch()
    if arch not in ("any", host):
        conversion.warnings.append(
            _("This package was built for {deb_arch} but this computer is {host}; it will not run here.").format(
                deb_arch=deb.architecture, host=host
            )
        )
    if quirk.note:
        conversion.notes.append(_(quirk.note))
    return conversion


def compatible(conversion: Conversion) -> bool:
    return conversion.arch in ("any", host_arch())


@dataclass
class Callbacks:
    step: Callable[[str], None] = lambda step: None
    progress: Callable[[float], None] = lambda fraction: None
    log: Callable[[str], None] = lambda line: None
    cancel: threading.Event = field(default_factory=threading.Event)

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


def _description(deb: DebPackage) -> str:
    summary = " ".join(deb.summary.split())
    return summary or deb.name


def convert(
    conversion: Conversion,
    callbacks: Callbacks | None = None,
    portable_dir: Path | None = None,
    build: bool = True,
) -> Conversion:
    """Extract, adapt and build the pacman package.

    The work directory lives in ~/.cache/debtap-mod (not /tmp, which is
    often a small tmpfs) and is removed by ``Conversion.cleanup()``.
    """
    cb = callbacks or Callbacks()
    deb = conversion.deb
    if not compatible(conversion):
        raise ConversionError(conversion.warnings[0])

    remove_stale_workdirs()
    conversion.workdir = Path(tempfile.mkdtemp(prefix=f"{conversion.pkgname}-", dir=cache_dir()))
    root = conversion.workdir / "root"
    target_arch = host_arch() if conversion.arch == "any" else conversion.arch
    try:
        cb.step("extract")
        cb.log(_("Extracting {name}…").format(name=deb.path.name))
        extracted = extract_data(deb, root, progress=cb.progress, cancelled=cb.cancelled)
        if extracted.skipped:
            conversion.warnings.append(
                _("Ignored unsafe or special entries: {list}").format(list=", ".join(extracted.skipped[:5]))
            )
        cb.check()

        cb.step("layout")
        report, path_map = normalize_layout(root, target_arch)
        for src, dst in report.moved.items():
            cb.log(f"/{src} → /{dst}")
        for rel in report.conflicts:
            conversion.warnings.append(_("Duplicate file kept from /usr: /{path}").format(path=rel))
        conversion.removed_paths = list(report.removed)
        for change in conversion.quirk.apply_files(root):
            cb.log(change)
            if change.startswith("removed "):
                conversion.removed_paths.append(change.split(" ", 1)[1])
        owners = {path_map.get(p, p): o for p, o in extracted.owners.items() if os.path.lexists(root / path_map.get(p, p))}
        backup = []
        for conffile in deb.conffiles:
            rel = conffile.lstrip("/")
            rel = path_map.get(rel, rel)
            if (root / rel).is_file():
                backup.append(rel)
        conversion.apps = desktop_entries(root)
        cb.check()

        cb.step("deps")
        cb.log(_("Looking for dependencies…"))
        debname = deb.name.lower()
        self_names = {conversion.pkgname, debname}
        deps = compute_dependencies(root, deb.control, target_arch, self_names, use_elf=conversion.quirk.elf_deps)
        conversion.deps = deps
        depends = conversion.quirk.filter_depends(deps.depends)
        optdepends = {k: v for k, v in deps.optdepends.items() if k not in depends}
        if deps.foreign_binaries:
            cb.log(
                _("Ignored {count} binaries built for other systems or architectures.").format(
                    count=deps.foreign_binaries
                )
            )
        if deps.unresolved_sonames:
            conversion.warnings.append(
                _("Libraries not found in the repositories: {list}").format(list=", ".join(deps.unresolved_sonames))
            )
        if deps.unresolved_debian:
            conversion.warnings.append(
                _("Debian dependencies without an equivalent here: {list}").format(
                    list=", ".join(deps.unresolved_debian)
                )
            )
        for dep in depends:
            cb.log(f"depends: {dep}")
        cb.check()

        cb.step("scripts")
        scripts = {
            name: deb.script(name)
            for name in ("preinst", "postinst", "prerm", "postrm")
            if name not in conversion.quirk.skip_scripts and deb.script(name)
        }
        install = build_install(scripts, debname, deb.architecture, conversion.quirk.post_install)
        conversion.notes.extend(install.notes)

        provides = [f"{debname}={conversion.version.pkgver}"] if debname != conversion.pkgname else []
        conflicts = [debname] if debname != conversion.pkgname else []
        if conflicts and debname in pacman.sync_packages():
            # never fight with a native package of the same name
            conflicts, provides = [], []
        conversion.spec = PackageSpec(
            pkgname=conversion.pkgname,
            version=conversion.version,
            pkgdesc=_description(deb),
            arch=conversion.arch,
            url=deb.control.get("homepage", "").strip(),
            depends=depends,
            optdepends=optdepends,
            provides=provides,
            conflicts=conflicts,
            backup=sorted(backup),
            owners=owners,
            install=install.text,
        )
        conversion.conflicts = check_conflicts(root, {conversion.pkgname, debname})
        if conversion.conflicts.owned:
            listed = ", ".join(f"{p} ({o})" for p, o in list(conversion.conflicts.owned.items())[:5])
            conversion.warnings.append(_("Files already owned by other packages: {list}").format(list=listed))
        if portable_dir is not None:
            write_portable(conversion.spec, deb.path, portable_dir, conversion.removed_paths)
        cb.check()
        if not build:
            return conversion

        cb.step("build")
        cb.log(_("Building the package…"))
        write_build_dir(conversion.spec, conversion.workdir)
        try:
            conversion.pkgfile = run_makepkg(conversion.workdir, log=cb.log, cancelled=cb.cancelled)
        except Exception as exc:
            cb.check()
            raise ConversionError(str(exc), getattr(exc, "log", "")) from exc
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(conversion.workdir / "build", ignore_errors=True)
        cb.progress(1.0)
        return conversion
    except BaseException:
        conversion.cleanup()
        raise
