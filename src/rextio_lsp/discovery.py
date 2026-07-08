"""Project-root and rextio-binary discovery.

Activation is gated on finding a ``rextio.toml`` by walking up from a document
(or from a workspace folder). Without one, the server is a silent no-op.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_FILENAME = "rextio.toml"


def find_project_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a directory containing ``rextio.toml``.

    ``start`` may be a file or directory. Returns the first ancestor (including
    ``start`` itself when it is a directory) that contains a ``rextio.toml``, or
    ``None`` if none is found before the filesystem root.
    """
    start = start.resolve()
    base = start if start.is_dir() else start.parent
    for directory in (base, *base.parents):
        if (directory / CONFIG_FILENAME).is_file():
            return directory
    return None


def find_project_root_for_uri(uri: str) -> Path | None:
    """Discover the project root for a ``file://`` document URI."""
    path = uri_to_path(uri)
    if path is None:
        return None
    return find_project_root(path)


def uri_to_path(uri: str) -> Path | None:
    """Best-effort conversion of a ``file://`` URI to a local path."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if not parsed.scheme:
        return Path(uri)
    return Path(unquote(parsed.path))


def find_rextio_binary(
    project_root: Path, interpreter_path: str | None = None
) -> Path | None:
    """Locate a ``rextio`` executable to use for the subprocess fallback.

    Preference order:

    0. A ``rextio`` binary next to ``interpreter_path`` when the client supplied
       one via ``initializationOptions.interpreter.path``. This is consulted
       first so an editor-configured interpreter wins over auto-discovery.
    1. A ``rextio`` binary next to the project's Python interpreter, when a
       project virtualenv is discoverable (``.venv``/``venv`` under the root,
       or ``VIRTUAL_ENV``).
    2. A ``rextio`` on ``PATH``.

    ``sys.executable`` (the LSP server's own interpreter) is deliberately *not*
    treated as the project interpreter: in production the server runs from its
    own environment, so its neighbour ``rextio`` would be the wrong one.
    """
    if interpreter_path:
        candidate = Path(interpreter_path).parent / _exe("rextio")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    for python in _candidate_project_pythons(project_root):
        candidate = python.parent / _exe("rextio")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    from shutil import which

    found = which("rextio")
    return Path(found) if found else None


def _candidate_project_pythons(project_root: Path) -> list[Path]:
    pythons: list[Path] = []
    venv_env = os.environ.get("VIRTUAL_ENV")
    roots: list[Path] = []
    if venv_env:
        roots.append(Path(venv_env))
    roots.extend([project_root / ".venv", project_root / "venv"])
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    for root in roots:
        for name in ("python3", "python"):
            candidate = root / bindir / _exe(name)
            if candidate.is_file():
                pythons.append(candidate)
                break
    return pythons


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name
