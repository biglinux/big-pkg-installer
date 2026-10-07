"""Translate apt, apt-get, apt-cache, apt-mark, dpkg, dnf and yum commands.

Users coming from Debian, Ubuntu, Mint or Fedora type the commands they
know. Instead of failing (or blindly passing everything to pamac), each
command is translated: the equivalent pacman and pamac commands are shown,
then the right one runs.
"""

import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field

from .i18n import _

FOREIGN_TOOLS = ("apt", "apt-get", "apt-cache", "apt-mark", "dpkg", "dnf", "yum")

# Options that only change how apt/dnf talk to the user; dropped silently.
_IGNORED_FLAGS = {
    "-q", "-qq", "--quiet", "-V", "--verbose-versions", "--no-install-recommends",
    "--install-recommends", "--install-suggests", "--no-install-suggests", "-f", "--fix-broken",
    "-m", "--fix-missing", "--allow-downgrades", "--allow-remove-essential", "--allow-change-held-packages",
    "--best", "--nobest", "--refresh", "--allowerasing", "-C", "--cacheonly", "--color",
    "--show-progress", "--no-show-upgraded", "--with-new-pkgs", "-t", "--target-release",
}
_YES_FLAGS = {"-y", "--yes", "--assume-yes", "--force-yes"}


# --------------------------------------------------------------- the plan
@dataclass
class Plan:
    """What a foreign command means here and how to run it."""

    tool: str
    typed: list[str]
    action: str
    pacman: str = ""
    pamac: str = ""
    run: list[str] | None = None
    notes: list[str] = field(default_factory=list)
    show_help: bool = False
    help_topic: str = ""
    passthrough: bool = False

    @property
    def typed_line(self) -> str:
        return " ".join([self.tool, *self.typed])


def _q(words: list[str]) -> str:
    return " ".join(words)


def _split(args: list[str]) -> tuple[list[str], set[str]]:
    words, flags = [], set()
    for arg in args:
        if arg.startswith("-") and arg != "-":
            flags.add(arg.split("=", 1)[0])
        else:
            words.append(arg)
    return words, flags


def _is_root() -> bool:
    return os.geteuid() == 0


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


class Translator:
    def __init__(self, tool: str, args: list[str]):
        self.tool = tool
        self.args = args
        words, flags = _split(args)
        self.words = words
        self.flags = flags
        self.yes = bool(flags & _YES_FLAGS)

    # ---------------------------------------------------------- builders
    def _pacman(self, *args: str, root: bool = False) -> list[str]:
        cmd = ["pacman", *args]
        if root and self.yes:
            cmd.append("--noconfirm")
        if root and not _is_root():
            cmd = ["sudo", *cmd]
        return cmd

    def _pamac(self, *args: str, confirm_flag: bool = False) -> list[str]:
        cmd = ["pamac", *args]
        if confirm_flag and self.yes:
            cmd.append("--no-confirm")
        return cmd

    def _transaction(self, pacman_args: list[str], pamac_args: list[str]) -> list[str]:
        """Changes go through pamac as a user, pacman when already root."""
        if _is_root() or not _have("pamac"):
            return self._pacman(*pacman_args, root=True)
        return self._pamac(*pamac_args, confirm_flag=True)

    def plan(self, action: str, pacman: list[str], pamac: list[str] | None, run: list[str] | None, *notes: str) -> Plan:
        return Plan(
            tool=self.tool,
            typed=self.args,
            action=action,
            pacman=_q(pacman),
            pamac=_q(pamac) if pamac else "",
            run=run,
            notes=list(notes),
        )

    # ----------------------------------------------------------- actions
    def refresh(self) -> Plan:
        cmd = ["pamac", "checkupdates"] if _have("pamac") else ["checkupdates"]
        return self.plan(
            "refresh",
            ["checkupdates"],
            ["pamac", "checkupdates"],
            cmd,
            _("“{typed}” only refreshes the package list. Here the safe equivalent checks for "
              "updates without installing anything; to install them use “{upgrade}”.").format(
                typed=f"{self.tool} {self.words[0] if self.words else 'update'}",
                upgrade="sudo pacman -Syu" if not _have("pamac") else "pamac upgrade",
            ),
            _("Never run “pacman -Sy” alone: refreshing without upgrading can break the system."),
        )

    def upgrade(self) -> Plan:
        notes = []
        if len(self.words) > 1:
            notes.append(_("Arch does not upgrade single packages; the whole system is upgraded together."))
        return self.plan(
            "upgrade",
            ["sudo", "pacman", "-Syu"],
            ["pamac", "upgrade"],
            self._transaction(["-Syu"], ["upgrade"]),
            *notes,
        )

    def install(self, names: list[str], reinstall: bool = False) -> Plan:
        if not names:
            return self.usage_error(_("Tell which package to install, for example: {example}").format(
                example=f"{self.tool} install vlc"))
        debs = [n for n in names if n.endswith(".deb")]
        rpms = [n for n in names if n.endswith(".rpm")]
        local = [n for n in names if ".pkg.tar" in n]
        if debs:
            return self.install_deb(debs)
        if rpms:
            return self.plan(
                "install-rpm", [], None, None,
                _(".rpm packages are made for Fedora/openSUSE and cannot be installed here."),
                _("Look for the program in the repositories (pamac search), on Flathub, or as an AppImage."),
            )
        if local:
            return self.plan(
                "install-file",
                ["sudo", "pacman", "-U", *local],
                ["pamac", "install", *local],
                self._pacman("-U", *local, root=True),
            )
        if reinstall:
            return self.plan(
                "reinstall",
                ["sudo", "pacman", "-S", *names],
                ["pamac", "reinstall", *names],
                self._transaction(["-S", *names], ["reinstall", *names]),
            )
        return self.plan(
            "install",
            ["sudo", "pacman", "-S", *names],
            ["pamac", "install", *names],
            self._transaction(["-S", "--needed", *names], ["install", *names]),
            _("pamac also finds programs in the AUR and Flatpak: {cmd}").format(cmd=f"pamac search {names[0]}"),
        )

    def install_deb(self, debs: list[str]) -> Plan:
        files = [os.path.abspath(d) for d in debs]
        missing = [d for d, f in zip(debs, files, strict=True) if not os.path.isfile(f)]
        if missing:
            return self.usage_error(_("File not found: {path}").format(path=", ".join(missing)))
        run = ["debtap-mod", *files]
        if _is_root():
            user = os.environ.get("SUDO_USER")
            # makepkg refuses to run as root: build as the user who called sudo
            run = ["sudo", "-u", user, *run] if user and user != "root" else None
        notes = [_(".deb packages are converted into native pacman packages by debtap-mod.")]
        if run is None:
            notes.append(_("Run this command as a regular user (without sudo)."))
        return self.plan("install-deb", ["debtap-mod", *debs], None, run, *notes)

    def remove(self, names: list[str], purge: bool = False, deps: bool = False) -> Plan:
        if not names:
            return self.usage_error(_("Tell which package to remove, for example: {example}").format(
                example=f"{self.tool} remove vlc"))
        flags = "-Rns" if purge else ("-Rs" if deps else "-R")
        notes = []
        if purge:
            notes.append(_("-n also deletes the configuration files the package installed in /etc."))
            # pamac has no "purge": pacman does it
            run = self._pacman(flags, *names, root=True)
            return self.plan("purge", ["sudo", "pacman", flags, *names], None, run, *notes)
        pamac = ["remove", *names] + (["--orphans"] if deps else [])
        return self.plan(
            "remove-deps" if deps else "remove",
            ["sudo", "pacman", flags, *names],
            ["pamac", *pamac],
            self._transaction([flags, *names], pamac),
        )

    def autoremove(self) -> Plan:
        names = self.words[1:]
        if names:
            return self.remove(names, deps=True)
        orphans = [] if not _have("pacman") else _orphans()
        if not orphans:
            return self.plan(
                "autoremove", ["sudo", "pacman", "-Rns", "$(pacman -Qdtq)"], ["pamac", "orphans"], None,
                _("There are no orphan packages to remove."),
            )
        return self.plan(
            "autoremove",
            ["sudo", "pacman", "-Rns", "$(pacman -Qdtq)"],
            ["pamac", "orphans"],
            self._pacman("-Rns", *orphans, root=True),
        )

    def search(self, terms: list[str]) -> Plan:
        if not terms:
            return self.usage_error(_("Tell what to search for, for example: {example}").format(
                example=f"{self.tool} search vlc"))
        run = ["pamac", "search", *terms] if _have("pamac") else ["pacman", "-Ss", *terms]
        return self.plan("search", ["pacman", "-Ss", *terms], ["pamac", "search", *terms], run)

    def info(self, names: list[str]) -> Plan:
        if not names:
            return self.usage_error(_("Tell which package, for example: {example}").format(
                example=f"{self.tool} show vlc"))
        installed = all(_installed(n) for n in names)
        flag = "-Qi" if installed else "-Si"
        run = ["pamac", "info", *names] if _have("pamac") else ["pacman", flag, *names]
        return self.plan("info", ["pacman", flag, *names], ["pamac", "info", *names], run)

    def list_all(self, pattern: list[str]) -> Plan:
        if pattern:
            return self.plan("search", ["pacman", "-Ss", *pattern], ["pamac", "search", *pattern],
                             ["pacman", "-Ss", *pattern])
        return self.plan("list-available", ["pacman", "-Sl"], ["pamac", "list", "--repos"], ["pacman", "-Sl"])

    def list_installed(self, pattern: list[str]) -> Plan:
        if pattern:
            return self.plan("list-installed-search", ["pacman", "-Qs", *pattern],
                             ["pamac", "search", "--installed", *pattern], ["pacman", "-Qs", *pattern])
        return self.plan("list-installed", ["pacman", "-Q"], ["pamac", "list"], ["pacman", "-Q"])

    def list_upgradable(self) -> Plan:
        run = ["pamac", "checkupdates"] if _have("pamac") else ["checkupdates"]
        return self.plan("list-upgradable", ["pacman", "-Qu"], ["pamac", "checkupdates"], run)

    def list_manual(self) -> Plan:
        return self.plan("list-manual", ["pacman", "-Qe"], ["pamac", "list", "--explicitly-installed"],
                         ["pacman", "-Qe"])

    def files(self, names: list[str]) -> Plan:
        if not names:
            return self.usage_error(_("Tell which package, for example: {example}").format(
                example=f"{self.tool} -L vlc"))
        if all(_installed(n) for n in names):
            return self.plan("files", ["pacman", "-Ql", *names], ["pamac", "list", "--files", *names],
                             ["pacman", "-Ql", *names])
        return self.plan("files", ["pacman", "-Fl", *names], None, ["pacman", "-Fl", *names],
                         _("The package is not installed; the list comes from the repository file database."))

    def owner(self, paths: list[str]) -> Plan:
        if not paths:
            return self.usage_error(_("Tell which file, for example: {example}").format(
                example=f"{self.tool} -S /usr/bin/vim"))
        existing = [p for p in paths if os.path.exists(p)]
        if len(existing) == len(paths):
            return self.plan("owner", ["pacman", "-Qo", *paths], ["pamac", "search", "--files", *paths],
                             ["pacman", "-Qo", *paths])
        return self.plan("owner", ["pacman", "-F", *paths], None, ["pacman", "-F", *paths],
                         _("The file is not on this system; searching the repository file database."))

    def depends(self, names: list[str], reverse: bool = False) -> Plan:
        if not names:
            return self.usage_error(_("Tell which package, for example: {example}").format(
                example=f"{self.tool} depends vlc"))
        if reverse:
            return self.plan("rdepends", ["pactree", "-r", *names[:1]], ["pamac", "whoneeds", *names[:1]],
                             ["pactree", "-r", names[0]] if _have("pactree") else ["pamac", "whoneeds", names[0]])
        return self.plan("depends", ["pactree", *names[:1]], ["pamac", "info", *names[:1]],
                         ["pactree", "-d1", names[0]] if _have("pactree") else ["pamac", "info", names[0]])

    def clean(self) -> Plan:
        return self.plan("clean", ["sudo", "pacman", "-Sc"], ["pamac", "clean"],
                         self._transaction(["-Sc"], ["clean"]))

    def download(self, names: list[str]) -> Plan:
        return self.plan("download", ["sudo", "pacman", "-Sw", *names], None, self._pacman("-Sw", *names, root=True))

    def group(self, names: list[str], install: bool) -> Plan:
        if install:
            return self.install(names)
        return self.plan("groups", ["pacman", "-Sg", *names], ["pamac", "list", "--groups", *names],
                         ["pacman", "-Sg", *names])

    def repositories(self) -> Plan:
        return self.plan(
            "repositories", [], ["pamac", "build", "<name>"], None,
            _("Arch-based systems have no PPAs or extra repositories to add. Programs from the community "
              "come from the AUR."),
            _("Search with “pamac search --aur <name>” and install with “pamac build <name>”."),
        )

    def hold(self) -> Plan:
        return self.plan(
            "hold", [], None, None,
            _("To keep a package from being upgraded, add it to IgnorePkg in /etc/pacman.conf."),
        )

    def unsupported(self, what: str) -> Plan:
        return self.plan(
            "unsupported", [], None, None,
            _("“{command}” has no direct equivalent here.").format(command=f"{self.tool} {what}".strip()),
            _("Run “{tool} help” to see the commands this system understands.").format(tool=self.tool),
        )

    def usage_error(self, message: str) -> Plan:
        return self.plan("usage", [], None, None, message)

    def help(self) -> Plan:
        topic = self.words[1] if len(self.words) > 1 else ""
        return Plan(tool=self.tool, typed=self.args, action="help", show_help=True, help_topic=topic)

    # ------------------------------------------------------------ parsers
    def translate(self) -> Plan:
        if not self.args or self.flags & {"-h", "--help", "-?"} or (self.words[:1] == ["help"]):
            return self.help()
        handler = {
            "apt": self._apt,
            "apt-get": self._apt,
            "apt-cache": self._apt_cache,
            "apt-mark": self._apt_mark,
            "dpkg": self._dpkg,
            "dnf": self._dnf,
            "yum": self._dnf,
        }[self.tool]
        return handler()

    def _apt(self) -> Plan:
        if not self.words:
            return self.help()
        cmd, rest = self.words[0], self.words[1:]
        purge = "--purge" in self.flags
        if cmd == "update":
            return self.refresh()
        if cmd in ("upgrade", "full-upgrade", "dist-upgrade"):
            return self.upgrade()
        if cmd == "install":
            return self.install(rest, reinstall="--reinstall" in self.flags)
        if cmd == "reinstall":
            return self.install(rest, reinstall=True)
        if cmd == "remove":
            return self.remove(rest, purge=purge)
        if cmd == "purge":
            return self.remove(rest, purge=True)
        if cmd in ("autoremove", "autopurge"):
            return self.autoremove()
        if cmd == "search":
            return self.search(rest)
        if cmd in ("show", "showpkg", "policy"):
            return self.info(rest)
        if cmd == "list":
            if self.flags & {"--installed", "-i"}:
                return self.list_installed(rest)
            if self.flags & {"--upgradable", "--upgradeable", "-u"}:
                return self.list_upgradable()
            if "--manual-installed" in self.flags:
                return self.list_manual()
            return self.list_all(rest)
        if cmd in ("clean", "autoclean"):
            return self.clean()
        if cmd == "depends":
            return self.depends(rest)
        if cmd == "rdepends":
            return self.depends(rest, reverse=True)
        if cmd == "download":
            return self.download(rest)
        if cmd in ("edit-sources", "add-repository"):
            return self.repositories()
        if cmd == "help":
            return self.help()
        return self.unsupported(cmd)

    def _apt_cache(self) -> Plan:
        if not self.words:
            return self.help()
        cmd, rest = self.words[0], self.words[1:]
        if cmd == "search":
            return self.search(rest)
        if cmd in ("show", "showpkg", "policy"):
            return self.info(rest)
        if cmd == "depends":
            return self.depends(rest)
        if cmd == "rdepends":
            return self.depends(rest, reverse=True)
        if cmd == "pkgnames":
            return self.plan("list-available", ["pacman", "-Slq"], None, ["pacman", "-Slq"])
        return self.unsupported(cmd)

    def _apt_mark(self) -> Plan:
        cmd = self.words[0] if self.words else ""
        if cmd == "showmanual":
            return self.list_manual()
        if cmd == "showauto":
            return self.plan("list-auto", ["pacman", "-Qd"], None, ["pacman", "-Qd"])
        if cmd in ("hold", "unhold", "showhold"):
            return self.hold()
        if cmd == "manual":
            return self.plan("mark", ["sudo", "pacman", "-D", "--asexplicit", *self.words[1:]], None,
                             self._pacman("-D", "--asexplicit", *self.words[1:], root=True))
        if cmd == "auto":
            return self.plan("mark", ["sudo", "pacman", "-D", "--asdeps", *self.words[1:]], None,
                             self._pacman("-D", "--asdeps", *self.words[1:], root=True))
        return self.unsupported(cmd)

    def _dpkg(self) -> Plan:
        args = self.args
        opt = args[0] if args else ""
        rest = [a for a in args[1:] if not a.startswith("-")]
        if opt in ("-i", "--install"):
            return self.install_deb(rest) if rest else self.usage_error(
                _("Tell which .deb file to install, for example: {example}").format(example="dpkg -i app.deb"))
        if opt in ("-r", "--remove"):
            return self.remove(rest)
        if opt in ("-P", "--purge"):
            return self.remove(rest, purge=True)
        if opt in ("-l", "--list"):
            return self.list_installed(rest)
        if opt in ("-L", "--listfiles"):
            return self.files(rest)
        if opt in ("-S", "--search"):
            return self.owner(rest)
        if opt in ("-s", "--status", "-p", "--print-avail"):
            return self.info(rest)
        if opt == "--get-selections":
            return self.list_installed([])
        if opt in ("--print-architecture", "--compare-versions"):
            # used by installer scripts: answer silently, exactly like dpkg
            return Plan(tool=self.tool, typed=self.args, action="silent")
        return self.unsupported(opt)

    def _dnf(self) -> Plan:
        if not self.words:
            return self.help()
        cmd, rest = self.words[0], self.words[1:]
        if cmd in ("check-update", "check-upgrade", "makecache"):
            return self.refresh()
        if cmd in ("upgrade", "update", "distro-sync", "dsync", "upgrade-minimal", "update-minimal"):
            return self.upgrade()
        if cmd in ("install", "localinstall", "in"):
            return self.install(rest)
        if cmd in ("reinstall", "rei"):
            return self.install(rest, reinstall=True)
        if cmd in ("remove", "erase", "rm"):
            return self.remove(rest)
        if cmd == "autoremove":
            return self.autoremove()
        if cmd in ("search", "se"):
            return self.search(rest)
        if cmd in ("info", "if"):
            return self.info(rest)
        if cmd in ("list", "ls"):
            scope = rest[0] if rest else ""
            pattern = rest[1:] if scope in ("installed", "available", "updates", "upgrades", "all") else rest
            if scope == "installed" or "--installed" in self.flags:
                return self.list_installed(pattern)
            if scope in ("updates", "upgrades") or "--upgrades" in self.flags:
                return self.list_upgradable()
            return self.list_all(pattern)
        if cmd in ("provides", "whatprovides", "wp"):
            return self.owner(rest)
        if cmd == "repoquery":
            if self.flags & {"-l", "--list"}:
                return self.files(rest)
            if "--userinstalled" in self.flags:
                return self.list_manual()
            return self.info(rest)
        if cmd == "history" and rest[:1] == ["userinstalled"]:
            return self.list_manual()
        if cmd == "clean":
            return self.clean()
        if cmd == "download":
            return self.download(rest)
        if cmd in ("group", "groups", "groupinstall", "grouplist"):
            sub = rest[0] if rest and cmd in ("group", "groups") else ""
            names = rest[1:] if sub else rest
            return self.group(names, install=sub == "install" or cmd == "groupinstall")
        if cmd in ("copr", "config-manager"):
            return self.repositories()
        if cmd in ("versionlock",):
            return self.hold()
        return self.unsupported(cmd)


def _installed(name: str) -> bool:
    import subprocess

    return subprocess.run(["pacman", "-Qq", name], capture_output=True).returncode == 0


def _orphans() -> list[str]:
    import subprocess

    result = subprocess.run(["pacman", "-Qdtq"], capture_output=True, text=True)
    return result.stdout.split()


DEBIAN_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf", "i686": "i386", "riscv64": "riscv64"}

_VERCMP_OPS = {
    "lt": lambda r: r < 0, "<<": lambda r: r < 0, "le": lambda r: r <= 0, "<=": lambda r: r <= 0,
    "eq": lambda r: r == 0, "=": lambda r: r == 0, "ne": lambda r: r != 0,
    "ge": lambda r: r >= 0, ">=": lambda r: r >= 0, "gt": lambda r: r > 0, ">>": lambda r: r > 0,
}


def dpkg_silent(args: list[str]) -> int:
    """dpkg --print-architecture / --compare-versions, as scripts expect."""
    import platform

    from .version import to_pacman_version

    if args[0] == "--print-architecture":
        machine = platform.machine()
        print(DEBIAN_ARCH.get(machine, machine))
        return 0
    if len(args) != 4 or args[2] not in _VERCMP_OPS:
        print("dpkg: error: --compare-versions takes three arguments: <version> <relation> <version>", file=sys.stderr)
        return 2
    import subprocess

    def pacman_form(version: str) -> str:
        if not version:
            return "0"
        v = to_pacman_version(version)
        # always pass the epoch so "1:1.0" is compared correctly against "2.0"
        return f"{v.epoch}:{v.pkgver}-{v.pkgrel}"

    result = subprocess.run(["vercmp", pacman_form(args[1]), pacman_form(args[3])], capture_output=True, text=True)
    return 0 if _VERCMP_OPS[args[2]](int(result.stdout.strip() or 0)) else 1


def translate(tool: str, args: list[str]) -> Plan:
    return Translator(tool, args).translate()


# ------------------------------------------------------------- rendering
class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"


BOLD, DIM, ITALIC = "1", "2", "3"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "31", "32", "33", "34", "35", "36"
FOREIGN_COLOR = {"apt": RED, "apt-get": RED, "apt-cache": RED, "apt-mark": RED, "dpkg": RED, "dnf": MAGENTA, "yum": MAGENTA}


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


def render_plan(plan: Plan, style: Style) -> str:
    foreign = FOREIGN_COLOR.get(plan.tool, RED)
    lines = []
    family = "apt" if plan.tool.startswith("apt") else plan.tool
    lines.append(
        style(f" {family} → pacman ", BOLD, "97", "44")
        + "  "
        + style(_("This system uses pacman, not {tool}.").format(tool=plan.tool), BOLD)
    )
    width = max(len(_("You typed:")), len(_("Equivalent:")), len(_("With pamac:")))
    lines.append(f"   {_('You typed:'):<{width}}  " + style(plan.typed_line, foreign, BOLD))
    if plan.pacman:
        lines.append(f"   {_('Equivalent:'):<{width}}  " + style(plan.pacman, BLUE, BOLD))
    if plan.pamac:
        lines.append(f"   {_('With pamac:'):<{width}}  " + style(plan.pamac, GREEN, BOLD))
    columns = shutil.get_terminal_size((100, 24)).columns
    for note in plan.notes:
        wrapped = textwrap.wrap(note, width=max(40, columns - 6)) or [""]
        lines.append("   " + style("•", YELLOW, BOLD) + " " + wrapped[0])
        lines.extend("     " + part for part in wrapped[1:])
    if plan.run:
        lines.append("")
        lines.append(" " + style(_("Running:"), DIM) + " " + style(" ".join(plan.run), BOLD))
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------- help
def help_rows() -> list[tuple[str, str, str, str, str, tuple[str, ...]]]:
    """(task, apt, dnf, pacman, pamac, keywords)."""
    return [
        (_("Update everything"), "sudo apt update && sudo apt upgrade", "sudo dnf upgrade",
         "sudo pacman -Syu", "pamac upgrade", ("upgrade", "update", "full-upgrade", "dist-upgrade")),
        (_("Check for updates"), "sudo apt update", "dnf check-update",
         "checkupdates", "pamac checkupdates", ("update", "check-update", "makecache")),
        (_("Install a program"), "sudo apt install vlc", "sudo dnf install vlc",
         "sudo pacman -S vlc", "pamac install vlc", ("install",)),
        (_("Install a downloaded file"), "sudo apt install ./app.deb", "sudo dnf install ./app.rpm",
         "sudo pacman -U app.pkg.tar.zst", "—", ("install", "-i", "localinstall", "file")),
        (_("Install a .deb file here"), "sudo dpkg -i app.deb", "—",
         "debtap-mod app.deb", "—", ("install", "deb", "-i", "dpkg")),
        (_("Reinstall"), "sudo apt reinstall vlc", "sudo dnf reinstall vlc",
         "sudo pacman -S vlc", "pamac reinstall vlc", ("reinstall",)),
        (_("Remove a program"), "sudo apt remove vlc", "sudo dnf remove vlc",
         "sudo pacman -R vlc", "pamac remove vlc", ("remove", "erase", "-r")),
        (_("Remove + dependencies"), "sudo apt autoremove vlc", "sudo dnf remove vlc",
         "sudo pacman -Rs vlc", "pamac remove -o vlc", ("autoremove", "remove")),
        (_("Remove + configuration"), "sudo apt purge vlc", "—",
         "sudo pacman -Rns vlc", "—", ("purge", "-P")),
        (_("Remove orphans (unused)"), "sudo apt autoremove", "sudo dnf autoremove",
         "sudo pacman -Rns $(pacman -Qdtq)", "pamac orphans", ("autoremove", "orphans")),
        (_("Search to install"), "apt search vlc", "dnf search vlc",
         "pacman -Ss vlc", "pamac search vlc", ("search",)),
        (_("Details (available)"), "apt show vlc", "dnf info vlc",
         "pacman -Si vlc", "pamac info vlc", ("show", "info", "policy")),
        (_("Details (installed)"), "dpkg -s vlc", "dnf info --installed vlc",
         "pacman -Qi vlc", "pamac info vlc", ("show", "info", "-s", "status")),
        (_("List everything available"), "apt list", "dnf list available",
         "pacman -Sl", "pamac list --repos", ("list",)),
        (_("List installed"), "apt list --installed", "dnf list installed",
         "pacman -Q", "pamac list", ("list", "installed", "-l")),
        (_("Search installed"), "apt list --installed vlc", "dnf list installed vlc",
         "pacman -Qs vlc", "pamac search -i vlc", ("list", "installed")),
        (_("Files of a package"), "dpkg -L vlc", "dnf repoquery -l vlc",
         "pacman -Ql vlc", "pamac list -f vlc", ("files", "-L", "repoquery")),
        (_("Which package owns a file?"), "dpkg -S /usr/bin/vim", "dnf provides /usr/bin/vim",
         "pacman -Qo /usr/bin/vim", "pamac search -f /usr/bin/vim", ("owner", "-S", "provides")),
        (_("What can be upgraded"), "apt list --upgradable", "dnf check-update",
         "pacman -Qu", "pamac checkupdates", ("upgradable", "check-update", "list")),
        (_("Installed by you"), "apt-mark showmanual", "dnf history userinstalled",
         "pacman -Qe", "pamac list -e", ("showmanual", "userinstalled", "apt-mark")),
        (_("Dependencies of a package"), "apt-cache depends vlc", "dnf repoquery --requires vlc",
         "pactree vlc", "pamac info vlc", ("depends",)),
        (_("Who depends on a package"), "apt-cache rdepends vlc", "dnf repoquery --whatrequires vlc",
         "pactree -r vlc", "pamac whoneeds vlc", ("rdepends", "whoneeds")),
        (_("Clean the download cache"), "sudo apt clean", "sudo dnf clean all",
         "sudo pacman -Sc", "pamac clean", ("clean", "autoclean")),
        (_("Add a repository / PPA"), "sudo add-apt-repository …", "sudo dnf copr enable …",
         _("(AUR)"), "pamac build <name>", ("ppa", "repository", "copr", "edit-sources")),
    ]


def _visible_len(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def _table(out, style, headers, table, widths, colors, gap) -> None:
    out.append(" " + gap.join(style(_pad(h, w), BOLD, "4") for h, w in zip(headers, widths, strict=True)))
    for i, row in enumerate(table):
        # zebra stripes make long rows easier to follow
        stripe = ("48;5;236",) if i % 2 else ()
        cells = []
        for cell, w, color in zip(row, widths, colors, strict=True):
            codes = (DIM,) if cell == "—" else (color,)
            cells.append(style(_pad(cell, w), *codes, *stripe))
        out.append(" " + style(gap, *stripe).join(cells))


def render_help(tool: str, topic: str, style: Style, columns: int | None = None) -> str:
    columns = columns or shutil.get_terminal_size((100, 24)).columns
    family = "dnf" if tool in ("dnf", "yum") else "apt"
    foreign = FOREIGN_COLOR.get(tool, RED)
    rows = help_rows()
    if topic:
        key = topic.lower()
        rows = [r for r in rows if any(key == k or key in k for k in r[5]) or key in r[0].lower()]

    out = []
    out.append(style(f" {family} × pacman ", BOLD, "97", "44"))
    out.append(style(_("Command translator: {family} (Debian, Ubuntu, Mint, Fedora) to pacman and pamac (Arch, BigLinux, Manjaro)").format(
        family="apt / dnf"), DIM))
    out.append("")
    if not topic:
        out.append(_("Both do the same job: install, update and remove programs. Only the commands change."))
        out.append(_("You can keep typing {tool} commands: they are translated and run with pamac or pacman.").format(
            tool=style(tool, foreign, BOLD)))
        out.append("")
    if not rows:
        out.append(_("No command matches “{topic}”.").format(topic=topic))
        out.append(_("Run “{tool} help” to see every command.").format(tool=tool))
        return "\n".join(out) + "\n"

    src = 1 if family == "apt" else 2
    headers = (_("What you want to do"), family, "pacman", "pamac")
    table = [(r[0], r[src], r[3], r[4]) for r in rows]
    widths = [max(_visible_len(h), *(_visible_len(t[i]) for t in table)) for i, h in enumerate(headers)]
    colors = (BOLD, foreign, BLUE, GREEN)
    gap = "  "
    total = sum(widths) + len(gap) * 3 + 2

    if total <= columns:
        _table(out, style, headers, table, widths, colors, gap)
    elif sum(widths[:3]) + len(gap) * 2 + 2 <= columns:
        # medium terminal: drop the pamac column, it is shown in the panels anyway
        _table(out, style, headers[:3], [t[:3] for t in table], widths[:3], colors[:3], gap)
    else:
        # narrow terminal: one block per task
        label_w = max(len(family), len("pacman"), len("pamac"))
        for task, foreign_cmd, pacman_cmd, pamac_cmd in table:
            out.append(" " + style(task, BOLD))
            out.append(f"   {style(_pad(family, label_w), DIM)}  {style(foreign_cmd, foreign)}")
            out.append(f"   {style(_pad('pacman', label_w), DIM)}  {style(pacman_cmd, BLUE)}")
            if pamac_cmd != "—":
                out.append(f"   {style(_pad('pamac', label_w), DIM)}  {style(pamac_cmd, GREEN)}")
    out.append("")

    if not topic:
        boxes = [
            (_("Differences that catch people"), RED, [
                _("apt update only refreshes the list; apt upgrade installs."),
                _("In pacman, -Syu does both together."),
                _("Avoid “pacman -Sy” alone: it can break the system."),
            ]),
            (_("Package types"), BLUE, [
                _("apt uses .deb files, dnf uses .rpm, pacman uses .pkg.tar.zst."),
                _(".deb files are converted and installed with debtap-mod (or a double click)."),
            ]),
            (_("Programs from the community"), YELLOW, [
                _("The AUR plays the role of PPAs and COPR."),
                _("Use “pamac build <name>”; never run AUR helpers with sudo."),
            ]),
        ]
        for title, color, items in boxes:
            out.append(" " + style("▍", color, BOLD) + style(title, color, BOLD))
            for item in items:
                out.append(" " + style("▍", color) + item)
            out.append("")
        out.append(style(_("Tip: “{tool} help install” shows only the lines about one command.").format(tool=tool), DIM))
        out.append(style(_("Quick reference: “sudo” is needed to install or remove; searching and listing are not."), DIM))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------- main
def _real_binary(tool: str) -> str | None:
    """A genuine apt/dnf/dpkg installed by the user takes precedence."""
    for directory in ("/usr/bin", "/bin"):
        candidate = os.path.join(directory, tool)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    tool = os.path.basename(argv[0]) if argv else "apt"
    if tool not in FOREIGN_TOOLS:
        # called as "python -m debtap_mod.pkgcompat apt install vlc"
        tool, argv = (argv[1], argv[1:]) if len(argv) > 1 and argv[1] in FOREIGN_TOOLS else ("apt", ["apt"])
    args = argv[1:]

    real = _real_binary(tool)
    if real and not os.environ.get("DEBTAP_COMPAT_FORCE"):
        os.execv(real, [real, *args])

    style = Style(_color_enabled(sys.stdout))
    plan = translate(tool, args)
    if plan.action == "silent":
        return dpkg_silent(args)
    if plan.show_help:
        sys.stdout.write(render_help(tool, plan.help_topic, style))
        return 0
    err_style = Style(_color_enabled(sys.stderr))
    sys.stderr.write(render_plan(plan, err_style))
    sys.stderr.flush()
    if not plan.run:
        return 0 if plan.action not in ("usage", "unsupported", "install-rpm") else 1
    if os.environ.get("DEBTAP_COMPAT_DRY_RUN"):
        return 0
    try:
        os.execvp(plan.run[0], plan.run)
    except FileNotFoundError:
        sys.stderr.write(err_style(_("Command not found: {cmd}").format(cmd=plan.run[0]), RED, BOLD) + "\n")
        return 127


if __name__ == "__main__":
    sys.exit(main())
