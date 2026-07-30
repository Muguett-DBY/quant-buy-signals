from pathlib import Path
import json
import shutil
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js"
REFRESH_WORKER = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "refresh_worker.js"
WRANGLER_CONFIG = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "wrangler.jsonc"


def test_dashboard_embedded_browser_script_has_valid_javascript_syntax():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the dashboard browser script"
    validator = r"""
let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;
const response = await worker.fetch(new Request("https://dashboard.test/"), {});
if (response.status !== 200) throw new Error("root response was not successful");
const html = await response.text();
const match = html.match(/<script>\s*([\s\S]*?)\s*<\/script>/);
if (!match) throw new Error("generated dashboard script was not found");
if (html.includes("__QUANT_METHODOLOGY_")) throw new Error("template token leaked");
new Function(match[1]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
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
    assert "const exact=finiteNumber(typeResult.score)" in source
    assert "if(exact!==null&&!hasMissing)" in source
    assert "沪深股票收盘后数据的只读展示" not in source
    assert "精确分只来自已核验资料" not in source
    assert "function cleanedEstimateReason" in source
    assert "未核验参考 " in source
    assert "该值只帮助定位缺口，不参与触发或否决。" in source


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
        "manifest_ok",
        "manifest_bytes",
        "signals_bytes",
        "signature_bytes",
    ):
        assert field in source
    assert "manifestRecord.ok&&catalogueOk&&signalsOk&&signatureOk" in source
    assert "actual!==expected||(marker&&marker!==expected)" in source
    assert "manifest?.company_details&&marker!==expected" in source
    assert source.index("object.size>MAX_MANIFEST_BYTES") < source.index("object.arrayBuffer()")
    assert "object.size!==bytes.byteLength" in source
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
    assert 'cacheKey=new Request(request.url,{method:"GET"})' in source
    assert "headSafeResponse(request" in source


def test_dashboard_formats_beijing_time_and_avoids_mobile_input_overflow():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'timeZone:"Asia/Shanghai"' in source
    assert "（北京时间）" in source
    assert "width:100%;max-width:100%" in source
    assert ".drawer-head{display:flex" in source
    assert "position:sticky;top:0" in source
    assert "body.drawer-open{overflow:hidden}" in source
    assert ".filters input,.filters select{font-size:16px}" in source


def test_dashboard_explains_methodology_and_separates_scope_data_and_action_gaps():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'if(path==="/api/methodology")' in source
    assert "METHODOLOGY_VERSION" in source
    for text in (
        "指标含义",
        "所需数据",
        "评分方向",
        "公司实际输入",
        "数据批次",
        "来源追溯",
        "触发阈值",
        "计算方式",
        "总分贡献",
        "这是公司的适用边界，不是缺失公司资料",
        "模型覆盖缺口",
        "需要建立相应专属模型后才能评价",
        "action_confirmation_company_count",
        "decision_relevant_gap_company_count",
        "待满足附加条件",
        "其中待确认仓位",
        "有客观资料缺口",
        "缺口可能改变结论",
        "数据覆盖",
        "评分质量",
        "股价透支计算",
    ):
        assert text in source
    assert 'dataMissing=missing.filter(dimension=>!(dimension==="6e"&&actionRequired))' in source
    assert "declaredIncomplete=value?.applicable===true&&value?.evidence_complete===false" in source
    assert 'state.key==="type6"&&company.types?.type6?.status==="conditional"' in source
    assert 'conditionalLabel=key==="type6"?"等待确认仓位":"待满足附加条件"' in source
    assert "EVIDENCE_META_NAMES" in source
    assert "v.reasons?.[dimension]||subReasons[dimension]" in source


def test_dashboard_uses_plain_language_version_and_exposes_only_traceable_detail_facts():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'const METHODOLOGY_LABEL="七类量化买入方法（2026年7月）"' in source
    assert '" · 量化口径："+METHODOLOGY_LABEL' in source
    assert '" · 量化口径："+METHODOLOGY_VERSION' not in source
    assert '.replace("__QUANT_METHODOLOGY_LABEL__",METHODOLOGY_LABEL)' in source
    assert 'addFact(facts,"行情日期",String(r.source_trade_date||marketAsOf||"—"))' in source
    assert 'addFact(facts,"可追溯版本",sourceVersion||"—")' in source
    assert "该子指标的单独财报报告期未随公开详情提供" in source
    assert "公开详情未附该子指标的单独来源链接" in source
    assert "不会猜测补全" in source


def test_dashboard_distinguishes_model_coverage_from_scope_and_type7_independent_gates():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "function isModelCoverageGap" in source
    assert 'typeKey==="type7"&&/金融/.test(explanation)' in source
    assert 'scope.classList.add("model-gap")' in source
    assert "三项不可相互抵消" in source
    assert "独立门槛：严格高于7.0分" in source
    assert '&&!independentGate)addDefinition(definitions,"总分贡献"' in source
    assert '&&independentGate)addDefinition(definitions,"门槛结果"' in source


def test_dashboard_focus_pagination_search_and_zero_bar_interactions_are_explicit():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "min-width:2px" not in source
    assert "if(visibleCount===0)fill.hidden=true" in source
    assert "function syncSearchStatus()" in source
    assert '$("status").disabled=searching' in source
    assert "代码/名称搜索会跨全部状态，状态筛选暂时停用。" in source
    assert "function changePage(delta)" in source
    assert '$("resultsPanel").scrollIntoView({block:"start"})' in source
    assert '$("resultMeta").focus({preventScroll:true})' in source
    assert '$("prev").onclick=()=>changePage(-1)' in source
    assert '$("next").onclick=()=>changePage(1)' in source
    assert "function trapDrawerFocus(event)" in source
    assert "setBackgroundInert(true)" in source
    assert "setBackgroundInert(false)" in source
    assert "target&&target.isConnected" in source
    assert "trapDrawerFocus(event)" in source


def test_dashboard_does_not_trap_vertical_scroll_and_rejects_stale_detail_responses():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert ".table-wrap{overflow-x:auto;overflow-y:visible" in source
    assert "overscroll-behavior-y:auto" in source
    assert "max-height:calc(100dvh - 24px)" in source
    assert "new AbortController()" in source
    assert "detailAbort.abort()" in source
    assert "requestId!==activeDetailRequest||activeDetailCode!==code" in source


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
    assert "existing.customMetadata?.sha256" in source
    assert "await env.DATA_BUCKET.put(key, body" in source


def test_refresh_worker_switches_generation_with_one_idempotent_d1_transaction():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert "ON CONFLICT(generation_id) DO UPDATE SET last_checked_at" in source
    assert "WHERE generations.market_as_of = excluded.market_as_of" in source
    assert "WHERE EXISTS (" in source
    assert "await env.DB.batch([generationStatement, pointerStatement])" in source
    assert "generation database transaction did not commit one consistent pointer" in source


def test_cloudflare_pipeline_validates_mirrors_and_serves_all_company_detail_shards():
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    refresh = REFRESH_WORKER.read_text(encoding="utf-8")

    for source in (dashboard, refresh):
        assert "sha256_code_first_nibble" in source
        assert "company_detail_v2" in source
        assert "company-details-" in source
        assert "shards.length!==16" in source or "shards.length !== 16" in source
    assert "readCompanyDetail" in dashboard
    assert "companyDetailShardId" in dashboard
    assert "company_details_ready" in dashboard
    assert 'detailContract="company_detail_v2"' in dashboard
    assert "companyDetailAssets" in refresh
    assert 'downloadAsset(metadata.filename, "company_detail", metadata)' in refresh
    assert "verifyCompanyDetailPayloads" in refresh
    assert "uncompressed checksum mismatch" in refresh
    assert "assigned to the wrong detail shard" in refresh
    assert "company detail canonical root" in refresh
    assert "DETAIL_DOWNLOAD_BATCH_SIZE = 4" in refresh
    assert "readBoundedStream" in refresh
    assert "verifyManifestSignature(sourceManifestBytes, signatureBytes)" in refresh
    assert refresh.index("verifyManifestSignature(sourceManifestBytes, signatureBytes)") < refresh.index(
        'downloadAsset(catalogueName, "catalogue"'
    )


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
