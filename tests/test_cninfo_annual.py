"""Tests for the CNINFO annual-report acquisition evidence module."""

from __future__ import annotations


import pytest

from data import cninfo_annual as cn
from data.cache import SafeFileCache


class _FakePdf:
    def __init__(self, words_by_page, *, pages=1):
        self._words_by_page = words_by_page
        self.page_count = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_text(self, mode):
        assert mode == "words"
        return self._words_by_page


def _words(lines):
    """Convert (text, x0) rows into PyMuPDF-style word tuples."""
    result = []
    for index, (text, x0) in enumerate(lines):
        y0 = float(index * 12)
        y1 = y0 + 10.0
        for offset, char in enumerate(text):
            result.append((x0 + offset * 4, y0, x0 + offset * 4 + 4, y1, char))
    return result


def test_parse_pdf_number_handles_commas_parentheses_and_signs():
    assert cn._parse_pdf_number("1,234,567.89") == 1234567.89
    assert cn._parse_pdf_number("(949)") == -949.0
    assert cn._parse_pdf_number("0") == 0.0
    assert cn._parse_pdf_number("-12.5") == -12.5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("单位：人民币百万元", "百万元"),
        ("金额单位均为人民币千元", "千元"),
        ("除特别注明外，以人民币万元列示", "万元"),
        ("金额单位：亿元", "亿元"),
        ("本集团以人民币百万元列示", "百万元"),
        ("无单位说明", ""),
    ],
)
def test_detect_unit_patterns(text, expected):
    assert cn._detect_unit(text) == expected


def test_detect_unit_english_millions():
    assert cn._detect_unit("in millions of RMB") == "百万元"


def test_unit_for_page_falls_back_to_previous_page_then_yuan():
    pages = {10: "百万元"}
    assert cn._unit_for_page(pages, 10) == "百万元"
    assert cn._unit_for_page(pages, 12) == "百万元"
    assert cn._unit_for_page(pages, 9) == "元"
    assert cn._unit_for_page({}, 3) == "元"


def test_parse_acquisition_zero_when_dash_and_unit_detected(monkeypatch):
    rows = _words([("取得子公司及其他营业单位支付的现金净额", 10), ("－", 300)])

    class _Doc:
        page_count = 1

        def __iter__(self):
            yield from (self,)

        def get_text(self, mode):
            return rows

        def close(self):
            pass

    monkeypatch.setattr(cn, "_import_fitz", lambda: type("F", (), {"open": staticmethod(lambda *a, **k: _Doc())}))
    value, unit, reason = cn._parse_acquisition_cashflow(b"pdf-bytes", "000429", 2025)
    assert value == 0.0
    assert reason is None
    assert unit in ("", "元")


def test_overlay_constructs_rows_when_eastmoney_records_empty():
    records = []
    overlay = cn_overlay_helper(records, {2024: 5.0, 2025: 0.0}, "600519")
    assert {r["REPORT_DATE"] for r in overlay} == {"2024-12-31", "2025-12-31"}
    by_year = {r["REPORT_DATE"][:4]: r for r in overlay}
    assert by_year["2024"]["OBTAIN_SUBSIDIARY_OTHER"] == 5.0
    assert by_year["2025"]["OBTAIN_SUBSIDIARY_OTHER"] == 0.0
    assert by_year["2024"]["SECURITY_CODE"] == "600519"
    assert by_year["2024"]["SOURCE_REPORT_NAME"] == "CNINFO ANNUAL REPORT"


def cn_overlay_helper(records, values, code):
    from data import growth_evidence as ge

    return ge._overlay_cninfo_acquisition(records, values, code=code)


def test_overlay_rejects_negative_values():
    from data import growth_evidence as ge

    with pytest.raises(ge.GrowthEvidenceError):
        ge._overlay_cninfo_acquisition([], {2024: -1.0}, code="600519")


def test_fetch_annual_acquisition_rejects_bad_inputs(tmp_path):
    with pytest.raises(ValueError):
        cn.fetch_annual_acquisition("123", 2025, cache_dir=tmp_path)
    with pytest.raises(ValueError):
        cn.fetch_annual_acquisition("600519", 1999, cache_dir=tmp_path)


def test_cache_roundtrip(tmp_path):
    cache = SafeFileCache(
        cn._cache_path("600519", 2025, tmp_path),
        schema_version=cn.CACHE_SCHEMA_VERSION,
        ttl=cn.CACHE_TTL_SECONDS,
        max_uncompressed_bytes=1_000_000,
    )
    payload = {
        "contract": cn._cache_contract("600519", 2025),
        "evidence": {
            "code": "600519",
            "year": 2025,
            "available": True,
            "acquisition_cashflow": 0.0,
            "unit": "元",
            "source_url": "http://static.cninfo.com.cn/finalpage/x.PDF",
            "reason": "",
        },
    }
    cache.save(payload)
    loaded = cache.load()
    assert loaded.hit
    assert loaded.value["contract"] == cn._cache_contract("600519", 2025)
    assert loaded.value["evidence"]["acquisition_cashflow"] == 0.0
