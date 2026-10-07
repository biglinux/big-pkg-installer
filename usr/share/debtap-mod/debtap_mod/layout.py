"""Make a Debian file tree match the Arch Linux filesystem hierarchy."""

import filecmp
import os
import posixpath
import shutil
from dataclasses import dataclass, field
from pathlib import Path

MULTIARCH = {
    "x86_64": "x86_64-linux-gnu",
    "aarch64": "aarch64-linux-gnu",
    "armv7h": "arm-linux-gnueabihf",
    "i686": "i386-linux-gnu",
    "riscv64": "riscv64-linux-gnu",
}

# Paths that only make sense for dpkg/apt.
DEBIAN_ONLY = ("usr/share/lintian", "usr/share/bug", "etc/apt")


def merge_rules(arch: str) -> list[tuple[str, str]]:
    rules = [
        ("bin", "usr/bin"),
        ("sbin", "usr/bin"),
        ("usr/sbin", "usr/bin"),
        ("lib64", "usr/lib"),
        ("usr/lib64", "usr/lib"),
        ("lib32", "usr/lib32"),
        ("lib", "usr/lib"),
    ]
    triplet = MULTIARCH.get(arch)
    if triplet:
        rules.append((f"usr/lib/{triplet}", "usr/lib"))
    return rules


def map_path(rel: str, rules: list[tuple[str, str]]) -> str:
    for src, dst in rules:
        if rel == src or rel.startswith(src + "/"):
            rel = dst + rel[len(src):]
    return rel


@dataclass
class LayoutReport:
    moved: dict[str, str] = field(default_factory=dict)  # old top dir -> new dir
    conflicts: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    compat_links: list[str] = field(default_factory=list)


def _walk(root: str):
    """Yield (relpath, is_dir, is_link) for every entry, parents first."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            is_link = os.path.islink(full)
            yield rel, (os.path.isdir(full) and not is_link), is_link


def normalize_layout(root: Path, arch: str) -> tuple[LayoutReport, dict[str, str]]:
    """Apply the merge rules in place.

    Returns the report and a mapping old relative path -> new relative path
    for every entry that moved (used to update conffiles and owners).
    """
    report = LayoutReport()
    rules = merge_rules(arch)
    root_s = str(root)
    path_map: dict[str, str] = {}

    for rel in DEBIAN_ONLY:
        target = os.path.join(root_s, rel)
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
            report.removed.append(rel)

    present = [src for src, _dst in rules if os.path.isdir(os.path.join(root_s, src))
               and not os.path.islink(os.path.join(root_s, src))]
    if not present:
        return report, path_map

    triplet_dir = f"usr/lib/{MULTIARCH.get(arch, '')}"
    triplet_children: list[str] = []

    entries = list(_walk(root_s))
    # Symlink targets must be computed before anything moves.
    new_targets: dict[str, tuple[str, str]] = {}
    for rel, _is_dir, is_link in entries:
        new_rel = map_path(rel, rules)
        if new_rel != rel:
            path_map[rel] = new_rel
        if not is_link:
            continue
        target = os.readlink(os.path.join(root_s, rel))
        if target.startswith("/"):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
        if resolved.startswith(".."):
            continue
        new_resolved = map_path(resolved, rules)
        if new_resolved != resolved or new_rel != rel:
            new_targets[new_rel] = (target, posixpath.relpath(new_resolved, posixpath.dirname(new_rel) or "."))

    for src, dst in rules:
        src_full = os.path.join(root_s, src)
        if not os.path.isdir(src_full) or os.path.islink(src_full):
            continue
        if src == triplet_dir:
            triplet_children = sorted(os.listdir(src_full))
        _merge_dir(src_full, os.path.join(root_s, dst), root_s, report)
        report.moved[src] = dst

    for new_rel, (old_target, target) in new_targets.items():
        full = os.path.join(root_s, new_rel)
        # skip entries replaced by another file during a merge conflict
        if os.path.islink(full) and os.readlink(full) == old_target and old_target != target:
            os.unlink(full)
            os.symlink(target, full)

    # Keep the Debian multiarch paths working for programs that hardcode them.
    if triplet_children:
        os.makedirs(os.path.join(root_s, triplet_dir), exist_ok=True)
        for name in triplet_children:
            link = os.path.join(root_s, triplet_dir, name)
            if not os.path.lexists(link):
                os.symlink(f"../{name}", link)
                report.compat_links.append(f"{triplet_dir}/{name}")
    return report, path_map


def _merge_dir(src: str, dst: str, root: str, report: LayoutReport) -> None:
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        s_is_dir = os.path.isdir(s) and not os.path.islink(s)
        if not os.path.lexists(d):
            os.rename(s, d)
            continue
        d_is_dir = os.path.isdir(d) and not os.path.islink(d)
        if s_is_dir and d_is_dir:
            _merge_dir(s, d, root, report)
            continue
        rel = os.path.relpath(d, root)
        # A real file wins over a symlink (often bin/foo -> /usr/bin/foo pairs)
        if os.path.islink(d) and not os.path.islink(s):
            _remove(d)
            os.rename(s, d)
        elif os.path.islink(s) and not os.path.islink(d):
            _remove(s)
        elif not s_is_dir and not d_is_dir and filecmp.cmp(s, d, shallow=False):
            _remove(s)
        else:
            report.conflicts.append(rel)
            _remove(s)
    os.rmdir(src)


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
