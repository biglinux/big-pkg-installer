"""Command line interface (compatible with the historic debtap options)."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, installer
from .converter import Callbacks, ConversionError, convert, inspect
from .debfile import Cancelled, DebError
from .i18n import _

STEP_LABELS = {
    "extract": _("Extracting package data"),
    "layout": _("Adapting the directory layout"),
    "deps": _("Resolving dependencies"),
    "scripts": _("Converting maintainer scripts"),
    "build": _("Building the pacman package"),
}

def _colors() -> bool:
    from .pkgcompat import _color_enabled

    return _color_enabled(sys.stdout)


_tty = _colors()


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


def info(text: str) -> None:
    print(_style("==>", "1;32"), _style(text, "1"))


def warn(text: str) -> None:
    print(_style(_("Warning:"), "1;33"), text)


def error(text: str) -> None:
    print(_style(_("Error:"), "1;31"), text, file=sys.stderr)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="debtap-mod",
        description=_("Convert a .deb or .rpm package into a pacman package and install it."),
    )
    parser.add_argument("package", nargs="?", help=_("the .deb or .rpm file"))
    parser.add_argument("-q", "--quiet", action="store_true", help=_("do not ask questions, but open the install script for review"))
    parser.add_argument("-Q", "--Quiet", action="store_true", help=_("do not ask any question"))
    parser.add_argument("-n", "--no-install", action="store_true", help=_("only build the package, do not install it"))
    parser.add_argument("-o", "--output", metavar="DIR", help=_("keep the built package in DIR"))
    parser.add_argument("-p", "--pkgbuild", action="store_true", help=_("also write a PKGBUILD next to the package file"))
    parser.add_argument("-P", "--Pkgbuild", action="store_true", help=_("only write a PKGBUILD, do not build"))
    parser.add_argument("-u", "--update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-s", "--pseudo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-w", "--wipeout", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-v", "--version", action="version", version=__version__)
    return parser


def _ask(question: str) -> bool:
    try:
        answer = input(_("{question} [y/N] ").format(question=question))
    except EOFError:
        return False
    # accept the English "y" and the first letter of the translated "yes"
    return answer.strip().lower()[:1] in {"y", _("yes")[:1].lower()}


def _review(path: str) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.call([*editor.split(), path])
    elif shutil.which("xdg-open"):
        subprocess.call(["xdg-open", path])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.update:
        print(_("debtap-mod no longer needs a local database; nothing to update."))
        return 0
    if not args.package:
        build_parser().print_usage(sys.stderr)
        error(_("no package file given"))
        return 2
    if args.pseudo or args.wipeout:
        warn(_("the -s and -w options are obsolete and were ignored"))

    try:
        conversion = inspect(args.package)
    except DebError as exc:
        error(str(exc))
        return 1

    deb = conversion.deb
    info(f"{deb.name} {deb.version} ({deb.architecture}) → {conversion.pkgname} {conversion.full_version}")
    if deb.summary:
        print("    " + deb.summary)
    print("    " + _("Installed size: {size}").format(size=human_size(deb.installed_size)))
    for warning in conversion.warnings:
        warn(warning)

    portable_dir = None
    if args.pkgbuild or args.Pkgbuild:
        portable_dir = Path(args.package).resolve().parent / conversion.pkgname

    if args.Pkgbuild:
        try:
            convert(conversion, Callbacks(), portable_dir=portable_dir, build=False)
        except (ConversionError, DebError) as exc:
            error(str(exc))
            return 1
        finally:
            conversion.cleanup()
        info(_("PKGBUILD written to {path}").format(path=portable_dir))
        return 0

    def on_step(step: str) -> None:
        info(STEP_LABELS[step] + "…")

    callbacks = Callbacks(step=on_step, log=lambda line: print("    " + line) if line else None)
    try:
        convert(conversion, callbacks, portable_dir=portable_dir)
    except (ConversionError, DebError) as exc:
        error(str(exc))
        log = getattr(exc, "log", "")
        if log:
            print(log, file=sys.stderr)
        return 1
    except Cancelled:
        error(_("cancelled"))
        return 130
    except KeyboardInterrupt:
        error(_("cancelled"))
        return 130

    try:
        return _finish(conversion, args, portable_dir)
    finally:
        conversion.cleanup()


def _finish(conversion, args, portable_dir) -> int:
    spec = conversion.spec
    print()
    info(_("Package {file} built").format(file=conversion.pkgfile.name))
    if spec.depends:
        print("    depends: " + " ".join(spec.depends))
    if spec.optdepends:
        print("    optdepends: " + " ".join(spec.optdepends))
    for note in conversion.notes:
        print("    " + _style("•", "1;34"), note)
    for warning in conversion.warnings:
        warn(warning)
    visible = [a for a in conversion.apps if not a.hidden]
    if visible:
        names = ", ".join(f"“{a.name}”" for a in visible)
        info(_("Menu entries: {names}").format(names=names))
    if portable_dir:
        info(_("PKGBUILD written to {path}").format(path=portable_dir))

    if args.output:
        dest = Path(args.output)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(conversion.pkgfile, dest / conversion.pkgfile.name)
        info(_("Package saved to {path}").format(path=dest / conversion.pkgfile.name))
    if args.no_install:
        return 0

    if conversion.conflicts and conversion.conflicts.owned:
        error(_("some files belong to other installed packages; remove them first:"))
        for path, owner in conversion.conflicts.owned.items():
            print(f"    {path} ({owner})")
        return 1

    if args.quiet and spec.install:
        with tempfile.NamedTemporaryFile("w", suffix=".install", delete=False) as tmp:
            tmp.write(spec.install)
        _review(tmp.name)
        os.unlink(tmp.name)
    if not args.quiet and not args.Quiet:
        if spec.install:
            print()
            print(_style(_("Install script that will run as root:"), "1"))
            print(conversion.install_functions)
        if not _ask(_("Install {name} now?").format(name=spec.pkgname)):
            return 0

    overwrite = conversion.conflicts.unowned if conversion.conflicts else []
    status = installer.run_install(conversion.pkgfile, overwrite)
    if status != 0 or not installer.is_installed(spec.pkgname, conversion.full_version):
        error(_("pacman could not install the package (exit code {code})").format(code=status))
        return 1
    info(_("{name} installed successfully").format(name=spec.pkgname))
    if visible:
        info(_("Look for {names} in the applications menu.").format(names=", ".join(f"“{a.name}”" for a in visible)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
