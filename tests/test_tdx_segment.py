"""Tests for the Tongdaxin (mootdx) segment fallback."""

from __future__ import annotations

from datetime import date

import pytest

from data.growth_evidence import validate_growth_evidence_record
from data.tdx_segment import parse_f10_segments, tdx_segment_records


def _f10_block() -> str:
    """A minimal F10 经营分析 block with two annual periods."""
    header = """☆经营分析☆ ◇002731 更新日期：2026-07-07◇
★本栏包括【1.主营业务】【2.主营构成分析】【3.经营投资】★
【1.主营业务】
┌────────────────────────────────────────┐
｜从事珠宝饰品的设计、加工、批发和零售。      ｜
└────────────────────────────────────────┘
【2.主营构成分析】
"""
    periods = []
    for report_date, rows in [
        ("2025-12-31", [("黄金产品", "4.0亿", "80.00%"), ("铂金产品", "1.0亿", "20.00%")]),
        ("2025-06-30", [("黄金产品", "2.0亿", "60.00%"), ("铂金产品", "1.0亿", "30.00%")]),
        (
            "2024-12-31",
            [("黄金产品", "3.15亿", "70.00%"), ("铂金产品", "0.9亿", "20.00%"), ("其他", "0.45亿", "10.00%")],
        ),
        ("2024-06-30", [("黄金产品", "1.5亿", "75.00%"), ("铂金产品", "0.5亿", "25.00%")]),
        ("2023-12-31", [("黄金产品", "2.5亿", "80.00%"), ("铂金产品", "0.6亿", "20.00%")]),
    ]:
        table = f"【截止日期】{report_date}\n"
        table += "┌───────────────┬───────┬────┬──┐\n"
        table += "｜项目名｜营业收入(元)｜收入比例｜...｜\n"
        table += "├───────────────┼───────┼────┼──┤\n"
        for name, rev, share in rows:
            table += f"｜{name}(产品)｜{rev}｜{share}｜-｜-｜-｜-｜\n"
        table += "｜合计(产品)｜4.0亿｜100.00%｜-｜-｜-｜-｜\n"
        table += "└───────────────┴───────┴────┴──┘\n"
        periods.append(table)
    return header + "".join(periods)


def test_parse_f10_segments_extracts_only_annual_rows():
    out = parse_f10_segments(_f10_block(), "002731")
    assert out.periods == ("2023-12-31", "2024-12-31", "2025-12-31")
    assert len(out.records) > 0
    dates = {record["report_date"] for record in out.records}
    assert dates == {"2023-12-31", "2024-12-31", "2025-12-31"}
    # 中报期不入 records
    assert all(record["report_date"].endswith("-12-31") for record in out.records)


def test_parse_f10_segments_maps_dimension_and_money():
    out = parse_f10_segments(_f10_block(), "002731")
    product = [record for record in out.records if record["mainop_type"] == 2]
    assert product, "expected product rows"
    assert all(record["dimension"] == "product" for record in product)
    # 2024 黄金产品 3.15亿
    row = next(
        record for record in product if record["report_date"] == "2024-12-31" and record["item_name"] == "黄金产品"
    )
    assert row["revenue"] == pytest.approx(3.15e8)
    assert row["reported_share"] == pytest.approx(0.70)


def test_records_pass_shared_schema_validation():
    out = parse_f10_segments(_f10_block(), "002731")
    normalized = tdx_segment_records(out.records, "002731")
    assert len(normalized) == len(out.records)
    # rank 唯一 per (date, dimension)
    identities = {(r["report_date"], r["mainop_type"], r["rank"]) for r in normalized}
    assert len(identities) == len(normalized)


def test_empty_or_garbage_f10_returns_no_records():
    assert parse_f10_segments("", "002731").records == ()
    assert parse_f10_segments("nothing here", "002731").records == ()
    assert parse_f10_segments("【截止日期】2025-06-30 ｜无表格", "002731").records == ()


def test_evidence_record_roundtrip_validate():
    from data.growth_evidence import (
        _build_segment_growth_sources,
        _validate_cached_segment_records,
        _validate_segment_evidence,
    )
    from data.tdx_segment import _evidence_record

    as_of = date(2026, 8, 10)
    out = parse_f10_segments(_f10_block(), "002731")
    records = _validate_cached_segment_records(out.as_list(), code="002731", as_of=as_of)
    segment = _validate_segment_evidence(
        _build_segment_growth_sources("002731", as_of, records),
        code="002731",
        as_of=as_of,
    )
    record = _evidence_record("002731", as_of, segment)
    normalized = validate_growth_evidence_record(record, "002731", as_of)
    assert normalized["segment_growth_sources"]["status"] == segment["status"]
    assert normalized["external_growth_evidence"]["status"] == "unavailable"
    assert normalized["available"] is False


def test_two_year_partial_segment_proxy_is_accepted():
    """A 2-year partial capture with full revenue coverage must produce a
    usable segment-growth proxy (Type 3 3d evidence)."""
    from data.growth_evidence import (
        _build_segment_growth_sources,
        _validate_cached_segment_records,
        _validate_segment_evidence,
    )
    from engine.quantitative_evidence import _segment_growth_proxy_inputs

    as_of = date(2026, 8, 10)
    # Two annual periods only (2024, 2025) -> partial, but full coverage.
    block = """☆经营分析☆
【2.主营构成分析】
【截止日期】2025-12-31
｜黄金产品(产品)｜4.0亿｜80.00%｜-｜-｜-｜-｜
｜铂金产品(产品)｜1.0亿｜20.00%｜-｜-｜-｜-｜
【截止日期】2024-12-31
｜黄金产品(产品)｜3.15亿｜70.00%｜-｜-｜-｜-｜
｜铂金产品(产品)｜0.9亿｜20.00%｜-｜-｜-｜-｜
｜其他(产品)｜0.45亿｜10.00%｜-｜-｜-｜-｜
"""
    out = parse_f10_segments(block, "002731")
    records = _validate_cached_segment_records(out.as_list(), code="002731", as_of=as_of)
    segment = _validate_segment_evidence(
        _build_segment_growth_sources("002731", as_of, records),
        code="002731",
        as_of=as_of,
    )
    assert segment["status"] == "partial"
    proxy = _segment_growth_proxy_inputs(segment)
    assert proxy is not None
    assert proxy["history_years"] == pytest.approx(2.0)


def test_load_tdx_cache_prefers_at_or_before_as_of_then_newer_same_year(tmp_path, monkeypatch):
    """The Tongdaxin cache lookup must prefer the newest capture at-or-before
    the requested as_of, falling back to newer same-fiscal-year captures."""
    from data import growth_evidence as ge
    from data import tdx_segment as ts

    code = "600285"
    # Capture A: 2026-08-06 (at-or-before the requested 2026-08-07)
    # Capture B: 2026-08-10 (newer than requested, same fiscal year)
    block = _f10_block()
    out_a = parse_f10_segments(block, code)

    def fake_index(cache_dir):
        return {
            code: [
                (date(2026, 8, 10), tmp_path / f"type3-segment-growth-v1_{code}_20260810.json.gz"),
                (date(2026, 8, 6), tmp_path / f"type3-segment-growth-v1_{code}_20260806.json.gz"),
            ]
        }

    monkeypatch.setattr(ge, "_segment_cache_index", fake_index)
    monkeypatch.setattr(ge, "SEGMENT_CACHE_DIR", tmp_path)
    # Simulate a valid cache for the 08-06 capture only; the 08-10 file is a
    # decoy that must not be chosen first (restatement look-ahead guard).
    ts._write_tdx_cache(code, date(2026, 8, 6), out_a.as_list())
    # The 08-10 entry is a VALID cache with different content; the at-or-before
    # preference must still pick the 08-06 capture first.
    decoy_block = """☆经营分析☆
【2.主营构成分析】
【截止日期】2025-12-31
｜其他(产品)｜5.0亿｜100.00%｜-｜-｜-｜-｜
"""
    out_b = parse_f10_segments(decoy_block, code)
    ts._write_tdx_cache(code, date(2026, 8, 10), out_b.as_list())
    record = ts._load_tdx_cache(code, date(2026, 8, 7))
    assert record is not None
    # The 08-06 capture has the gold/platinum products; the decoy has "其他".
    names = {seg.get("item_name") for seg in record["segment_growth_sources"].get("segments", [])}
    assert "黄金产品" in names
    assert names != {"其他"}


def test_load_tdx_cache_falls_back_to_newer_when_at_or_before_stale(tmp_path, monkeypatch):
    """An at-or-before capture outside the reuse window must not block a newer
    same-fiscal-year capture from being reused."""
    from data import growth_evidence as ge
    from data import tdx_segment as ts

    code = "600285"
    block = _f10_block()
    out = parse_f10_segments(block, code)

    # Stale capture: 2026-05-01 (31 days before the requested 2026-08-07)
    # Fresh capture: 2026-08-10 (newer, same fiscal year)
    def fake_index(cache_dir):
        return {
            code: [
                (date(2026, 8, 10), tmp_path / f"type3-segment-growth-v1_{code}_20260810.json.gz"),
                (date(2026, 5, 1), tmp_path / f"type3-segment-growth-v1_{code}_20260501.json.gz"),
            ]
        }

    monkeypatch.setattr(ge, "_segment_cache_index", fake_index)
    monkeypatch.setattr(ge, "SEGMENT_CACHE_DIR", tmp_path)
    ts._write_tdx_cache(code, date(2026, 5, 1), out.as_list())
    ts._write_tdx_cache(code, date(2026, 8, 10), out.as_list())
    record = ts._load_tdx_cache(code, date(2026, 8, 7))
    assert record is not None
    assert record["segment_growth_sources"]["status"] in {"complete", "partial"}
