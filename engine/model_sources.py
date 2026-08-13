"""Public rule-source contract shared by the website publisher and Worker tests."""

from __future__ import annotations

from typing import Any

from engine.audit import AUDIT_SCHEMA_VERSION, TYPE7_SOURCE_DOCUMENTS


MODEL_SOURCE_CONTRACT_SCHEMA_VERSION = 1


def public_model_source_contract() -> dict[str, Any]:
    """Return the immutable rule documents and precedence for one web generation."""

    documents = {
        key: {
            "filename": str(value["path_at_model_authoring"]).replace("\\", "/").rsplit("/", 1)[-1],
            "sha256": str(value["sha256"]),
        }
        for key, value in TYPE7_SOURCE_DOCUMENTS.items()
    }
    return {
        "schema_version": MODEL_SOURCE_CONTRACT_SCHEMA_VERSION,
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "documents": documents,
        "precedence": [
            "current_independent_patch6_and_patch7",
            "independent_templates_and_patches_1_to_5",
            "subsequent_addenda_unique_content",
            "historical_aggregations",
        ],
        "resolutions": {
            "type5": "later_specialised_appendix_supersedes_early_5c_thresholds",
            "type6": "five_dimension_quantification_is_the_executable_rule_without_a_hidden_second_score",
            "missing_evidence": "fail_closed_without_invented_values",
        },
        "scope": {
            "implemented": "prospective_buy_and_add_screening",
            "excluded": "holder_specific_sell_gate",
            "no_buy_is_not_sell": True,
        },
    }
