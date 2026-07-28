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
    assert re.fullmatch(r"catalog-[0-9a-f]{16}\.json\.gz", catalog_filename)
    assert re.fullmatch(r"signals-[0-9a-f]{16}\.json\.gz", signals_filename)
    assert re.fullmatch(r"manifest-[0-9a-f]{16}\.sig", signature_filename)
    generation = catalog_filename.removeprefix("catalog-").removesuffix(".json.gz")
    assert signals_filename == f"signals-{generation}.json.gz"
    assert signature_filename == f"manifest-{generation}.sig"
    assert manifest["signature"]["algorithm"] == "ECDSA_P256_SHA256"

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
    assert generation == hashlib.sha256(canonical_catalogue + b"\0" + canonical_signals).hexdigest()[:16]
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
        "decision_contract": True,
    }
    assert catalogue["capabilities"] == manifest["capabilities"]
    assert signals["capabilities"] == manifest["capabilities"]
    assert sum(catalogue["coverage"]["type1"].values()) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [catalog_filename, signals_filename, mobile_snapshot.MANIFEST_FILENAME]
    )


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
    assert set(type1["sub_scores"]) == {"1a", "1b", "1c", "1d"}
    assert all(0.0 <= float(value) <= 10.0 for value in type1["sub_scores"].values())
    assert set(type1["sub_score_reasons"]) == {"1a", "1b", "1c", "1d"}
    assert all(type1["sub_score_reasons"].values())
    assert types["type3"]["status"] == "not_applicable"
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
        "4a": "model_id=patch6-type7-quality-equity-v6",
        "4b": "schema_version=6",
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
