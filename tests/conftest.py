"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def check_boundary() -> dict[str, Any]:
    """Real ``check --format json`` from the boundary_demo example."""
    return load_fixture("check_boundary.json")


@pytest.fixture
def check_pure_math() -> dict[str, Any]:
    """Real ``check --format json`` from the pure_math example (no rejections)."""
    return load_fixture("check_pure_math.json")


@pytest.fixture
def capabilities_boundary() -> dict[str, Any]:
    """Real ``capabilities --format json`` for the boundary_demo config."""
    return load_fixture("capabilities_boundary.json")


@pytest.fixture
def check_syntax_error() -> dict[str, Any]:
    """Real ``check --format json`` for a file with a syntax error.

    Zero functions; a single top-level RXT000 parse diagnostic (duplicated in
    the owning module's ``diagnostics``, as rextio emits it).
    """
    return load_fixture("check_syntax_error.json")


rextio_available = importlib.util.find_spec("rextio.cli.main") is not None

needs_rextio = pytest.mark.needs_rextio
skip_without_rextio = pytest.mark.skipif(
    not rextio_available, reason="rextio is not importable in this environment"
)


TINY_MODULE = """\
import rextio


@rextio.native
def add_one(x: int) -> int:
    return x + 1


@rextio.exempt
def helper(xs: list[int]) -> int:
    return sum(xs)


@rextio.native
def rejected(xs: list[int]) -> int:
    return helper(xs)
"""

TINY_TOML = """\
[build]
native_backend = "rust"

[policy]
native_marker = "auto"
require_type_hints = true
"""


def make_tiny_project(root: Path) -> Path:
    """Write a minimal rextio project (one accepted + one rejected fn)."""
    (root / "rextio.toml").write_text(TINY_TOML, encoding="utf-8")
    pkg = root / "src" / "tiny"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    module = pkg / "ops.py"
    module.write_text(TINY_MODULE, encoding="utf-8")
    return module
