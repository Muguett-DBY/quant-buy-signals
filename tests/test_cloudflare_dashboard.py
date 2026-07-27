from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js"


def test_dashboard_select_filters_use_change_events_and_type_scoped_statuses():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'addEventListener("change"' in source
    assert 'typeState.status!=="not_applicable"' in source
    assert "typeState.status===s" in source


def test_dashboard_contract_contains_dimension_labels_and_sub_score_rendering():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "TYPE_DIMENSIONS" in source
    for label in ("买入区深度", "底部信号", "技术壁垒", "长期质量与回报", "商业质量与安全边际"):
        assert label in source
    assert "sub_scores" in source
    assert "sub_score_reasons" in source
    assert "资料不足" in source
