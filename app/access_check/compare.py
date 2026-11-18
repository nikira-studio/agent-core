"""Compare two access snapshots and list every difference, with a path and both values.

Paths are tuples of segments (never joined and split again), so an id or a name containing a dot cannot be mistaken for
two segments. A pattern is a tuple too; ``"*"`` stands for any one segment. A dotted string is accepted for a pattern
whose segments hold no dot (``"identities.*.kind"``).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

Path = tuple[str, ...]


def _walk(before: Any, after: Any, path: Path, out: list[dict]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            child = path + (str(key),)
            if key not in before:
                out.append({"path": child, "before": None, "after": after[key], "change": "added"})
            elif key not in after:
                out.append({"path": child, "before": before[key], "after": None, "change": "removed"})
            else:
                _walk(before[key], after[key], child, out)
    elif before != after:
        out.append({"path": path, "before": before, "after": after, "change": "changed"})


def dotted(path: Sequence[str]) -> str:
    """A path for people to read. Never parse this back."""
    return ".".join(path)


def _pattern(value: str | Sequence[str]) -> Path:
    return tuple(value.split(".")) if isinstance(value, str) else tuple(value)


def differences(before: dict, after: dict, allowed: Iterable[str | Sequence[str]] = ()) -> list[dict]:
    """Every difference between two snapshots that is not covered by ``allowed``.

    A difference under an allowed pattern is not reported. Nothing else is skipped, in either direction: an entry that
    gains access and one that loses it are both reported.
    """
    found: list[dict] = []
    _walk(before, after, (), found)
    patterns = [_pattern(p) for p in allowed]

    def covered(path: Path) -> bool:
        for pattern in patterns:
            if len(path) >= len(pattern) and all(p == "*" or p == q for p, q in zip(pattern, path)):
                return True
        return False

    return [d for d in found if not covered(d["path"])]


def summarise(diffs: list[dict], limit: int = 40) -> str:
    lines = [f"{d['change']}: {dotted(d['path'])}  before={d['before']!r}  after={d['after']!r}" for d in diffs[:limit]]
    if len(diffs) > limit:
        lines.append(f"... and {len(diffs) - limit} more")
    return "\n".join(lines)
