import copy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import evidence_bundle as eb


SOURCE_COMMIT = "a" * 40
AS_OF = "2026-08-10"


def _cache_payload(label: str) -> bytes:
    return gzip.compress(json.dumps({"label": label}, sort_keys=True).encode("utf-8"), mtime=0)


def _seed_cache(cache_root: Path) -> dict[str, bytes]:
    payloads = {
        "commodity_cycle/commodity-cycle-sina-v2_RB0.json.gz": _cache_payload("commodity"),
        "dividend_history/000001.json.gz": _cache_payload("dividend"),
        "exchange_financials/sse-xbrl-test.json.gz": _cache_payload("exchange"),
        "growth_evidence/type3-segment-growth-v1_000001_20260810.json.gz": _cache_payload("segment"),
        "industry_history/shenwan-industry-history-v1_cninfo_000001_20260810.json.gz": _cache_payload("industry"),
        "investor_relations/cninfo-ir-000001.json.gz": _cache_payload("ir"),
        "market_coldness/eastmoney_sh_sz_a.json.gz": _cache_payload("coldness"),
        "quality_history/type7-market-history-v1_000002_20260810.json.gz": _cache_payload("quality"),
        "research_reports/type7-research-report-content-v4_000003_20260810.json.gz": _cache_payload("research"),
    }
    for relative, payload in payloads.items():
        path = cache_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.with_suffix(path.suffix + ".lock").write_text("not evidence", encoding="utf-8")
    return payloads


def test_bundle_is_deterministic_content_addressed_and_excludes_lock_files(tmp_path):
    cache_root = tmp_path / "cache"
    payloads = _seed_cache(cache_root)

    first_bundle, first_pointer, first = eb.bundle_evidence(
        cache_root=cache_root,
        output_dir=tmp_path / "first",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )
    second_bundle, second_pointer, second = eb.bundle_evidence(
        cache_root=cache_root,
        output_dir=tmp_path / "second",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )

    assert first_bundle.name == second_bundle.name
    assert first_bundle.read_bytes() == second_bundle.read_bytes()
    assert first_pointer.read_bytes() == second_pointer.read_bytes()
    assert first == second
    assert first_bundle.name == f"evidence-cache-{hashlib.sha256(first_bundle.read_bytes()).hexdigest()}.zip"
    assert first["source_commit"] == SOURCE_COMMIT
    assert first["as_of"] == AS_OF
    assert first["bundle"]["member_count"] == len(payloads)
    assert [member["path"] for member in first["members"]] == sorted(f"data/cache/{relative}" for relative in payloads)
    assert all(not member["path"].endswith(".lock") for member in first["members"])
    assert eb.verify_evidence_bundle(first_pointer, first_bundle) == first


def test_import_verifies_then_atomically_merges_only_whitelisted_cache_members(tmp_path):
    source_cache = tmp_path / "source-cache"
    payloads = _seed_cache(source_cache)
    bundle, pointer, _manifest = eb.bundle_evidence(
        cache_root=source_cache,
        output_dir=tmp_path / "bundle",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )
    target_cache = tmp_path / "target-cache"
    existing = target_cache / "quality_history" / "existing.json.gz"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(_cache_payload("existing"))

    summary = eb.import_evidence_bundle(
        pointer,
        bundle,
        cache_root=target_cache,
        expected_source_commit=SOURCE_COMMIT,
        expected_as_of=AS_OF,
    )

    assert summary["imported_members"] == len(payloads)
    assert existing.read_bytes() == _cache_payload("existing")
    for relative, payload in payloads.items():
        assert (target_cache / relative).read_bytes() == payload
    assert not list(target_cache.parent.glob(".evidence-import-*"))


def test_verify_rejects_archive_tampering_before_import(tmp_path):
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root)
    bundle, pointer, _manifest = eb.bundle_evidence(
        cache_root=cache_root,
        output_dir=tmp_path / "bundle",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )
    tampered_dir = tmp_path / "tampered"
    tampered_dir.mkdir()
    tampered = tampered_dir / bundle.name
    payload = bytearray(bundle.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    tampered.write_bytes(payload)

    with pytest.raises(eb.EvidenceBundleError, match="size or sha256"):
        eb.verify_evidence_bundle(pointer, tampered)


def test_fallback_bundle_preserves_the_restored_actions_cache(tmp_path):
    source = tmp_path / "source"
    payloads = _seed_cache(source)
    bundle, pointer, _ = eb.bundle_evidence(
        cache_root=source,
        output_dir=tmp_path / "bundle",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )
    target = tmp_path / "target"
    relative = next(name for name in payloads if name.startswith("quality_history/"))
    restored = target / relative
    restored.parent.mkdir(parents=True)
    restored.write_bytes(_cache_payload("newer validated Actions evidence"))
    result = eb.import_evidence_bundle(pointer, bundle, cache_root=target, preserve_existing=True)
    assert result["preserved_members"] == 1
    assert result["imported_members"] == len(payloads) - 1
    assert restored.read_bytes() == _cache_payload("newer validated Actions evidence")
    for name, payload in payloads.items():
        if name != relative:
            assert (target / name).read_bytes() == payload


def test_pointer_rejects_source_mismatch_and_path_traversal(tmp_path):
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root)
    _bundle, pointer, manifest = eb.bundle_evidence(
        cache_root=cache_root,
        output_dir=tmp_path / "bundle",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )

    with pytest.raises(eb.EvidenceBundleError, match="source_commit"):
        eb.resolve_pointer(pointer, expected_source_commit="b" * 40)

    unsafe = copy.deepcopy(manifest)
    unsafe["members"][0]["path"] = "data/cache/growth_evidence/../../outside.json.gz"
    unsafe_pointer = tmp_path / "unsafe-pointer.json"
    unsafe_pointer.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(eb.EvidenceBundleError, match="outside the whitelist"):
        eb.resolve_pointer(unsafe_pointer)


def test_resolve_command_needs_only_the_standard_library(tmp_path):
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root)
    bundle, pointer, _manifest = eb.bundle_evidence(
        cache_root=cache_root,
        output_dir=tmp_path / "bundle",
        as_of=AS_OF,
        source_commit=SOURCE_COMMIT,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "tools.evidence_bundle",
            "resolve",
            "--pointer",
            str(pointer),
            "--expected-source-commit",
            SOURCE_COMMIT,
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == bundle.name


def test_tdx_compatibility_entry_requires_an_explicit_date_and_uses_repo_root():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "_tdx_segment_backfill.py").read_text(encoding="utf-8")

    assert "Path(__file__).resolve().parents[1]" in source
    assert 'parser.add_argument("--as-of", required=True' in source
    assert '"2026-08-10"' not in source
    assert '"--segment-provider"' in source and '"tdx"' in source
