"""Turn Debian maintainer scripts into a pacman .install file.

The scripts are embedded verbatim and executed with the same arguments dpkg
would use. A temporary directory with small replacements for Debian-only
tools (dpkg, debconf, deb-systemd-helper, adduser, ...) is put first in PATH
so ``set -e`` scripts do not abort on a missing command.
"""

import re
import secrets
from dataclasses import dataclass, field

from .i18n import _

# Shell snippet that writes the compatibility tools into "$1".
SHIMS = r'''
_debtap_shims() {
    d=$1
    for t in dpkg-maintscript-helper dpkg-divert dpkg-trigger dpkg-statoverride \
             update-alternatives invoke-rc.d update-rc.d apt-key apt-get apt \
             update-menus update-mime install-docs install-info-wrapper \
             update-icon-caches lintian; do
        printf '#!/bin/sh\nexit 0\n' > "$d/$t"
    done
    cat > "$d/dpkg" <<'DEBTAP_SHIM'
#!/bin/sh
case "$1" in
    --compare-versions)
        r=$(vercmp "$2" "$4")
        case "$3" in
            lt|"<<") [ "$r" -lt 0 ] ;;
            le|"<=") [ "$r" -le 0 ] ;;
            eq|"=")  [ "$r" -eq 0 ] ;;
            ne)      [ "$r" -ne 0 ] ;;
            ge|">=") [ "$r" -ge 0 ] ;;
            gt|">>") [ "$r" -gt 0 ] ;;
            *) exit 1 ;;
        esac ;;
    --print-architecture)
        case "$(uname -m)" in
            x86_64) echo amd64 ;; aarch64) echo arm64 ;; armv7l) echo armhf ;;
            i?86) echo i386 ;; *) uname -m ;;
        esac ;;
    -L|--listfiles) pacman -Qlq "$2" 2>/dev/null ;;
    -l|--list|-s|--status|-S|--search) exit 1 ;;
    *) exit 0 ;;
esac
DEBTAP_SHIM
    cat > "$d/dpkg-query" <<'DEBTAP_SHIM'
#!/bin/sh
exit 1
DEBTAP_SHIM
    cat > "$d/deb-systemd-helper" <<'DEBTAP_SHIM'
#!/bin/sh
while [ "${1#-}" != "$1" ]; do shift; done
action=$1; shift
[ -d /run/systemd/system ] && [ "$(id -u)" = 0 ] || exit 0
case "$action" in
    enable) systemctl enable "$@" >/dev/null 2>&1 || true ;;
    disable|purge|mask) systemctl disable "$@" >/dev/null 2>&1 || true ;;
    unmask) systemctl unmask "$@" >/dev/null 2>&1 || true ;;
    was-enabled|debian-installed|update-state) exit 0 ;;
esac
exit 0
DEBTAP_SHIM
    cat > "$d/deb-systemd-invoke" <<'DEBTAP_SHIM'
#!/bin/sh
while [ "${1#-}" != "$1" ]; do shift; done
[ -d /run/systemd/system ] && [ "$(id -u)" = 0 ] || exit 0
systemctl "$@" >/dev/null 2>&1 || true
exit 0
DEBTAP_SHIM
    cat > "$d/adduser" <<'DEBTAP_SHIM'
#!/bin/sh
system=; group=; home=; shell=; ingroup=; nohome=1; args=
while [ $# -gt 0 ]; do
    case "$1" in
        --system) system=1 ;;
        --group) group=1 ;;
        --home) home=$2; shift ;;
        --shell) shell=$2; shift ;;
        --ingroup) ingroup=$2; shift ;;
        --gecos|--uid|--gid|--firstuid|--lastuid|--conf) shift ;;
        --create-home) nohome= ;;
        --*) ;;
        *) args="$args $1" ;;
    esac
    shift
done
set -- $args
[ $# -eq 0 ] && exit 0
if [ $# -ge 2 ]; then gpasswd -a "$1" "$2" >/dev/null 2>&1; exit 0; fi
getent passwd "$1" >/dev/null && exit 0
opts=
[ -n "$system" ] && opts="$opts -r"
[ -n "$group" ] && opts="$opts -U"
[ -n "$ingroup" ] && opts="$opts -g $ingroup"
[ -n "$home" ] && opts="$opts -d $home"
[ -n "$nohome" ] && opts="$opts -M" || opts="$opts -m"
opts="$opts -s ${shell:-/usr/bin/nologin}"
useradd $opts "$1" >/dev/null 2>&1 || true
exit 0
DEBTAP_SHIM
    cat > "$d/addgroup" <<'DEBTAP_SHIM'
#!/bin/sh
system=; args=
for a in "$@"; do case "$a" in --system) system=1 ;; --*) ;; *) args="$args $a" ;; esac; done
set -- $args
[ $# -eq 0 ] && exit 0
if [ $# -ge 2 ]; then gpasswd -a "$1" "$2" >/dev/null 2>&1; exit 0; fi
getent group "$1" >/dev/null && exit 0
groupadd ${system:+-r} "$1" >/dev/null 2>&1 || true
exit 0
DEBTAP_SHIM
    cat > "$d/deluser" <<'DEBTAP_SHIM'
#!/bin/sh
for a in "$@"; do case "$a" in --*) ;; *) userdel "$a" >/dev/null 2>&1; exit 0 ;; esac; done
DEBTAP_SHIM
    cat > "$d/delgroup" <<'DEBTAP_SHIM'
#!/bin/sh
for a in "$@"; do case "$a" in --*) ;; *) groupdel "$a" >/dev/null 2>&1; exit 0 ;; esac; done
DEBTAP_SHIM
    cat > "$d/confmodule" <<'DEBTAP_SHIM'
# debconf replacement: every question is answered with its default
db_get() { RET=""; return 0; }
db_metaget() { RET=""; return 0; }
for _f in db_input db_go db_set db_fset db_reset db_register db_unregister \
          db_subst db_clear db_title db_beginblock db_endblock db_capb \
          db_purge db_stop db_version db_settitle db_progress db_info db_x_loadtemplatefile; do
    eval "$_f() { return 0; }"
done
DEBTAP_SHIM
    chmod 755 "$d"/*
}

_debtap_run() {
    script=$1; shift
    tmp=$(mktemp -d /run/debtap-mod.XXXXXX 2>/dev/null || mktemp -d /tmp/debtap-mod.XXXXXX) || return 0
    _debtap_shims "$tmp"
    cat > "$tmp/script"
    chmod 755 "$tmp/script"
    apt_before=0; [ -e /etc/apt ] && apt_before=1
    keyrings_before=0; [ -e /usr/share/keyrings ] && keyrings_before=1
    PATH="$tmp:$PATH" DEBTAP_SHIM="$tmp" DEBIAN_FRONTEND=noninteractive \
        DPKG_MAINTSCRIPT_NAME="$script" DPKG_MAINTSCRIPT_PACKAGE="@DEBNAME@" \
        DPKG_MAINTSCRIPT_ARCH="@DEBARCH@" $(sed -n '1s/^#!//p' "$tmp/script") "$tmp/script" "$@"
    status=$?
    [ $status -ne 0 ] && echo "debtap-mod: the Debian $script script exited with status $status" >&2
    # APT repositories and keys are meaningless on pacman systems
    [ $apt_before = 0 ] && rm -rf /etc/apt
    [ $keyrings_before = 0 ] && rm -rf /usr/share/keyrings
    rm -rf "$tmp"
    return 0
}
'''


SCRIPTS_MARKER = "# ---- maintainer scripts ----"


@dataclass
class InstallScript:
    text: str | None
    scripts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_SHEBANG_FIX = {
    "/bin/dash": "/bin/sh",
    "/usr/bin/dash": "/bin/sh",
}


def _prepare(body: str, notes: list[str], name: str) -> str:
    lines = body.splitlines()
    if not lines or not lines[0].startswith("#!"):
        lines.insert(0, "#!/bin/sh")
    else:
        interpreter = lines[0][2:].strip().split()
        if interpreter and interpreter[0] in _SHEBANG_FIX:
            lines[0] = "#!" + " ".join([_SHEBANG_FIX[interpreter[0]], *interpreter[1:]])
    text = "\n".join(lines) + "\n"
    text = re.sub(r"/usr/share/debconf/confmodule\b", "${DEBTAP_SHIM}/confmodule", text)
    if "/etc/apt/" in text or "apt-key" in text:
        notes.append(_("The {script} script configures an APT repository; that part has no effect on this system.").format(script=name))
    return text


def _delimiter(*bodies: str) -> str:
    while True:
        token = "DEBTAP_EOF_" + secrets.token_hex(4).upper()
        if not any(token in body for body in bodies):
            return token


def build_install(
    scripts: dict[str, str],
    debname: str,
    debarch: str,
    extra_post_install: str = "",
) -> InstallScript:
    """Return the .install contents, or None when nothing needs to run."""
    notes: list[str] = []
    prepared = {name: _prepare(body, notes, name) for name, body in scripts.items() if body and body.strip()}
    if not prepared and not extra_post_install.strip():
        return InstallScript(text=None)

    delim = _delimiter(*prepared.values(), extra_post_install)

    def call(name: str, *args: str) -> str:
        quoted = " ".join(args)
        return f"    _debtap_run {name} {quoted} <<'{delim}'\n{prepared[name]}{delim}\n"

    functions: list[str] = []

    def add(func: str, body: str) -> None:
        if body.strip():
            functions.append(f"{func}() {{\n{body}}}\n")

    extra = ""
    if extra_post_install.strip():
        extra = "".join(f"    {line}\n" for line in extra_post_install.strip().splitlines())

    add("pre_install", call("preinst", "install") if "preinst" in prepared else "")
    add("post_install", (call("postinst", "configure", "''") if "postinst" in prepared else "") + extra)
    add("pre_upgrade", call("preinst", "upgrade", '"$2"') if "preinst" in prepared else "")
    add("post_upgrade", (call("postinst", "configure", '"$2"') if "postinst" in prepared else "") + extra)
    add("pre_remove", call("prerm", "remove") if "prerm" in prepared else "")
    add("post_remove", call("postrm", "remove") if "postrm" in prepared else "")

    header = (
        f"# Generated by debtap-mod from the maintainer scripts of {debname}.\n"
        "# The original Debian scripts are kept verbatim below.\n"
    )
    shims = SHIMS.replace("@DEBNAME@", debname).replace("@DEBARCH@", debarch)
    text = header + shims + "\n" + SCRIPTS_MARKER + "\n\n" + "\n".join(functions)
    return InstallScript(text=text, scripts=sorted(prepared), notes=notes)
