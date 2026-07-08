import os
import re

DANGEROUS_PATTERNS = [
    (re.compile(r"\{\{\s*cred\.raw\s*\}\}"), "CRED_RAW_BLOCK"),
    (re.compile(r"\$\{[^}]*\}"), "ENV_VAR_INJECTION"),
    (re.compile(r"\$\([^)]+\)"), "COMMAND_SUBSTITUTION"),
    (re.compile(r"&&"), "SHELL_CHAINING_AND"),
    (re.compile(r"\|\|"), "SHELL_CHAINING_OR"),
    (re.compile(r"\.\.\/"), "PATH_TRAVERSAL"),
]


def contains_dangerous_patterns(text: str) -> bool:
    for pattern, _ in DANGEROUS_PATTERNS:
        if pattern.search(text):
            return True
    return False


def scan_dangerous_patterns(text: str) -> list[str]:
    found = []
    for pattern, label in DANGEROUS_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


def validate_adapter_source(adapter_json: str) -> tuple[bool, list[str]]:
    patterns_found = scan_dangerous_patterns(adapter_json)
    is_safe = len(patterns_found) == 0
    return is_safe, patterns_found


# Interpreters, shells, and process multiplexers turn "run one fixed binary with
# a fixed argv" into "run arbitrary code" via their own arguments (e.g.
# `bash -c ...`, `python -c ...`, `env CMD ...`). A CLI adapter that names one of
# these as its backend.bin defeats the whole point of a non-shell argv, so we
# refuse to install it. This is a guardrail, not a sandbox: a CLI adapter still
# runs a real local binary, so installing a third-party CLI adapter is a trust
# decision. See SECURITY.md ("CLI adapter trust model").
CLI_BIN_BLOCKLIST = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "ash", "busybox",
        "pwsh", "powershell", "cmd", "command",
        "python", "python2", "python3", "pypy", "pypy3",
        "perl", "ruby", "node", "nodejs", "deno", "bun", "php", "lua", "luajit",
        "tclsh", "wish", "osascript", "rscript", "groovy", "awk", "gawk",
        "env", "xargs", "nice", "nohup", "setsid", "stdbuf", "timeout",
        "unbuffer", "script", "eval", "exec", "source",
    }
)


def validate_cli_bin(bin_value: str) -> tuple[bool, str | None]:
    """Reject a CLI adapter whose backend.bin is a shell/interpreter.

    Returns (ok, reason). Matches on the basename (case-insensitive, stripping a
    Windows-style executable suffix) so both `bash` and `/bin/bash` are caught.
    """
    name = os.path.basename((bin_value or "").strip()).lower()
    for suffix in (".exe", ".bat", ".cmd", ".ps1", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not name:
        return False, "CLI adapter backend.bin is empty"
    if name in CLI_BIN_BLOCKLIST:
        return (
            False,
            f"CLI adapter backend.bin '{name}' is a shell or interpreter and is not "
            "allowed; a CLI adapter must invoke a specific non-shell binary",
        )
    return True, None
