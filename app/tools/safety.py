"""Decide whether a Boss shell command is dangerous and needs the user's approval.

classify_command() splits a command line into simple commands (on && || ; | &,
newlines, ( ) { }, $( ), backticks and <( )), unwraps shells such as
`bash -c "..."`, `cmd /c ...` and `powershell -Command ...`, and checks each
simple command against the rules below. Anything it can't parse is dangerous.

Commands that only touch the project folder (build, test, run, lint, installs
into a project venv) are not dangerous. The check is a best-effort text
analysis: it can't see what scripts or inline code (python -c) do when run.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

OsKind = Literal["windows", "macos", "linux"]
PathStatus = Literal["inside", "root", "outside", "unknown"]

MAX_DEPTH = 8  # nested shells / substitutions before giving up


@dataclass(frozen=True)
class SafetyResult:
    dangerous: bool
    reason: str


class _ParseError(Exception):
    pass


# --- Rule tables (also used to describe the rules) ------------------------------

# Always dangerous, whatever the arguments.
ALWAYS_DANGEROUS: dict[str, str] = {
    # privileges
    **dict.fromkeys(("sudo", "su", "doas", "runas", "pkexec", "gsudo"), "runs with elevated privileges"),
    # disks and filesystems
    **dict.fromkeys(
        ("dd", "mkfs", "fdisk", "sfdisk", "cfdisk", "gdisk", "sgdisk", "parted", "wipefs", "diskpart",
         "format", "format-volume", "clear-disk", "initialize-disk", "remove-partition", "new-partition",
         "mount", "umount", "swapon", "swapoff", "losetup", "cryptsetup", "shred", "srm", "sdelete", "cipher"),
        "can damage disks or filesystems",
    ),
    # power and processes
    **dict.fromkeys(
        ("shutdown", "reboot", "halt", "poweroff", "init", "telinit", "logoff", "stop-computer",
         "restart-computer"),
        "shuts down or restarts the computer",
    ),
    **dict.fromkeys(
        ("kill", "killall", "pkill", "skill", "xkill", "taskkill", "tskill", "stop-process", "spps"),
        "stops processes outside WorkLoop's control",
    ),
    # network and remote execution
    **dict.fromkeys(
        ("ssh", "scp", "sftp", "ftp", "tftp", "telnet", "nc", "ncat", "netcat", "socat", "mstsc", "psexec",
         "psexec64", "winrs", "enter-pssession", "new-pssession", "invoke-command"),
        "connects to or runs commands on another machine",
    ),
    **dict.fromkeys(
        ("iex", "invoke-expression", "eval"), "evaluates code built at runtime (can't be checked)"
    ),
    **dict.fromkeys(
        ("certutil", "bitsadmin", "mshta", "regsvr32", "rundll32", "wscript", "cscript", "installutil",
         "wmic"),
        "Windows system tool often used to download or run code",
    ),
    # registry, environment, system config
    **dict.fromkeys(("regedit", "regini", "setx", "set-executionpolicy"), "changes system settings"),
    **dict.fromkeys(("crontab", "at", "schtasks", "register-scheduledtask", "unregister-scheduledtask"),
                    "changes scheduled tasks"),
    **dict.fromkeys(
        ("useradd", "userdel", "usermod", "groupadd", "groupdel", "passwd", "chpasswd", "visudo", "chsh",
         "new-localuser", "remove-localuser", "set-localuser", "add-localgroupmember"),
        "changes user accounts",
    ),
    **dict.fromkeys(
        ("iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "netsh", "new-netfirewallrule",
         "set-netfirewallrule", "remove-netfirewallrule", "set-netfirewallprofile", "route"),
        "changes firewall or network settings",
    ),
    **dict.fromkeys(("set-mppreference", "add-mppreference", "remove-mppreference"),
                    "changes antivirus settings"),
    **dict.fromkeys(("bcdedit", "grub-install", "update-grub", "efibootmgr"), "changes boot settings"),
    **dict.fromkeys(("vssadmin", "wbadmin"), "changes system backups or shadow copies"),
    **dict.fromkeys(("modprobe", "insmod", "rmmod", "sysctl", "chattr"), "changes kernel or file attributes"),
    **dict.fromkeys(("icacls", "cacls", "takeown", "set-acl"), "changes file permissions or ownership"),
    **dict.fromkeys(("launchctl", "service", "new-service", "remove-service", "set-service", "stop-service",
                     "start-service", "restart-service", "suspend-service"), "controls system services"),
    **dict.fromkeys(("pipx", "msiexec", "install-module", "install-package", "uninstall-module",
                     "uninstall-package", "add-appxpackage", "remove-appxpackage", "install-script"),
                    "installs or removes software system-wide"),
    **dict.fromkeys(("conda", "mamba", "micromamba"), "changes a conda environment outside the project"),
}

# System package managers: dangerous unless the first argument is read-only.
PACKAGE_MANAGERS = frozenset(
    {"apt", "apt-get", "aptitude", "dpkg", "rpm", "yum", "dnf", "zypper", "pacman", "apk", "emerge", "snap",
     "flatpak", "brew", "port", "choco", "winget", "scoop", "nix-env", "pkg", "softwareupdate"}
)
PACKAGE_READ_ONLY = frozenset(
    {"list", "search", "show", "info", "policy", "outdated", "doctor", "--version", "-v", "-V", "version",
     "-l", "-q", "--query", "-qa", "query", "status", "config", "--help", "-h", "help"}
)

# Delete commands: dangerous if a target is outside the project, is the project folder itself,
# or can't be determined (variables, pipeline input).
DELETE_COMMANDS = frozenset({"rm", "rmdir", "unlink", "del", "erase", "rd", "remove-item", "ri", "trash"})

# Commands that write files: dangerous if a target is outside the project or unknown.
WRITE_COMMANDS = frozenset(
    {"tee", "touch", "mkdir", "md", "truncate", "ln", "mklink", "install", "chmod", "chown", "chgrp",
     "mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni",
     "set-content", "add-content", "out-file", "clear-content", "clc", "new-item", "ni",
     "set-item", "new-itemproperty", "set-itemproperty", "remove-itemproperty", "rp", "sp"}
)
# Copy commands: only the destination (last path argument) must be inside the project.
COPY_COMMANDS = frozenset({"cp", "copy", "xcopy", "robocopy", "copy-item", "cpi"})

# cmd.exe-style tools where "/x" is a switch, not a path.
SLASH_SWITCH_COMMANDS = frozenset(
    {"del", "erase", "rd", "rmdir", "md", "copy", "xcopy", "robocopy", "move", "ren", "rename", "mklink",
     "reg", "sc", "attrib", "icacls", "dir"}
)

SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "ash", "busybox"})
POWERSHELLS = frozenset({"powershell", "pwsh"})
# Prefix commands that run the rest of the line as a command.
WRAPPERS = frozenset({"env", "nice", "nohup", "time", "command", "builtin", "exec", "stdbuf", "ionice",
                      "caffeinate", "xargs", "timeout", "start", "start-process", "saps", "call"})

DOWNLOADERS = r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|fetch|aria2c)"
INTERPRETERS = (
    r"(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|py|perl|ruby|node|deno|bun|php"
    r"|pwsh|powershell|iex|invoke-expression|cmd)"
)

# Patterns checked against the whole command line.
WHOLE_LINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # `curl URL | sh` / `iwr URL | iex`: the interpreter runs whatever comes down the pipe.
    # `curl URL | python -m json.tool` passes a script argument, so it is not matched.
    (re.compile(rf"\b{DOWNLOADERS}\b[^\n]*\|\s*(?:sudo\s+)?(?:\S*[/\\])?{INTERPRETERS}"
                r"(?:\s+-[a-z-]*)*\s*(?:$|[|;&)\n]|--)", re.IGNORECASE),
     "downloads code and runs it (download piped to a shell)"),
    (re.compile(rf"\b{INTERPRETERS}\b[^\n]*<\(\s*{DOWNLOADERS}\b", re.IGNORECASE),
     "downloads code and runs it (process substitution)"),
    (re.compile(rf"\b{INTERPRETERS}\s+(?:-c\s+)?[\"']?\$\(\s*{DOWNLOADERS}\b", re.IGNORECASE),
     "downloads code and runs it (command substitution)"),
    (re.compile(r"downloadstring|downloadfile|net\.webclient|frombase64string", re.IGNORECASE),
     "downloads or decodes code at runtime"),
    (re.compile(r"\[\s*(?:system\.)?environment\s*\]\s*::\s*setenvironmentvariable", re.IGNORECASE),
     "changes system or user environment variables"),
    (re.compile(r"\b(?:hklm|hkcu|hkcr|hku|hkcc|hkey_[a-z_]+)\s*[:\\]", re.IGNORECASE), "touches the Windows registry"),
    (re.compile(r"registry::", re.IGNORECASE), "touches the Windows registry"),
    # fork bombs: :(){ :|:& };:  and  name(){ name|name& }
    (re.compile(r"(\w+|:)\s*\(\s*\)\s*\{[^}]*\1\s*\|\s*\1"), "fork bomb"),
    (re.compile(r"%0\s*\|\s*%0"), "fork bomb"),
    (re.compile(r"\bwhile\s*\(?\s*(?:true|1|\$true)\s*\)?\s*[;{]?[^\n]*\b(?:fork|start-process|start)\b",
                re.IGNORECASE),
     "possible fork bomb (endless loop starting processes)"),
    (re.compile(r"/dev/(?:sd[a-z]|nvme\d|hd[a-z]|disk\d|mmcblk\d)", re.IGNORECASE), "writes to a raw disk device"),
    (re.compile(r"\\\\\.\\physicaldrive", re.IGNORECASE), "writes to a raw disk device"),
]

SENSITIVE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[/\\])\.(bashrc|bash_profile|zshrc|zprofile|profile|login|cshrc|tcshrc)$", re.IGNORECASE),
     "a shell startup file"),
    (re.compile(r"(^|[/\\])\.ssh([/\\]|$)", re.IGNORECASE), "SSH keys or config"),
    (re.compile(r"^/etc([/\\]|$)", re.IGNORECASE), "system configuration in /etc"),
    (re.compile(r"\$profile", re.IGNORECASE), "the PowerShell profile"),
    (re.compile(r"(^|[/\\])(windows|system32)([/\\]|$)", re.IGNORECASE), "a Windows system folder"),
]


# --- OS helpers -----------------------------------------------------------------


def _os_kind(os_name: str) -> OsKind:
    name = os_name.lower()
    if name.startswith("win") or name == "nt":
        return "windows"
    if name in ("darwin", "macos", "mac", "osx"):
        return "macos"
    return "linux"


def _current_os() -> OsKind:
    return _os_kind(sys.platform)


@dataclass
class _Ctx:
    os: OsKind
    root: str  # normalized project root
    cwd: str | None  # None = unknown (after `cd $X`)
    home: str
    venv_active: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def pathmod(self):  # type: ignore[no-untyped-def]
        return ntpath if self.os == "windows" else posixpath

    def add(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


# --- Splitting ------------------------------------------------------------------


def _take_until(text: str, start: int, close: str) -> tuple[str, int]:
    """Text up to the matching close char (for $( ) and <( ), nesting-aware, quote-aware)."""
    depth, i, quote = 1, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    raise _ParseError(f"unclosed '{close}'")


def _split(command: str, os_kind: OsKind) -> tuple[list[str], list[str]]:
    """Split into simple commands. Returns (segments, substituted commands)."""
    posix = os_kind != "windows"
    segments: list[str] = []
    subs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)

    def flush() -> None:
        part = "".join(buf).strip()
        if part:
            segments.append(part)
        buf.clear()

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and posix and nxt:
            buf.append(ch + nxt)
            i += 2
            continue
        if ch == "`":
            if posix:  # command substitution
                end = command.find("`", i + 1)
                if end == -1:
                    raise _ParseError("unclosed backtick")
                subs.append(command[i + 1:end])
                buf.append(" __SUBST__ ")
                i = end + 1
                continue
            buf.append(command[i:i + 2])  # PowerShell escape character
            i += 2
            continue
        if ch == "$" and nxt == "(":
            inner, i = _take_until(command, i + 2, ")")
            subs.append(inner)
            buf.append(" __SUBST__ ")
            continue
        if ch in "<>" and nxt == "(" and quote is None:
            inner, i = _take_until(command, i + 2, ")")
            subs.append(inner)
            buf.append(" __SUBST__ ")
            continue
        if quote == '"':
            buf.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "&":
            prev = command[i - 1] if i else ""
            if prev in "<>" or nxt == ">":  # 2>&1, &>file
                buf.append(ch)
                i += 1
                continue
            flush()
            i += 2 if nxt == "&" else 1
            continue
        if ch == "|":
            flush()
            i += 2 if nxt == "|" else 1
            continue
        if ch in ";\n\r(){}":
            if ch == "{" and i and command[i - 1] in "$@":  # ${VAR}, @{...}
                buf.append(ch)
                i += 1
                continue
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote:
        raise _ParseError("unbalanced quotes")
    flush()
    return segments, subs


@dataclass
class _Tokens:
    words: list[str]
    write_targets: list[str]  # files written by > or >>


_NULL_TARGETS = frozenset({"/dev/null", "nul", "$null", "/dev/stdout", "/dev/stderr", "con"})


def _tokenize(segment: str, os_kind: OsKind) -> _Tokens:
    posix = os_kind != "windows"
    words: list[str] = []
    targets: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    pending: str | None = None  # "write" or "read" when the next word is a redirect target
    have_word = False
    i, n = 0, len(segment)

    def end_word() -> None:
        nonlocal pending, have_word
        if not have_word:
            return
        word = "".join(cur)
        cur.clear()
        have_word = False
        if pending == "write":
            targets.append(word)
        elif pending is None:
            words.append(word)
        pending = None

    while i < n:
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and posix and quote == '"' and i + 1 < n:
                cur.append(segment[i + 1])
                i += 1
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            have_word = True
            i += 1
            continue
        if ch == "\\" and posix and i + 1 < n:
            cur.append(segment[i + 1])
            have_word = True
            i += 2
            continue
        if ch.isspace():
            end_word()
            i += 1
            continue
        if ch in "<>":
            word = "".join(cur)
            if have_word and word not in ("1", "2", "3", "&", "*"):
                end_word()
            else:
                cur.clear()
                have_word = False
            j = i + 1
            if j < n and segment[j] == ch:
                j += 1  # >> or <<
            if ch == ">" and j < n and segment[j] == "&":
                j += 1
                while j < n and (segment[j].isdigit() or segment[j] == "-"):
                    j += 1  # >&2, 2>&1: no file
                i = j
                continue
            pending = "write" if ch == ">" else "read"
            i = j
            continue
        cur.append(ch)
        have_word = True
        i += 1
    if quote:
        raise _ParseError("unbalanced quotes")
    end_word()
    return _Tokens(words, targets)


# --- Paths ----------------------------------------------------------------------

_GLOB_CHARS = set("*?[")
_HOME_VARS = re.compile(
    r"^(?:~|\$home|\$\{home\}|\$env:userprofile|\$env:home|%userprofile%|\$\{env:userprofile\})(?=$|[/\\])",
    re.IGNORECASE,
)
_VAR = re.compile(r"\$\{?[a-z_][\w:]*\}?|%[a-z_][\w()]*%|__SUBST__", re.IGNORECASE)


def _norm_key(ctx: _Ctx, path: str) -> str:
    norm = ctx.pathmod.normpath(path)
    return norm.casefold() if ctx.os in ("windows", "macos") else norm


def _is_abs(ctx: _Ctx, path: str) -> bool:
    if ctx.os == "windows":
        return bool(ntpath.splitdrive(path)[0]) or path.startswith(("\\", "/"))
    return path.startswith("/")


def _expand_home(ctx: _Ctx, text: str) -> str:
    return _HOME_VARS.sub(lambda _m: ctx.home, text, count=1)


def _strip_glob(text: str) -> tuple[str, bool]:
    """Folder part before the first wildcard, and whether the wildcard matches everything.

    "src/*.py" -> ("src", False); "*" -> (".", True); "/tmp/*" -> ("/tmp", True)
    """
    idx = min((text.find(c) for c in _GLOB_CHARS if c in text), default=-1)
    if idx < 0:
        return text, False
    cut = max(text.rfind("/", 0, idx), text.rfind("\\", 0, idx))
    last = re.split(r"[/\\]", text)[-1]
    match_all = last in ("*", "*.*", ".*", "**")
    if cut == -1:
        return ".", match_all
    if cut == 0:
        return text[0], match_all
    base = text[:cut]
    return (base + "\\" if base.endswith(":") else base), match_all


def _path_status(ctx: _Ctx, raw: str) -> PathStatus:
    """Where a path argument points, relative to the project root."""
    text = _expand_home(ctx, raw.strip())
    if not text or _VAR.search(text):
        return "unknown"
    base, match_all = _strip_glob(text)
    # A pattern such as "*.log" only matches some files in its folder.
    partial_glob = base != text and not match_all
    if not _is_abs(ctx, base):
        if ctx.cwd is None:
            return "unknown"
        base = ctx.pathmod.join(ctx.cwd, base)
    elif ctx.os == "windows" and base[:1] in "/\\" and not base.startswith(("\\\\", "//")):
        base = ntpath.splitdrive(ctx.cwd or ctx.root)[0] + base  # "\foo" = root of the current drive
    full = ctx.pathmod.normpath(base)
    if _current_os() == ctx.os:
        try:
            full = os.path.realpath(full)  # follow symlinks when classifying for this machine
        except OSError:
            pass
    key, root = _norm_key(ctx, full), _norm_key(ctx, ctx.root)
    sep = "\\" if ctx.os == "windows" else "/"
    if key == root:
        return "inside" if partial_glob else "root"
    if key.startswith(root.rstrip("\\/") + sep):
        return "inside"
    return "outside"


def _sensitive(path: str) -> str | None:
    for pattern, label in SENSITIVE_HINTS:
        if pattern.search(path.replace("\\", "/")) or pattern.search(path):
            return label
    return None


# --- Per-command checks ---------------------------------------------------------


def _prog_name(word: str) -> str:
    name = re.split(r"[/\\]", word)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_option(word: str, prog: str, ctx: _Ctx) -> bool:
    if word.startswith("-") and word != "-":
        return True
    return (
        ctx.os == "windows"
        and prog in SLASH_SWITCH_COMMANDS
        and re.fullmatch(r"/[a-z?]{1,4}(?::\S*)?", word, re.IGNORECASE) is not None
    )


def _path_args(words: list[str], prog: str, ctx: _Ctx) -> list[str]:
    args = []
    for w in words:
        if w == "__SUBST__":
            args.append("$SUBST")  # unknown value
        elif not _is_option(w, prog, ctx):
            args.append(w)
    return args


def _describe(ctx: _Ctx, raw: str, status: PathStatus) -> str:
    hint = _sensitive(raw)
    if status == "unknown":
        return f"'{raw}' can't be resolved (variable or unknown folder)"
    if status == "root":
        return f"'{raw}' is the whole project folder"
    return f"'{raw}' is outside the project" + (f" ({hint})" if hint else "")


def _check_targets(ctx: _Ctx, targets: Iterable[str], verb: str, allow_root: bool) -> None:
    for raw in targets:
        status = _path_status(ctx, raw)
        if status == "inside" or (status == "root" and allow_root):
            continue
        ctx.add(f"{verb}: {_describe(ctx, raw, status)}")


def _check_git(ctx: _Ctx, args: list[str]) -> None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return
    sub, rest = args[i].lower(), [a.lower() for a in args[i + 1:]]
    if sub == "push":
        ctx.add("git push sends changes to a remote repository")
    elif sub == "config" and any(a in ("--global", "--system") for a in rest):
        ctx.add("changes global git configuration")
    elif sub == "reset" and "--hard" in rest:
        ctx.add("git reset --hard discards uncommitted changes")
    elif sub == "clean" and any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a)
                                for a in rest):
        ctx.add("git clean -f deletes untracked files")
    elif sub in ("checkout", "restore") and ("." in rest or "--" in rest and rest[-1:] == ["."]):
        ctx.add(f"git {sub} . discards uncommitted changes")
    elif sub == "stash" and rest[:1] in (["drop"], ["clear"]):
        ctx.add("git stash drop/clear deletes saved work")
    elif sub == "branch" and (
        "-D" in args[i + 1:] or (("--delete" in rest or "-d" in rest) and ("--force" in rest or "-f" in rest))
    ):
        ctx.add("git branch -D deletes a branch without checks")
    elif sub in ("filter-branch", "filter-repo") or (sub == "reflog" and rest[:1] == ["expire"]) or \
            (sub == "update-ref" and "-d" in rest):
        ctx.add(f"git {sub} rewrites or deletes history")


def _check_pip(ctx: _Ctx, pip_exe: str, args: list[str]) -> None:
    lowered = [a.lower() for a in args]
    sub = next((a for a in lowered if not a.startswith("-")), "")
    if sub not in ("install", "uninstall", "remove"):
        return
    if "--user" in lowered:
        ctx.add("pip --user installs into your user site-packages")
        return
    if "--break-system-packages" in lowered:
        ctx.add("pip --break-system-packages changes the system Python")
        return
    for opt in ("--target", "-t", "--prefix", "--root"):
        if opt in lowered:
            idx = lowered.index(opt)
            if idx + 1 < len(args) and _path_status(ctx, args[idx + 1]) in ("inside", "root"):
                return
    in_venv = ctx.venv_active or (_is_path_like(pip_exe) and _path_status(ctx, pip_exe) == "inside")
    if not in_venv:
        ctx.add("pip install without a project virtualenv may change the system Python "
                "(use .venv/bin/pip, venv\\Scripts\\pip or activate the venv first)")


INTERPRETER_PROGS = re.compile(r"python[\d.]*|py|pypy3?|node|nodejs|deno|bun|ruby|perl|php")
# Flags whose value is a piece of code to run.
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-E", "--eval", "-p", "--print", "-r"})
# Inline code that runs other programs, deletes / moves / writes files, changes permissions,
# opens network connections or evaluates code built at runtime can't be checked: dangerous.
DANGEROUS_INLINE = re.compile(
    r"\b(os\.(system|popen|exec\w*|spawn\w*|remove|unlink|rmdir|removedirs|rename|replace|chmod|chown|kill|putenv)"
    r"|subprocess|pty\.spawn|shutil\.(rmtree|move|chown|copytree|copy\w*)|send2trash|ctypes|winreg"
    r"|child_process|exec(Sync|File|FileSync)?\s*\(|spawn(Sync)?\s*\(|process\.kill|rimraf|del\s*\("
    r"|(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync|rename|renameSync|writeFile|writeFileSync|appendFile"
    r"|appendFileSync|chmod|chmodSync|chown|chownSync|copyFile|copyFileSync|truncate)\s*\("
    r"|write_text|write_bytes|\bunlink\b|\brmdir\b|\brmtree\b|FileUtils|File\.(delete|write)|Dir\.(delete|rmdir)"
    r"|shell_exec|passthru|proc_open|popen|system\s*\(|eval\s*\(|exec\s*\(|Function\s*\(|__import__"
    r"|socket|urllib|requests\.|http\.client|fetch\s*\(|Deno\.(run|remove|writeFile|Command))"
    r"|open\s*\([^)]*['\"][wax]\+?b?['\"]|`|%x\{|qx",
)


def _check_inline_code(ctx: _Ctx, prog: str, args: list[str]) -> None:
    """python -c / node -e / ruby -e / perl -e / php -r ...: check the code itself."""
    if not args or args == ["-"]:
        ctx.add(f"{prog} runs code from stdin (can't be checked)")
        return
    if prog == "deno" and args[0] == "eval":
        code = " ".join(args[1:])
    else:
        code = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in INLINE_CODE_FLAGS), "")
    if code and DANGEROUS_INLINE.search(code):
        ctx.add(f"{prog} inline code runs programs or changes files (can't be checked)")


def _is_path_like(word: str) -> bool:
    return "/" in word or "\\" in word


def _check_simple(ctx: _Ctx, words: list[str], write_targets: list[str], depth: int) -> None:
    # Redirections (> file) count as writes for every command; /dev/null and friends don't.
    real_targets = [t for t in write_targets if t.lower() not in _NULL_TARGETS]
    _check_targets(ctx, real_targets, "Writes to a file", allow_root=False)
    # Strip leading VAR=value assignments.
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    if not words:
        return

    prog_word = words[0]
    prog = _prog_name(prog_word)
    args = words[1:]

    if prog == "__subst__":
        ctx.add("runs a command whose name comes from a substitution (can't be checked)")
        return

    # Wrappers: check the wrapped command.
    if prog in WRAPPERS:
        if prog in ("start-process", "saps") and any(a.lower() == "runas" for a in args):
            ctx.add("starts a process with elevated privileges")
        # Skip only the wrapper's own leading options; the wrapped command keeps its flags.
        rest = list(args)
        value_opts = {"-n", "-c", "-p", "-u", "-s", "-k", "-i", "-o", "-e", "-d", "-l", "-P", "-I",
                      "--signal", "--kill-after"}
        while rest and (rest[0].startswith("-") or (prog == "start" and rest[0].startswith("/"))):
            opt = rest.pop(0)
            if opt.lower() == "-filepath":
                break  # Start-Process -FilePath X: X is the command
            if opt in value_opts and rest and prog in ("nice", "ionice", "timeout", "xargs", "stdbuf"):
                rest.pop(0)
        if prog == "timeout" and rest and re.fullmatch(r"[\d.]+[smhd]?", rest[0]):
            rest = rest[1:]
        while rest and re.fullmatch(r"[A-Za-z_]\w*=.*", rest[0]):
            rest = rest[1:]
        if prog == "xargs" and rest and _prog_name(rest[0]) in DELETE_COMMANDS:
            ctx.add("deletes files whose names come from input (can't be checked)")
            return
        if rest:
            _check_simple(ctx, rest, [], depth)
        return

    # Shells: check the inner command.
    if prog in SHELLS:
        if "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args):
                _classify_into(ctx, args[idx + 1], depth + 1)
        return
    if prog == "cmd":
        lowered = [a.lower() for a in args]
        for flag in ("/c", "/k", "/r"):
            if flag in lowered:
                inner = " ".join(args[lowered.index(flag) + 1:])
                _classify_into(ctx, inner, depth + 1, os_override="windows")
                return
        return
    if prog in POWERSHELLS:
        lowered = [a.lower() for a in args]
        if any(a in ("-encodedcommand", "-enc", "-ec", "-e") for a in lowered):
            ctx.add("runs an encoded PowerShell command (can't be checked)")
            return
        for flag in ("-command", "-c"):
            if flag in lowered:
                inner = " ".join(args[lowered.index(flag) + 1:])
                _classify_into(ctx, inner, depth + 1, os_override="windows")
                return
        positional = [a for a in args if not a.startswith("-")]
        if positional and not any(a in ("-file", "-f") for a in lowered):
            _classify_into(ctx, " ".join(positional), depth + 1, os_override="windows")
        return

    # Directory changes affect later relative paths.
    if prog in ("cd", "chdir", "pushd", "set-location", "sl"):
        targets = [a for a in args if not a.startswith("-")]
        if not targets:
            ctx.cwd = ctx.home if ctx.os != "windows" else ctx.cwd
            return
        status = _path_status(ctx, targets[0])
        if status == "unknown":
            ctx.cwd = None
        else:
            base = _expand_home(ctx, targets[0])
            joined = base if _is_abs(ctx, base) else ctx.pathmod.join(ctx.cwd or ctx.root, base)
            ctx.cwd = ctx.pathmod.normpath(joined)
        return
    if prog in ("popd",):
        ctx.cwd = None
        return

    # venv activation makes later pip installs project-local.
    if prog in ("source", ".", "activate", "activate.ps1", "&") or prog.startswith("activate"):
        target = args[0] if args and prog in ("source", ".") else prog_word
        if "activate" in target.lower() and _path_status(ctx, target) in ("inside", "root"):
            ctx.venv_active = True
            return
        if prog in ("source", ".") and args and _path_status(ctx, args[0]) not in ("inside", "root"):
            ctx.add(f"runs a script from outside the project: {args[0]}")
        return

    if prog in ALWAYS_DANGEROUS:
        reason = ALWAYS_DANGEROUS[prog]
        if prog == "kill" and args and all(re.fullmatch(r"%\d+", a) for a in args if not a.startswith("-")):
            return  # shell job of this command line
        ctx.add(f"{prog}: {reason}")
        return
    if prog.startswith("mkfs"):
        ctx.add(f"{prog}: can damage disks or filesystems")
        return

    if prog in PACKAGE_MANAGERS:
        first = next((a for a in args if not a.startswith("-") or a in PACKAGE_READ_ONLY), "")
        if first.lower() not in PACKAGE_READ_ONLY:
            ctx.add(f"{prog}: installs or changes system packages")
        return

    if prog == "diskutil":
        if (args[0].lower() if args else "") not in ("list", "info", "activity"):
            ctx.add("diskutil: can erase or change disks")
        return
    if prog == "systemctl":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub not in ("status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
                       "list-timers", "show", "cat"):
            ctx.add("systemctl: controls system services")
        return
    if prog == "sc":
        sub = args[0].lower() if args else ""
        if sub not in ("query", "queryex", "qc", "qdescription", "getdisplayname", "getkeyname"):
            ctx.add("sc: controls Windows services (or Set-Content in PowerShell)")
        return
    if prog == "reg":
        sub = args[0].lower() if args else ""
        if sub not in ("query", "compare"):
            ctx.add("reg: changes the Windows registry")
        return
    if prog == "net":
        sub = args[0].lower() if args else ""
        if sub in ("user", "localgroup", "group", "share", "use", "start", "stop", "accounts"):
            ctx.add(f"net {sub}: changes system accounts, shares or services")
        return

    if prog == "git":
        _check_git(ctx, args)
        return

    if prog in ("npm", "pnpm", "yarn", "bun", "cnpm"):
        lowered = [a.lower() for a in args]
        if "-g" in lowered or "--global" in lowered or (prog == "yarn" and lowered[:1] == ["global"]) or \
                "--location=global" in lowered:
            ctx.add(f"{prog}: global install changes your system")
        elif any(a in ("publish", "unpublish", "deprecate", "owner", "dist-tag") for a in lowered[:1]):
            ctx.add(f"{prog} {lowered[0]}: changes a public package registry")
        return
    if prog in ("twine", "gem", "cargo", "go", "dotnet", "composer", "docker", "podman"):
        lowered = [a.lower() for a in args]
        first = lowered[0] if lowered else ""
        if prog == "twine" and first == "upload":
            ctx.add("twine upload publishes a package")
        elif prog == "gem" and first in ("install", "uninstall", "update", "push"):
            if "--install-dir" not in lowered and "-i" not in lowered:
                ctx.add(f"gem {first}: changes globally installed gems")
        elif prog == "cargo" and first in ("install", "uninstall", "publish", "yank"):
            ctx.add(f"cargo {first}: changes global binaries or a public registry")
        elif prog == "go" and first == "install":
            ctx.add("go install puts binaries in your global GOPATH/bin")
        elif prog == "dotnet" and first == "tool" and ("-g" in lowered or "--global" in lowered):
            ctx.add("dotnet tool --global changes your system")
        elif prog == "composer" and first == "global":
            ctx.add("composer global changes your system")
        elif prog in ("docker", "podman") and first in ("push", "login", "system", "volume", "rm", "rmi", "prune"):
            ctx.add(f"{prog} {first}: changes images, volumes or a remote registry")
        return

    if prog in ("pip", "pip3") or re.fullmatch(r"pip\d(\.\d+)?", prog):
        _check_pip(ctx, prog_word, args)
        return
    if re.fullmatch(r"(python[\d.]*|py)", prog) and len(args) >= 2 and args[0] == "-m" and \
            args[1] in ("pip", "pip3"):
        _check_pip(ctx, prog_word, args[2:])
        return
    if INTERPRETER_PROGS.fullmatch(prog):
        _check_inline_code(ctx, prog, args)
        return
    if prog == "uv":
        lowered = [a.lower() for a in args]
        if "--system" in lowered:
            ctx.add("uv --system installs into the system Python")
        elif lowered[:1] == ["tool"] and len(lowered) > 1 and lowered[1] in ("install", "uninstall"):
            ctx.add("uv tool install changes global tools")
        return

    if prog in DELETE_COMMANDS:
        targets = _path_args(args, prog, ctx)
        if not targets:
            ctx.add(f"{prog}: delete targets are unknown (pipeline or no path given)")
        _check_targets(ctx, targets, "Deletes", allow_root=False)
        return

    if prog == "find":
        lowered = [a.lower() for a in args]
        exec_rm = any(a in ("-exec", "-execdir", "-ok") for a in lowered) and any(
            _prog_name(a) in DELETE_COMMANDS for a in args
        )
        if "-delete" in lowered or exec_rm:
            starts = []
            for a in args:
                if a.startswith(("-", "(", "!")):
                    break
                starts.append(a)
            _check_targets(ctx, starts or ["."], "Deletes (find)", allow_root=True)
        return

    if prog == "sed":
        if any(a == "--in-place" or a.startswith("--in-place=") or re.fullmatch(r"-[a-zA-Z]*i\S*", a)
               for a in args):
            files = [a for a in _path_args(args, prog, ctx)][1:]  # first non-option is the script
            _check_targets(ctx, files, "Edits a file", allow_root=False)
        return
    if prog == "perl" and any(re.fullmatch(r"-\w*i\S*", a) for a in args):
        _check_targets(ctx, [a for a in args if not a.startswith("-")][1:], "Edits a file", allow_root=False)
        return

    if prog in COPY_COMMANDS:
        targets = _path_args(args, prog, ctx)
        lowered = [a.lower() for a in args]
        dest = None
        for opt in ("-destination", "-t", "--target-directory"):
            if opt in lowered and lowered.index(opt) + 1 < len(args):
                dest = args[lowered.index(opt) + 1]
        if dest is None and len(targets) >= 2:
            dest = targets[-1] if prog != "robocopy" else targets[1]
        if dest is not None:
            _check_targets(ctx, [dest], "Copies into", allow_root=True)
        return

    if prog in WRITE_COMMANDS:
        targets = _path_args(args, prog, ctx)
        if prog in ("chmod", "chown", "chgrp") and targets:
            targets = targets[1:]  # first argument is the mode / owner
        if prog in ("new-itemproperty", "set-itemproperty", "remove-itemproperty", "rp", "sp"):
            ctx.add(f"{prog}: changes item properties (usually the registry)")
            return
        moves = ("mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni")
        if prog in moves and len(targets) > 1:
            _check_targets(ctx, targets[:-1], "Moves", allow_root=False)
            _check_targets(ctx, targets[-1:], "Moves into", allow_root=True)
        else:
            _check_targets(ctx, targets, "Writes", allow_root=True)
        for t in targets:
            if (label := _sensitive(t)) is not None:
                ctx.add(f"{prog}: touches {label}")
        return


def _classify_into(ctx: _Ctx, command: str, depth: int, os_override: OsKind | None = None) -> None:
    if depth > MAX_DEPTH:
        ctx.add("command is nested too deeply to check")
        return
    os_kind = os_override or ctx.os
    for pattern, reason in WHOLE_LINE_PATTERNS:
        if pattern.search(command):
            ctx.add(reason)
    try:
        segments, subs = _split(command, os_kind)
    except _ParseError as exc:
        ctx.add(f"could not parse the command ({exc})")
        return
    for sub in subs:
        _classify_into(ctx, sub, depth + 1, os_override)
    for segment in segments:
        try:
            tokens = _tokenize(segment, os_kind)
        except _ParseError as exc:
            ctx.add(f"could not parse the command ({exc})")
            continue
        _check_simple(ctx, tokens.words, tokens.write_targets, depth)
        # Writes to sensitive files are worth naming explicitly.
        for t in tokens.write_targets:
            if t.lower() not in _NULL_TARGETS and (label := _sensitive(t)):
                ctx.add(f"writes to {label}")


def classify_command(
    command: str, project_root: Path | str, os_name: str | None = None, cwd: Path | str | None = None
) -> SafetyResult:
    """Classify a shell command. Unknown or unparseable -> dangerous (safe default)."""
    if not isinstance(command, str) or not command.strip():
        return SafetyResult(True, "empty command")
    if "\x00" in command:
        return SafetyResult(True, "command contains a null byte")
    os_kind = _os_kind(os_name or sys.platform)
    pathmod = ntpath if os_kind == "windows" else posixpath
    root = pathmod.normpath(str(project_root))
    start = pathmod.normpath(str(cwd)) if cwd is not None else root
    if _current_os() == os_kind:
        root, start = os.path.realpath(root), os.path.realpath(start)
    if _current_os() == os_kind:
        home = str(Path.home())
    else:  # classifying for another OS: use a typical home folder for it
        home = {"windows": "C:\\Users\\user", "macos": "/Users/user", "linux": "/home/user"}[os_kind]
    ctx = _Ctx(os=os_kind, root=root, cwd=start, home=home)
    _classify_into(ctx, command, depth=0)
    if ctx.reasons:
        return SafetyResult(True, "; ".join(ctx.reasons[:4]) + (" ..." if len(ctx.reasons) > 4 else ""))
    return SafetyResult(False, "no dangerous patterns found")
