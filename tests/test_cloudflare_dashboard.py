from pathlib import Path
import json
import re
import shutil
import sqlite3
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "pages_worker.js"
REFRESH_WORKER = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "refresh_worker.js"
WRANGLER_CONFIG = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "wrangler.jsonc"
SCHEMA = PROJECT_ROOT / "cloudflare" / "quant-dashboard" / "schema.sql"
TRADING_CALENDAR = PROJECT_ROOT / "tools" / "china_a_share_trading_calendar.json"


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
const match = html.match(/<script nonce="([^"]+)">\s*([\s\S]*?)\s*<\/script>/);
if (!match) throw new Error("generated dashboard script was not found");
const nonce = match[1];
const csp = response.headers.get("content-security-policy") || "";
if (!csp.includes("script-src 'nonce-" + nonce + "'")) throw new Error("script nonce is not bound to CSP");
if (!csp.includes("style-src 'nonce-" + nonce + "'")) throw new Error("style nonce is not bound to CSP");
if (csp.includes("unsafe-inline")) throw new Error("CSP permits unsafe inline content");
if (!html.includes('<style nonce="' + nonce + '">')) throw new Error("style nonce is missing");
if (response.headers.get("x-content-type-options") !== "nosniff") throw new Error("nosniff header is missing");
if (response.headers.get("x-frame-options") !== "DENY") throw new Error("frame protection is missing");
if (html.includes("__QUANT_METHODOLOGY_") || html.includes("__CATALOGUE_INDEX_CONTRACT_VERSION__") || html.includes("__QUANT_CSP_NONCE__")) throw new Error("template token leaked");
new Function(match[2]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_dashboard_sanitizes_legacy_machine_reasons_before_browser_rendering():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the dashboard reason sanitizer"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;
const response = await worker.fetch(new Request("https://dashboard.test/"), {});
assert.equal(response.status, 200);
const html = await response.text();
const scriptMatch = html.match(/<script nonce="[^"]+">\s*([\s\S]*?)\s*<\/script>/);
assert.ok(scriptMatch, "generated dashboard script was not found");
const script = scriptMatch[1];
const start = script.indexOf("function publicReasonText(value)");
const end = script.indexOf("function addFact", start);
assert.ok(start >= 0 && end > start, "reason sanitizer source was not found");
const helpers = new Function(script.slice(start, end) + ";return {publicReasonText,publicClassName};")();
const { publicReasonText, publicClassName } = helpers;

assert.equal(publicReasonText("证据:patch6-observable"), "可核验的财务与行业数据");
assert.equal(publicReasonText("坡的长度是短板(证据:patch6-observable)"), "坡的长度是短板");
assert.equal(
  publicReasonText("坡的长度是短板；model_id=patch6-type7-classified-equity-v1"),
  "坡的长度是短板",
);
assert.equal(publicReasonText("schema_version=1"), "可核验的财务与行业数据");
assert.equal(publicReasonText("derived_proxy"), "根据财务表现间接判断");
assert.equal(
  publicReasonText("financial_fade_horizon_not_tam_or_penetration_proof"),
  "可核验的财务与行业数据",
);
assert.equal(
  publicReasonText("(normalised_roe - g) / (cost_of_equity - g)"),
  "可核验的财务与行业数据",
);
assert.equal(publicReasonText("证据:patch6-type2c-quantity-price-v1"), "量价与换手数据");
for (const readable of ["PB分位与库存去化", "行业营收与利润数据", "现金流改善；收入增速回升"]) {
  assert.equal(publicReasonText(readable), readable);
}
for (const legacy of ["patch6-observable", "model_id", "schema_version", "derived_proxy"]) {
  assert.ok(!publicReasonText("中文说明；" + legacy).includes(legacy), legacy);
}
assert.deepEqual(["C", "T", "N", "W"].map(publicClassName), ["强周期", "强科技", "弱周期", "弱周期"]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    assert "publicReasonText(v.reason)" in source
    assert "publicReasonText(v.evidence_gap)" in source
    assert "publicValue=publicReasonText(value)" in source
    assert "publicReasonText(v.reasons?.[dimension]||subReasons[dimension])" in source
    assert ".map(publicReasonText).filter(Boolean)" in source
    assert "text.textContent=String(value)" not in source


def test_catalogue_index_enforces_contract_and_aggregates_action_and_evidence_gaps():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the dashboard worker route"
    validator = r"""
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;
const cached = new Map();
globalThis.caches = {
  default: {
    match: async request => cached.get(request.url)?.clone(),
    put: async (request, response) => { cached.set(request.url, response.clone()); },
  },
};
const unresolvedDecision = {
  decision_complete: false,
  decision_basis: "unresolved_missing_evidence",
  potentially_triggerable: true,
  score_lower_bound: 0,
  score_upper_bound: 8,
  missing_dimensions: ["3b"],
};
const confirmedVetoDecision = {
  decision_complete: true,
  decision_basis: "confirmed_veto",
  potentially_triggerable: false,
  score_lower_bound: 0,
  score_upper_bound: 4,
  missing_dimensions: ["3b"],
};
const actionDecision = {
  decision_complete: false,
  decision_basis: "action_condition",
  potentially_triggerable: true,
  score_lower_bound: 7,
  score_upper_bound: 8,
  missing_dimensions: ["6e"],
};
const inactiveActionDecision = {
  decision_complete: true,
  decision_basis: "conservative_upper_bound",
  potentially_triggerable: false,
  score_lower_bound: 0,
  score_upper_bound: 4,
  missing_dimensions: ["6e"],
};
const knownPositionDecision = {
  decision_complete: true,
  decision_basis: "full_evidence",
  potentially_triggerable: false,
  score_lower_bound: 5,
  score_upper_bound: 6,
  missing_dimensions: [],
};
const catalogue = {
  capabilities: { dimension_scores: true },
  companies: [
    {
      code: "300001", name: "待补资料候选", industry: "测试行业", diagnostic_score: null, primary_label: "",
      types: { type3: { status: "insufficient_evidence", score: null, applicable: true, evidence_complete: false, decision: unresolvedDecision } },
    },
    {
      code: "300002", name: "已有否决", industry: "测试行业", diagnostic_score: 4, primary_label: "",
      types: { type3: { status: "vetoed", score: 4, applicable: true, evidence_complete: false, decision: confirmedVetoDecision } },
    },
    {
      code: "300003", name: "仓位待确认", industry: "测试行业", diagnostic_score: 7, primary_label: "",
      types: { type6: { status: "conditional", score: 7, applicable: true, evidence_complete: false, action_required: "position_confirmation", decision: actionDecision } },
    },
    {
      code: "300004", name: "当前无需确认", industry: "测试行业", diagnostic_score: 4, primary_label: "",
      types: { type6: { status: "not_triggered", score: 4, applicable: true, evidence_complete: true, investor_action_dimensions: ["6e"], decision: inactiveActionDecision } },
    },
    {
      code: "300005", name: "已知仓位不合规", industry: "测试行业", diagnostic_score: 6, primary_label: "",
      types: { type6: { status: "conditional", score: 6, applicable: true, evidence_complete: true, decision: knownPositionDecision } },
    },
  ],
};
const catalogueRaw = Buffer.from(JSON.stringify(catalogue));
const catalogueBytes = gzipSync(catalogueRaw);
const manifest = {
  catalogue: {
    filename: "catalog.json.gz",
    size: catalogueBytes.byteLength,
    sha256: createHash("sha256").update(catalogueBytes).digest("hex"),
    uncompressed_size: catalogueRaw.byteLength,
  },
};
const manifestBytes = Buffer.from(JSON.stringify(manifest));
const manifestHash = createHash("sha256").update(manifestBytes).digest("hex");
function objectFor(bytes, hash = "") {
  return {
    size: bytes.byteLength,
    customMetadata: hash ? { sha256: hash } : {},
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    get body() { return new Response(bytes).body; },
  };
}
const generation = { generation_id: "0123456789abcdef", manifest_sha256: manifestHash };
const objects = new Map([
  ["generations/0123456789abcdef/manifest.json", objectFor(manifestBytes, manifestHash)],
  ["generations/0123456789abcdef/catalog.json.gz", objectFor(catalogueBytes)],
]);
const env = {
  DB: { prepare: () => ({ bind() { return this; }, first: async () => generation }) },
  DATA_BUCKET: {
    get: async (key) => objects.get(key) || null,
    head: async (key) => objects.get(key) || null,
  },
};
const legacyUrl = "https://dashboard.test/api/catalogue-index?generation_id=0123456789abcdef";
const canonicalUrl = legacyUrl + "&index_contract=2";
cached.set(legacyUrl, new Response(JSON.stringify({ index_contract: 1, generation_id: "0123456789abcdef", summary: { company_count: 1 }, companies: [] })));
const response = await worker.fetch(
  new Request(canonicalUrl),
  env,
);
assert.equal(response.status, 200);
const payload = await response.json();
assert.equal(payload.index_contract, 2);
assert.equal(payload.summary.company_count, 5);
assert.ok(cached.has(canonicalUrl));
assert.deepEqual(payload.summary.type_coverage.type3, {
  evidence_missing: 2,
  decision_unresolved: 1,
  potentially_triggerable: 1,
  action_confirmation: 0,
});
assert.deepEqual(payload.summary.type_coverage.type6, {
  evidence_missing: 0,
  decision_unresolved: 0,
  potentially_triggerable: 0,
  action_confirmation: 1,
});
assert.deepEqual(payload.companies[0].types.type3, {
  status: "insufficient_evidence",
  score: null,
  score_lower_bound: 0,
  score_upper_bound: 8,
  has_missing_dimensions: true,
  has_evidence_gap: true,
});
assert.equal(payload.companies[1].types.type3.has_evidence_gap, true);
assert.equal(payload.companies[2].types.type6.has_evidence_gap, false);
assert.equal(payload.companies[3].types.type6.has_evidence_gap, false);
assert.equal(payload.companies[4].types.type6.has_evidence_gap, false);
assert.equal("investor_action_dimensions" in payload.companies[2].types.type6, false);
const reordered = await worker.fetch(
  new Request("https://dashboard.test/api/catalogue-index?index_contract=2&generation_id=0123456789abcdef"),
  env,
);
assert.equal(reordered.status, 200);
assert.equal((await reordered.json()).index_contract, 2);
const cacheBustedHealth = await worker.fetch(
  new Request("https://dashboard.test/api/health?verify=1"),
  env,
);
assert.equal(cacheBustedHealth.status, 503);
const unhealthy = await cacheBustedHealth.json();
assert.equal(unhealthy.ok, false);
assert.equal(unhealthy.stale, true);
assert.equal(unhealthy.deep_check, false);
for (const invalidUrl of [
  legacyUrl,
  legacyUrl + "&index_contract=1",
  canonicalUrl + "&debug=1",
  canonicalUrl + "&generation_id=0123456789abcdef",
]) {
  const invalid = await worker.fetch(new Request(invalidUrl), env);
  assert.equal(invalid.status, 400, invalidUrl);
}
const tamperedCatalogue = Buffer.from(catalogueBytes);
tamperedCatalogue[0] ^= 1;
for (const invalidUrl of [
  "https://dashboard.test/api/company/300001?generation_id=0123456789abcdef&debug=1",
  "https://dashboard.test/api/catalogue?generation_id=coverage-test",
]) {
  const invalid = await worker.fetch(new Request(invalidUrl), env);
  assert.equal(invalid.status, 400, invalidUrl);
}
objects.set("generations/0123456789abcdef/catalog.json.gz", objectFor(tamperedCatalogue));
const tampered = await worker.fetch(
  new Request("https://dashboard.test/api/catalogue?generation_id=0123456789abcdef"),
  env,
);
assert.equal(tampered.status, 500);
assert.match((await tampered.json()).error, /目录正文完整性校验失败/);
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
    assert "function typeStatusMatches" in source
    assert "typeState?.status===status" in source
    assert 'status==="evidence_gap"?typeState?.has_evidence_gap===true' in source


def test_dashboard_contract_contains_dimension_labels_and_sub_score_rendering():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "TYPE_DIMENSIONS" in source
    for label in ("买入区深度", "底部信号", "技术壁垒", "本类别的商业模式", "本类别的长期成长"):
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
    assert "typeState?.status===status" in source
    assert 's=q?"":$("status").value' in source
    assert "当前确实为 0 家命中" in source
    assert "没有找到匹配该代码或名称的公司" in source
    assert "displayedScore" in source
    assert "点击查看完整依据" in source


def test_dashboard_coverage_cards_separate_signals_conditions_and_unresolved_evidence():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "function coverageEvidenceStats" in source
    assert "Object.entries(TYPE_NAMES).map" in source
    assert 'value?.status!=="triggered"&&value?.status!=="conditional"' in source
    assert 'decision.decision_basis==="unresolved_missing_evidence"' in source
    assert "decision.potentially_triggerable===true" in source
    assert (
        'preferredStatus=triggered>0?"triggered":conditional>0?"conditional":evidence.evidenceMissing>0?"evidence_gap":""'
        in source
    )
    assert "function conditionalCoverageLabel" in source
    assert "action_confirmation" in source
    for text in (
        "七类命中、待确认与资料缺口分布",
        "已触发”才是实际信号",
        "资料缺口 ",
        "其中结论待定 ",
        "其中补齐后仍可能触发 ",
        "待满足其它条件 ",
        "coverage-breakdown",
    ):
        assert text in source


def test_dashboard_health_separates_light_freshness_from_explicit_deep_asset_checks():
    source = DASHBOARD.read_text(encoding="utf-8")

    for field in (
        "data_generated_at",
        "generation_published_at",
        "last_mirror_check_at",
        "data_age_hours",
        "stale",
        "stale_reason",
        "expected_market_as_of",
        "market_date_current",
        "calendar_coverage",
        "hard_age_limit_hours",
        "manifest_ok",
        "manifest_bytes",
        "signals_bytes",
        "signature_bytes",
    ):
        assert field in source
    assert 'url.searchParams.get("deep")==="1"' in source
    assert "deep?await deepGenerationHealth" in source
    assert "recordOk&&!freshness.stale&&(!deep||integrityOk)" in source
    assert "ok?200:503" in source
    assert "manifestRecord.ok&&catalogueOk&&signalsOk&&signatureOk&&detailsOk" in source
    assert "actual!==expected||(marker&&marker!==expected)" in source
    assert "manifest?.company_details&&marker!==expected" in source
    assert source.index("object.size>MAX_MANIFEST_BYTES") < source.index("object.arrayBuffer()")
    assert "object.size!==bytes.byteLength" in source
    assert "updated_at:generation?.data_timestamp_utc" in source
    assert 'requestedGeneration?"public, max-age=31536000, immutable":"no-store"' in source
    assert "数据生成时间距今不超过36小时" not in source


def test_dashboard_health_is_light_by_default_and_deep_checks_assets_on_demand():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute dashboard health routes"
    validator = r"""
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;
Date.now = () => Date.parse("2026-08-11T12:00:00Z");
const checksum = "a".repeat(64);
const manifest = {
  catalogue: { filename: "catalogue.json.gz", size: 120, sha256: checksum, uncompressed_size: 240 },
  signals: { filename: "signals.json.gz", size: 80, sha256: checksum, uncompressed_size: 160 },
  signature: { filename: "manifest.sig" },
};
const manifestBytes = Buffer.from(JSON.stringify(manifest));
const manifestHash = createHash("sha256").update(manifestBytes).digest("hex");
const objectFor = (size, bytes = null, hash = "") => ({
  size,
  customMetadata: hash ? { sha256: hash } : {},
  arrayBuffer: async () => {
    if (!bytes) throw new Error("unexpected body read");
    return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  },
});
const generation = {
  generation_id: "0123456789abcdef",
  manifest_sha256: manifestHash,
  market_as_of: "2026-08-11",
  data_timestamp_utc: "2026-08-11T09:00:00Z",
  created_at: "2026-08-11T09:05:00Z",
  last_checked_at: "2026-08-11T11:55:00Z",
  company_count: 10,
};
const prefix = "generations/0123456789abcdef/";
const objects = new Map([
  [prefix + "manifest.json", objectFor(manifestBytes.byteLength, manifestBytes, manifestHash)],
  [prefix + "catalogue.json.gz", objectFor(120)],
  [prefix + "signals.json.gz", objectFor(80)],
  [prefix + "manifest.sig", objectFor(64)],
]);
let getCount = 0;
let headCount = 0;
const env = {
  DB: { prepare: () => ({ bind() { return this; }, first: async () => generation }) },
  DATA_BUCKET: {
    get: async key => { getCount++; return objects.get(key) || null; },
    head: async key => { headCount++; return objects.get(key) || null; },
  },
};

let response = await worker.fetch(new Request("https://dashboard.test/api/health"), env);
assert.equal(response.status, 200);
let payload = await response.json();
assert.equal(payload.ok, true);
assert.equal(payload.deep_check, false);
assert.equal(payload.integrity_checked, false);
assert.equal(payload.integrity_ok, null);
assert.equal(payload.manifest_ok, null);
assert.equal(getCount, 0);
assert.equal(headCount, 0);
assert.equal(response.headers.get("x-content-type-options"), "nosniff");

response = await worker.fetch(new Request("https://dashboard.test/api/health?deep=1"), env);
assert.equal(response.status, 200);
payload = await response.json();
assert.equal(payload.ok, true);
assert.equal(payload.deep_check, true);
assert.equal(payload.integrity_checked, true);
assert.equal(payload.integrity_ok, true);
assert.equal(payload.manifest_ok, true);
assert.equal(payload.company_details_declared, false);
assert.equal(getCount, 1);
assert.equal(headCount, 3);

objects.delete(prefix + "signals.json.gz");
response = await worker.fetch(new Request("https://dashboard.test/api/health?deep=1"), env);
assert.equal(response.status, 503);
payload = await response.json();
assert.equal(payload.ok, false);
assert.equal(payload.integrity_ok, false);
assert.equal(payload.signals_ok, false);

const requestsBeforeStale = getCount + headCount;
generation.market_as_of = "2026-08-10";
response = await worker.fetch(new Request("https://dashboard.test/api/health"), env);
assert.equal(response.status, 503);
payload = await response.json();
assert.equal(payload.ok, false);
assert.equal(payload.stale, true);
assert.equal(payload.stale_reason, "尚未覆盖最近应完成的交易日");
assert.equal(getCount + headCount, requestsBeforeStale);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_dashboard_health_uses_closed_trading_sessions_for_weekends_and_holidays():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate dashboard freshness rules"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const start = source.indexOf("const A_SHARE_EXCHANGE_CLOSURES=");
const end = source.indexOf("function limitedGzipStream", start);
assert.ok(start >= 0 && end > start, "trading-calendar freshness source was not found");
const helpers = new Function(
  source.slice(start, end) + ";return {latestExpectedClosedTradingDate,tradingDataFreshness};",
)();
const { latestExpectedClosedTradingDate, tradingDataFreshness } = helpers;
const utc = value => Date.parse(value);

// Saturday/Sunday retain Friday's already-closed session even after 36 hours.
const sunday = utc("2026-08-02T04:00:00Z"); // 12:00 Beijing
assert.equal(latestExpectedClosedTradingDate(sunday), "2026-07-31");
let result = tradingDataFreshness("2026-07-31", "2026-07-31T09:00:00Z", sunday);
assert.equal(result.stale, false);
assert.ok(result.data_age_hours > 36);
assert.equal(result.expected_market_as_of, "2026-07-31");
assert.equal(result.calendar_coverage, "交易所公告日历");

// A trading day is not required until the explicit 18:00 Beijing deadline.
const mondayBeforeDeadline = utc("2026-08-03T09:59:00Z");
const mondayAtDeadline = utc("2026-08-03T10:00:00Z");
assert.equal(latestExpectedClosedTradingDate(mondayBeforeDeadline), "2026-07-31");
assert.equal(latestExpectedClosedTradingDate(mondayAtDeadline), "2026-08-03");
assert.equal(
  tradingDataFreshness("2026-07-31", "2026-07-31T09:00:00Z", mondayAtDeadline).stale_reason,
  "尚未覆盖最近应完成的交易日",
);
assert.equal(
  tradingDataFreshness("2026-08-04", "2026-08-03T09:00:00Z", mondayAtDeadline).stale_reason,
  "市场日期晚于最近已收盘交易日",
);

// Official 2026 exchange closures come from tools/china_a_share_trading_calendar.json.
const springFestival = utc("2026-02-23T12:00:00Z"); // 20:00 Beijing
assert.equal(latestExpectedClosedTradingDate(springFestival), "2026-02-13");
result = tradingDataFreshness("2026-02-13", "2026-02-13T10:00:00Z", springFestival);
assert.equal(result.stale, false);
assert.ok(result.data_age_hours > 9 * 24);
assert.equal(latestExpectedClosedTradingDate(utc("2026-10-07T12:00:00Z")), "2026-09-30");
assert.equal(latestExpectedClosedTradingDate(utc("2026-02-24T09:59:00Z")), "2026-02-13");
assert.equal(latestExpectedClosedTradingDate(utc("2026-02-24T10:00:00Z")), "2026-02-24");

// The calendar cannot suppress alarms indefinitely, even if market_as_of looks current.
const hardLimitNow = utc("2026-08-03T12:00:00Z");
result = tradingDataFreshness("2026-08-03", new Date(hardLimitNow - 337 * 3600000).toISOString(), hardLimitNow);
assert.equal(result.stale, true);
assert.equal(result.stale_reason, "数据生成时间超过14天安全上限");

// An unregistered calendar year is deliberately weekday-only (fail closed).
const unknownYear = utc("2027-01-01T12:00:00Z"); // Friday, 20:00 Beijing
result = tradingDataFreshness("2026-12-31", "2026-12-31T10:00:00Z", unknownYear);
assert.equal(result.expected_market_as_of, "2027-01-01");
assert.equal(result.calendar_coverage, "仅周末规则（该年份节假日表尚未登记）");
assert.equal(result.stale, true);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_dashboard_embedded_exchange_closures_match_the_audited_calendar_file():
    source = DASHBOARD.read_text(encoding="utf-8")
    calendar = json.loads(TRADING_CALENDAR.read_text(encoding="utf-8"))
    expected = {
        (period["start"], period["end"]) for year in calendar["years"].values() for period in year["closure_periods"]
    }
    embedded = set(
        re.findall(
            r'Object\.freeze\(\["(\d{4}-\d{2}-\d{2})","(\d{4}-\d{2}-\d{2})"\]\)',
            source,
        )
    )

    assert embedded == expected
    assert "tools/china_a_share_trading_calendar.json" in source


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
    assert '"&index_contract="+CATALOGUE_INDEX_CONTRACT_VERSION' in source
    assert "Number(c.index_contract)!==CATALOGUE_INDEX_CONTRACT_VERSION" in source
    assert "function canonicalCatalogueIndexRequest" in source
    assert "function catalogueIndexCacheRequest" in source
    assert 'cacheKey=new Request(cacheRequest.url,{method:"GET"})' in source
    assert "headSafeResponse(request" in source
    assert "const MAX_UNCOMPRESSED_ASSET_BYTES=32_000_000;" in source
    assert "24*1024*1024" not in source
    assert "MAX_DETAIL_PREFETCHES=2" in source
    assert 'addEventListener("pointerover"' in source
    assert 'addEventListener("focusin"' in source
    assert '"requestIdleCallback" in window' in source
    assert "prefetchVisibleDetails" not in source
    assert ".slice(0,MAX_DETAIL_PREFETCHES)" in source
    assert "SHARD_CACHE_LIMIT=2" in source
    assert "shardInflight.get(inflightKey)" in source
    assert "shardInflight.set(inflightKey,pending)" in source
    assert "shardInflight.delete(inflightKey)" in source
    assert ".style." not in source


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
    assert "summary.type7_quality_certified_company_count??record.quality_certified??0" in source
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
    assert "function investorActionDimensions(typeKey,value)" in source
    assert "investor_action_dimensions" in source
    assert 'legacy=value?.action_required==="position_confirmation"&&missing.includes("6e")' in source
    assert "positionInstruction=investorActions.has(dimension)&&missing.has(dimension)" in source
    assert "dataMissingScore=missingScore&&!positionInstruction" in source
    assert "function typeDataGap(typeKey,value)" in source
    assert "dataMissing=missing.filter(dimension=>!actionDimensions.has(dimension))" in source
    assert 'actionRequired=actionDimensions.has("6e")&&missing.includes("6e")&&value?.status==="conditional"' in source
    assert "declaredIncomplete=value?.applicable===true&&value?.evidence_complete===false" in source
    assert "hasAction=states.some(state=>state.action_required)" in source
    assert 'state.key==="type6"&&company.types?.type6?.status==="conditional"' not in source
    assert "function conditionalCoverageLabel" in source
    assert "待满足其它条件" in source
    assert "EVIDENCE_META_NAMES" in source
    assert "publicReasonText(v.reasons?.[dimension]||subReasons[dimension])" in source
    assert (
        "actionConfirmationCount=Number(summary.action_confirmation_company_count??s.action_confirmation_company_count??0)"
        in source
    )
    assert '["其中待确认仓位",actionConfirmationCount]' in source
    assert 'positionAction=positionInstruction&&v.status==="conditional"' in source
    assert 'inactivePositionAction?"当前无需确认"' in source


def test_dashboard_uses_plain_language_version_and_exposes_only_traceable_detail_facts():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'const METHODOLOGY_LABEL="七类量化买入方法+补丁7总闸门（2026年8月）"' in source
    assert 'const METHODOLOGY_VERSION="patch6-seven-types-2026-08-01-classified-type7-v4"' in source
    assert '" · 量化口径："+METHODOLOGY_LABEL' in source
    assert '" · 量化口径："+METHODOLOGY_VERSION' not in source
    assert '.replace("__QUANT_METHODOLOGY_LABEL__",METHODOLOGY_LABEL)' in source
    assert 'addFact(facts,"行情日期",String(r.source_trade_date||marketAsOf||"—"))' in source
    assert 'addFact(facts,"可追溯版本",sourceVersion||"—")' in source
    assert "Array.isArray(r.annual_history)" in source
    assert "年度历史只在现有证据能够确认起止年份和连续年数时展示" in source
    assert "不会根据行情日期倒推" in source
    assert "各子指标可能只使用其中一部分年度" in source
    assert "公开详情未附该子指标的单独来源链接" in source
    for developer_phrase in ("白名单量化输入", "代理证据最高分", "分类专用评估路径", "分类专用路径"):
        assert developer_phrase not in source


def test_dashboard_distinguishes_model_coverage_and_uses_latest_type7_mean_rule():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "function isModelCoverageGap" in source
    assert 'typeKey==="type7"&&/金融/.test(explanation)' in source
    assert 'scope.classList.add("model-gap")' in source
    assert "质量认证取三项算术平均并严格大于7.000" in source
    assert "强科技三项还必须各自不低于7分" in source
    assert "强科技近五年市净率分位不高于20%" in source
    assert "强周期当前市净率不高于1.20且近五年分位不高于20%" in source
    assert "其他买入情况已触发也不能免除" in source
    assert "1.20和20%是程序对“接近净资产”和“处于历史底部区”的量化定义" in source
    assert "仅第七类单独触发时执行该类别的价格门槛" not in source
    assert "算术平均权重33.3%" in source
    assert "第七类三项平均中的贡献＝该维度分÷3" in source
    assert "独立门槛：严格高于7.0分" not in source
    assert "三项不可相互抵消" not in source
    for opaque_text in ("类内商业模式(BM)", "类内护城河(MOAT)", "Type7三维"):
        assert opaque_text not in source


def test_dashboard_renders_type7_classification_twelve_items_gates_and_outdated_warning():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "function renderType7MethodDetail" in source
    assert "第七类完整量化明细" in source
    assert "确定归类：" in source
    assert "暂定归类：" in source
    assert "三项算术平均：" in source
    assert "质量认证：" in source
    assert "当前买点：" in source
    assert "公司类别是怎样算出来的" in source
    assert "强周期、强科技和弱周期特征" in source
    assert "强周期（C）、强科技（T）和弱周期特征（N）" not in source
    assert 'publicClassName(route?.name)||"公司类别"' in source
    assert "route?.code" not in source
    assert "classification_scores" in source
    assert "uses_industry_proxy" in source
    assert "uses_financial_proxy" in source
    assert "当前采用" in source
    assert "资料性质：" in source
    assert "实际依据：" in source
    assert "证据覆盖" in source
    assert "实际计算数据：" in source
    assert "计算规则：" in source
    assert "资料未齐，仅表示范围" in source
    assert "旧数据待刷新" in source
    assert "未通过项目：" in source
    assert 'method.status==="outdated"' in source
    assert "数据版本过旧，请刷新" in source
    assert 'const type7MethodDetail=k==="type7"?renderType7MethodDetail(v):null' in source


def test_dashboard_type5_trigger_text_matches_the_weighted_model_without_invented_5c_gates():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "强周期属性（5a）至少7分才进入本模型" in source
    assert "其余子项证据完整后，五项加权总分至少7分即触发" in source
    assert "抗周期能力（5c）只按20%权重进入总分" in source
    assert "不另设5分门槛，也不存在3分否决线" in source
    assert "抗周期能力至少5分" not in source
    assert "抗周期能力不高于3分时直接否决" not in source


def test_dashboard_focus_pagination_search_and_zero_bar_interactions_are_explicit():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "min-width:2px" not in source
    assert 'document.createElement("progress")' in source
    assert "if(visibleCount===0)track.hidden=true" in source
    assert "function syncSearchStatus()" in source
    assert '$("status").disabled=searching' in source
    assert "代码/名称搜索会跨全部状态，状态筛选暂时停用。" in source
    assert "typeState&&(q||s?(!s||typeStatusMatches(typeState,s))" in source
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

    assert 'repairedObjects || databaseRepairNeeded ? "repaired" : "unchanged"' in source
    assert "existing.size === object.expectedSize" in source
    assert "existing.customMetadata?.sha256" in source
    assert "const objectsToPut = hydratedObjects.filter((object) => !object.complete)" in source
    assert "await putGenerationObjects(env.DATA_BUCKET, objectsToPut)" in source


def test_refresh_worker_switches_generation_with_one_idempotent_d1_transaction():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert "ON CONFLICT(generation_id) DO UPDATE SET" in source
    assert "last_checked_at = excluded.last_checked_at" in source
    assert "WHERE generations.market_as_of = excluded.market_as_of" in source
    assert "WHERE EXISTS (" in source
    assert "await env.DB.batch([generationStatement, pointerStatement])" in source
    assert "generation database transaction did not commit one consistent pointer" in source


def test_refresh_worker_same_generation_validates_and_repairs_d1_before_returning_unchanged():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert "LEFT JOIN generations AS g ON g.generation_id = c.generation_id" in source
    assert "CASE WHEN g.generation_id IS NULL THEN 0 ELSE 1 END AS target_exists" in source
    assert "stored generation metadata does not match the signed manifest" in source
    assert "const databaseRepairNeeded = pointerIsDangling" in source
    assert "if (previous?.generation_id === generationId)" not in source
    assert "sameGenerationPointer && !databaseRepairNeeded && allObjectsComplete" in source
    assert "await touchCompleteGeneration(env, generationId, manifestHash, now)" in source
    assert source.index("inspectGenerationObjects(env.DATA_BUCKET, expectedObjects)") < source.index(
        'downloadAsset(catalogueName, "catalogue", manifest.catalogue)'
    )
    assert source.index('status: "unchanged"') < source.index(
        'downloadAsset(catalogueName, "catalogue", manifest.catalogue)'
    )
    assert source.index("await env.DB.batch([generationStatement, pointerStatement])") < source.index(
        'repairedObjects || databaseRepairNeeded ? "repaired" : "unchanged"'
    )


def test_refresh_worker_bounds_primary_decompression_and_r2_head_put_concurrency():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the refresh resource contracts"
    validator = r"""
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
source += "\nexport { inspectGenerationObjects, putGenerationObjects, validatePrimaryAssetMetadata, verifyPrimaryAssetSize };";
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const {
  inspectGenerationObjects,
  putGenerationObjects,
  validatePrimaryAssetMetadata,
  verifyPrimaryAssetSize,
} = await import(url);

const raw = Buffer.from('{"ok":true}');
const compressed = gzipSync(raw);
await verifyPrimaryAssetSize(compressed, { uncompressed_size: raw.byteLength }, "catalogue");
await assert.rejects(
  verifyPrimaryAssetSize(compressed, { uncompressed_size: raw.byteLength + 1 }, "signals"),
  /signals uncompressed size mismatch/,
);
assert.throws(
  () => validatePrimaryAssetMetadata({ uncompressed_size: 32_000_001 }, "catalogue"),
  /invalid catalogue uncompressed size/,
);
const overflow = gzipSync(Buffer.alloc(32_000_001));
await assert.rejects(
  verifyPrimaryAssetSize(overflow, { uncompressed_size: 32_000_000 }, "catalogue"),
  /catalogue uncompressed response exceeds its byte limit/,
);

const objects = Array.from({ length: 11 }, (_, index) => ({
  name: `asset-${index}`,
  key: `generations/0123456789abcdef/asset-${index}`,
  body: new Uint8Array([index]).buffer,
  contentType: "application/json",
  contentEncoding: null,
  expectedSize: 1,
  expectedHash: "a".repeat(64),
}));
let activeHeads = 0;
let maxHeads = 0;
const inspected = await inspectGenerationObjects({
  async head() {
    activeHeads += 1;
    maxHeads = Math.max(maxHeads, activeHeads);
    await new Promise((resolve) => setTimeout(resolve, 2));
    activeHeads -= 1;
    return { size: 1, customMetadata: { sha256: "a".repeat(64) } };
  },
}, objects);
assert.equal(inspected.length, objects.length);
assert.equal(maxHeads, 4);
assert.ok(inspected.every((object) => object.complete));

let activePuts = 0;
let maxPuts = 0;
let putCount = 0;
await putGenerationObjects({
  async put() {
    activePuts += 1;
    maxPuts = Math.max(maxPuts, activePuts);
    await new Promise((resolve) => setTimeout(resolve, 2));
    putCount += 1;
    activePuts -= 1;
  },
}, objects);
assert.equal(putCount, objects.length);
assert.equal(maxPuts, 4);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_refresh_worker_retention_never_deletes_current_and_retries_incomplete_r2_cleanup():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the refresh retention contract"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
source += "\nexport { pruneOldGenerations };";
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const { pruneOldGenerations } = await import(url);

function row(index) {
  return {
    generation_id: index.toString(16).padStart(16, "0"),
    market_as_of: `2026-08-${String(20 - index).padStart(2, "0")}`,
    data_timestamp_utc: `2026-08-${String(20 - index).padStart(2, "0")}T08:00:00Z`,
    generated_at_utc: `2026-08-${String(20 - index).padStart(2, "0")}T09:00:00Z`,
    manifest_sha256: index.toString(16).padStart(64, "0"),
    company_count: 5000,
    triggered_company_count: 10,
    conditional_company_count: 2,
    pending_company_count: 1,
    source_commit: "b".repeat(40),
    created_at: "2026-08-20T10:00:00Z",
    last_checked_at: "2026-08-20T11:00:00Z",
  };
}

function database(rows, events) {
  return {
    prepare(sql) {
      return {
        bind(...parameters) {
          return {
            async all() {
              assert.match(sql, /ORDER BY CASE WHEN generation_id = \?/);
              assert.equal(parameters[1], 16);
              return { success: true, results: rows };
            },
            async run() {
              if (sql.includes("DELETE FROM generations")) {
                events.push(`db-delete:${parameters[0]}`);
                assert.notEqual(parameters[0], parameters[1]);
                return { success: true, meta: { changes: 1 } };
              }
              throw new Error("unexpected SQL");
            },
          };
        },
      };
    },
  };
}

const rows = Array.from({ length: 10 }, (_, index) => row(index));
const current = rows[0].generation_id;
const events = [];
const deletedKeys = [];
const env = {
  GENERATION_RETENTION_COUNT: "8",
  DB: database(rows, events),
  DATA_BUCKET: {
    async list({ prefix }) {
      const generation = prefix.split("/")[1];
      events.push(`r2-list:${generation}`);
      return { truncated: false, objects: [{ key: `${prefix}manifest.json` }] };
    },
    async delete(keys) { deletedKeys.push(...keys); },
  },
};
assert.equal(await pruneOldGenerations(env, current), 2);
assert.deepEqual(events, [
  `r2-list:${rows[8].generation_id}`,
  `db-delete:${rows[8].generation_id}`,
  `r2-list:${rows[9].generation_id}`,
  `db-delete:${rows[9].generation_id}`,
]);
assert.ok(deletedKeys.every((key) => !key.includes(current)));

const failedEvents = [];
const failedEnv = {
  GENERATION_RETENTION_COUNT: "8",
  DB: database(rows.slice(0, 9), failedEvents),
  DATA_BUCKET: {
    async list({ prefix }) {
      failedEvents.push(`r2-list:${prefix.split("/")[1]}`);
      throw new Error("R2 unavailable");
    },
    async delete() { throw new Error("delete must not run"); },
  },
};
await assert.rejects(pruneOldGenerations(failedEnv, current), /R2 unavailable/);
assert.deepEqual(failedEvents, [`r2-list:${rows[8].generation_id}`]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    assert "MAX_STALE_GENERATIONS_PER_REFRESH = 8" in source
    assert "generationId === currentGenerationId" in source
    assert "AND generation_id <> ?" in source
    assert "WHERE singleton = 1 AND generation_id = ?" in source
    assert source.index("await touchCompleteGeneration(env, generationId, manifestHash, now)") < source.index(
        "await pruneOldGenerations(env, generationId)"
    )
    assert source.index("await env.DB.batch([generationStatement, pointerStatement])") < source.rindex(
        "await pruneOldGenerations(env, generationId)"
    )


def test_refresh_worker_generation_upsert_repairs_missing_same_generation_but_rejects_metadata_mismatch():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    generation_sql_match = re.search(
        r"const generationStatement = env\.DB\.prepare\(\s*`(?P<sql>.*?)`\s*\)\.bind\(",
        source,
        flags=re.DOTALL,
    )
    assert generation_sql_match is not None
    generation_sql = generation_sql_match.group("sql")
    pointer_sql_match = re.search(
        r"const pointerStatement = env\.DB\.prepare\(\s*`(?P<sql>.*?)`\s*\)\.bind\(",
        source,
        flags=re.DOTALL,
    )
    assert pointer_sql_match is not None
    pointer_sql = pointer_sql_match.group("sql")

    expected = (
        "0123456789abcdef",
        "2026-07-31",
        "2026-07-31T08:30:00Z",
        "2026-07-31T08:31:00Z",
        "a" * 64,
        4_986,
        17,
        2,
        3,
        "b" * 40,
        "2026-07-31T08:32:00Z",
        "2026-08-01T00:00:00Z",
    )
    pointer_parameters = (
        expected[0],
        expected[11],
        *expected[:10],
        expected[1],
        expected[1],
        expected[2],
        expected[1],
        expected[2],
        expected[3],
    )

    # D1 normally enforces this reference, but a damaged/legacy database can
    # contain the pointer without its generation row when foreign keys were off.
    missing_row_database = sqlite3.connect(":memory:")
    missing_row_database.executescript(SCHEMA.read_text(encoding="utf-8"))
    missing_row_database.execute("PRAGMA foreign_keys = OFF")
    missing_row_database.execute(
        "INSERT INTO current_generation(singleton, generation_id, updated_at) VALUES (1, ?, ?)",
        (expected[0], expected[10]),
    )
    inserted = missing_row_database.execute(generation_sql, expected)
    assert inserted.rowcount == 1
    repaired_pointer = missing_row_database.execute(pointer_sql, pointer_parameters)
    assert repaired_pointer.rowcount == 1
    assert missing_row_database.execute(
        "SELECT market_as_of, manifest_sha256, company_count FROM generations WHERE generation_id = ?",
        (expected[0],),
    ).fetchone() == (expected[1], expected[4], expected[5])
    assert missing_row_database.execute(
        "SELECT generation_id, updated_at FROM current_generation WHERE singleton = 1"
    ).fetchone() == (expected[0], expected[11])

    mismatch_database = sqlite3.connect(":memory:")
    mismatch_database.executescript(SCHEMA.read_text(encoding="utf-8"))
    mismatched = list(expected)
    mismatched[5] += 1
    mismatch_database.execute("INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", mismatched)
    mismatch_database.execute(
        "INSERT INTO current_generation(singleton, generation_id, updated_at) VALUES (1, ?, ?)",
        (expected[0], expected[10]),
    )
    rejected = mismatch_database.execute(generation_sql, expected)
    assert rejected.rowcount == 0
    rejected_pointer = mismatch_database.execute(pointer_sql, pointer_parameters)
    assert rejected_pointer.rowcount == 0
    assert mismatch_database.execute(
        "SELECT company_count FROM generations WHERE generation_id = ?", (expected[0],)
    ).fetchone() == (mismatched[5],)


def test_refresh_worker_never_moves_the_public_pointer_back_to_an_older_generation():
    source = REFRESH_WORKER.read_text(encoding="utf-8")

    assert "AND NOT EXISTS (" in source
    assert "served.market_as_of > ?" in source
    assert "served.data_timestamp_utc > ?" in source
    assert "served.generated_at_utc > ?" in source
    assert "served.generation_id <> ?" not in source
    assert 'status: "superseded"' in source
    assert "current_generation_id: current.generation_id" in source

    pointer_sql_match = re.search(
        r"const pointerStatement = env\.DB\.prepare\(\s*`(?P<sql>.*?)`\s*\)\.bind\(",
        source,
        flags=re.DOTALL,
    )
    assert pointer_sql_match is not None
    pointer_sql = pointer_sql_match.group("sql")

    def generation(generation_id: str, market_as_of: str, timestamp: str, generated_at: str) -> tuple[object, ...]:
        return (
            generation_id,
            market_as_of,
            timestamp,
            generated_at,
            generation_id.rjust(64, "0"),
            4_986,
            1,
            0,
            0,
            "a" * 40,
            generated_at,
            generated_at,
        )

    older = generation("1111111111111111", "2026-07-30", "2026-07-30T08:30:00Z", "2026-07-30T08:31:00Z")
    newer = generation("2222222222222222", "2026-07-31", "2026-07-31T08:30:00Z", "2026-07-31T08:31:00Z")

    def pointer_parameters(target: tuple[object, ...]) -> tuple[object, ...]:
        generation_id, market_as_of, timestamp, generated_at = target[:4]
        return (
            generation_id,
            "2026-08-01T00:00:00Z",
            *target[:10],
            market_as_of,
            market_as_of,
            timestamp,
            market_as_of,
            timestamp,
            generated_at,
        )

    database = sqlite3.connect(":memory:")
    database.executescript(SCHEMA.read_text(encoding="utf-8"))
    database.executemany("INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [older, newer])
    database.execute(
        "INSERT INTO current_generation(singleton, generation_id, updated_at) VALUES (1, ?, ?)",
        (newer[0], newer[3]),
    )
    rejected = database.execute(pointer_sql, pointer_parameters(older))
    assert rejected.rowcount == 0
    assert database.execute("SELECT generation_id FROM current_generation").fetchone() == (newer[0],)

    database.execute("UPDATE current_generation SET generation_id = ?, updated_at = ?", (older[0], older[3]))
    advanced = database.execute(pointer_sql, pointer_parameters(newer))
    assert advanced.rowcount == 1
    assert database.execute("SELECT generation_id FROM current_generation").fetchone() == (newer[0],)

    rebuilt = generation("3333333333333333", newer[1], newer[2], newer[3])
    database.execute("INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rebuilt)
    replaced = database.execute(pointer_sql, pointer_parameters(rebuilt))
    assert replaced.rowcount == 1
    assert database.execute("SELECT generation_id FROM current_generation").fetchone() == (rebuilt[0],)


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


def test_refresh_worker_enforces_matching_company_detail_asset_and_total_boundaries():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the refresh capacity contract"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
source += "\nexport { companyDetailAssets };";
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const { companyDetailAssets } = await import(url);

const generation = "0123456789abcdef";
const checksum = "a".repeat(64);
const manifest = {
  summary: { company_count: 16 },
  company_details: {
    schema_version: 2,
    record_schema: "company_detail_v2",
    company_count: 16,
    partition: { algorithm: "sha256_code_first_nibble", shard_count: 16 },
    root_algorithm: "SHA256_CANONICAL_SHARD_INDEX_V1",
    root_sha256: checksum,
    shards: Array.from({ length: 16 }, (_, index) => {
      const id = index.toString(16).padStart(2, "0");
      return {
        id,
        filename: `company-details-${generation}-${id}.json.gz`,
        company_count: 1,
        sha256: checksum,
        uncompressed_sha256: checksum,
        size: 3_000_000,
        uncompressed_size: 9_000_000,
      };
    }),
  },
};

assert.equal(companyDetailAssets(manifest, generation).length, 16);

const compressedOverflow = structuredClone(manifest);
compressedOverflow.company_details.shards[0].size += 1;
assert.throws(() => companyDetailAssets(compressedOverflow, generation), /company detail coverage mismatch/);

const uncompressedOverflow = structuredClone(manifest);
uncompressedOverflow.company_details.shards[0].uncompressed_size += 1;
assert.throws(() => companyDetailAssets(uncompressedOverflow, generation), /company detail coverage mismatch/);

const compressedAssetOverflow = structuredClone(manifest);
compressedAssetOverflow.company_details.shards[0].size = 8_000_001;
assert.throws(() => companyDetailAssets(compressedAssetOverflow, generation), /invalid company detail shard/);

const uncompressedAssetOverflow = structuredClone(manifest);
uncompressedAssetOverflow.company_details.shards[0].uncompressed_size = 24_000_001;
assert.throws(() => companyDetailAssets(uncompressedAssetOverflow, generation), /invalid company detail shard/);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_refresh_worker_deployment_uses_real_bindings_without_a_plaintext_key():
    config = json.loads(WRANGLER_CONFIG.read_text(encoding="utf-8"))

    assert config["workers_dev"] is True
    assert config["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "quant-market-data",
            "database_id": "1ea1f08e-640f-4e75-a25e-c47d0a41ae66",
        }
    ]
    assert config["r2_buckets"] == [{"binding": "DATA_BUCKET", "bucket_name": "quant-market-data"}]
    assert config["vars"] == {"GENERATION_RETENTION_COUNT": "8"}
    assert "REFRESH_KEY" not in config["vars"]
