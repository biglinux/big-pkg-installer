"""Thin wrappers around pacman/ldconfig queries (all read-only, no root)."""

import functools
import os
import re
import shutil
import subprocess

ENV_C = {**os.environ, "LC_ALL": "C", "LANG": "C"}

_OWNED_RE = re.compile(r"^(?P<path>/.*) is owned by (?P<pkg>\S+) (?P<ver>\S+)$")
_LDCONFIG_RE = re.compile(r"^\s*(?P<soname>\S+) \((?P<flags>[^)]*)\) => (?P<path>\S+)$")


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, env=ENV_C, check=check)


def available() -> bool:
    return shutil.which("pacman") is not None


@functools.cache
def sync_packages() -> frozenset[str]:
    result = _run(["pacman", "-Slq"])
    return frozenset(result.stdout.split())


@functools.cache
def installed_packages() -> frozenset[str]:
    result = _run(["pacman", "-Qq"])
    return frozenset(result.stdout.split())


@functools.cache
def repo_order() -> tuple[str, ...]:
    result = _run(["pacman-conf", "--repo-list"])
    return tuple(result.stdout.split())


def installed_version(name: str) -> str | None:
    result = _run(["pacman", "-Q", name])
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    return parts[1] if len(parts) == 2 else None


def owners_of(paths: list[str]) -> dict[str, str]:
    """Map existing absolute paths to the package that owns them."""
    owners: dict[str, str] = {}
    for start in range(0, len(paths), 200):
        chunk = paths[start : start + 200]
        result = _run(["pacman", "-Qo", *chunk])
        for line in result.stdout.splitlines():
            match = _OWNED_RE.match(line.strip())
            if match:
                owners[match["path"]] = match["pkg"]
    return owners


@functools.cache
def ldconfig_cache() -> tuple[tuple[str, str, str], ...]:
    ldconfig = shutil.which("ldconfig") or "/usr/bin/ldconfig"
    result = _run([ldconfig, "-p"])
    entries = []
    for line in result.stdout.splitlines():
        match = _LDCONFIG_RE.match(line)
        if match:
            entries.append((match["soname"], match["flags"], match["path"]))
    return tuple(entries)


def ldconfig_lookup(sonames: set[str], flag: str | None) -> dict[str, str]:
    """Resolve sonames to library paths using the ld.so cache.

    ``flag`` is the ld.so.cache marker for the wanted ABI ("x86-64",
    "AArch64"); None accepts entries without a 64-bit marker.
    """
    found: dict[str, str] = {}
    for soname, flags, path in ldconfig_cache():
        if soname not in sonames or soname in found:
            continue
        if flag is None:
            if "64" in flags:
                continue
        elif flag not in flags:
            continue
        found[soname] = path
    return found


def files_db_lookup(names: set[str]) -> dict[str, list[tuple[str, str, str]]]:
    """Search the sync file databases (pacman -F) for files with these names.

    Returns name -> [(repo, package, path)].
    """
    found: dict[str, list[tuple[str, str, str]]] = {}
    ordered = sorted(names)
    for start in range(0, len(ordered), 100):
        chunk = ordered[start : start + 100]
        result = _run(["pacman", "-F", "--machinereadable", *chunk])
        for line in result.stdout.splitlines():
            parts = line.split("\0")
            if len(parts) != 4:
                continue
            repo, pkg, _ver, path = parts
            base = os.path.basename(path)
            if base in names:
                found.setdefault(base, []).append((repo, pkg, path))
    return found


def vercmp(a: str, b: str) -> int:
    result = _run(["vercmp", a, b])
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0
