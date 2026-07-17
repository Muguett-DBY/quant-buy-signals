from pathlib import Path

from engine.buy_screener import TYPE_PRIORITY, TYPE_WEIGHTS


ROOT = Path(__file__).resolve().parents[1]


def test_model_documentation_covers_runtime_weights_and_priority():
    document = (ROOT / "docs" / "MODEL.md").read_text(encoding="utf-8")

    for type_key, weights in TYPE_WEIGHTS.items():
        type_number = type_key.removeprefix("type")
        row = next(line for line in document.splitlines() if line.startswith(f"| Type {type_number} "))
        for dimension, weight in weights.items():
            assert dimension in row
            assert f"{weight:.0%}" in row

    rendered_priority = " > ".join(key.replace("type", "Type ") for key in TYPE_PRIORITY)
    assert rendered_priority in document


def test_model_documentation_states_core_formula_and_missing_data_boundaries():
    document = (ROOT / "docs" / "MODEL.md").read_text(encoding="utf-8")

    assert "WACC =" in document
    assert "TerminalValue_N" in document
    assert "JustifiedPB =" in document
    assert "恰好三条 `bear_case`" in document
    assert "不会把缺失资本开支当作零" in document
