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
