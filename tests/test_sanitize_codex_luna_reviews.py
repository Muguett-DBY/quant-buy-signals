from __future__ import annotations

import json

from tools.sanitize_codex_luna_reviews import (
    _claim_is_unbound,
    sanitize,
)


def _claim(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "search_finding_id": "search-001",
        "source_ref": "https://example.test/report",
        "source_refs": ["https://example.test/report"],
        "statement": "股票代码：603508；2026年中报披露经营现金流。",
    }
    value.update(overrides)
    return value


def test_only_code_bound_sina_or_explicit_wrong_company_sources_are_rejected() -> None:
    assert _claim_is_unbound(
        _claim(
            source_ref=("https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300966"),
            source_refs=["https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300966"],
            statement="共同药业(300966)半年报",
        ),
        "603508",
    )
    assert _claim_is_unbound(
        _claim(
            source_ref="https://static.cninfo.com.cn/report.pdf",
            statement="共同药业(300966)半年报",
            source_context="股票代码：603508",
        ),
        "603508",
    )
    # CFI uses an internal numeric stockid; the visible candidate code binds
    # the page and must not be rejected as if it were a Sina security code.
    assert not _claim_is_unbound(
        _claim(
            source_ref="https://gg.cfi.cn/quote.aspx?stockid=28565",
            source_refs=["https://gg.cfi.cn/quote.aspx?stockid=28565"],
            statement="思维列控(603508)股票全景",
        ),
        "603508",
    )


def test_sanitize_removes_bad_claims_and_http_extras_without_touching_scores() -> None:
    payload = {
        "candidate_total": 1,
        "packets": [
            {
                "security_code": "603508",
                "name": "思维列控",
                "ai_review": {
                    "ai_action": "watchlist",
                    "buy_attractiveness_score": 69.0,
                    "claims": [
                        _claim(
                            source_ref="https://gg.cfi.cn/quote.aspx?stockid=28565",
                            source_refs=[
                                "https://gg.cfi.cn/quote.aspx?stockid=28565",
                                "http://example.test/old",
                            ],
                            statement="思维列控(603508)股票全景",
                        ),
                        _claim(
                            source_ref=(
                                "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300966"
                            ),
                            source_refs=[
                                "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300966"
                            ],
                            statement="共同药业(300966)半年报",
                        ),
                    ],
                    "summary": "量化快照。公司公开资料：共同药业(300966)半年报",
                    "key_strengths": ["公司公开资料：共同药业(300966)半年报"],
                    "risk_flags": ["估值风险"],
                    "quantitative_facts": ["2025年经营现金流为正"],
                },
            }
        ],
    }

    sanitized, changed = sanitize(payload)
    review = sanitized["packets"][0]["ai_review"]
    assert changed == 1
    assert len(review["claims"]) == 1
    assert review["claims"][0]["source_refs"] == ["https://gg.cfi.cn/quote.aspx?stockid=28565"]
    assert "未将该页面作为本公司事实" in review["summary"]
    assert "未将该页面作为本公司事实" in review["key_strengths"][0]
    assert review["risk_flags"] == ["估值风险"]
    assert review["quantitative_facts"] == ["2025年经营现金流为正"]
    assert review["buy_attractiveness_score"] == 69.0
    assert sanitized["publication_sanitization"] == {
        "contract_version": 2,
        "removed_unbound_claim_count": 1,
        "removed_http_source_ref_count": 1,
        "removed_future_claim_count": 0,
        "cleaned_search_metadata_count": 0,
    }


def test_sanitize_removes_raw_search_metadata_and_future_result_rows() -> None:
    payload = {
        "market_as_of": "2026-08-24",
        "packets": [
            {
                "security_code": "002395",
                "name": "双象股份",
                "ai_review": {
                    "summary": (
                        "量化快照；公开资料摘要：双象股份公告（ turn42search0 [wordlim: 200] "
                        "Published: today; Crawled: today; 2026-08-25 半年度报告）。；主要风险：等待核验。"
                    ),
                    "key_strengths": ["公开资料：双象股份（ turn42search1 [wordlim: 200] Crawled: today; 2025 年报"],
                    "risk_flags": ["估值风险"],
                    "quantitative_facts": ["2025 年经营现金流为正"],
                    "claims": [
                        _claim(
                            statement=(
                                "双象股份公告（ turn42search0 [wordlim: 200] Crawled: today; 2026-08-25 半年度报告"
                            )
                        ),
                        _claim(statement="2025年经营现金流为正。"),
                    ],
                },
            }
        ],
    }

    sanitized, changed = sanitize(payload)
    review = sanitized["packets"][0]["ai_review"]
    assert changed == 1
    assert len(review["claims"]) == 1
    assert "turn42search" not in json.dumps(review, ensure_ascii=False)
    assert "wordlim" not in json.dumps(review, ensure_ascii=False)
    assert "2026-08-25" not in review["summary"]
    assert sanitized["publication_sanitization"]["removed_future_claim_count"] == 1
    assert sanitized["publication_sanitization"]["cleaned_search_metadata_count"] >= 2
