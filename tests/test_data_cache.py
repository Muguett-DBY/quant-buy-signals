from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from decimal import Decimal
from threading import Thread

import pandas as pd
import pytest

import data.cache as cache_module
from data.cache import Cache, CacheError, SafeCacheConflict, SafeFileCache


def test_sqlite_cache_honors_per_entry_ttl_and_round_trips_types(tmp_path, monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])
    cache = Cache(str(tmp_path / "cache.sqlite"), ttl=100)
    value = {
        "decimal": Decimal("12.340"),
        "date": date(2026, 7, 15),
        "datetime": datetime(2026, 7, 15, 12, 30, 1),
        "bytes": b"safe",
        "tuple": (1, "x"),
        "set": {"a", "b"},
    }
    cache.set("short", value, ttl=1)
    assert cache.get("short") == value
    now[0] = 1_001.0
    assert cache.get("short") is None
    cache.close()


def test_sqlite_cache_does_not_silently_stringify_or_swallow_corruption(tmp_path):
    cache = Cache(str(tmp_path / "cache.sqlite"))
    with pytest.raises(CacheError, match="unsupported cache value type"):
        cache.set("bad", object())

    cache._conn.execute(
        "INSERT OR REPLACE INTO cache(key,value,updated_at,expires_at) VALUES(?,?,?,?)",
        ("corrupt", "not-json", 0, 10**10),
    )
    cache._conn.commit()
    with pytest.raises(CacheError, match="cannot read cache key"):
        cache.get("corrupt")
    cache.close()


def _sample_snapshot():
    return {
        "quotes": pd.DataFrame(
            [
                {"code": "000001", "name": "平安银行", "price": 10.5},
                {"code": "600519", "name": "贵州茅台", "price": float("nan")},
            ]
        ),
        "fin_map": {
            "000001": {"cashflow": [{"REPORT_DATE": "2025-12-31", "value": 1}]},
            "600519": {"cashflow": []},
        },
    }


def test_safe_file_cache_is_single_generation_versioned_and_checksummed(tmp_path):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=7, ttl=60)
    metadata = cache.save(_sample_snapshot())

    assert path.is_file()
    assert metadata["schema_version"] == 7
    assert metadata["counts"] == {"quotes": 2, "fin_map": 2}
    loaded = cache.load(expected_counts={"quotes": 2, "fin_map": 2})
    assert loaded.hit, loaded.reason
    pd.testing.assert_frame_equal(loaded.value["quotes"], _sample_snapshot()["quotes"], check_dtype=False)
    assert loaded.value["fin_map"] == _sample_snapshot()["fin_map"]


def test_safe_file_cache_uses_indexed_type_decode_without_rescanning_primitive_tree(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=7)
    cache.save(_sample_snapshot())
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    assert envelope["type_paths"] == [["quotes"]]

    def fail_legacy_scan(_value):
        raise AssertionError("indexed cache unexpectedly used the recursive legacy decoder")

    monkeypatch.setattr(cache_module, "_decode_json_value", fail_legacy_scan)
    loaded = cache.load()
    assert loaded.hit, loaded.reason
    assert loaded.value["fin_map"] == _sample_snapshot()["fin_map"]


def test_safe_file_cache_integrity_failure_is_an_explained_miss(tmp_path):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=1)
    cache.save(_sample_snapshot())
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["payload"]["fin_map"]["000001"]["cashflow"][0]["value"] = 999
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False)

    loaded = cache.load()
    assert not loaded.hit
    assert loaded.reason == "hash_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [("created_at", "tampered"), ("expires_at", float("nan"))],
)
def test_safe_file_cache_metadata_is_finite_ordered_and_checksummed(tmp_path, field, value):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=1)
    cache.save(_sample_snapshot())
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope[field] = value
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False)

    loaded = cache.load(allow_expired=True)

    assert not loaded.hit
    assert loaded.reason in {"invalid_created_at", "invalid_expiry", "invalid_json", "metadata_hash_mismatch"}
    assert loaded.metadata["artifact_sha256"]


def test_safe_file_cache_rejects_incorrect_declared_counts_before_write(tmp_path):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path)
    with pytest.raises(ValueError, match="do not match payload"):
        cache.save(_sample_snapshot(), counts={"quotes": 1, "fin_map": 2})
    assert not path.exists()


def test_safe_file_cache_schema_expiry_and_expected_count_are_misses(tmp_path, monkeypatch):
    now = [2_000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])
    path = tmp_path / "snapshot.json.gz"
    SafeFileCache(path, schema_version=2, ttl=5).save(_sample_snapshot())

    assert SafeFileCache(path, schema_version=3).load().reason == "schema_version_mismatch"
    assert SafeFileCache(path, schema_version=2).load(expected_counts={"quotes": 1}).reason == "expected_count_mismatch"
    now[0] = 2_005.0
    assert SafeFileCache(path, schema_version=2).load().reason == "expired"


def test_safe_file_cache_concurrent_writes_leave_one_complete_generation(tmp_path):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=1)
    snapshots = [
        {"quotes": pd.DataFrame([{"code": f"{index:06d}"}]), "fin_map": {str(index): {}}} for index in range(8)
    ]
    threads = [Thread(target=cache.save, args=(snapshot,)) for snapshot in snapshots]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    loaded = cache.load()
    assert loaded.hit, loaded.reason
    assert len(loaded.value["quotes"]) == 1
    assert len(loaded.value["fin_map"]) == 1


def test_safe_file_cache_compare_and_swap_uses_the_ordinary_writer_lock(tmp_path):
    path = tmp_path / "snapshot.json.gz"
    cache = SafeFileCache(path, schema_version=1)
    original = _sample_snapshot()
    first = cache.save(original)

    other_writer = _sample_snapshot()
    other_writer["fin_map"]["000001"]["writer"] = "other"
    cache.save(other_writer)

    candidate = _sample_snapshot()
    candidate["fin_map"]["000001"]["writer"] = "candidate"
    with pytest.raises(SafeCacheConflict, match="active payload changed"):
        cache.compare_and_swap(
            candidate,
            expected_payload_sha256=first["payload_sha256"],
        )

    loaded = cache.load(allow_expired=True)
    assert loaded.hit
    assert loaded.value["fin_map"]["000001"]["writer"] == "other"

    with pytest.raises(SafeCacheConflict, match="before artifact capture"):
        cache.read_bytes_if_payload(first["payload_sha256"])
    captured = cache.read_bytes_if_payload(loaded.metadata["payload_sha256"])
    assert captured == path.read_bytes()
