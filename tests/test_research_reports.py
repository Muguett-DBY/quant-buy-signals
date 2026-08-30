from __future__ import annotations

from datetime import date, timedelta
import html
import json

import pytest
import requests

from data import research_reports as rr
from data.cache import SafeFileCache
from engine.quality_equity import RESEARCH_SOURCE_FIELDS


class _NoWait:
    def acquire(self):
        return None


class _FakeResponse:
    def __init__(
        self,
        payload,
        *,
        url=rr.EASTMONEY_REPORT_ENDPOINT,
        content_type="application/json; charset=utf-8",
        declared_length=None,
        chunks=None,
        status_error=None,
    ):
        self.payload = payload
        raw = (
            bytes(payload)
            if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self._chunks = list(chunks) if chunks is not None else [raw]
        self.url = url
        self.headers = {"Content-Type": content_type}
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)
        self.status_error = status_error
        self.closed = False

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responses, *, auto_detail=True, detail_overrides=None):
        self.responses = list(responses)
        self.calls = []
        self.auto_detail = auto_detail
        self.detail_overrides = dict(detail_overrides or {})
        self.rows_by_id = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.startswith(rr.EASTMONEY_REPORT_DETAIL_PREFIX):
            report_id = url.removeprefix(rr.EASTMONEY_REPORT_DETAIL_PREFIX).removesuffix(".html")
            override = self.detail_overrides.get(report_id)
            if override is not None:
                return override
            if self.auto_detail and report_id in self.rows_by_id:
                return _detail_response(self.rows_by_id[report_id])
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        payload = getattr(response, "payload", None)
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            self.rows_by_id.update(
                {
                    row["infoCode"]: row
                    for row in payload["data"]
                    if isinstance(row, dict) and isinstance(row.get("infoCode"), str)
                }
            )
        return response


def _row(
    index,
    *,
    code="600519",
    publisher_id=None,
    publisher=None,
    published="2026-07-16 00:00:00.000",
):
    report_id = f"AP20260716{index:010d}"
    return {
        "stockCode": code,
        "stockName": "贵州茅台",
        "title": f"研报 {index}",
        "orgCode": publisher_id or f"{80_000_000 + index:08d}",
        "orgSName": publisher or f"机构 {index}",
        "publishDate": published,
        "infoCode": report_id,
        "ignoredProviderField": "not exported",
    }


def _payload(rows, *, page=1, hits=None):
    count = len(rows) if hits is None else hits
    return {
        "TotalPage": (count + rr.PAGE_SIZE - 1) // rr.PAGE_SIZE,
        "currentYear": 2026,
        "data": rows,
        "hits": count,
        "pageNo": page,
        "size": len(rows),
    }


def _detail_paragraphs(row, *, revenue=35.54, unique=True):
    identity = f"本篇为{row['orgSName']}发布的《{row['title']}》。" if unique else ""
    return [
        (
            f"投资要点：{row['stockName']}（{row['stockCode']}）2026Q1实现营业收入{revenue:.2f}亿元，"
            "归母净利润表现稳健。公司经营分析显示业务规模、盈利能力和现金流质量保持稳健，"
            f"核心产品竞争优势、渠道建设与运营效率仍值得持续跟踪。{identity}"
        ),
        (
            "投资建议：维持审慎研究判断。本报告基于公开信息开展独立分析，重点核对已经披露的"
            "季度经营事实，不把预测数字冒充已实现业绩。风险提示：商品价格波动、需求不及预期、"
            "行业竞争加剧、海外经营变化以及公开数据修订风险。"
        ),
    ]


def _detail_response(row, *, paragraphs=None, zwinfo_overrides=None, **response_kwargs):
    if paragraphs is None:
        paragraphs = _detail_paragraphs(row)
    body = " ".join(" ".join(str(paragraph).split()) for paragraph in paragraphs)
    zwinfo = {
        "info_code": row["infoCode"],
        "notice_content": body,
        "notice_date": row["publishDate"],
        "notice_title": row["title"],
        "source_sample_name": row["orgSName"],
        "short_name": row["stockName"],
        "security": [{"stock": row["stockCode"], "short_name": row["stockName"]}],
    }
    zwinfo.update(zwinfo_overrides or {})
    markup = (
        "<!doctype html><html><body>"
        '<div id="ctx-content">'
        + "".join(f"<p>{html.escape(str(paragraph))}</p>" for paragraph in paragraphs)
        + "</div><script>var zwinfo = "
        + json.dumps(zwinfo, ensure_ascii=False, separators=(",", ":"))
        + ";</script></body></html>"
    ).encode("utf-8")
    return _FakeResponse(
        markup,
        url=f"{rr.EASTMONEY_REPORT_DETAIL_PREFIX}{row['infoCode']}.html",
        content_type="text/html; charset=utf-8",
        **response_kwargs,
    )


def test_fetch_research_reports_uses_eastmoney_page_contract_and_exports_only_bound_metadata():
    rows = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000002", publisher="机构二"),
        _row(3, publisher_id="80000003", publisher="机构三"),
        _row(4, publisher_id="80000001", publisher="机构一", published="2026-07-15 00:00:00.000"),
    ]
    response = _FakeResponse(_payload(rows))
    session = _FakeSession([response])

    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available
    assert result.code == "600519"
    assert result.distinct_publishers == 3
    assert len(result.sources) == 3
    assert result.content_verification["passed"]
    assert result.content_verification["verified_bodies"] == 3
    assert result.content_verification["cross_check"]["consensus_value"] == 35.54
    assert result.content_verification["cross_check"]["fact_unit"] == "CNY_100M"
    assert response.closed
    assert all(source["security_code"] == "600519" for source in result.sources)
    assert all(set(source) == RESEARCH_SOURCE_FIELDS for source in result.sources)
    assert all(source["url"].startswith(rr.EASTMONEY_REPORT_DETAIL_PREFIX) for source in result.sources)
    assert all(source["evidence_id"].startswith("eastmoney:AP") for source in result.sources)
    assert "ignoredProviderField" not in result.sources[0]

    url, call = session.calls[0]
    assert url == rr.EASTMONEY_REPORT_ENDPOINT
    assert call["params"] == {
        "industryCode": "*",
        "pageSize": 50,
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": "2025-07-17",
        "endTime": "2026-07-17",
        "pageNo": 1,
        "fields": "",
        "qType": 0,
        "orgCode": "",
        "code": "600519",
        "rcode": "",
    }
    assert call["stream"] is True
    assert call["allow_redirects"] is True


def test_fetch_research_reports_fails_closed_on_one_cross_company_row():
    response = _FakeResponse(
        _payload(
            [
                _row(1, publisher_id="80000001"),
                _row(2, publisher_id="80000002"),
                _row(3, code="000001", publisher_id="80000003"),
            ]
        )
    )
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([response]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.sources == []
    assert "identity mismatch" in result.reason
    assert response.closed


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            _FakeResponse(
                _payload([_row(1), _row(2), _row(3)]),
                url="https://evil.example/report/list",
            ),
            "pinned HTTPS",
        ),
        (
            _FakeResponse(
                _payload([_row(1), _row(2), _row(3)]),
                content_type="text/html",
            ),
            "not JSON",
        ),
        (
            _FakeResponse(
                _payload([_row(1), _row(2), _row(3)]),
                declared_length=rr.MAX_RESPONSE_BYTES + 1,
            ),
            "byte limit",
        ),
    ],
)
def test_fetch_research_reports_rejects_redirect_content_type_and_declared_oversize(response, reason):
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([response]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert reason in result.reason
    assert response.closed


def test_fetch_research_reports_retries_transport_failures_and_closes_the_winner(monkeypatch):
    response = _FakeResponse(_payload([_row(1), _row(2), _row(3)]))
    session = _FakeSession([requests.ConnectionError("temporary"), response])
    monkeypatch.setattr(rr.time, "sleep", lambda _seconds: None)

    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available
    assert len([call for call in session.calls if call[0] == rr.EASTMONEY_REPORT_ENDPOINT]) == 2
    assert response.closed


def test_fetch_research_reports_fetches_a_second_page_until_three_publishers_exist():
    first_page = [
        _row(
            index,
            publisher_id="80000001" if index % 2 else "80000002",
            publisher="机构一" if index % 2 else "机构二",
        )
        for index in range(1, rr.PAGE_SIZE + 1)
    ]
    second_page = [_row(51, publisher_id="80000003", publisher="机构三")]
    session = _FakeSession(
        [
            _FakeResponse(_payload(first_page, page=1, hits=51)),
            _FakeResponse(_payload(second_page, page=2, hits=51)),
        ]
    )

    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available
    assert result.distinct_publishers == 3
    assert [call[1]["params"]["pageNo"] for call in session.calls if call[0] == rr.EASTMONEY_REPORT_ENDPOINT] == [1, 2]


def test_fetch_research_reports_uses_stable_publisher_ids_not_names_for_independence():
    same_id = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000001", publisher="机构一研究所"),
        _row(3, publisher_id="80000001", publisher="机构一证券"),
    ]
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([_FakeResponse(_payload(same_id))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.distinct_publishers == 1
    assert len(result.sources) == 1
    assert result.reason == "insufficient_independent_report_metadata"


def test_fetch_research_reports_rejects_stale_rows_even_if_provider_returns_them():
    stale = (date(2026, 7, 17) - timedelta(days=rr.RESEARCH_MAX_AGE_DAYS + 1)).isoformat()
    rows = [_row(index, published=f"{stale} 00:00:00.000") for index in range(1, 4)]
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([_FakeResponse(_payload(rows))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.sources == []
    assert "outside the requested recent window" in result.reason


@pytest.mark.parametrize(
    ("ages", "available", "reason"),
    [
        ((183, 365, 365), True, ""),
        ((184, 365, 365), False, "no_report_within_recent_window"),
    ],
)
def test_fetch_research_reports_enforces_recent_report_inside_365_day_independent_window(
    ages,
    available,
    reason,
):
    cutoff = date(2026, 7, 17)
    rows = [
        _row(
            index,
            publisher_id=f"{80_000_000 + index:08d}",
            published=f"{(cutoff - timedelta(days=age)).isoformat()} 00:00:00.000",
        )
        for index, age in enumerate(ages, start=1)
    ]

    result = rr.fetch_research_reports(
        "600519",
        cutoff,
        session=_FakeSession([_FakeResponse(_payload(rows))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available is available
    assert result.reason == reason
    assert result.distinct_publishers == 3


def test_fetch_research_reports_accepts_day_365_but_rejects_day_366():
    cutoff = date(2026, 7, 17)
    accepted_rows = [
        _row(
            index,
            published=f"{(cutoff - timedelta(days=age)).isoformat()} 00:00:00.000",
        )
        for index, age in enumerate((1, 365, 365), start=1)
    ]
    accepted = rr.fetch_research_reports(
        "600519",
        cutoff,
        session=_FakeSession([_FakeResponse(_payload(accepted_rows))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )
    assert accepted.available

    rejected_rows = [
        _row(
            index,
            published=f"{(cutoff - timedelta(days=age)).isoformat()} 00:00:00.000",
        )
        for index, age in enumerate((1, 2, 366), start=1)
    ]
    rejected = rr.fetch_research_reports(
        "600519",
        cutoff,
        session=_FakeSession([_FakeResponse(_payload(rejected_rows))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )
    assert not rejected.available
    assert rejected.sources == []
    assert "outside the requested recent window" in rejected.reason


def test_fetch_research_reports_cache_is_validated_and_avoids_a_second_network_call(tmp_path):
    first = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([_FakeResponse(_payload([_row(1), _row(2), _row(3)]))]),
        cache_dir=tmp_path,
        cache_ttl_seconds=3_600,
        rate_limiter=_NoWait(),
    )
    assert first.available
    assert first.cache_diagnostic.endswith(";saved")

    second = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([]),
        cache_dir=tmp_path,
        cache_ttl_seconds=3_600,
        rate_limiter=_NoWait(),
        cache_only=True,
    )
    assert second.available
    assert second.cache_hit
    assert second.cache_diagnostic == "hit"
    assert second.sources == first.sources
    loaded = SafeFileCache(
        rr._cache_path("600519", date(2026, 7, 17), tmp_path),
        schema_version=rr.CACHE_SCHEMA_VERSION,
        ttl=3_600,
        max_uncompressed_bytes=rr.MAX_RESPONSE_BYTES,
    ).load()
    assert loaded.hit
    serialized = json.dumps(loaded.value, ensure_ascii=False, sort_keys=True)
    assert "投资要点" not in serialized
    assert "风险提示" not in serialized
    assert "notice_content" not in serialized
    assert "content_sha256" in serialized


def test_cache_only_research_miss_stays_unavailable_without_network(tmp_path):
    session = _FakeSession([])
    result = rr.fetch_research_reports("600519", "2026-08-28", session=session, cache_dir=tmp_path, cache_only=True)
    assert result.available is False
    assert result.cache_hit is False
    assert session.calls == []


@pytest.mark.parametrize(
    "zwinfo_overrides",
    [
        {"notice_title": "伪造标题"},
        {"short_name": "其他公司"},
        {"notice_date": "2026-07-15 00:00:00"},
        {"security": [{"stock": "000001", "short_name": "贵州茅台"}]},
    ],
)
def test_fetch_research_reports_fails_closed_when_one_body_identity_differs(zwinfo_overrides):
    rows = [_row(index) for index in range(1, 4)]
    invalid = _detail_response(rows[-1], zwinfo_overrides=zwinfo_overrides)
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession(
            [_FakeResponse(_payload(rows))],
            detail_overrides={rows[-1]["infoCode"]: invalid},
        ),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.content_verification["verified_bodies"] == 2
    assert result.reason == "insufficient_verified_report_bodies"
    assert invalid.closed


def test_fetch_research_reports_rejects_three_valid_bodies_without_a_shared_fact():
    rows = [_row(index) for index in range(1, 4)]
    overrides = {
        row["infoCode"]: _detail_response(
            row,
            paragraphs=_detail_paragraphs(row, revenue=float(index * 10)),
        )
        for index, row in enumerate(rows, start=1)
    }
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=_FakeSession([_FakeResponse(_payload(rows))], detail_overrides=overrides),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.content_verification["verified_bodies"] == 3
    assert not result.content_verification["cross_check"]["passed"]
    assert result.reason == "no_cross_report_fact_consensus"


def test_report_fact_units_preserve_decimal_100m_values_without_tenfold_error():
    first = rr._extract_key_facts(["2026Q1公司实现营业收入35.50亿元。"])
    second = rr._extract_key_facts(["2026年第一季度公司实现收入35.54亿元。"])

    checked = rr._cross_check_facts({"report-a": first, "report-b": second})

    assert checked["passed"]
    assert checked["fact_key"] == "2026Q1:revenue"
    assert checked["fact_unit"] == "CNY_100M"
    assert checked["consensus_value"] == 35.52
    assert checked["max_relative_spread"] == pytest.approx(0.00112613)


def test_fetch_research_reports_invalidates_old_schema_cache(tmp_path):
    cutoff = date(2026, 7, 17)
    path = rr._cache_path("600519", cutoff, tmp_path)
    SafeFileCache(
        path,
        schema_version=rr.CACHE_SCHEMA_VERSION - 1,
        ttl=3_600,
        max_uncompressed_bytes=rr.MAX_RESPONSE_BYTES,
    ).save(
        {
            "contract": rr._cache_contract("600519", cutoff),
            "sources": [],
        }
    )
    session = _FakeSession([_FakeResponse(_payload([_row(1), _row(2), _row(3)]))])

    result = rr.fetch_research_reports(
        "600519",
        cutoff,
        session=session,
        cache_dir=tmp_path,
        cache_ttl_seconds=3_600,
        rate_limiter=_NoWait(),
    )

    assert rr.MODEL_ID.endswith("-v4")
    assert rr.CACHE_SCHEMA_VERSION == 4
    assert result.available
    assert not result.cache_hit
    assert result.cache_diagnostic.startswith("miss:schema_version_mismatch")
    assert len([call for call in session.calls if call[0] == rr.EASTMONEY_REPORT_ENDPOINT]) == 1


def test_fetch_research_reports_rejects_unbounded_or_malformed_inputs_before_network():
    session = _FakeSession([])
    with pytest.raises(ValueError, match="Shanghai/Shenzhen"):
        rr.fetch_research_reports("920001", "2026-07-17", session=session, use_cache=False)
    with pytest.raises(ValueError, match="timeout"):
        rr.fetch_research_reports(
            "600519",
            "2026-07-17",
            session=session,
            use_cache=False,
            timeout=(0, 30),
        )
    assert session.calls == []


def test_fetch_research_reports_batch_is_sorted_and_worker_failure_isolated(monkeypatch):
    def fake_fetch(code, as_of, *, cache_only=False):
        assert cache_only is False
        if code == "000001":
            raise RuntimeError("one worker failed")
        return rr.ResearchReportEvidence(
            available=False,
            code=code,
            as_of=as_of,
            model_id=rr.MODEL_ID,
            sources=[],
            distinct_publishers=0,
            content_verification=rr._empty_content_verification(
                code,
                date.fromisoformat(as_of),
                "metadata_prerequisite_failed",
            ),
            cache_hit=False,
            cache_diagnostic="",
            reason="insufficient_independent_report_metadata",
        )

    monkeypatch.setattr(rr, "fetch_research_reports", fake_fetch)
    progress = []
    result = rr.fetch_research_reports_batch(
        [
            {"code": "600519", "as_of": "2026-07-17"},
            {"code": "000001", "as_of": "2026-07-17"},
        ],
        max_workers=2,
        progress_cb=lambda done, total: progress.append((done, total)),
    )

    assert list(result) == ["000001", "600519"]
    assert result["000001"]["reason"].startswith("worker_failure:")
    assert result["600519"]["reason"] == "insufficient_independent_report_metadata"
    assert sorted(progress) == [(1, 2), (2, 2)]

    with pytest.raises(ValueError, match="duplicate"):
        rr.fetch_research_reports_batch(
            [
                {"code": "600519", "as_of": "2026-07-17"},
                {"code": "600519", "as_of": "2026-07-17"},
            ]
        )


def _save_research_cache(cache_dir, code, as_of, sources, content_verification):
    cache = SafeFileCache(
        rr._cache_path(code, as_of, cache_dir),
        schema_version=rr.CACHE_SCHEMA_VERSION,
        ttl=rr.CACHE_TTL_SECONDS,
        max_uncompressed_bytes=rr.MAX_RESPONSE_BYTES,
    )
    cache.save(
        {
            "contract": rr._cache_contract(code, as_of),
            "sources": sources,
            "content_verification": content_verification,
        }
    )


def test_fetch_research_reports_reuses_a_recent_capture_under_a_later_cutoff(tmp_path):
    rows = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000002", publisher="机构二"),
        _row(3, publisher_id="80000003", publisher="机构三"),
    ]
    session = _FakeSession([_FakeResponse(_payload(rows))])
    captured = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )
    assert captured.available
    _save_research_cache(
        tmp_path,
        "600519",
        date(2026, 7, 17),
        captured.sources,
        captured.content_verification,
    )

    later_session = _FakeSession([])
    reused = rr.fetch_research_reports(
        "600519",
        "2026-07-20",
        session=later_session,
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    assert reused.cache_hit
    assert reused.cache_diagnostic == "reused_source_as_of:2026-07-17"
    assert reused.available
    assert later_session.calls == [], "reuse must not touch the network"
    assert reused.as_of == "2026-07-20"


def test_fetch_research_reports_exact_hit_takes_priority_over_reuse(tmp_path):
    rows = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000002", publisher="机构二"),
        _row(3, publisher_id="80000003", publisher="机构三"),
    ]
    session = _FakeSession([_FakeResponse(_payload(rows))])
    captured = rr.fetch_research_reports(
        "600519",
        "2026-07-17",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )
    _save_research_cache(
        tmp_path,
        "600519",
        date(2026, 7, 17),
        captured.sources,
        captured.content_verification,
    )
    later = rr.fetch_research_reports(
        "600519",
        "2026-07-18",
        session=_FakeSession([]),
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    assert later.cache_diagnostic == "reused_source_as_of:2026-07-17"
    _save_research_cache(
        tmp_path,
        "600519",
        date(2026, 7, 18),
        later.sources,
        later.content_verification,
    )
    exact = rr.fetch_research_reports(
        "600519",
        "2026-07-18",
        session=_FakeSession([]),
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    assert exact.cache_hit
    assert exact.cache_diagnostic == "hit"


def test_fetch_research_reports_refetches_when_reuse_window_expired(tmp_path):
    rows = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000002", publisher="机构二"),
        _row(3, publisher_id="80000003", publisher="机构三"),
    ]
    session = _FakeSession([_FakeResponse(_payload(rows))])
    captured = rr.fetch_research_reports(
        "600519",
        "2026-07-01",
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )
    _save_research_cache(
        tmp_path,
        "600519",
        date(2026, 7, 1),
        captured.sources,
        captured.content_verification,
    )

    fresh_rows = [
        _row(10, publisher_id="80000001", publisher="机构一"),
        _row(11, publisher_id="80000002", publisher="机构二"),
        _row(12, publisher_id="80000003", publisher="机构三"),
    ]
    fresh_session = _FakeSession([_FakeResponse(_payload(fresh_rows))])
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-20",
        session=fresh_session,
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    assert not result.cache_hit
    assert fresh_session.calls, "expired captures must refetch from the source"
    assert result.available


def test_fetch_research_reports_ignores_corrupt_reusable_payload_and_refetches(tmp_path):
    cache = SafeFileCache(
        rr._cache_path("600519", date(2026, 7, 17), tmp_path),
        schema_version=rr.CACHE_SCHEMA_VERSION,
        ttl=rr.CACHE_TTL_SECONDS,
        max_uncompressed_bytes=rr.MAX_RESPONSE_BYTES,
    )
    cache.save(
        {
            "contract": rr._cache_contract("600519", date(2026, 7, 17)),
            "sources": "not-a-list",
            "content_verification": {},
        }
    )
    rows = [
        _row(1, publisher_id="80000001", publisher="机构一"),
        _row(2, publisher_id="80000002", publisher="机构二"),
        _row(3, publisher_id="80000003", publisher="机构三"),
    ]
    fresh_session = _FakeSession([_FakeResponse(_payload(rows))])
    result = rr.fetch_research_reports(
        "600519",
        "2026-07-20",
        session=fresh_session,
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    assert not result.cache_hit
    assert fresh_session.calls
    assert result.available
