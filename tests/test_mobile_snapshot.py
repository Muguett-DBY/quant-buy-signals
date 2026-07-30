import gzip
import hashlib
import json
import re

import pandas as pd
import pytest

from data import mobile_snapshot
from engine.buy_screener import screen_all_types


def _scores():
    return screen_all_types(
        {"000001": {}},
        pd.DataFrame(
            [
                {
                    "code": "000001",
                    "name": "样本",
                    "market": "SZ",
                    "price": 10.0,
                    "pe": 10.0,
                    "pb": 1.0,
                    "market_cap": 1_000_000_000.0,
                    "quote_status": "trading",
                    "price_source": "last_trade",
                }
            ]
        ),
    )


def test_mobile_snapshot_exports_verified_compact_catalogue_and_hashes(tmp_path):
    manifest = mobile_snapshot.write_mobile_snapshot(
        tmp_path,
        _scores(),
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True, "score_rows": 1},
        provenance={"git_commit": "a" * 40},
    )

    catalog_filename = manifest["catalogue"]["filename"]
    signals_filename = manifest["signals"]["filename"]
    signature_filename = manifest["signature"]["filename"]
    company_details = manifest["company_details"]
    assert re.fullmatch(r"catalog-[0-9a-f]{16}\.json\.gz", catalog_filename)
    assert re.fullmatch(r"signals-[0-9a-f]{16}\.json\.gz", signals_filename)
    assert re.fullmatch(r"manifest-[0-9a-f]{16}\.sig", signature_filename)
    generation = catalog_filename.removeprefix("catalog-").removesuffix(".json.gz")
    assert signals_filename == f"signals-{generation}.json.gz"
    assert signature_filename == f"manifest-{generation}.sig"
    assert manifest["signature"]["algorithm"] == "ECDSA_P256_SHA256"
    assert company_details["schema_version"] == mobile_snapshot.COMPANY_DETAIL_SCHEMA_VERSION
    assert company_details["record_schema"] == "company_detail_v2"
    assert company_details["company_count"] == 1
    assert company_details["partition"] == {
        "algorithm": "sha256_code_first_nibble",
        "shard_count": mobile_snapshot.COMPANY_DETAIL_SHARD_COUNT,
    }
    assert company_details["root_algorithm"] == "SHA256_CANONICAL_SHARD_INDEX_V1"
    assert re.fullmatch(r"[0-9a-f]{64}", company_details["root_sha256"])
    assert [entry["id"] for entry in company_details["shards"]] == [
        f"{index:02x}" for index in range(mobile_snapshot.COMPANY_DETAIL_SHARD_COUNT)
    ]

    catalog_path = tmp_path / catalog_filename
    signals_path = tmp_path / signals_filename
    manifest_path = tmp_path / mobile_snapshot.MANIFEST_FILENAME
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest["catalogue"]["size"] == catalog_path.stat().st_size
    assert manifest["signals"]["size"] == signals_path.stat().st_size
    assert manifest["catalogue"]["uncompressed_size"] == len(gzip.decompress(catalog_path.read_bytes()))
    assert manifest["signals"]["uncompressed_size"] == len(gzip.decompress(signals_path.read_bytes()))
    assert manifest["catalogue"]["sha256"] == hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    assert manifest["signals"]["sha256"] == hashlib.sha256(signals_path.read_bytes()).hexdigest()

    catalogue = json.loads(gzip.decompress(catalog_path.read_bytes()))
    signals = json.loads(gzip.decompress(signals_path.read_bytes()))
    canonical_catalogue = json.dumps(
        catalogue, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    canonical_signals = json.dumps(
        signals, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert (
        generation
        == hashlib.sha256(
            canonical_catalogue
            + b"\0"
            + canonical_signals
            + b"\0company-details-v2\0"
            + bytes.fromhex(company_details["root_sha256"])
        ).hexdigest()[:16]
    )
    root_contract = {
        "schema_version": mobile_snapshot.COMPANY_DETAIL_SCHEMA_VERSION,
        "record_schema": "company_detail_v2",
        "partition": company_details["partition"],
        "shards": [
            {
                "id": entry["id"],
                "company_count": entry["company_count"],
                "uncompressed_sha256": entry["uncompressed_sha256"],
            }
            for entry in company_details["shards"]
        ],
    }
    canonical_root = json.dumps(
        root_contract, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert company_details["root_sha256"] == hashlib.sha256(canonical_root).hexdigest()
    detail_companies = []
    for entry in company_details["shards"]:
        assert entry["filename"] == f"company-details-{generation}-{entry['id']}.json.gz"
        path = tmp_path / entry["filename"]
        compressed = path.read_bytes()
        raw = gzip.decompress(compressed)
        payload = json.loads(raw)
        assert entry["size"] == len(compressed)
        assert entry["uncompressed_size"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(compressed).hexdigest()
        assert entry["uncompressed_sha256"] == hashlib.sha256(raw).hexdigest()
        assert payload["schema_version"] == mobile_snapshot.COMPANY_DETAIL_SCHEMA_VERSION
        assert payload["record_schema"] == "company_detail_v2"
        assert payload["shard_id"] == entry["id"]
        assert payload["company_count"] == entry["company_count"] == len(payload["companies"])
        assert all(
            mobile_snapshot._company_detail_shard_id(company["code"]) == entry["id"] for company in payload["companies"]
        )
        detail_companies.extend(payload["companies"])
    assert [company["code"] for company in detail_companies] == ["000001"]
    detail = detail_companies[0]
    assert detail["schema_version"] == mobile_snapshot.COMPANY_DETAIL_SCHEMA_VERSION
    assert set(detail["types"]) == {f"type{number}" for number in range(1, 8)}
    assert detail["types"]["type1"]["decision"] == catalogue["companies"][0]["types"]["type1"]["decision"]
    assert catalogue["company_count"] == 1
    assert catalogue["companies"][0]["code"] == "000001"
    assert set(catalogue["companies"][0]["types"]) == {f"type{number}" for number in range(1, 8)}
    assert all("triggered" not in state for state in catalogue["companies"][0]["types"].values())
    assert catalogue["companies"][0]["pending_types"] == ["type1", "type2"]
    assert set(catalogue["companies"][0]["types"]["type1"]["decision"]) == {
        "schema_version",
        "model_id",
        "decision_complete",
        "decision_basis",
        "score_lower_bound",
        "score_upper_bound",
        "veto_state",
        "potentially_triggerable",
        "missing_dimensions",
    }
    assert catalogue["companies"][0]["types"]["type1"]["decision"]["potentially_triggerable"] is True
    assert signals["triggered_company_count"] == 0
    assert signals["conditional_company_count"] == 0
    assert signals["conditional_only_company_count"] == 0
    assert signals["pending_company_count"] == 1
    assert signals["visible_candidate_company_count"] == 1
    assert signals["candidate_detail_count"] == 0
    assert manifest["summary"]["pending_company_count"] == 1
    assert manifest["summary"]["visible_candidate_company_count"] == 1
    assert manifest["capabilities"] == {
        "dimension_scores": True,
        "dimension_score_estimates": True,
        "decision_contract": True,
    }
    assert catalogue["capabilities"] == manifest["capabilities"]
    assert signals["capabilities"] == manifest["capabilities"]
    assert sum(catalogue["coverage"]["type1"].values()) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [
            catalog_filename,
            signals_filename,
            mobile_snapshot.MANIFEST_FILENAME,
            *(entry["filename"] for entry in company_details["shards"]),
        ]
    )


def test_company_detail_v2_publishes_all_public_type_meta_without_growing_catalogue(tmp_path, monkeypatch):
    scores = _scores()
    scores.at[0, "source_trade_date"] = "2026-07-17"
    type4 = dict(scores.at[0, "type4"])
    type4.update(
        {
            "status": "observe",
            "triggered": False,
            "applicable": True,
            "evidence_complete": True,
            "total": 5.8,
            "sub_scores": {"4a": 4.0, "4b": 8.0, "4c": 7.0, "4d": 4.0, "4e": 5.0, "4f": 6.0},
            "reasons": {
                "4a": "增长趋势偏弱",
                "4b": "利润与现金流较厚",
                "4c": "多年盈利较稳定",
                "4d": "终局估值偏高",
                "4e": "行业扩张需观察",
                "4f": "乐观上沿透支三年",
                "_blocked": "交易状态暂不支持",
                "_adjustment": "同行样本调整完成",
                "_coverage": "同行样本覆盖八成",
                "_profile": "平稳产业反转型",
                "_score_quality": "完整证据评分",
                "_4f_formula": "乐观上沿透支三年",
            },
            "decision": {
                **dict(type4["decision"]),
                "decision_complete": True,
                "decision_basis": "full_evidence",
                "score_lower_bound": 5.8,
                "score_upper_bound": 5.8,
                "veto_state": "none",
                "potentially_triggerable": False,
                "missing_dimensions": [],
            },
        }
    )
    scores.at[0, "type4"] = type4
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    manifest = mobile_snapshot.write_mobile_snapshot(
        tmp_path,
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    catalogue_path = tmp_path / manifest["catalogue"]["filename"]
    catalogue = json.loads(gzip.decompress(catalogue_path.read_bytes()))
    company = catalogue["companies"][0]
    assert "company_details" not in catalogue
    assert "type_details" not in company
    assert set(company) == {
        "code",
        "name",
        "industry",
        "industry_code",
        "price",
        "pe",
        "pb",
        "market_cap",
        "buy_types",
        "conditional_types",
        "pending_types",
        "primary_type",
        "primary_label",
        "diagnostic_type",
        "diagnostic_label",
        "diagnostic_score",
        "types",
    }
    assert catalogue["capabilities"] == {
        "dimension_scores": True,
        "dimension_score_estimates": True,
        "decision_contract": True,
    }

    shard_id = mobile_snapshot._company_detail_shard_id("000001")
    shard_meta = next(entry for entry in manifest["company_details"]["shards"] if entry["id"] == shard_id)
    shard = json.loads(gzip.decompress((tmp_path / shard_meta["filename"]).read_bytes()))
    detail = shard["companies"][0]
    assert detail["source_trade_date"] == "2026-07-17"
    assert set(detail["types"]) == {f"type{number}" for number in range(1, 8)}
    exported = detail["types"]["type4"]
    assert exported["applicable"] is True
    assert exported["evidence_complete"] is True
    assert exported["decision"]["decision_basis"] == "full_evidence"
    assert exported["reasons"] == {
        "_blocked": "交易状态暂不支持",
        "_adjustment": "同行样本调整完成",
        "_coverage": "同行样本覆盖八成",
        "_profile": "平稳产业反转型",
        "_score_quality": "完整证据评分",
        "_4f_formula": "乐观上沿透支三年",
        "4a": "增长趋势偏弱",
        "4b": "利润与现金流较厚",
        "4c": "多年盈利较稳定",
        "4d": "终局估值偏高",
        "4e": "行业扩张需观察",
        "4f": "乐观上沿透支三年",
    }
    catalogue_text = json.dumps(catalogue, ensure_ascii=False)
    assert "同行样本调整完成" not in catalogue_text
    assert "同行样本覆盖八成" not in catalogue_text


def test_mobile_catalogue_exposes_each_verified_sub_score_and_plain_evidence(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": "triggered",
            "triggered": True,
            "evidence_complete": True,
            "total": 8.0,
            "sub_scores": {"1a": 8.0, "1b": 7.0, "1c": 9.0, "1d": 6.0},
            "reasons": {"1a": "买入区内折价", "1b": "陷阱排查通过", "1c": "现金流安全边际", "1d": "业绩拐点催化"},
            "veto": False,
            "decision": {
                **dict(type1["decision"]),
                "decision_complete": True,
                "decision_basis": "full_evidence",
                "score_lower_bound": 8.0,
                "score_upper_bound": 8.0,
                "veto_state": "none",
                "potentially_triggerable": True,
                "missing_dimensions": [],
            },
        }
    )
    scores.at[0, "type1"] = type1
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    types = catalogue["companies"][0]["types"]
    type1 = types["type1"]
    assert type1["applicable"] is True
    assert type1["evidence_complete"] is True
    assert set(type1["sub_scores"]) == {"1a", "1b", "1c", "1d"}
    assert all(0.0 <= float(value) <= 10.0 for value in type1["sub_scores"].values())
    assert set(type1["sub_score_reasons"]) == {"1a", "1b", "1c", "1d"}
    assert all(type1["sub_score_reasons"].values())
    assert types["type3"]["status"] == "not_applicable"
    assert types["type3"]["applicable"] is False
    assert types["type3"].get("sub_scores", {}) == {}


def test_mobile_catalogue_keeps_known_dimensions_when_only_one_dimension_is_missing(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": "insufficient_evidence",
            "triggered": False,
            "evidence_complete": False,
            "total": 0.0,
            "sub_scores": {"1a": 8.0, "1b": 7.0, "1c": 0.0, "1d": 6.0},
            "reasons": {
                "1a": "买入区内折价",
                "1b": "陷阱排查通过",
                "1c": "内部缺失占位",
                "1d": "业绩拐点催化",
            },
            "decision": {
                **dict(type1["decision"]),
                "decision_complete": False,
                "decision_basis": "unresolved_missing_evidence",
                "score_lower_bound": 5.45,
                "score_upper_bound": 7.45,
                "potentially_triggerable": True,
                "missing_dimensions": ["1c"],
            },
        }
    )
    scores.at[0, "type1"] = type1
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    exported = catalogue["companies"][0]["types"]["type1"]
    assert exported["score"] is None
    assert set(exported["sub_scores"]) == {"1a", "1b", "1d"}
    assert set(exported["sub_score_reasons"]) == {"1a", "1b", "1d"}
    assert "1c" not in exported["sub_scores"]
    assert exported["estimated_sub_scores"] == {"1c": 0.0}
    assert exported["estimated_sub_score_reasons"]["1c"].startswith("未确认估算，不用于触发")


@pytest.mark.parametrize(
    ("status", "decision_basis", "veto_state", "expected_reason"),
    [
        ("vetoed", "confirmed_veto", "confirmed", "买入区深度不足"),
        (
            "not_triggered",
            "conservative_upper_bound",
            "none",
            "补全全部缺失证据后仍不达标",
        ),
    ],
)
def test_mobile_catalogue_never_publishes_missing_placeholders_as_exact_scores(
    monkeypatch,
    status,
    decision_basis,
    veto_state,
    expected_reason,
):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": status,
            "triggered": False,
            "evidence_complete": False,
            "total": 4.0,
            "sub_scores": {"1a": 1.5, "1b": 8.0, "1c": 3.8, "1d": 2.0},
            "reasons": {
                "1a": "远离买入区",
                "1b": "陷阱排查通过",
                "1c": "现金流安全边际",
                "1d": "内部缺失占位",
                "_missing": "缺催化剂证据",
                "_condition": "补全全部缺失证据后仍不达标",
                "_veto": "买入区深度不足",
            },
            "decision": {
                **dict(type1["decision"]),
                "decision_complete": True,
                "decision_basis": decision_basis,
                "score_lower_bound": 3.7,
                "score_upper_bound": 5.2,
                "veto_state": veto_state,
                "potentially_triggerable": False,
                "missing_dimensions": ["1d"],
            },
        }
    )
    scores.at[0, "type1"] = type1
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    exported = catalogue["companies"][0]["types"]["type1"]
    assert exported["status"] == status
    assert exported["score"] is None
    assert set(exported["sub_scores"]) == {"1a", "1b", "1c"}
    assert "1d" not in exported["sub_score_reasons"]
    assert exported["estimated_sub_scores"] == {"1d": 2.0}
    assert "未确认估算，不用于触发" in exported["estimated_sub_score_reasons"]["1d"]
    assert exported["reason"] == expected_reason
    assert exported["evidence_gap"] == "缺催化剂证据"
    assert exported["decision"]["score_lower_bound"] == 3.7
    assert exported["decision"]["score_upper_bound"] == 5.2


def test_mobile_catalogue_diagnostic_maximum_uses_only_complete_public_scores(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": "not_triggered",
            "total": 9.0,
            "sub_scores": {"1a": 9.0, "1b": 9.0, "1c": 9.0, "1d": 9.0},
            "decision": {
                **dict(type1["decision"]),
                "decision_complete": True,
                "decision_basis": "conservative_upper_bound",
                "score_lower_bound": 7.0,
                "score_upper_bound": 9.5,
                "potentially_triggerable": False,
                "missing_dimensions": ["1d"],
            },
        }
    )
    type2 = dict(scores.at[0, "type2"])
    type2.update(
        {
            "status": "observe",
            "total": 5.5,
            "sub_scores": {"2a": 5.5, "2b": 5.5, "2c": 5.5, "2d": 5.5},
            "decision": {
                **dict(type2["decision"]),
                "decision_complete": True,
                "decision_basis": "full_evidence",
                "score_lower_bound": 5.5,
                "score_upper_bound": 5.5,
                "potentially_triggerable": False,
                "missing_dimensions": [],
            },
        }
    )
    scores.at[0, "type1"] = type1
    scores.at[0, "type2"] = type2
    scores.at[0, "diagnostic_type"] = "type1"
    scores.at[0, "diagnostic_label"] = "1️⃣ 估值买入区"
    scores.at[0, "max_score"] = 9.0
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    company = catalogue["companies"][0]
    assert company["types"]["type1"]["score"] is None
    assert company["diagnostic_type"] == "type2"
    assert company["diagnostic_label"] == "2️⃣ 两热一冷"
    assert company["diagnostic_score"] == 5.5


def test_mobile_snapshot_exports_a_chinese_industry_label_and_keeps_the_enum_separate(monkeypatch):
    scores = _scores()
    scores.at[0, "code"] = "600519"
    scores.at[0, "name"] = "贵州茅台"
    scores.at[0, "industry"] = "ALCOHOL"
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-20",
        data_timestamp_utc="2026-07-20T08:30:00Z",
        analysis_quality={"ok": True},
    )

    company = catalogue["companies"][0]
    assert company["industry"] == "酿酒行业"
    assert company["industry_code"] == "ALCOHOL"


def test_mobile_snapshot_keeps_conditional_candidates_out_of_buy_signals(monkeypatch):
    scores = _scores()
    row = scores.iloc[0].copy()
    payload = dict(row["type6"])
    payload.update(
        {
            "status": "conditional",
            "triggered": False,
            "total": 7.1,
            "sub_scores": {"6a": 8.0, "6b": 8.0, "6c": 6.0, "6d": 7.0, "6e": 9.0},
            "reasons": {
                "6a": "产业高速增长",
                "6b": "研发与经营数据",
                "6c": "经营效率与现金流数据",
                "6d": "利润连续改善",
                "6e": "下单前确认仓位",
                "_condition": "须确认实际仓位符合建议上限",
                "_status": "conditional",
                "_applicable": "yes",
                "_evidence": "complete",
            },
            "veto": False,
            "applicable": True,
            "evidence_complete": True,
            "decision": {
                "schema_version": 1,
                "model_id": "buy-decision-bounds-v1",
                "decision_complete": False,
                "decision_basis": "action_condition",
                "score_lower_bound": 6.2,
                "score_upper_bound": 7.7,
                "veto_state": "none",
                "potentially_triggerable": True,
                "missing_dimensions": ["6e"],
            },
        }
    )
    scores.at[0, "type6"] = payload
    scores.at[0, "buy_types"] = []
    scores.at[0, "primary_type"] = None
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    assert catalogue["companies"][0]["buy_types"] == []
    assert catalogue["companies"][0]["conditional_types"] == ["type6"]
    assert catalogue["companies"][0]["types"]["type6"]["reason"] == "须确认实际仓位符合建议上限"
    assert catalogue["companies"][0]["types"]["type6"]["action_required"] == "position_confirmation"
    assert catalogue["companies"][0]["types"]["type6"]["position_guidance"] == {
        "recommendation": "下单前确认仓位",
        "hard_caps": "硬上限：单票不超过5%，此类组合不超过15%",
        "worst_case_loss": "请按最坏归零情景核对组合损失",
    }
    assert "6e" not in catalogue["companies"][0]["types"]["type6"].get("estimated_sub_scores", {})
    assert signals["triggered_company_count"] == 0
    assert signals["conditional_company_count"] == 1
    assert signals["conditional_only_company_count"] == 1
    assert signals["pending_company_count"] == 1
    assert signals["visible_candidate_company_count"] == 1
    assert signals["candidate_detail_count"] == 1
    assert "valuation" not in signals["signals"][0]
    assert "bear_case" not in signals["signals"][0]
    assert signals["signals"][0]["type_details"]["type6"]["reasons"] == {
        "_condition": "须确认实际仓位符合建议上限",
        "6a": "产业高速增长",
        "6b": "研发与经营数据",
        "6c": "经营效率与现金流数据",
        "6d": "利润连续改善",
        "6e": "下单前确认仓位",
    }
    assert "高风险早期/困境型" in signals["signals"][0]["detail_text"]
    assert "须确认实际仓位符合建议上限" in signals["signals"][0]["detail_text"]


def test_mobile_snapshot_keeps_unresolved_possible_candidates_visible_without_calling_them_signals():
    manifest, catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        _scores(),
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    company = catalogue["companies"][0]
    assert company["buy_types"] == []
    assert company["conditional_types"] == []
    assert company["pending_types"] == ["type1", "type2"]
    for type_key in company["pending_types"]:
        decision = company["types"][type_key]["decision"]
        assert decision["decision_complete"] is False
        assert decision["decision_basis"] == "unresolved_missing_evidence"
        assert decision["potentially_triggerable"] is True
    # Keep the legacy detail-array contract for installed 11.2 clients.  The
    # all-company catalogue is the complete visibility ledger used by 11.3.
    assert signals["signals"] == []
    assert signals["candidate_detail_count"] == 0
    assert signals["pending_company_count"] == 1
    assert manifest["summary"]["visible_candidate_company_count"] == 1


def test_mobile_snapshot_rejects_a_tampered_public_decision_even_if_upstream_validation_is_bypassed(monkeypatch):
    scores = _scores()
    payload = dict(scores.at[0, "type1"])
    decision = dict(payload["decision"])
    decision["score_upper_bound"] = 11.0
    payload["decision"] = decision
    scores.at[0, "type1"] = payload
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    with pytest.raises(mobile_snapshot.MobileSnapshotError, match="type1 decision contract"):
        mobile_snapshot.build_mobile_snapshot(
            scores,
            market_as_of="2026-07-17",
            data_timestamp_utc="2026-07-17T08:20:00+00:00",
            analysis_quality={"ok": True},
        )


def test_mobile_snapshot_counts_a_mixed_trigger_and_conditional_company_in_both_totals(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": "triggered",
            "triggered": True,
            "total": 7.2,
            "reasons": {"_status": "triggered", "_applicable": "yes", "_evidence": "complete"},
        }
    )
    type6 = dict(scores.at[0, "type6"])
    type6.update(
        {
            "status": "conditional",
            "triggered": False,
            "total": 7.1,
            "reasons": {
                "_condition": "须确认实际仓位符合建议上限",
                "_status": "conditional",
                "_applicable": "yes",
                "_evidence": "complete",
            },
        }
    )
    scores.at[0, "type1"] = type1
    scores.at[0, "type6"] = type6
    scores.at[0, "buy_types"] = ["type1"]
    scores.at[0, "primary_type"] = "type1"
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    manifest, _catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    assert manifest["summary"]["triggered_company_count"] == 1
    assert manifest["summary"]["conditional_company_count"] == 1
    assert manifest["summary"]["conditional_only_company_count"] == 0
    assert signals["triggered_company_count"] == 1
    assert signals["conditional_company_count"] == 1
    assert signals["conditional_only_company_count"] == 0


def test_mobile_snapshot_is_deterministic_for_the_same_validated_generation():
    kwargs = {
        "market_as_of": "2026-07-17",
        "data_timestamp_utc": "2026-07-17T08:20:00+00:00",
        "analysis_quality": {"ok": True},
    }

    first = mobile_snapshot.build_mobile_snapshot(_scores(), **kwargs)
    second = mobile_snapshot.build_mobile_snapshot(_scores(), **kwargs)

    assert first == second
    assert first[0]["generated_at_utc"] == "2026-07-17T08:20:00+00:00"


def test_mobile_snapshot_keeps_not_applicable_scope_and_weakest_public_reason(monkeypatch):
    scores = _scores()
    type3 = dict(scores.at[0, "type3"])
    type3["status"] = "not_applicable"
    type3["triggered"] = False
    type3["reasons"] = {
        **dict(type3["reasons"]),
        "_scope": "金融机构不适用该增长模板",
        "_status": "not_applicable",
    }
    scores.at[0, "type3"] = type3
    type4 = dict(scores.at[0, "type4"])
    type4["reasons"] = {
        **{key: value for key, value in type4["reasons"].items() if not str(key).startswith("_")},
        "4a": "可持续市场空间不足",
    }
    type4["sub_scores"] = {
        **{key: 10.0 for key in type4["sub_scores"]},
        "4a": 1.0,
    }
    scores.at[0, "type4"] = type4
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    types = catalogue["companies"][0]["types"]
    assert types["type3"]["reason"] == "金融机构不适用该增长型"
    assert types["type3"]["score"] is None
    assert types["type4"]["reason"] == "可持续市场空间不足"


@pytest.mark.parametrize(
    ("type_key", "status", "legacy_total"),
    [
        ("type1", "insufficient_evidence", 0.0),
        ("type2", "insufficient_evidence", 0.9),
        ("type3", "not_applicable", 0.0),
    ],
)
def test_mobile_snapshot_never_publishes_placeholder_scores_for_scoreless_states(
    monkeypatch, type_key, status, legacy_total
):
    scores = _scores()
    payload = dict(scores.at[0, type_key])
    payload.update(
        {
            "status": status,
            "triggered": False,
            "total": legacy_total,
            "reasons": {
                **dict(payload["reasons"]),
                "_status": status,
                "_evidence": "incomplete" if status == "insufficient_evidence" else "complete",
            },
        }
    )
    scores.at[0, type_key] = payload
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    exported = catalogue["companies"][0]["types"][type_key]
    assert exported["status"] == status
    assert exported["score"] is None


def test_mobile_candidate_detail_omits_score_and_subscores_for_scoreless_frameworks(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update(
        {
            "status": "triggered",
            "triggered": True,
            "total": 7.2,
            "reasons": {
                **dict(type1["reasons"]),
                "_status": "triggered",
                "_evidence": "complete",
            },
        }
    )
    type2 = dict(scores.at[0, "type2"])
    type2.update(
        {
            "status": "insufficient_evidence",
            "triggered": False,
            "total": 0.9,
            "reasons": {
                **dict(type2["reasons"]),
                "_status": "insufficient_evidence",
                "_evidence": "incomplete",
            },
        }
    )
    scores.at[0, "type1"] = type1
    scores.at[0, "type2"] = type2
    scores.at[0, "buy_types"] = ["type1"]
    scores.at[0, "primary_type"] = "type1"
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, _catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    exported = signals["signals"][0]["type_details"]["type2"]
    assert exported["score"] is None
    assert exported["sub_scores"] == {}
    assert "资料不足，0.9分" not in signals["signals"][0]["detail_text"]


def test_mobile_snapshot_hides_legacy_model_identifiers_from_all_public_text(monkeypatch):
    scores = _scores()
    type4 = dict(scores.at[0, "type4"])
    type4.update({"status": "triggered", "triggered": True, "total": 7.1})
    type4["sub_scores"] = {"4a": 6.0, "4b": 6.0, "4c": 6.0, "4d": 6.0, "4e": 6.0, "4f": 6.0}
    type4["reasons"] = {
        "_condition": "证据:patch6-observable",
        "4a": "model_id=patch6-type7-quality-equity-v7",
        "4b": "schema_version=7",
        "4c": "derived_proxy",
        "4d": "reported_formula",
        "4e": "financial_fade_horizon_not_tam_or_penetration_proof",
        "4f": "(normalised_roe - g) / (cost_of_equity - g)",
    }
    scores.at[0, "type4"] = type4
    scores.at[0, "buy_types"] = ["type4"]
    scores.at[0, "primary_type"] = "type4"
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _scores: [])

    _manifest, catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-20",
        data_timestamp_utc="2026-07-20T08:30:00Z",
        analysis_quality={"ok": True},
    )

    def _string_values(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from _string_values(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from _string_values(nested)

    # Public contract keys such as ``schema_version`` are machine-readable and
    # intentionally stable.  Only reject internal identifiers when they leak
    # into text that a person can actually see.
    public_text = " ".join(_string_values({"catalogue": catalogue, "signals": signals}))
    assert "patch6" not in public_text.casefold()
    assert "observable" not in public_text.casefold()
    assert "model=" not in public_text.casefold()
    assert "model_id" not in public_text.casefold()
    assert "schema_version" not in public_text.casefold()
    assert "derived_proxy" not in public_text.casefold()
    assert "reported_formula" not in public_text.casefold()
    assert "financial_fade_horizon" not in public_text.casefold()
    assert "normalised_roe" not in public_text.casefold()
    assert catalogue["companies"][0]["types"]["type4"]["reason"] == "可核验的财务与行业数据"


def test_mobile_snapshot_truncates_reasons_to_the_android_utf16_contract(monkeypatch):
    scores = _scores()
    type1 = dict(scores.at[0, "type1"])
    type1.update({"status": "triggered", "triggered": True, "total": 7.1})
    type1["reasons"] = {"_condition": "😀" + "说明" * 120}
    scores.at[0, "type1"] = type1
    scores.at[0, "buy_types"] = ["type1"]
    scores.at[0, "primary_type"] = "type1"
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _scores: [])

    _manifest, catalogue, signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-20",
        data_timestamp_utc="2026-07-20T08:30:00Z",
        analysis_quality={"ok": True},
    )

    compact_reason = catalogue["companies"][0]["types"]["type1"]["reason"]
    detail_reason = signals["signals"][0]["type_details"]["type1"]["reasons"]["_condition"]
    for reason in (compact_reason, detail_reason):
        assert len(reason.encode("utf-16-le")) // 2 <= mobile_snapshot.MAX_PUBLIC_REASON_UTF16_UNITS
        assert reason.endswith("…")


@pytest.mark.parametrize(
    ("limit_name", "message"),
    [
        ("MAX_UNCOMPRESSED_ASSET_BYTES", "uncompressed limit"),
        ("MAX_COMPRESSED_ASSET_BYTES", "download limit"),
    ],
)
def test_mobile_snapshot_refuses_payloads_the_android_client_cannot_open(tmp_path, monkeypatch, limit_name, message):
    monkeypatch.setattr(mobile_snapshot, limit_name, 1)

    with pytest.raises(mobile_snapshot.MobileSnapshotError, match=message):
        mobile_snapshot.write_mobile_snapshot(
            tmp_path,
            _scores(),
            market_as_of="2026-07-17",
            data_timestamp_utc="2026-07-17T08:20:00+00:00",
            analysis_quality={"ok": True},
        )

    assert list(tmp_path.iterdir()) == []


def test_five_thousand_company_catalogue_retains_one_megabyte_of_android_headroom(monkeypatch):
    base = _scores().iloc[0].to_dict()
    scores = pd.DataFrame(
        [{**base, "code": f"{600_000 + offset:06d}", "name": f"容量样本{offset}"} for offset in range(5_000)]
    )
    monkeypatch.setattr(mobile_snapshot, "validate_screening_result", lambda _frame: [])

    _manifest, catalogue, _signals = mobile_snapshot.build_mobile_snapshot(
        scores,
        market_as_of="2026-07-17",
        data_timestamp_utc="2026-07-17T08:20:00+00:00",
        analysis_quality={"ok": True},
    )

    raw = json.dumps(
        catalogue,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(raw) <= mobile_snapshot.MAX_UNCOMPRESSED_ASSET_BYTES - 1_000_000
    assert all("triggered" not in state for company in catalogue["companies"] for state in company["types"].values())


def test_mobile_snapshot_writer_refuses_to_mix_with_an_existing_generation(tmp_path):
    output = tmp_path / "mobile"
    output.mkdir()
    marker = output / "old-generation.json"
    marker.write_text("old", encoding="utf-8")

    with pytest.raises(mobile_snapshot.MobileSnapshotError, match="absent or empty"):
        mobile_snapshot.write_mobile_snapshot(
            output,
            _scores(),
            market_as_of="2026-07-17",
            data_timestamp_utc="2026-07-17T08:20:00+00:00",
            analysis_quality={"ok": True},
        )

    assert marker.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    ("quality", "message"),
    [({}, "analysis quality gate"), ({"ok": False}, "analysis quality gate")],
)
def test_mobile_snapshot_rejects_unpromoted_analysis(quality, message):
    with pytest.raises(mobile_snapshot.MobileSnapshotError, match=message):
        mobile_snapshot.build_mobile_snapshot(
            _scores(),
            market_as_of="2026-07-17",
            data_timestamp_utc="2026-07-17T08:20:00+00:00",
            analysis_quality=quality,
        )
