"""Unit tests for severity mapping, diagnostic/hover conversion, and wiring."""

from __future__ import annotations

from lsprotocol import types as lsp

from rextio_lsp.contract import (
    DiagnosticRecord,
    FunctionReport,
    parse_capabilities,
    parse_check_report,
)
from rextio_lsp.server import (
    RextioLanguageServer,
    build_hover_markdown,
    create_server,
    diagnostics_for_file,
    function_at_line,
    map_severity,
    to_lsp_diagnostic,
)

PIPELINE = "/Volumes/Data/workspace/rextio/rextio/examples/boundary_demo/src/boundary_demo/pipeline.py"


def test_map_severity_never_error():
    assert map_severity("RXT070", is_rejection=True) == lsp.DiagnosticSeverity.Warning
    assert map_severity("RXT075", is_rejection=False) == lsp.DiagnosticSeverity.Hint
    assert map_severity("RXT090", is_rejection=False) == lsp.DiagnosticSeverity.Hint
    assert map_severity("RXT073", is_rejection=False) == lsp.DiagnosticSeverity.Information
    # rejection wins even for an otherwise-informational code
    assert map_severity("RXT075", is_rejection=True) == lsp.DiagnosticSeverity.Warning


def test_to_lsp_diagnostic_positions_and_enrichment():
    record = DiagnosticRecord(
        code="RXT070",
        message="native function calls fallback-only function",
        severity="error",
        file_path=PIPELINE,
        line=41,
        column=11,
        suggestion="Mark the dependency as @rextio.native ...",
    )
    diag = to_lsp_diagnostic(record, is_rejection=True, degraded=False)
    assert diag.range.start.line == 40  # 1-based -> 0-based
    assert diag.range.start.character == 11  # already 0-based
    assert diag.source == "rextio"
    assert diag.code == "RXT070"
    assert diag.severity == lsp.DiagnosticSeverity.Warning
    assert "Mark the dependency" in diag.message  # enriched


def test_to_lsp_diagnostic_degraded_omits_guidance():
    record = DiagnosticRecord(
        code="RXT070",
        message="native function calls fallback-only function",
        severity="error",
        file_path=PIPELINE,
        line=41,
        column=11,
        suggestion="do the thing",
    )
    diag = to_lsp_diagnostic(record, is_rejection=True, degraded=True)
    assert diag.message == "native function calls fallback-only function"
    assert "do the thing" not in diag.message


def test_diagnostics_for_file_maps_boundary(check_boundary):
    report = parse_check_report(check_boundary)
    diags = diagnostics_for_file(report, PIPELINE, degraded=False)
    by_code = {d.code: d for d in diags}
    assert by_code["RXT070"].severity == lsp.DiagnosticSeverity.Warning  # rejection
    assert by_code["RXT075"].severity == lsp.DiagnosticSeverity.Hint  # informational
    assert by_code["RXT073"].severity == lsp.DiagnosticSeverity.Information  # advisory
    assert all(d.source == "rextio" for d in diags)


def test_function_at_line(check_boundary):
    report = parse_check_report(check_boundary)
    # compute_rejected is defined on line 37 (1-based) -> LSP line 36
    fn = function_at_line(report, PIPELINE, 36)
    assert fn is not None
    assert fn.qualname == "boundary_demo.pipeline.compute_rejected"
    assert function_at_line(report, PIPELINE, 999) is None


def test_build_hover_markdown_with_guidance(check_boundary, capabilities_boundary):
    report = parse_check_report(check_boundary)
    manifest = parse_capabilities(capabilities_boundary)
    fn = function_at_line(report, PIPELINE, 36)
    assert fn is not None
    md = build_hover_markdown(fn, manifest, degraded=False)
    assert "fallback-python" in md
    assert "rejected" in md
    assert "RXT070" in md
    # guidance text pulled from the manifest rule record
    assert "—" in md


def test_build_hover_markdown_accepted_no_rejections():
    fn = FunctionReport(
        qualname="m.f",
        name="f",
        file_path="/x.py",
        line=1,
        column=0,
        route="native-direct",
        native_status="accepted",
    )
    md = build_hover_markdown(fn, None, degraded=False)
    assert "native-direct" in md
    assert "Rejections" not in md


def test_create_server_registers_only_rextio_features():
    server = create_server()
    registered = set(server.protocol.fm.features.keys())
    assert registered == {
        lsp.TEXT_DOCUMENT_DID_OPEN,
        lsp.TEXT_DOCUMENT_DID_SAVE,
        lsp.TEXT_DOCUMENT_HOVER,
    }
    # explicitly NOT advertised
    for forbidden in (
        lsp.TEXT_DOCUMENT_COMPLETION,
        lsp.TEXT_DOCUMENT_FORMATTING,
        lsp.TEXT_DOCUMENT_RENAME,
        lsp.TEXT_DOCUMENT_DEFINITION,
        lsp.TEXT_DOCUMENT_REFERENCES,
    ):
        assert forbidden not in registered


def test_hover_for_uses_cached_report(tmp_path):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    module = tmp_path / "ops.py"
    module.write_text("x = 1\n", encoding="utf-8")

    server = RextioLanguageServer()
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(tmp_path),
            "modules": [
                {
                    "file_path": str(module),
                    "functions": [
                        {
                            "qualname": "ops.rejected",
                            "name": "rejected",
                            "file_path": str(module),
                            "line": 3,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [],
                        }
                    ],
                }
            ],
        }
    )
    server._reports[str(tmp_path)] = report  # pre-seed cache; no engine call
    server._degraded[str(tmp_path)] = True  # degraded avoids capabilities acquisition

    hover = server.hover_for(module.as_uri(), lsp.Position(line=2, character=0))
    assert hover is not None
    assert "fallback-python" in hover.contents.value
    assert "RXT070" in hover.contents.value

    # a non-definition line yields no hover
    assert server.hover_for(module.as_uri(), lsp.Position(line=50, character=0)) is None
