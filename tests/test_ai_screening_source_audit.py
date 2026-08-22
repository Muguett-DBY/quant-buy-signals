from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
from email.message import Message

import pytest

from tools import audit_ai_screening_sources as source_audit
from tools.publish_ai_screening import build_artifact


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
    assert len(handlers) == 1
    assert isinstance(handlers[0], source_audit._NoRedirectHandler)


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
    assert result["final_url"] == "https://reports.example/final"
    assert result["redirect_count"] == 1
    assert calls == ["https://reports.example/start", "https://reports.example/final"]


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
            "status": 200,
            "official_market_domain": True,
        },
    )
    report = source_audit.audit(merged, tmp_path / "audit.json", workers=1)
    assert report["merged_sha256"] == hashlib.sha256(merged.read_bytes()).hexdigest()
    assert report["snapshot_generation"] == "g1"
    assert report["market_as_of"] == "2026-08-21"
    assert report["checked"] == report["ok"] == 1
    assert report["claim_count"] == 3
    assert report["invalid_claim_url_count"] == 1
    assert report["results"][0]["references"] == [
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
