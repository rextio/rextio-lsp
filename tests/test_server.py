"""Unit tests for severity mapping, diagnostic/hover conversion, and wiring."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from lsprotocol import types as lsp
from pygls.capabilities import ServerCapabilitiesBuilder
from pygls.workspace import Workspace

from rextio_lsp.contract import (
    DiagnosticRecord,
    FunctionReport,
    parse_capabilities,
    parse_check_report,
)
from rextio_lsp.server import (
    LATENCY_WARN_SECONDS,
    ROUTE_INFO_COMMAND,
    RextioLanguageServer,
    build_hover_markdown,
    code_actions_for,
    code_lenses_for,
    create_server,
    diagnostics_for_file,
    _native_exempt_span,
    find_native_decorator_line,
    function_at_line,
    latency_log,
    map_severity,
    parse_initialization_options,
    to_lsp_diagnostic,
)

PIPELINE = (
    "/Volumes/Data/workspace/rextio/rextio/examples/boundary_demo/src/boundary_demo/pipeline.py"
)


def _setup_workspace(server: RextioLanguageServer, *docs: tuple[str, str]) -> None:
    """Attach a minimal initialized workspace holding the given ``(uri, text)``."""
    ws = Workspace(None, lsp.TextDocumentSyncKind.Full)
    server.protocol._workspace = ws
    for uri, text in docs:
        ws.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="python", version=1, text=text)
        )


def _capture_publishes(server: RextioLanguageServer, monkeypatch) -> dict[str, list]:
    """Record every ``publishDiagnostics`` params keyed by URI."""
    published: dict[str, list] = {}

    def record(params: lsp.PublishDiagnosticsParams) -> None:
        published[params.uri] = list(params.diagnostics)

    monkeypatch.setattr(server, "text_document_publish_diagnostics", record)
    return published


def _server_capabilities(server: RextioLanguageServer) -> lsp.ServerCapabilities:
    """Build the ServerCapabilities pygls would advertise for ``server``."""
    fm = server.protocol.fm
    return ServerCapabilitiesBuilder(
        lsp.ClientCapabilities(),
        set({**fm.features, **fm.builtin_features}.keys()),
        fm.feature_options,
        list(fm.commands.keys()),
        lsp.TextDocumentSyncKind.Incremental,
        None,
        "utf-16",
    ).build()


def _rejected_report(module: str, *, def_line: int, diag_line: int) -> "object":
    """A report with one rejected function carrying an RXT070 diagnostic."""
    return parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(Path(module).parent),
            "modules": [
                {
                    "file_path": module,
                    "functions": [
                        {
                            "qualname": "ops.rejected",
                            "name": "rejected",
                            "file_path": module,
                            "line": def_line,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [
                                {
                                    "code": "RXT070",
                                    "message": "native function calls fallback-only function",
                                    "severity": "error",
                                    "file_path": module,
                                    "line": diag_line,
                                    "column": 4,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def _promotion_report(
    module: str,
    *,
    marker_kind: str = "none",
    status: str = "ineligible",
    provenance: str = "auto",
    skip_reason: str | None = None,
    legacy_diagnostics: list[dict] | None = None,
    assessment_diagnostics: list[dict] | None = None,
    contract_version: str = "2.2.0",
) -> "object":
    """Build one coherent tooling-contract 2.2 function report."""
    if assessment_diagnostics is None:
        assessment_diagnostics = [
            {
                "kind": "blocker",
                "code": "RXT001",
                "message": "native promotion requires resolved types",
                "suggestion": "Add supported type annotations.",
                "line": 2,
                "column": 0,
                "end_line": 2,
                "end_column": 10,
            }
        ]
    if status == "skipped":
        assessment_diagnostics = []
    codes = sorted({record["code"] for record in assessment_diagnostics})
    return parse_check_report(
        {
            "contract_version": contract_version,
            "project_root": str(Path(module).parent),
            "modules": [
                {
                    "file_path": module,
                    "functions": [
                        {
                            "qualname": "ops.auto_bad",
                            "name": "auto_bad",
                            "file_path": module,
                            "line": 2,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "not-candidate",
                            "rejection_codes": [],
                            "diagnostics": legacy_diagnostics or [],
                            "marker_kind": marker_kind,
                            "promotion_assessment": {
                                "status": status,
                                "provenance": provenance,
                                "diagnostic_codes": codes,
                                "diagnostics": assessment_diagnostics,
                                "skip_reason": skip_reason,
                            },
                            "source_range": {
                                "start": {"line": 2, "column": 0},
                                "end": {"line": 3, "column": 12},
                            },
                            "name_range": {
                                "start": {"line": 2, "column": 4},
                                "end": {"line": 2, "column": 12},
                            },
                        }
                    ],
                }
            ],
        }
    )


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


def test_assessment_blocker_is_warning_with_producer_suggestion():
    module = "/proj/ops.py"
    report = _promotion_report(module)
    diags = diagnostics_for_file(report, module, degraded=False)
    assert len(diags) == 1
    assert diags[0].code == "RXT001"
    assert diags[0].severity == lsp.DiagnosticSeverity.Warning
    assert "Add supported type annotations." in diags[0].message
    assert diags[0].severity != lsp.DiagnosticSeverity.Error


def test_assessment_advisory_uses_hint_policy_and_manifest_guidance():
    module = "/proj/ops.py"
    report = _promotion_report(
        module,
        status="eligible",
        assessment_diagnostics=[
            {
                "kind": "advisory",
                "code": "RXT075",
                "message": "scalar boundary call",
                "suggestion": None,
                "line": 3,
                "column": 4,
                "end_line": None,
                "end_column": None,
            }
        ],
    )
    manifest = parse_capabilities(
        {
            "contract_version": "2.2.0",
            "rules": [
                {
                    "id": "core.scalar-boundary",
                    "provider": "core",
                    "diagnostic_code": "RXT075",
                    "constraint": "scalar only",
                    "guidance": "Keep the boundary scalar and typed.",
                    "outcome": "note",
                    "stability": "stable",
                }
            ],
        }
    )
    diags = diagnostics_for_file(
        report,
        module,
        degraded=False,
        manifest=manifest,
    )
    assert len(diags) == 1
    assert diags[0].severity == lsp.DiagnosticSeverity.Hint
    assert "Keep the boundary scalar and typed." in diags[0].message


def test_assessment_diagnostic_dedups_matching_legacy_six_field_key():
    module = "/proj/ops.py"
    legacy = {
        "code": "RXT001",
        "message": "native promotion requires resolved types",
        "severity": "error",
        "file_path": module,
        "line": 2,
        "column": 0,
        "end_line": 2,
        "end_column": 10,
        "suggestion": "Add supported type annotations.",
    }
    report = _promotion_report(module, legacy_diagnostics=[legacy])
    diags = diagnostics_for_file(report, module, degraded=False)
    assert len(diags) == 1
    assert diags[0].code == "RXT001"


def test_assessment_diagnostic_with_different_span_is_not_deduped():
    module = "/proj/ops.py"
    legacy = {
        "code": "RXT001",
        "message": "native promotion requires resolved types",
        "severity": "error",
        "file_path": module,
        "line": 2,
        "column": 0,
        "end_line": 2,
        "end_column": 9,
    }
    report = _promotion_report(module, legacy_diagnostics=[legacy])
    assert len(diagnostics_for_file(report, module, degraded=False)) == 2


def test_exact_exempt_suppresses_assessment_but_keeps_legacy_diagnostic():
    module = "/proj/ops.py"
    legacy = {
        "code": "RXT091",
        "message": "unrelated analyzer note",
        "severity": "info",
        "file_path": module,
        "line": 3,
        "column": 4,
    }
    report = _promotion_report(
        module,
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
        legacy_diagnostics=[legacy],
    )
    diags = diagnostics_for_file(report, module, degraded=False)
    assert [diag.code for diag in diags] == ["RXT091"]


def test_function_at_line(check_boundary):
    report = parse_check_report(check_boundary)
    # compute_rejected is defined on line 37 (1-based) -> LSP line 36
    fn = function_at_line(report, PIPELINE, 36)
    assert fn is not None
    assert fn.qualname == "boundary_demo.pipeline.compute_rejected"
    assert function_at_line(report, PIPELINE, 999) is None


def test_function_at_line_uses_utf16_name_range_when_character_supplied():
    module = "/proj/ops.py"
    report = parse_check_report(
        {
            "contract_version": "2.2.0",
            "modules": [
                {
                    "functions": [
                        {
                            "qualname": "ops.함수",
                            "name": "함수",
                            "file_path": module,
                            "line": 1,
                            "column": 0,
                            "route": "native-direct",
                            "native_status": "accepted",
                            "source_range": {
                                "start": {"line": 1, "column": 0},
                                "end": {"line": 2, "column": 12},
                            },
                            "name_range": {
                                "start": {"line": 1, "column": 4},
                                "end": {"line": 1, "column": 10},
                            },
                        }
                    ]
                }
            ],
        }
    )
    lines = ["def 함수(x):", "    return x"]
    assert function_at_line(report, module, 0, character=4, lines=lines) is not None
    assert function_at_line(report, module, 0, character=5, lines=lines) is not None
    assert function_at_line(report, module, 0, character=3, lines=lines) is None
    assert function_at_line(report, module, 0, character=6, lines=lines) is None


def test_unsupported_major_ignores_name_range_and_uses_legacy_def_line():
    module = "/proj/ops.py"
    report = _promotion_report(module, contract_version="3.0.0")
    # The character is outside the serialized name range, but unsupported-major
    # additions are ignored and the legacy def-line lookup remains available.
    assert function_at_line(report, module, 1, character=99, lines=["", "def auto_bad():"]) \
        is not None


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


def test_build_hover_markdown_renders_failed_auto_guidance():
    report = _promotion_report("/proj/ops.py")
    md = build_hover_markdown(report.functions[0], None, degraded=False)
    assert "Promotion assessment:** `ineligible`" in md
    assert "Assessment source:** `auto`" in md
    assert "Promotion blockers" in md
    assert "RXT001" in md
    assert "native promotion requires resolved types" in md
    assert "Add supported type annotations." in md


def test_build_hover_markdown_renders_skipped_reason():
    report = _promotion_report(
        "/proj/ops.py",
        status="skipped",
        provenance="structural-skip",
        skip_reason="method-auto-promotion-not-supported",
    )
    md = build_hover_markdown(report.functions[0], None, degraded=False)
    assert "Promotion assessment:** `skipped`" in md
    assert "method auto-promotion is not supported" in md


def test_build_hover_markdown_degraded_ignores_assessment():
    report = _promotion_report("/proj/ops.py", contract_version="3.0.0")
    md = build_hover_markdown(report.functions[0], None, degraded=True)
    assert "fallback-python" in md
    assert "Promotion assessment" not in md
    assert "RXT001" not in md


def test_create_server_registers_only_rextio_features():
    server = create_server()
    registered = set(server.protocol.fm.features.keys())
    # Static registrations: the initialize hooks, the document triggers, hover,
    # and the rextio.toml watch. Code lens/action are registered at initialize.
    assert registered == {
        lsp.INITIALIZE,
        lsp.INITIALIZED,
        lsp.TEXT_DOCUMENT_DID_OPEN,
        lsp.TEXT_DOCUMENT_DID_SAVE,
        lsp.TEXT_DOCUMENT_DID_CLOSE,
        lsp.TEXT_DOCUMENT_HOVER,
        lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES,
    }
    # code lens / code action are not registered until initialize
    assert lsp.TEXT_DOCUMENT_CODE_LENS not in registered
    assert lsp.TEXT_DOCUMENT_CODE_ACTION not in registered
    # explicitly NOT advertised, ever
    for forbidden in (
        lsp.TEXT_DOCUMENT_COMPLETION,
        lsp.TEXT_DOCUMENT_FORMATTING,
        lsp.TEXT_DOCUMENT_RENAME,
        lsp.TEXT_DOCUMENT_DEFINITION,
        lsp.TEXT_DOCUMENT_REFERENCES,
    ):
        assert forbidden not in registered


# --------------------------------------------------------------------------- #
# initializationOptions parsing / defaults.
# --------------------------------------------------------------------------- #
def test_parse_initialization_options_defaults():
    # missing / empty / non-dict all fall back to the documented defaults
    for raw in (None, {}, "nonsense", {"other": 1}):
        opts = parse_initialization_options(raw)
        assert opts.code_lens_enabled is True
        assert opts.interpreter_path is None


def test_parse_initialization_options_full_shape():
    opts = parse_initialization_options(
        {"codeLens": {"enable": False}, "interpreter": {"path": "/opt/py/bin/python"}}
    )
    assert opts.code_lens_enabled is False
    assert opts.interpreter_path == "/opt/py/bin/python"


def test_parse_initialization_options_partial_and_null_path():
    opts = parse_initialization_options({"interpreter": {"path": None}})
    assert opts.code_lens_enabled is True  # default when codeLens omitted
    assert opts.interpreter_path is None
    # blank string path is treated as unset
    assert parse_initialization_options({"interpreter": {"path": "   "}}).interpreter_path is None


# --------------------------------------------------------------------------- #
# Conditional code-lens capability by option.
# --------------------------------------------------------------------------- #
def test_code_lens_capability_present_when_enabled():
    server = create_server()
    server.apply_initialization_options({"codeLens": {"enable": True}})
    assert lsp.TEXT_DOCUMENT_CODE_LENS in server.protocol.fm.features
    assert lsp.TEXT_DOCUMENT_CODE_ACTION in server.protocol.fm.features
    caps = _server_capabilities(server)
    assert caps.code_lens_provider is not None
    assert caps.code_action_provider is not None


def test_code_lens_capability_absent_when_disabled():
    server = create_server()
    server.apply_initialization_options({"codeLens": {"enable": False}})
    assert lsp.TEXT_DOCUMENT_CODE_LENS not in server.protocol.fm.features
    # code actions are always registered, regardless of the code-lens option
    assert lsp.TEXT_DOCUMENT_CODE_ACTION in server.protocol.fm.features
    caps = _server_capabilities(server)
    assert caps.code_lens_provider is None
    assert caps.code_action_provider is not None


def test_apply_initialization_options_sets_engine_interpreter_path():
    server = create_server()
    server.apply_initialization_options({"interpreter": {"path": "/opt/py/bin/python"}})
    assert server.engine.interpreter_path == "/opt/py/bin/python"


# --------------------------------------------------------------------------- #
# Code lens content.
# --------------------------------------------------------------------------- #
def test_code_lenses_for_titles_and_args(check_boundary):
    report = parse_check_report(check_boundary)
    lenses = code_lenses_for(report, PIPELINE)
    titles = {lens.command.title for lens in lenses}
    assert "Rextio: native-direct" in titles
    assert "Rextio: fallback-python" in titles
    # one lens per analyzed function; all carry the no-op command + qualname arg
    assert len(lenses) == len(report.functions_in_file(PIPELINE))
    for lens in lenses:
        assert lens.command.command == ROUTE_INFO_COMMAND
        assert len(lens.command.arguments) == 1
    # square is defined on line 5 (1-based) -> LSP line 4
    square = next(
        lens for lens in lenses if lens.command.arguments == ["boundary_demo.pipeline.square"]
    )
    assert square.range.start.line == 4


def test_code_lens_contract_22_has_route_assessment_and_source_anchor():
    module = "/proj/ops.py"
    report = _promotion_report(module)
    (lens,) = code_lenses_for(report, module, lines=["", "def auto_bad(x):", "    return x"])
    assert lens.command.title == "Rextio: fallback-python · ineligible"
    assert lens.command.command == ROUTE_INFO_COMMAND
    assert lens.command.arguments == ["ops.auto_bad"]
    assert isinstance(lens.command.arguments[0], str)
    assert (lens.range.start.line, lens.range.start.character) == (1, 0)


def test_code_lens_contract_22_skipped_includes_human_reason():
    module = "/proj/ops.py"
    report = _promotion_report(
        module,
        status="skipped",
        provenance="structural-skip",
        skip_reason="async-auto-promotion-not-supported",
    )
    (lens,) = code_lenses_for(report, module)
    assert "fallback-python" in lens.command.title
    assert "skipped" in lens.command.title
    assert "async auto-promotion is not supported" in lens.command.title


def test_code_lens_exact_exempt_is_suppressed():
    module = "/proj/ops.py"
    report = _promotion_report(
        module,
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
    )
    assert code_lenses_for(report, module) == []


def test_code_lens_unsupported_major_ignores_additions_and_exempt():
    module = "/proj/ops.py"
    report = _promotion_report(
        module,
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
        contract_version="3.0.0",
    )
    (lens,) = code_lenses_for(report, module)
    assert lens.command.title == "Rextio: fallback-python"
    assert lens.range.start.line == 1
    assert lens.command.arguments == ["ops.auto_bad"]


def test_code_lens_contract_21_same_named_fields_are_not_authoritative():
    module = "/proj/ops.py"
    report = _promotion_report(
        module,
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
        contract_version="2.1.0",
    )
    (fn,) = report.functions
    assert fn.marker_kind == "none"
    assert fn.promotion_assessment is None
    assert fn.source_range is None
    assert fn.name_range is None
    (lens,) = code_lenses_for(report, module)
    assert lens.command.title == "Rextio: fallback-python"


# --------------------------------------------------------------------------- #
# Exempt quick-fix code action.
# --------------------------------------------------------------------------- #
def test_code_action_exempt_offered_and_preserves_indentation():
    module = "/proj/ops.py"
    # a rejected method inside a class: decorator indented four spaces
    text = (
        "class C:\n"
        "    @rextio.native\n"
        "    def rejected(self, xs: list[int]) -> int:\n"
        "        return helper(xs)\n"
    )
    report = _rejected_report(module, def_line=3, diag_line=4)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    assert len(actions) == 1
    action = actions[0]
    assert action.title == "Rextio: keep on Python fallback (@rextio.exempt)"
    assert action.kind == lsp.CodeActionKind.QuickFix
    (edit,) = action.edit.changes["file:///proj/ops.py"]
    # only the decorator token span is replaced (indentation preserved outside it)
    assert edit.new_text == "@rextio.exempt"
    assert edit.range.start.line == 1  # the decorator line (0-based)
    assert edit.range.start.character == 4  # after the four-space indent
    assert edit.range.end.character == 4 + len("@rextio.native")


def test_code_action_not_offered_for_non_rejection_diagnostic():
    # an accepted function with an advisory diagnostic gets no quick fix
    module = "/proj/ops.py"
    text = "@rextio.native\ndef accepted(x: int) -> int:\n    return x + 1\n"
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "modules": [
                {
                    "file_path": module,
                    "functions": [
                        {
                            "qualname": "ops.accepted",
                            "name": "accepted",
                            "file_path": module,
                            "line": 2,
                            "column": 0,
                            "route": "native-direct",
                            "native_status": "accepted",
                            "rejection_codes": [],
                            "diagnostics": [
                                {
                                    "code": "RXT075",
                                    "message": "scalar boundary call",
                                    "severity": "info",
                                    "file_path": module,
                                    "line": 3,
                                    "column": 4,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    assert actions == []


def test_code_action_not_offered_without_native_decorator():
    # rejected but auto-discovered (no explicit @rextio.native line) -> no fix
    module = "/proj/ops.py"
    text = "def rejected(xs: list[int]) -> int:\n    return helper(xs)\n"
    report = _rejected_report(module, def_line=1, diag_line=2)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    assert actions == []


def test_code_action_not_offered_with_two_native_decorators():
    module = "/proj/ops.py"
    text = (
        "@rextio.native\n"
        "@rextio.native\n"
        "def rejected(xs: list[int]) -> int:\n"
        "    return helper(xs)\n"
    )
    report = _rejected_report(module, def_line=3, diag_line=4)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    assert actions == []


def test_find_native_decorator_line_in_stack():
    # a stacked decorator block: native above another decorator, one native only
    lines = ["@other", "@rextio.native", "@other2", "def f():", "    pass"]
    # def on 1-based line 4; block above holds exactly one native decorator
    assert find_native_decorator_line(lines, 4) == 1


# --------------------------------------------------------------------------- #
# Advisory hover section.
# --------------------------------------------------------------------------- #
def test_build_hover_markdown_advisory_section(check_boundary, capabilities_boundary):
    report = parse_check_report(check_boundary)
    manifest = parse_capabilities(capabilities_boundary)
    # compute_boundary is accepted but carries an advisory RXT075
    fn = next(f for f in report.functions if f.name == "compute_boundary")
    md = build_hover_markdown(fn, manifest, degraded=False)
    assert "**Advisory:**" in md
    assert "RXT075" in md
    assert "Rejections" not in md  # accepted function -> no rejection section
    # guidance text pulled from the manifest rule record
    assert "—" in md


def test_build_hover_markdown_advisory_omitted_when_none():
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
    assert "Advisory" not in md


# --------------------------------------------------------------------------- #
# rextio.toml watch -> invalidation.
# --------------------------------------------------------------------------- #
def test_watched_files_change_invalidates_cache(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    # seed the engine capability cache and the report cache for this root
    server.engine._key_by_root[str(root)] = "k1"
    server.engine._manifest_by_key["k1"] = object()  # type: ignore[assignment]
    server._reports[str(root)] = parse_check_report({"contract_version": "1.0.0"})
    # a fake notification about this project's rextio.toml
    monkeypatch.setattr(server, "_debounce", lambda _root: None)  # no timer in test
    params = lsp.DidChangeWatchedFilesParams(
        changes=[
            lsp.FileEvent(uri=(tmp_path / "rextio.toml").as_uri(), type=lsp.FileChangeType.Changed)
        ]
    )
    server.handle_watched_files_change(params)
    assert str(root) not in server.engine._key_by_root
    assert "k1" not in server.engine._manifest_by_key
    assert str(root) not in server._reports


def test_watched_files_change_ignores_non_toml(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    server.engine._key_by_root[str(root)] = "k1"
    calls = []
    monkeypatch.setattr(server, "_debounce", lambda r: calls.append(r))
    params = lsp.DidChangeWatchedFilesParams(
        changes=[lsp.FileEvent(uri=(tmp_path / "ops.py").as_uri(), type=lsp.FileChangeType.Changed)]
    )
    server.handle_watched_files_change(params)
    assert str(root) in server.engine._key_by_root  # untouched
    assert calls == []


# --------------------------------------------------------------------------- #
# Latency instrumentation.
# --------------------------------------------------------------------------- #
def test_latency_log_thresholds():
    slow_type, slow_msg = latency_log(Path("/proj"), LATENCY_WARN_SECONDS + 0.5)
    assert slow_type == lsp.MessageType.Info
    assert slow_msg == "rextio check /proj: 2.50s"
    fast_type, fast_msg = latency_log(Path("/proj"), 0.84)
    assert fast_type == lsp.MessageType.Log
    assert fast_msg == "rextio check /proj: 0.84s"


def test_analyze_project_records_last_duration(monkeypatch):
    server = RextioLanguageServer()
    # an unsupported-major report skips the capabilities warm-up; publish is stubbed out
    report = parse_check_report({"contract_version": "3.0.0", "project_root": "/proj"})
    monkeypatch.setattr(server.engine, "check", lambda _root: report)
    monkeypatch.setattr(server, "_publish_for_project", lambda *a, **k: None)
    logs = []
    monkeypatch.setattr(server, "window_log_message", lambda p: logs.append(p))
    server.analyze_project(Path("/proj"))
    key = str(Path("/proj").resolve())
    assert key in server._last_duration
    assert server._last_duration[key] >= 0.0
    assert logs and logs[0].message.startswith("rextio check")


# --------------------------------------------------------------------------- #
# Real-span diagnostic ranges.
# --------------------------------------------------------------------------- #
def test_to_lsp_diagnostic_uses_real_span_when_present():
    record = DiagnosticRecord(
        code="RXT070",
        message="msg",
        severity="error",
        file_path="/x.py",
        line=41,
        column=11,
        end_line=41,
        end_column=24,
    )
    diag = to_lsp_diagnostic(record, is_rejection=True, degraded=False)
    assert diag.range.start.line == 40
    assert diag.range.start.character == 11
    assert diag.range.end.line == 40
    assert diag.range.end.character == 24  # real span, not zero-width


def test_to_lsp_diagnostic_zero_width_without_span():
    record = DiagnosticRecord(
        code="RXT070",
        message="msg",
        severity="error",
        file_path="/x.py",
        line=41,
        column=11,
    )
    diag = to_lsp_diagnostic(record, is_rejection=True, degraded=False)
    assert diag.range.start == diag.range.end


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


def test_hover_for_contract_22_uses_name_range(tmp_path):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    module = tmp_path / "ops.py"
    text = "\ndef auto_bad(x):\n    return x\n"
    module.write_text(text, encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (module.as_uri(), text))
    report = _promotion_report(str(module))
    server._reports[str(tmp_path)] = report
    server._degraded[str(tmp_path)] = False
    server.engine.capabilities = lambda _root: None  # type: ignore[method-assign]

    # Cursor on the identifier: hover includes assessment and returns the exact
    # identifier range. Cursor on `def` is outside the name range.
    hover = server.hover_for(module.as_uri(), lsp.Position(line=1, character=4))
    assert hover is not None
    assert hover.range == lsp.Range(
        start=lsp.Position(line=1, character=4),
        end=lsp.Position(line=1, character=12),
    )
    assert "Promotion blockers" in hover.contents.value
    assert server.hover_for(module.as_uri(), lsp.Position(line=1, character=1)) is None


def test_hover_for_exact_exempt_suppressed_but_unsupported_major_not_suppressed(tmp_path):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    module = tmp_path / "ops.py"
    text = "\ndef auto_bad(x):\n    return x\n"
    module.write_text(text, encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (module.as_uri(), text))

    exempt = _promotion_report(
        str(module),
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
    )
    server._reports[str(tmp_path)] = exempt
    server._degraded[str(tmp_path)] = False
    assert server.hover_for(module.as_uri(), lsp.Position(line=1, character=4)) is None

    unsupported = _promotion_report(
        str(module),
        marker_kind="exempt",
        status="skipped",
        provenance="explicit-exempt",
        skip_reason="explicit-exemption",
        contract_version="3.0.0",
    )
    server._reports[str(tmp_path)] = unsupported
    server._degraded[str(tmp_path)] = True
    hover = server.hover_for(module.as_uri(), lsp.Position(line=1, character=99))
    assert hover is not None
    assert hover.range is None
    assert "Promotion assessment" not in hover.contents.value


# --------------------------------------------------------------------------- #
# Top-level / module diagnostics are surfaced (fix #2).
# --------------------------------------------------------------------------- #
def test_diagnostics_for_file_surfaces_top_level(check_syntax_error):
    report = parse_check_report(check_syntax_error)
    diags = diagnostics_for_file(report, "/proj/src/broken/bad.py", degraded=False)
    assert len(diags) == 1
    assert diags[0].code == "RXT000"
    # top-level parse error is not a function rejection -> Information, never Error
    assert diags[0].severity == lsp.DiagnosticSeverity.Information


def test_diagnostics_for_file_dedups_top_level_against_function():
    module = "/proj/m.py"
    dup = {
        "code": "RXT070",
        "message": "dup",
        "severity": "error",
        "file_path": module,
        "line": 5,
        "column": 4,
    }
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "diagnostics": [dict(dup)],  # also emitted at top level
            "modules": [
                {
                    "file_path": module,
                    "functions": [
                        {
                            "qualname": "m.f",
                            "name": "f",
                            "file_path": module,
                            "line": 5,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [dict(dup)],
                        }
                    ],
                }
            ],
        }
    )
    diags = diagnostics_for_file(report, module, degraded=False)
    keys = [(d.code, d.range.start.line, d.range.start.character) for d in diags]
    assert keys.count(("RXT070", 4, 4)) == 1  # top-level copy suppressed


def test_publish_surfaces_top_level_for_open_doc(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    bad = root / "bad.py"
    bad.write_text("def broken(\n", encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (bad.as_uri(), "def broken(\n"))
    published = _capture_publishes(server, monkeypatch)
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(root),
            "diagnostics": [
                {
                    "code": "RXT000",
                    "message": "Python parse error",
                    "severity": "error",
                    "file_path": str(bad),
                    "line": 1,
                    "column": 11,
                }
            ],
            "modules": [{"file_path": str(bad), "functions": []}],
        }
    )
    server._publish_for_project(root, report, degraded=False)
    assert bad.as_uri() in published
    assert [d.code for d in published[bad.as_uri()]] == ["RXT000"]


def test_publish_project_scope_diagnostic_to_toml(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    _setup_workspace(server)  # no open docs
    published = _capture_publishes(server, monkeypatch)
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(root),
            "diagnostics": [
                {
                    "code": "RXT091",
                    "message": "project-scope note",
                    "severity": "info",
                    "file_path": "",  # no file -> project scope
                    "line": 1,
                    "column": 0,
                }
            ],
        }
    )
    server._publish_for_project(root, report, degraded=False)
    toml_uri = (root / "rextio.toml").as_uri()
    assert toml_uri in published
    assert [d.code for d in published[toml_uri]] == ["RXT091"]


# --------------------------------------------------------------------------- #
# Stale diagnostics are cleared (fix #3).
# --------------------------------------------------------------------------- #
def test_did_close_clears_diagnostics(monkeypatch):
    server = RextioLanguageServer()
    published = _capture_publishes(server, monkeypatch)
    server._published_uris["/root"] = {"file:///x.py"}
    server.handle_did_close("file:///x.py")
    assert published["file:///x.py"] == []
    assert "file:///x.py" not in server._published_uris["/root"]


def test_analyze_none_clears_stale_report_and_diagnostics(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    published = _capture_publishes(server, monkeypatch)
    server._reports[str(root)] = parse_check_report({"contract_version": "1.0.0"})
    server._published_uris[str(root)] = {"file:///x.py"}
    monkeypatch.setattr(server.engine, "check", lambda _root: None)  # became unavailable
    monkeypatch.setattr(server, "window_log_message", lambda _p: None)

    assert server.analyze_project(root) is None
    assert published["file:///x.py"] == []  # stale diagnostics cleared
    assert str(root) not in server._reports


def test_toml_deleted_clears_project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    published = _capture_publishes(server, monkeypatch)
    server._reports[str(root)] = parse_check_report({"contract_version": "1.0.0"})
    server._published_uris[str(root)] = {"file:///x.py"}
    server.engine._key_by_root[str(root)] = "k1"
    debounced: list[str] = []
    monkeypatch.setattr(server, "_debounce", lambda r: debounced.append(r))
    params = lsp.DidChangeWatchedFilesParams(
        changes=[
            lsp.FileEvent(uri=(tmp_path / "rextio.toml").as_uri(), type=lsp.FileChangeType.Deleted)
        ]
    )
    server.handle_watched_files_change(params)
    assert published["file:///x.py"] == []
    assert str(root) not in server._reports
    assert str(root) not in server.engine._key_by_root
    assert debounced == []  # a deletion does not re-debounce an analysis


def test_publish_clears_dropped_uris_on_reanalysis(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    _setup_workspace(server)  # no docs -> nothing republished
    published = _capture_publishes(server, monkeypatch)
    # a URI was published last time but is no longer present this round
    server._published_uris[str(root)] = {"file:///gone.py"}
    report = parse_check_report({"contract_version": "1.0.0", "project_root": str(root)})
    server._publish_for_project(root, report, degraded=False)
    assert published["file:///gone.py"] == []  # cleared


# --------------------------------------------------------------------------- #
# No synchronous analysis in request paths + in-flight guard (fix #5).
# --------------------------------------------------------------------------- #
def test_hover_report_miss_schedules_and_returns_none(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    module = root / "ops.py"
    module.write_text("x = 1\n", encoding="utf-8")
    server = RextioLanguageServer()
    scheduled: list[str] = []
    monkeypatch.setattr(server, "schedule_analysis_for_uri", lambda uri: scheduled.append(uri))

    def _fail(_root):
        raise AssertionError("must not analyze synchronously in the request path")

    monkeypatch.setattr(server, "analyze_project", _fail)

    hover = server.hover_for(module.as_uri(), lsp.Position(line=0, character=0))
    assert hover is None
    assert scheduled == [module.as_uri()]


def test_in_flight_guard_prevents_duplicate_analysis(monkeypatch):
    server = RextioLanguageServer()
    calls: list[Path] = []
    monkeypatch.setattr(server, "analyze_project", lambda p: calls.append(p))
    server._analyzing.add("/proj")  # an analysis is already running
    server._run_analysis("/proj")
    assert calls == []  # no duplicate concurrent analysis
    assert "/proj" in server._rerun_pending  # re-armed for after it finishes


def test_rapid_saves_coalesce_to_single_analysis(monkeypatch):
    server = RextioLanguageServer()
    runs: list[str] = []
    monkeypatch.setattr(server, "_run_analysis", lambda root: runs.append(root))
    monkeypatch.setattr("rextio_lsp.server.DEBOUNCE_SECONDS", 0.05)
    for _ in range(5):  # five rapid saves
        server._debounce("/proj")
    time.sleep(0.25)
    assert runs == ["/proj"]  # coalesced to one


# --------------------------------------------------------------------------- #
# Multi-root publish isolation (fix #12 test gap).
# --------------------------------------------------------------------------- #
def test_multi_root_publish_isolation(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for r in (a, b):
        r.mkdir()
        (r / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    a_root, b_root = a.resolve(), b.resolve()
    a_mod, b_mod = a_root / "x.py", b_root / "y.py"
    a_mod.write_text("x = 1\n", encoding="utf-8")
    b_mod.write_text("y = 1\n", encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (a_mod.as_uri(), "x = 1\n"), (b_mod.as_uri(), "y = 1\n"))
    published = _capture_publishes(server, monkeypatch)
    report = parse_check_report(
        {"contract_version": "1.0.0", "project_root": str(a_root), "modules": []}
    )
    server._publish_for_project(a_root, report, degraded=False)
    assert a_mod.as_uri() in published  # only root A's doc touched
    assert b_mod.as_uri() not in published


# --------------------------------------------------------------------------- #
# UTF-8 byte offset -> UTF-16 conversion applied to ranges (fix #6).
# --------------------------------------------------------------------------- #
def test_diagnostics_for_file_converts_byte_offset_to_utf16():
    module = "/p/m.py"
    line = 'x = "한글" + f(1)'  # `f` at UTF-8 byte 15, UTF-16 index 11
    # Non-RXT000: 0-based UTF-8 byte column on every supported major.
    record = {
        "code": "RXT070",
        "message": "m",
        "severity": "error",
        "file_path": module,
        "line": 1,
        "column": 15,
    }
    for version in ("1.0.0", "2.0.0"):
        report = parse_check_report({"contract_version": version, "diagnostics": [record]})
        # no lines -> raw byte offset fallback
        assert diagnostics_for_file(report, module, degraded=False)[0].range.start.character == 15
        # with the document line -> converted to the UTF-16 index
        converted = diagnostics_for_file(report, module, degraded=False, lines=[line])
        assert converted[0].range.start.character == 11


def test_diagnostics_for_file_rxt000_contract2_is_utf8_byte_offset():
    # Contract 2.x: RXT000 uses the same 0-based UTF-8 byte column as every
    # other code. A non-BMP char (𝐀 = U+1D400, 4 UTF-8 bytes / 2 UTF-16 units)
    # makes byte offset, code-point index, and UTF-16 index all diverge.
    module = "/p/m.py"
    line = "𝐀 = ("  # '(' at UTF-8 byte 7; prefix "𝐀 = " is 5 UTF-16 units
    record = {
        "code": "RXT000",
        "message": "invalid syntax",
        "severity": "error",
        "file_path": module,
        "line": 1,
        "column": 7,  # 0-based UTF-8 byte offset of '('
    }
    report = parse_check_report({"contract_version": "2.0.0", "diagnostics": [record]})
    # without line text: raw byte offset fallback
    assert diagnostics_for_file(report, module, degraded=False)[0].range.start.character == 7
    # with line text: byte 7 -> UTF-16 index 5 (𝐀 is a surrogate pair)
    converted = diagnostics_for_file(report, module, degraded=False, lines=[line])
    assert converted[0].range.start.character == 5


def test_diagnostics_for_file_rxt000_contract1_is_one_based_code_point():
    # Contract 1.x legacy: RXT000.column is SyntaxError.offset (1-based code
    # points). Same astral line: '(' is the 5th code point (1-based), UTF-16 5.
    module = "/p/m.py"
    line = "𝐀 = ("
    record = {
        "code": "RXT000",
        "message": "invalid syntax",
        "severity": "error",
        "file_path": module,
        "line": 1,
        "column": 5,  # 1-based code point pointing at '('
    }
    report = parse_check_report({"contract_version": "1.0.0", "diagnostics": [record]})
    # without line text: subtract 1, pass through
    assert diagnostics_for_file(report, module, degraded=False)[0].range.start.character == 4
    # with line text: 4 code points -> 5 UTF-16 units (𝐀 is a surrogate pair)
    converted = diagnostics_for_file(report, module, degraded=False, lines=[line])
    assert converted[0].range.start.character == 5


def test_diagnostics_for_file_rxt000_ascii_line_placement_both_contracts():
    # ASCII: 0-based byte column for major 2; 1-based code point for major 1.
    module = "/p/m.py"
    line = "def broken("  # '(' at 0-based index 10
    record2 = {
        "code": "RXT000",
        "message": "invalid syntax",
        "severity": "error",
        "file_path": module,
        "line": 1,
        "column": 10,
    }
    report2 = parse_check_report({"contract_version": "2.0.0", "diagnostics": [record2]})
    assert (
        diagnostics_for_file(report2, module, degraded=False, lines=[line])[0].range.start.character
        == 10
    )
    record1 = {**record2, "column": 11}  # 1-based code point at '('
    report1 = parse_check_report({"contract_version": "1.0.0", "diagnostics": [record1]})
    assert (
        diagnostics_for_file(report1, module, degraded=False, lines=[line])[0].range.start.character
        == 10
    )


def test_cross_version_rxt000_maps_to_same_utf16_character():
    """Smoke: legacy 1.x and new 2.x RXT000 columns land on the same UTF-16 char.

    Real CPython SyntaxError on a line where code points, UTF-8 bytes, and
    UTF-16 units all diverge (Korean BMP + astral emoji before the error site).
    """
    module = "/p/m.py"
    source = 'x = "한글😀" + ('  # unclosed '(' after 한글 + 😀
    try:
        compile(source, module, "exec")
    except SyntaxError as exc:
        err = exc  # keep after except (PEP 3110 clears the as-target)
    else:
        raise AssertionError(f"expected SyntaxError for {source!r}")
    assert err.offset is not None
    assert err.lineno == 1
    line = (err.text or source).rstrip("\n")
    # CPython 3.11–3.14: offset 13 points at '(' (1-based code point).
    assert err.offset == 13
    assert line == source
    prefix = line[: err.offset - 1]
    utf8_column = len(prefix.encode("utf-8"))
    assert utf8_column == 19
    # Adversarial lock: three unit systems disagree on the numeric value.
    assert len(prefix) == 12  # 0-based code points
    assert utf8_column == 19
    utf16_at_error = sum(2 if ord(ch) > 0xFFFF else 1 for ch in prefix)
    assert utf16_at_error == 13
    assert len({12, 19, 13}) == 3

    base = {
        "code": "RXT000",
        "message": f"Python parse error: {err.msg}",
        "severity": "error",
        "file_path": module,
        "line": err.lineno,
    }
    # Contract 1.x producer: raw SyntaxError.offset
    legacy = parse_check_report(
        {"contract_version": "1.0.0", "diagnostics": [{**base, "column": err.offset}]}
    )
    # Contract 2.x producer: 0-based UTF-8 byte offset
    modern = parse_check_report(
        {"contract_version": "2.0.0", "diagnostics": [{**base, "column": utf8_column}]}
    )
    legacy_char = diagnostics_for_file(legacy, module, degraded=False, lines=[line])[
        0
    ].range.start.character
    modern_char = diagnostics_for_file(modern, module, degraded=False, lines=[line])[
        0
    ].range.start.character
    assert legacy_char == modern_char == 13
    # Without line text the fallbacks differ by unit system (still intentional).
    assert diagnostics_for_file(legacy, module, degraded=False)[0].range.start.character == 12
    assert diagnostics_for_file(modern, module, degraded=False)[0].range.start.character == 19


def test_diagnostics_for_file_rxt000_real_syntaxerror_offset_maps_to_utf16():
    # Normalized core contract 2.x: RXT000.column is a 0-based UTF-8 byte offset
    # derived from CPython SyntaxError.offset (1-based code point).
    module = "/p/m.py"
    source = 'x = "한글😀" + ('  # unclosed '(' after 한글 + 😀
    try:
        compile(source, module, "exec")
    except SyntaxError as exc:
        err = exc  # keep after except (PEP 3110 clears the as-target)
    else:
        raise AssertionError(f"expected SyntaxError for {source!r}")
    assert err.offset is not None
    assert err.lineno == 1
    line = (err.text or source).rstrip("\n")
    assert err.offset == 13
    assert line == source
    prefix = line[: err.offset - 1]
    core_column = len(prefix.encode("utf-8"))
    assert core_column == 19
    record = {
        "code": "RXT000",
        "message": f"Python parse error: {err.msg}",
        "severity": "error",
        "file_path": module,
        "line": err.lineno,
        "column": core_column,
    }
    report = parse_check_report({"contract_version": "2.0.0", "diagnostics": [record]})
    bare = diagnostics_for_file(report, module, degraded=False)
    assert bare[0].range.start.character == 19
    converted = diagnostics_for_file(report, module, degraded=False, lines=[line])
    assert converted[0].range.start.character == 13


# --------------------------------------------------------------------------- #
# Exempt quick fix preserves trailing content / handles args (fix #8).
# --------------------------------------------------------------------------- #
def _apply(text: str, edit: lsp.TextEdit) -> str:
    line = text.splitlines()[edit.range.start.line]
    return line[: edit.range.start.character] + edit.new_text + line[edit.range.end.character :]


def test_code_action_preserves_trailing_comment():
    module = "/proj/ops.py"
    text = (
        "@rextio.native  # keep native\n"
        "def rejected(xs: list[int]) -> int:\n"
        "    return helper(xs)\n"
    )
    report = _rejected_report(module, def_line=2, diag_line=3)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    (edit,) = actions[0].edit.changes["file:///proj/ops.py"]
    assert edit.new_text == "@rextio.exempt"
    assert _apply(text, edit) == "@rextio.exempt  # keep native"


def test_code_action_replaces_native_with_target_arg():
    module = "/proj/ops.py"
    text = (
        '@rextio.native(target="rust")\n'
        "def rejected(xs: list[int]) -> int:\n"
        "    return helper(xs)\n"
    )
    report = _rejected_report(module, def_line=2, diag_line=3)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    (edit,) = actions[0].edit.changes["file:///proj/ops.py"]
    # the whole @rextio.native(...) span is replaced, args included
    assert _apply(text, edit) == "@rextio.exempt"


def _exempt_edit_apply(text: str) -> list:
    """Run the exempt quick fix over ``text`` (one rejected fn on line 2)."""
    module = "/proj/ops.py"
    report = _rejected_report(module, def_line=2, diag_line=3)
    diags = diagnostics_for_file(report, module, degraded=False)
    return code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )


def test_code_action_replaces_native_with_nested_paren_string_arg():
    # a `)` inside a string literal must not truncate the span (fix #4a):
    # regex `[^)]*` would stop early and yield a broken `@rextio.exempt")`.
    text = (
        '@rextio.native(target="rust(builtin)")\n'
        "def rejected(xs: list[int]) -> int:\n"
        "    return helper(xs)\n"
    )
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_replaces_native_with_call_arg():
    # a nested call `f(x)` in the argument list is spanned by paren balance.
    text = (
        "@rextio.native(target=f(x))\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n"
    )
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_absent_for_multiline_native_args():
    # a MULTI-LINE @rextio.native(...) argument list: the fix is withheld rather
    # than emit an edit that leaves dangling continuation lines (fix #4b).
    module = "/proj/ops.py"
    text = (
        "@rextio.native(\n"
        '    target="rust",\n'
        ")\n"
        "def rejected(xs: list[int]) -> int:\n"
        "    return helper(xs)\n"
    )
    report = _rejected_report(module, def_line=4, diag_line=5)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    assert actions == []


# --------------------------------------------------------------------------- #
# Quote-aware paren-balance in the exempt span scan (fix #3).
# --------------------------------------------------------------------------- #
def test_code_action_span_ignores_paren_inside_string():
    # a `)` inside a string literal must NOT end the span early: a naive scan
    # would stop there and yield a corrupting `@rextio.exempt b")`.
    text = (
        '@rextio.native(target="a)b")\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n'
    )
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_span_string_is_only_a_close_paren():
    # the string content is a bare `)`; the real arg-list close is the last `)`.
    text = (
        '@rextio.native(target=")")\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n'
    )
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_span_ignores_open_paren_inside_single_quotes():
    # an unbalanced `(` inside a single-quoted string must not inflate the balance
    # (else the real close paren would be mistaken for an inner one).
    text = (
        "@rextio.native(target='a(b')\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n"
    )
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_withheld_for_unterminated_string_in_args():
    # an unterminated string literal on the decorator line is ambiguous: withhold
    # the fix rather than guess a span.
    text = (
        '@rextio.native(target="oops)\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n'
    )
    assert _exempt_edit_apply(text) == []


# --------------------------------------------------------------------------- #
# tokenize-based exempt span: full adversarial string matrix (rounds 17-19).
# --------------------------------------------------------------------------- #
def _native_line(arg_line: str) -> str:
    """A one-rejected-fn document whose native decorator line is ``arg_line``."""
    return f"{arg_line}\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n"


def test_code_action_span_single_line_triple_double_quote():
    # F1 of round 19: a single-line triple-quoted arg with an in-string `)` — the
    # single-char quote scanner stopped at the in-string `)` and corrupted the
    # edit. tokenize sees one STRING token, so the real close is spanned.
    text = _native_line('@rextio.native(target="""a")b""")')
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_span_single_line_triple_single_quote():
    # the triple-SINGLE-quote sibling of the F1 shape.
    text = _native_line("@rextio.native(target='''a')b''')")
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_span_call_arg_with_trailing_comment():
    # a nested call f(x) plus a trailing comment: the comment (and any parens in
    # it) is a COMMENT token and never joins the paren depth.
    text = _native_line("@rextio.native(target=f(x))  # keep )(")
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt  # keep )("


def test_code_action_span_escaped_quote_in_string():
    # an escaped quote inside the string must not end the literal early.
    text = _native_line(r'@rextio.native(target="a\"b)c")')
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_code_action_span_adjacent_string_literals():
    # implicit string adjacency "a" "b)c": the `)` sits inside the second literal.
    text = _native_line('@rextio.native(target="a" "b)c")')
    (edit,) = _exempt_edit_apply(text)[0].edit.changes["file:///proj/ops.py"]
    assert _apply(text, edit) == "@rextio.exempt"


def test_native_exempt_span_withholds_on_unterminated_triple_quote():
    # a line tokenize rejects (unterminated triple quote) is unanalyzable: the
    # helper withholds rather than guess a span.
    assert _native_exempt_span('@rextio.native(target="""oops)') is None


def test_native_exempt_span_withholds_on_multiline_opener():
    # a bare multi-line opener never closes on the line -> tokenize raises -> None.
    assert _native_exempt_span("@rextio.native(") is None


def test_native_exempt_span_columns_are_code_points_with_non_ascii():
    # tokenize columns are 0-based code-point offsets, matching line[:col]; a
    # non-ASCII arg must not shift the span.
    line = '@rextio.native(target="rüst🦀")'
    span = _native_exempt_span(line)
    assert span is not None
    start, end = span
    assert line[start:end] == line  # whole decorator token + arg list


# --------------------------------------------------------------------------- #
# Multi-line decorator tolerance in the quick-fix scan (fix #10).
# --------------------------------------------------------------------------- #
def test_find_native_decorator_multiline_decorator_below_native():
    # native, then a multi-line decorator between it and the def
    lines = ["@rextio.native", "@foo(", "    arg,", ")", "def bar():", "    pass"]
    assert find_native_decorator_line(lines, 5) == 0  # def on 1-based line 5


def test_find_native_decorator_not_counted_inside_paren_args():
    # @rextio.native appears as a continuation line of @config(...)'s args
    lines = ["@config(", "    @rextio.native", ")", "def f():", "    pass"]
    assert find_native_decorator_line(lines, 4) is None


def test_find_native_decorator_class_method_without_blank_line():
    lines = ["class C:", "    @rextio.native", "    def m(self):", "        pass"]
    assert find_native_decorator_line(lines, 3) == 1


# --------------------------------------------------------------------------- #
# tokenize-based block balance: sibling string/comment parens no longer skew
# the contiguous-decorator scan (round 19 F2).
# --------------------------------------------------------------------------- #
def test_find_native_decorator_sibling_string_paren_offers_fix():
    # F2: a `(` inside a sibling decorator's STRING arg made the char-count
    # balance nonzero, so @rextio.native was mistaken for a continuation line and
    # the fix was suppressed. Token paren depth ignores the in-string paren.
    lines = ['@foo("(")', "@rextio.native", "def bar():", "    pass"]
    assert find_native_decorator_line(lines, 3) == 1


def test_find_native_decorator_sibling_comment_paren_offers_fix():
    # a `(` in a sibling decorator's trailing comment must not skew the balance.
    lines = ["@foo  # (", "@rextio.native", "def bar():", "    pass"]
    assert find_native_decorator_line(lines, 3) == 1


def test_find_native_decorator_multiline_sibling_above_native():
    # a genuine multi-line sibling decorator above native: OP paren tokens on the
    # fragment lines keep the running balance, so native is still found.
    lines = ["@foo(", '    arg="x",', ")", "@rextio.native", "def bar():", "    pass"]
    assert find_native_decorator_line(lines, 5) == 3


def test_code_action_offered_over_sibling_string_paren_full_path():
    # drive F2 through the real code-action path, not just the locator.
    module = "/proj/ops.py"
    text = '@foo("(")\n@rextio.native\ndef rejected(xs: list[int]) -> int:\n    return helper(xs)\n'
    report = _rejected_report(module, def_line=3, diag_line=4)
    diags = diagnostics_for_file(report, module, degraded=False)
    actions = code_actions_for(
        report,
        file_path=module,
        uri="file:///proj/ops.py",
        document_text=text,
        context_diagnostics=diags,
    )
    (edit,) = actions[0].edit.changes["file:///proj/ops.py"]
    assert edit.new_text == "@rextio.exempt"
    assert edit.range.start.line == 1  # the @rextio.native line


# --------------------------------------------------------------------------- #
# Top-level dedupe key includes the message (fix #9).
# --------------------------------------------------------------------------- #
def test_diagnostics_for_file_keeps_distinct_messages_at_same_position():
    module = "/proj/m.py"
    at = {"file_path": module, "line": 5, "column": 4, "severity": "error"}
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            # a top-level record sharing code + position but a DIFFERENT message
            "diagnostics": [{"code": "RXT070", "message": "second", **at}],
            "modules": [
                {
                    "file_path": module,
                    "functions": [
                        {
                            "qualname": "m.f",
                            "name": "f",
                            "file_path": module,
                            "line": 5,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [{"code": "RXT070", "message": "first", **at}],
                        }
                    ],
                }
            ],
        }
    )
    diags = diagnostics_for_file(report, module, degraded=False)
    # both are kept: the message is part of the dedupe key, so the distinct
    # top-level record is not collapsed into the function's.
    assert sorted(d.message.split("\n")[0] for d in diags) == ["first", "second"]


# --------------------------------------------------------------------------- #
# didClose on rextio.toml keeps project-scope diagnostics (fix #5).
# --------------------------------------------------------------------------- #
def test_did_close_toml_keeps_project_scope_diagnostics(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    server = RextioLanguageServer()
    _setup_workspace(server)  # no open docs
    published = _capture_publishes(server, monkeypatch)
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(root),
            "diagnostics": [
                {
                    "code": "RXT091",
                    "message": "project-scope note",
                    "severity": "info",
                    "file_path": "",
                    "line": 1,
                    "column": 0,
                }
            ],
        }
    )
    server._publish_for_project(root, report, degraded=False)
    toml_uri = (root / "rextio.toml").as_uri()
    assert [d.code for d in published[toml_uri]] == ["RXT091"]

    # closing the toml document must NOT wipe the project-scope diagnostics
    published.clear()
    server.handle_did_close(toml_uri)
    assert toml_uri not in published  # never cleared
    assert toml_uri in server._published_uris[str(root)]  # publish record intact


# --------------------------------------------------------------------------- #
# Project-switch URI ownership (fix #7).
# --------------------------------------------------------------------------- #
def test_migrated_uri_not_cleared_by_old_root(tmp_path, monkeypatch):
    # a doc's URI migrated from root A to a newly-appeared nested root B (which
    # published first). A's next clear pass must skip the URI now owned by B.
    a = tmp_path / "a"
    b = a / "nested"
    a.mkdir()
    b.mkdir()
    a_root, b_root = a.resolve(), b.resolve()
    doc_uri = (b / "mod.py").as_uri()
    server = RextioLanguageServer()
    published = _capture_publishes(server, monkeypatch)
    # B already published and owns the URI
    server._published_uris[str(b_root)] = {doc_uri}
    server._uri_owner[doc_uri] = str(b_root)
    # A had published the same URI before the nested toml appeared
    server._published_uris[str(a_root)] = {doc_uri}

    # A re-analyzes and no longer owns the URI (doc migrated to B)
    server._set_published(str(a_root), set())

    assert doc_uri not in published  # B's fresh diagnostics NOT wiped
    assert server._uri_owner[doc_uri] == str(b_root)  # ownership intact


# --------------------------------------------------------------------------- #
# Deleted-toml vs in-flight analysis race: generation guard (fix #1).
# --------------------------------------------------------------------------- #
def _one_rejection_report(root: Path, module: Path) -> "object":
    return parse_check_report(
        {
            "contract_version": "1.0.0",
            "project_root": str(root),
            "modules": [
                {
                    "file_path": str(module),
                    "functions": [
                        {
                            "qualname": "ops.rejected",
                            "name": "rejected",
                            "file_path": str(module),
                            "line": 1,
                            "column": 0,
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [
                                {
                                    "code": "RXT070",
                                    "message": "m",
                                    "severity": "error",
                                    "file_path": str(module),
                                    "line": 1,
                                    "column": 0,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_deleted_toml_discards_in_flight_analysis(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    module = root / "ops.py"
    module.write_text("x = 1\n", encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (module.as_uri(), "x = 1\n"))
    published = _capture_publishes(server, monkeypatch)
    monkeypatch.setattr(server, "window_log_message", lambda _p: None)

    # simulate an earlier completed run: the module URI is published + owned
    server._published_uris[str(root)] = {module.as_uri()}
    server._uri_owner[module.as_uri()] = str(root)

    started = threading.Event()
    release = threading.Event()
    report = _one_rejection_report(root, module)

    def blocking_check(_root):
        started.set()
        release.wait(3.0)
        return report

    monkeypatch.setattr(server.engine, "check", blocking_check)

    worker = threading.Thread(target=server._run_analysis, args=(str(root),))
    worker.start()
    assert started.wait(3.0)  # analysis is now blocked mid-run

    # the toml is deleted while the analysis is in flight
    params = lsp.DidChangeWatchedFilesParams(
        changes=[
            lsp.FileEvent(uri=(root / "rextio.toml").as_uri(), type=lsp.FileChangeType.Deleted)
        ]
    )
    server.handle_watched_files_change(params)
    assert published[module.as_uri()] == []  # cleared by the deletion

    release.set()  # let the stale analysis finish
    worker.join(3.0)
    assert not worker.is_alive()

    # the late result was discarded: no report stored, diagnostics stay cleared
    assert str(root) not in server._reports
    assert published[module.as_uri()] == []


def test_changed_toml_midrun_ends_with_fresh_rerun(tmp_path, monkeypatch):
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    module = root / "ops.py"
    module.write_text("x = 1\n", encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (module.as_uri(), "x = 1\n"))
    _capture_publishes(server, monkeypatch)
    monkeypatch.setattr(server, "window_log_message", lambda _p: None)
    monkeypatch.setattr("rextio_lsp.server.DEBOUNCE_SECONDS", 0.02)

    stale_report = _one_rejection_report(root, module)
    fresh_report = _one_rejection_report(root, module)
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def check_seq(_root):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            release.wait(3.0)
            return stale_report  # in-flight when the toml changes -> discarded
        return fresh_report

    monkeypatch.setattr(server.engine, "check", check_seq)

    worker = threading.Thread(target=server._run_analysis, args=(str(root),))
    worker.start()
    assert started.wait(3.0)

    # a Changed event mid-run bumps the generation and debounces a fresh re-run
    params = lsp.DidChangeWatchedFilesParams(
        changes=[
            lsp.FileEvent(uri=(root / "rextio.toml").as_uri(), type=lsp.FileChangeType.Changed)
        ]
    )
    server.handle_watched_files_change(params)
    release.set()
    worker.join(3.0)

    # wait for the debounced fresh re-run to store its result
    deadline = time.time() + 3.0
    while time.time() < deadline and server._reports.get(str(root)) is not fresh_report:
        time.sleep(0.02)
    assert server._reports.get(str(root)) is fresh_report  # not the stale one


def test_deleted_toml_during_capabilities_warm_discards_analysis(tmp_path, monkeypatch):
    # The post-check window (fix #2): engine.check has already returned, but a
    # toml Deleted lands DURING the (potentially slow) capabilities warm. The
    # final generation gate must skip both the store and the publish.
    (tmp_path / "rextio.toml").write_text("[build]\n", encoding="utf-8")
    root = tmp_path.resolve()
    module = root / "ops.py"
    module.write_text("x = 1\n", encoding="utf-8")
    server = RextioLanguageServer()
    _setup_workspace(server, (module.as_uri(), "x = 1\n"))
    published = _capture_publishes(server, monkeypatch)
    monkeypatch.setattr(server, "window_log_message", lambda _p: None)

    # simulate an earlier completed run: the module URI is published + owned
    server._published_uris[str(root)] = {module.as_uri()}
    server._uri_owner[module.as_uri()] = str(root)

    report = _one_rejection_report(root, module)
    monkeypatch.setattr(server.engine, "check", lambda _root: report)

    started = threading.Event()
    release = threading.Event()

    def blocking_capabilities(_root):
        started.set()
        release.wait(3.0)  # block the warm until the deletion has been processed
        return None

    monkeypatch.setattr(server.engine, "capabilities", blocking_capabilities)

    worker = threading.Thread(target=server._run_analysis, args=(str(root),))
    worker.start()
    assert started.wait(3.0)  # analysis is now blocked inside the capabilities warm

    # the toml is deleted while the warm is in flight
    params = lsp.DidChangeWatchedFilesParams(
        changes=[
            lsp.FileEvent(uri=(root / "rextio.toml").as_uri(), type=lsp.FileChangeType.Deleted)
        ]
    )
    server.handle_watched_files_change(params)
    assert published[module.as_uri()] == []  # cleared by the deletion

    release.set()  # let the stale analysis finish
    worker.join(3.0)
    assert not worker.is_alive()

    # discarded past the warm: no report stored, no publish, diagnostics cleared
    assert str(root) not in server._reports
    assert published[module.as_uri()] == []
