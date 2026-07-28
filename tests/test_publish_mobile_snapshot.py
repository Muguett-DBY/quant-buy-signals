from copy import deepcopy
from functools import lru_cache
import hashlib
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.buy_screener import screen_all_types
from data.market_coldness import (
    EASTMONEY_CLIST_ENDPOINT,
    EASTMONEY_SOURCE,
)
from engine.market_coldness import MARKET_COLDNESS_MODEL_ID
from tools import publish_mobile_snapshot as publisher
from tools.run_full_audit import (
    _canonical_market_coldness_json,
    _replay_market_coldness_reference_artifact,
)

_prepare_quality_history_evidence = publisher._prepare_quality_history_evidence


@pytest.fixture(autouse=True)
def _published_source_commit(monkeypatch):
    """Keep publication tests independent of the developer worktree state."""

    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(
        publisher,
        "archive_market_coldness_session_snapshot",
        lambda snapshot, _session: snapshot,
    )
    monkeypatch.setattr(
        publisher,
        "_prepare_quality_history_evidence",
        lambda codes, _as_of: (
            {},
            {
                "requested_companies": len(codes),
                "reused_companies": 0,
                "network_tranche_companies": 0,
                "returned_companies": 0,
                "available_companies": 0,
                "remaining_companies": len(codes),
            },
        ),
    )


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


def _snapshot(source="cache"):
    return SimpleNamespace(
        source=source,
        eligible_codes=("000001",),
        analysis_quotes=pd.DataFrame(),
        analysis_financials={"000001": {}},
        quotes=pd.DataFrame(),
        financials={"000001": {}},
        data_timestamp=1_784_297_200.0,
        retrieved_at=1_784_297_210.0,
        baseline_timestamp=1_784_297_000.0,
        baseline_payload_sha256="b" * 64,
        validation={"trading_source_trade_dates": ["2026-07-17"]},
    )


def _after_close(monkeypatch):
    monkeypatch.setattr(
        publisher,
        "_shanghai_now",
        lambda: datetime(2026, 7, 20, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


@lru_cache(maxsize=2)
def _market_coldness_fixture(as_of_session="2026-07-17"):
    codes = tuple(f"{index:06d}" for index in range(1, 1_001))
    retrieved_at = f"{as_of_session}T08:20:00Z"
    artifact = {
        "schema_version": 2,
        "model_id": MARKET_COLDNESS_MODEL_ID,
        "source": EASTMONEY_SOURCE,
        "source_url": EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "listed_codes": list(codes),
        "source_record_count": len(codes),
        "records": [
            [
                code,
                "2020-01-01",
                round(-30.0 + (index % 101) * 0.5, 2),
                round(-40.0 + (index % 121) * 0.6, 2),
                round(0.25 + (index % 80) * 0.1, 2),
                round(0.4 + (index % 40) * 0.05, 2),
                int(datetime.fromisoformat(f"{as_of_session}T07:34:00+00:00").timestamp()),
            ]
            for index, code in enumerate(codes)
        ],
    }
    replay = _replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=("000001",),
        as_of_session=as_of_session,
    )
    evidence = replay["eligible_evidence"]
    status = {
        "available": True,
        "evidence_available": True,
        "evidence_reason": "available",
        "model_id": MARKET_COLDNESS_MODEL_ID,
        "source": EASTMONEY_SOURCE,
        "source_url": EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "reference_artifact_sha256": hashlib.sha256(_canonical_market_coldness_json(artifact)).hexdigest(),
        "full_listed_evidence_count": len(replay["full_evidence"]),
        "eligible_evidence_count": 1,
        "eligible_evidence_coverage": 1.0,
        "eligible_applicable_count": 1,
        "eligible_applicable_evidence_coverage": 1.0,
        "eligible_not_applicable_count": 0,
        "eligible_not_applicable_codes_by_reason": {
            "listed_in_current_year": [],
            "listing_history_lt_120_days": [],
        },
        "eligible_unscored_data_gap_count": 0,
        "eligible_unscored_data_gap_codes_by_reason": {},
    }
    return artifact, evidence, status


def _market_coldness_record(code="000001", as_of_session="2026-07-17"):
    return deepcopy(_market_coldness_fixture(as_of_session)[1][code])


def _market_coldness_status(as_of_session="2026-07-17"):
    return deepcopy(_market_coldness_fixture(as_of_session)[2])


def _valid_market_coldness_loader(
    *_args,
    reference_artifact_out=None,
    archive_candidate_out=None,
    **_kwargs,
):
    artifact, evidence, status = deepcopy(_market_coldness_fixture())
    if reference_artifact_out is not None:
        reference_artifact_out.update(artifact)
    if archive_candidate_out is not None:
        archive_candidate_out.append(SimpleNamespace(available=True))
    return evidence, status


def test_mobile_publisher_console_manifest_is_safe_on_windows_cp1252(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        publisher,
        "publish_mobile_snapshot",
        lambda **_kwargs: {"status": "发布成功", "company": "贵州茅台"},
    )

    assert publisher.main(["--output-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    output.encode("cp1252")
    assert "\\u53d1\\u5e03\\u6210\\u529f" in output
    assert "\\u8d35\\u5dde\\u8305\\u53f0" in output


def test_mobile_source_commit_rejects_an_invalid_github_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "not-a-commit")

    with pytest.raises(RuntimeError, match="source Git commit is invalid"):
        publisher._source_commit()


def test_mobile_source_commit_rejects_a_dirty_local_worktree(monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(publisher.shutil, "which", lambda _name: "git")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=" M engine/audit.py\n?? unsigned-output.json\n")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="requires a clean Git worktree"):
        publisher._source_commit()

    assert [call[0] for call in calls] == [["git", "status", "--porcelain", "--untracked-files=all"]]
    assert calls[0][1]["check"] is True
    assert calls[0][1]["capture_output"] is True


def test_mobile_source_commit_uses_head_only_after_a_clean_local_worktree(monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(publisher.shutil, "which", lambda _name: "git")
    calls = []
    outputs = iter(("", "B" * 40 + "\n"))

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=next(outputs))

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)

    assert publisher._source_commit() == "b" * 40
    assert [call[0] for call in calls] == [
        ["git", "status", "--porcelain", "--untracked-files=all"],
        ["git", "rev-parse", "HEAD"],
    ]
    assert all(call[1]["cwd"] == publisher.Path(publisher.__file__).resolve().parents[1] for call in calls)


def test_quality_history_backfill_reuses_all_cache_and_fetches_one_bounded_tranche(monkeypatch):
    codes = [f"{index:06d}" for index in range(1, 1_206)]
    monkeypatch.setattr(
        publisher,
        "load_quality_history_cache_batch",
        lambda requests: {
            request["code"]: {"available": True, "code": request["code"]}
            for request in requests[:5]
        },
    )
    captured = []

    def fetch(requests):
        captured.extend(requests)
        return {
            request["code"]: {"available": True, "code": request["code"]}
            for request in requests
        }

    monkeypatch.setattr(publisher, "fetch_quality_history_batch", fetch)

    evidence, status = _prepare_quality_history_evidence(codes, "2026-07-17")

    assert len(captured) == publisher._QUALITY_HISTORY_BACKFILL_LIMIT
    assert captured[0] == {"code": "000006", "as_of": "2026-07-17"}
    assert len(evidence) == 1_005
    assert status == {
        "requested_companies": 1_205,
        "reused_companies": 5,
        "network_tranche_companies": 1_000,
        "returned_companies": 1_005,
        "available_companies": 1_005,
        "remaining_companies": 200,
    }


def test_publish_mobile_snapshot_writes_only_a_quality_gated_generation(monkeypatch, tmp_path):
    snapshot = _snapshot()
    cache = SimpleNamespace(read_bytes_if_payload=lambda payload: b"verified-" + payload.encode("ascii"))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: cache)
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(publisher, "_snapshot_reporting_period_contract", lambda _snapshot: object())
    monkeypatch.setattr(publisher, "_comparison_quality", lambda _snapshot: {})
    monkeypatch.setattr(
        publisher,
        "_load_market_coldness_evidence",
        _valid_market_coldness_loader,
    )
    monkeypatch.setattr(
        publisher,
        "run_market_analysis",
        lambda *_args, **_kwargs: SimpleNamespace(
            scores=_scores(),
            issues=[],
            quality={"ok": True, "score_rows": 1},
            dcf_results={},
        ),
    )
    monkeypatch.setattr(
        publisher,
        "_analysis_coverage_summary",
        lambda _scores: {
            "goal_readiness": {gate: True for gate in publisher._MOBILE_STRUCTURAL_EVIDENCE_GATES} | {"ready": False}
        },
    )

    manifest = publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=False)

    assert manifest["market_as_of"] == "2026-07-17"
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / manifest["catalogue"]["filename"]).is_file()
    assert (tmp_path / manifest["signals"]["filename"]).is_file()
    assert manifest["provenance"]["snapshot_source"] == "cache"
    assert manifest["provenance"]["source_commit"] == "a" * 40
    assert manifest["provenance"]["screening_coverage"]["publication_readiness"]["artifact_integrity_ready"] is True


def test_mobile_screening_gate_publishes_honest_gaps_but_rejects_broken_records(monkeypatch):
    readiness = {
        "all_framework_payloads_present": True,
        "all_sub_scores_valid": True,
        "all_applicable_frameworks_evidence_complete": False,
        "all_incomplete_frameworks_explained": True,
        "all_quantitative_evidence_records_valid": True,
        "no_missing_quantitative_evidence": False,
        "no_partial_quantitative_evidence": False,
        "artifact_integrity_ready": True,
        "candidate_visibility_ready": True,
        "candidate_recall_ready": True,
        "ideal_zero_gap_ready": False,
        "ready": False,
    }
    monkeypatch.setattr(
        publisher,
        "_analysis_coverage_summary",
        lambda _scores: {"goal_readiness": dict(readiness), "quantitative_evidence_gap_examples": []},
    )

    published = publisher._mobile_screening_coverage(pd.DataFrame([{"code": "000001"}]))
    assert published["publication_readiness"] == {
        "artifact_integrity_ready": True,
        "candidate_visibility_ready": True,
        "candidate_recall_ready": True,
        "ideal_zero_gap_ready": False,
    }

    for gate in publisher._MOBILE_STRUCTURAL_EVIDENCE_GATES:
        broken = dict(readiness)
        broken[gate] = False
        monkeypatch.setattr(
            publisher,
            "_analysis_coverage_summary",
            lambda _scores, payload=broken: {"goal_readiness": payload},
        )
        with pytest.raises(RuntimeError, match=gate):
            publisher._mobile_screening_coverage(pd.DataFrame([{"code": "000001"}]))


def test_mobile_publication_keeps_existing_files_when_coldness_coverage_is_zero(monkeypatch, tmp_path):
    snapshot = _snapshot()
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-17",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    marker = tmp_path / "existing.txt"
    marker.write_text("last-known-good", encoding="utf-8")
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(publisher, "_snapshot_reporting_period_contract", lambda _snapshot: object())
    monkeypatch.setattr(publisher, "_comparison_quality", lambda _snapshot: {})
    monkeypatch.setattr(publisher, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, status))
    monkeypatch.setattr(
        publisher,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )

    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=False)

    assert marker.read_text(encoding="utf-8") == "last-known-good"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.txt"]


def test_mobile_publication_keeps_existing_files_when_declared_coldness_records_are_invalid(
    monkeypatch,
    tmp_path,
):
    snapshot = _snapshot()
    record = _market_coldness_record()
    record["components"].pop("raw_values")
    status = _market_coldness_status()
    marker = tmp_path / "existing.txt"
    marker.write_text("last-known-good", encoding="utf-8")
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(publisher, "_snapshot_reporting_period_contract", lambda _snapshot: object())
    monkeypatch.setattr(publisher, "_comparison_quality", lambda _snapshot: {})
    monkeypatch.setattr(
        publisher,
        "_load_market_coldness_evidence",
        lambda *_args, **_kwargs: ({"000001": record}, status),
    )
    monkeypatch.setattr(
        publisher,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )

    with pytest.raises(RuntimeError, match="component provenance is invalid"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=False)

    assert marker.read_text(encoding="utf-8") == "last-known-good"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.txt"]


def test_publish_mobile_snapshot_refuses_to_replace_daily_data_with_stale_refresh(monkeypatch, tmp_path):
    _after_close(monkeypatch)
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: _snapshot(source="cache"))

    with pytest.raises(RuntimeError, match="fresh market refresh did not complete"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=True)

    assert not list(tmp_path.iterdir())


def test_mobile_publication_refuses_an_old_trading_session_after_a_fresh_fetch(monkeypatch, tmp_path):
    _after_close(monkeypatch)
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: _snapshot(source="network"))
    monkeypatch.setattr(publisher, "_shanghai_today", lambda: "2026-07-18")

    with pytest.raises(RuntimeError, match="is not today's Shanghai session"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=True)

    assert not list(tmp_path.iterdir())


def test_mobile_publication_requires_one_validated_market_session():
    with pytest.raises(RuntimeError, match="unique validated trading session"):
        publisher._market_as_of(SimpleNamespace(validation={"trading_source_trade_dates": []}))
    with pytest.raises(RuntimeError, match="timestamp is invalid"):
        publisher._utc_timestamp(True)


def test_mobile_publication_refuses_a_manual_refresh_before_the_safe_close_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        publisher,
        "_shanghai_now",
        lambda: datetime(2026, 7, 20, 16, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="16:15"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=True)

    assert not list(tmp_path.iterdir())


def test_mobile_publication_refuses_intraday_quotes_replayed_after_close(monkeypatch, tmp_path):
    _after_close(monkeypatch)
    snapshot = _snapshot(source="network")
    snapshot.analysis_quotes = pd.DataFrame(
        [
            {
                "code": "000001",
                "market": "SH",
                "quote_status": "trading",
                "source_trade_date": "2026-07-20",
                "quote_tick_time": "10:30:00",
            }
        ]
    )
    snapshot.validation["trading_source_trade_dates"] = ["2026-07-20"]
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)

    with pytest.raises(RuntimeError, match="post-close quote coverage"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=True)

    assert not list(tmp_path.iterdir())


def test_post_close_gate_uses_the_complete_eligible_universe_as_its_denominator():
    codes = tuple(f"{index:06d}" for index in range(100))

    def snapshot(trading_count):
        return SimpleNamespace(
            eligible_codes=codes,
            analysis_quotes=pd.DataFrame(
                [
                    {
                        "code": code,
                        "market": "SZ",
                        "quote_status": "trading" if index < trading_count else "suspended_or_no_trade",
                        "source_trade_date": "2026-07-20",
                        "quote_tick_time": "15:00:00",
                    }
                    for index, code in enumerate(codes)
                ]
            ),
        )

    assert publisher._require_post_close_quotes(snapshot(99), "2026-07-20") == 0.99
    with pytest.raises(RuntimeError, match="trading quote coverage 98.0%"):
        publisher._require_post_close_quotes(snapshot(98), "2026-07-20")
