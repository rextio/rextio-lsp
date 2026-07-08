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
    """Best-effort conversion of a ``file://`` URI to a local path.

    Delegates to pygls' own URI helper (``pygls.uris.to_fs_path``) so Windows
    drive-letter (``file:///C:/x``) and UNC (``file://server/share/x``) shapes
    convert correctly -- a hand-rolled ``Path(unquote(urlparse(uri).path))``
    mangles both (drive letters keep a leading slash and lose their drive; UNC
    hosts drop the netloc). Non-``file`` schemes return ``None``; a bare path
    with no scheme is treated as a local path.
    """
    from urllib.parse import urlparse

    from pygls.uris import to_fs_path

    if not urlparse(uri).scheme:
        return Path(uri)
    fs_path = to_fs_path(uri)
    if fs_path is None:
        return None
    return Path(fs_path)


def find_rextio_binary(
    project_root: Path, interpreter_path: str | None = None
) -> Path | None:
    """Locate a ``rextio`` executable to use for the subprocess fallback.

    Preference order:

    0. A ``rextio`` binary next to ``interpreter_path`` when the client supplied
       one via ``initializationOptions.interpreter.path``. This is consulted
       first so an editor-configured interpreter wins over auto-discovery.
    1. A ``rextio`` binary inside a project virtualenv (see
       :func:`find_project_venv_binary`).
    2. A ``rextio`` on ``PATH``.

    ``sys.executable`` (the LSP server's own interpreter) is deliberately *not*
    treated as the project interpreter: in production the server runs from its
    own environment, so its neighbour ``rextio`` would be the wrong one.
    """
    if interpreter_path:
        candidate = Path(interpreter_path).parent / _exe("rextio")
        if _is_executable(candidate):
            return candidate

    venv_binary = find_project_venv_binary(project_root)
    if venv_binary is not None:
        return venv_binary

    from shutil import which

    found = which("rextio")
    return Path(found) if found else None


def find_project_venv_binary(project_root: Path) -> Path | None:
    """Return a ``rextio`` binary from a project virtualenv, or ``None``.

    For each candidate venv root (``VIRTUAL_ENV`` first, then ``<root>/.venv``
    and ``<root>/venv``):

    1. probe ``<venv>/bin/rextio`` (``Scripts/rextio.exe`` on Windows) directly
       -- the binary is what we ultimately want, so prefer it over the
       interpreter indirection (this also covers venvs that expose only a
       versioned ``python3.X`` and no bare ``python3``/``python``);
    2. otherwise probe a ``rextio`` beside the venv's Python interpreter
       (``python3``/``python``, then a ``python3.X`` glob fallback).
    """
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    for venv in _candidate_venv_roots(project_root):
        direct = venv / bindir / _exe("rextio")
        if _is_executable(direct):
            return direct
        for python in _venv_pythons(venv / bindir):
            candidate = python.parent / _exe("rextio")
            if _is_executable(candidate):
                return candidate
    return None


def _candidate_venv_roots(project_root: Path) -> list[Path]:
    roots: list[Path] = []
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        roots.append(Path(venv_env))
    roots.extend([project_root / ".venv", project_root / "venv"])
    return roots


def _venv_pythons(bindir: Path) -> list[Path]:
    """Return interpreter candidates in ``bindir``.

    ``python3``/``python`` first, else a ``python3.X`` glob fallback for venvs
    that expose only a versioned interpreter name.
    """
    pythons: list[Path] = []
    for name in ("python3", "python"):
        candidate = bindir / _exe(name)
        if candidate.is_file():
            pythons.append(candidate)
    if not pythons:
        pattern = "python3.*.exe" if sys.platform == "win32" else "python3.*"
        pythons.extend(sorted(p for p in bindir.glob(pattern) if p.is_file()))
    return pythons


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name
