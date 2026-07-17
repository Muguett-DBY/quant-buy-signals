from __future__ import annotations

import pytest

from tools.build_official_industry_source import parse_capco_tables


def _row(code: str, name: str = "测试公司", division: str = "65") -> list[str]:
    return [
        code,
        name,
        "I",
        "信息传输、软件和信息技术服务业",
        "",
        "",
        division,
        "软件和信息技术服务业",
    ]


def test_capco_parser_builds_a_complete_deterministic_code_map():
    rows = [_row(f"{code:06d}") for code in range(5_000)]

    records = parse_capco_tables([[rows[2], ["上市公司代码"], *reversed(rows)]])

    assert len(records) == 5_000
    assert list(records)[:3] == ["000000", "000001", "000002"]
    assert records["000123"]["division_code"] == "65"


def test_capco_parser_rejects_conflicting_duplicate_and_invalid_division():
    rows = [_row(f"{code:06d}") for code in range(5_000)]
    conflict = _row("000001", name="另一家公司")
    with pytest.raises(ValueError, match="conflicting duplicate"):
        parse_capco_tables([[*rows, conflict]])

    rows[10] = _row("000010", division="X")
    with pytest.raises(ValueError, match="invalid division"):
        parse_capco_tables([rows])
