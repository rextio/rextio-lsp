"""pygls server: Rextio-only diagnostics, hover, code lens, and code actions.

Capabilities are deliberately narrow (owner-decided): the server advertises
publishDiagnostics, hover, and -- when enabled -- code lens and quick-fix code
actions, and *nothing else* -- no completion, formatting, rename, definition, or
references. Diagnostics carry ``source: "rextio"`` and the RXT/RXTP code;
severity never escalates to Error (see :func:`map_severity`).

Code lens is registered only when ``initializationOptions.codeLens.enable`` is
true, using the pygls initialize hook: the user INITIALIZE handler runs before
server capabilities are computed, so features registered there are advertised
(and omitted entirely when disabled).

The pure conversion helpers (:func:`map_severity`, :func:`to_lsp_diagnostic`,
:func:`diagnostics_for_file`, :func:`build_hover_markdown`,
:func:`function_at_line`, :func:`code_lenses_for`, :func:`code_actions_for`,
:func:`parse_initialization_options`, :func:`latency_log`) are module-level and
pygls-free so they can be unit tested without a running server.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    utf16_character,
    utf16_len,
)
from rextio_lsp.discovery import CONFIG_FILENAME, find_project_root, uri_to_path
from rextio_lsp.engine import Engine

logger = logging.getLogger("rextio_lsp.server")

DEBOUNCE_SECONDS = 0.3

# A whole-project check slower than this is surfaced at window/logMessage Info
# (otherwise Log). See :func:`latency_log`.
LATENCY_WARN_SECONDS = 2.0

# Client-bindable command id carried by each route code lens. The server does
# not implement it (it registers no command handler); the lens is informational
# and the client may bind the id later.
ROUTE_INFO_COMMAND = "rextio.showRouteInfo"

# Title of the exempt quick fix and the native decorator it rewrites.
EXEMPT_ACTION_TITLE = "Rextio: keep on Python fallback (@rextio.exempt)"
# Matches the ``@rextio.native`` decorator token itself (name only), used only
# to spot a native decorator start line. The quick fix computes the
# argument-list span separately over Python ``tokenize`` output (see
# :func:`_native_exempt_span`), never a regex: a regex ``[^)]*`` group stops at
# the first ``)`` and would corrupt nested (``target="rust(builtin)"``) or
# multi-line argument lists.
_NATIVE_DECORATOR_RE = re.compile(r"@rextio\.native\b")


@dataclass(frozen=True)
class InitializationOptions:
    """Parsed ``initializationOptions`` (see :func:`parse_initialization_options`)."""

    code_lens_enabled: bool = True
    interpreter_path: str | None = None


def parse_initialization_options(raw: Any) -> InitializationOptions:
    """Parse the fixed ``initializationOptions`` shape, leniently.

    Contract shape (missing keys fall back to the defaults shown)::

        { "codeLens": {"enable": true}, "interpreter": {"path": null} }

    Anything unexpected (wrong types, extra keys) is ignored rather than raised.
    """
    if not isinstance(raw, dict):
        return InitializationOptions()

    code_lens_enabled = True
    code_lens = raw.get("codeLens")
    if isinstance(code_lens, dict) and "enable" in code_lens:
        code_lens_enabled = bool(code_lens["enable"])

    interpreter_path: str | None = None
    interpreter = raw.get("interpreter")
    if isinstance(interpreter, dict):
        path = interpreter.get("path")
        if isinstance(path, str) and path.strip():
            interpreter_path = path

    return InitializationOptions(
        code_lens_enabled=code_lens_enabled, interpreter_path=interpreter_path
    )


def latency_log(project_root: Path, elapsed: float) -> tuple[lsp.MessageType, str]:
    """Return the (message type, text) for a project-check duration log line.

    Info when the check exceeds :data:`LATENCY_WARN_SECONDS`, Log otherwise.
    """
    message = f"rextio check {project_root}: {elapsed:.2f}s"
    msg_type = (
        lsp.MessageType.Info if elapsed > LATENCY_WARN_SECONDS else lsp.MessageType.Log
    )
    return msg_type, message


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


def _character_at(
    lines: list[str] | None, contract_line: int, column: int, code: str = ""
) -> int:
    """LSP character for a contract (1-based line, UTF-8 byte ``column``).

    Contract columns are UTF-8 byte offsets; LSP positions default to UTF-16
    code units. When the document's line text is available, convert the byte
    offset accurately (see :func:`utf16_character`); otherwise fall back to the
    raw byte offset (:func:`lsp_character`).

    ``RXT000`` (syntax error) is a documented exception: core emits its column
    as ``SyntaxError.offset`` -- a **1-based CODE POINT** offset -- not the
    0-based UTF-8 byte offset every other diagnostic uses. It is special-cased
    here (subtract 1, clamp, then map code points -> UTF-16 via the line text;
    without line text, subtract 1 and pass through). This is a core contract
    inconsistency to be normalized upstream in a future core release; until
    then the server compensates so RXT000 lands correctly on non-ASCII lines.
    """
    # Defensive: the parse now coerces a null/junk column to 0 (see
    # contract._int_or), but guard here too so a None can never reach the
    # arithmetic below (int(None) - 1 would raise).
    if column is None:
        column = 0
    line_text: str | None = None
    if lines is not None:
        idx = lsp_line(contract_line)
        if 0 <= idx < len(lines):
            line_text = lines[idx]
    if code == "RXT000":
        col0 = max(column - 1, 0)
        if line_text is not None:
            return utf16_len(line_text[:col0])
        return col0
    if line_text is not None:
        return utf16_character(line_text, column)
    return lsp_character(column)


def to_lsp_diagnostic(
    record: DiagnosticRecord,
    *,
    is_rejection: bool,
    degraded: bool,
    lines: list[str] | None = None,
) -> lsp.Diagnostic:
    """Convert a contract diagnostic to an LSP diagnostic.

    In degraded mode (unsupported contract major) the message is the raw
    analyzer message with no guidance enrichment; otherwise the analyzer's
    single-sourced ``suggestion`` (the same string the capability manifest
    carries) is appended. ``lines`` (the document's text split into lines) lets
    the range's columns be mapped from UTF-8 byte offsets to UTF-16 code units.
    """
    start = lsp.Position(
        line=lsp_line(record.line),
        character=_character_at(lines, record.line, record.column, record.code),
    )
    # Use the real span when the record carries one; else a zero-width range.
    if record.end_line is not None and record.end_column is not None:
        end = lsp.Position(
            line=lsp_line(record.end_line),
            character=_character_at(
                lines, record.end_line, record.end_column, record.code
            ),
        )
    else:
        end = start
    message = record.message
    if not degraded and record.suggestion:
        message = f"{record.message}\n\n{record.suggestion}"
    return lsp.Diagnostic(
        range=lsp.Range(start=start, end=end),
        message=message,
        severity=map_severity(record.code, is_rejection=is_rejection),
        code=record.code,
        source="rextio",
    )


def diagnostics_for_file(
    report: ProjectReport,
    file_path: str,
    *,
    degraded: bool,
    lines: list[str] | None = None,
) -> list[lsp.Diagnostic]:
    """Build all LSP diagnostics for one file from a project report.

    Includes both per-function diagnostics and any top-level/module diagnostics
    (e.g. a syntax-error ``RXT000``) whose ``file_path`` matches -- these produce
    zero functions, so without them the file would publish nothing. Top-level
    records are de-duplicated against per-function diagnostics on
    ``(code, line, column, message)`` -- the message is part of the key so two
    distinct diagnostics that share a code and position are not collapsed.
    """
    diagnostics: list[lsp.Diagnostic] = []
    covered: set[tuple[str, int, int, str]] = set()
    for fn in report.functions_in_file(file_path):
        rejection_codes = set(fn.rejection_codes)
        for record in fn.diagnostics:
            is_rejection = fn.native_status == "rejected" and record.code in rejection_codes
            covered.add((record.code, record.line, record.column, record.message))
            diagnostics.append(
                to_lsp_diagnostic(
                    record, is_rejection=is_rejection, degraded=degraded, lines=lines
                )
            )
    for record in report.top_level_diagnostics:
        if record.file_path != file_path:
            continue
        key = (record.code, record.line, record.column, record.message)
        if key in covered:
            continue
        covered.add(key)
        # Top-level records are module/parse-level notes, never function
        # rejections, so they map by code (Information/Hint), never Warning.
        diagnostics.append(
            to_lsp_diagnostic(record, is_rejection=False, degraded=degraded, lines=lines)
        )
    return diagnostics


def project_scope_diagnostics(
    report: ProjectReport, *, degraded: bool
) -> list[lsp.Diagnostic]:
    """Top-level diagnostics with no ``file_path`` (project-scope).

    Published against the project's ``rextio.toml`` URI (there is no source file
    to attach them to). Never rejections, so mapped by code.
    """
    return [
        to_lsp_diagnostic(record, is_rejection=False, degraded=degraded)
        for record in report.top_level_diagnostics
        if not record.file_path
    ]


def function_at_line(
    report: ProjectReport, file_path: str, line: int
) -> FunctionReport | None:
    """Return the function whose definition is on 0-based LSP ``line``."""
    for fn in report.functions_in_file(file_path):
        if lsp_line(fn.line) == line:
            return fn
    return None


def _guidance_line(
    code: str, manifest: CapabilityManifest | None, *, degraded: bool
) -> str:
    """Render one ``- `CODE` — guidance`` bullet, guidance from the manifest."""
    guidance = None
    if not degraded and manifest is not None:
        rule = manifest.guidance_for(code)
        if rule is not None and rule.guidance:
            guidance = rule.guidance
    return f"- `{code}` — {guidance}" if guidance else f"- `{code}`"


def _advisory_codes(fn: FunctionReport) -> list[str]:
    """Non-rejection diagnostic codes on ``fn`` (informational/advisory), deduped."""
    rejection = set(fn.rejection_codes)
    seen: list[str] = []
    for record in fn.diagnostics:
        if record.code and record.code not in rejection and record.code not in seen:
            seen.append(record.code)
    return seen


def build_hover_markdown(
    fn: FunctionReport, manifest: CapabilityManifest | None, *, degraded: bool
) -> str:
    """Render hover markdown: route, status, rejection and advisory guidance."""
    lines = [
        f"**Rextio route:** `{fn.route}`",
        f"**Native status:** `{fn.native_status}`",
    ]
    if fn.rejection_codes:
        lines.append("")
        lines.append("**Rejections:**")
        for code in fn.rejection_codes:
            lines.append(_guidance_line(code, manifest, degraded=degraded))
    advisory = _advisory_codes(fn)
    if advisory:
        lines.append("")
        lines.append("**Advisory:**")
        for code in advisory:
            lines.append(_guidance_line(code, manifest, degraded=degraded))
    return "\n".join(lines)


def code_lenses_for(
    report: ProjectReport, file_path: str, *, lines: list[str] | None = None
) -> list[lsp.CodeLens]:
    """One route lens per analyzed function definition line in ``file_path``.

    Title is ``Rextio: <route>``; the command is the informational no-op
    :data:`ROUTE_INFO_COMMAND` carrying ``[qualname]`` as its argument.
    """
    lenses: list[lsp.CodeLens] = []
    for fn in report.functions_in_file(file_path):
        position = lsp.Position(
            line=lsp_line(fn.line), character=_character_at(lines, fn.line, fn.column)
        )
        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(start=position, end=position),
                command=lsp.Command(
                    title=f"Rextio: {fn.route}",
                    command=ROUTE_INFO_COMMAND,
                    arguments=[fn.qualname],
                ),
            )
        )
    return lenses


def _line_tokens(line: str) -> list[tokenize.TokenInfo] | None:
    """Tokenize a single physical ``line``, or ``None`` if it is unanalyzable.

    Built on :func:`tokenize.generate_tokens`, which classifies every Python
    string form -- single/double quotes, single-line triple quotes, escapes and
    string adjacency -- and comments exactly, so a ``(`` or ``)`` inside a string
    or comment is a ``STRING``/``COMMENT`` token, never an ``OP`` paren. Adopting
    tokenize retired the string-shape whack-a-mole of council rounds 17-19
    (nested parens, single quotes, single-line triple quotes): the tokenizer
    settles every string form for us instead of a hand-rolled quote scanner.

    Any tokenizer failure -- ``TokenError`` from an unterminated string or an
    unclosed bracket (a multi-line construct), ``IndentationError``, or anything
    else -- means the line cannot be understood; return ``None`` so callers
    WITHHOLD their action rather than guess a span and corrupt code.

    tokenize reports token columns as 0-based code-point offsets into ``line`` --
    the very indices ``line[:col]`` uses elsewhere in this module -- so token
    columns need no conversion (verified against non-ASCII string content).
    """
    try:
        return list(tokenize.generate_tokens(io.StringIO(line).readline))
    except Exception:  # any tokenizer failure => unanalyzable => withhold
        return None


def _line_paren_delta(line: str) -> int:
    """Net ``OP`` paren depth change on ``line`` (open minus close).

    Counts only ``OP`` ``(``/``)`` TOKENS, so parens inside string literals or
    comments never skew the contiguous-decorator-block balance -- the F2 edge of
    round 19, where a sibling ``@foo("(")`` (or ``@foo  # (``) above
    ``@rextio.native`` inflated a char-count balance and suppressed the fix.

    A line that is only a FRAGMENT of a multi-line construct (e.g. ``@foo(`` on
    its own) makes tokenize raise once it runs off the end of the line; the
    tokens produced before that point still carry the real open/close parens, so
    they are counted -- matching the old char scan on such fragments while still
    ignoring string/comment parens.
    """
    delta = 0
    gen = tokenize.generate_tokens(io.StringIO(line).readline)
    try:
        for tok in gen:
            if tok.type == tokenize.OP:
                if tok.string == "(":
                    delta += 1
                elif tok.string == ")":
                    delta -= 1
    except Exception:  # keep the parens counted before the tokenizer gave up
        pass
    return delta


def find_native_decorator_line(lines: list[str], def_line: int) -> int | None:
    r"""Return the 0-based index of the sole ``@rextio.native`` decorator line.

    Scans the decorator block immediately above the 1-based ``def`` line,
    tolerating multi-line decorators (e.g. ``@foo(\n arg\n)``): a line only
    starts a decorator when the running parenthesis balance is zero, so
    continuation lines of another decorator's argument list are not mistaken for
    decorator starts. ``@rextio.native`` is counted only on decorator-start
    lines. Returns the index only when exactly one native decorator is present
    (otherwise ``None``, so no -- or no wrong -- quick fix is offered).
    """
    # Collect the contiguous non-blank block immediately above the def.
    bottom = def_line - 2  # 0-based line directly above the def
    top = bottom
    while top >= 0 and lines[top].strip():
        top -= 1
    top += 1
    if top > bottom:
        return None  # no lines above the def (blank or start-of-file)

    native_indices: list[int] = []
    balance = 0
    for i in range(top, bottom + 1):
        stripped = lines[i].strip()
        if balance == 0:
            if not stripped.startswith("@"):
                # A non-decorator line at top level (e.g. ``class C:`` directly
                # above the block): the decorator block, if any, begins below it.
                native_indices = []
            elif _NATIVE_DECORATOR_RE.match(stripped):
                native_indices.append(i)
        # Continuation lines (balance > 0) never start a decorator. Balance is
        # tracked over OP paren TOKENS (see :func:`_line_paren_delta`), not raw
        # characters, so a string/comment paren in a sibling decorator such as
        # ``@foo("(")`` no longer skews the block scan (round 19 F2).
        balance += _line_paren_delta(lines[i])
        if balance < 0:
            balance = 0
    return native_indices[0] if len(native_indices) == 1 else None


def _native_exempt_span(line: str) -> tuple[int, int] | None:
    """Char span of ``@rextio.native`` + its single-line arg list on ``line``.

    Returns the ``(start, end)`` code-point slice to replace with
    ``@rextio.exempt``, computed over :func:`tokenize` output:

    * locate the ``@rextio.native`` token sequence -- ``OP '@'``, ``NAME
      'rextio'``, ``OP '.'``, ``NAME 'native'``;
    * a bare decorator (no argument list, e.g. ``@rextio.native  # note``) spans
      only the token, leaving any trailing comment untouched;
    * when an ``OP '('`` follows, the span ends at the token-level matching
      ``OP ')'`` (paren depth over ``OP`` tokens only). Because parens inside a
      string or comment are ``STRING``/``COMMENT`` tokens, no string form can
      confuse the depth: ``target="a)b"``, ``target="rust(builtin)"`` and even a
      single-line triple-quoted ``target='''a')b'''`` span the real close. This
      settled the rounds 17-19 string-edge class.
    * an argument list that does NOT close on this line (multi-line), or any line
      tokenize rejects (e.g. an unterminated string), returns ``None`` -- the
      quick fix is withheld rather than emit a corrupting edit.

    Token columns are 0-based code-point offsets into ``line`` (the same indices
    ``line[:start]`` uses), so they are used directly with no conversion.
    """
    tokens = _line_tokens(line)
    if tokens is None:
        return None
    op, name = tokenize.OP, tokenize.NAME
    for i in range(len(tokens) - 3):
        if (
            tokens[i].type == op
            and tokens[i].string == "@"
            and tokens[i + 1].type == name
            and tokens[i + 1].string == "rextio"
            and tokens[i + 2].type == op
            and tokens[i + 2].string == "."
            and tokens[i + 3].type == name
            and tokens[i + 3].string == "native"
        ):
            start = tokens[i].start[1]
            native_end = tokens[i + 3].end[1]
            nxt = tokens[i + 4] if i + 4 < len(tokens) else None
            if nxt is None or nxt.type != op or nxt.string != "(":
                return start, native_end  # bare decorator: replace only the token
            depth = 0
            for tok in tokens[i + 4 :]:
                if tok.type != op:
                    continue
                if tok.string == "(":
                    depth += 1
                elif tok.string == ")":
                    depth -= 1
                    if depth == 0:
                        return start, tok.end[1]
            # A balanced line always closes the opener (an unclosed bracket is a
            # TokenError already turned into None above); withhold defensively.
            return None
    return None


def _function_for_diagnostic(
    report: ProjectReport, file_path: str, diagnostic: lsp.Diagnostic
) -> FunctionReport | None:
    """Find the function that owns ``diagnostic`` by matching code and line."""
    for fn in report.functions_in_file(file_path):
        for record in fn.diagnostics:
            if record.code == diagnostic.code and lsp_line(record.line) == (
                diagnostic.range.start.line
            ):
                return fn
    return None


def code_actions_for(
    report: ProjectReport,
    *,
    file_path: str,
    uri: str,
    document_text: str,
    context_diagnostics: list[lsp.Diagnostic],
) -> list[lsp.CodeAction]:
    """Build quick-fix code actions for the rextio diagnostics in context.

    The only M2 action: for a ``native_status == "rejected"`` function whose def
    carries exactly one explicit ``@rextio.native`` decorator, offer replacing
    that decorator with ``@rextio.exempt`` (indentation preserved).
    """
    lines = document_text.splitlines()
    actions: list[lsp.CodeAction] = []
    seen: set[str] = set()
    for diagnostic in context_diagnostics:
        if diagnostic.source != "rextio":
            continue
        fn = _function_for_diagnostic(report, file_path, diagnostic)
        if fn is None or fn.native_status != "rejected" or fn.qualname in seen:
            continue
        dec_idx = find_native_decorator_line(lines, fn.line)
        if dec_idx is None:
            continue
        original = lines[dec_idx]
        span = _native_exempt_span(original)
        if span is None:
            # No token match, or a multi-line argument list whose span cannot be
            # replaced on a single line without corrupting code: withhold the fix.
            continue
        start_char, end_char = span
        seen.add(fn.qualname)
        # Replace only the decorator token span (``@rextio.native`` plus any
        # single-line ``(...)`` args) so indentation and trailing comments are
        # preserved.
        edit = lsp.TextEdit(
            range=lsp.Range(
                start=lsp.Position(
                    line=dec_idx, character=utf16_len(original[:start_char])
                ),
                end=lsp.Position(
                    line=dec_idx, character=utf16_len(original[:end_char])
                ),
            ),
            new_text="@rextio.exempt",
        )
        actions.append(
            lsp.CodeAction(
                title=EXEMPT_ACTION_TITLE,
                kind=lsp.CodeActionKind.QuickFix,
                diagnostics=[diagnostic],
                edit=lsp.WorkspaceEdit(changes={uri: [edit]}),
            )
        )
    return actions


# --------------------------------------------------------------------------- #
# Server.
# --------------------------------------------------------------------------- #
class RextioLanguageServer(LanguageServer):
    """Language server that analyzes Rextio projects on open/save."""

    def __init__(self) -> None:
        super().__init__(name="rextio-lsp", version=__version__)
        self.engine = Engine()
        self.init_options = InitializationOptions()
        self._reports: dict[str, ProjectReport] = {}
        self._degraded: dict[str, bool] = {}
        self._timers: dict[str, threading.Timer] = {}
        # Last whole-project check duration (seconds), keyed by resolved root.
        self._last_duration: dict[str, float] = {}
        # URIs the server last published diagnostics for, per resolved root, so
        # stale diagnostics can be cleared exactly (see _set_published/_clear).
        self._published_uris: dict[str, set[str]] = {}
        # Current owning root (key) of each published URI. A doc whose owning
        # root changes (a nested rextio.toml appears) must not have its fresh
        # diagnostics wiped by the old root's next clear pass -- clearing skips a
        # URI now owned by a different root (see _set_published/_clear_project).
        self._uri_owner: dict[str, str] = {}
        # Synthetic project-scope URIs (a project's rextio.toml carrying
        # top-level diagnostics), keyed by root. Tracked so handle_did_close does
        # not wipe project-scope diagnostics when the toml document is closed.
        self._project_scope_uris: dict[str, str] = {}
        # Per-root analysis generation. Bumped under _state_lock on every toml
        # Changed/Deleted and in _clear_project; a _run_analysis captures the
        # generation before analyzing and discards its result (no store, no
        # publish) if the generation moved mid-run -- so a deleted-toml clear is
        # never overwritten by an analysis already in flight for that root.
        self._generation: dict[str, int] = {}
        # Roots with an analysis currently running, and roots whose debounce
        # fired while one was running (re-armed when it finishes) -- the
        # in-flight guard that stops duplicate concurrent analyses.
        self._analyzing: set[str] = set()
        self._rerun_pending: set[str] = set()
        self._state_lock = threading.Lock()

    # -- initialization ----------------------------------------------------- #
    def apply_initialization_options(self, raw: Any) -> None:
        """Parse ``initializationOptions`` and register conditional features.

        Called from the INITIALIZE handler (before capabilities are computed) so
        that code lens is advertised only when enabled. Code actions are always
        registered (quick fixes for rextio diagnostics only).
        """
        options = parse_initialization_options(raw)
        self.init_options = options
        self.engine.interpreter_path = options.interpreter_path

        features = self.protocol.fm.features
        if lsp.TEXT_DOCUMENT_CODE_ACTION not in features:

            @self.feature(
                lsp.TEXT_DOCUMENT_CODE_ACTION,
                lsp.CodeActionOptions(code_action_kinds=[lsp.CodeActionKind.QuickFix]),
            )
            def _code_action(
                ls: RextioLanguageServer, params: lsp.CodeActionParams
            ) -> list[lsp.CodeAction] | None:
                return ls.code_action(params)

        if options.code_lens_enabled and lsp.TEXT_DOCUMENT_CODE_LENS not in features:

            @self.feature(lsp.TEXT_DOCUMENT_CODE_LENS)
            def _code_lens(
                ls: RextioLanguageServer, params: lsp.CodeLensParams
            ) -> list[lsp.CodeLens] | None:
                return ls.code_lens(params)

    def register_watched_files(self) -> None:
        """Ask the client (if capable) to watch ``**/rextio.toml`` for changes.

        Best-effort dynamic registration; the notification handler works
        regardless of whether this registration is accepted.
        """
        caps = self.client_capabilities
        workspace = getattr(caps, "workspace", None)
        watched = getattr(workspace, "did_change_watched_files", None)
        if not getattr(watched, "dynamic_registration", False):
            return
        try:
            self.client_register_capability(
                lsp.RegistrationParams(
                    registrations=[
                        lsp.Registration(
                            id="rextio-watch-toml",
                            method=lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                            register_options=lsp.DidChangeWatchedFilesRegistrationOptions(
                                watchers=[
                                    lsp.FileSystemWatcher(
                                        glob_pattern=f"**/{CONFIG_FILENAME}"
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )
        except Exception:  # noqa: BLE001 -- registration is best-effort
            logger.debug("client/registerCapability for watched files unavailable")

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
        # In-flight guard: coalesce a debounce firing while an analysis for this
        # root is already running -- re-arm once instead of running a duplicate
        # concurrently (rapid saves collapse to a single trailing re-run).
        with self._state_lock:
            self._timers.pop(root, None)
            if root in self._analyzing:
                self._rerun_pending.add(root)
                return
            self._analyzing.add(root)
            generation = self._generation.get(root, 0)
        try:
            self.analyze_project(Path(root), generation=generation)
        except Exception:  # noqa: BLE001 -- analysis must never crash the server
            logger.exception("analysis failed for %s", root)
        finally:
            with self._state_lock:
                self._analyzing.discard(root)
                rerun = root in self._rerun_pending
                self._rerun_pending.discard(root)
            if rerun:
                self._debounce(root)

    def analyze_project(
        self, project_root: Path, *, generation: int | None = None
    ) -> ProjectReport | None:
        """Run a whole-project check and publish diagnostics for open docs.

        ``generation`` is the per-root analysis generation captured before this
        run started (see :attr:`_generation`). When supplied, the result is
        discarded -- no report stored, nothing published -- if the generation
        moved while ``engine.check`` was running (the toml was changed or
        deleted mid-run), so an in-flight analysis can never resurrect stale
        diagnostics after a clear. ``None`` (direct callers/tests) skips the
        guard.
        """
        # rextio emits fully-resolved absolute paths; resolve here too so cache
        # keys and file-path matching agree across symlinks (e.g. macOS
        # /var -> /private/var).
        project_root = project_root.resolve()
        key = str(project_root)
        started = time.perf_counter()
        report = self.engine.check(project_root)
        self._record_check_duration(project_root, time.perf_counter() - started)
        # Early gate: a cheap fast-path discard when the toml changed/was deleted
        # while ``engine.check`` ran. The authoritative gate is the final one
        # below (this one just avoids the work in between when already stale).
        if generation is not None:
            with self._state_lock:
                stale = self._generation.get(key, 0) != generation
            if stale:
                logger.info(
                    "discarding stale analysis for %s (rextio.toml changed mid-run)",
                    project_root,
                )
                return None
        if report is None:
            # A previously-analyzed root that now returns nothing (rextio became
            # unavailable): drop its cached state and clear its diagnostics so
            # stale markers do not linger. A never-analyzed root is a silent no-op.
            self._clear_project(project_root)
            logger.info("rextio unavailable for %s; no-op", project_root)
            return None

        degraded = not is_contract_supported(report.contract_version)
        if degraded:
            logger.warning(
                "unsupported contract_version %r for %s; degrading to generic diagnostics",
                report.contract_version,
                project_root,
            )

        # Warm the guidance manifest (used by hover) BEFORE the final gate: the
        # manifest is cache-keyed by the current config, so warming even when the
        # config may already have changed is harmless -- and doing it here keeps
        # the final gate the LAST thing before the store, so this potentially
        # slow acquisition cannot run *after* the gate and re-open the race.
        if not degraded:
            self.engine.capabilities(project_root)

        # Final generation gate. Invariant: once the generation has moved -- a
        # toml Changed/Deleted landed while this analysis was in flight, INCLUDING
        # during the capabilities warm above -- we neither store the report nor
        # publish its diagnostics. The re-check and the store are one atomic step
        # under ``_state_lock``, so a bump cannot slip between them. Combined with
        # the early gate, an in-flight analysis can never resurrect stale
        # diagnostics after a clear.
        with self._state_lock:
            stale = generation is not None and self._generation.get(key, 0) != generation
            if not stale:
                self._reports[key] = report
                self._degraded[key] = degraded
        if stale:
            logger.info(
                "discarding stale analysis for %s (rextio.toml changed mid-run)",
                project_root,
            )
            return None

        self._publish_for_project(project_root, report, degraded=degraded)
        return report

    def _publish_for_project(
        self, project_root: Path, report: ProjectReport, *, degraded: bool
    ) -> None:
        published: set[str] = set()
        for doc in list(self.workspace.text_documents.values()):
            doc_path = uri_to_path(doc.uri)
            if doc_path is None:
                continue
            resolved = doc_path.resolve()
            if find_project_root(resolved) != project_root:
                continue
            lines = doc.source.splitlines()
            diagnostics = diagnostics_for_file(
                report, str(resolved), degraded=degraded, lines=lines
            )
            self._publish(doc.uri, diagnostics)
            published.add(doc.uri)

        # Project-scope diagnostics (top-level, no file_path) attach to the
        # project's rextio.toml since there is no source file to carry them. The
        # toml URI is a synthetic target (tracked in _project_scope_uris) so
        # closing the toml document does not wipe these -- see handle_did_close.
        scope = project_scope_diagnostics(report, degraded=degraded)
        key = str(project_root)
        if scope:
            toml_uri = (project_root / CONFIG_FILENAME).as_uri()
            self._publish(toml_uri, scope)
            published.add(toml_uri)
            with self._state_lock:
                self._project_scope_uris[key] = toml_uri
        else:
            with self._state_lock:
                self._project_scope_uris.pop(key, None)

        self._set_published(key, published)

    # -- publish tracking / clearing --------------------------------------- #
    def _publish(self, uri: str, diagnostics: list[lsp.Diagnostic]) -> None:
        self.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
        )

    def _set_published(self, root_key: str, uris: set[str]) -> None:
        """Record the URIs published for a root, clearing any it dropped.

        A dropped URI now owned by a *different* root (a doc that migrated to a
        newly-appeared nested root) is NOT cleared: its fresh diagnostics belong
        to the new owner. Only URIs still owned by this root are cleared.
        """
        with self._state_lock:
            previous = self._published_uris.get(root_key, set())
            self._published_uris[root_key] = set(uris)
            for uri in uris:
                self._uri_owner[uri] = root_key
            to_clear: list[str] = []
            for uri in previous - uris:
                # Clear unless the URI is now owned by a DIFFERENT root (a
                # migrated doc); an unrecorded owner defaults to this root.
                if self._uri_owner.get(uri, root_key) == root_key:
                    self._uri_owner.pop(uri, None)
                    to_clear.append(uri)
        for uri in to_clear:
            self._publish(uri, [])

    def _clear_project(self, project_root: Path) -> None:
        """Drop cached state for a root and clear every URI it last published.

        Bumps the root's analysis generation so any in-flight analysis discards
        its result instead of re-publishing (see :meth:`analyze_project`). A URI
        now owned by a different root is left intact.
        """
        key = str(project_root)
        with self._state_lock:
            self._reports.pop(key, None)
            self._degraded.pop(key, None)
            self._project_scope_uris.pop(key, None)
            self._generation[key] = self._generation.get(key, 0) + 1
            previous = self._published_uris.pop(key, set())
            to_clear: list[str] = []
            for uri in previous:
                # An unrecorded owner defaults to this root (clearable); only a
                # URI now owned by a different root is left intact.
                if self._uri_owner.get(uri, key) == key:
                    self._uri_owner.pop(uri, None)
                    to_clear.append(uri)
        for uri in to_clear:
            self._publish(uri, [])

    def handle_did_close(self, uri: str) -> None:
        """Clear diagnostics for a closed document and forget its publish record.

        A synthetic project-scope URI (a rextio.toml carrying project-level
        diagnostics) is exempt: closing the toml document must not wipe those
        diagnostics, so the publish record and ownership are left intact.
        """
        with self._state_lock:
            if uri in self._project_scope_uris.values():
                return
            for uris in self._published_uris.values():
                uris.discard(uri)
            self._uri_owner.pop(uri, None)
        self._publish(uri, [])

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
            # Do not analyze synchronously in the request path (it would block
            # the handler and duplicate the debounced background check); schedule
            # it and return no hover for now.
            self.schedule_analysis_for_uri(uri)
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

    # -- latency ------------------------------------------------------------ #
    def _record_check_duration(self, project_root: Path, elapsed: float) -> None:
        """Store the last check duration and log it (Info when slow, else Log)."""
        self._last_duration[str(project_root)] = elapsed
        msg_type, message = latency_log(project_root, elapsed)
        try:
            self.window_log_message(lsp.LogMessageParams(type=msg_type, message=message))
        except Exception:  # noqa: BLE001 -- logging must never break analysis
            logger.debug("window/logMessage unavailable: %s", message)

    # -- code lens ---------------------------------------------------------- #
    def code_lens(self, params: lsp.CodeLensParams) -> list[lsp.CodeLens] | None:
        """Return route lenses for the requested document, if analyzed."""
        uri = params.text_document.uri
        result = self._report_and_path_for_uri(uri)
        if result is None:
            return None
        report, resolved = result
        lines = self.workspace.get_text_document(uri).source.splitlines()
        return code_lenses_for(report, str(resolved), lines=lines)

    # -- code actions ------------------------------------------------------- #
    def code_action(self, params: lsp.CodeActionParams) -> list[lsp.CodeAction] | None:
        """Return rextio quick fixes for the diagnostics in the request context."""
        uri = params.text_document.uri
        result = self._report_and_path_for_uri(uri)
        if result is None:
            return None
        report, resolved = result
        document = self.workspace.get_text_document(uri)
        return code_actions_for(
            report,
            file_path=str(resolved),
            uri=uri,
            document_text=document.source,
            context_diagnostics=list(params.context.diagnostics),
        )

    def _report_and_path_for_uri(
        self, uri: str
    ) -> tuple[ProjectReport, Path] | None:
        """Resolve ``uri`` to (cached-or-fresh report, resolved path) or ``None``."""
        path = uri_to_path(uri)
        if path is None:
            return None
        resolved = path.resolve()
        root = find_project_root(resolved)
        if root is None:
            return None
        report = self._reports.get(str(root.resolve()))
        if report is None:
            # Report-miss: schedule the debounced background analysis rather than
            # running it synchronously in this request path; no result for now.
            self.schedule_analysis_for_uri(uri)
            return None
        return report, resolved

    # -- rextio.toml watch -------------------------------------------------- #
    def handle_watched_files_change(
        self, params: lsp.DidChangeWatchedFilesParams
    ) -> None:
        """On a ``rextio.toml`` change, drop its cache entry and re-analyze.

        A deletion instead clears the project entirely: its manifest cache is
        dropped, any pending debounce timer is cancelled, and its published
        diagnostics are cleared (the root is no longer a rextio project), rather
        than re-debouncing an analysis that would no-op. Both branches bump the
        root's analysis generation so an analysis already in flight for that
        root discards its (now stale) result instead of re-publishing.
        """
        for change in params.changes:
            path = uri_to_path(change.uri)
            if path is None or path.name != CONFIG_FILENAME:
                continue
            root = path.parent.resolve()
            key = str(root)
            self.engine.invalidate(root)
            if change.type == lsp.FileChangeType.Deleted:
                with self._state_lock:
                    timer = self._timers.pop(key, None)
                    self._rerun_pending.discard(key)
                if timer is not None:
                    timer.cancel()
                self._clear_project(root)  # bumps generation, clears diagnostics
                continue
            with self._state_lock:
                self._reports.pop(key, None)
                self._degraded.pop(key, None)
                self._generation[key] = self._generation.get(key, 0) + 1
            self._debounce(key)


def create_server() -> RextioLanguageServer:
    """Construct the server and register the (narrow) feature handlers.

    Code lens and code actions are registered later, from the INITIALIZE
    handler, so code lens can be advertised conditionally on
    ``initializationOptions.codeLens.enable``.
    """
    server = RextioLanguageServer()

    @server.feature(lsp.INITIALIZE)
    def _initialize(ls: RextioLanguageServer, params: lsp.InitializeParams) -> None:
        ls.apply_initialization_options(getattr(params, "initialization_options", None))

    @server.feature(lsp.INITIALIZED)
    def _initialized(ls: RextioLanguageServer, params: lsp.InitializedParams) -> None:
        ls.register_watched_files()

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    def _did_open(ls: RextioLanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
        if params.text_document.uri.endswith(".py"):
            ls.schedule_analysis_for_uri(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def _did_save(ls: RextioLanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
        if params.text_document.uri.endswith(".py"):
            ls.schedule_analysis_for_uri(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
    def _did_close(ls: RextioLanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
        ls.handle_did_close(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    def _hover(ls: RextioLanguageServer, params: lsp.HoverParams) -> lsp.Hover | None:
        return ls.hover_for(params.text_document.uri, params.position)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    def _watched(
        ls: RextioLanguageServer, params: lsp.DidChangeWatchedFilesParams
    ) -> None:
        ls.handle_watched_files_change(params)

    return server
