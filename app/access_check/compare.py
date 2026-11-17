"""Compare two access snapshots and list every difference, with a path and both values."""

from __future__ import annotations

from typing import Any, Iterable


def _walk(before: Any, after: Any, path: str, out: list[dict]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                out.append({"path": child, "before": None, "after": after[key], "change": "added"})
            elif key not in after:
                out.append({"path": child, "before": before[key], "after": None, "change": "removed"})
            else:
                _walk(before[key], after[key], child, out)
    elif before != after:
        out.append({"path": path, "before": before, "after": after, "change": "changed"})


def differences(before: dict, after: dict, allowed: Iterable[str] = ()) -> list[dict]:
    """Every difference between two snapshots that is not covered by ``allowed``.

    ``allowed`` holds path patterns a revision declares it will change; a ``*`` stands for one path segment
    (``identities.*.kind``). A difference under an allowed pattern is not reported. Nothing else is skipped, in
    either direction: an entry that gains access and one that loses it are both reported.
    """
    found: list[dict] = []
    _walk(before, after, "", found)
    patterns = [p.split(".") for p in allowed]

    def covered(path: str) -> bool:
        parts = path.split(".")
        for pattern in patterns:
            if len(parts) >= len(pattern) and all(p == "*" or p == q for p, q in zip(pattern, parts)):
                return True
        return False

    return [d for d in found if not covered(d["path"])]


def summarise(diffs: list[dict], limit: int = 40) -> str:
    lines = [f"{d['change']}: {d['path']}  before={d['before']!r}  after={d['after']!r}" for d in diffs[:limit]]
    if len(diffs) > limit:
        lines.append(f"... and {len(diffs) - limit} more")
    return "\n".join(lines)
