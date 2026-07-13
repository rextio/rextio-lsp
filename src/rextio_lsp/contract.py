"""Rextio tooling-contract shapes, parsing, and contract-version gating.

This module is the LSP server's data model. It intentionally re-derives small
frozen dataclasses from the contract JSON rather than importing any rextio
internal type, so the server stays decoupled from analyzer internals and only
depends on the documented contract surface (see
``rextio/docs/specs/tooling-contract.md``).

Two JSON surfaces are parsed:

* ``rextio check --format json`` -> :class:`ProjectReport` (per-function routes
  and diagnostics).
* ``rextio capabilities --format json`` -> :class:`CapabilityManifest` (rule
  records used for guidance lookup).

Positions in the contract follow Python's ``ast`` conventions: ``line`` is
1-based. Under contract major 2, every ``column`` (including ``RXT000``) is a
0-based UTF-8 byte offset (``ast.col_offset``). Contract major 1 left
``RXT000.column`` as CPython's 1-based Unicode code-point
``SyntaxError.offset``; see :func:`uses_legacy_rxt000_columns`. LSP positions
are fully 0-based UTF-16 code units.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Contract majors this server fully understands. Major 1 = legacy RXT000
# code-point columns; major 2 = standardized UTF-8 byte columns for every
# diagnostic. A check/capabilities payload whose major is outside this set
# triggers degraded (generic) diagnostics -- see :func:`is_contract_supported`.
# A frozenset (not a single int) is required so a major-1-only gate cannot
# silently accept a 2.x producer and mis-map RXT000, and so this server can
# still map both producers correctly.
SUPPORTED_CONTRACT_MAJORS: frozenset[int] = frozenset({1, 2})

# Back-compat alias for callers/tests that still import the singular name.
# Equals the highest supported major; prefer :data:`SUPPORTED_CONTRACT_MAJORS`.
SUPPORTED_CONTRACT_MAJOR = 2

# Codes that are informational notes/hints rather than promotion blockers. They
# map to a low LSP severity regardless of the rextio-side severity string. Kept
# here (not in the server) so it is unit-testable without pygls.
INFORMATIONAL_CODES = frozenset({"RXT075", "RXT080", "RXT090", "RXT091"})


def lsp_line(contract_line: int) -> int:
    """Convert a 1-based contract line to a 0-based LSP line (never negative)."""
    return max(contract_line - 1, 0)


def lsp_character(contract_column: int) -> int:
    """Convert a contract column to a 0-based LSP character.

    Contract columns are already 0-based ``ast.col_offset`` values -- but they
    are UTF-8 *byte* offsets, whereas LSP positions default to UTF-16 code
    units. This clamp is the byte-offset *fallback* used when the document's
    line text is unavailable (see :func:`utf16_character` for the accurate
    conversion). It also guards against malformed negative offsets.
    """
    return max(contract_column, 0)


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (non-BMP chars count as 2)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def utf16_character(line_text: str, byte_offset: int) -> int:
    """Map a UTF-8 byte offset within ``line_text`` to a UTF-16 code unit index.

    Contract columns (major 2, and non-RXT000 on major 1) are ``ast.col_offset``
    values: 0-based offsets into the line's UTF-8 *bytes*. LSP positions default
    to UTF-16 code units, so a line with multi-byte characters (e.g.
    ``x = "한글" + f(1)``, where ``f`` is byte 15 but UTF-16 index 11) needs
    conversion. Decode the byte prefix, then sum its UTF-16 code-unit lengths.
    A ``byte_offset`` landing inside a multi-byte sequence decodes the largest
    valid prefix.
    """
    if byte_offset <= 0:
        return 0
    prefix = line_text.encode("utf-8")[:byte_offset]
    try:
        decoded = prefix.decode("utf-8")
    except UnicodeDecodeError:
        decoded = prefix.decode("utf-8", errors="ignore")
    return utf16_len(decoded)


def codepoint_character(line_text: str, one_based_codepoint: int) -> int:
    """Map a 1-based Unicode code-point index to a 0-based UTF-16 unit index.

    Used only for **legacy contract major 1** ``RXT000`` columns
    (CPython ``SyntaxError.offset``). Subtract 1, clamp, then sum UTF-16
    code-unit lengths of the code-point prefix.
    """
    col0 = max(one_based_codepoint - 1, 0)
    return utf16_len(line_text[:col0])


def uses_legacy_rxt000_columns(contract_version: str | None) -> bool:
    """Return whether ``RXT000`` columns are legacy 1-based code points.

    Contract major 1 serialized ``RXT000.column`` as CPython's
    ``SyntaxError.offset`` (1-based Unicode code points). Major 2+ uses the
    same 0-based UTF-8 byte offsets as every other diagnostic.
    """
    return parse_major(contract_version) == 1


@dataclass(frozen=True)
class DiagnosticRecord:
    """One analyzer diagnostic from a check report.

    ``severity`` is the raw rextio severity string (``info``/``warning``/
    ``error``); the LSP severity is derived separately (the contract mandates
    the server never surfaces Error), so it is preserved verbatim here.
    """

    code: str
    message: str
    severity: str
    file_path: str
    line: int
    column: int
    function_name: str | None = None
    suggestion: str | None = None
    # Optional end span (contract line 1-based, column 0-based). Present on many
    # records; when both are set the LSP diagnostic uses the real span rather
    # than a zero-width range (see ``to_lsp_diagnostic``).
    end_line: int | None = None
    end_column: int | None = None


@dataclass(frozen=True)
class FunctionReport:
    """A single analyzed function: where it runs and why."""

    qualname: str
    name: str
    file_path: str
    line: int
    column: int
    route: str
    native_status: str
    rejection_codes: tuple[str, ...] = ()
    diagnostics: tuple[DiagnosticRecord, ...] = ()


@dataclass(frozen=True)
class ProjectReport:
    """Parsed ``check --format json`` payload, flattened to functions."""

    contract_version: str
    project_root: str
    functions: tuple[FunctionReport, ...] = ()
    top_level_diagnostics: tuple[DiagnosticRecord, ...] = ()

    def functions_in_file(self, file_path: str) -> list[FunctionReport]:
        """Return functions whose ``file_path`` matches (order preserved)."""
        return [fn for fn in self.functions if fn.file_path == file_path]


@dataclass(frozen=True)
class RuleRecord:
    """A capability-manifest rule record (L2 fields)."""

    id: str
    provider: str
    diagnostic_code: str | None
    constraint: str
    guidance: str
    outcome: str
    stability: str


@dataclass(frozen=True)
class CapabilityManifest:
    """Parsed ``capabilities --format json`` payload used for guidance lookup."""

    contract_version: str
    config_fingerprint: str
    rextio_version: str
    project_root: str
    rules: tuple[RuleRecord, ...] = ()
    plugins: tuple[dict[str, Any], ...] = ()
    _by_code: dict[str, RuleRecord] = field(default_factory=dict, compare=False, repr=False)

    def guidance_for(self, code: str) -> RuleRecord | None:
        """Look up the rule record whose ``diagnostic_code`` equals ``code``."""
        if not self._by_code:
            for rule in self.rules:
                if rule.diagnostic_code and rule.diagnostic_code not in self._by_code:
                    self._by_code[rule.diagnostic_code] = rule
        return self._by_code.get(code)

    def cache_key(self) -> str | None:
        """Composite cache key, or ``None`` when the manifest is not cache-safe.

        Per the tooling contract, consumers MUST key a cached manifest on
        ``(config_fingerprint, rextio_version, sorted plugin id+version)`` -- the
        fingerprint alone hashes only the resolved config (plugin *ids*, not
        their versions). A plugin whose ``version`` is ``None`` (no distribution
        metadata) is explicitly NOT cache-safe: such a manifest returns ``None``
        so the caller always re-acquires it.
        """
        plugin_parts: list[str] = []
        for plugin in self.plugins:
            version = plugin.get("version")
            if version is None:
                return None
            plugin_parts.append(f"{plugin.get('id', '')}@{version}")
        plugin_parts.sort()
        return "\x00".join(
            (self.config_fingerprint, self.rextio_version, *plugin_parts)
        )


def parse_major(version: str | None) -> int | None:
    """Return the SemVer major of ``version``, or ``None`` if unparseable."""
    if not version:
        return None
    head = version.split(".", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def is_contract_supported(version: str | None) -> bool:
    """Return whether ``version``'s major is in :data:`SUPPORTED_CONTRACT_MAJORS`.

    Majors 1 and 2 are both fully supported; position mapping for ``RXT000``
    branches on the major (see :func:`uses_legacy_rxt000_columns`). Any other
    major (or unparseable version) is unsupported and triggers degraded
    diagnostics.
    """
    major = parse_major(version)
    return major is not None and major in SUPPORTED_CONTRACT_MAJORS


def _optional_int(value: Any) -> int | None:
    """Coerce a raw contract value to ``int`` or ``None`` (never raises)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or(value: Any, default: int) -> int:
    """Coerce a raw contract value to ``int``, or ``default`` for junk/None.

    An explicit JSON ``null`` (``line``/``column`` can both be ``None`` -- core
    serializes ``SyntaxError.lineno``/``offset`` verbatim into RXT000 and CPython
    can emit ``None`` for either, e.g. source containing a NUL byte) reaches
    ``int(None)`` and raises ``TypeError``, which would abort the whole report
    parse. This tolerant coercion falls back to ``default`` instead so one
    malformed position never discards every diagnostic for the project.
    """
    coerced = _optional_int(value)
    return default if coerced is None else coerced


def _parse_diagnostic(raw: dict[str, Any]) -> DiagnosticRecord:
    return DiagnosticRecord(
        code=str(raw.get("code", "")),
        message=str(raw.get("message", "")),
        severity=str(raw.get("severity", "")),
        file_path=str(raw.get("file_path", "")),
        line=_int_or(raw.get("line"), 1),
        column=_int_or(raw.get("column"), 0),
        function_name=raw.get("function_name"),
        suggestion=raw.get("suggestion"),
        end_line=_optional_int(raw.get("end_line")),
        end_column=_optional_int(raw.get("end_column")),
    )


def _parse_function(raw: dict[str, Any]) -> FunctionReport:
    return FunctionReport(
        qualname=str(raw.get("qualname", raw.get("name", ""))),
        name=str(raw.get("name", "")),
        file_path=str(raw.get("file_path", "")),
        line=_int_or(raw.get("line"), 1),
        column=_int_or(raw.get("column"), 0),
        route=str(raw.get("route", "")),
        native_status=str(raw.get("native_status", "")),
        rejection_codes=tuple(str(c) for c in raw.get("rejection_codes", ()) or ()),
        diagnostics=tuple(
            _parse_diagnostic(d) for d in raw.get("diagnostics", ()) or () if isinstance(d, dict)
        ),
    )


def parse_check_report(data: dict[str, Any]) -> ProjectReport:
    """Parse a ``check --format json`` payload into a :class:`ProjectReport`.

    Unknown top-level and per-record fields are ignored (forward compatibility,
    per the contract's "tolerate unknown fields" rule).
    """
    functions: list[FunctionReport] = []
    for module in data.get("modules", ()) or ():
        if not isinstance(module, dict):
            continue
        for fn in module.get("functions", ()) or ():
            if isinstance(fn, dict):
                functions.append(_parse_function(fn))
    top = tuple(
        _parse_diagnostic(d)
        for d in data.get("diagnostics", ()) or ()
        if isinstance(d, dict)
    )
    return ProjectReport(
        contract_version=str(data.get("contract_version", "")),
        project_root=str(data.get("project_root", "")),
        functions=tuple(functions),
        top_level_diagnostics=top,
    )


def _parse_rule(raw: dict[str, Any]) -> RuleRecord:
    return RuleRecord(
        id=str(raw.get("id", "")),
        provider=str(raw.get("provider", "")),
        diagnostic_code=raw.get("diagnostic_code"),
        constraint=str(raw.get("constraint", "")),
        guidance=str(raw.get("guidance", "")),
        outcome=str(raw.get("outcome", "")),
        stability=str(raw.get("stability", "")),
    )


def parse_capabilities(data: dict[str, Any]) -> CapabilityManifest:
    """Parse a ``capabilities --format json`` payload into a manifest."""
    rules = tuple(
        _parse_rule(r) for r in data.get("rules", ()) or () if isinstance(r, dict)
    )
    plugins = tuple(
        p for p in data.get("plugins", ()) or () if isinstance(p, dict)
    )
    return CapabilityManifest(
        contract_version=str(data.get("contract_version", "")),
        config_fingerprint=str(data.get("config_fingerprint", "")),
        rextio_version=str(data.get("rextio_version", "")),
        project_root=str(data.get("project_root", "")),
        rules=rules,
        plugins=plugins,
    )
