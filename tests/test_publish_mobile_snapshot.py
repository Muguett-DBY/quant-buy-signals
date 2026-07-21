from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.buy_screener import screen_all_types
from tools import publish_mobile_snapshot as publisher


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
        lambda: datetime(2026, 7, 20, 16, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def test_publish_mobile_snapshot_writes_only_a_quality_gated_generation(monkeypatch, tmp_path):
    snapshot = _snapshot()
    cache = SimpleNamespace(read_bytes_if_payload=lambda payload: b"verified-" + payload.encode("ascii"))
    monkeypatch.setattr(publisher, "audit_state_hashes", lambda: {"code_sha256": "a" * 64})
    monkeypatch.setattr(publisher, "SafeFileCache", lambda *_args, **_kwargs: cache)
    monkeypatch.setattr(publisher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(publisher, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(publisher, "_snapshot_reporting_period_contract", lambda _snapshot: object())
    monkeypatch.setattr(publisher, "_comparison_quality", lambda _snapshot: {})
    monkeypatch.setattr(
        publisher, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, {"available": True})
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

    manifest = publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=False)

    assert manifest["market_as_of"] == "2026-07-17"
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / manifest["catalogue"]["filename"]).is_file()
    assert (tmp_path / manifest["signals"]["filename"]).is_file()
    assert manifest["provenance"]["snapshot_source"] == "cache"


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


def test_mobile_publication_refuses_a_manual_refresh_before_four_pm(monkeypatch, tmp_path):
    monkeypatch.setattr(
        publisher,
        "_shanghai_now",
        lambda: datetime(2026, 7, 20, 15, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="16:00"):
        publisher.publish_mobile_snapshot(output_dir=tmp_path, refresh=True)

    assert not list(tmp_path.iterdir())


def test_mobile_publication_refuses_intraday_quotes_replayed_after_close(monkeypatch, tmp_path):
    _after_close(monkeypatch)
    snapshot = _snapshot(source="network")
    snapshot.analysis_quotes = pd.DataFrame(
        [
            {
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
