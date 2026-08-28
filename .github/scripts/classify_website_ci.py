from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import sys


WEB_PREFIXES = ("cloudflare/quant-dashboard/", "cloudflare-cron/")
RUN_BOTH_FILES = frozenset(
    {
        ".github/scripts/classify_website_ci.py",
        ".github/workflows/tests.yml",
        "cloudflare/quant-dashboard/market_signing_public_key.txt",
    }
)
# The AI overlay has its own compact contract suite and R2 publication lane;
# keep those website-only changes out of the full market-data test matrix.
AI_SCREENING_FILES = frozenset(
    {
        "tools/ai_screening_contract.py",
        "tools/validate_ai_screening_public.py",
        "tools/calibrate_ai_screening_ranking.py",
        "tools/ai_screening_identity.py",
        "tools/audit_ai_screening_full.py",
        "tools/sanitize_ai_screening_identity.py",
        "tools/build_codex_luna_review.py",
        "tools/ai_company_research_knowledge.md",
        "tools/ai_screening_dual_channel_contract.py",
        "tools/publish_ai_screening.py",
        "tools/ai_screening_narrative.py",
        "tools/ai_screening_comparison.py",
        "tools/audit_ai_screening_sources.py",
        "tools/convert_luna_web_reviews.py",
        "tools/sanitize_codex_luna_reviews.py",
        "tests/test_ai_screening_contract.py",
        "tests/test_validate_ai_screening_public.py",
        "tests/test_convert_luna_web_reviews.py",
        "tests/test_ai_screening_release_contract.py",
        "tests/test_ai_screening_narrative.py",
        "tests/test_ai_screening_comparison.py",
        "tests/test_ai_screening_identity_audit.py",
        "tests/test_build_codex_luna_review.py",
        "tests/test_ai_screening_dual_channel_contract.py",
        "tests/test_sanitize_codex_luna_reviews.py",
        "tests/test_assemble_ai_screening_reviews.py",
        "tests/test_merge_ai_screening_reviews.py",
    }
)
WEB_FILES = (
    frozenset(
        {
            ".github/workflows/deploy-cloudflare.yml",
            "tests/test_cloudflare_dashboard.py",
            "tests/test_website_ci.py",
        }
    )
    | AI_SCREENING_FILES
)

# These products remain in the repository, but they are intentionally outside
# the website release gate while their development is paused.
PAUSED_PRODUCT_PREFIXES = ("android/", "desktop/", "ui/")
PAUSED_PRODUCT_TESTS = frozenset(
    {
        "app.py",
        "tests/test_android_release.py",
        "tests/test_build_desktop.py",
        "tests/test_desktop_installer.py",
        "tests/test_desktop_launcher.py",
        "tests/test_desktop_updater.py",
        "tests/test_streamlit_app.py",
        "tests/test_ui.py",
    }
)


@dataclass(frozen=True)
class WebsiteGates:
    web: bool
    data: bool


def classify_paths(paths: Iterable[str]) -> WebsiteGates:
    normalized = tuple(dict.fromkeys(path.strip().replace("\\", "/") for path in paths if path.strip()))
    if not normalized:
        raise ValueError("changed-file list is empty")

    web = False
    data = False
    for path in normalized:
        if path in RUN_BOTH_FILES:
            web = True
            data = True
        elif path in WEB_FILES or path.startswith(WEB_PREFIXES):
            web = True
        elif path in PAUSED_PRODUCT_TESTS or path.startswith(PAUSED_PRODUCT_PREFIXES):
            continue
        else:
            # data/, engine/, tools/ and every unclassified path fail closed
            # into the website data-integrity gate.
            data = True
    return WebsiteGates(web=web, data=data)


def _write_outputs(gates: WebsiteGates, output_path: Path | None) -> None:
    lines = (f"web={str(gates.web).lower()}", f"data={str(gates.data).lower()}")
    if output_path is None:
        print("\n".join(lines))
        return
    with output_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify changed files for the website CI gates.")
    parser.add_argument("--all", action="store_true", help="run both gates (manual or unbounded revision)")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    gates = WebsiteGates(web=True, data=True) if args.all else classify_paths(sys.stdin)
    _write_outputs(gates, args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
