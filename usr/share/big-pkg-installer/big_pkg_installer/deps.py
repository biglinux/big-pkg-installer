"""Work out pacman dependencies for an extracted .deb or .rpm package."""

import os
import re
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from . import pacman
from .elf import HOST_MACHINES, ElfInfo, is_elf, read_elf

DATA_DIR = Path(__file__).resolve().parent / "data"

# Virtual packages that pacman can resolve through "provides" even though
# no package carries that exact name.
VIRTUAL_PACKAGES = {"java-runtime", "java-environment", "libgl", "opengl-driver", "vulkan-driver"}

LDCONFIG_FLAGS = {62: "x86-64", 183: "AArch64", 243: "RISC-V"}

_DEP_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9.+\-]+)(?::(?P<arch>[a-z0-9-]+))?")


@cache
def name_tables() -> tuple[dict[str, str], dict[str, str]]:
    with open(DATA_DIR / "debian-names.toml", "rb") as f:
        data = tomllib.load(f)
    return data.get("map", {}), data.get("soname", {})


@cache
def rpm_name_table() -> dict[str, str]:
    with open(DATA_DIR / "rpm-names.toml", "rb") as f:
        return tomllib.load(f).get("map", {})


@dataclass
class DeclaredDeps:
    """Dependencies written in the package metadata, in the source naming."""

    required: list[list[str]]
    recommended: list[list[str]]
    suggested: list[list[str]]
    mapper: Callable[[str, frozenset[str]], str | None]
    # names the ELF scan already handles (libraries): no warning if unmapped
    covered: Callable[[str], bool]
    origin: str


@dataclass
class ScannedElf:
    path: str  # relative to the package root
    info: ElfInfo


@dataclass
class DependencyReport:
    depends: list[str] = field(default_factory=list)
    optdepends: dict[str, str] = field(default_factory=dict)
    unresolved_sonames: list[str] = field(default_factory=list)
    unresolved_declared: list[str] = field(default_factory=list)
    foreign_binaries: int = 0
    elf_count: int = 0


def parse_relationship(value: str) -> list[list[str]]:
    """Parse a Depends-like field into [[alternative, ...], ...] names."""
    groups = []
    for group in value.replace("\n", " ").split(","):
        names = []
        for alternative in group.split("|"):
            match = _DEP_RE.match(alternative)
            if not match:
                continue
            name = match["name"].lower()
            if match["arch"] == "any":
                name += ":any"
            names.append(name)
        if names:
            groups.append(names)
    return groups


def map_debian_name(name: str, sync: frozenset[str]) -> str | None:
    """Return the Arch name, "" when nothing is needed, None when unknown."""
    table, _ = name_tables()
    if name in table:
        return table[name]
    bare = name.split(":", 1)[0]
    if bare in table:
        return table[bare]
    if bare in sync and not bare.startswith("lib"):
        return bare
    return None


def debian_declared(control: dict[str, str]) -> DeclaredDeps:
    return DeclaredDeps(
        required=parse_relationship(control.get("pre-depends", "")) + parse_relationship(control.get("depends", "")),
        recommended=parse_relationship(control.get("recommends", "")),
        suggested=parse_relationship(control.get("suggests", "")),
        mapper=map_debian_name,
        covered=lambda name: name.startswith("lib"),
        origin="Debian",
    )


def map_rpm_name(name: str, sync: frozenset[str]) -> str | None:
    """Fedora/openSUSE package name or file path -> Arch package."""
    table = rpm_name_table()
    if name in table:
        return table[name]
    if name.startswith("/"):
        if os.path.lexists(name):
            return pacman.owners_of([os.path.realpath(name)]).get(os.path.realpath(name))
        return None
    if name.startswith("python3-") and "python-" + name[8:] in sync:
        return "python-" + name[8:]
    if name.startswith("perl-") and name in sync:
        return name
    lowered = name.lower()
    if lowered in sync:
        return lowered
    return None


def _rpm_covered(name: str) -> bool:
    # sonames ("libc.so.6()(64bit)"), rpmlib()/config() markers and libraries
    return "(" in name or name.startswith(("lib", "/", "rpmlib", "config"))


def parse_rpm_requirement(value: str) -> list[list[str]]:
    """Plain names, or RPM rich dependencies such as "(a or b)", into groups."""
    value = value.strip()
    if not value.startswith("("):
        return [[value]] if value else []
    expr = value.strip("() ")
    for keyword in (" if ", " unless ", " with ", " without "):
        expr = expr.split(keyword, 1)[0]
    groups = []
    for part in expr.split(" and "):
        names = [alt.strip("() ").split()[0] for alt in part.split(" or ") if alt.strip("() ")]
        if names:
            groups.append(names)
    return groups


def rpm_declared(requires: list[str], recommends: list[str], suggests: list[str]) -> DeclaredDeps:
    def groups(values):
        result = []
        for value in values:
            result.extend(parse_rpm_requirement(value))
        return result

    return DeclaredDeps(
        required=groups(requires),
        recommended=groups(recommends),
        suggested=groups(suggests),
        mapper=map_rpm_name,
        covered=_rpm_covered,
        origin="RPM",
    )


def _package_usable(name: str, sync: frozenset[str], installed: frozenset[str]) -> bool:
    return name in sync or name in installed or name in VIRTUAL_PACKAGES


def scan_elves(root: Path) -> list[ScannedElf]:
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full) or not is_elf(full):
                continue
            info = read_elf(full)
            if info is not None:
                found.append(ScannedElf(os.path.relpath(full, root), info))
    return found


def _is_foreign_libc(info: ElfInfo) -> bool:
    """Prebuilt modules for musl or Android that will never be loaded here."""
    for lib in info.needed:
        if lib.startswith("libc.musl") or lib in ("libc.so", "liblog.so", "libandroid.so"):
            return True
    return info.interp is not None and "musl" in info.interp


def _bundled_names(root: Path) -> set[str]:
    names = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        names.update(filenames)
        names.update(dirnames)
    return names


def _choose_provider(candidates: list[tuple[str, str, str]], installed: frozenset[str]) -> str:
    order = {repo: i for i, repo in enumerate(pacman.repo_order())}

    def rank(candidate):
        repo, pkg, path = candidate
        return (
            pkg not in installed,
            not path.startswith("usr/lib/") or path.count("/") != 2,
            pkg.startswith("lib32-"),
            order.get(repo, len(order)),
            len(pkg),
            pkg,
        )

    return min(candidates, key=rank)[1]


def resolve_sonames(sonames: set[str], machine: int, elfclass: int) -> tuple[dict[str, str], set[str]]:
    _, soname_table = name_tables()
    resolved: dict[str, str] = {}
    for soname in list(sonames):
        if soname in soname_table:
            resolved[soname] = soname_table[soname]
    pending = sonames - resolved.keys()

    flag = LDCONFIG_FLAGS.get(machine) if elfclass == 2 else None
    paths = pacman.ldconfig_lookup(pending, flag)
    owners = pacman.owners_of(sorted(set(paths.values())))
    for soname, path in paths.items():
        if path in owners:
            resolved[soname] = owners[path]
    pending = sonames - resolved.keys()

    if pending:
        installed = pacman.installed_packages()
        for soname, candidates in pacman.files_db_lookup(pending).items():
            # 64-bit binaries never want lib32-*, 32-bit ones always do on x86_64
            if elfclass == 2:
                candidates = [c for c in candidates if not c[1].startswith("lib32-") and "/lib32/" not in c[2]]
            if candidates:
                resolved[soname] = _choose_provider(candidates, installed)
    return resolved, sonames - resolved.keys()


def compute_dependencies(
    root: Path,
    declared: DeclaredDeps | dict[str, str],
    target_arch: str,
    self_names: set[str],
    use_elf: bool = True,
) -> DependencyReport:
    if isinstance(declared, dict):
        declared = debian_declared(declared)
    report = DependencyReport()
    sync = pacman.sync_packages()
    installed = pacman.installed_packages()
    depends: set[str] = set()
    optional: dict[str, str] = {}

    if use_elf and target_arch in HOST_MACHINES:
        machine, elfclass = HOST_MACHINES[target_arch]
        elves = scan_elves(root)
        native = []
        for elf in elves:
            if elf.info.machine != machine or elf.info.elfclass != elfclass or _is_foreign_libc(elf.info):
                report.foreign_binaries += 1
            else:
                native.append(elf)
        report.elf_count = len(native)

        bundled = _bundled_names(root)
        provides: dict[str, list[ScannedElf]] = {}
        for elf in native:
            provides.setdefault(os.path.basename(elf.path), []).append(elf)
            if elf.info.soname:
                provides.setdefault(elf.info.soname, []).append(elf)

        # Libraries reachable from the programs are hard dependencies. The
        # rest are loaded with dlopen() (Qt shims, plugins, node modules) and
        # their system libraries become optional dependencies.
        programs = [e for e in native if e.info.is_program]
        reachable: set[str] = set()
        queue = deque(programs)
        while queue:
            elf = queue.popleft()
            if elf.path in reachable:
                continue
            reachable.add(elf.path)
            for lib in elf.info.needed:
                queue.extend(provides.get(lib, []))
        if not programs:
            reachable = {e.path for e in native}

        hard_sonames: set[str] = set()
        soft_sonames: dict[str, str] = {}
        for elf in native:
            for lib in elf.info.needed:
                if lib in bundled or lib.startswith("ld-linux") or lib.startswith("ld64"):
                    continue
                if elf.path in reachable:
                    hard_sonames.add(lib)
                else:
                    soft_sonames.setdefault(lib, os.path.basename(elf.path))

        resolved, unresolved = resolve_sonames(hard_sonames | set(soft_sonames), machine, elfclass)
        report.unresolved_sonames = sorted(unresolved)
        for soname in hard_sonames:
            if soname in resolved:
                depends.add(resolved[soname])
        for soname, user in soft_sonames.items():
            pkg = resolved.get(soname)
            if pkg and pkg not in depends:
                optional.setdefault(pkg, f"used by {user}")

    for group in declared.required:
        mapped = [declared.mapper(n, sync) for n in group]
        if any(m == "" for m in mapped):
            continue
        usable = [m for m in mapped if m and _package_usable(m, sync, installed)]
        if usable:
            depends.add(usable[0])
        elif not all(declared.covered(n) for n in group):
            # library alternatives are already covered by the ELF scan
            report.unresolved_declared.append(" | ".join(group))

    for kind, groups in (("recommended", declared.recommended), ("suggested", declared.suggested)):
        for group in groups:
            for name in group:
                mapped = declared.mapper(name, sync)
                if mapped and _package_usable(mapped, sync, installed) and mapped not in depends:
                    optional.setdefault(mapped, f"{kind} by the {declared.origin} package")
                    break

    depends -= self_names
    for name in self_names | depends:
        optional.pop(name, None)
    report.depends = sorted(depends)
    report.optdepends = dict(sorted(optional.items()))
    return report
