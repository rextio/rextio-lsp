"""Unit tests for contract parsing, version gating, and guidance lookup."""

from __future__ import annotations

from rextio_lsp.contract import (
    INFORMATIONAL_CODES,
    is_contract_supported,
    lsp_character,
    lsp_line,
    parse_capabilities,
    parse_check_report,
    parse_major,
)


def test_parse_check_report_flattens_functions(check_boundary):
    report = parse_check_report(check_boundary)
    assert report.contract_version == "1.0.0"
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
    assert is_contract_supported("1.0.0") is True
    assert is_contract_supported("1.9.9") is True
    assert is_contract_supported("2.0.0") is False
    assert is_contract_supported(None) is False


def test_parse_capabilities_and_guidance_lookup(capabilities_boundary):
    manifest = parse_capabilities(capabilities_boundary)
    assert manifest.contract_version == "1.0.0"
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
