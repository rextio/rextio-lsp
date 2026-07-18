"""Unit tests for acquisition precedence, subprocess fallback, and caching."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rextio_lsp import engine

CHECK_PAYLOAD = {"contract_version": "1.0.0", "project_root": "/p", "modules": []}


def test_in_process_probe_short_circuits_when_rextio_parent_is_absent(monkeypatch):
    """A missing optional parent package is normal, not an import failure."""
    calls: list[str] = []

    def find_spec(name):
        calls.append(name)
        if name == "rextio":
            return None
        raise AssertionError("child lookup must be short-circuited")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert engine._rextio_available_in_process() is False
    assert calls == ["rextio"]


def test_conftest_loads_when_rextio_parent_package_is_absent():
    """The real-core integration marker must resolve to a skip, not crash."""
    conftest = Path(__file__).with_name("conftest.py")
    script = f"""
import runpy
import sys
import types

pytest = types.ModuleType("pytest")
pytest.fixture = lambda function: function

class Mark:
    def __getattr__(self, name):
        return lambda *args, **kwargs: object()

pytest.mark = Mark()
sys.modules["pytest"] = pytest

namespace = runpy.run_path({str(conftest)!r})
assert namespace["rextio_available"] is False
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _record_paths(monkeypatch, *, in_process_ok=True):
    """Stub the two acquisition primitives; return the ordered call log."""
    calls: list[str] = []

    def fake_in_process(argv):
        calls.append("in_process")
        return dict(CHECK_PAYLOAD)

    def fake_subprocess(binary, argv):
        calls.append("subprocess")
        return dict(CHECK_PAYLOAD)

    monkeypatch.setattr(engine, "_rextio_available_in_process", lambda: in_process_ok)
    monkeypatch.setattr(engine, "_run_in_process", fake_in_process)
    monkeypatch.setattr(engine, "_run_subprocess", fake_subprocess)
    return calls


# --------------------------------------------------------------------------- #
# Acquisition precedence (interpreter path / per-root venv).
# --------------------------------------------------------------------------- #
def test_interpreter_path_prefers_subprocess_over_in_process(tmp_path, monkeypatch):
    # the discovered binary IS the interpreter's own neighbour -> subprocess wins
    interp = tmp_path / "bin" / "python"
    fake = tmp_path / "bin" / "rextio"
    calls = _record_paths(monkeypatch, in_process_ok=True)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: fake)

    eng = engine.Engine()
    eng.interpreter_path = str(interp)  # explicit interpreter -> its rextio wins
    report = eng.check(tmp_path)

    assert report is not None
    # subprocess used even though in-process import is available
    assert calls == ["subprocess"]


def test_interpreter_path_path_fallback_binary_keeps_in_process(tmp_path, monkeypatch):
    # interpreter_path is set, but the discovered binary is a bare PATH fallback
    # NOT next to that interpreter -> it must NOT displace in-process (fix #10).
    interp = tmp_path / "env" / "bin" / "python"
    path_hit = tmp_path / "usr" / "bin" / "rextio"  # different dir than interp
    calls = _record_paths(monkeypatch, in_process_ok=True)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: path_hit)

    eng = engine.Engine()
    eng.interpreter_path = str(interp)
    report = eng.check(tmp_path)

    assert report is not None
    assert calls == ["in_process"]  # PATH fallback did not preempt in-process


def test_project_venv_binary_differs_prefers_subprocess(tmp_path, monkeypatch):
    fake = tmp_path / "other" / "bin" / "rextio"
    calls = _record_paths(monkeypatch, in_process_ok=True)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: fake)
    monkeypatch.setattr(engine, "find_project_venv_binary", lambda root: fake)
    monkeypatch.setattr(engine, "_is_server_environment", lambda b: False)

    eng = engine.Engine()  # no interpreter_path
    report = eng.check(tmp_path)

    assert report is not None
    assert calls == ["subprocess"]


def test_server_environment_binary_prefers_in_process(tmp_path, monkeypatch):
    fake = tmp_path / "rextio"
    calls = _record_paths(monkeypatch, in_process_ok=True)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: fake)
    monkeypatch.setattr(engine, "find_project_venv_binary", lambda root: fake)
    monkeypatch.setattr(engine, "_is_server_environment", lambda b: True)  # same env

    eng = engine.Engine()
    report = eng.check(tmp_path)

    assert report is not None
    assert calls == ["in_process"]


def test_preferred_subprocess_falls_back_to_in_process_on_failure(tmp_path, monkeypatch):
    interp = tmp_path / "bin" / "python"
    fake = tmp_path / "bin" / "rextio"  # the interpreter's neighbour
    calls: list[str] = []

    def raising_subprocess(binary, argv):
        calls.append("subprocess")
        raise engine.AcquisitionError("boom")

    def fake_in_process(argv):
        calls.append("in_process")
        return dict(CHECK_PAYLOAD)

    monkeypatch.setattr(engine, "_rextio_available_in_process", lambda: True)
    monkeypatch.setattr(engine, "_run_subprocess", raising_subprocess)
    monkeypatch.setattr(engine, "_run_in_process", fake_in_process)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: fake)

    eng = engine.Engine()
    eng.interpreter_path = str(interp)  # prefer subprocess...
    report = eng.check(tmp_path)

    assert report is not None
    # subprocess tried first, then in-process fallback
    assert calls == ["subprocess", "in_process"]


# --------------------------------------------------------------------------- #
# _is_server_environment compares via sys.prefix, not resolve(sys.executable)
# (fix #2).
# --------------------------------------------------------------------------- #
def test_is_server_environment_via_sys_prefix_with_symlinked_python(tmp_path, monkeypatch):
    # Production layout: the server venv's python is a symlink escaping to the
    # pyenv base, but rextio sits in <sys.prefix>/bin and resolves in place.
    # Classification must key off sys.prefix, not resolve(sys.executable).
    venv = tmp_path / ".venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    rextio = bindir / "rextio"
    rextio.write_text("", encoding="utf-8")
    # a python symlink pointing OUT of the venv (as pyenv/uv venvs create)
    pyenv_base = tmp_path / "pyenv" / "bin"
    pyenv_base.mkdir(parents=True)
    (pyenv_base / "python").write_text("", encoding="utf-8")
    (bindir / "python").symlink_to(pyenv_base / "python")

    monkeypatch.setattr(engine.sys, "prefix", str(venv))
    assert engine._is_server_environment(rextio) is True


def test_is_server_environment_foreign_venv_is_not_server(tmp_path, monkeypatch):
    server_venv = tmp_path / "server"
    (server_venv / "bin").mkdir(parents=True)
    other_bin = tmp_path / "other" / "bin"
    other_bin.mkdir(parents=True)
    foreign = other_bin / "rextio"
    foreign.write_text("", encoding="utf-8")
    monkeypatch.setattr(engine.sys, "prefix", str(server_venv))
    assert engine._is_server_environment(foreign) is False


# --------------------------------------------------------------------------- #
# _try_subprocess swallows unexpected escapes symmetrically (fix #8).
# --------------------------------------------------------------------------- #
def test_try_subprocess_swallows_unexpected_exception(tmp_path, monkeypatch):
    fake = tmp_path / "rextio"

    def boom(binary, argv):
        raise RuntimeError("unforeseen escape")

    monkeypatch.setattr(engine, "_run_subprocess", boom)
    # a non-AcquisitionError escape is logged and returns None (so the other
    # acquisition path can still be tried), never propagated.
    assert engine._try_subprocess("check", fake, ["check"]) is None


# --------------------------------------------------------------------------- #
# Subprocess fallback path against a real fake binary (fix #12 test gap).
# --------------------------------------------------------------------------- #
def _write_script(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)


def test_run_subprocess_reads_json_from_fake_binary(tmp_path):
    script = tmp_path / "rextio"
    _write_script(
        script,
        "import json\n"
        "print(json.dumps({'contract_version': '1.0.0', 'project_root': '/p', 'modules': []}))\n",
    )
    data = engine._run_subprocess(script, ["check", "/p", "--format", "json"])
    assert data["contract_version"] == "1.0.0"


def test_run_subprocess_nonzero_exit_with_garbage_stdout_raises(tmp_path):
    script = tmp_path / "rextio"
    _write_script(script, "import sys\nsys.stdout.write('not json at all')\nsys.exit(3)\n")
    with pytest.raises(engine.AcquisitionError):
        engine._run_subprocess(script, ["check", "/p", "--format", "json"])


def test_run_subprocess_empty_stdout_raises(tmp_path):
    script = tmp_path / "rextio"
    _write_script(script, "import sys\nsys.stderr.write('kaboom')\nsys.exit(1)\n")
    with pytest.raises(engine.AcquisitionError):
        engine._run_subprocess(script, ["check", "/p", "--format", "json"])


def test_acquire_json_returns_none_when_subprocess_fails_and_no_in_process(tmp_path, monkeypatch):
    script = tmp_path / "rextio"
    _write_script(script, "import sys\nsys.exit(2)\n")
    monkeypatch.setattr(engine, "_rextio_available_in_process", lambda: False)
    monkeypatch.setattr(engine, "find_rextio_binary", lambda root, ip=None: script)
    eng = engine.Engine()
    eng.interpreter_path = str(tmp_path / "python")  # script is its neighbour
    assert eng.check(tmp_path) is None


# --------------------------------------------------------------------------- #
# Composite manifest cache key behaviour in the Engine.
# --------------------------------------------------------------------------- #
def _capabilities_payload(plugins):
    return {
        "contract_version": "1.0.0",
        "config_fingerprint": "fp",
        "rextio_version": "0.1.1",
        "project_root": "/p",
        "rules": [],
        "plugins": plugins,
    }


def test_capabilities_cached_by_composite_key(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_acquire(root, command, extra, *, interpreter_path=None):
        calls.append(command)
        return _capabilities_payload([{"id": "a", "version": "1.0"}])

    monkeypatch.setattr(engine, "_acquire_json", fake_acquire)
    eng = engine.Engine()
    m1 = eng.capabilities(tmp_path)
    m2 = eng.capabilities(tmp_path)
    assert m1 is m2  # cached: identical object
    assert calls.count("capabilities") == 1


def test_capabilities_null_plugin_version_not_cached(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_acquire(root, command, extra, *, interpreter_path=None):
        calls.append(command)
        return _capabilities_payload([{"id": "a", "version": None}])

    monkeypatch.setattr(engine, "_acquire_json", fake_acquire)
    eng = engine.Engine()
    m1 = eng.capabilities(tmp_path)
    m2 = eng.capabilities(tmp_path)
    assert m1 is not m2  # uncacheable -> re-acquired each call
    assert calls.count("capabilities") == 2
