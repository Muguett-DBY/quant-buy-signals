import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MOBILE_WORKFLOW = ROOT / ".github" / "workflows" / "mobile-market-data.yml"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _workflow(path: Path) -> dict:
    value = yaml.safe_load(_workflow_text(path))
    assert isinstance(value, dict)
    return value


def _powershell_run_blocks(path: Path) -> list[str]:
    workflow = _workflow(path)
    blocks: list[str] = []
    for job in workflow["jobs"].values():
        windows_default = str(job.get("runs-on", "")).startswith("windows-")
        for step in job.get("steps", []):
            run = step.get("run")
            shell = str(step.get("shell", "")).casefold()
            if isinstance(run, str) and (shell in {"pwsh", "powershell"} or (not shell and windows_default)):
                blocks.append(run)
    return blocks


def _bash_run_blocks(path: Path) -> list[str]:
    workflow = _workflow(path)
    blocks: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            shell = str(step.get("shell", "")).casefold()
            if isinstance(run, str) and shell == "bash":
                blocks.append(run)
    return blocks


def _bash_executable() -> str | None:
    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git is not None:
        git_root = Path(git).resolve().parents[1]
        candidates.extend((str(git_root / "bin" / "bash.exe"), str(git_root / "usr" / "bin" / "bash.exe")))
    for candidate in dict.fromkeys(value for value in candidates if value):
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except OSError:
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _pwsh_executable() -> str:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is not installed on this test host")
    return executable


def test_mobile_publication_uses_immutable_release_assets_and_an_atomic_pages_manifest():
    workflow = _workflow_text(MOBILE_WORKFLOW)

    immutable_step = workflow.index("- name: Publish immutable payload assets and validate the previous generation")
    pages_step = workflow.index("- name: Atomically switch the stable mobile manifest")
    cleanup_step = workflow.index("- name: Retain only the current and previous complete generations")
    assert immutable_step < pages_step < cleanup_step
    assert "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b" in workflow
    assert "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
    assert "muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json" in workflow
    assert 'gh release upload $tag "$manifest#manifest.json" --clobber' not in workflow
    assert "--clobber" not in "\n".join(line for line in workflow.splitlines() if "gh release upload" in line)
    assert "signals-$generation.json.gz" in workflow
    assert "manifest-$generation.sig" in workflow
    assert "Test-DetachedSignature" in workflow
    assert "previous_assets=" in workflow
    assert "previous_manifest_state=" in workflow


def test_mobile_publication_artifacts_survive_failed_job_and_full_workflow_reruns():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    jobs = _workflow(MOBILE_WORKFLOW)["jobs"]

    assert "github.run_attempt" not in workflow
    build_upload = next(
        step for step in jobs["build"]["steps"] if step.get("name") == "Upload the verified bundle between jobs"
    )
    assert build_upload["with"]["name"] == "mobile-market-data-build-${{ github.run_id }}"
    assert build_upload["with"]["overwrite"] is True
    publish_download = next(
        step for step in jobs["publish"]["steps"] if step.get("name") == "Download the verified release bundle"
    )
    assert publish_download["with"]["name"] == build_upload["with"]["name"]

    canonical_upload = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("name") == "Upload the canonical published bundle for downstream jobs"
    )
    assert canonical_upload["with"]["name"] == "mobile-market-data-published-${{ github.run_id }}"
    assert canonical_upload["with"]["overwrite"] is True
    assert canonical_upload["if"] == "steps.release.outputs.published == 'true'"
    for job_name in ("prepare_pages", "mirror_cloudflare", "verify_cleanup", "archive_manifest"):
        download = next(
            step for step in jobs[job_name]["steps"] if str(step.get("name", "")).startswith("Download the")
        )
        assert download["with"]["name"] == canonical_upload["with"]["name"]


def test_cloudflare_live_check_uses_the_same_methodology_version_as_the_pages_worker():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    pages_worker = (ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js").read_text(encoding="utf-8")
    version_match = re.search(r'^const METHODOLOGY_VERSION="([^"]+)";', pages_worker)

    assert version_match is not None
    assert version_match.group(1) in workflow
    assert "classified-type7-v2" not in workflow
    assert "classified-type7-v3" not in workflow


def test_mobile_publication_removes_only_incomplete_starter_assets_before_retry():
    parsed = _workflow(MOBILE_WORKFLOW)
    release = next(step for step in parsed["jobs"]["publish"]["steps"] if step.get("id") == "release")["run"]

    assert release.count("[string]$_.state -ceq 'starter'") == 2
    assert release.count("[long]$_.size -eq 0") == 2
    assert release.count("[string]$asset.apiUrl") == 2
    assert release.count("api\\.github\\.com/repos/") == 2
    assert release.count("[string]$Matches.repository -ine $env:GH_REPO") == 2
    assert release.count('gh api --method DELETE "repos/$env:GH_REPO/releases/assets/$assetId"') == 2
    assert "already exists with different bytes" in release
    assert "Removing incomplete GitHub release asset" in release


def test_pages_deployment_builds_a_static_chinese_status_page_and_manifest(tmp_path):
    parsed = _workflow(MOBILE_WORKFLOW)
    prepare_steps = parsed["jobs"]["prepare_pages"]["steps"]
    prepare = next(step for step in prepare_steps if step.get("name") == "Prepare the atomic Pages deployment")
    script = prepare["run"]

    assert isinstance(script, str)
    assert "<<'HTML'" in script
    assert '<html lang="zh-CN">' in script
    assert '<a href="mobile-data/manifest.json">' in script
    assert '<a href="https://github.com/Muguett-DBY/quant-buy-signals/releases">' in script
    assert "<script" not in script.casefold()
    assert "${{" not in script

    executable = _bash_executable()
    if executable is None:
        pytest.skip("Bash is not installed on this test host")
    release = tmp_path / "ds-dcf-mobile-market-data-release"
    release.mkdir()
    generation = "0123456789abcdef"
    detail_names = [f"company-details-{generation}-{index:02x}.json.gz" for index in range(16)]
    manifest = {
        "schema_version": 1,
        "catalogue": {"filename": f"catalog-{generation}.json.gz"},
        "company_details": {
            "schema_version": 2,
            "record_schema": "company_detail_v2",
            "partition": {"algorithm": "sha256_code_first_nibble", "shard_count": 16},
            "root_algorithm": "SHA256_CANONICAL_SHARD_INDEX_V1",
            "root_sha256": "a" * 64,
            "shards": [{"id": f"{index:02x}", "filename": name} for index, name in enumerate(detail_names)],
        },
    }
    (release / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (release / f"manifest-{generation}.sig").write_bytes(b"signature")
    (release / f"catalog-{generation}.json.gz").write_bytes(b"catalogue")
    (release / f"signals-{generation}.json.gz").write_bytes(b"signals")
    for name in detail_names:
        (release / name).write_bytes(b"detail")
    workflow_script = tmp_path / "prepare-pages.sh"
    workflow_script.write_text(textwrap.dedent(script), encoding="utf-8")
    environment = os.environ.copy()
    environment["RUNNER_TEMP"] = str(tmp_path)
    result = subprocess.run(
        [executable, str(workflow_script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert result.returncode == 0, result.stderr

    pages = tmp_path / "mobile-pages"
    index = (pages / "index.html").read_text(encoding="utf-8")
    assert "DS_DCF 移动数据服务" in index
    assert (pages / "mobile-data" / "manifest.json").read_bytes() == (release / "manifest.json").read_bytes()
    expected_files = [
        "index.html",
        f"mobile-data/catalog-{generation}.json.gz",
        *(f"mobile-data/{name}" for name in detail_names),
        f"mobile-data/manifest-{generation}.sig",
        "mobile-data/manifest.json",
        f"mobile-data/signals-{generation}.json.gz",
    ]
    assert sorted(path.relative_to(pages).as_posix() for path in pages.rglob("*") if path.is_file()) == sorted(
        expected_files
    )


def test_company_detail_shards_are_part_of_every_signed_publication_boundary():
    parsed = _workflow(MOBILE_WORKFLOW)
    jobs = parsed["jobs"]
    workflow = _workflow_text(MOBILE_WORKFLOW)

    build_steps = jobs["build"]["steps"]
    build = next(step for step in build_steps if step.get("name") == "Build validated market data")["run"]
    verify = next(
        step for step in build_steps if step.get("name") == "Verify the post-close, content-addressed manifest"
    )["run"]
    exact = next(step for step in build_steps if step.get("name") == "Verify the exact immutable release bundle")["run"]
    transferred = next(
        step for step in jobs["publish"]["steps"] if step.get("name") == "Reverify the transferred release bundle"
    )["run"]
    release = next(step for step in jobs["publish"]["steps"] if step.get("id") == "release")["run"]
    public_verify = next(
        step
        for step in jobs["verify_cleanup"]["steps"]
        if step.get("name") == "Verify the public Pages switch and immutable downloads"
    )["run"]
    cleanup = next(
        step
        for step in jobs["verify_cleanup"]["steps"]
        if step.get("name") == "Retain only the current and previous complete generations"
    )["run"]

    for script in (build, verify, transferred, release):
        assert "company_detail_v2" in script
        assert "sha256_code_first_nibble" in script
        assert "SHA256_CANONICAL_SHARD_INDEX_V1" in script
        assert "root_sha256" in script
        assert "company-details-$generation-" in script or "company-details-$previousGeneration-" in script
    assert "[IO.Compression.GZipStream]::new" in verify
    assert "uncompressed_sha256" in verify
    assert "uncompressed_size" in verify
    assert "$computedGeneration -cne $generation" in verify
    assert "catalogue, signals, and company detail root generation" in verify
    assert "MOBILE_DETAIL_NAMES" in exact
    assert "MOBILE_DETAIL_NAMES" in transferred
    assert "Publish-ImmutableAsset $name" in release
    assert "$previousDetailNames" in release
    assert "$computedPreviousGeneration -cne $previousGeneration" in release
    assert "function Receive-ReleaseAssets" in release
    assert "Receive-ReleaseAssets $previousAssetNames $previousDirectory" in release
    assert "$arguments += @('--pattern', $name)" in release
    assert "detail_names=$env:MOBILE_DETAIL_NAMES" in release
    assert jobs["publish"]["outputs"]["detail_names"] == "${{ steps.release.outputs.detail_names }}"
    assert jobs["publish"]["timeout-minutes"] == 45
    assert jobs["verify_cleanup"]["env"]["MOBILE_DETAIL_NAMES"] == "${{ needs.publish.outputs.detail_names }}"
    assert "$assetNames" in public_verify
    assert "$detailNames" in public_verify
    assert "generation-wide passes" in public_verify
    assert "company-details-[0-9a-f]{16}-[0-9a-f]{2}" in cleanup
    assert workflow.count('shard_count":16') >= 4
    assert 'wc -l)" -eq 20' in workflow


def test_mobile_publication_rechecks_close_time_hashes_signatures_and_exact_file_set():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)

    assert "retrieval_time_oldest" in workflow
    assert "source_quote_timestamp_latest" in workflow
    assert workflow.count("[TimeSpan]::FromMinutes(975)") >= 2
    assert "[TimeSpan]::FromHours(15)" in workflow
    assert workflow.count("Compare-Object $actual $expected") >= 2
    assert "does not match the signed manifest" in workflow
    assert "workflow signing secret does not match the public key pinned by the Android client" in workflow
    assert "Transferred manifest signature does not match the app-pinned key" in workflow
    assert "Previous manifest signature is invalid" in workflow
    assert "Previous asset $($entry[1]) is incomplete" in workflow
    assert "Public immutable asset $name differs from the verified build artifact" in workflow
    signing = next(
        step
        for step in parsed["jobs"]["build"]["steps"]
        if step.get("name") == "Sign the exact market manifest and verify the app-pinned key"
    )
    assert "sign_mobile_manifest.ps1" in signing["run"]
    assert "$LASTEXITCODE" not in signing["run"]


def test_mobile_publication_does_not_rename_or_rewrite_the_signed_generation():
    workflow = _workflow_text(MOBILE_WORKFLOW)

    assert "catalog.json.gz" not in workflow
    assert "signals.json.gz" not in workflow
    assert "Move-Item -LiteralPath $cataloguePath" not in workflow
    assert "Move-Item -LiteralPath $signalsPath" not in workflow
    assert "manifestHash.Substring" not in workflow
    assert "$manifest.catalogue.filename =" not in workflow
    assert "$manifest.signals.filename =" not in workflow


def test_mobile_publication_is_main_only_and_uses_least_privilege_jobs():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)
    jobs = parsed["jobs"]

    assert set(jobs) == {
        "preflight",
        "build",
        "publish",
        "prepare_pages",
        "deploy_pages",
        "mirror_cloudflare",
        "verify_cleanup",
        "archive_manifest",
    }
    assert jobs["preflight"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["build"]["if"] == ("github.ref == 'refs/heads/main' && needs.preflight.outputs.should_run == 'true'")
    for name in ("publish", "deploy_pages", "archive_manifest"):
        assert jobs[name]["if"] == "github.ref == 'refs/heads/main'"
    for name in ("prepare_pages", "mirror_cloudflare", "verify_cleanup"):
        assert jobs[name]["if"] == "github.ref == 'refs/heads/main' && needs.publish.outputs.published == 'true'"
    assert jobs["preflight"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert jobs["prepare_pages"]["permissions"] == {"contents": "read", "pages": "write"}
    assert jobs["deploy_pages"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["mirror_cloudflare"]["permissions"] == {}
    assert jobs["verify_cleanup"]["permissions"] == {"contents": "write"}
    assert jobs["archive_manifest"]["permissions"] == {"contents": "write"}
    assert jobs["archive_manifest"]["needs"] == "verify_cleanup"
    assert jobs["build"]["needs"] == "preflight"
    assert parsed["concurrency"] == {
        "group": "mobile-market-data",
        "cancel-in-progress": False,
    }
    assert jobs["build"]["environment"] == "mobile-production"
    assert jobs["deploy_pages"]["environment"]["name"] == "github-pages"
    assert jobs["mirror_cloudflare"]["needs"] == ["publish", "deploy_pages"]
    assert jobs["verify_cleanup"]["needs"] == ["publish", "deploy_pages", "mirror_cloudflare"]
    mirror = jobs["mirror_cloudflare"]
    assert mirror["env"]["REFRESH_URL"] == "https://quant-market-refresh.1203135430.workers.dev/refresh"
    assert mirror["env"]["REFRESH_KEY"] == "${{ secrets.CLOUDFLARE_MARKET_REFRESH_KEY }}"
    mirror_download = mirror["steps"][0]
    assert mirror_download["with"]["name"] == "mobile-market-data-published-${{ github.run_id }}"
    mirror_script = mirror["steps"][1]["run"]
    assert "x-refresh-key: ${REFRESH_KEY}" in mirror_script
    assert "quant.custard.top/api/meta" in mirror_script
    assert "expected_generation" in mirror_script
    assert "expected_market_as_of" in mirror_script
    assert "expected_manifest_sha256" in mirror_script
    assert "expected_company_count" in mirror_script
    assert "expected_source_commit" in mirror_script
    assert "actual_generation" in mirror_script
    assert "actual_manifest_sha256" in mirror_script
    assert 'transport_ok="false"' not in mirror_script
    assert "transport_ok=false" in mirror_script
    assert mirror["timeout-minutes"] == 60
    assert "mirror_attempt_limit=8" in mirror_script
    assert "mirror_retry_base_delay_seconds=10" in mirror_script
    assert "for ((attempt=1; attempt<=mirror_attempt_limit; attempt++))" in mirror_script
    assert "(.current_generation_id // .generation_id)" in mirror_script
    assert '"${response_generation}" == "${expected_generation}"' in mirror_script
    assert "generation '${response_generation:-none}' on attempt ${attempt} of ${mirror_attempt_limit}" in mirror_script
    assert "sleep $((mirror_retry_base_delay_seconds * attempt))" in mirror_script
    assert "verify_cloudflare_projection() {" in mirror_script
    assert 'if verify_cloudflare_projection "${attempt}"; then' in mirror_script
    assert "Cloudflare public projection has not converged on attempt ${attempt} of 6." in mirror_script
    assert "|| return 1" in mirror_script
    assert "Cloudflare dashboard did not retain the exact published generation." in mirror_script
    assert "requirements-dev-lock.txt" not in workflow
    # The build job needs room for a whole-market Type 3 growth-evidence
    # backfill (segment + annual cash-flow rows) in addition to the snapshot
    # fetch and full-market scoring.
    assert "timeout-minutes: 180" in workflow
    assert workflow.count("continue-on-error: true") == 4
    assert "persist-credentials: false" in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow


def test_old_main_reruns_are_noops_before_build_and_immediately_before_publication():
    parsed = _workflow(MOBILE_WORKFLOW)
    jobs = parsed["jobs"]
    guard = next(step for step in jobs["preflight"]["steps"] if step.get("id") == "guard")["run"]
    release_step = next(step for step in jobs["publish"]["steps"] if step.get("id") == "release")
    release = release_step["run"]

    for script in (guard, release):
        assert "function Get-RemoteMainRevision" in script
        assert "git ls-remote --exit-code origin refs/heads/main" in script
        assert "$attemptLimit = 3" in script
        assert "attempt $attempt of $attemptLimit failed: $lastFailure" in script
        assert "failed after $attemptLimit attempts: $lastFailure" in script
        assert "$remoteMainSha -cne $env:GITHUB_SHA" in script
    assert guard.index("$remoteMainSha -cne $env:GITHUB_SHA") < guard.index("mobile_market_workflow_guard.ps1")
    assert "reason=stale_main_workflow_run" in guard
    assert release.index("$remoteMainSha -cne $env:GITHUB_SHA") < release.index("$tag = 'mobile-market-data'")
    assert "'published=false'" in release
    assert "'published=true'" in release
    assert jobs["publish"]["outputs"]["published"] == "${{ steps.release.outputs.published }}"


def test_job_level_environment_does_not_reference_runner_context():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    jobs = _workflow(MOBILE_WORKFLOW)["jobs"]

    for job_name, job in jobs.items():
        for key, value in (job.get("env") or {}).items():
            assert "${{ runner." not in str(value), f"{job_name}.{key} uses a forbidden job-level context"

    assert workflow.count("MOBILE_DATA_DIR=$env:RUNNER_TEMP\\ds-dcf-mobile-market-data-release") == 2
    for job_name in ("publish", "verify_cleanup"):
        setup = next(
            step for step in jobs[job_name]["steps"] if step.get("name") == "Set the private release directory"
        )
        assert setup["shell"] == "pwsh"
        assert "$env:RUNNER_TEMP" in setup["run"]
        assert "Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append" in setup["run"]


def test_mobile_publication_archives_only_the_signed_manifest_on_a_data_branch():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)
    archive = parsed["jobs"]["archive_manifest"]
    archive_steps = archive["steps"]

    assert archive["if"] == "github.ref == 'refs/heads/main'"
    assert archive["needs"] == "verify_cleanup"
    checkout = archive_steps[0]
    assert checkout["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 1}
    branch_setup = archive_steps[1]["run"]
    assert '"${GITHUB_REF}" != "refs/heads/main"' in branch_setup
    assert '"$(git rev-parse HEAD)" != "${GITHUB_SHA}"' in branch_setup
    assert "git ls-remote --exit-code --heads origin refs/heads/mobile-data" in branch_setup
    assert "lookup_attempt_limit=3" in branch_setup
    assert (
        "Remote mobile-data branch lookup attempt ${attempt} of ${lookup_attempt_limit} failed with exit "
        "${lookup_status}: ${lookup_error_detail}"
    ) in branch_setup
    assert (
        "Could not determine whether the remote data audit branch exists after ${lookup_attempt_limit} attempts"
        in branch_setup
    )
    assert "lookup_status} -eq 0" in branch_setup
    assert "lookup_status} -eq 2" in branch_setup
    assert "git fetch --no-tags --depth=1 origin refs/heads/mobile-data" in branch_setup
    assert "git switch --create mobile-data FETCH_HEAD" in branch_setup
    assert 'git switch --create mobile-data "${GITHUB_SHA}"' in branch_setup
    assert "--force" not in branch_setup
    assert "latest/manifest.json" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "gh release download mobile-market-data" in workflow
    assert "source_commit=\"$(jq -er '.provenance.source_commit'" in workflow
    assert "retry gh release download mobile-market-data" in workflow
    assert "retry git push origin HEAD:mobile-data" in workflow
    assert "git push origin HEAD:mobile-data" in workflow
    assert "git push --force" not in workflow
    assert "git push -f" not in workflow
    assert "[skip ci]" in workflow
    assert "catalogue_name" not in workflow[workflow.index("  archive_manifest:") :]
    assert "signals_name" not in workflow[workflow.index("  archive_manifest:") :]


def test_mobile_publication_retries_after_close_and_caches_every_deep_evidence_source():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)
    triggers = parsed.get("on", parsed.get(True))

    assert triggers["schedule"] == [{"cron": "17 8 * * 1-5"}]
    assert triggers["workflow_dispatch"]["inputs"]["rebuild_latest_closed"] == {
        "description": (
            "Re-score the latest already closed and published market session with the current source revision"
        ),
        "required": False,
        "default": False,
        "type": "boolean",
    }
    preflight = parsed["jobs"]["preflight"]
    guard = next(step for step in preflight["steps"] if step.get("id") == "guard")
    assert "mobile_market_workflow_guard.ps1" in guard["run"]
    assert "${{ github.event_name }}" in guard["run"]
    assert "'${{ inputs.rebuild_latest_closed }}' -eq 'true'" in guard["run"]
    assert "reason=manual_rebuild_latest_closed" in guard["run"]
    guard_invocation = guard["run"].index("mobile_market_workflow_guard.ps1")
    assert "$LASTEXITCODE" not in guard["run"][guard_invocation:]
    assert preflight["outputs"] == {
        "should_run": "${{ steps.guard.outputs.should_run }}",
        "reason": "${{ steps.guard.outputs.reason }}",
    }
    assert workflow.count("data/cache/market_snapshot.json.gz") == 3
    for path in (
        "data/cache/market_coldness",
        "data/cache/market_history",
        "data/cache/quality_history",
        "data/cache/growth_evidence",
        "data/cache/research_reports",
        "data/cache/patch4_evidence",
    ):
        assert workflow.count(path) == 4
    for contract in (
        "data/growth_evidence.py",
        "data/quality_history.py",
        "data/research_reports.py",
        "data/patch4_evidence.py",
    ):
        assert workflow.count(contract) == 3
    assert workflow.count("mobile-market-v1-${{ runner.os }}-") == 4
    assert "mobile-market-v1-${{ runner.os }}-\n" in workflow
    assert workflow.count("mobile-deep-evidence-v1-${{ runner.os }}-") == 3


def test_manual_model_rebuild_reuses_only_the_latest_validated_closed_session():
    parsed = _workflow(MOBILE_WORKFLOW)
    build_steps = parsed["jobs"]["build"]["steps"]
    build = next(step for step in build_steps if step.get("name") == "Build validated market data")["run"]
    verify = next(
        step for step in build_steps if step.get("name") == "Verify the post-close, content-addressed manifest"
    )["run"]

    assert "$attemptLimit = if ($rebuildLatestClosed) { 1 } else { 2 }" in build
    assert "data/cache/market_snapshot.json.gz" in build
    assert "refusing a live-data fallback" in build
    assert "$publisherArguments += '--refresh'" in build
    assert "if (-not $rebuildLatestClosed)" in build
    assert "& python @publisherArguments" in build
    assert "--refresh --output-dir" not in build
    assert "model_rebuild=$env:GITHUB_RUN_ID" in verify
    assert "$publishedManifest.market_as_of" in verify
    assert "is not the latest published closed session" in verify
    assert "older than 14 days" in verify


def test_production_workflows_use_only_the_validated_python_lanes():
    tests_workflow = _workflow_text(TEST_WORKFLOW)
    mobile_workflow = _workflow_text(MOBILE_WORKFLOW)

    assert 'python-version: "3.12"' not in tests_workflow
    assert 'python-version: "3.12"' not in mobile_workflow
    assert 'python-version: "3.13"' in tests_workflow
    assert 'python-version: "3.13"' in mobile_workflow


def test_stale_tracked_audit_defers_desktop_archive_check_without_failing_ci():
    workflow = _workflow_text(TEST_WORKFLOW)
    marker = "Tracked release audit is bound to"
    assert marker in workflow
    assert workflow.index(marker) < workflow.index("$root = Join-Path $env:RUNNER_TEMP 'ds-dcf-source-package'")
    assert "desktop archive verification is deferred until a fresh audit is committed" in workflow


def test_every_powershell_workflow_script_parses_with_the_real_parser(tmp_path):
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is not installed on this test host")
    parser = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:WORKFLOW_SCRIPT_PATH,[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors.Count){$errors | ForEach-Object { Write-Error $_.Message }; exit 1}"
    )
    blocks = _powershell_run_blocks(MOBILE_WORKFLOW)
    assert len(blocks) >= 9
    assert all("set -euo pipefail" not in block for block in blocks)
    for index, block in enumerate(blocks):
        script = tmp_path / f"workflow-block-{index}.ps1"
        script.write_text(textwrap.dedent(block), encoding="utf-8")
        environment = os.environ.copy()
        environment["WORKFLOW_SCRIPT_PATH"] = str(script)
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", parser],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        assert result.returncode == 0, f"PowerShell block {index} failed to parse:\n{result.stderr}"


def test_every_bash_workflow_script_parses_with_bash(tmp_path):
    executable = _bash_executable()
    if executable is None:
        pytest.skip("Bash is not installed on this test host")
    blocks = _bash_run_blocks(MOBILE_WORKFLOW)
    assert len(blocks) >= 2
    for index, block in enumerate(blocks):
        script = tmp_path / f"workflow-block-{index}.sh"
        script.write_text(textwrap.dedent(block), encoding="utf-8")
        result = subprocess.run(
            [executable, "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, f"Bash block {index} failed to parse:\n{result.stderr}"


def test_pages_action_revisions_and_release_patterns_are_pinned_and_bounded():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)
    verify_cleanup = parsed["jobs"]["verify_cleanup"]
    public_verification = next(
        step
        for step in verify_cleanup["steps"]
        if step.get("name") == "Verify the public Pages switch and immutable downloads"
    )["run"]

    assert re.search(r"actions/configure-pages@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/upload-pages-artifact@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/deploy-pages@[0-9a-f]{40}", workflow)
    assert "$attempt -le 6" in workflow
    assert "Start-Sleep -Seconds (5 * $attempt)" in workflow
    attempts_match = re.search(r"\$assetVerificationAttempts = (?P<value>\d+)", public_verification)
    delay_match = re.search(r"\$assetVerificationBaseDelaySeconds = (?P<value>\d+)", public_verification)
    assert attempts_match is not None
    assert delay_match is not None
    attempts = int(attempts_match.group("value"))
    base_delay_seconds = int(delay_match.group("value"))
    total_wait_seconds = base_delay_seconds * sum(range(1, attempts))
    assert 240 <= total_wait_seconds <= 300
    assert verify_cleanup["timeout-minutes"] == 60
    assert "for ($attempt = 1; $attempt -le $assetVerificationAttempts; $attempt++)" in public_verification
    assert "Start-Sleep -Seconds ($assetVerificationBaseDelaySeconds * $attempt)" in public_verification
    assert "verification attempt $attempt of $assetVerificationAttempts failed:" in public_verification
    assert "$($assetFailures[$name])" in public_verification
    assert "could not be verified after $assetVerificationAttempts attempts:" in public_verification
    assert "gh release delete-asset $tag $name --repo $env:GH_REPO --yes" in workflow
    assert "manifest\\.json" in workflow


def test_critical_remote_and_release_queries_use_bounded_retries_with_diagnostics():
    parsed = _workflow(MOBILE_WORKFLOW)
    jobs = parsed["jobs"]
    release = next(step for step in jobs["publish"]["steps"] if step.get("id") == "release")["run"]
    cleanup = next(
        step
        for step in jobs["verify_cleanup"]["steps"]
        if step.get("name") == "Retain only the current and previous complete generations"
    )["run"]

    assert "$releaseQueryAttemptLimit = 3" in release
    assert "Mobile data release lookup attempt $attempt of $releaseQueryAttemptLimit failed:" in release
    assert "Mobile data release creation attempt $attempt of $releaseQueryAttemptLimit failed:" in release
    assert "Mobile release asset query attempt $attempt of $attemptLimit failed:" in release
    assert "after $releaseQueryAttemptLimit attempts: $lastReleaseCreateFailure" in release
    assert "$attemptLimit = 3" in cleanup
    assert "Mobile retention asset query attempt $attempt of $attemptLimit failed:" in cleanup
    assert "after $attemptLimit attempts: $lastFailure" in cleanup


def test_public_release_asset_url_preserves_the_filename_during_powershell_interpolation():
    parsed = _workflow(MOBILE_WORKFLOW)
    public_verification = next(
        step
        for step in parsed["jobs"]["verify_cleanup"]["steps"]
        if step.get("name") == "Verify the public Pages switch and immutable downloads"
    )["run"]
    assignment = next(
        line.strip() for line in public_verification.splitlines() if '$url = "https://github.com/' in line
    )
    script = (
        "$env:GH_REPO='owner/repository';"
        "$env:GITHUB_RUN_ID='12345';"
        "$name='catalog-0123456789abcdef.json.gz';"
        "$attempt=2;"
        f"{assignment};"
        "[Console]::Out.Write($url)"
    )
    result = subprocess.run(
        [_pwsh_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout == (
        "https://github.com/owner/repository/releases/download/mobile-market-data/"
        "catalog-0123456789abcdef.json.gz?run_id=12345&attempt=2"
    )


def test_previous_manifest_errors_skip_cleanup_and_only_verified_first_publication_allows_cleanup():
    workflow = _workflow_text(MOBILE_WORKFLOW)
    parsed = _workflow(MOBILE_WORKFLOW)
    publish = parsed["jobs"]["publish"]
    release_step = next(step for step in publish["steps"] if step.get("id") == "release")
    release_script = release_step["run"]

    assert "$previousManifestState = 'error'" in release_script
    assert "$previousManifestState = 'found'" in release_script
    assert "$allPreviousFetchesWereNotFound = $true" in release_script
    assert "if ($statusCode -ne 404)" in release_script
    assert "$allPreviousFetchesWereNotFound -and $releaseCreated" in release_script
    assert "$attempt -le 3" in release_script
    assert "$previousManifestState = 'missing'" in release_script
    assert "retention cleanup will be skipped" in release_script
    assert "previous_manifest_state=$previousManifestState" in release_script
    assert publish["outputs"]["previous_manifest_state"] == "${{ steps.release.outputs.previous_manifest_state }}"

    cleanup = next(
        step
        for step in parsed["jobs"]["verify_cleanup"]["steps"]
        if step.get("name") == "Retain only the current and previous complete generations"
    )["run"]
    guard_index = cleanup.index("MOBILE_PREVIOUS_MANIFEST_STATE -ceq 'error'")
    deletion_index = cleanup.index("gh release delete-asset")
    assert guard_index < deletion_index
    assert "exit 0" in cleanup[guard_index:deletion_index]
    assert "MOBILE_PREVIOUS_MANIFEST_STATE -cnotin @('found', 'missing')" in cleanup
    assert workflow.count("previous_manifest_state") >= 3


def test_cleanup_runtime_never_invokes_github_when_previous_manifest_validation_failed(tmp_path):
    parsed = _workflow(MOBILE_WORKFLOW)
    cleanup = next(
        step
        for step in parsed["jobs"]["verify_cleanup"]["steps"]
        if step.get("name") == "Retain only the current and previous complete generations"
    )["run"]
    script = tmp_path / "cleanup-error.ps1"
    script.write_text(
        "function gh { throw 'gh must not run when previous manifest state is error' }\n" + cleanup,
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["MOBILE_PREVIOUS_MANIFEST_STATE"] = "error"

    result = subprocess.run(
        [_pwsh_executable(), "-NoProfile", "-NonInteractive", "-File", str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping retention cleanup" in result.stdout + result.stderr


def test_ci_workflow_runs_on_every_branch_push_and_supports_manual_reverification():
    workflow = _workflow_text(TEST_WORKFLOW)
    parsed = _workflow(TEST_WORKFLOW)

    push_section = workflow[workflow.index("  push:") : workflow.index("  pull_request:")]
    assert "branches:" in push_section
    assert '- "**"' in push_section
    assert "tags-ignore:" not in push_section
    assert "  workflow_dispatch:" in workflow
    assert parsed["jobs"]["verify"]["timeout-minutes"] >= 45


def _locked_versions(path: Path) -> dict[str, str]:
    matches = re.findall(
        r"(?m)^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
        path.read_text(encoding="utf-8"),
    )
    versions: dict[str, str] = {}
    for raw_name, version in matches:
        name = re.sub(r"[-_.]+", "-", raw_name).lower()
        assert name not in versions
        versions[name] = version
    assert versions
    return versions


def test_security_workflow_audits_the_hashed_dev_lock_once_without_invoking_pip():
    workflow = _workflow_text(TEST_WORKFLOW)
    commands = [line.strip() for line in workflow.splitlines() if "python -m pip_audit" in line]

    assert commands == [
        "python -m pip_audit --strict --progress-spinner off --require-hashes "
        "--disable-pip --timeout 30 -r requirements-dev-lock.txt"
    ]


def test_security_workflow_rejects_mobile_and_update_signing_material():
    workflow = _workflow_text(TEST_WORKFLOW)

    assert "pem|key|der|pk8|pkcs8|p8|ppk|p12|pfx|jks|keystore" in workflow
    assert "signing-private-key|release-credentials" in workflow
    assert "android/release\\.properties" in workflow


def test_android_sdk_install_reports_sdkmanager_status_instead_of_yes_broken_pipe():
    workflow = _workflow_text(TEST_WORKFLOW)

    assert "set +o pipefail" in workflow
    assert 'pipeline_status=("${PIPESTATUS[@]}")' in workflow
    assert "sdkmanager_status=${pipeline_status[1]}" in workflow
    assert "for attempt in 1 2 3" in workflow
    assert "sleep $((attempt * 2))" in workflow
    assert 'exit "$sdkmanager_status"' in workflow


@pytest.mark.parametrize("sdkmanager_status", [0, 23])
def test_android_sdk_install_block_ignores_yes_broken_pipe_but_propagates_sdkmanager(tmp_path, sdkmanager_status):
    executable = _bash_executable()
    if executable is None:
        pytest.skip("Bash is not installed on this test host")
    parsed = _workflow(TEST_WORKFLOW)
    step = next(
        item
        for item in parsed["jobs"]["android"]["steps"]
        if item.get("name") == "Install the pinned Android SDK components"
    )
    sdkmanager = tmp_path / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    sdkmanager.parent.mkdir(parents=True)
    sdkmanager.write_text(f"#!/usr/bin/env bash\nexit {sdkmanager_status}\n", encoding="utf-8")
    sdkmanager.chmod(0o755)
    environment = os.environ.copy()
    environment["ANDROID_HOME"] = tmp_path.as_posix()

    result = subprocess.run(
        [executable, "-e", "-o", "pipefail", "-c", step["run"]],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )

    assert result.returncode == sdkmanager_status, result.stderr


@pytest.mark.parametrize(
    "pattern",
    [
        "*.der",
        "*.pk8",
        "*.pkcs8",
        "*.p8",
        "*.ppk",
        "*.jks",
        "*.keystore",
        "*-signing-private-key.properties",
        "*-release-credentials.properties",
        "android/release.properties",
    ],
)
def test_gitignore_excludes_private_key_keystore_and_signing_property_formats(pattern):
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert pattern in rules


def test_runtime_lock_is_a_same_version_subset_of_the_audited_dev_lock():
    runtime = _locked_versions(ROOT / "requirements-lock.txt")
    development = _locked_versions(ROOT / "requirements-dev-lock.txt")

    assert runtime.items() <= development.items()


DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudflare.yml"


def test_cloudflare_deploy_waits_for_tests_on_main_and_serializes_production_deploys():
    parsed = _workflow(DEPLOY_WORKFLOW)
    trigger = parsed.get("on", parsed.get(True))
    assert trigger["workflow_run"]["workflows"] == ["tests"]
    assert trigger["workflow_run"]["types"] == ["completed"]
    assert trigger["workflow_run"]["branches"] == ["main"]
    assert "workflow_dispatch" in trigger
    assert parsed["concurrency"]["group"] == "cloudflare-site-deploy"
    assert parsed["concurrency"]["cancel-in-progress"] is False
    jobs = parsed["jobs"]
    assert set(jobs) == {"deploy"}
    assert jobs["deploy"]["if"] == (
        "github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'"
    )


def test_cloudflare_deploy_pins_wrangler_and_never_embeds_or_logs_secrets():
    workflow = _workflow_text(DEPLOY_WORKFLOW)
    assert "WRANGLER_VERSION: 4.118.0" in workflow
    assert workflow.count("wrangler@") == workflow.count("wrangler@${WRANGLER_VERSION}")
    assert "@latest" not in workflow
    assert "REFRESH_KEY" not in workflow
    assert "CLOUDFLARE_MARKET_REFRESH_KEY" not in workflow
    assert "x-refresh-key" not in workflow
    parsed = _workflow(DEPLOY_WORKFLOW)
    env = parsed["jobs"]["deploy"]["env"]
    assert env["CLOUDFLARE_API_TOKEN"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}"
    assert "CLOUDFLARE_MARKET_REFRESH_KEY" not in env
    assert "GH_TOKEN" not in env


def test_cloudflare_deploy_keeps_refresh_bindings_and_stages_only_the_pages_worker():
    parsed = _workflow(DEPLOY_WORKFLOW)
    steps = parsed["jobs"]["deploy"]["steps"]
    refresh = next(step for step in steps if "Deploy the quant-market-refresh worker" in step["name"])
    assert refresh["working-directory"] == "cloudflare/quant-dashboard"
    assert "--keep-vars" in refresh["run"]
    assert "CLOUDFLARE_API_TOKEN" in refresh["run"] and "CLOUDFLARE_ACCOUNT_ID" in refresh["run"]
    pages = next(step for step in steps if "Deploy the Cloudflare Pages worker" in step["name"])
    assert 'cp cloudflare/quant-dashboard/pages_worker.js "${pages_dir}/_worker.js"' in pages["run"]
    assert 'find "${pages_dir}" -type f | wc -l' in pages["run"]
    assert pages["run"].count("wrangler@") == 1  # pages worker deploy


def test_cloudflare_deploy_refuses_stale_main_and_verifies_every_endpoint():
    parsed = _workflow(DEPLOY_WORKFLOW)
    steps = parsed["jobs"]["deploy"]["steps"]
    guard = next(step for step in steps if step["name"] == "Refuse an outdated main revision")
    assert "gh api \"repos/${{ github.repository }}/commits/main\" --jq '.sha'" in guard["run"]
    assert "skip=true" in guard["run"]
    verify = next(step for step in steps if step["name"] == "Verify the deployed site")
    for endpoint in ("/api/methodology", "/api/health", "/api/meta", "https://quant.custard.top/"):
        assert endpoint in verify["run"]
    assert "jq -er '.methodology_version'" in verify["run"]
    assert "grep -qF" in verify["run"]


def test_cloudflare_deploy_bash_blocks_parse_with_bash():
    blocks = _bash_run_blocks(DEPLOY_WORKFLOW)
    executable = _bash_executable()
    if executable is None:
        pytest.skip("bash is not installed on this test host")
    for block in blocks:
        result = subprocess.run(
            [executable, "--noprofile", "--norc", "-n"],
            input=block,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"bash syntax failure:\n{block}\n{result.stderr}"
