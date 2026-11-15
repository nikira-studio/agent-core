"""Repository folders as the machine running the tool names them.

Agent Core usually runs on Linux while a person's tool runs on Windows, macOS or
Linux. A session's working directory arrives as that machine's own text, so a
folder is compared as text under the rules of the path's own style: a Windows
folder (`C:\\Users\\me\\repo`, `C:/Users/me/repo`, `\\\\server\\share\\repo`) is
matched ignoring case and the direction of the slashes, a POSIX folder exactly.
"""

import ntpath
import os
import re

_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]|//[^/])")


def is_windows_path(path: str) -> bool:
    return bool(_WINDOWS.match(path or ""))


def is_absolute_folder(path: str) -> bool:
    return bool(path) and (is_windows_path(path) or os.path.isabs(path))


def normalize_folder(path: str) -> str:
    """The form stored for a folder: tidy, no trailing separator, the path's own style."""
    if is_windows_path(path):
        clean = ntpath.normpath(path)
        return clean[0].upper() + clean[1:] if clean[1:2] == ":" else clean
    return os.path.normpath(path)


def _windows_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path)).rstrip("\\")


def folder_contains(root: str, path: str) -> bool:
    """True when `path` is `root` or a folder inside it."""
    if not root or not path:
        return False
    if is_windows_path(root) or is_windows_path(path):
        if not (is_windows_path(root) and is_windows_path(path)):
            return False
        root_key, path_key = _windows_key(root), _windows_key(path)
        return path_key == root_key or path_key.startswith(root_key + "\\")
    root_clean, path_clean = os.path.normpath(root), os.path.normpath(path)
    return path_clean == root_clean or path_clean.startswith(root_clean.rstrip(os.sep) + os.sep)
