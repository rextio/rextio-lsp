"""Unit tests for severity mapping, diagnostic/hover conversion, and wiring."""

from __future__ import annotations

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
    find_native_decorator_line,
    function_at_line,
    latency_log,
    map_severity,
    parse_initialization_options,
    to_lsp_diagnostic,
)

PIPELINE = "/Volumes/Data/workspace/rextio/rextio/examples/boundary_demo/src/boundary_demo/pipeline.py"


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
    square = next(lens for lens in lenses if lens.command.arguments == ["boundary_demo.pipeline.square"])
    assert square.range.start.line == 4


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
            lsp.FileEvent(
                uri=(tmp_path / "rextio.toml").as_uri(), type=lsp.FileChangeType.Changed
            )
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
    # a degraded report skips the capabilities warm-up; publish is stubbed out
    report = parse_check_report({"contract_version": "2.0.0", "project_root": "/proj"})
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
            lsp.FileEvent(
                uri=(tmp_path / "rextio.toml").as_uri(), type=lsp.FileChangeType.Deleted
            )
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
    record = {
        "code": "RXT000",
        "message": "m",
        "severity": "error",
        "file_path": module,
        "line": 1,
        "column": 15,
    }
    report = parse_check_report({"contract_version": "1.0.0", "diagnostics": [record]})
    # no lines -> raw byte offset fallback
    assert diagnostics_for_file(report, module, degraded=False)[0].range.start.character == 15
    # with the document line -> converted to the UTF-16 index
    converted = diagnostics_for_file(report, module, degraded=False, lines=[line])
    assert converted[0].range.start.character == 11


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
