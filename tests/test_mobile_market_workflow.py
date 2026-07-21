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
    (release / "manifest.json").write_text('{"schema_version":1}\n', encoding="utf-8")
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
    assert sorted(path.relative_to(pages).as_posix() for path in pages.rglob("*") if path.is_file()) == [
        "index.html",
        "mobile-data/manifest.json",
    ]


def test_mobile_publication_rechecks_close_time_hashes_signatures_and_exact_file_set():
    workflow = _workflow_text(MOBILE_WORKFLOW)

    assert "retrieval_time_oldest" in workflow
    assert "source_quote_timestamp_latest" in workflow
    assert "[TimeSpan]::FromHours(16)" in workflow
    assert "[TimeSpan]::FromHours(15)" in workflow
    assert workflow.count("Compare-Object $actual $expected") >= 2
    assert "does not match the signed manifest" in workflow
    assert "workflow signing secret does not match the public key pinned by the Android client" in workflow
    assert "Transferred manifest signature does not match the app-pinned key" in workflow
    assert "Previous manifest signature is invalid" in workflow
    assert "Previous asset $($entry[1]) is incomplete" in workflow
    assert "Public immutable asset $name differs from the verified build artifact" in workflow


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
        "build",
        "publish",
        "prepare_pages",
        "deploy_pages",
        "verify_cleanup",
        "archive_manifest",
    }
    assert all(job.get("if") == "github.ref == 'refs/heads/main'" for job in jobs.values())
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert jobs["prepare_pages"]["permissions"] == {"contents": "read", "pages": "write"}
    assert jobs["deploy_pages"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["verify_cleanup"]["permissions"] == {"contents": "write"}
    assert jobs["archive_manifest"]["permissions"] == {"contents": "write"}
    assert jobs["archive_manifest"]["needs"] == "verify_cleanup"
    assert jobs["build"]["environment"] == "mobile-production"
    assert jobs["deploy_pages"]["environment"]["name"] == "github-pages"
    assert "requirements-dev-lock.txt" not in workflow
    assert "timeout-minutes: 90" in workflow
    assert workflow.count("continue-on-error: true") == 2
    assert "persist-credentials: false" in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow


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
    assert "lookup_status} -eq 0" in branch_setup
    assert "lookup_status} -eq 2" in branch_setup
    assert "git fetch --no-tags --depth=1 origin refs/heads/mobile-data" in branch_setup
    assert "git switch --create mobile-data FETCH_HEAD" in branch_setup
    assert 'git switch --create mobile-data "${GITHUB_SHA}"' in branch_setup
    assert "--force" not in branch_setup
    assert "latest/manifest.json" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "gh release download mobile-market-data" in workflow
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

    assert triggers["schedule"] == [
        {"cron": "17 8 * * 1-5"},
        {"cron": "47 8 * * 1-5"},
        {"cron": "17 9 * * 1-5"},
    ]
    for path in (
        "data/cache/market_snapshot.json.gz",
        "data/cache/market_coldness",
        "data/cache/market_history",
        "data/cache/quality_history",
        "data/cache/growth_evidence",
        "data/cache/research_reports",
    ):
        assert workflow.count(path) == 2
    for contract in (
        "data/growth_evidence.py",
        "data/quality_history.py",
        "data/research_reports.py",
    ):
        assert workflow.count(contract) == 3


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

    assert re.search(r"actions/configure-pages@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/upload-pages-artifact@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/deploy-pages@[0-9a-f]{40}", workflow)
    assert "$attempt -le 6" in workflow
    assert "Start-Sleep -Seconds (5 * $attempt)" in workflow
    assert "gh release delete-asset $tag $name --repo $env:GH_REPO --yes" in workflow
    assert "manifest\\.json" in workflow


def test_ci_workflow_runs_on_every_branch_push_and_supports_manual_reverification():
    workflow = _workflow_text(TEST_WORKFLOW)

    push_section = workflow[workflow.index("  push:") : workflow.index("  pull_request:")]
    assert "branches:" in push_section
    assert '- "**"' in push_section
    assert "tags-ignore:" not in push_section
    assert "  workflow_dispatch:" in workflow


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
    assert "sdkmanager_status=${PIPESTATUS[1]}" in workflow
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
