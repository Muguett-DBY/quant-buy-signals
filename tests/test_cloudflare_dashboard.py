from pathlib import Path
import json
import re
import shutil
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js"
REFRESH_WORKER = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "refresh_worker.js"
WRANGLER_CONFIG = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "wrangler.jsonc"


def test_dashboard_embedded_browser_script_has_valid_javascript_syntax():
    source = DASHBOARD.read_text(encoding="utf-8")
    script = re.search(r"<script>\s*(.*?)\s*</script>", source, flags=re.DOTALL)

    assert script is not None
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the dashboard browser script"
    result = subprocess.run(
        [node, "--check", "-"],
        input=script.group(1).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


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
    assert "参考范围 " in source
    assert "待仓位确认" in source
    assert "position_guidance" in source
    assert "资料不足" in source
    assert "dimensionScoresAvailable" in source
    assert "数据版本过旧" in source
    assert "missing_dimensions" in source
    assert "missingScore=missing.has(dimension)||!hasScore" in source
    assert "function scoreLabel" in source
    assert "参考范围 " in source
    assert "综合诊断分" in source
    assert "if(typeResult.score!=null&&!hasMissing)" in source
    assert "沪深股票收盘后数据的只读展示" not in source
    assert "精确分只来自已核验资料" not in source
    assert "未确认估算" not in source


def test_dashboard_defaults_to_real_triggers_and_explains_a_true_zero_result():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert '<option value="triggered">实际命中</option>' in source
    assert "typeState.status===s" in source
    assert 's=q?"":$("status").value' in source
    assert "当前确实为 0 家命中" in source
    assert "没有找到匹配该代码或名称的公司" in source
    assert "displayedScore" in source
    assert "点击查看完整依据" in source


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
    assert 'requestedGeneration?"public, max-age=31536000, immutable":"no-store"' in source


def test_dashboard_loads_a_lightweight_index_and_company_details_on_demand():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'fetch("/api/catalogue-index?generation_id="' in source
    assert 'fetch("/api/company/"+encodeURIComponent(code)' in source
    assert 'if(path==="/api/catalogue-index")' in source
    assert r"path.match(/^\/api\/company\/([036][0-9]{5})$/)" in source
    assert 'if(path==="/api/catalogue")' in source
    assert "const pageSize=50" in source
    assert '$("rows").addEventListener("click"' in source
    assert 'tr.addEventListener("click"' not in source


def test_dashboard_formats_beijing_time_and_avoids_mobile_input_overflow():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'timeZone:"Asia/Shanghai"' in source
    assert "（北京时间）" in source
    assert "width:100%;max-width:100%" in source
    assert ".drawer-head{display:flex" in source
    assert "position:sticky;top:0" in source
    assert "body.drawer-open{overflow:hidden}" in source


def test_dashboard_search_is_frame_scheduled_and_ime_safe():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "requestAnimationFrame" in source
    assert '"compositionstart"' in source
    assert '"compositionend"' in source
    assert "if(!composing)scheduleRender()" in source


def test_refresh_worker_repairs_missing_objects_even_when_generation_is_unchanged():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert 'status: repaired ? "repaired" : "unchanged"' in source
    assert "existing.size !== expectedSize" in source
    assert "await env.DATA_BUCKET.put(key, body" in source


def test_refresh_worker_deployment_uses_real_bindings_without_a_plaintext_key():
    config = json.loads(WRANGLER_CONFIG.read_text(encoding="utf-8"))

    assert config["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "quant-market-data",
            "database_id": "1ea1f08e-640f-4e75-a25e-c47d0a41ae66",
        }
    ]
    assert config["r2_buckets"] == [{"binding": "DATA_BUCKET", "bucket_name": "quant-market-data"}]
    assert "vars" not in config
