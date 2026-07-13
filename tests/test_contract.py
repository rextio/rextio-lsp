"""Unit tests for contract parsing, version gating, and guidance lookup."""

from __future__ import annotations

from rextio_lsp.contract import (
    INFORMATIONAL_CODES,
    SUPPORTED_CONTRACT_MAJORS,
    CapabilityManifest,
    codepoint_character,
    is_contract_supported,
    lsp_character,
    lsp_line,
    parse_capabilities,
    parse_check_report,
    parse_major,
    uses_legacy_rxt000_columns,
    utf16_character,
    utf16_len,
)


def test_parse_check_report_flattens_functions(check_boundary):
    report = parse_check_report(check_boundary)
    assert report.contract_version in {"1.0.0", "2.0.0"}
    assert parse_major(report.contract_version) in SUPPORTED_CONTRACT_MAJORS
    qualnames = {fn.qualname for fn in report.functions}
    assert "boundary_demo.pipeline.compute_rejected" in qualnames
    assert "boundary_demo.pipeline.square" in qualnames


def test_parse_check_report_rejection_shape(check_boundary):
    report = parse_check_report(check_boundary)
    rejected = next(
        fn for fn in report.functions if fn.qualname == "boundary_demo.pipeline.compute_rejected"
    )
    assert rejected.native_status == "rejected"
    assert rejected.route == "fallback-python"
    assert rejected.rejection_codes == ("RXT070",)
    assert any(d.code == "RXT070" for d in rejected.diagnostics)


def test_parse_check_report_accepted_shape(check_pure_math):
    report = parse_check_report(check_pure_math)
    accepted = next(
        fn for fn in report.functions if fn.qualname == "pure_math.math_ops.sum_squares"
    )
    assert accepted.native_status == "accepted"
    assert accepted.route == "native-direct"
    assert accepted.rejection_codes == ()


def test_functions_in_file_filters_by_path(check_boundary):
    report = parse_check_report(check_boundary)
    path = "/Volumes/Data/workspace/rextio/rextio/examples/boundary_demo/src/boundary_demo/pipeline.py"
    fns = report.functions_in_file(path)
    assert fns
    assert all(fn.file_path == path for fn in fns)


def test_position_conversion_is_asymmetric():
    # line is 1-based -> decrement; column is 0-based -> passthrough.
    assert lsp_line(5) == 4
    assert lsp_line(28) == 27
    assert lsp_character(0) == 0
    assert lsp_character(11) == 11
    # never negative
    assert lsp_line(0) == 0
    assert lsp_character(-3) == 0


def test_parse_major():
    assert parse_major("1.0.0") == 1
    assert parse_major("2.3.1") == 2
    assert parse_major("") is None
    assert parse_major(None) is None
    assert parse_major("weird") is None


def test_contract_version_gate():
    # Dual-map server: majors 1 (legacy RXT000) and 2 (standardized) are supported.
    assert SUPPORTED_CONTRACT_MAJORS == frozenset({1, 2})
    assert is_contract_supported("1.0.0") is True
    assert is_contract_supported("1.9.9") is True
    assert is_contract_supported("2.0.0") is True
    assert is_contract_supported("2.1.0") is True
    # Major 3+ and junk are unsupported so old "silent accept" cannot recur.
    assert is_contract_supported("3.0.0") is False
    assert is_contract_supported(None) is False
    assert is_contract_supported("") is False


def test_uses_legacy_rxt000_columns_only_for_major_1():
    assert uses_legacy_rxt000_columns("1.0.0") is True
    assert uses_legacy_rxt000_columns("1.9.9") is True
    assert uses_legacy_rxt000_columns("2.0.0") is False
    assert uses_legacy_rxt000_columns("2.1.0") is False
    assert uses_legacy_rxt000_columns(None) is False
    assert uses_legacy_rxt000_columns("weird") is False


def test_parse_capabilities_and_guidance_lookup(capabilities_boundary):
    manifest = parse_capabilities(capabilities_boundary)
    assert parse_major(manifest.contract_version) in SUPPORTED_CONTRACT_MAJORS
    assert manifest.config_fingerprint
    assert manifest.rules

    rule = manifest.guidance_for("RXT070")
    assert rule is not None
    assert rule.diagnostic_code == "RXT070"
    assert rule.guidance
    assert rule.outcome == "reject"

    # cached index returns the same object on repeat lookups
    assert manifest.guidance_for("RXT070") is rule
    assert manifest.guidance_for("RXT-does-not-exist") is None


def test_informational_codes_constant():
    assert {"RXT075", "RXT080", "RXT090", "RXT091"} == set(INFORMATIONAL_CODES)


def test_parse_tolerates_unknown_and_missing_fields():
    report = parse_check_report({"contract_version": "1.0.0", "surprise": 1})
    assert report.functions == ()
    manifest = parse_capabilities({"contract_version": "1.0.0"})
    assert manifest.rules == ()


def test_parse_check_report_captures_top_level_diagnostics(check_syntax_error):
    report = parse_check_report(check_syntax_error)
    assert report.functions == ()
    assert len(report.top_level_diagnostics) == 1
    diag = report.top_level_diagnostics[0]
    assert diag.code == "RXT000"
    assert diag.file_path == "/proj/src/broken/bad.py"


def test_parse_tolerates_null_line_and_column():
    # Core serializes SyntaxError.lineno/offset verbatim into RXT000, and CPython
    # can emit an explicit null for either (e.g. source with a NUL byte). A null
    # must fall back to the defaults instead of raising TypeError and aborting the
    # WHOLE report parse.
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "diagnostics": [
                {
                    "code": "RXT000",
                    "message": "invalid syntax",
                    "severity": "error",
                    "file_path": "/proj/bad.py",
                    "line": None,
                    "column": None,
                }
            ],
        }
    )
    assert len(report.top_level_diagnostics) == 1
    diag = report.top_level_diagnostics[0]
    assert diag.line == 1  # null line -> default 1
    assert diag.column == 0  # null column -> default 0


def test_parse_tolerates_junk_column_and_function_position():
    # A non-int-coercible junk column is tolerated (default 0), and null/junk
    # positions on a FUNCTION record fall back too rather than aborting the parse.
    report = parse_check_report(
        {
            "contract_version": "1.0.0",
            "modules": [
                {
                    "file_path": "/proj/ops.py",
                    "functions": [
                        {
                            "qualname": "ops.f",
                            "name": "f",
                            "file_path": "/proj/ops.py",
                            "line": None,
                            "column": "nope",
                            "route": "fallback-python",
                            "native_status": "rejected",
                            "rejection_codes": ["RXT070"],
                            "diagnostics": [
                                {
                                    "code": "RXT070",
                                    "message": "m",
                                    "severity": "error",
                                    "file_path": "/proj/ops.py",
                                    "line": 3,
                                    "column": "junk",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    (fn,) = report.functions
    assert fn.line == 1  # null line -> default 1
    assert fn.column == 0  # junk column -> default 0
    (diag,) = fn.diagnostics
    assert diag.column == 0  # junk column -> default 0
    assert diag.line == 3  # a valid position is preserved


# --------------------------------------------------------------------------- #
# UTF-8 byte offset -> UTF-16 code unit conversion.
# --------------------------------------------------------------------------- #
def test_utf16_character_korean_line():
    # ast.col_offset is a UTF-8 byte offset: `f` sits at byte 15 but UTF-16
    # index 11 (each Hangul syllable is 3 UTF-8 bytes but 1 UTF-16 unit).
    line = 'x = "한글" + f(1)'
    assert line.encode("utf-8")[15:16] == b"f"
    assert utf16_character(line, 15) == 11


def test_utf16_character_emoji_is_surrogate_pair():
    # a non-BMP emoji counts as TWO UTF-16 code units (surrogate pair).
    line = 'y = "😀" + g()'
    assert utf16_character(line, 13) == 11  # `g` byte 13 -> utf16 index 11
    assert utf16_len("😀") == 2


def test_utf16_character_ascii_passthrough():
    assert utf16_character("abcdef", 4) == 4
    assert utf16_character("abc", 0) == 0
    assert utf16_character("abc", -1) == 0


def test_utf16_character_offset_inside_multibyte_sequence():
    # a byte offset landing mid-character decodes the largest valid prefix.
    line = "한글"  # 6 UTF-8 bytes, 2 UTF-16 units
    assert utf16_character(line, 1) == 0  # inside the first 3-byte sequence
    assert utf16_character(line, 3) == 1  # exactly after the first char


def test_codepoint_character_legacy_rxt000_mapping():
    # Legacy major-1 RXT000: 1-based code points → UTF-16 units.
    line = 'x = "한글😀" + ('
    # SyntaxError.offset 13 (1-based) points at '('; UTF-16 index of '(' is 13.
    assert codepoint_character(line, 13) == 13
    # Astral: 𝐀 is one code point / two UTF-16 units.
    astral = "𝐀 = ("
    assert codepoint_character(astral, 5) == 5  # 1-based points at '('


# --------------------------------------------------------------------------- #
# Composite manifest cache key (tooling-contract lines ~141-150).
# --------------------------------------------------------------------------- #
def _manifest(fingerprint, version, plugins):
    return CapabilityManifest(
        contract_version="1.0.0",
        config_fingerprint=fingerprint,
        rextio_version=version,
        project_root="/proj",
        plugins=tuple(plugins),
    )


def test_cache_key_folds_fingerprint_version_and_plugins():
    m1 = _manifest("fp", "0.1.1", [{"id": "a", "version": "1.0"}])
    m2 = _manifest("fp", "0.1.1", [{"id": "a", "version": "1.0"}])
    assert m1.cache_key() == m2.cache_key()
    # rextio version participates
    assert m1.cache_key() != _manifest("fp", "0.1.2", [{"id": "a", "version": "1.0"}]).cache_key()
    # plugin version participates
    assert m1.cache_key() != _manifest("fp", "0.1.1", [{"id": "a", "version": "2.0"}]).cache_key()
    # fingerprint participates
    assert m1.cache_key() != _manifest("gp", "0.1.1", [{"id": "a", "version": "1.0"}]).cache_key()


def test_cache_key_plugin_order_insensitive():
    a = _manifest("fp", "0.1.1", [{"id": "a", "version": "1.0"}, {"id": "b", "version": "2.0"}])
    b = _manifest("fp", "0.1.1", [{"id": "b", "version": "2.0"}, {"id": "a", "version": "1.0"}])
    assert a.cache_key() == b.cache_key()


def test_cache_key_null_plugin_version_is_uncacheable():
    m = _manifest("fp", "0.1.1", [{"id": "a", "version": None}])
    assert m.cache_key() is None
    # a mix with one null version is still uncacheable
    mixed = _manifest("fp", "0.1.1", [{"id": "a", "version": "1.0"}, {"id": "b", "version": None}])
    assert mixed.cache_key() is None


def test_cache_key_no_plugins_is_cacheable():
    m = _manifest("fp", "0.1.1", [])
    assert m.cache_key() is not None
