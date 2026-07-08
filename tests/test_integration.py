"""Integration test against the real rextio contract.

Marked ``needs_rextio`` and auto-skipped when rextio is not importable. Runs the
real in-process acquisition path over a tiny generated project.
"""

from __future__ import annotations

from lsprotocol import types as lsp

from rextio_lsp.contract import is_contract_supported
from rextio_lsp.engine import Engine
from rextio_lsp.server import (
    build_hover_markdown,
    code_lenses_for,
    diagnostics_for_file,
    function_at_line,
)

from conftest import make_tiny_project, needs_rextio, skip_without_rextio

pytestmark = [needs_rextio, skip_without_rextio]


def test_real_check_acquisition_and_diagnostics(tmp_path):
    module = make_tiny_project(tmp_path)
    engine = Engine()

    report = engine.check(tmp_path)
    assert report is not None
    assert is_contract_supported(report.contract_version)

    by_name = {fn.name: fn for fn in report.functions}
    assert by_name["add_one"].native_status == "accepted"
    assert by_name["add_one"].route == "native-direct"

    rejected = by_name["rejected"]
    assert rejected.native_status == "rejected"
    assert rejected.route == "fallback-python"
    assert rejected.rejection_codes  # at least one RXT code

    diags = diagnostics_for_file(report, str(module), degraded=False)
    assert diags
    # the rejection surfaces as a Warning (never Error)
    assert any(d.severity == lsp.DiagnosticSeverity.Warning for d in diags)
    assert all(d.severity != lsp.DiagnosticSeverity.Error for d in diags)
    assert all(d.source == "rextio" for d in diags)


def test_real_capabilities_guidance_and_hover(tmp_path):
    module = make_tiny_project(tmp_path)
    engine = Engine()

    report = engine.check(tmp_path)
    assert report is not None
    manifest = engine.capabilities(tmp_path)
    assert manifest is not None
    assert manifest.config_fingerprint

    # cache returns the identical object for the same config
    assert engine.capabilities(tmp_path) is manifest

    rejected = next(fn for fn in report.functions if fn.name == "rejected")
    line = rejected.line - 1  # to 0-based LSP line
    fn = function_at_line(report, str(module), line)
    assert fn is not None
    md = build_hover_markdown(fn, manifest, degraded=False)
    assert "fallback-python" in md
    for code in rejected.rejection_codes:
        assert code in md


def test_real_code_lens_renders_on_accepted_function(tmp_path):
    module = make_tiny_project(tmp_path)
    engine = Engine()
    report = engine.check(tmp_path)
    assert report is not None

    lenses = code_lenses_for(report, str(module.resolve()))
    assert lenses  # one per analyzed function
    by_arg = {lens.command.arguments[0]: lens for lens in lenses}
    # the accepted function renders a native-direct route lens
    add_one = by_arg["tiny.ops.add_one"]
    assert add_one.command.title == "Rextio: native-direct"
    assert add_one.command.command == "rextio.showRouteInfo"
