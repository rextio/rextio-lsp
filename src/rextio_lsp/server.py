"""pygls server: Rextio-only diagnostics and hover.

Capabilities are deliberately narrow (owner-decided): the server advertises
publishDiagnostics and hover and *nothing else* -- no completion, formatting,
rename, definition, or references. Diagnostics carry ``source: "rextio"`` and
the RXT/RXTP code; severity never escalates to Error (see :func:`map_severity`).

The pure conversion helpers (:func:`map_severity`, :func:`to_lsp_diagnostic`,
:func:`diagnostics_for_file`, :func:`build_hover_markdown`,
:func:`function_at_line`) are module-level and pygls-free so they can be unit
tested without a running server.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from rextio_lsp.__about__ import __version__
from rextio_lsp.contract import (
    INFORMATIONAL_CODES,
    CapabilityManifest,
    DiagnosticRecord,
    FunctionReport,
    ProjectReport,
    is_contract_supported,
    lsp_character,
    lsp_line,
)
from rextio_lsp.discovery import find_project_root, uri_to_path
from rextio_lsp.engine import Engine

logger = logging.getLogger("rextio_lsp.server")

DEBOUNCE_SECONDS = 0.3


# --------------------------------------------------------------------------- #
# Pure conversion helpers (no pygls server state).
# --------------------------------------------------------------------------- #
def map_severity(code: str, *, is_rejection: bool) -> lsp.DiagnosticSeverity:
    """Map a Rextio diagnostic to an LSP severity.

    * A rejected native candidate's rejection code -> Warning.
    * Informational notes/hints -> Hint.
    * Everything else advisory -> Information.

    The contract mandates the server never surfaces Error.
    """
    if is_rejection:
        return lsp.DiagnosticSeverity.Warning
    if code in INFORMATIONAL_CODES:
        return lsp.DiagnosticSeverity.Hint
    return lsp.DiagnosticSeverity.Information


def to_lsp_diagnostic(
    record: DiagnosticRecord, *, is_rejection: bool, degraded: bool
) -> lsp.Diagnostic:
    """Convert a contract diagnostic to an LSP diagnostic.

    In degraded mode (unsupported contract major) the message is the raw
    analyzer message with no guidance enrichment; otherwise the analyzer's
    single-sourced ``suggestion`` (the same string the capability manifest
    carries) is appended.
    """
    position = lsp.Position(
        line=lsp_line(record.line), character=lsp_character(record.column)
    )
    message = record.message
    if not degraded and record.suggestion:
        message = f"{record.message}\n\n{record.suggestion}"
    return lsp.Diagnostic(
        range=lsp.Range(start=position, end=position),
        message=message,
        severity=map_severity(record.code, is_rejection=is_rejection),
        code=record.code,
        source="rextio",
    )


def diagnostics_for_file(
    report: ProjectReport, file_path: str, *, degraded: bool
) -> list[lsp.Diagnostic]:
    """Build all LSP diagnostics for one file from a project report."""
    diagnostics: list[lsp.Diagnostic] = []
    for fn in report.functions_in_file(file_path):
        rejection_codes = set(fn.rejection_codes)
        for record in fn.diagnostics:
            is_rejection = fn.native_status == "rejected" and record.code in rejection_codes
            diagnostics.append(
                to_lsp_diagnostic(record, is_rejection=is_rejection, degraded=degraded)
            )
    return diagnostics


def function_at_line(
    report: ProjectReport, file_path: str, line: int
) -> FunctionReport | None:
    """Return the function whose definition is on 0-based LSP ``line``."""
    for fn in report.functions_in_file(file_path):
        if lsp_line(fn.line) == line:
            return fn
    return None


def build_hover_markdown(
    fn: FunctionReport, manifest: CapabilityManifest | None, *, degraded: bool
) -> str:
    """Render hover markdown: route, native status, and rejection guidance."""
    lines = [
        f"**Rextio route:** `{fn.route}`",
        f"**Native status:** `{fn.native_status}`",
    ]
    if fn.rejection_codes:
        lines.append("")
        lines.append("**Rejections:**")
        for code in fn.rejection_codes:
            guidance = None
            if not degraded and manifest is not None:
                rule = manifest.guidance_for(code)
                if rule is not None and rule.guidance:
                    guidance = rule.guidance
            lines.append(f"- `{code}` — {guidance}" if guidance else f"- `{code}`")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Server.
# --------------------------------------------------------------------------- #
class RextioLanguageServer(LanguageServer):
    """Language server that analyzes Rextio projects on open/save."""

    def __init__(self) -> None:
        super().__init__(name="rextio-lsp", version=__version__)
        self.engine = Engine()
        self._reports: dict[str, ProjectReport] = {}
        self._degraded: dict[str, bool] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._state_lock = threading.Lock()

    # -- trigger / debounce ------------------------------------------------- #
    def schedule_analysis_for_uri(self, uri: str) -> None:
        """Debounced analysis trigger for the project owning ``uri``."""
        path = uri_to_path(uri)
        if path is None:
            return
        root = find_project_root(path)
        if root is None:
            logger.debug("no rextio.toml for %s; ignoring", uri)
            return
        self._debounce(str(root))

    def _debounce(self, root: str) -> None:
        with self._state_lock:
            existing = self._timers.get(root)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._run_analysis, args=(root,))
            timer.daemon = True
            self._timers[root] = timer
            timer.start()

    # -- analysis ----------------------------------------------------------- #
    def _run_analysis(self, root: str) -> None:
        try:
            self.analyze_project(Path(root))
        except Exception:  # noqa: BLE001 -- analysis must never crash the server
            logger.exception("analysis failed for %s", root)

    def analyze_project(self, project_root: Path) -> ProjectReport | None:
        """Run a whole-project check and publish diagnostics for open docs."""
        # rextio emits fully-resolved absolute paths; resolve here too so cache
        # keys and file-path matching agree across symlinks (e.g. macOS
        # /var -> /private/var).
        project_root = project_root.resolve()
        report = self.engine.check(project_root)
        if report is None:
            logger.info("rextio unavailable for %s; no-op", project_root)
            return None

        degraded = not is_contract_supported(report.contract_version)
        if degraded:
            logger.warning(
                "unsupported contract_version %r for %s; degrading to generic diagnostics",
                report.contract_version,
                project_root,
            )
        with self._state_lock:
            self._reports[str(project_root)] = report
            self._degraded[str(project_root)] = degraded

        # Warm the guidance manifest (used by hover); skipped when degraded.
        if not degraded:
            self.engine.capabilities(project_root)

        self._publish_for_project(project_root, report, degraded=degraded)
        return report

    def _publish_for_project(
        self, project_root: Path, report: ProjectReport, *, degraded: bool
    ) -> None:
        for doc in list(self.workspace.text_documents.values()):
            doc_path = uri_to_path(doc.uri)
            if doc_path is None:
                continue
            resolved = doc_path.resolve()
            if find_project_root(resolved) != project_root:
                continue
            diagnostics = diagnostics_for_file(report, str(resolved), degraded=degraded)
            self.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(uri=doc.uri, diagnostics=diagnostics)
            )

    # -- hover -------------------------------------------------------------- #
    def hover_for(self, uri: str, position: lsp.Position) -> lsp.Hover | None:
        """Build hover content for a function-definition line, if any."""
        path = uri_to_path(uri)
        if path is None:
            return None
        resolved = path.resolve()
        root = find_project_root(resolved)
        if root is None:
            return None

        report = self._reports.get(str(root))
        if report is None:
            report = self.analyze_project(root)
            if report is None:
                return None

        fn = function_at_line(report, str(resolved), position.line)
        if fn is None:
            return None

        degraded = self._degraded.get(str(root), False)
        manifest = None if degraded else self.engine.capabilities(root)
        markdown = build_hover_markdown(fn, manifest, degraded=degraded)
        return lsp.Hover(
            contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=markdown)
        )


def create_server() -> RextioLanguageServer:
    """Construct the server and register the (narrow) feature handlers."""
    server = RextioLanguageServer()

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    def _did_open(ls: RextioLanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
        if params.text_document.uri.endswith(".py"):
            ls.schedule_analysis_for_uri(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def _did_save(ls: RextioLanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
        if params.text_document.uri.endswith(".py"):
            ls.schedule_analysis_for_uri(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    def _hover(ls: RextioLanguageServer, params: lsp.HoverParams) -> lsp.Hover | None:
        return ls.hover_for(params.text_document.uri, params.position)

    return server
