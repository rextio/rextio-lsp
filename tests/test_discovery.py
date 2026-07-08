"""Unit tests for project-root and rextio-binary discovery."""

from __future__ import annotations

import os
from pathlib import PureWindowsPath

from rextio_lsp.discovery import (
    find_project_root,
    find_project_root_for_uri,
    find_project_venv_binary,
    find_rextio_binary,
    uri_to_path,
)


def test_find_project_root_walks_up(tmp_path):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    module = nested / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")

    assert find_project_root(module) == tmp_path
    assert find_project_root(nested) == tmp_path
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_none_when_absent(tmp_path):
    module = tmp_path / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")
    assert find_project_root(module) is None


def test_find_project_root_for_uri(tmp_path):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    module = tmp_path / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")
    uri = module.as_uri()
    assert find_project_root_for_uri(uri) == tmp_path


def test_uri_to_path_roundtrip(tmp_path):
    module = tmp_path / "a b" / "mod.py"
    module.parent.mkdir()
    module.write_text("", encoding="utf-8")
    assert uri_to_path(module.as_uri()) == module
    assert uri_to_path("untitled:foo") is None


def test_uri_to_path_windows_drive_letter():
    # Platform-independent assertions (run on macOS via PureWindowsPath): the
    # hand-rolled Path(unquote(urlparse(...).path)) yielded "/C:/..." which
    # PureWindowsPath treats as drive-less and non-absolute. pygls' helper keeps
    # the drive.
    path = uri_to_path("file:///C:/Users/x/a.py")
    assert path is not None
    win = PureWindowsPath(str(path))
    assert win.drive.lower() == "c:"
    assert win.is_absolute()
    assert win.name == "a.py"


def test_uri_to_path_unc_share():
    path = uri_to_path("file://server/share/a.py")
    assert path is not None
    win = PureWindowsPath(str(path))
    # the netloc (host) is preserved as a UNC anchor, not dropped
    assert "server" in win.anchor
    assert "share" in win.anchor
    assert win.name == "a.py"


def test_uri_to_path_rejects_non_file_scheme():
    assert uri_to_path("http://example.com/a.py") is None
    assert uri_to_path("vscode-notebook-cell:/x.py") is None


def test_find_rextio_binary_prefers_project_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python3"
    py.write_text("", encoding="utf-8")
    binary = bindir / "rextio"
    binary.write_text("", encoding="utf-8")
    os.chmod(binary, 0o755)

    assert find_rextio_binary(tmp_path) == binary


def test_find_rextio_binary_none(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert find_rextio_binary(tmp_path) is None


def test_find_rextio_binary_prefers_interpreter_path(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # a project venv rextio exists...
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").write_text("", encoding="utf-8")
    venv_rextio = venv_bin / "rextio"
    venv_rextio.write_text("", encoding="utf-8")
    os.chmod(venv_rextio, 0o755)
    # ...but an explicit interpreter path points elsewhere and must win
    interp_bin = tmp_path / "custom" / "bin"
    interp_bin.mkdir(parents=True)
    interp_py = interp_bin / "python"
    interp_py.write_text("", encoding="utf-8")
    interp_rextio = interp_bin / "rextio"
    interp_rextio.write_text("", encoding="utf-8")
    os.chmod(interp_rextio, 0o755)

    assert find_rextio_binary(tmp_path, str(interp_py)) == interp_rextio
    # without the interpreter path, discovery falls back to the project venv
    assert find_rextio_binary(tmp_path) == venv_rextio


def test_find_rextio_binary_interpreter_path_missing_binary_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    # interpreter path has no neighbouring rextio -> falls through to venv/PATH
    interp_bin = tmp_path / "custom" / "bin"
    interp_bin.mkdir(parents=True)
    interp_py = interp_bin / "python"
    interp_py.write_text("", encoding="utf-8")
    assert find_rextio_binary(tmp_path, str(interp_py)) is None


def test_find_project_venv_binary_direct_probe_without_python(tmp_path, monkeypatch):
    # a venv that exposes rextio but NO python3/python interpreter at all: the
    # direct <venv>/bin/rextio probe must still find it.
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    binary = bindir / "rextio"
    binary.write_text("", encoding="utf-8")
    os.chmod(binary, 0o755)
    assert find_project_venv_binary(tmp_path) == binary
    assert find_rextio_binary(tmp_path) == binary


def test_find_project_venv_binary_versioned_python_only(tmp_path, monkeypatch):
    # venv with only ``python3.12`` (no python3/python): the neighbour-of-python
    # fallback still locates rextio via the python3.X glob.
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python3.12").write_text("", encoding="utf-8")
    binary = bindir / "rextio"
    binary.write_text("", encoding="utf-8")
    os.chmod(binary, 0o755)
    # remove the direct-probe advantage is not possible (same dir), but this
    # documents the versioned-python discovery path explicitly.
    assert find_project_venv_binary(tmp_path) == binary


def test_find_project_venv_binary_none_without_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert find_project_venv_binary(tmp_path) is None


# --------------------------------------------------------------------------- #
# VIRTUAL_ENV must not shadow a root-local venv (fix #3).
# --------------------------------------------------------------------------- #
def test_root_local_venv_wins_over_ambient_virtualenv(tmp_path, monkeypatch):
    # a server-ambient VIRTUAL_ENV points at an unrelated env; the root-local
    # .venv/bin/rextio must win in a multi-root workspace.
    ambient = tmp_path / "ambient"
    (ambient / "bin").mkdir(parents=True)
    ambient_rextio = ambient / "bin" / "rextio"
    ambient_rextio.write_text("", encoding="utf-8")
    os.chmod(ambient_rextio, 0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(ambient))

    proj = tmp_path / "proj"
    local_bin = proj / ".venv" / "bin"
    local_bin.mkdir(parents=True)
    local_rextio = local_bin / "rextio"
    local_rextio.write_text("", encoding="utf-8")
    os.chmod(local_rextio, 0o755)

    assert find_project_venv_binary(proj) == local_rextio


def test_virtualenv_under_root_is_a_candidate(tmp_path, monkeypatch):
    # a VIRTUAL_ENV that lies UNDER project_root is still discovered when there
    # is no .venv/venv (resolve + is_relative_to admits it).
    proj = tmp_path / "proj"
    env_bin = proj / "env" / "bin"
    env_bin.mkdir(parents=True)
    rextio = env_bin / "rextio"
    rextio.write_text("", encoding="utf-8")
    os.chmod(rextio, 0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(proj / "env"))
    assert find_project_venv_binary(proj) == rextio


def test_ambient_virtualenv_not_under_root_is_ignored(tmp_path, monkeypatch):
    # an unrelated ambient VIRTUAL_ENV (not under root, no root-local venv) must
    # not be a candidate at all.
    ambient = tmp_path / "ambient"
    (ambient / "bin").mkdir(parents=True)
    r = ambient / "bin" / "rextio"
    r.write_text("", encoding="utf-8")
    os.chmod(r, 0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(ambient))
    proj = tmp_path / "proj"
    proj.mkdir()
    assert find_project_venv_binary(proj) is None
