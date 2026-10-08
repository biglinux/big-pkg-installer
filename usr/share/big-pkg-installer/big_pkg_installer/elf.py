"""Minimal ELF reader: machine, interpreter and dynamic section entries.

Reads only the headers and the few bytes it needs, so scanning a 300 MB
Electron binary costs a handful of small reads.
"""

import platform
import struct
from dataclasses import dataclass, field

ET_EXEC = 2
ET_DYN = 3
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_RUNPATH = 29

# pacman arch -> (e_machine, ELF class)
HOST_MACHINES = {
    "x86_64": (62, 2),
    "aarch64": (183, 2),
    "riscv64": (243, 2),
    "i686": (3, 1),
    "armv7h": (40, 1),
}

_MAX_STRTAB = 64 << 20


@dataclass
class ElfInfo:
    machine: int
    elfclass: int
    etype: int
    interp: str | None = None
    needed: list[str] = field(default_factory=list)
    soname: str | None = None
    runpath: list[str] = field(default_factory=list)

    @property
    def is_program(self) -> bool:
        return self.etype == ET_EXEC or (self.etype == ET_DYN and self.interp is not None)


def host_arch() -> str:
    machine = platform.machine()
    return {"armv7l": "armv7h", "amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def read_elf(path: str) -> ElfInfo | None:
    try:
        with open(path, "rb") as f:
            return _read(f)
    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        return None


def _read(f) -> ElfInfo | None:
    ident = f.read(16)
    if len(ident) < 16 or ident[:4] != b"\x7fELF":
        return None
    elfclass, data = ident[4], ident[5]
    end = "<" if data == 1 else ">"
    if elfclass == 1:
        hdr = struct.unpack(end + "HHIIIIIHHHHHH", f.read(36))
        phdr_fmt, dyn_fmt = end + "IIIIIIII", end + "iI"
    elif elfclass == 2:
        hdr = struct.unpack(end + "HHIQQQIHHHHHH", f.read(48))
        phdr_fmt, dyn_fmt = end + "IIQQQQQQ", end + "qQ"
    else:
        return None
    etype, machine, phoff, phentsize, phnum = hdr[0], hdr[1], hdr[4], hdr[8], hdr[9]
    info = ElfInfo(machine=machine, elfclass=elfclass, etype=etype)
    if phnum == 0 or phnum > 4096 or phentsize < struct.calcsize(phdr_fmt):
        return info

    loads: list[tuple[int, int, int]] = []
    dynamic = interp = None
    f.seek(phoff)
    table = f.read(phentsize * phnum)
    for i in range(phnum):
        entry = struct.unpack_from(phdr_fmt, table, i * phentsize)
        if elfclass == 1:
            p_type, p_offset, p_vaddr, _paddr, p_filesz = entry[:5]
        else:
            p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = entry[:6]
        if p_type == PT_LOAD:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
        elif p_type == PT_INTERP:
            interp = (p_offset, p_filesz)

    if interp and 0 < interp[1] < 4096:
        f.seek(interp[0])
        info.interp = f.read(interp[1]).split(b"\0", 1)[0].decode("utf-8", "replace")
    if not dynamic or dynamic[1] > 1 << 20:
        return info

    f.seek(dynamic[0])
    blob = f.read(dynamic[1])
    size = struct.calcsize(dyn_fmt)
    entries = []
    strtab = strsz = None
    for off in range(0, len(blob) - size + 1, size):
        tag, val = struct.unpack_from(dyn_fmt, blob, off)
        if tag == DT_NULL:
            break
        if tag == DT_STRTAB:
            strtab = val
        elif tag == DT_STRSZ:
            strsz = val
        elif tag in (DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH):
            entries.append((tag, val))
    if strtab is None or not strsz or strsz > _MAX_STRTAB:
        return info

    offset = None
    for vaddr, foff, filesz in loads:
        if vaddr <= strtab < vaddr + filesz:
            offset = strtab - vaddr + foff
            break
    if offset is None:
        return info
    f.seek(offset)
    strings = f.read(strsz)

    def string_at(index: int) -> str:
        endpos = strings.find(b"\0", index)
        return strings[index : endpos if endpos >= 0 else None].decode("utf-8", "replace")

    for tag, val in entries:
        if val >= len(strings):
            continue
        value = string_at(val)
        if tag == DT_NEEDED:
            info.needed.append(value)
        elif tag == DT_SONAME:
            info.soname = value
        else:
            info.runpath.extend(p for p in value.split(":") if p)
    return info
