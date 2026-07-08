"""Unit tests for project-root and rextio-binary discovery."""

from __future__ import annotations

import os

from rextio_lsp.discovery import (
    find_project_root,
    find_project_root_for_uri,
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
