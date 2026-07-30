import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from data import mobile_snapshot


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "mobile_market_workflow_guard.ps1"
CALENDAR = ROOT / "tools" / "china_a_share_trading_calendar.json"
ANDROID_REPOSITORY = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "muguett" / "dsdcf" / "MarketRepository.java"
)
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
DECISION_MODEL_ID = "buy-decision-bounds-v1"


def _complete_decision(score: float = 5.0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_id": DECISION_MODEL_ID,
        "decision_complete": True,
        "decision_basis": "full_evidence",
        "score_lower_bound": score,
        "score_upper_bound": score,
        "veto_state": "none",
        "potentially_triggerable": False,
        "missing_dimensions": [],
    }


def test_readme_describes_a_closed_market_as_a_successful_noop():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "交易所休市时，本次运行会记录原因后成功结束且不发布新数据" in readme
    assert "遇到休市、来源未刷新" not in readme


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
    legacy_decision_contract: bool = False,
    pending_candidate: bool = False,
    pending_mismatch: bool = False,
    decision_damage: str | None = None,
    complete_veto_with_missing: bool = False,
    overlapping_candidate: bool = False,
    conditional_only_mismatch: bool = False,
    action_confirmation_mismatch: bool = False,
    omit_uncompressed_size: bool = False,
    uncompressed_size_mismatch: bool = False,
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
        types = {
            f"type{number}": {
                "status": "not_triggered",
                "score": 5.0,
                "reason": "",
                **({} if legacy_decision_contract else {"decision": _complete_decision()}),
            }
            for number in range(1, 8)
        }
        companies.append(
            {
                "code": f"{600000 + offset:06d}",
                "name": f"测试公司{offset}",
                "industry": "测试行业",
                "price": 10.0,
                "buy_types": [],
                "conditional_types": [],
                **({} if legacy_decision_contract else {"pending_types": []}),
                "types": types,
            }
        )
    triggered_company_count = 0
    conditional_company_count = 0
    conditional_only_company_count = 0
    action_confirmation_company_count = 0
    pending_company_count = 0
    visible_candidate_company_count = 0
    signal_rows: list[dict[str, str]] = []
    if overlapping_candidate:
        if legacy_decision_contract:
            raise ValueError("the overlap fixture requires the current decision contract")
        triggered_company_count = 1
        conditional_company_count = 1
        action_confirmation_company_count = 1
        visible_candidate_company_count = 1
        signal_rows = [{"code": companies[0]["code"], "detail_text": "已校验候选详情"}]
        companies[0]["buy_types"] = ["type1"]
        companies[0]["conditional_types"] = ["type6"]
        companies[0]["types"]["type1"]["status"] = "triggered"
        companies[0]["types"]["type1"]["score"] = 8.0
        companies[0]["types"]["type1"]["decision"] = {
            **_complete_decision(8.0),
            "potentially_triggerable": True,
        }
        companies[0]["types"]["type6"]["status"] = "conditional"
        companies[0]["types"]["type6"]["score"] = 7.2
        companies[0]["types"]["type6"]["investor_action_dimensions"] = ["6e"]
        companies[0]["types"]["type6"]["action_required"] = "position_confirmation"
        companies[0]["types"]["type6"]["position_guidance"] = "请确认单票与组合仓位均符合建议上限"
        companies[0]["types"]["type6"]["decision"] = {
            "schema_version": 1,
            "model_id": DECISION_MODEL_ID,
            "decision_complete": False,
            "decision_basis": "action_condition",
            "score_lower_bound": 6.2,
            "score_upper_bound": 8.2,
            "veto_state": "none",
            "potentially_triggerable": True,
            "missing_dimensions": ["6e"],
        }
        coverage["type1"]["not_triggered"] -= 1
        coverage["type1"]["triggered"] += 1
        coverage["type6"]["not_triggered"] -= 1
        coverage["type6"]["conditional"] += 1
    if pending_candidate:
        if legacy_decision_contract:
            raise ValueError("legacy fixtures cannot declare pending decision candidates")
        pending_company_count = 1
        visible_candidate_company_count = 1
        companies[0]["types"]["type1"] = {
            "status": "insufficient_evidence",
            "score": None,
            "reason": "缺少1a证据",
            "decision": {
                "schema_version": 1,
                "model_id": DECISION_MODEL_ID,
                "decision_complete": False,
                "decision_basis": "unresolved_missing_evidence",
                "score_lower_bound": 3.0,
                "score_upper_bound": 8.0,
                "veto_state": "none",
                "potentially_triggerable": True,
                "missing_dimensions": ["1a"],
            },
        }
        companies[0]["pending_types"] = [] if pending_mismatch else ["type1"]
        coverage["type1"]["not_triggered"] -= 1
        coverage["type1"]["insufficient_evidence"] += 1
    if complete_veto_with_missing:
        companies[0]["types"]["type4"] = {
            "status": "vetoed",
            "score": 4.0,
            "reason": "已确认硬否决",
            "decision": {
                "schema_version": 1,
                "model_id": DECISION_MODEL_ID,
                "decision_complete": True,
                "decision_basis": "confirmed_veto",
                "score_lower_bound": 1.0,
                "score_upper_bound": 6.0,
                "veto_state": "confirmed",
                "potentially_triggerable": False,
                "missing_dimensions": ["4a", "4b"],
            },
        }
        coverage["type4"]["not_triggered"] -= 1
        coverage["type4"]["vetoed"] += 1
    if decision_damage == "extra_field":
        companies[0]["types"]["type1"]["decision"]["unexpected"] = True
    elif decision_damage == "unknown_dimension":
        companies[0]["types"]["type1"]["decision"]["decision_complete"] = False
        companies[0]["types"]["type1"]["decision"]["missing_dimensions"] = ["9z"]
    elif decision_damage == "partial_company":
        del companies[0]["types"]["type1"]["decision"]
    elif decision_damage == "mixed_catalogue":
        for state in companies[0]["types"].values():
            del state["decision"]
        del companies[0]["pending_types"]
    elif decision_damage is not None:
        raise ValueError(f"unknown decision damage: {decision_damage}")
    if scalar_type_list is not None:
        companies[0][scalar_type_list] = "type1"
    timestamp = data_timestamp_utc or f"{market_date}T08:20:00+00:00"
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
        "triggered_company_count": triggered_company_count,
        "conditional_company_count": conditional_company_count,
        "conditional_only_company_count": conditional_company_count
        if conditional_only_mismatch
        else conditional_only_company_count,
        "candidate_detail_count": len(signal_rows),
        **(
            {}
            if legacy_decision_contract
            else {
                "pending_company_count": pending_company_count,
                "visible_candidate_company_count": visible_candidate_company_count,
                "action_confirmation_company_count": action_confirmation_company_count
                + (1 if action_confirmation_mismatch else 0),
            }
        ),
        "signals": signal_rows,
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
            **(
                {}
                if legacy_decision_contract or omit_uncompressed_size
                else {
                    "uncompressed_size": len(catalogue_raw) + (1 if uncompressed_size_mismatch else 0),
                }
            ),
        },
        "signals": {
            "filename": signals.name,
            "size": signals.stat().st_size,
            "sha256": hashlib.sha256(signals.read_bytes()).hexdigest(),
            **({} if legacy_decision_contract or omit_uncompressed_size else {"uncompressed_size": len(signals_raw)}),
        },
        "signature": {
            "filename": signature.name,
            "algorithm": "ECDSA_P256_SHA256",
        },
        "summary": {
            "company_count": company_count,
            "triggered_company_count": triggered_company_count,
            "conditional_company_count": conditional_company_count,
            "conditional_only_company_count": conditional_company_count
            if conditional_only_mismatch
            else conditional_only_company_count,
            "candidate_detail_count": len(signal_rows),
            **(
                {}
                if legacy_decision_contract
                else {
                    "pending_company_count": pending_company_count,
                    "visible_candidate_company_count": visible_candidate_company_count,
                    "action_confirmation_company_count": action_confirmation_company_count
                    + (1 if action_confirmation_mismatch else 0),
                }
            ),
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
    assert "$script:MaximumUncompressedPayloadBytes = 24000000" in guard_source
    assert mobile_snapshot.MAX_UNCOMPRESSED_ASSET_BYTES == 24_000_000
    assert "MAX_UNCOMPRESSED_ASSET_BYTES = 24_000_000;" in ANDROID_REPOSITORY.read_text(encoding="utf-8")


def test_manual_dispatch_forces_post_close_refresh_without_reading_network(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="workflow_dispatch",
        now_utc="2026-07-22T08:15:00.0000000+00:00",
    )

    assert result.returncode == 0, result.stderr
    assert decision == {"should_run": "true", "reason": "manual_dispatch_forced"}


def test_manual_dispatch_before_close_is_a_successful_noop(tmp_path):
    result, decision = _run_guard(
        tmp_path,
        event="workflow_dispatch",
        now_utc="2026-07-22T08:14:59.0000000+00:00",
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


def test_valid_same_day_signed_generation_suppresses_a_duplicate_scheduled_run(tmp_path):
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


def test_valid_pending_candidate_is_visible_without_entering_the_legacy_signal_array(tmp_path):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        pending_candidate=True,
    )

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


def test_complete_confirmed_veto_may_retain_unneeded_missing_dimensions(tmp_path):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        complete_veto_with_missing=True,
    )

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


def test_company_with_trigger_and_conditional_type_is_not_counted_as_conditional_only(tmp_path):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        overlapping_candidate=True,
    )

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


def test_legacy_signed_generation_remains_accepted_during_the_11_2_upgrade_window(tmp_path):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        legacy_decision_contract=True,
    )

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
    "generation_options",
    [
        {"overlapping_candidate": True, "conditional_only_mismatch": True},
        {"overlapping_candidate": True, "action_confirmation_mismatch": True},
        {"omit_uncompressed_size": True},
        {"uncompressed_size_mismatch": True},
    ],
)
def test_current_signed_generation_rejects_invalid_counts_or_uncompressed_metadata(
    tmp_path,
    generation_options,
):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        **generation_options,
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


@pytest.mark.parametrize(
    "generation_options",
    [
        {"pending_candidate": True, "pending_mismatch": True},
        {"decision_damage": "extra_field"},
        {"decision_damage": "unknown_dimension"},
        {"decision_damage": "partial_company"},
        {"decision_damage": "mixed_catalogue"},
    ],
)
def test_signed_generation_rejects_inconsistent_or_partial_candidate_decisions(
    tmp_path,
    generation_options,
):
    manifest, release, archive, android_source = _signed_generation(
        tmp_path,
        **generation_options,
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
