from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.error
from datetime import date
from email.message import Message

import pytest

from tools import audit_ai_screening_sources as source_audit
from tools.ai_source_urls import canonical_urls
from tools.publish_ai_screening import _validated_source_audit, build_artifact


def test_source_audit_rejects_non_public_urls() -> None:
    assert source_audit._public_http_url("file:///tmp/report.pdf") == ""
    assert source_audit._public_http_url("http://127.0.0.1/report") == ""
    assert source_audit._public_http_url("https://localhost/report") == ""
    assert source_audit._public_http_url("https://static.cninfo.com.cn/report.pdf")


def test_source_audit_rejects_hostname_with_any_non_public_dns_address(monkeypatch) -> None:
    monkeypatch.setattr(
        source_audit.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 443, 0, 0)),
        ],
    )

    with pytest.raises(source_audit.UnsafeUrlError, match="non-public"):
        source_audit._resolve_public_addresses("https://reports.example/result")


class _Response:
    status = 200
    headers = {"content-type": "text/html"}

    def __init__(self, url: str) -> None:
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return b"ok"[:limit]


def _redirect_error(url: str, location: str) -> urllib.error.HTTPError:
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError(url, 302, "Found", headers, None)


def _http_error(url: str, status: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, reason, Message(), None)


def _single_claim_payload(url: str) -> dict[str, object]:
    return {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600000",
                "type_key": "type1",
                "ai_review": {"claims": [{"source_ref": url}]},
            }
        ],
    }


def _semantic_claim_payload(
    url: str,
    *,
    published_at: str = "2026-08-13",
    report_period: str = "2026H1",
) -> dict[str, object]:
    return {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600585",
                "type_key": "type1",
                "ai_review": {
                    "claims": [
                        {
                            "source_ref": url,
                            "search_finding_id": "search-002",
                        }
                    ],
                    "search_findings": [
                        {
                            "id": "search-002",
                            "url": url,
                            "published_at": published_at,
                            "report_period": report_period,
                        }
                    ],
                },
            }
        ],
    }


def _pdf_with_text(text: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    try:
        document.new_page().insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()


def _projection_payload(url: str) -> dict[str, object]:
    return {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "review_mode": "opencode_native_company_research_review",
        "packets": [
            {
                "security_code": "600585",
                "name": "海螺水泥",
                "type_key": "type1",
                "ai_review": {
                    "claims": [
                        {
                            "statement": "水泥主业现金流改善",
                            "source_ref": url,
                            "source_context": f"{url} 半年度报告",
                            "support": "supports",
                            "search_finding_id": "search-002",
                            "source_kind": "official_filing",
                        }
                    ],
                    "search_findings": [
                        {
                            "id": "search-002",
                            "query": "海螺水泥 2026 半年度报告",
                            "title": "海螺水泥2026年半年度报告",
                            "url": url,
                            "published_at": "2026-08-13",
                            "report_period": "2026H1",
                            "finding": "水泥主业经营现金流改善",
                            "stance": "support",
                            "source_kind": "official_filing",
                            "source_quality": "primary",
                        }
                    ],
                },
            }
        ],
    }


def test_source_projection_binds_final_public_claim_and_finding_semantics() -> None:
    url = "https://reports.example/2026-half-year"
    digest, counts = source_audit.source_semantic_projection_sha256(_projection_payload(url))
    projection = source_audit.public_source_semantic_projection(_projection_payload(url))

    assert len(digest) == 64
    assert counts == {
        "projection_company_count": 1,
        "projection_claim_count": 1,
        "projection_search_finding_count": 1,
        "projection_source_reference_count": 2,
        "projection_unique_url_count": 1,
    }
    company = projection["companies"][0]
    assert (company["security_code"], company["name"], company["type_key"]) == (
        "600585",
        "海螺水泥",
        "type1",
    )
    assert company["claims"][0] == {
        "claim_index": 0,
        "statement": "水泥主业现金流改善",
        "source_ref": url,
        "source_context": f"{url} 半年度报告",
        "source_refs": [url],
        "support": "supports",
        "fact_id": "",
        "search_finding_id": "search-002",
        "source_kind": "official_filing",
        "linked_published_at": "2026-08-13",
        "linked_report_period": "2026H1",
        "linked_source_kind": "official_filing",
    }
    assert company["search_findings"][0]["finding"] == "水泥主业经营现金流改善"
    assert company["search_findings"][0]["url"] == url
    assert company["search_findings"][0]["published_at"] == "2026-08-13"
    assert company["search_findings"][0]["report_period"] == "2026H1"
    assert company["search_findings"][0]["source_kind"] == "official_filing"


def test_source_projection_uses_public_claim_numeric_normalisation() -> None:
    assert source_audit.public_claim_statement("同比 +0.08个百分点%") == "同比 +0.08个百分点"
    assert source_audit.public_claim_statement("同比 期末口径%") == "同比 期末口径"
    assert source_audit.public_claim_statement("PE -12.5倍") == "PE 不适用（原始 PE -12.5 倍）"


def test_source_projection_is_canonical_across_company_order() -> None:
    payload = _projection_payload("https://reports.example/600585-half-year")
    second = json.loads(json.dumps(payload["packets"][0], ensure_ascii=False))
    second["security_code"] = "000001"
    second["name"] = "平安银行"
    second["ai_review"]["claims"][0]["source_ref"] = "https://reports.example/000001-half-year"
    second["ai_review"]["claims"][0]["source_context"] = "https://reports.example/000001-half-year"
    second["ai_review"]["search_findings"][0]["url"] = "https://reports.example/000001-half-year"
    payload["packets"].append(second)
    reversed_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    reversed_payload["packets"].reverse()

    digest, counts = source_audit.source_semantic_projection_sha256(payload)
    reversed_digest, reversed_counts = source_audit.source_semantic_projection_sha256(reversed_payload)

    assert digest == reversed_digest
    assert counts == reversed_counts


@pytest.mark.parametrize(
    ("collection", "field", "replacement"),
    [
        ("claims", "statement", "水泥主业现金流恶化"),
        ("search_findings", "finding", "水泥主业经营现金流恶化"),
    ],
)
def test_source_audit_projection_detects_text_tampering_with_unchanged_url(
    tmp_path, monkeypatch, collection, field, replacement
) -> None:
    url = "https://reports.example/2026-half-year"
    original = _projection_payload(url)
    tampered = json.loads(json.dumps(original, ensure_ascii=False))
    tampered["packets"][0]["ai_review"][collection][0][field] = replacement
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": (
                '<p>600585 海螺水泥 2026H1</p><meta property="article:published_time" content="2026-08-13">'
            ).encode(),
        },
    )
    original_path = tmp_path / "original.json"
    tampered_path = tmp_path / "tampered.json"
    original_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    tampered_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")

    original_report = source_audit.audit(original_path, tmp_path / "original-audit.json", workers=1)
    tampered_report = source_audit.audit(tampered_path, tmp_path / "tampered-audit.json", workers=1)

    assert original_report["audit_contract_version"] == 3
    assert original_report["audit_passed"] is True
    assert tampered_report["audit_passed"] is True
    assert original_report["canonical_urls"] == tampered_report["canonical_urls"] == [url]
    assert original_report["projection_claim_count"] == tampered_report["projection_claim_count"] == 1
    assert original_report["projection_search_finding_count"] == tampered_report["projection_search_finding_count"] == 1
    assert original_report["projection_sha256"] != tampered_report["projection_sha256"]


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<script type="application/ld+json">{"datePublished":"2026-08-13"}</script>', date(2026, 8, 13)),
        ('<meta property="article:published_time" content="2026-08-14T08:00:00+08:00">', date(2026, 8, 14)),
        ('<time datetime="2026-08-15T08:00:00+08:00"></time>', date(2026, 8, 15)),
        ("<script>var newsDT='202408280223';</script>", date(2024, 8, 28)),
    ],
)
def test_article_published_date_uses_structured_html_metadata(html, expected) -> None:
    assert source_audit._article_published_date(html.encode(), "text/html; charset=utf-8") == expected


def test_source_audit_rejects_aastocks_old_article_relabelled_as_2026(tmp_path, monkeypatch) -> None:
    url = "https://www.aastocks.com/sc/stocks/news/aafn-con/now.1375908/latest-news"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_semantic_claim_payload(url)), encoding="utf-8")
    html = b"""
        <html><body>
        <p>600585 2026H1</p>
        <time datetime="2026-08-24T09:00:00+08:00">today</time>
        <script>var newsDT='202408280223';</script>
        </body></html>
    """
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": html,
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["audit_contract_version"] == 3
    assert report["audit_passed"] is False
    assert report["semantic_claim_count"] == 1
    assert report["semantic_passed_count"] == 0
    assert report["semantic_failed_count"] == 1
    assert report["published_at_mismatch_count"] == 1
    assert report["report_period_after_publication_count"] == 1
    assert any("article date 2024-08-28" in issue["reason"] for issue in report["semantic_issues"])
    assert "_body" not in report["results"][0]
    assert "2026-08-24" not in json.dumps(report)


def test_source_audit_accepts_matching_html_article_period(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/2026-half-year"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_semantic_claim_payload(url, published_at="2026-08-13")), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": b'<p>600585 2026H1</p><meta property="article:published_time" content="2026-08-13">',
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_passed_count"] == 1
    assert report["semantic_failed_count"] == 0
    assert report["semantic_html_date_checked_count"] == 1
    assert report["audit_passed"] is True


def test_source_audit_matches_financial_facts_across_statement_units() -> None:
    claim = {"statement": "2025年度营业收入245.009亿元"}
    numbers = source_audit._claim_numbers(claim, {})

    assert source_audit._structured_number_match(
        "SECURITY_CODE 000401 REPORT_DATE 2025-12-31 TOTAL_OPERATE_INCOME 24500900000元",
        numbers,
        claim=claim,
        finding={},
    )


def test_source_audit_rejects_claim_finding_url_mismatch(tmp_path, monkeypatch) -> None:
    claim_url = "https://reports.example/company"
    payload = _semantic_claim_payload(claim_url, published_at="2026-08-13", report_period="2026H1")
    payload["packets"][0]["ai_review"]["search_findings"][0]["url"] = "https://reports.example/other"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html",
            "official_market_domain": False,
            "_body": b'<p>600585 2026H1</p><meta property="article:published_time" content="2026-08-13">',
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    # The mismatched claim URL and the finding URL are two independently
    # published source identities; the claim/finding binding still fails.
    assert report["semantic_claim_count"] == 2
    assert report["semantic_passed_count"] == 1
    assert report["semantic_failed_count"] == 1
    assert report["audit_passed"] is False
    assert "different URL" in report["semantic_issues"][0]["reason"]


def test_source_audit_uses_one_complete_canonical_url_set_for_all_source_fields(tmp_path, monkeypatch) -> None:
    long_url = "https://reports.example/" + ("segment-" * 180) + ".html?period=2026H1"
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600585",
                "name": "海螺水泥",
                "type_key": "type1",
                "ai_review": {
                    "claims": [
                        {
                            "statement": "2026H1经营现金流18亿元",
                            "source_ref": f"{long_url}（半年度报告）",
                            "source_context": f"正文来源：{long_url}",
                            "source_refs": [long_url],
                            "search_finding_id": "finding-1",
                        }
                    ],
                    "search_findings": [
                        {
                            "id": "finding-1",
                            "url": long_url,
                            "report_period": "2026H1",
                            "finding": "2026H1经营现金流18亿元",
                        }
                    ],
                },
            }
        ],
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": "<p>600585 海螺水泥 2026H1 经营现金流18亿元</p>".encode(),
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["canonical_urls"] == [long_url]
    assert report["results"][0]["url"] == long_url
    assert report["source_bindings"][0]["search_finding_id"] == "finding-1"
    assert report["source_bindings"][0]["claim_index"] == 0
    assert report["semantic_passed_count"] == 1
    assert report["company_coverage"][0]["status"] == "pass"


def test_canonical_urls_stop_unencoded_ascii_space_but_keep_encoded_space() -> None:
    assert canonical_urls("https://reports.example/report%20final.pdf explanatory text") == [
        "https://reports.example/report%20final.pdf"
    ]
    assert canonical_urls("https://reports.example/report.pdf explanatory text") == [
        "https://reports.example/report.pdf"
    ]


def test_source_audit_accepts_json_fact_provenance_with_identity_and_fact_gate(tmp_path, monkeypatch) -> None:
    url = "https://api.example/facts?columns=REPORT_DATE,TOTAL_OPERATE_INCOME"
    payload = _single_claim_payload(url)
    payload["packets"][0]["security_code"] = "600585"
    payload["packets"][0]["name"] = "海螺水泥"
    payload["packets"][0]["ai_review"] = {
        "claims": [
            {
                "statement": "600585 2026-06-30 营业收入 120",
                "source_ref": url,
                "fact_id": "latest_income",
            }
        ]
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/json; charset=utf-8",
            "official_market_domain": False,
            "_body": b'{"SECURITY_CODE":"600585","REPORT_DATE":"2026-06-30","TOTAL_OPERATE_INCOME":12000000000}',
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_passed_count"] == 1
    assert report["semantic_failed_count"] == 0
    assert report["semantic_unverified_count"] == 0
    assert report["audit_passed"] is True


def test_structured_source_gate_rejects_same_year_wrong_report_period() -> None:
    issues = source_audit._structured_source_issues(
        b'{"SECURITY_CODE":"600585","REPORT_DATE":"2026-03-31"}',
        "application/json",
        url="https://api.example/facts",
        security_code="600585",
        name="海螺水泥",
        claim={"statement": "600585 2026-06-30", "report_period": "2026-06-30"},
        finding={},
    )

    assert any("report period" in issue for issue in issues)


def test_source_audit_rejects_cited_finding_without_url_in_strict_publish(tmp_path, monkeypatch) -> None:
    payload = _semantic_claim_payload("https://reports.example/unused")
    review = payload["packets"][0]["ai_review"]
    review["claims"][0]["source_ref"] = ""
    review["claims"][0]["source_context"] = ""
    review["search_findings"][0]["url"] = ""
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload), encoding="utf-8")

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)
    coverage = report["company_coverage"][0]
    assert coverage["searched_no_source_finding_ids"] == ["search-002"]
    assert coverage["referenced_no_source_finding_ids"] == ["search-002"]

    with pytest.raises(ValueError, match="cited finding has no URL"):
        _validated_source_audit(
            tmp_path / "audit.json",
            merged_sha256=hashlib.sha256(merged.read_bytes()).hexdigest(),
            generation="g1",
            market_as_of="2026-08-21",
            expected_urls=set(),
            expected_companies={
                "600585": {
                    "finding_urls": {},
                    "searched_no_source": {"search-002"},
                    "referenced_no_source": {"search-002"},
                }
            },
            strict=True,
        )


def test_source_audit_marks_non_html_finding_unverified(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/report.pdf"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_semantic_claim_payload(url)), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/pdf",
            "official_market_domain": False,
            "_body": b"%PDF-1.7",
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_claim_count"] == 1
    assert report["semantic_passed_count"] == 0
    assert report["semantic_unverified_count"] == 1
    assert report["semantic_failed_count"] == 0
    assert report["audit_passed"] is False
    assert report["results"][0]["semantic_status"] == "unverified"
    with pytest.raises(ValueError, match="unverified non-HTML"):
        _validated_source_audit(
            tmp_path / "audit.json",
            merged_sha256=hashlib.sha256(merged.read_bytes()).hexdigest(),
            generation="g1",
            market_as_of="2026-08-21",
            expected_urls={url},
            expected_companies={
                "600585": {
                    "finding_urls": {"search-002": {url}},
                    "searched_no_source": set(),
                }
            },
            strict=True,
        )


def test_source_audit_marks_non_html_fact_claim_unverified(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/fact.pdf"
    payload = _single_claim_payload(url)
    payload["packets"][0]["name"] = "浦发银行"
    payload["packets"][0]["ai_review"] = {
        "claims": [{"statement": "2026H1经营现金流18亿元", "source_ref": url, "fact_id": "fact-1"}]
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/pdf",
            "official_market_domain": False,
            "_body": b"%PDF-1.7",
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_claim_count"] == 1
    assert report["semantic_unverified_count"] == 1
    assert report["semantic_failed_count"] == 0
    assert report["audit_passed"] is False
    assert report["results"][0]["semantic_status"] == "unverified"


def test_source_audit_accepts_real_pdf_with_matching_identity_period_and_fact(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/real-report.pdf"
    payload = _semantic_claim_payload(url)
    review = payload["packets"][0]["ai_review"]
    review["claims"][0]["statement"] = "2026H1 cash flow 18.5 billion"
    review["search_findings"][0]["finding"] = "2026H1 cash flow 18.5 billion"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    body = _pdf_with_text("600585 Hailuo Cement 2026H1 cash flow 18.5 billion")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/pdf",
            "official_market_domain": False,
            "_body": body,
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_claim_count"] == 1
    assert report["semantic_passed_count"] == 1
    assert report["semantic_failed_count"] == 0
    assert report["semantic_unverified_count"] == 0
    assert report["results"][0]["semantic_status"] == "pass"
    assert report["audit_passed"] is True


def test_source_audit_rejects_parseable_pdf_with_wrong_company_identity(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/wrong-report.pdf"
    payload = _semantic_claim_payload(url)
    review = payload["packets"][0]["ai_review"]
    review["claims"][0]["statement"] = "2026H1 cash flow 18.5 billion"
    review["search_findings"][0]["finding"] = "2026H1 cash flow 18.5 billion"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    body = _pdf_with_text("000001 Other Bank 2026H1 cash flow 18.5 billion")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/pdf",
            "official_market_domain": False,
            "_body": body,
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_passed_count"] == 0
    assert report["semantic_failed_count"] == 1
    assert report["semantic_unverified_count"] == 0
    assert report["results"][0]["semantic_status"] == "failed"
    assert any("company code" in issue["reason"] for issue in report["semantic_issues"])


def test_pdf_identity_accepts_short_name_used_in_legal_issuer_name() -> None:
    text = "成都超纯应用材料股份有限公司 证券简称：超纯应材 2025 年度报告"
    assert source_audit._pdf_company_identity_matches(text, "301717", "超纯应材")


def test_industry_pdf_claim_does_not_require_issuer_identity() -> None:
    text = "中国汽车流通行业协会 2026-04-10 2026年1-3月新能源乘用车渗透率41.6%"
    claim = {
        "source_context": "中国汽车流通行业协会信息中心",
        "source_kind": "codex_luna_web_search",
        "statement": "2026-04-10协会报告显示新能源乘用车渗透率41.6%",
    }
    finding = {"finding": "2026年1-3月新能源乘用车渗透率41.6%"}
    assert (
        source_audit._pdf_text_semantic_issues(
            text,
            security_code="605228",
            name="神通科技",
            claim=claim,
            finding=finding,
            require_identity=False,
        )
        == []
    )


def test_source_audit_marks_unclaimed_finding_non_html_unverified(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/unclaimed.pdf"
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600000",
                "name": "浦发银行",
                "type_key": "type1",
                "ai_review": {
                    "claims": [],
                    "search_findings": [
                        {
                            "id": "finding-only",
                            "url": url,
                            "finding": "2026H1经营现金流18亿元",
                            "report_period": "2026H1",
                        }
                    ],
                },
            }
        ],
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "application/pdf",
            "official_market_domain": False,
            "_body": b"%PDF-1.7",
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["semantic_claim_count"] == 1
    assert report["semantic_unverified_count"] == 1
    assert report["company_coverage"][0]["status"] == "unverified"
    assert report["audit_passed"] is False


def test_source_audit_does_not_call_finding_with_url_searched_no_source(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/finding-only"
    payload = _semantic_claim_payload(url)
    payload["packets"][0]["name"] = "浦发银行"
    payload["packets"][0]["ai_review"]["claims"][0]["source_ref"] = ""
    payload["packets"][0]["ai_review"]["claims"][0]["source_context"] = ""
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": b'<p>600585 2026H1 18</p><meta property="article:published_time" content="2026-08-13">',
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    coverage = report["company_coverage"][0]
    assert coverage["referenced_finding_ids"] == ["search-002"]
    assert coverage["searched_no_source_finding_ids"] == []
    assert coverage["status"] == "pass"
    assert report["semantic_passed_count"] == 1


def test_source_audit_unreferenced_no_source_does_not_fail_referenced_findings(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/finding-only"
    payload = _semantic_claim_payload(url)
    payload["packets"][0]["name"] = "浦发银行"
    payload["packets"][0]["ai_review"]["search_findings"].append(
        {
            "id": "search-no-source",
            "url": None,
            "finding": "已完成搜索但未找到可引用来源",
            "report_period": "2026H1",
        }
    )
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "official_market_domain": False,
            "_body": b'<p>600585 2026H1 18</p><meta property="article:published_time" content="2026-08-13">',
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    coverage = report["company_coverage"][0]
    assert coverage["referenced_finding_ids"] == ["search-002"]
    assert coverage["searched_no_source_finding_ids"] == ["search-no-source"]
    assert coverage["referenced_no_source_finding_ids"] == []
    assert coverage["all_referenced_findings_semantic_pass"] is True
    assert coverage["status"] == "searched_no_source"


def test_source_audit_reports_company_coverage_without_global_count_substitution(tmp_path, monkeypatch) -> None:
    good_url = "https://reports.example/good"
    bad_url = "https://reports.example/bad"
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600000",
                "name": "浦发银行",
                "type_key": "type1",
                "ai_review": {
                    "claims": [
                        {"statement": "2026H1现金流18亿元", "source_ref": good_url, "search_finding_id": "good"}
                    ],
                    "search_findings": [
                        {"id": "good", "url": good_url, "report_period": "2026H1", "finding": "现金流18亿元"}
                    ],
                },
            },
            {
                "security_code": "000001",
                "name": "平安银行",
                "type_key": "type1",
                "ai_review": {
                    "claims": [{"statement": "2026H1现金流19亿元", "source_ref": bad_url, "search_finding_id": "bad"}],
                    "search_findings": [
                        {"id": "bad", "url": bad_url, "report_period": "2026H1", "finding": "现金流19亿元"}
                    ],
                },
            },
        ],
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def check(url: str, **_: object) -> dict[str, object]:
        return {
            "url": url,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "content_type": "text/html",
            "official_market_domain": False,
            "_body": (
                b"<p>600000 2026H1 18\xe4\xbf\x84\xe5\x85\x83</p>"
                if url == good_url
                else b"<title>404 Not Found</title><p>000001</p>"
            ),
        }

    monkeypatch.setattr(source_audit, "_check_url", check)
    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)
    coverage = {item["security_code"]: item for item in report["company_coverage"]}

    assert report["semantic_passed_count"] == 1
    assert report["semantic_failed_count"] == 1
    assert coverage["600000"]["status"] == "pass"
    assert coverage["000001"]["status"] == "failed"
    assert coverage["000001"]["all_referenced_findings_semantic_pass"] is False

    # Even if a tampered report makes the global counters look clean, strict
    # publication must evaluate each company's actual finding URLs.
    audit_path = tmp_path / "tampered-audit.json"
    tampered = dict(report)
    tampered.update(
        {
            "audit_passed": True,
            "semantic_passed_count": 2,
            "semantic_failed_count": 0,
            "semantic_unverified_count": 0,
        }
    )
    audit_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="company findings did not all semantically pass"):
        _validated_source_audit(
            audit_path,
            merged_sha256=hashlib.sha256(merged.read_bytes()).hexdigest(),
            generation="g1",
            market_as_of="2026-08-21",
            expected_urls={good_url, bad_url},
            expected_companies={
                "600000": {"finding_urls": {"good": {good_url}}, "searched_no_source": set()},
                "000001": {"finding_urls": {"bad": {bad_url}}, "searched_no_source": set()},
            },
            strict=True,
        )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<title>Sign in</title><form action='/login'>login</form>", "login"),
        (b"<form><input name='captcha'></form><p>captcha</p>", "CAPTCHA"),
        (b"<div id='not-found'>Page not found</div>", "soft-404"),
    ],
)
def test_html_semantic_gate_rejects_placeholder_pages(body: bytes, expected: str) -> None:
    issues = source_audit._html_semantic_issues(
        body,
        "text/html; charset=utf-8",
        security_code="600000",
        name="浦发银行",
        report_period="2026H1",
        claim={"statement": "2026H1现金流18亿元"},
        finding={"finding": "现金流18亿元"},
    )

    assert issues
    assert expected.casefold() in issues[0].casefold()


def test_source_audit_does_not_semantically_pass_blocked_unique_source(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/anti-bot"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_semantic_claim_payload(url)), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "blocked",
            "reachability": "blocked",
            "body_retrieved": False,
            "status": 429,
            "official_market_domain": False,
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["blocked"] == 1
    assert report["blocked_semantic_claim_count"] == 1
    assert report["semantic_failed_count"] == 1
    assert report["audit_passed"] is False


def test_source_audit_validates_redirect_dns_before_second_request(monkeypatch) -> None:
    calls: list[str] = []

    class _Opener:
        def open(self, request, *, timeout):
            calls.append(request.full_url)
            raise _redirect_error(request.full_url, "http://internal.example/private")

    def addresses(host, *_args, **_kwargs):
        address = "10.0.0.8" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    handlers: list[object] = []
    monkeypatch.setattr(source_audit.socket, "getaddrinfo", addresses)
    monkeypatch.setattr(
        source_audit.urllib.request,
        "build_opener",
        lambda *values: handlers.extend(values) or _Opener(),
    )

    result = source_audit._check_url("https://reports.example/start", timeout=1, max_bytes=32)

    assert result["result"] == "invalid"
    assert result["final_url"] == "http://internal.example/private"
    assert calls == ["https://reports.example/start"]
    assert len(handlers) == 2
    assert isinstance(handlers[0], source_audit._NoRedirectHandler)
    assert isinstance(handlers[1], source_audit.urllib.request.ProxyHandler)


def test_source_audit_manually_follows_public_redirect(monkeypatch) -> None:
    calls: list[str] = []

    class _Opener:
        def open(self, request, *, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise _redirect_error(request.full_url, "/final")
            return _Response(request.full_url)

    monkeypatch.setattr(
        source_audit.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(source_audit.urllib.request, "build_opener", lambda *_handlers: _Opener())

    result = source_audit._check_url("https://reports.example/start", timeout=1, max_bytes=32)

    assert result["result"] == "ok"
    assert result["reachability"] == "reachable"
    assert result["body_retrieved"] is True
    assert result["final_url"] == "https://reports.example/final"
    assert result["redirect_count"] == 1
    assert calls == ["https://reports.example/start", "https://reports.example/final"]


def test_source_audit_fails_closed_on_http_404(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/missing"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_single_claim_payload(url)), encoding="utf-8")
    monkeypatch.setattr(
        source_audit.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )

    class _Opener:
        def open(self, request, *, timeout):
            raise _http_error(request.full_url, 404, "Not Found")

    monkeypatch.setattr(source_audit.urllib.request, "build_opener", lambda *_handlers: _Opener())

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["reachable"] == 0
    assert report["failed"] == 1
    assert report["blocked"] == 0
    assert report["audit_passed"] is False
    assert report["results"][0]["status"] == 404
    assert report["results"][0]["body_retrieved"] is False


def test_source_audit_fails_closed_on_dns_failure(tmp_path, monkeypatch) -> None:
    url = "https://missing-host.example/report"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_single_claim_payload(url)), encoding="utf-8")

    def fail_dns(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(source_audit.socket, "getaddrinfo", fail_dns)

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["reachable"] == 0
    assert report["failed"] == 1
    assert report["audit_passed"] is False
    assert report["results"][0]["status"] == 0
    assert report["results"][0]["body_retrieved"] is False


def test_source_audit_records_http_403_as_blocked_without_body_verification(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/anti-bot"
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(_single_claim_payload(url)), encoding="utf-8")
    monkeypatch.setattr(
        source_audit.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )

    class _Opener:
        def open(self, request, *, timeout):
            raise _http_error(request.full_url, 403, "Forbidden")

    monkeypatch.setattr(source_audit.urllib.request, "build_opener", lambda *_handlers: _Opener())

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["reachable"] == 0
    assert report["blocked"] == 1
    assert report["failed"] == 0
    assert report["body_retrieved_count"] == 0
    assert report["audit_passed"] is False
    assert report["semantic_failed_count"] == 1
    assert report["results"][0]["reachability"] == "blocked"
    assert report["results"][0]["body_retrieved"] is False


def test_source_audit_cli_exits_nonzero_when_any_source_failed(tmp_path, monkeypatch) -> None:
    merged = tmp_path / "merged.json"
    output = tmp_path / "audit.json"
    merged.write_text(json.dumps(_single_claim_payload("https://reports.example/missing")), encoding="utf-8")
    monkeypatch.setattr(
        source_audit,
        "audit",
        lambda *_args, **_kwargs: {
            "checked": 1,
            "reachable": 0,
            "blocked": 0,
            "failed": 1,
            "invalid": 0,
            "audit_passed": False,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_ai_screening_sources", "--merged", str(merged), "--output", str(output)],
    )

    assert source_audit.main() == 1


def test_source_audit_deduplicates_urls_and_keeps_pair_references(tmp_path, monkeypatch) -> None:
    url = "https://static.cninfo.com.cn/report.pdf"
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "600000",
                "type_key": "type1",
                "ai_review": {"claims": [{"source_ref": url}]},
            },
            {
                "security_code": "600000",
                "type_key": "type7",
                "ai_review": {
                    "claims": [
                        {"source_ref": url},
                        {"source_ref": "http://127.0.0.1/private"},
                    ]
                },
            },
        ],
    }
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "ok",
            "reachability": "reachable",
            "body_retrieved": True,
            "status": 200,
            "official_market_domain": True,
        },
    )
    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)
    assert report["merged_sha256"] == hashlib.sha256(merged.read_bytes()).hexdigest()
    assert report["snapshot_generation"] == "g1"
    assert report["market_as_of"] == "2026-08-21"
    assert report["checked"] == 2
    assert report["ok"] == 1
    assert report["invalid"] == 1
    assert report["reachable"] == 1
    assert report["body_retrieved_count"] == 1
    assert report["audit_passed"] is False
    assert report["claim_count"] == 3
    assert report["invalid_claim_url_count"] == 1
    valid_result = next(result for result in report["results"] if result["url"] == url)
    assert valid_result["references"] == [
        {"security_code": "600000", "type_key": "type1"},
        {"security_code": "600000", "type_key": "type7"},
    ]


def test_source_audit_counts_unsafe_dns_or_redirect_result_as_invalid_claim(tmp_path, monkeypatch) -> None:
    url = "https://reports.example/private-redirect"
    merged = tmp_path / "merged.json"
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "packets": [
                    {
                        "security_code": "600000",
                        "type_key": "type1",
                        "ai_review": {"claims": [{"source_ref": url}]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        source_audit,
        "_check_url",
        lambda value, **_: {
            "url": value,
            "result": "invalid",
            "reachability": "invalid",
            "body_retrieved": False,
            "status": 0,
            "error": "DNS resolved to non-public address(es): 10.0.0.8",
            "official_market_domain": False,
        },
    )

    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)

    assert report["invalid"] == 1
    assert report["invalid_claim_url_count"] == 1
    assert report["invalid_claim_urls"] == [
        {
            "security_code": "600000",
            "type_key": "type1",
            "source": url,
            "reason": "DNS resolved to non-public address(es): 10.0.0.8",
        }
    ]


def test_publish_accepts_only_audit_bound_to_exact_merged_file(tmp_path) -> None:
    merged = tmp_path / "merged.json"
    audit_path = tmp_path / "audit.json"
    output = tmp_path / "public.json"
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "candidate_count": 0,
                "candidate_total": 0,
                "packets": [],
            }
        ),
        encoding="utf-8",
    )
    source_audit.audit(merged, audit_path, workers=1)

    artifact = build_artifact(
        merged,
        output,
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
        source_audit_path=audit_path,
    )

    assert artifact["source_audit"]["available"] is True
    assert artifact["source_audit"]["audit_passed"] is True
    assert artifact["source_audit"]["audit_sha256"] == hashlib.sha256(audit_path.read_bytes()).hexdigest()
    assert artifact["source_audit"]["merged_sha256"] == hashlib.sha256(merged.read_bytes()).hexdigest()
    assert artifact["source_audit"]["invalid_claim_url_count"] == 0


def test_publish_rejects_merged_file_changed_after_source_audit(tmp_path) -> None:
    merged = tmp_path / "merged.json"
    audit_path = tmp_path / "audit.json"
    output = tmp_path / "public.json"
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "candidate_count": 0,
        "candidate_total": 0,
        "packets": [],
    }
    merged.write_text(json.dumps(payload), encoding="utf-8")
    source_audit.audit(merged, audit_path, workers=1)
    merged.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        build_artifact(
            merged,
            output,
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
            source_audit_path=audit_path,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("merged_sha256", "0" * 64, "does not match"),
        ("snapshot_generation", "other", "generation does not match"),
        ("market_as_of", "2026-08-20", "market_as_of does not match"),
        ("audit_contract_version", 1, "contract version is obsolete"),
        ("projection_sha256", "0" * 64, "semantic projection does not match"),
        ("projection_company_count", 1, "semantic projection counts"),
        ("projection_claim_count", 1, "semantic projection counts"),
        ("projection_search_finding_count", 1, "semantic projection counts"),
        ("projection_source_reference_count", 1, "semantic projection counts"),
        ("projection_unique_url_count", 1, "semantic projection counts"),
        ("invalid_claim_url_count", 1, "invalid or non-public"),
    ],
)
def test_publish_rejects_stale_or_unsafe_source_audit(tmp_path, field, value, error) -> None:
    merged = tmp_path / "merged.json"
    audit_path = tmp_path / "audit.json"
    output = tmp_path / "public.json"
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "candidate_count": 0,
                "candidate_total": 0,
                "packets": [],
            }
        ),
        encoding="utf-8",
    )
    report = source_audit.audit(merged, audit_path, workers=1)
    report[field] = value
    audit_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        build_artifact(
            merged,
            output,
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
            source_audit_path=audit_path,
        )
    assert not output.exists()
