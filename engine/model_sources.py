"""Public rule-source contract shared by the website publisher and Worker tests."""

from __future__ import annotations

from typing import Any

AUDIT_SCHEMA_VERSION = 6
PATCH6_SOURCE_PATH = r"E:\模板汇总MD\补丁6· 公司三属性分类与三维度量化打分机制.md"
PATCH6_SOURCE_SHA256 = "dfade9961a182bfff67f95e2f8d55fd637cf8a15cedd44c12300b4f9c4c1549b"
PATCH7_SOURCE_PATH = r"E:\模板汇总MD\补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md"
PATCH7_SOURCE_SHA256 = "69b6bbeaa44755b9935518c665bc1ac0cac5c473aaba5b106bdf0f9fc88beb6d"
TYPE7_SOURCE_DOCUMENTS = {
    "template1": {
        "path_at_model_authoring": r"E:\模板汇总MD\第1模板.md",
        "sha256": "98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d",
    },
    "template5": {
        "path_at_model_authoring": r"E:\模板汇总MD\第5模板.md",
        "sha256": "37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946",
    },
    "patch5": {
        "path_at_model_authoring": r"E:\模板汇总MD\补丁5.md",
        "sha256": "8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4",
    },
    "patch6": {
        "path_at_model_authoring": PATCH6_SOURCE_PATH,
        "sha256": PATCH6_SOURCE_SHA256,
    },
    "patch7": {
        "path_at_model_authoring": PATCH7_SOURCE_PATH,
        "sha256": PATCH7_SOURCE_SHA256,
    },
    "subsequent_addenda": {
        "path_at_model_authoring": r"E:\模板汇总MD\后续附加补丁们.md",
        "sha256": "0dea9125bbe2039acf741ac997e62b53c49b6e3dc32e7d956ed96f9d7054b64f",
    },
}
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
