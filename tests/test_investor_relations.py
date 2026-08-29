from __future__ import annotations

from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

from data.investor_relations import (
    CNINFO_IR_LOOKUP_URL,
    CNINFO_IR_QUESTION_URL,
    InvestorRelationsClient,
    attach_investor_relations_evidence,
)


def _epoch(day: date) -> int:
    value = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(value.timestamp() * 1000)


class _Response:
    def __init__(self, url: str, payload) -> None:
        self.url = url
        self._raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.headers = {"Content-Length": str(len(self._raw))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield from (self._raw[offset : offset + chunk_size] for offset in range(0, len(self._raw), chunk_size))

    def close(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url, **_kwargs):
        self.calls.append(url)
        if url == CNINFO_IR_LOOKUP_URL:
            return _Response(url, {"statusCode": 200, "data": [{"stockCode": "000001", "secid": "gssz0000001"}]})
        if url == CNINFO_IR_QUESTION_URL:
            return _Response(
                url,
                {
                    "pageNo": 1,
                    "pageSize": 20,
                    "total": 1,
                    "totalPage": 1,
                    "rows": [
                        {
                            "stockCode": "000001",
                            "indexId": "2340017250482114560",
                            "mainContent": "公司研发投入和产品进展如何？",
                            "attachedContent": "公司将以定期报告披露数据为准。",
                            "updateDate": _epoch(date(2026, 8, 28)),
                        }
                    ],
                },
            )
        raise AssertionError(url)


def test_ir_client_replays_raw_cache_and_never_marks_company_reply_independent(tmp_path):
    session = _Session()
    client = InvestorRelationsClient(session=session, cache_dir=tmp_path, retries=1, request_interval=0)
    first = client.fetch("000001", as_of=date(2026, 8, 28))
    second = client.fetch("000001", as_of=date(2026, 8, 28))

    assert first == second
    assert session.calls == [CNINFO_IR_LOOKUP_URL, CNINFO_IR_QUESTION_URL]
    assert first[0]["security_code"] == "000001"
    assert first[0]["source_role"] == "company_statement"
    assert first[0]["independent"] is False
    assert first[0]["use_for_automatic_score"] is False
    assert len(first[0]["source_raw_sha256"]) == 64


def test_ir_attachment_targets_only_missing_rd_and_adds_no_score_fields():
    financials = {
        "000001": {"indicators": [{"REPORT_DATE": "2025-12-31", "RDEXPEND": None}]},
        "000002": {"indicators": [{"REPORT_DATE": "2025-12-31", "RDEXPEND": 100.0}]},
    }
    evidence = [
        {
            "schema_version": 1,
            "security_code": "000001",
            "evidence_id": "cninfo-ir:000001:2340017250482114560",
            "as_of": "2026-08-28",
            "question": "问题",
            "company_answer": "回复",
            "source": "深交所互动易公司回复",
            "source_url": "https://irm.cninfo.com.cn/ircs/search?keyword=000001",
            "source_raw_sha256": "a" * 64,
            "source_role": "company_statement",
            "independent": False,
            "use_for_automatic_score": False,
        }
    ]

    class _Client:
        def load_cached(self, code, *, as_of):
            assert code == "000001"
            return evidence

        def fetch(self, *_args, **_kwargs):
            raise AssertionError("cache should satisfy the target")

        def diagnostic(self):
            return {"network_requests": 0, "cache_hits": 1, "request_limit": 72, "max_parallel_requests": 1}

    output, diagnostic = attach_investor_relations_evidence(
        financials,
        as_of=date(2026, 8, 28),
        client=_Client(),
    )

    assert output["000001"]["investor_relations_evidence"] == evidence
    assert "investor_relations_evidence" not in output["000002"]
    assert not any("score" in key.lower() for key in output["000001"])
    assert diagnostic["automatic_score_enabled"] is False
