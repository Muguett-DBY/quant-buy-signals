import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "mobile_market_workflow_guard.ps1"
CALENDAR = ROOT / "tools" / "china_a_share_trading_calendar.json"
SOURCE_COMMIT = "a" * 40
TYPE_STATUSES = (
    "triggered",
    "conditional",
    "observe",
    "insufficient_evidence",
    "vetoed",
    "blocked",
    "not_triggered",
    "not_applicable",
)


def _pwsh() -> str:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is not installed on this test host")
    return executable


def _run_guard(
    tmp_path: Path,
    *,
    event: str,
    now_utc: str | None = None,
    manifest: Path | None = None,
    release: Path | None = None,
    archive: Path | None = None,
    android_source: Path | None = None,
    expected_source_commit: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = tmp_path / "guard-output.txt"
    command = [
        _pwsh(),
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(GUARD),
        "-EventName",
        event,
        "-CalendarPath",
        str(CALENDAR),
        "-OutputPath",
        str(output),
        "-MinimumCompanyCount",
        "12",
        "-MaximumCompanyCount",
        "100",
    ]
    if now_utc is not None:
        command.extend(("-NowUtc", now_utc))
    if manifest is not None:
        command.extend(("-ManifestPath", str(manifest)))
    if release is not None:
        command.extend(("-ReleaseDirectory", str(release)))
    if archive is not None:
        command.extend(("-ArchiveDirectory", str(archive)))
    if android_source is not None:
        command.extend(("-AndroidSourcePath", str(android_source)))
    if expected_source_commit is not None:
        command.extend(("-ExpectedSourceCommit", expected_source_commit))
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    values: dict[str, str] = {}
    if output.exists():
        for line in output.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            assert separator
            values[key] = value
    return result, values


def _signed_generation(
    tmp_path: Path,
    *,
    market_date: str = "2026-07-22",
    data_timestamp_utc: str | None = None,
    catalogue_product: str = "DS_DCF",
    corrupt_catalogue_gzip: bool = False,
    scalar_type_list: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    release = tmp_path / "release"
    release.mkdir()
    generation = "0123456789abcdef"
    catalogue = release / f"catalog-{generation}.json.gz"
    signals = release / f"signals-{generation}.json.gz"
    signature = release / f"manifest-{generation}.sig"
    company_count = 12
    quality = {
        "ok": True,
        "expected_companies": company_count,
        "score_raw_rows": company_count,
        "score_rows": company_count,
        "score_coverage": 1.0,
        "pipeline_issues": 0,
    }
    type_names = {f"type{number}": f"买入情况{number}" for number in range(1, 8)}
    coverage = {
        f"type{number}": {status: company_count if status == "not_triggered" else 0 for status in TYPE_STATUSES}
        for number in range(1, 8)
    }
    companies = []
    for offset in range(company_count):
        companies.append(
            {
                "code": f"{600000 + offset:06d}",
                "name": f"测试公司{offset}",
                "industry": "测试行业",
                "price": 10.0,
                "buy_types": [],
                "conditional_types": [],
                "types": {
                    f"type{number}": {"status": "not_triggered", "score": 5.0, "reason": ""} for number in range(1, 8)
                },
            }
        )
    if scalar_type_list is not None:
        companies[0][scalar_type_list] = "type1"
    timestamp = data_timestamp_utc or f"{market_date}T08:05:00+00:00"
    provenance = {"source_commit": SOURCE_COMMIT}
    shared = {
        "schema_version": 1,
        "product": catalogue_product,
        "market_as_of": market_date,
        "data_timestamp_utc": timestamp,
        "analysis_quality": quality,
        "provenance": provenance,
    }
    catalogue_payload = {
        **shared,
        "type_names": type_names,
        "coverage": coverage,
        "company_count": company_count,
        "companies": companies,
    }
    signals_payload = {
        **shared,
        "triggered_company_count": 0,
        "conditional_company_count": 0,
        "conditional_only_company_count": 0,
        "candidate_detail_count": 0,
        "signals": [],
    }
    catalogue_raw = json.dumps(catalogue_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    signals_raw = json.dumps(signals_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    catalogue.write_bytes(b"not-gzip" if corrupt_catalogue_gzip else gzip.compress(catalogue_raw, mtime=0))
    signals.write_bytes(gzip.compress(signals_raw, mtime=0))
    manifest = tmp_path / "manifest.json"
    payload = {
        **shared,
        "catalogue": {
            "filename": catalogue.name,
            "size": catalogue.stat().st_size,
            "sha256": hashlib.sha256(catalogue.read_bytes()).hexdigest(),
        },
        "signals": {
            "filename": signals.name,
            "size": signals.stat().st_size,
            "sha256": hashlib.sha256(signals.read_bytes()).hexdigest(),
        },
        "signature": {
            "filename": signature.name,
            "algorithm": "ECDSA_P256_SHA256",
        },
        "summary": {
            "company_count": company_count,
            "triggered_company_count": 0,
            "conditional_company_count": 0,
            "conditional_only_company_count": 0,
            "candidate_detail_count": 0,
            "type_coverage": coverage,
        },
    }
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    android_source = tmp_path / "MarketRepository.java"
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_MANIFEST": str(manifest),
            "TEST_SIGNATURE": str(signature),
            "TEST_ANDROID_SOURCE": str(android_source),
        }
    )
    signer = r"""
$ErrorActionPreference = 'Stop'
$curve = [Security.Cryptography.ECCurve]::CreateFromFriendlyName('nistP256')
$key = [Security.Cryptography.ECDsa]::Create($curve)
try {
  $publicKey = [Convert]::ToBase64String($key.ExportSubjectPublicKeyInfo())
  $signature = $key.SignData(
    [IO.File]::ReadAllBytes($env:TEST_MANIFEST),
    [Security.Cryptography.HashAlgorithmName]::SHA256,
    [Security.Cryptography.DSASignatureFormat]::Rfc3279DerSequence
  )
  [IO.File]::WriteAllBytes($env:TEST_SIGNATURE, $signature)
  [IO.File]::WriteAllText(
    $env:TEST_ANDROID_SOURCE,
    'private static final String MOBILE_SIGNING_PUBLIC_KEY_BASE64 = "' + $publicKey + '";',
    [Text.UTF8Encoding]::new($false)
  )
} finally {
  $key.Dispose()
}
"""
    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", signer],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    archive = tmp_path / "archive"
    archive.mkdir()
    shutil.copy2(manifest, archive / "manifest.json")
    shutil.copy2(signature, archive / signature.name)
    (archive / "SHA256SUMS.txt").write_text(
        f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  manifest.json\n"
        f"{hashlib.sha256(signature.read_bytes()).hexdigest()}  {signature.name}\n",
        encoding="ascii",
    )
    return manifest, release, archive, android_source


def test_calendar_pins_matching_official_sse_and_szse_2026_notices():
    calendar = json.loads(CALENDAR.read_text(encoding="utf-8"))

    assert calendar["schema_version"] == 1
    assert calendar["timezone"] == "Asia/Shanghai"
    year = calendar["years"]["2026"]
    assert year["published_on"] == "2025-12-22"
    assert {source["exchange"]: source["url"] for source in year["sources"]} == {
        "SSE": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
        "SZSE": "https://www.szse.cn/disclosure/notice/t20251222_618087.html",
    }
    assert [(period["start"], period["end"]) for period in year["closure_periods"]] == [
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ]
    guard_source = GUARD.read_text(encoding="utf-8")
    assert "[int]$MinimumCompanyCount = 4500" in guard_source
    assert "[int]$MaximumCompanyCount = 6500" in guard_source


def test_manual_dispatch_forces_post_close_refresh_without_reading_network(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="workflow_dispatch",
        now_utc="2026-07-22T08:05:00.0000000+00:00",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "manual_dispatch_forced"}


def test_manual_dispatch_before_close_is_a_successful_noop(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="workflow_dispatch",
        now_utc="2026-07-22T07:59:59.0000000+00:00",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {
        "should_run": "false",
        "reason": "manual_dispatch_before_post_close_window",
    }


def test_manual_dispatch_on_weekend_is_a_successful_noop(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="workflow_dispatch",
        now_utc="2026-07-25T08:17:00.0000000+00:00",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "false", "reason": "market_closed_weekend"}


@pytest.mark.parametrize(
    "now_utc",
    [
        "2026-01-01T08:17:00.0000000+00:00",
        "2026-02-16T08:17:00.0000000+00:00",
        "2026-04-06T08:17:00.0000000+00:00",
        "2026-05-01T08:17:00.0000000+00:00",
        "2026-06-19T08:17:00.0000000+00:00",
        "2026-09-25T08:17:00.0000000+00:00",
        "2026-10-01T08:17:00.0000000+00:00",
    ],
)
def test_scheduled_official_exchange_holidays_are_successful_noops(tmp_path, now_utc):
    result, decision = _run_guard(tmp_path, event="schedule", now_utc=now_utc)

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "false", "reason": "market_closed_exchange_notice"}


def test_scheduled_weekend_is_a_successful_noop(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-25T08:17:00.0000000+00:00",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "false", "reason": "market_closed_weekend"}


def test_valid_same_day_signed_generation_suppresses_later_recovery_crons(tmp_path):
    manifest, release, archive, android_source = _signed_generation(tmp_path)

    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T08:47:00.0000000+00:00",
        manifest=manifest,
        release=release,
        archive=archive,
        android_source=android_source,
        expected_source_commit=SOURCE_COMMIT,
    )

    assert result.returncode == 0, result.stderr
    assert decision == {
        "should_run": "false",
        "reason": "current_signed_generation_already_published",
    }


@pytest.mark.parametrize(
    ("generation_options", "expected_source_commit"),
    [
        ({"corrupt_catalogue_gzip": True}, SOURCE_COMMIT),
        ({"catalogue_product": "NOT_DS_DCF"}, SOURCE_COMMIT),
        ({"data_timestamp_utc": "2026-07-22T12:00:00+00:00"}, SOURCE_COMMIT),
        ({}, "b" * 40),
    ],
)
def test_signed_generation_must_match_the_android_payload_and_source_contract(
    tmp_path, generation_options, expected_source_commit
):
    manifest, release, archive, android_source = _signed_generation(tmp_path, **generation_options)

    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T09:17:00.0000000+00:00",
        manifest=manifest,
        release=release,
        archive=archive,
        android_source=android_source,
        expected_source_commit=expected_source_commit,
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "market_open"}
    assert "cannot suppress a refresh" in result.stdout + result.stderr


@pytest.mark.parametrize("property_name", ["buy_types", "conditional_types"])
def test_signed_generation_rejects_scalar_type_lists_that_android_cannot_parse(tmp_path, property_name):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        scalar_type_list=property_name,
    )

    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T09:17:00.0000000+00:00",
        manifest=manifest,
        release=release,
        archive=archive,
        android_source=android_source,
        expected_source_commit=SOURCE_COMMIT,
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "market_open"}
    assert "cannot suppress a refresh" in result.stdout + result.stderr


def test_live_generation_without_matching_archive_completion_marker_is_retried(tmp_path):
    manifest, release, archive, android_source = _signed_generation(tmp_path)
    (archive / "SHA256SUMS.txt").unlink()

    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T09:17:00.0000000+00:00",
        manifest=manifest,
        release=release,
        archive=archive,
        android_source=android_source,
        expected_source_commit=SOURCE_COMMIT,
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "market_open"}
    assert "cannot suppress a refresh" in result.stdout + result.stderr


@pytest.mark.parametrize("damage", ["catalogue", "signature", "manifest_date", "duplicate_key"])
def test_invalid_or_stale_published_generation_never_suppresses_refresh(tmp_path, damage):
    manifest, release, archive, android_source = _signed_generation(tmp_path)
    if damage == "catalogue":
        (release / "catalog-0123456789abcdef.json.gz").write_bytes(b"tampered")
    elif damage == "signature":
        signature = release / "manifest-0123456789abcdef.sig"
        payload = bytearray(signature.read_bytes())
        payload[-1] ^= 1
        signature.write_bytes(payload)
    elif damage == "manifest_date":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["market_as_of"] = "2026-07-21"
        manifest.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    else:
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            text.replace('{"analysis_quality"', '{"market_as_of":"2026-07-22","analysis_quality"'),
            encoding="utf-8",
        )

    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T09:17:00.0000000+00:00",
        manifest=manifest,
        release=release,
        archive=archive,
        android_source=android_source,
        expected_source_commit=SOURCE_COMMIT,
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "market_open"}
    assert "cannot suppress a refresh" in result.stdout + result.stderr


def test_missing_published_manifest_on_a_trading_day_runs_refresh(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2026-07-22T08:17:00.0000000+00:00",
        manifest=tmp_path / "missing-manifest.json",
        release=tmp_path / "missing-release",
        android_source=tmp_path / "missing-source.java",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "market_open"}


def test_unknown_calendar_year_runs_fail_closed_publisher_instead_of_silently_skipping(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="schedule",
        now_utc="2027-01-04T08:17:00.0000000+00:00",
        manifest=tmp_path / "missing-manifest.json",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "calendar_year_unavailable"}
