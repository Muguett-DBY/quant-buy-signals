from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudflare.yml"
PUBLISH_AI_WORKFLOW = ROOT / ".github" / "workflows" / "publish-ai-screening.yml"
CLASSIFIER_PATH = ROOT / ".github" / "scripts" / "classify_website_ci.py"
MARKET_SIGNING_PUBLIC_KEY = ROOT / "cloudflare" / "quant-dashboard" / "market_signing_public_key.txt"
REFRESH_WORKER = ROOT / "cloudflare" / "quant-dashboard" / "refresh_worker.js"
DISPATCHER_CONFIG = ROOT / "cloudflare-cron" / "wrangler.toml"
REFRESH_CONFIG = ROOT / "cloudflare" / "quant-dashboard" / "wrangler.jsonc"


def _workflow(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _trigger(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


def _classifier_module():
    spec = importlib.util.spec_from_file_location("classify_website_ci", CLASSIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cloudflare_crons_use_unambiguous_weekday_names():
    dispatcher = tomllib.loads(DISPATCHER_CONFIG.read_text(encoding="utf-8"))
    refresh = json.loads(REFRESH_CONFIG.read_text(encoding="utf-8"))

    assert dispatcher["triggers"]["crons"] == ["15 8 * * MON-FRI"]
    assert refresh["triggers"]["crons"] == ["0 15 * * MON-FRI"]


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


@pytest.mark.parametrize(
    ("paths", "web", "data"),
    [
        (["cloudflare/quant-dashboard/pages_worker.js"], True, False),
        (["cloudflare-cron/worker.js"], True, False),
        (["cloudflare/quant-dashboard/market_signing_public_key.txt"], True, True),
        (["tests/test_cloudflare_dashboard.py"], True, False),
        (["tools/ai_screening_narrative.py"], True, False),
        (["tests/test_ai_screening_comparison.py"], True, False),
        ([".github/workflows/tests.yml"], True, True),
        ([".github/scripts/classify_website_ci.py"], True, True),
        (["data/fetcher.py"], False, True),
        (["engine/buy_screener.py"], False, True),
        (["tools/evidence_bundle.py"], False, True),
        (["unexpected/new-area.txt"], False, True),
        (["desktop/launcher.py", "android/app/build.gradle.kts"], False, False),
        (["app.py", "ui/buy_types_page.py", "tests/test_streamlit_app.py"], False, False),
        (["cloudflare/quant-dashboard/pages_worker.js", "data/fetcher.py"], True, True),
    ],
)
def test_website_change_classifier_is_fast_for_web_and_fail_closed_everywhere_else(paths, web, data):
    gates = _classifier_module().classify_paths(paths)

    assert gates.web is web
    assert gates.data is data


def test_website_change_classifier_rejects_an_empty_diff():
    with pytest.raises(ValueError, match="empty"):
        _classifier_module().classify_paths([])


def test_refresh_worker_uses_the_website_owned_market_signing_key():
    public_key = MARKET_SIGNING_PUBLIC_KEY.read_text(encoding="ascii").strip()
    refresh_worker = REFRESH_WORKER.read_text(encoding="utf-8")

    assert public_key
    assert f'base64Bytes("{public_key}")' in refresh_worker


def test_tests_workflow_has_one_authoritative_path_aware_website_gate():
    parsed = _workflow(TEST_WORKFLOW)
    trigger = _trigger(parsed)
    jobs = parsed["jobs"]

    assert trigger["push"]["branches"] == ["**"]
    assert "pull_request" in trigger
    assert "workflow_dispatch" in trigger
    assert set(jobs) == {"classify", "hygiene", "web", "data", "website-ci"}
    assert jobs["web"]["if"] == "needs.classify.outputs.web == 'true'"
    assert jobs["data"]["if"] == "needs.classify.outputs.data == 'true'"
    assert jobs["website-ci"]["if"] == "always()"
    assert jobs["website-ci"]["needs"] == ["classify", "hygiene", "web", "data"]

    classifier = next(step for step in jobs["classify"]["steps"] if step.get("name") == "Select required website gates")
    assert "git diff --no-renames --name-only" in classifier["run"]
    assert '"${EVENT_NAME}" == "push" && "${REF_NAME}" == "refs/heads/main"' in classifier["run"]
    main_push = classifier["run"].index('"${EVENT_NAME}" == "push"')
    assert "previous_tests_ok" in classifier["run"][main_push:]
    assert "gh api" in classifier["run"][main_push:]
    assert "using path-aware website CI classification" in classifier["run"][main_push:]
    assert "--all --github-output" in classifier["run"][main_push:]
    assert parsed["permissions"]["actions"] == "read"

    hygiene = next(
        step for step in jobs["hygiene"]["steps"] if step.get("name") == "Validate tracked files and line endings"
    )
    assert "git ls-files --eol" in hygiene["run"]
    assert "Tracked runtime artifact" in hygiene["run"]
    assert "Validate repository hygiene" not in {step.get("name") for step in jobs["data"]["steps"]}

    aggregate = jobs["website-ci"]["steps"][0]
    assert aggregate["env"]["HYGIENE_RESULT"] == "${{ needs.hygiene.result }}"
    assert '"${HYGIENE_RESULT}" != "success"' in aggregate["run"]
    assert '"${WEB_REQUIRED}" != "true" && "${WEB_REQUIRED}" != "false"' in aggregate["run"]
    assert '"${DATA_REQUIRED}" != "true" && "${DATA_REQUIRED}" != "false"' in aggregate["run"]

    data_tests = next(step for step in jobs["data"]["steps"] if step.get("name") == "Website Python tests")
    assert '-m "not desktop and not android and not parked_client"' in data_tests["run"]
    for parked_test in (
        "test_android_release.py",
        "test_build_desktop.py",
        "test_desktop_installer.py",
        "test_desktop_launcher.py",
        "test_desktop_updater.py",
        "test_streamlit_app.py",
        "test_ui.py",
    ):
        assert f"--ignore=tests/{parked_test}" in data_tests["run"]
    all_workflow_text = TEST_WORKFLOW.read_text(encoding="utf-8")
    assert "Build and smoke-test Windows desktop release" not in all_workflow_text
    assert "Verify tracked desktop source archive" not in all_workflow_text
    assert "setup-java" not in all_workflow_text
    assert "gradlew" not in all_workflow_text


def test_web_gate_is_small_and_matches_the_predeployment_contract_gate():
    tests = _workflow(TEST_WORKFLOW)
    deploy = _workflow(DEPLOY_WORKFLOW)
    web_steps = tests["jobs"]["web"]["steps"]
    predeploy_steps = deploy["jobs"]["deploy"]["steps"]
    web_run = next(step for step in web_steps if step.get("name") == "Worker syntax and website contracts")["run"]
    predeploy_run = next(
        step for step in predeploy_steps if step.get("name") == "Verify both workers and the dashboard contract tests"
    )["run"]

    expected_tests = "tests/test_cloudflare_dashboard.py tests/test_website_ci.py"
    assert expected_tests in web_run
    for ai_test in (
        "tests/test_ai_screening_narrative.py",
        "tests/test_ai_screening_comparison.py",
        "tests/test_ai_screening_source_audit.py",
        "tests/test_merge_ai_screening_reviews.py",
    ):
        assert ai_test in web_run
    assert expected_tests in predeploy_run
    assert "requirements-dev-lock.txt" not in web_run
    assert "node --check cloudflare-cron/worker.js" in web_run
    assert "node --check cloudflare/quant-dashboard/pages_worker.js" in web_run
    assert "node --check cloudflare/quant-dashboard/refresh_worker.js" in web_run


def test_cloudflare_deploy_waits_for_the_tests_workflow_and_has_no_direct_push_trigger():
    parsed = _workflow(DEPLOY_WORKFLOW)
    trigger = _trigger(parsed)

    assert trigger["workflow_run"]["workflows"] == ["tests"]
    assert trigger["workflow_run"]["types"] == ["completed"]
    assert trigger["workflow_run"]["branches"] == ["main"]
    assert "push" not in trigger
    assert "workflow_dispatch" not in trigger
    assert parsed["concurrency"] == {"group": "cloudflare-site-deploy", "cancel-in-progress": False}
    assert parsed["jobs"]["deploy"]["if"] == "github.event.workflow_run.conclusion == 'success'"


def test_cloudflare_deploy_is_pinned_fail_closed_and_verifies_the_live_site():
    parsed = _workflow(DEPLOY_WORKFLOW)
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    steps = parsed["jobs"]["deploy"]["steps"]

    assert "WRANGLER_VERSION: 4.118.0" in workflow
    assert workflow.count("wrangler@") == workflow.count("wrangler@${WRANGLER_VERSION}")
    assert "@latest" not in workflow
    assert "REFRESH_KEY" not in workflow
    assert "Skip pushes that did not touch the website" not in workflow
    assert "steps.paths" not in workflow

    job_env = parsed["jobs"]["deploy"]["env"]
    assert "CLOUDFLARE_API_TOKEN" not in job_env
    assert "CLOUDFLARE_ACCOUNT_ID" not in job_env

    refresh = next(step for step in steps if "Deploy the quant-market-refresh worker" in step["name"])
    dispatcher = next(step for step in steps if "Deploy the post-close website data dispatcher" in step["name"])
    pages = next(step for step in steps if "Deploy the Cloudflare Pages worker" in step["name"])
    for deploy_step in (refresh, dispatcher, pages):
        assert deploy_step["env"] == {
            "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}",
            "CLOUDFLARE_ACCOUNT_ID": "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}",
        }
    verify = next(step for step in steps if step["name"] == "Verify the deployed site")
    assert refresh["working-directory"] == "cloudflare/quant-dashboard"
    assert dispatcher["working-directory"] == "cloudflare-cron"
    assert "--keep-vars" in refresh["run"]
    assert "--keep-vars" in dispatcher["run"]
    assert 'cp cloudflare/quant-dashboard/pages_worker.js "${pages_dir}/_worker.js"' in pages["run"]
    assert "node --check cloudflare-cron/worker.js" in workflow
    for endpoint in (
        "/api/methodology",
        "/api/health",
        "/api/meta",
        "https://quant.custard.top/",
        "https://quant.custard.top/ai-screening",
    ):
        assert endpoint in verify["run"]
    assert 'grep -qF "AI"' in verify["run"]
    upload_ai = next(
        step for step in steps if "Upload the optional generation-bound AI screening overlay" in step["name"]
    )
    preflight_ai = next(
        step for step in steps if "Preflight the optional generation-bound AI screening overlay" in step["name"]
    )
    assert upload_ai["env"] == {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}",
        "CLOUDFLARE_ACCOUNT_ID": "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}",
    }
    assert "r2 object put" in upload_ai["run"]
    assert upload_ai["id"] == "ai_overlay"
    assert "quant-market-data/ai-screening/${generation}.json" in upload_ai["run"]
    assert preflight_ai["id"] == "ai_preflight"
    assert steps.index(preflight_ai) < steps.index(refresh)
    assert steps.index(preflight_ai) < steps.index(dispatcher)
    assert steps.index(preflight_ai) < steps.index(pages)
    assert steps.index(upload_ai) < steps.index(pages)
    assert "python -m tools.validate_ai_screening_public" in preflight_ai["run"]
    assert "33554432" in preflight_ai["run"]
    assert ".source_audit.audit_passed == true" in preflight_ai["run"]
    assert ".source_audit.audit_contract_version == 3" in preflight_ai["run"]
    assert ".source_audit.projection_sha256" in preflight_ai["run"]
    assert ".source_audit.projection_company_count == .candidate_total" in preflight_ai["run"]
    assert ".source_audit.projection_claim_count" in preflight_ai["run"]
    assert ".source_audit.projection_search_finding_count" in preflight_ai["run"]
    assert ".source_audit.projection_source_reference_count" in preflight_ai["run"]
    assert ".source_audit.projection_unique_url_count" in preflight_ai["run"]
    assert ".source_audit.semantic_failed_count == 0" in preflight_ai["run"]
    assert ".source_audit.semantic_unverified_count == 0" in preflight_ai["run"]
    assert ".source_audit.company_coverage" in preflight_ai["run"]
    assert ".referenced_no_source_finding_ids" in preflight_ai["run"]
    assert ".source_audit.audit_sha256" in preflight_ai["run"]
    assert "AI screening seed targets" in preflight_ai["run"]
    assert "seed_changed=${seed_changed}" in preflight_ai["run"]
    assert "Changed AI screening seed targets" in preflight_ai["run"]
    assert "stale_seed=true" in preflight_ai["run"]
    assert "stale AI screening seed passed the current Worker contract" in preflight_ai["run"]
    assert preflight_ai["run"].index("python -m tools.validate_ai_screening_public") < preflight_ai["run"].index(
        "stale AI screening seed passed the current Worker contract"
    )
    assert 'echo "uploaded=true"' in upload_ai["run"]
    assert "steps.ai_preflight.outputs.seed_changed" in verify["run"]
    assert "steps.ai_overlay.outputs.uploaded" in verify["run"]
    assert "ai-screening-generation" in verify["run"]
    assert "/api/health?deep=1" in verify["run"]
    assert ".integrity_checked == true" in verify["run"]
    assert ".type_pair_reviewed_count == .type_pair_candidate_total" in verify["run"]
    assert ".type_pair_candidate_total == .type_pair_expected_total" in verify["run"]
    assert ".candidate_offset == 0" in verify["run"]
    assert ".type_pair_unreviewed_count == 0" in verify["run"]
    assert '.review_mode == "codex_luna_web_review"' in verify["run"]
    assert '.review_mode == "opencode_native_company_research_review"' in verify["run"]
    assert ".reviewed_without_web_search == 0" in verify["run"]
    assert ".full_coverage_web_search == true" in verify["run"]
    assert ".web_search_attempted_count == .candidate_total" in verify["run"]
    assert ".web_search_event_verified_count == .candidate_total" in verify["run"]
    assert ".type_pair_web_search_attempted_count == .type_pair_candidate_total" in verify["run"]
    assert ".type_pair_web_search_event_verified_count == .type_pair_candidate_total" in verify["run"]
    assert ".research_source_urls_verified_count == .candidate_total" in verify["run"]
    assert ".type_pair_research_source_urls_verified_count == .type_pair_candidate_total" in verify["run"]
    assert ".type_pair_web_search_claim_urls_verified_count == .type_pair_candidate_total" in verify["run"]


def test_ai_overlay_release_skips_stale_generation_without_publishing_it():
    publish = _workflow(PUBLISH_AI_WORKFLOW)
    publish_text = PUBLISH_AI_WORKFLOW.read_text(encoding="utf-8")
    publish_steps = publish["jobs"]["publish"]["steps"]
    preflight = next(step for step in publish_steps if step.get("id") == "preflight")
    upload = next(step for step in publish_steps if "Upload the AI overlay" in step["name"])
    pages = next(step for step in publish_steps if "Deploy only the Pages AI reader" in step["name"])
    verify = next(step for step in publish_steps if step.get("name") == "Verify the published AI API")

    assert 'echo "stale=false"' in preflight["run"]
    assert 'echo "stale=true"' in preflight["run"]
    assert "leaving the generation-bound overlay unchanged" in preflight["run"]
    for step in (upload, pages, verify):
        assert step["if"] == "steps.preflight.outputs.stale != 'true'"
    assert "does not match the live market generation" not in publish_text
    assert "generation-bound" in publish_text


def test_cloudflare_deploy_does_not_fail_on_a_changed_but_stale_ai_seed():
    deploy = _workflow(DEPLOY_WORKFLOW)
    deploy_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    steps = deploy["jobs"]["deploy"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "ai_preflight")
    verify = next(step for step in steps if step.get("name") == "Verify the deployed site")

    assert 'echo "stale_seed=false"' in preflight["run"]
    assert 'echo "stale_seed=true"' in preflight["run"]
    assert "continuing the website deployment" in preflight["run"]
    assert "steps.ai_preflight.outputs.stale_seed" in verify["run"]
    assert "refusing to publish a stale overlay" in deploy_text


def test_cloudflare_deploy_bash_blocks_parse_with_bash():
    executable = _bash_executable()
    if executable is None:
        pytest.skip("Bash is not installed on this test host")
    parsed = _workflow(DEPLOY_WORKFLOW)
    blocks = [
        step["run"]
        for step in parsed["jobs"]["deploy"]["steps"]
        if isinstance(step.get("run"), str) and step.get("shell") == "bash"
    ]
    assert blocks
    for block in blocks:
        result = subprocess.run(
            [executable, "--noprofile", "--norc", "-n"],
            input=block,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"bash syntax failure:\n{block}\n{result.stderr}"
