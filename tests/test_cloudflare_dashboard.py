from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js"
REFRESH_WORKER = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "refresh_worker.js"


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
    assert "estimated_sub_scores" in source
    assert "estimated_sub_score_reasons" in source
    assert "未确认估算" in source
    assert "待仓位确认" in source
    assert "position_guidance" in source
    assert "资料不足" in source
    assert "dimensionScoresAvailable" in source
    assert "数据版本过旧" in source
    assert "missing_dimensions" in source
    assert "missingScore=missing.has(dimension)||!hasScore" in source
    assert "function scoreLabel" in source
    assert "估算范围 " in source
    assert "综合诊断分" in source
    assert "仅供排序" in source
    assert "if(typeResult.score!=null&&missing.length===0)" in source


def test_dashboard_defaults_to_real_triggers_and_explains_a_true_zero_result():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert '<option value="triggered">实际命中</option>' in source
    assert "typeState.status===s" in source
    assert "当前条件没有公司；这表示真实零命中或没有适用记录" in source
    assert "displayedScore" in source
    assert "r.types?.[t]?.reason" in source


def test_dashboard_health_separates_data_time_from_mirror_checks_and_verifies_all_assets():
    source = DASHBOARD.read_text(encoding="utf-8")

    for field in (
        "data_generated_at",
        "generation_published_at",
        "last_mirror_check_at",
        "data_age_hours",
        "stale",
        "signals_bytes",
        "signature_bytes",
    ):
        assert field in source
    assert "catalogueOk&&signalsOk&&signatureOk" in source
    assert "updated_at:generation?.data_timestamp_utc" in source
    assert 'requestedGeneration?"public, max-age=300, immutable":"no-store"' in source


def test_refresh_worker_repairs_missing_objects_even_when_generation_is_unchanged():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert 'status: repaired ? "repaired" : "unchanged"' in source
    assert "existing.size !== expectedSize" in source
    assert "await env.DATA_BUCKET.put(key, body" in source
