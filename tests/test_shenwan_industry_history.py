from __future__ import annotations

import base64
from datetime import date
import hashlib
import json

import pandas as pd
import pytest

from data.provider_http import RequestRateLimiter
from data.cache import SafeFileCache
from data.shenwan_industry_history import (
    CACHE_SCHEMA_VERSION,
    CNINFO_HISTORY_URL,
    SHENWAN_XLS_URL,
    ShenwanIndustryBatch,
    ShenwanIndustryHistory,
    ShenwanIndustryHistoryError,
    ShenwanIndustryRecord,
    _cninfo_enckey,
    audit_shenwan_industry_drift,
    fetch_cninfo_industry_history_batch,
    fetch_shenwan_industry_history,
    parse_shenwan_xls,
    resolve_shenwan_industry_history,
    shenwan_industry_as_of,
)


_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture"


class _Response:
    def __init__(self, payload: bytes, *, url: str) -> None:
        self._payload = payload
        self.url = url
        self.headers = {"Content-Length": str(len(payload))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 64 * 1024):
        del chunk_size
        yield self._payload


class _Session:
    def __init__(self, payloads: list[bytes], *, url: str) -> None:
        self.payloads = list(payloads)
        self.url = url
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Response(self.payloads.pop(0), url=self.url)


def _record(code: str, effective: str, industry: str, *, standard: str = "shenwan_official_workbook"):
    return ShenwanIndustryRecord(
        code=code,
        effective_from=effective,
        industry_code=industry,
        l1_code=industry[:2] + "0000",
        l2_code=industry[:4] + "00",
        classification_standard=standard,
        source_name="test",
        source_url=SHENWAN_XLS_URL,
        source_sha256="a" * 64,
    )


def test_parse_shenwan_xls_normalises_codes_dates_and_levels(monkeypatch) -> None:
    frame = pd.DataFrame(
        [
            {
                "股票代码": 1,
                "计入日期": "1991-04-03",
                "行业代码": 440101,
                "更新日期": "2014-02-21",
            },
            {
                "股票代码": "000001",
                "计入日期": pd.Timestamp("2014-02-21"),
                "行业代码": "480101",
                "更新日期": None,
            },
        ]
    )
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: frame)

    history = parse_shenwan_xls(_OLE, min_records=2)

    assert [row.code for row in history.records] == ["000001", "000001"]
    assert history.records[0].l1_code == "440000"
    assert history.records[1].l2_code == "480100"
    assert history.source_sha256 == hashlib.sha256(_OLE).hexdigest()
    assert shenwan_industry_as_of(history.records, "1", "2013-01-01").industry_code == "440101"
    assert shenwan_industry_as_of(history.records, "000001", "2016-01-01").industry_code == "480101"


def test_parse_shenwan_xls_rejects_structure_and_duplicate_identity(monkeypatch) -> None:
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: pd.DataFrame([{"股票代码": "000001"}]))
    with pytest.raises(ShenwanIndustryHistoryError, match="missing columns"):
        parse_shenwan_xls(_OLE)

    duplicate = pd.DataFrame(
        [
            {"股票代码": "000001", "计入日期": "2021-07-30", "行业代码": "480301"},
            {"股票代码": "000001", "计入日期": "2021-07-30", "行业代码": "480301"},
        ]
    )
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: duplicate)
    with pytest.raises(ShenwanIndustryHistoryError, match="duplicate"):
        parse_shenwan_xls(_OLE)


def test_fetch_shenwan_xls_uses_strict_https_and_verified_cache(tmp_path, monkeypatch) -> None:
    frame = pd.DataFrame([{"股票代码": "000001", "计入日期": "2021-07-30", "行业代码": "480301"}])
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: frame)
    session = _Session([_OLE], url=SHENWAN_XLS_URL)

    first = fetch_shenwan_industry_history(
        session=session,
        cache_dir=tmp_path,
        min_records=1,
    )
    second = fetch_shenwan_industry_history(
        session=session,
        cache_dir=tmp_path,
        min_records=1,
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == SHENWAN_XLS_URL
    assert "verify" not in session.calls[0]

    cache_path = next(tmp_path.glob("*official-xls*.json.gz"))
    payload = SafeFileCache(cache_path, schema_version=CACHE_SCHEMA_VERSION, ttl=7 * 24 * 3600).load().value
    assert set(payload) == {"contract", "raw_response_base64", "source_sha256"}
    poisoned = dict(payload)
    poisoned["raw_response_base64"] = base64.b64encode(_OLE + b"tampered").decode("ascii")
    SafeFileCache(cache_path, schema_version=CACHE_SCHEMA_VERSION, ttl=7 * 24 * 3600).save(poisoned)
    replacement = _Session([_OLE], url=SHENWAN_XLS_URL)
    refreshed = fetch_shenwan_industry_history(
        session=replacement,
        cache_dir=tmp_path,
        min_records=1,
    )
    assert refreshed.cache_hit is False
    assert len(replacement.calls) == 1


def test_cninfo_enckey_is_aes_cbc_epoch_token() -> None:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    token = _cninfo_enckey(1_800_000_000)
    key = b"1234567887654321"
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    padded = decryptor.update(base64.b64decode(token)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    assert unpadder.update(padded) + unpadder.finalize() == b"1800000000"


def _cninfo_payload(code: str = "000001") -> bytes:
    records = [
        {
            "SECCODE": code,
            "VARYDATE": "1991-04-03",
            "F001V": "008018",
            "F002V": "申银万国行业分类标准(旧)",
            "F003V": "S440101",
            "F008C": "0",
        },
        {
            "SECCODE": code,
            "VARYDATE": "2021-07-30",
            "F001V": "008003",
            "F002V": "申银万国行业分类标准",
            "F003V": "S480301",
            "F008C": "1",
        },
        {
            "SECCODE": code,
            "VARYDATE": "2024-02-08",
            "F001V": "008001",
            "F002V": "中国上市公司协会上市公司行业分类标准",
            "F003V": "J66",
            "F008C": "1",
        },
    ]
    return json.dumps({"total": len(records), "records": records}, ensure_ascii=False).encode()


def test_cninfo_batch_filters_exact_shenwan_standards_and_caches(tmp_path) -> None:
    session = _Session([_cninfo_payload()], url=CNINFO_HISTORY_URL)

    first = fetch_cninfo_industry_history_batch(
        ["1"],
        "2026-08-30",
        session=session,
        cache_dir=tmp_path,
        enckey_factory=lambda: "token",
        rate_limiter=RequestRateLimiter(0),
    )
    second = fetch_cninfo_industry_history_batch(
        ["000001"],
        date(2026, 8, 30),
        session=session,
        cache_dir=tmp_path,
        enckey_factory=lambda: "unused",
        rate_limiter=RequestRateLimiter(0),
    )

    assert first.request_count == 1
    assert first.failures == {}
    assert [item.industry_code for item in first.histories["000001"].records] == ["440101", "480301"]
    assert [item.classification_standard for item in first.histories["000001"].records] == [
        "shenwan_legacy",
        "shenwan_current",
    ]
    assert second.request_count == 0
    assert second.histories["000001"].cache_hit is True
    assert len(session.calls) == 1
    assert session.calls[0]["headers"]["Accept-Enckey"] == "token"
    assert session.calls[0]["params"]["edate"] == "2026-08-30"
    assert "verify" not in session.calls[0]


def test_cninfo_batch_rejects_foreign_identity_and_hard_request_overflow(tmp_path) -> None:
    session = _Session([_cninfo_payload("000002")], url=CNINFO_HISTORY_URL)
    result = fetch_cninfo_industry_history_batch(
        ["000001"],
        "2026-08-30",
        session=session,
        cache_dir=tmp_path,
        use_cache=False,
        enckey_factory=lambda: "token",
        rate_limiter=RequestRateLimiter(0),
    )
    assert result.histories == {}
    assert result.failures == {"000001": "ShenwanIndustryHistoryError"}

    with pytest.raises(ShenwanIndustryHistoryError, match="hard limit"):
        fetch_cninfo_industry_history_batch(
            ["000001", "000002"],
            "2026-08-30",
            cache_dir=tmp_path,
            use_cache=False,
            request_limit=1,
        )

    with pytest.raises(ShenwanIndustryHistoryError, match="duplicate company codes"):
        fetch_cninfo_industry_history_batch(
            ["000001", "1"],
            "2026-08-30",
            cache_dir=tmp_path,
        )


def test_cninfo_rejects_duplicate_json_keys_and_credentialed_redirects(tmp_path) -> None:
    duplicate = b'{"total":0,"total":0,"records":[]}'
    duplicate_session = _Session([duplicate], url=CNINFO_HISTORY_URL)
    result = fetch_cninfo_industry_history_batch(
        ["000001"],
        "2026-08-30",
        session=duplicate_session,
        cache_dir=tmp_path,
        use_cache=False,
        enckey_factory=lambda: "token",
        rate_limiter=RequestRateLimiter(0),
    )
    assert result.failures == {"000001": "ShenwanIndustryHistoryError"}

    redirected = _Session([_cninfo_payload()], url="https://user@webapi.cninfo.com.cn/api/stock/p_stock2110")
    result = fetch_cninfo_industry_history_batch(
        ["000001"],
        "2026-08-30",
        session=redirected,
        cache_dir=tmp_path,
        use_cache=False,
        enckey_factory=lambda: "token",
        rate_limiter=RequestRateLimiter(0),
    )
    assert result.failures == {"000001": "ShenwanIndustryHistoryError"}


def test_cninfo_batch_rejects_shenwan_records_after_the_requested_cutoff(tmp_path) -> None:
    payload = json.loads(_cninfo_payload())
    payload["records"][1]["VARYDATE"] = "2026-09-01"
    session = _Session([json.dumps(payload, ensure_ascii=False).encode()], url=CNINFO_HISTORY_URL)

    result = fetch_cninfo_industry_history_batch(
        ["000001"],
        "2026-08-30",
        session=session,
        cache_dir=tmp_path,
        use_cache=False,
        enckey_factory=lambda: "token",
        rate_limiter=RequestRateLimiter(0),
    )

    assert result.histories == {}
    assert result.failures == {"000001": "ShenwanIndustryHistoryError"}


def test_resolver_only_falls_back_for_exact_missing_codes() -> None:
    primary = ShenwanIndustryHistory(
        records=(_record("000001", "2021-07-30", "480301"),),
        source_name="test",
        source_url=SHENWAN_XLS_URL,
        source_sha256="a" * 64,
    )
    fallback_history = ShenwanIndustryHistory(
        records=(_record("000002", "2021-07-30", "480301", standard="shenwan_current"),),
        source_name="cninfo",
        source_url=CNINFO_HISTORY_URL,
        source_sha256="b" * 64,
    )
    calls = []

    def fallback(codes, as_of):
        calls.append((tuple(codes), as_of))
        return ShenwanIndustryBatch(histories={"000002": fallback_history}, failures={}, request_count=1)

    result = resolve_shenwan_industry_history(
        ["000001", "000002"],
        "2026-08-30",
        xls_loader=lambda: primary,
        cninfo_loader=fallback,
    )

    assert calls == [(("000002",), date(2026, 8, 30))]
    assert result.primary_source_available is True
    assert result.fallback_codes == ("000002",)
    assert result.unresolved_codes == ()
    assert {record.code for record in result.records} == {"000001", "000002"}


def test_resolver_uses_cninfo_when_the_strict_xls_source_is_unavailable() -> None:
    fallback_history = ShenwanIndustryHistory(
        records=(_record("000001", "2021-07-30", "480301", standard="shenwan_current"),),
        source_name="cninfo",
        source_url=CNINFO_HISTORY_URL,
        source_sha256="b" * 64,
    )

    def unavailable():
        raise ShenwanIndustryHistoryError("TLS validation failed")

    result = resolve_shenwan_industry_history(
        ["000001"],
        "2026-08-30",
        xls_loader=unavailable,
        cninfo_loader=lambda *_args: ShenwanIndustryBatch(
            histories={"000001": fallback_history},
            failures={},
            request_count=1,
        ),
    )

    assert result.primary_source_available is False
    assert result.fallback_codes == ("000001",)
    assert result.unresolved_codes == ()
    assert result.source_errors == {"申万研究行业分类历史公开表": "ShenwanIndustryHistoryError"}


def test_drift_audit_is_point_in_time_and_does_not_remap_model_industries() -> None:
    records = (
        _record("000001", "1991-04-03", "440101"),
        _record("000001", "2014-02-21", "480101"),
        _record("000001", "2021-07-30", "480301"),
        _record("000002", "2000-01-01", "480301"),
    )

    rows = audit_shenwan_industry_drift(
        records,
        ["000001", "000002", "000003"],
        from_as_of="2016-01-01",
        to_as_of="2026-08-30",
    )

    assert rows[0]["status"] == "changed"
    assert rows[0]["from_industry_code"] == "480101"
    assert rows[0]["to_industry_code"] == "480301"
    assert rows[1]["status"] == "unchanged"
    assert rows[2]["status"] == "missing_from"
    assert all("model_industry" not in row for row in rows)
