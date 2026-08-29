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


def test_dashboard_labels_nonpositive_pe_as_loss_or_not_applicable():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the dashboard PE formatter"
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
const start = script.indexOf("function finiteNumber(value)");
const end = script.indexOf("function publicReasonText", start);
assert.ok(start >= 0 && end > start, "PE formatter source was not found");
const { peText } = new Function(script.slice(start, end) + ";return {peText};")();
assert.equal(peText(-35.03), "亏损/不适用（原始 PE -35.03）");
assert.equal(peText(0), "亏损/不适用（原始 PE 0.00）");
assert.equal(peText(12.345), "12.35");
assert.equal(peText(null), "—");
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert 'addFact(facts,"市盈率 PE",peText(r.pe))' in source


def test_ai_screening_labels_nonpositive_pe_as_loss_or_not_applicable():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the AI PE formatter"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const start = source.indexOf("    function compactNumber(value){");
const end = source.indexOf("    function economicProfile(review){", start);
assert.ok(start >= 0 && end > start, "AI PE formatter source was not found");
const { peResearchText } = new Function(source.slice(start, end) + ";return {peResearchText};")();
assert.equal(peResearchText(-35.03), "亏损/不适用（原始 PE -35.03）");
assert.equal(peResearchText(0), "亏损/不适用（原始 PE 0）");
assert.equal(peResearchText(12.345), "12.345倍");
assert.equal(peResearchText(null), "—");
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_dashboard_uses_signed_analysis_scope_and_type6_decision_contract():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the dashboard scope helpers"
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
const start = script.indexOf("const EXCLUSION_REASON_NAMES");
const end = script.indexOf("function formatBeijing", start);
assert.ok(start >= 0 && end > start, "analysis-scope helper source was not found");
const helpers = new Function(
  script.slice(start, end) + ";return {analysisScope,analysisScopeText,positionConfirmationRequired};",
)();

const analysisExclusions = {};
for (let index = 0; index < 204; index++) analysisExclusions["ST" + index] = "special_treatment";
analysisExclusions.suspended = "suspended_or_no_trade";
for (let index = 0; index < 3; index++) analysisExclusions["financial" + index] = "incomplete_financial_evidence";
analysisExclusions.stale = "stale_or_incomplete_current_financials";
analysisExclusions.interim = "missing_comparative_interim";
for (let index = 0; index < 7; index++) analysisExclusions["industry" + index] = "unclassified_industry";
analysisExclusions.bj = "unsupported_market";
const scope = helpers.analysisScope({
  provenance: {
    snapshot_validation: {
      analysis_market_quotes: 5207,
      eligible_companies: 4990,
      analysis_eligible_coverage: 4990 / 5207,
      analysis_exclusions: analysisExclusions,
    },
  },
}, 4990);
assert.deepEqual({ market: scope.market, eligible: scope.eligible, excluded: scope.excluded }, {
  market: 5207,
  eligible: 4990,
  excluded: 217,
});
const scopeText = helpers.analysisScopeText(scope, 4990);
for (const expected of [
  "沪深市场 5,207 家",
  "纳入 4,990 家（95.83%）",
  "未纳入 217 家",
  "ST/特别处理 204 家",
  "停牌或无成交 1 家",
  "财务证据不完整 3 家",
  "最新财务数据陈旧或不完整 1 家",
  "缺少同期中报 1 家",
  "行业未分类 7 家",
]) assert.ok(scopeText.includes(expected), expected);
assert.ok(!scopeText.includes("unsupported_market"));
assert.equal(helpers.analysisScope({}, 4990), null);
assert.equal(helpers.analysisScopeText(null, 4990), "纳入分析公司：4,990 家");

const observeAction = {
  status: "observe",
  decision: {
    decision_basis: "action_condition",
    potentially_triggerable: true,
    missing_dimensions: ["6e"],
  },
};
assert.equal(helpers.positionConfirmationRequired("type6", observeAction), true);
assert.equal(helpers.positionConfirmationRequired("type7", observeAction), false);
assert.equal(helpers.positionConfirmationRequired("type6", {
  decision: {
    decision_basis: "conservative_upper_bound",
    potentially_triggerable: false,
    missing_dimensions: ["6e"],
  },
}), false);
assert.equal(helpers.positionConfirmationRequired("type6", {
  decision: {
    decision_basis: "action_condition",
    potentially_triggerable: true,
    missing_dimensions: [],
  },
}), false);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    assert 'const cards=[["模型实际触发候选"' in source
    assert '"全市场公司"' not in source
    assert "m.provenance?.snapshot_validation" not in source
    assert "manifest?.provenance?.snapshot_validation" in source
    assert (
        "const hasPositionConfirmation=conditional.some(([key,value])=>positionConfirmationRequired(key,value))"
        in source
    )
    assert '点击对应类型查看附加条件"+(hasPositionConfirmation?"与仓位确认要求":"")' in source
    assert "点击对应类型查看附加条件与仓位确认要求。" not in source


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
const proxyVerificationDecision = {
  decision_complete: true,
  decision_basis: "conservative_upper_bound",
  potentially_triggerable: false,
  score_lower_bound: 2,
  score_upper_bound: 6,
  missing_dimensions: ["6b", "6c"],
};
const mixedVerificationDecision = {
  decision_complete: false,
  decision_basis: "unresolved_missing_evidence",
  potentially_triggerable: true,
  score_lower_bound: 2,
  score_upper_bound: 8,
  missing_dimensions: ["6a", "6b", "6c"],
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
    {
      code: "600001", name: "真实多类型触发", industry: "测试行业", diagnostic_score: 9.9,
      buy_types: ["type3", "type5"], primary_type: "type3", primary_label: "第三种情况",
      types: {
        type3: { status: "triggered", score: 8.5, applicable: true, evidence_complete: true },
        type5: { status: "triggered", score: 7.3, applicable: true, evidence_complete: true },
      },
    },
    {
      code: "300006", name: "代理证据待核验", industry: "测试行业", diagnostic_score: 4, primary_label: "",
      types: {
        type6: {
          status: "insufficient_evidence", score: null, applicable: true, evidence_complete: false,
          estimated_sub_score_reasons: {
            "6b": "未确认估算，不用于触发；技术模型代理证据，最高4分；须补可追溯原始资料",
            "6c": "未确认估算，不用于触发；商业模式模型代理证据，最高4分；须补可追溯原始资料",
          },
          decision: proxyVerificationDecision,
        },
      },
    },
    {
      code: "300007", name: "直接资料与代理均待补", industry: "测试行业", diagnostic_score: null, primary_label: "",
      types: {
        type6: {
          status: "insufficient_evidence", score: null, applicable: true, evidence_complete: false,
          estimated_sub_score_reasons: {
            "6b": "未确认估算，不用于触发；技术模型代理证据，最高4分；须补可追溯原始资料",
            "6c": "未确认估算，不用于触发；商业模式模型代理证据，最高4分；须补可追溯原始资料",
          },
          decision: mixedVerificationDecision,
        },
      },
    },
  ],
};
const catalogueRaw = Buffer.from(JSON.stringify(catalogue));
const catalogueBytes = gzipSync(catalogueRaw);
const manifest = {
  provenance: { methodology_version: "patch7-seven-types-buy-gate-2026-08-04-v5" },
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
const canonicalUrl = legacyUrl + "&index_contract=4";
cached.set(legacyUrl, new Response(JSON.stringify({ index_contract: 1, generation_id: "0123456789abcdef", summary: { company_count: 1 }, companies: [] })));
const response = await worker.fetch(
  new Request(canonicalUrl),
  env,
);
assert.equal(response.status, 200);
const payload = await response.json();
assert.equal(payload.index_contract, 4);
assert.equal(payload.methodology_version, "patch7-seven-types-buy-gate-2026-08-04-v5");
assert.equal(payload.methodology_current, true);
assert.equal(payload.summary.company_count, 8);
assert.equal(payload.summary.evidence_gap_company_count, 4);
assert.equal(payload.summary.source_gap_company_count, 3);
assert.equal(payload.summary.proxy_verification_company_count, 2);
assert.equal(payload.summary.decision_relevant_gap_company_count, 2);
assert.equal(payload.summary.source_decision_relevant_company_count, 2);
assert.equal(payload.summary.proxy_decision_relevant_company_count, 1);
assert.equal(payload.summary.bounded_gap_company_count, 2);
assert.equal(payload.summary.action_confirmation_company_count, 1);
assert.ok(cached.has(canonicalUrl));
assert.deepEqual(payload.summary.type_coverage.type3, {
  evidence_missing: 2,
  source_missing: 2,
  proxy_verification: 0,
  decision_relevant: 1,
  source_decision_relevant: 1,
  proxy_decision_relevant: 0,
  bounded: 1,
  decision_unresolved: 1,
  potentially_triggerable: 1,
  action_confirmation: 0,
});
assert.deepEqual(payload.summary.type_coverage.type6, {
  evidence_missing: 2,
  source_missing: 1,
  proxy_verification: 2,
  decision_relevant: 1,
  source_decision_relevant: 1,
  proxy_decision_relevant: 1,
  bounded: 1,
  decision_unresolved: 1,
  potentially_triggerable: 1,
  action_confirmation: 1,
});
assert.deepEqual(payload.companies[0].types.type3, {
  status: "insufficient_evidence",
  score: null,
  score_lower_bound: 0,
  score_upper_bound: 8,
  has_missing_dimensions: true,
  has_evidence_gap: true,
  source_gap: true,
  proxy_verification: false,
  decision_relevant: true,
  bounded: false,
});
assert.equal(payload.companies[1].types.type3.has_evidence_gap, true);
assert.equal(payload.companies[1].types.type3.source_gap, true);
assert.equal(payload.companies[1].types.type3.proxy_verification, false);
assert.equal(payload.companies[1].types.type3.decision_relevant, false);
assert.equal(payload.companies[1].types.type3.bounded, true);
assert.equal(payload.companies[2].types.type6.has_evidence_gap, false);
assert.equal(payload.companies[3].types.type6.has_evidence_gap, false);
assert.equal(payload.companies[4].types.type6.has_evidence_gap, false);
assert.equal("investor_action_dimensions" in payload.companies[2].types.type6, false);
const projectedSignal = payload.companies[5];
assert.deepEqual(projectedSignal.buy_types, ["type3", "type5"]);
assert.equal(projectedSignal.primary_type, "type3");
assert.equal(projectedSignal.primary_label, "第三种情况");
const projectedProxy = payload.companies[6].types.type6;
assert.equal(projectedProxy.has_evidence_gap, true);
assert.equal(projectedProxy.source_gap, false);
assert.equal(projectedProxy.proxy_verification, true);
assert.equal(projectedProxy.decision_relevant, false);
assert.equal(projectedProxy.bounded, true);
const projectedMixed = payload.companies[7].types.type6;
assert.equal(projectedMixed.source_gap, true);
assert.equal(projectedMixed.proxy_verification, true);
assert.equal(projectedMixed.decision_relevant, true);
assert.equal(projectedMixed.bounded, false);

const pageResponse = await worker.fetch(new Request("https://dashboard.test/"), env);
assert.equal(pageResponse.status, 200);
const pageHtml = await pageResponse.text();
const scriptMatch = pageHtml.match(/<script nonce="[^"]+">\s*([\s\S]*?)\s*<\/script>/);
assert.ok(scriptMatch, "generated dashboard script was not found");
const script = scriptMatch[1];
const helperStart = script.indexOf("const TYPE_NAMES");
const helperEnd = script.indexOf("function renderCoverage", helperStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, "ranking helper source was not found");
const helpers = new Function(
  script.slice(helperStart, helperEnd) + ";return {actualBuyTypes,primaryTriggeredType,primaryTriggeredScore};",
)();
assert.deepEqual(helpers.actualBuyTypes(projectedSignal), ["type5", "type3"]);
assert.equal(helpers.primaryTriggeredType(projectedSignal), "type3");
assert.equal(helpers.primaryTriggeredScore(projectedSignal), 8.5);
const reordered = await worker.fetch(
  new Request("https://dashboard.test/api/catalogue-index?index_contract=4&generation_id=0123456789abcdef"),
  env,
);
assert.equal(reordered.status, 200);
assert.equal((await reordered.json()).index_contract, 4);
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
const tamperedError = await tampered.json();
assert.equal(tamperedError.error, "服务器暂时无法完成请求");
assert.match(tamperedError.request_id, /^[0-9a-f-]{36}$/);
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
    assert 'if(status==="evidence_gap")return typeState?.has_evidence_gap===true' in source
    assert 'if(status==="decision_relevant")return typeState?.decision_relevant===true' in source
    assert 'if(status==="source_gap")return typeState?.source_gap===true' in source
    assert 'if(status==="proxy_verification")return typeState?.proxy_verification===true' in source
    assert 'if(status==="bounded")return typeState?.bounded===true' in source


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
    assert "七类诊断最高分（非买入信号）" in source
    assert "const exact=finiteNumber(typeResult.score)" in source
    assert "if(exact!==null&&!hasMissing)" in source
    assert "沪深股票收盘后数据的只读展示" not in source
    assert "精确分只来自已核验资料" not in source
    assert "function cleanedEstimateReason" in source
    assert "未核验参考 " in source
    assert "该值只帮助定位缺口，不参与触发或否决。" in source


def test_dashboard_defaults_to_real_triggers_and_explains_a_true_zero_result():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert '<option value="triggered">模型实际触发候选</option>' in source
    assert "typeState?.status===status" in source
    assert 's=q?"":$("status").value' in source
    assert "当前确实为 0 家命中" in source
    assert "没有找到匹配该代码或名称的公司" in source
    assert "displayedScore" in source
    assert "点击查看完整依据" in source


def test_dashboard_default_ranking_uses_only_triggered_types_and_the_primary_trigger_score():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the dashboard ranking helpers"
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
const start = script.indexOf("const TYPE_NAMES");
const end = script.indexOf("function renderCoverage", start);
assert.ok(start >= 0 && end > start, "ranking helper source was not found");
const helpers = new Function(
  script.slice(start, end) + ";return {actualBuyTypes,primaryTriggeredType,primaryTriggeredScore,compareRowsByScore,decisionCategoryCounts,rowMatches};",
)();

const deepDiagnosticOnly = {
  code: "300001",
  diagnostic_score: 9.9,
  types: { type3: { status: "observe", score: 9.9 } },
};
const type1Signal = {
  code: "600001",
  diagnostic_score: 9.8,
  types: { type1: { status: "triggered", score: 7.1 }, type3: { status: "vetoed", score: 9.8 } },
};
const multiSignal = {
  code: "600002",
  diagnostic_score: 9.7,
  buy_types: ["type3", "type5"],
  primary_type: "type3",
  types: { type3: { status: "triggered", score: 8.5 }, type5: { status: "triggered", score: 7.3 } },
};
const conditionalOnly = {
  code: "300002",
  diagnostic_score: 10,
  types: { type7: { status: "conditional", score: 10 } },
};

assert.deepEqual(helpers.actualBuyTypes(deepDiagnosticOnly), []);
assert.deepEqual(helpers.actualBuyTypes(conditionalOnly), []);
assert.deepEqual(helpers.actualBuyTypes(type1Signal), ["type1"]);
assert.deepEqual(helpers.actualBuyTypes(multiSignal), ["type5", "type3"]);
assert.equal(helpers.primaryTriggeredType(multiSignal), "type3");
assert.equal(helpers.primaryTriggeredScore(multiSignal), 8.5);
assert.deepEqual(
  helpers.actualBuyTypes({ buy_types: [], types: { type1: { status: "triggered", score: 9 } } }),
  [],
);
assert.deepEqual(
  helpers.actualBuyTypes({ buy_types: ["type7"], types: { type7: { status: "conditional", score: 9 } } }),
  [],
);
assert.equal(
  helpers.rowMatches(
    { _search: "stale", _market: "SZ", buy_types: [], types: { type1: { status: "triggered" } } },
    { q: "", m: "", t: "", s: "triggered", i: "" },
  ),
  false,
);
assert.equal(
  helpers.rowMatches(
    { _search: "live", _market: "SH", buy_types: ["type1"], types: { type1: { status: "triggered" } } },
    { q: "", m: "", t: "type1", s: "triggered", i: "" },
  ),
  true,
);
assert.deepEqual(
  [deepDiagnosticOnly, type1Signal, conditionalOnly, multiSignal]
    .sort((left, right) => helpers.compareRowsByScore(left, right, ""))
    .map(row => row.code),
  ["600002", "600001", "300002", "300001"],
);
assert.deepEqual(
  [type1Signal, conditionalOnly, multiSignal]
    .sort((left, right) => helpers.compareRowsByScore(left, right, "type7"))
    .map(row => row.code),
  ["300002", "600001", "600002"],
);
assert.deepEqual(
  helpers.decisionCategoryCounts(
    [type1Signal, conditionalOnly, deepDiagnosticOnly, { code: "300003", types: {} }],
    { visible_candidate_company_count: 3, pending_company_count: 1 },
  ),
  {
    companyCount: 4,
    triggeredCompanyCount: 1,
    conditionalOnlyCompanyCount: 1,
    pendingOnlyCompanyCount: 1,
    currentNoBuyCount: 1,
  },
);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    assert "七类诊断最高分（非买入信号）" in source
    assert "综合诊断分" not in source
    assert "conditional/pending 均不是买入信号" in source
    assert "首页前四项按“实际触发 → 待行动确认/附加条件 → 资料待补 → 其余当前不买”的顺序互斥归类" in source
    assert 'td.textContent=primaryType?TYPE_NAMES[primaryType]:"无触发（不买）"' in source
    assert "function decisionCategoryCounts(rows,summary={})" in source
    assert "triggeredCompanyCount=rows.filter(row=>actualBuyTypes(row).length>0).length" in source
    assert (
        "pendingOnlyCompanyCount=Math.max(0,visibleCandidateCompanyCount-triggeredCompanyCount-conditionalOnlyCompanyCount)"
        in source
    )
    assert (
        "currentNoBuyCount=Math.max(0,companyCount-triggeredCompanyCount-conditionalOnlyCompanyCount-pendingOnlyCompanyCount)"
        in source
    )


def test_dashboard_verdict_treats_market_blocks_as_non_signals():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the dashboard verdict helper"
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
const start = script.indexOf("const TYPE_NAMES");
const end = script.indexOf("function renderVerdict", start);
assert.ok(start >= 0 && end > start, "verdict helper source was not found");
const { verdictModel } = new Function(script.slice(start, end) + ";return {verdictModel};")();

const blocked = verdictModel({ types: { type2: { status: "blocked" }, type3: { status: "observe" } } });
assert.equal(blocked.tagText, "市场状态阻断");
assert.equal(blocked.tone, "blocked");
assert.match(blocked.note, /不是买入信号/);
assert.match(blocked.note, /两热一冷/);

const conditional = verdictModel({ types: { type6: { status: "conditional" } } });
assert.equal(conditional.tagText, "待确认（非买入信号）");
assert.match(conditional.note, /仍不是买入信号/);

const staleTrigger = verdictModel({ buy_types: [], types: { type1: { status: "triggered" } } });
assert.equal(staleTrigger.tagText, "当前不买");
assert.match(staleTrigger.note, /没有买入信号/);

const actualTrigger = verdictModel({ buy_types: ["type1"], types: { type1: { status: "triggered" } } });
assert.equal(actualTrigger.tagText, "模型实际触发候选");
assert.match(actualTrigger.note, /已触发/);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_dashboard_coverage_cards_separate_signals_conditions_and_unresolved_evidence():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "function coverageEvidenceStats" in source
    assert "Object.entries(TYPE_NAMES).map" in source
    assert 'value?.status!=="triggered"&&value?.status!=="conditional"' in source
    assert 'decision.decision_basis==="unresolved_missing_evidence"' in source
    assert "decision.potentially_triggerable===true" in source
    assert (
        'preferredStatus=triggered>0?"triggered":conditional>0?"conditional":evidence.decisionRelevant>0?"decision_relevant"'
        in source
    )
    assert "function conditionalCoverageLabel" in source
    assert "action_confirmation" in source
    for text in (
        "七类实际触发、待确认与证据完整度分布",
        "已触发”才是实际买入候选",
        "结论相关",
        "直接/结构化资料",
        "代理待核验",
        "结论已锁定",
        "并非抓取失败",
        "两者可重叠",
        "补齐后仍可能触发 ",
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


def test_dashboard_rejects_write_methods_and_advertises_get_and_head():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute dashboard method routes"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;

for (const method of ["POST", "OPTIONS"]) {
  const response = await worker.fetch(new Request("https://dashboard.test/api/meta", { method }), {});
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD");
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.deepEqual(await response.json(), { error: "只读接口不接受写请求" });
}
const generation = { generation_id: "0123456789abcdef", market_as_of: "2026-08-28", source_commit: "a".repeat(40) };
const env = { DB: { prepare: () => ({ bind() { return this; }, first: async () => generation }) } };
for (const path of ["/api/methodology", "/api/meta"]) {
  const response = await worker.fetch(new Request("https://dashboard.test" + path, { method: "HEAD" }), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
}
const missing = await worker.fetch(new Request("https://dashboard.test/missing", { method: "HEAD" }), env);
assert.equal(missing.status, 404);
assert.equal(await missing.text(), "");
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_refresh_worker_validates_signed_manifest_scalar_metadata():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    validator = r"""
import assert from "node:assert/strict";
let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source + "\nexport { validateManifestMetadata };\n").toString("base64");
const { validateManifestMetadata } = await import(url);
const valid = {
  market_as_of: "2026-08-28",
  data_timestamp_utc: "2026-08-28T08:20:00Z",
  generated_at_utc: "2026-08-28T08:21:00Z",
  provenance: { source_commit: "a".repeat(40) },
  summary: { company_count: 5000, triggered_company_count: 100, conditional_company_count: 50, pending_company_count: 300 },
};
assert.doesNotThrow(() => validateManifestMetadata(valid));
for (const mutation of [
  value => { value.market_as_of = "2026-02-30"; },
  value => { value.generated_at_utc = "not-a-date"; },
  value => { value.provenance.source_commit = "short"; },
  value => { value.summary.company_count = 0; },
  value => { value.summary.pending_company_count = 5001; },
]) {
  const value = structuredClone(valid);
  mutation(value);
  assert.throws(() => validateManifestMetadata(value));
}
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


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
  const catalogueBytes = Buffer.alloc(120, 1);
  const signalsBytes = Buffer.alloc(80, 2);
  const signatureBytes = Buffer.concat([
    Buffer.from([0x30, 0x44, 0x02, 0x20]), Buffer.alloc(32, 1),
    Buffer.from([0x02, 0x20]), Buffer.alloc(32, 2),
  ]);
  const digest = bytes => createHash("sha256").update(bytes).digest("hex");
  const manifest = {
    catalogue: { filename: "catalogue.json.gz", size: 120, sha256: digest(catalogueBytes), uncompressed_size: 240 },
    signals: { filename: "signals.json.gz", size: 80, sha256: digest(signalsBytes), uncompressed_size: 160 },
    signature: { filename: "manifest-0123456789abcdef.sig" },
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
  [prefix + "catalogue.json.gz", objectFor(120, catalogueBytes, digest(catalogueBytes))],
  [prefix + "signals.json.gz", objectFor(80, signalsBytes, digest(signalsBytes))],
  [prefix + "manifest-0123456789abcdef.sig", objectFor(signatureBytes.byteLength, signatureBytes, digest(signatureBytes))],
  ]);
  const realCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: { subtle: {
    digest: (...args) => realCrypto.subtle.digest(...args),
    importKey: (...args) => realCrypto.subtle.importKey(...args),
    verify: async () => true,
  } } });
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
  assert.equal(getCount, 4);
  assert.equal(headCount, 0);

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
result = tradingDataFreshness("2026-08-03", "2026-08-03T08:19:00Z", mondayBeforeDeadline);
assert.equal(result.stale, false);
assert.equal(result.expected_market_as_of, "2026-08-03");
assert.equal(result.market_date_current, true);
assert.equal(
  tradingDataFreshness("2026-08-03", "2026-08-03T06:59:00Z", mondayBeforeDeadline).stale_reason,
  "市场日期晚于最近已收盘交易日",
);
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
        "source_gap_company_count",
        "proxy_verification_company_count",
        "source_decision_relevant_company_count",
        "proxy_decision_relevant_company_count",
        "bounded_gap_company_count",
        "待行动确认/附加条件（非信号）",
        "其中待确认仓位",
        "资料待补候选（非信号）",
        "当前不买（其余公司）",
        "结论相关资料待补（并集）",
        "其中缺直接/结构化证据（可重叠）",
        "不等同于抓取失败，也不保证能够补齐",
        "其中量化代理待核验（可重叠）",
        "未完全核验但结论已锁定",
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
    assert "function proxyVerificationDimensions(value,dimensions)" in source
    assert '.includes("模型代理证据，最高4分")' in source
    assert 'actionRequired=actionDimensions.has("6e")&&missing.includes("6e")&&value?.status==="conditional"' in source
    assert "declaredIncomplete=value?.applicable===true&&value?.evidence_complete===false" in source
    assert "source_gap:gap.source_gap" in source
    assert "proxy_verification:gap.proxy_verification" in source
    assert "decision_relevant:gap.decision_relevant" in source
    assert "bounded:gap.bounded" in source
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
    assert 'positionAction=dimension==="6e"&&positionConfirmationRequired(k,v)' in source
    assert 'positionAction?(v.status==="conditional"?"待仓位确认":"仓位待输入")' in source
    assert 'inactivePositionAction?"当前无需确认"' in source
    assert "仓位输入仍可能改变评分上界，当前尚非买入信号" in source


def test_dashboard_uses_plain_language_version_and_exposes_only_traceable_detail_facts():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'const METHODOLOGY_LABEL="七类量化买入方法+补丁7总闸门（2026年8月）"' in source
    assert 'const METHODOLOGY_VERSION="patch7-seven-types-buy-gate-2026-08-04-v5"' in source
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


def test_dashboard_rule_sources_match_the_signed_manifest_contract():
    from engine.model_sources import public_model_source_contract

    source = DASHBOARD.read_text(encoding="utf-8")
    worker_sources = {
        source_id: {"filename": filename, "sha256": sha256.casefold()}
        for source_id, filename, sha256 in re.findall(
            r'Object\.freeze\(\{id:"([^"]+)",filename:"([^"]+)",sha256:"([0-9A-F]{64})"\}\)',
            source,
        )
    }
    published = public_model_source_contract()

    assert worker_sources == published["documents"]
    assert f"precedence:Object.freeze({json.dumps(published['precedence'], separators=(',', ':'))})" in source
    for key, value in published["resolutions"].items():
        assert f'{key}:"{value}"' in source


def test_methodology_api_exposes_patch7_type5_sell_scope_thresholds_and_source_hashes():
    source = DASHBOARD.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the methodology API contract"
    validator = r"""
import assert from "node:assert/strict";

let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const worker = (await import(url)).default;
const response = await worker.fetch(new Request("https://dashboard.test/api/methodology"), {});
assert.equal(response.status, 200);
const payload = await response.json();

assert.equal(payload.schema_version, 2);
assert.equal(payload.methodology_version, "patch7-seven-types-buy-gate-2026-08-04-v5");
assert.equal(payload.decision_domain, "new_buy_or_add");
assert.equal(payload.qualify_threshold, 7);
assert.equal(payload.patch7_total_gate.version, "2026-08-04");
assert.equal(payload.patch7_total_gate.rules.length, 4);
assert.match(payload.patch7_total_gate.rules[0].requirement, /价值陷阱/);
assert.match(payload.patch7_total_gate.rules[1].requirement, /乐观值×120%/);
assert.match(payload.patch7_total_gate.rules[2].requirement, /合理或低估/);
assert.equal(payload.patch7_total_gate.rules[3].when, "every_still_triggered_type");
assert.deepEqual(
  payload.patch7_total_gate.rules[3].types,
  ["type1", "type2", "type3", "type4", "type5", "type6", "type7"],
);
assert.match(payload.patch7_total_gate.rules[3].requirement, /未来自由现金流前提/);
assert.match(payload.patch7_total_gate.rules[3].requirement, /明确失败则否决/);
assert.match(payload.patch7_total_gate.rules[3].requirement, /资料不足则待补/);
assert.match(payload.patch7_total_gate.outside_extra_gate, /共同未来自由现金流前提/);

assert.equal(payload.type5_appendix.applicability, "5a≥7");
assert.equal(payload.type5_appendix.trigger, "5b至5e证据完整且五项加权总分≥7");
assert.match(payload.type5_appendix.no_extra_gate, /5c/);

assert.equal(payload.sell_domain.implemented, false);
assert.equal(payload.sell_domain.default_state, "持有");
assert.equal(payload.sell_domain.core_rule, "买入不触发≠卖出");
assert.match(payload.sell_domain.reason, /持仓成本/);
assert.equal(payload.sell_domain.hard_triggers.length, 4);
assert.equal(payload.sell_domain.soft_permission, "短期获利丰厚时可极小减仓，非必须动作");

assert.equal(payload.rule_source_contract.hash_algorithm, "SHA-256");
assert.equal(payload.rule_source_contract.source_count, 6);
assert.equal(payload.rule_source_contract.sources.length, 6);
for (const item of payload.rule_source_contract.sources) assert.match(item.sha256, /^[0-9A-F]{64}$/);
assert.equal(
  payload.rule_source_contract.sources.find(item => item.id === "patch6").filename,
  "补丁6· 公司三属性分类与三维度量化打分机制.md",
);
assert.equal(
  payload.rule_source_contract.sources.find(item => item.id === "patch7").filename,
  "补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md",
);
assert.equal(
  payload.rule_source_contract.sources.find(item => item.id === "patch7").sha256,
  "69B6BBEAA44755B9935518C665BC1AC0CAC5C473AABA5B106BDF0F9FC88BEB6D",
);
assert.equal(
  payload.rule_source_contract.sources.find(item => item.id === "subsequent_addenda").filename,
  "后续附加补丁们.md",
);
assert.deepEqual(payload.rule_source_contract.precedence, [
  "current_independent_patch6_and_patch7",
  "independent_templates_and_patches_1_to_5",
  "subsequent_addenda_unique_content",
  "historical_aggregations",
]);
assert.match(payload.rule_source_contract.resolutions.type6, /five_dimension_quantification/);
for (const typeKey of ["type1", "type2", "type3", "type4", "type5", "type6", "type7"]) {
  assert.ok(Array.isArray(payload.types[typeKey].key_thresholds));
  assert.ok(payload.types[typeKey].key_thresholds.length > 0);
  assert.match(payload.types[typeKey].key_thresholds.join("；"), /补丁7共同前提.*自由现金流/);
}
assert.match(payload.types.type1.key_thresholds.join("；"), /行业专属中位增长率及样本量/);
assert.match(payload.types.type1.key_thresholds.join("；"), /最近连续4年收入/);
assert.match(payload.types.type1.key_thresholds.join("；"), /不默认通过/);
assert.match(payload.types.type6.key_thresholds.join("；"), /间接代理最多4分/);
assert.match(payload.types.type6.key_thresholds.join("；"), /不计入核心项达到5分/);

const pageResponse = await worker.fetch(new Request("https://dashboard.test/"), {});
assert.equal(pageResponse.status, 200);
const pageHtml = await pageResponse.text();
assert.match(pageHtml, /行业专属增长基准须有中位数和样本量/);
assert.match(pageHtml, /最近连续4年收入/);
assert.match(pageHtml, /资料待补、不默认通过/);
assert.match(pageHtml, /七类共同前提/);
assert.match(pageHtml, /资料不足则待补，不能发布为实际触发/);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", validator],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    for text in (
        "默认仅展示至少一个类型状态为已触发的公司",
        "待行动/附加确认、资料待补、观察、未触发均不是买入信号",
        "买入不触发≠卖出",
        "当前网站不生成卖出信号",
        "补丁7跨类型总闸门",
        "七类共同前提",
        "资料待补、不默认通过",
        "资料不足则待补，不能发布为实际触发",
    ):
        assert text in source


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
    assert 'selectedTypeMatches=s==="triggered"?actualBuyTypes(r).includes(t):typeStatusMatches(typeState,s)' in source
    assert 's==="triggered"?actualBuyTypes(r).length>0' in source
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


def test_refresh_worker_bounds_primary_decompression_and_r2_verify_put_concurrency():
    source = REFRESH_WORKER.read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the refresh resource contracts"
    validator = r"""
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { createHash } from "node:crypto";

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

const objects = Array.from({ length: 11 }, (_, index) => {
  const bytes = new Uint8Array([index]);
  return {
    name: `asset-${index}`,
    key: `generations/0123456789abcdef/asset-${index}`,
    body: bytes.buffer,
    contentType: "application/json",
    contentEncoding: null,
    expectedSize: 1,
    expectedHash: createHash("sha256").update(bytes).digest("hex"),
  };
});
let activeGets = 0;
let maxGets = 0;
const inspected = await inspectGenerationObjects({
  async get(key) {
    activeGets += 1;
    maxGets = Math.max(maxGets, activeGets);
    await new Promise((resolve) => setTimeout(resolve, 2));
    activeGets -= 1;
    const index = Number(key.split("-").at(-1));
    const bytes = new Uint8Array([index]);
    const hash = createHash("sha256").update(bytes).digest("hex");
    return { size: 1, body: new Response(bytes).body, customMetadata: { sha256: hash } };
  },
}, objects);
assert.equal(inspected.length, objects.length);
assert.equal(maxGets, 4);
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


def test_ai_screening_route_is_read_only_generation_bound_and_csp_protected(tmp_path):
    source = DASHBOARD.read_text(encoding="utf-8")
    page_function = source.split("function aiScreeningPageResponse(request){", 1)[1].split("export default{", 1)[0]
    assert 'if(path==="/ai-screening")return aiScreeningPageResponse(request);' in source
    assert source.count("function aiScreeningPageResponse(request)") == 1
    assert "aiScreeningPageResponseSimple" not in source
    assert "aiScreeningPageResponseV2" not in source
    assert "legacyAiScreeningPageResponse" not in source
    assert "MutationObserver" not in source
    assert 'class="meta" id="meta"' not in page_function
    assert '<div class="notice">' not in page_function
    assert "原生搜索事件证明" not in page_function
    assert "访问/正文不可用" in source
    assert "公司、期间或数字不匹配" in source
    assert "查看来源核验明细" in source
    assert "原生搜索事件已核验 · 财报来源已核验" in source
    assert "仅模型声明已搜索 · 无运行事件证明" in source
    assert "已移除无效来源" in source
    assert "AI独立判断" in source
    assert "AI独立建议 · 接近达标" not in source
    assert "AI_RULE_REASON_RE" in source
    assert "const displayHtml=" not in source
    assert "const sourceSemanticsHtml=" not in source
    assert "const finalHtml=" not in source
    assert ".replace(" not in page_function
    assert "AI为什么这样判断" in source
    assert "humanExplanation" in source
    assert "知识库是检查清单，不替代公司事实" in source
    assert "aiScreeningChangesPageResponse" in source
    assert 'if(path==="/ai-screening-changes")return aiScreeningChangesPageResponse(request);' in source
    assert "ai-review-details" in source
    assert "查看完整 AI 解释" in source
    assert "代码：" in source
    assert "公司量化事实" in source
    assert "公司商业画像" in source
    assert "估值与安全边际" in source
    assert "主要反证与风险" in source
    assert "公司资料与来源" in source
    assert "为何进入研究池（仅说明候选范围）" in source
    assert "这里仅说明公司为何进入 AI 研究范围，不是 AI 的最终结论" in source
    assert "确定性前筛状态：" in source
    assert "candidate-rules" in source
    assert "quantitative_facts" in source
    assert "economic_category" in source
    assert "score_components" in source
    assert "calibration_adjustments" in source
    assert "evidence_bindings" in source
    assert "search_findings" in source
    assert "normalization_anchor" in source
    assert "multiple_basis" in source
    assert "function evidenceLinks" in source
    assert "Ox Alpha Free" in source
    assert "DeepSeek V4 Flash（OpenCode Go）" in source
    assert "Array.isArray(packet.type_keys)" in source
    assert 'types.filter(Boolean).join(" / ")' in source
    assert "MAX_AI_SCREENING_BYTES=32*1024*1024" in source
    assert 'env.DATA_BUCKET.get("ai-screening/"+generation.generation_id+".json")' in source
    assert "value.ai_is_advisory!==true" in source
    assert "value.auto_buy_promotion!==false" in source
    node = shutil.which("node")
    assert node is not None
    validator = r"""
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
let source = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) source += chunk;
const url = "data:text/javascript;base64," + Buffer.from(source + "\nexport { sourceSemanticProjectionDigest };\n").toString("base64");
const loadedWorker = await import(url);
const worker = loadedWorker.default;
const sourceSemanticProjectionDigest = loadedWorker.sourceSemanticProjectionDigest;
const generation = { generation_id: "0123456789abcdef", market_as_of: "2026-08-13" };
assert.deepEqual(await sourceSemanticProjectionDigest({
  review_mode: "opencode_native_company_research_review",
  packets: [{ security_code: "600000", name: "Demo Co", type_key: "type1", ai_review: {
    claims: [{ statement: "cash flow improved", source_ref: "https://example.test/report", source_context: "https://example.test/report filing", support: "supports", search_finding_id: "finding-1", source_kind: "official_filing" }],
    search_findings: [{ id: "finding-1", query: "Demo Co filing", title: "Interim report", url: "https://example.test/report", published_at: "2026-08-20", report_period: "2026H1", finding: "cash flow improved", stance: "support", source_kind: "official_filing", source_quality: "primary" }],
  } }],
}), {
  projection_sha256: "1f3d738f5225266bcaec750adbb06f03f9ada417c6eba6a20e666d063fbab47f",
  projection_company_count: 1,
  projection_claim_count: 1,
  projection_search_finding_count: 1,
  projection_source_reference_count: 2,
  projection_unique_url_count: 1,
});
const bindCandidateIdentities = value => {
  const pairs = value.packets.flatMap(packet => packet.type_keys.map(type => [String(packet.security_code), String(type)]));
  pairs.sort((left, right) => left[0] === right[0] ? Number(left[1].slice(4)) - Number(right[1].slice(4)) : left[0] < right[0] ? -1 : 1);
  const digest = createHash("sha256").update(pairs.map(([code, type]) => code + "\0" + type + "\n").join(""), "utf8").digest("hex");
  value.candidate_identity_sha256 = digest;
  value.candidate_universe_identity_sha256 = digest;
  value.type_pair_candidate_identity_sha256 = digest;
  value.type_pair_universe_identity_sha256 = digest;
  return value;
};
const valuationSnapshot = (securityCode, valuation) => {
  const number = value => value === null || value === undefined || value === "" ? null : Number(value).toFixed(8).replace(/0+$/, "").replace(/\.$/, "") || "0";
  const snapshot = { contract_version: 1, security_code: securityCode, snapshot_generation: generation.generation_id, market_as_of: generation.market_as_of, current_price: valuation.current_price, pe: valuation.pe, pb: valuation.pb, market_cap: valuation.market_cap };
  const canonical = { contract_version: 1, current_price: number(snapshot.current_price), market_as_of: snapshot.market_as_of, market_cap: number(snapshot.market_cap), pb: number(snapshot.pb), pe: number(snapshot.pe), security_code: snapshot.security_code, snapshot_generation: snapshot.snapshot_generation };
  snapshot.canonical_sha256 = createHash("sha256").update(JSON.stringify(canonical), "utf8").digest("hex");
  return snapshot;
};
const artifact = {
  schema_version: 2,
  review_schema_version: 2,
  artifact_kind: "ai_screening_overlay",
  ai_is_advisory: true,
      auto_buy_promotion: false,
      full_coverage_final_recommendation: true,
      review_mode: "local_codex_review",
      review_models: ["opencode-go/ox-alpha-free"],
      review_efforts: ["max"],
      snapshot_generation: generation.generation_id,
      market_as_of: "2026-08-13",
      candidate_total: 1,
      candidate_identity_sha256: "204f58fc8253c17d36a5b3125999811155572bfc950162469554e0ef9cf622b4",
      candidate_universe_identity_sha256: "204f58fc8253c17d36a5b3125999811155572bfc950162469554e0ef9cf622b4",
      type_pair_candidate_identity_sha256: "204f58fc8253c17d36a5b3125999811155572bfc950162469554e0ef9cf622b4",
      type_pair_universe_identity_sha256: "204f58fc8253c17d36a5b3125999811155572bfc950162469554e0ef9cf622b4",
      type_pair_unique_company_count: 1,
      candidate_offset: 0,
      type_pair_candidate_total: 1,
      type_pair_expected_total: 1,
      type_pair_reviewed_count: 1,
      type_pair_unreviewed_count: 0,
      type_pair_needs_review_count: 0,
      type_pair_verdict_counts: { confirmed: 0, caution: 1, misclassified: 0, missed_candidate: 0, needs_review: 0 },
      type_pair_web_search_attempted_count: 0,
      type_pair_web_search_completed_count: 0,
      type_pair_web_search_event_verified_count: 0,
      type_pair_web_search_claim_urls_verified_count: 0,
      type_pair_web_search_dropped_claim_url_count: 0,
      reviewed_count: 1,
      attempted_review_count: 1,
      unreviewed_candidate_count: 0,
      attempted_needs_review_count: 0,
      completed_review_count: 1,
      pending_review_count: 0,
      verdict_counts: { confirmed: 0, caution: 1, misclassified: 0, missed_candidate: 0, needs_review: 0 },
      web_search_attempted_count: 0,
      reviewed_without_web_search: 1,
      web_search_completed_count: 0,
      web_source_verified_count: 0,
      web_search_event_verified_count: 0,
      web_search_claim_urls_verified_count: 0,
      web_search_dropped_claim_url_count: 0,
      ai_action_counts: { priority_buy: 0, watchlist: 1, avoid: 0, insufficient_evidence: 0 },
      final_category_counts: { recommend_buy: 0, observe: 1, do_not_recommend: 0 },
      recommend_buy_count: 0,
      do_not_recommend_buy_count: 1,
      priority_buy_count: 0,
      watchlist_count: 1,
      avoid_count: 0,
       insufficient_evidence_count: 0,
       freshness_counts: { current_or_recent: 0, historical: 1, undated: 0 },
       packets: [{
    ai_rank: 1,
    security_code: "600339",
    type_key: "type1",
    type_keys: ["type1"],
    type_pair_count: 1,
    deterministic: { status: "triggered", score: 7.8 },
            ai_review: { verdict: "caution", recommended_action: "manual_review", buy_attractiveness_score: 64, ai_action: "watchlist", final_category: "observe", final_recommendation: "do_not_recommend_buy", recommendation_label: "观察·需更新资料", ai_independent: false, confidence: "medium", summary: "当前资料较旧，列入观察并等待更新。", key_strengths: ["2025年经营现金流同比改善"], quantitative_facts: ["估值快照：PE 8.4 倍；2025年经营现金流 12.3 亿元"], risk_flags: [], claims: [], model: "opencode-go/ox-alpha-free", effort: "max", web_search_performed: false, web_search_event_verified: false, web_search_claim_urls_verified: false, web_search_verified: false, web_search_query_count: 0, web_search_verified_claim_url_count: 0, web_search_dropped_claim_url_count: 0, freshness_status: "historical", freshness_years: [2024], freshness_penalty: 8, freshness_note: "主要事实只到 2024 年或更早" },
  }],
};
const bytes = new TextEncoder().encode(JSON.stringify(artifact));
const objects = new Map([["ai-screening/0123456789abcdef.json", {
  size: bytes.byteLength,
  arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
}]]);
const env = {
  DB: { prepare: () => ({ bind() { return this; }, first: async () => generation }) },
  DATA_BUCKET: { get: async key => objects.get(key) || null },
};
let response = await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env);
assert.equal(response.status, 200);
    let payload = await response.json();
    assert.equal(payload.snapshot_generation, generation.generation_id);
    assert.equal(payload.ai_is_advisory, true);
    assert.deepEqual(payload.review_models, ["opencode-go/ox-alpha-free"]);
    assert.deepEqual(payload.review_efforts, ["max"]);
    assert.match(response.headers.get("etag") || "", /^"[0-9a-f]{64}"$/);
    assert.equal(response.headers.get("cache-control"), "public, max-age=60, stale-while-revalidate=300");
    const notModified = await worker.fetch(new Request("https://dashboard.test/api/ai-screening", { headers: { "if-none-match": response.headers.get("etag") } }), env);
    assert.equal(notModified.status, 304);
    const immutableResponse = await worker.fetch(new Request("https://dashboard.test/api/ai-screening?generation_id=0123456789abcdef"), env);
    assert.equal(immutableResponse.status, 200);
    assert.equal(immutableResponse.headers.get("cache-control"), "public, max-age=31536000, immutable");
    const statusForArtifact = async value => {
      const valueBytes = new TextEncoder().encode(JSON.stringify(value));
      objects.set("ai-screening/0123456789abcdef.json", {
        size: valueBytes.byteLength,
        arrayBuffer: async () => valueBytes.buffer.slice(valueBytes.byteOffset, valueBytes.byteOffset + valueBytes.byteLength),
      });
      return (await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env)).status;
    };
    let oversizedBodyRead = false;
    objects.set("ai-screening/0123456789abcdef.json", {
      size: 32 * 1024 * 1024 + 1,
      arrayBuffer: async () => {
        oversizedBodyRead = true;
        throw new Error("oversized AI artifact body must not be read");
      },
    });
    assert.equal((await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env)).status, 404);
    assert.equal(oversizedBodyRead, false);
    objects.set("ai-screening/0123456789abcdef.json", {
      size: 32 * 1024 * 1024,
      arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    });
    assert.equal((await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env)).status, 404);
    const largeArtifact = structuredClone(artifact);
    largeArtifact.test_padding = "x".repeat(9 * 1024 * 1024);
    const largeBytes = new TextEncoder().encode(JSON.stringify(largeArtifact));
    assert.ok(largeBytes.byteLength > 8 * 1024 * 1024);
    assert.ok(largeBytes.byteLength < 32 * 1024 * 1024);
    objects.set("ai-screening/0123456789abcdef.json", {
      size: largeBytes.byteLength,
      arrayBuffer: async () => largeBytes.buffer.slice(largeBytes.byteOffset, largeBytes.byteOffset + largeBytes.byteLength),
    });
    assert.equal((await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env)).status, 200);
    const muse = structuredClone(artifact);
    muse.review_models = ["opencode-go/muse-spark-1.2-contributor"];
    muse.review_efforts = ["xhigh"];
    muse.packets[0].ai_review.model = "opencode-go/muse-spark-1.2-contributor";
    muse.packets[0].ai_review.effort = "xhigh";
    assert.equal(await statusForArtifact(muse), 200);
    const wrongMuseEffort = structuredClone(muse);
    wrongMuseEffort.review_efforts = ["max"];
    wrongMuseEffort.packets[0].ai_review.effort = "max";
    assert.equal(await statusForArtifact(wrongMuseEffort), 404);
    const external = structuredClone(artifact);
    external.review_mode = "opencode_web_review";
    external.full_coverage_web_search = true;
    external.reviewed_without_web_search = 0;
    external.type_pair_web_search_attempted_count = 1;
    external.type_pair_web_search_event_verified_count = 1;
    external.type_pair_web_search_claim_urls_verified_count = 1;
    external.web_search_attempted_count = 1;
    external.web_search_event_verified_count = 1;
    external.web_search_claim_urls_verified_count = 1;
    external.source_audit = { available: true, invalid_claim_url_count: 0, failed: 0 };
    external.packets[0].ai_review.web_search_performed = true;
    external.packets[0].ai_review.web_search_event_verified = true;
    external.packets[0].ai_review.web_search_claim_urls_verified = true;
    external.packets[0].ai_review.web_search_query_count = 1;
    assert.equal(await statusForArtifact(external), 200);
    const droppedClaim = structuredClone(external);
    droppedClaim.type_pair_web_search_dropped_claim_url_count = 1;
    droppedClaim.web_search_dropped_claim_url_count = 1;
    droppedClaim.packets[0].ai_review.web_search_dropped_claim_url_count = 1;
    assert.equal(await statusForArtifact(droppedClaim), 200);
    const missingCardEvent = structuredClone(external);
    missingCardEvent.packets[0].ai_review.web_search_event_verified = false;
    assert.equal(await statusForArtifact(missingCardEvent), 404);
    const missingTopEvent = structuredClone(external);
    missingTopEvent.web_search_event_verified_count = 0;
    assert.equal(await statusForArtifact(missingTopEvent), 404);
    const missingClaimBinding = structuredClone(external);
    missingClaimBinding.packets[0].ai_review.web_search_claim_urls_verified = false;
    assert.equal(await statusForArtifact(missingClaimBinding), 404);
    const codexLuna = structuredClone(external);
    codexLuna.review_mode = "codex_luna_web_review";
    codexLuna.review_models = ["codex-luna-max"];
    codexLuna.review_efforts = ["max"];
    codexLuna.type_pair_web_search_completed_count = 1;
    codexLuna.type_pair_research_source_urls_verified_count = 1;
    codexLuna.web_search_completed_count = 1;
    codexLuna.web_source_verified_count = 1;
    codexLuna.research_source_urls_verified_count = 1;
    Object.assign(codexLuna.packets[0].ai_review, {
      model: "codex-luna-max",
      effort: "max",
      web_search_verified: true,
      web_search_verified_claim_url_count: 1,
      research_source_urls_verified: true,
      claims: [{ statement: "2026年报告披露经营现金流改善。", source_ref: "https://example.test/report" }],
      source_verification_status: "pass",
      source_verification_issue_count: 0,
      source_verification_issues: [],
      source_verification_issue_kinds: {},
    });
    const codexProjection = await sourceSemanticProjectionDigest(codexLuna);
    codexLuna.source_audit = {
      available: true,
      audit_contract_version: 3,
      audit_passed: true,
      audit_sha256: "a".repeat(64),
      merged_sha256: "b".repeat(64),
      ...codexProjection,
      checked: 1,
      ok: 1,
      failed: 0,
      blocked: 0,
      invalid: 0,
      invalid_claim_url_count: 0,
      semantic_claim_count: codexProjection.projection_claim_count,
      semantic_passed_count: codexProjection.projection_claim_count,
      semantic_failed_count: 0,
      semantic_unverified_count: 0,
      company_pass_count: 1,
      company_failed_count: 0,
      company_unverified_count: 0,
      affected_company_count: 0,
      network_warnings_allowed: false,
      release_status: "passed",
      company_coverage: [{ security_code: "600339", status: "pass", semantic_claim_count: codexProjection.projection_claim_count, semantic_passed_count: codexProjection.projection_claim_count, semantic_failed_count: 0, semantic_unverified_count: 0 }],
    };
    assert.equal(await statusForArtifact(codexLuna), 200);
    const codexWarningRelease = structuredClone(codexLuna);
    Object.assign(codexWarningRelease.source_audit, { audit_passed: false, failed: 1, ok: 0, network_warnings_allowed: true, release_status: "passed_with_source_access_warnings" });
    assert.equal(await statusForArtifact(codexWarningRelease), 404);
    const codexPacketWarning = structuredClone(codexLuna);
    Object.assign(codexPacketWarning.packets[0].ai_review, { source_verification_status: "unverified", source_verification_issue_count: 1, source_verification_issues: [{ kind: "access" }], source_verification_issue_kinds: { access: 1 } });
    assert.equal(await statusForArtifact(codexPacketWarning), 404);
    const companyResearch = structuredClone(external);
    companyResearch.review_mode = "opencode_native_company_research_review";
    companyResearch.review_models = ["opencode-go/muse-spark-1.2-contributor"];
    companyResearch.review_efforts = ["xhigh"];
    companyResearch.research_as_of = "2026-08-15";
    companyResearch.web_search_claim_urls_verified_count = 0;
    companyResearch.type_pair_web_search_claim_urls_verified_count = 0;
    companyResearch.research_source_urls_verified_count = 1;
    companyResearch.type_pair_research_source_urls_verified_count = 1;
    companyResearch.source_audit = { available: true, audit_contract_version: 3, audit_passed: true, audit_sha256: "c".repeat(64), merged_sha256: "b".repeat(64), invalid_claim_url_count: 0, checked: 1, ok: 1, failed: 0, blocked: 0, invalid: 0, claim_count: 3, semantic_claim_count: 3, semantic_passed_count: 3, semantic_failed_count: 0, semantic_unverified_count: 0, canonical_urls: ["https://example.test/financial-api", "https://example.test/financial-api#cashflow", "https://example.test/financial-api#valuation"], source_bindings: [
      { security_code: "600339", name: "", type_key: "type1", claim_index: 0, search_finding_id: "search-business", url: "https://example.test/financial-api", kind: "claim" },
      { security_code: "600339", name: "", type_key: "type1", claim_index: 1, search_finding_id: "", url: "https://example.test/financial-api#cashflow", kind: "claim" },
      { security_code: "600339", name: "", type_key: "type1", claim_index: 2, search_finding_id: "", url: "https://example.test/financial-api#valuation", kind: "claim" },
      { security_code: "600339", name: "", type_key: "type1", claim_index: null, finding_index: 0, search_finding_id: "search-business", url: "https://example.test/financial-api", kind: "search_finding" },
    ], company_coverage: [{ security_code: "600339", name: "测试公司", referenced_finding_ids: ["search-business"], searched_no_source_finding_ids: [], referenced_no_source_finding_ids: [], canonical_url_count: 3, semantic_claim_count: 3, semantic_passed_count: 3, semantic_failed_count: 0, semantic_unverified_count: 0, all_referenced_findings_semantic_pass: true, status: "pass" }] };
    Object.assign(companyResearch.packets[0].ai_review, {
      model: "opencode-go/muse-spark-1.2-contributor",
      effort: "xhigh",
      buy_attractiveness_score: 50,
      economic_category: "quality_equity",
      score_components: { risk_adjusted_expected_return: 56, evidence_confidence: 80 },
      calibration_adjustments: { raw_score: 56, source_penalty: 0, freshness_penalty: 8, pre_band_score: 48, action_band_min: 50, action_band_max: 69, final_score: 50, source_quality: "verified_https", freshness_status: "historical", band_clamped: true, verdict: "caution" },
      claims: [{ statement: "2026年半年度报告披露企业金融与零售金融业务。", source_ref: "https://example.test/financial-api", source_kind: "company_ir", search_finding_id: "search-business" }],
      web_search_claim_urls_verified: false,
      claims: [
        { statement: "2026�����ȱ�����¶��ҵ���������۽���ҵ��", source_ref: "https://example.test/financial-api", source_kind: "company_ir", search_finding_id: "search-business" },
        { statement: "2025�꾭Ӫ�ֽ��� 18 ��Ԫ", source_ref: "https://example.test/financial-api#cashflow", source_kind: "company_ir", fact_id: "cashflow-fact" },
        { statement: "2025��������¶��ǰ��ֵ��ɼۡ�", source_ref: "https://example.test/financial-api#valuation", source_kind: "company_ir", fact_id: "valuation-fact" },
      ],
      search_findings: [{ id: "search-business", query: "600339 ������� ���¾�Ӫ���", title: "��˾����ȱ���", url: "https://example.test/financial-api", published_at: "2026-08-15", report_period: "2026H1", finding: "��Ӫҵ��;�Ӫ�ֽ�������������顣", stance: "neutral", source_kind: "company_ir", source_quality: "primary" }],
      evidence_bindings: {
        summary: { fact_ids: ["cashflow-fact", "valuation-fact"], search_finding_ids: [] },
        strengths: [{ fact_ids: ["cashflow-fact"], search_finding_ids: [] }],
        risks: [],
        economic_profile: {
          business_model: { fact_ids: [], search_finding_ids: ["search-business"] },
          moat: { fact_ids: ["valuation-fact"], search_finding_ids: [] },
          cycle: { fact_ids: ["cashflow-fact"], search_finding_ids: [] },
          fcf_outlook: { fact_ids: ["cashflow-fact"], search_finding_ids: [] },
          governance: { fact_ids: ["valuation-fact"], search_finding_ids: [] },
        },
        valuation: { fact_ids: ["valuation-fact"], search_finding_ids: [] },
      },
      web_search_verified: false,
      web_search_verified_claim_url_count: 0,
      research_source_urls_verified: true,
      retrieval_backend: "reasonix-native-server-web-search",
      retrieval_model: "opencode-go-muse/muse-spark-1.2-contributor",
      retrieval_effort: "xhigh",
      native_search_completed: true,
      official_fetch_completed: false,
      research_as_of: "2026-08-15",
          economic_profile: {
            business_model: "通过企业金融与零售金融获取利差及手续费收入。",
            business_model_source_ids: ["search-business"],
            business_model_sources: [{ id: "search-business", statement: "2026年半年度报告披露企业金融与零售金融业务。", source_ref: "https://example.test/financial-api", source_kind: "company_ir" }],
            business_model_source_quality: "current_primary",
            business_model_source_status: "source_found",
            business_model_uncertainty: "已由2026年半年度报告的一手业务分部口径核验。",
            moat: "客户基础仍有价值，但净息差下行构成反证。",
        cycle: "信用成本与净息差处于需要继续观察的阶段。",
        fcf_outlook: "结合资本充足率和分红能力判断股东现金回报。",
        governance: "资本补充需求与分红安排需要同时核验。",
      },
      valuation: {
        method: "book_value_multiple",
        as_of: "2026-08-13",
        current_price: 12.34,
        pe: 6.2,
        pb: 0.55,
        market_cap: 3621,
        scenarios: {
          bear: { value_per_share: 11.2, upside_pct: -9.2382495948, book_value_per_share: 20, target_pb: 0.56 },
          base: { value_per_share: 16, upside_pct: 29.6596434360, book_value_per_share: 20, target_pb: 0.8 },
          bull: { value_per_share: 20.8, upside_pct: 68.5575364668, book_value_per_share: 20, target_pb: 1.04 },
        },
        margin_of_safety: -10.1785714286,
        safety_margin_band: "negative",
        evidence_ids: ["valuation-fact"],
        normalization_anchor: { metric: "book_value_per_share", years: [], total: null, share_count: null, per_share: 20, source_ref: "https://example.test/financial-api#valuation" },
        multiple_basis: { metric: "pb", value: 0.8, source_ref: "https://example.test/financial-api#valuation", search_finding_id: null },
        basis: "结合市净率、资产质量与悲观信用成本情景判断安全边际。",
      },
    });
    companyResearch.packets[0].ai_review.valuation_snapshot = valuationSnapshot("600339", companyResearch.packets[0].ai_review.valuation);
    Object.assign(companyResearch.source_audit, await sourceSemanticProjectionDigest(companyResearch));
    assert.equal(await statusForArtifact(companyResearch), 200);
    const staleClaimProjection = structuredClone(companyResearch);
    staleClaimProjection.packets[0].ai_review.claims[0].statement = "同一网址下被篡改的声明";
    assert.equal(await statusForArtifact(staleClaimProjection), 404);
    const staleFindingProjection = structuredClone(companyResearch);
    staleFindingProjection.packets[0].ai_review.search_findings[0].finding = "同一网址下被篡改的搜索结论";
    assert.equal(await statusForArtifact(staleFindingProjection), 404);
    const staleProjectionCount = structuredClone(companyResearch);
    staleProjectionCount.source_audit.projection_claim_count += 1;
    assert.equal(await statusForArtifact(staleProjectionCount), 404);
    const missingValuationSnapshot = structuredClone(companyResearch);
    delete missingValuationSnapshot.packets[0].ai_review.valuation_snapshot;
    assert.equal(await statusForArtifact(missingValuationSnapshot), 404);
    const synchronizedValuationTamper = structuredClone(companyResearch);
    const tamperedValuation = synchronizedValuationTamper.packets[0].ai_review.valuation;
    tamperedValuation.current_price = 13;
    synchronizedValuationTamper.packets[0].ai_review.valuation_snapshot.current_price = 13;
    for (const scenario of Object.values(tamperedValuation.scenarios)) scenario.upside_pct = (scenario.value_per_share / 13 - 1) * 100;
    tamperedValuation.margin_of_safety = (tamperedValuation.scenarios.bear.value_per_share - 13) / tamperedValuation.scenarios.bear.value_per_share * 100;
    assert.equal(await statusForArtifact(synchronizedValuationTamper), 404);
    const legacyValuationMethod = structuredClone(companyResearch);
    legacyValuationMethod.packets[0].ai_review.valuation.method = "scenario_multiple";
    assert.equal(await statusForArtifact(legacyValuationMethod), 404);
    const insecureCompanyClaim = structuredClone(companyResearch);
    insecureCompanyClaim.packets[0].ai_review.claims[0].source_ref = "http://example.test/financial-api";
    assert.equal(await statusForArtifact(insecureCompanyClaim), 404);
    const legacyCompanyResearchAudit = structuredClone(companyResearch);
    delete legacyCompanyResearchAudit.source_audit.audit_contract_version;
    assert.equal(await statusForArtifact(legacyCompanyResearchAudit), 404);
    const missingCompanyResearchSourceStatus = structuredClone(companyResearch);
    delete missingCompanyResearchSourceStatus.packets[0].ai_review.economic_profile.business_model_source_status;
    assert.equal(await statusForArtifact(missingCompanyResearchSourceStatus), 404);
    const mixedCompanyResearch = structuredClone(companyResearch);
    const deepseekPacket = structuredClone(mixedCompanyResearch.packets[0]);
    deepseekPacket.security_code = "000001";
    deepseekPacket.type_key = "type7";
    deepseekPacket.type_keys = ["type7"];
    deepseekPacket.ai_rank = 2;
    Object.assign(deepseekPacket.ai_review, {
      security_code: "000001",
      type_key: "type7",
      model: "opencode-go/deepseek-v4-flash",
      effort: "max",
      retrieval_model: "opencode-go-deepseek-responses/deepseek-v4-flash",
      retrieval_effort: "max",
    });
    deepseekPacket.ai_review.valuation_snapshot = valuationSnapshot("000001", deepseekPacket.ai_review.valuation);
    mixedCompanyResearch.packets.push(deepseekPacket);
    mixedCompanyResearch.review_models = ["opencode-go/deepseek-v4-flash", "opencode-go/muse-spark-1.2-contributor"];
    mixedCompanyResearch.review_efforts = ["max", "xhigh"];
    mixedCompanyResearch.candidate_total = 2;
    mixedCompanyResearch.type_pair_unique_company_count = 2;
    mixedCompanyResearch.type_pair_candidate_total = 2;
    mixedCompanyResearch.type_pair_expected_total = 2;
    mixedCompanyResearch.type_pair_reviewed_count = 2;
    mixedCompanyResearch.type_pair_web_search_attempted_count = 2;
    mixedCompanyResearch.type_pair_web_search_event_verified_count = 2;
    mixedCompanyResearch.type_pair_research_source_urls_verified_count = 2;
    mixedCompanyResearch.reviewed_count = 2;
    mixedCompanyResearch.attempted_review_count = 2;
    mixedCompanyResearch.completed_review_count = 2;
    mixedCompanyResearch.type_pair_verdict_counts.caution = 2;
    mixedCompanyResearch.verdict_counts.caution = 2;
    mixedCompanyResearch.web_search_attempted_count = 2;
    mixedCompanyResearch.web_search_event_verified_count = 2;
    mixedCompanyResearch.research_source_urls_verified_count = 2;
    mixedCompanyResearch.ai_action_counts.watchlist = 2;
    mixedCompanyResearch.final_category_counts.observe = 2;
    mixedCompanyResearch.do_not_recommend_buy_count = 2;
    mixedCompanyResearch.watchlist_count = 2;
    mixedCompanyResearch.source_audit.claim_count = 6;
    mixedCompanyResearch.source_audit.semantic_claim_count = 6;
    mixedCompanyResearch.source_audit.semantic_passed_count = 6;
    mixedCompanyResearch.source_audit.source_bindings = mixedCompanyResearch.source_audit.source_bindings.flatMap((item) => [
      item,
      { ...structuredClone(item), security_code: "000001", type_key: "type7" },
    ]);
    mixedCompanyResearch.source_audit.company_coverage = mixedCompanyResearch.source_audit.company_coverage.flatMap((item) => [
      item,
      { ...structuredClone(item), security_code: "000001", name: "平安银行" },
    ]);
    mixedCompanyResearch.packets.sort((left, right) => left.security_code < right.security_code ? -1 : 1);
    mixedCompanyResearch.packets.forEach((packet, index) => { packet.ai_rank = index + 1; });
    bindCandidateIdentities(mixedCompanyResearch);
    Object.assign(mixedCompanyResearch.source_audit, await sourceSemanticProjectionDigest(mixedCompanyResearch));
    assert.equal(await statusForArtifact(mixedCompanyResearch), 200);
    const crossedDeepseekProfile = structuredClone(mixedCompanyResearch);
    crossedDeepseekProfile.packets.find(packet => packet.security_code === "000001").ai_review.retrieval_effort = "xhigh";
    assert.equal(await statusForArtifact(crossedDeepseekProfile), 404);
    const researchBeforeMarket = structuredClone(companyResearch);
    researchBeforeMarket.research_as_of = "2026-08-12";
    researchBeforeMarket.packets[0].ai_review.research_as_of = "2026-08-12";
    assert.equal(await statusForArtifact(researchBeforeMarket), 404);
    const mismatchedResearchDate = structuredClone(companyResearch);
    mismatchedResearchDate.packets[0].ai_review.research_as_of = "2026-08-14";
    assert.equal(await statusForArtifact(mismatchedResearchDate), 404);
    const missingEconomicProfile = structuredClone(companyResearch);
    delete missingEconomicProfile.packets[0].ai_review.economic_profile;
    assert.equal(await statusForArtifact(missingEconomicProfile), 404);
    const missingBusinessSourceQuality = structuredClone(companyResearch);
    delete missingBusinessSourceQuality.packets[0].ai_review.economic_profile.business_model_source_quality;
    assert.equal(await statusForArtifact(missingBusinessSourceQuality), 404);
    const wrongScenarioUpside = structuredClone(companyResearch);
    wrongScenarioUpside.packets[0].ai_review.valuation.scenarios.base.upside_pct = 99;
    assert.equal(await statusForArtifact(wrongScenarioUpside), 404);
    const missingValuationMethod = structuredClone(companyResearch);
    delete missingValuationMethod.packets[0].ai_review.valuation.method;
    assert.equal(await statusForArtifact(missingValuationMethod), 404);
    const missingResearchSourceProof = structuredClone(companyResearch);
    missingResearchSourceProof.packets[0].ai_review.research_source_urls_verified = false;
    missingResearchSourceProof.research_source_urls_verified_count = 0;
    missingResearchSourceProof.type_pair_research_source_urls_verified_count = 0;
    assert.equal(await statusForArtifact(missingResearchSourceProof), 404);
    const invalidResearchSourceAudit = structuredClone(companyResearch);
    invalidResearchSourceAudit.source_audit.invalid_claim_url_count = 1;
    assert.equal(await statusForArtifact(invalidResearchSourceAudit), 404);
    const failedResearchSourceAudit = structuredClone(companyResearch);
    failedResearchSourceAudit.source_audit.audit_passed = false;
    assert.equal(await statusForArtifact(failedResearchSourceAudit), 404);
    const forgedBusinessSource = structuredClone(companyResearch);
    forgedBusinessSource.packets[0].ai_review.economic_profile.business_model_sources[0].source_ref = "https://example.test/forged";
    assert.equal(await statusForArtifact(forgedBusinessSource), 404);
    for (const [field, leakedReason] of [
      ["summary", "2026年营收100亿元，但模型已达标。"],
      ["key_strengths", ["规则评分88分"]],
      ["risk_flags", ["type1 已触发"]],
      ["quantitative_facts", ["2026年营收100亿元", "入池原因88分"]],
    ]) {
      const ruleLeakingCompanyResearch = structuredClone(companyResearch);
      ruleLeakingCompanyResearch.packets[0].ai_review[field] = leakedReason;
      assert.equal(await statusForArtifact(ruleLeakingCompanyResearch), 404);
    }
    const qualifiedWatch = structuredClone(artifact);
    qualifiedWatch.packets[0].ai_review.summary = "若估值进一步回落并完成复核，再考虑建议买入。";
    assert.equal(await statusForArtifact(qualifiedWatch), 200);
    const negatedBuyPhraseWatch = structuredClone(artifact);
    negatedBuyPhraseWatch.packets[0].ai_review.summary = "当前暂不具备AI独立建议买入条件，继续观察。";
    assert.equal(await statusForArtifact(negatedBuyPhraseWatch), 200);
    const negatedSuffixWatch = structuredClone(artifact);
    negatedSuffixWatch.packets[0].ai_review.summary = "独立建议买入条件完全不具备，继续观察。";
    assert.equal(await statusForArtifact(negatedSuffixWatch), 200);
    const contradictoryWatch = structuredClone(artifact);
    contradictoryWatch.packets[0].ai_review.summary = "当前建议买入并立即建仓。";
    assert.equal(await statusForArtifact(contradictoryWatch), 404);
    for (const field of ["recommendation_label", "key_strengths", "risk_flags"]) {
      const contradictoryField = structuredClone(artifact);
      contradictoryField.packets[0].ai_review[field] = field === "recommendation_label" ? "观察·当前建议买入" : ["当前建议买入并立即建仓"];
      assert.equal(await statusForArtifact(contradictoryField), 404);
    }
    const mismatchedFinalCategory = structuredClone(artifact);
    mismatchedFinalCategory.packets[0].ai_review.final_category = "recommend_buy";
    assert.equal(await statusForArtifact(mismatchedFinalCategory), 404);
    const priority = structuredClone(artifact);
    Object.assign(priority.packets[0].ai_review, { verdict: "confirmed", recommended_action: "keep", buy_attractiveness_score: 70, ai_action: "priority_buy", final_category: "recommend_buy", final_recommendation: "recommend_buy", recommendation_label: "建议买·当前复核通过", summary: "当前建议买入，但仍需控制仓位。", freshness_status: "current_or_recent", freshness_years: [2026], freshness_penalty: 0 });
    priority.ai_action_counts = { priority_buy: 1, watchlist: 0, avoid: 0, insufficient_evidence: 0 };
    priority.final_category_counts = { recommend_buy: 1, observe: 0, do_not_recommend: 0 };
    priority.priority_buy_count = 1;
    priority.recommend_buy_count = 1;
    priority.watchlist_count = 0;
    priority.do_not_recommend_buy_count = 0;
    priority.verdict_counts = { confirmed: 1, caution: 0, misclassified: 0, missed_candidate: 0, needs_review: 0 };
    priority.type_pair_verdict_counts = { confirmed: 1, caution: 0, misclassified: 0, missed_candidate: 0, needs_review: 0 };
    assert.equal(await statusForArtifact(priority), 200);
    const priorityWithoutKeep = structuredClone(priority);
    priorityWithoutKeep.packets[0].ai_review.recommended_action = "manual_review";
    assert.equal(await statusForArtifact(priorityWithoutKeep), 404);
    const contradictoryPriority = structuredClone(priority);
    contradictoryPriority.packets[0].ai_review.summary = "建议继续观望，当前不构成买点。";
    assert.equal(await statusForArtifact(contradictoryPriority), 404);
    for (const field of ["recommendation_label", "key_strengths", "risk_flags"]) {
      const contradictoryPriorityField = structuredClone(priority);
      contradictoryPriorityField.packets[0].ai_review[field] = field === "recommendation_label" ? "建议买·当前应当观望" : ["当前不建议买入，应当继续观望"];
      assert.equal(await statusForArtifact(contradictoryPriorityField), 404);
    }
    const misclassified = structuredClone(artifact);
    Object.assign(misclassified.packets[0].ai_review, { verdict: "misclassified", recommended_action: "demote", buy_attractiveness_score: 45, ai_action: "avoid", final_category: "do_not_recommend", final_recommendation: "do_not_recommend_buy", recommendation_label: "不建议·规则可能误判", summary: "当前不建议买入。" });
    misclassified.ai_action_counts = { priority_buy: 0, watchlist: 0, avoid: 1, insufficient_evidence: 0 };
    misclassified.final_category_counts = { recommend_buy: 0, observe: 0, do_not_recommend: 1 };
    misclassified.watchlist_count = 0;
    misclassified.avoid_count = 1;
    misclassified.verdict_counts = { confirmed: 0, caution: 0, misclassified: 1, missed_candidate: 0, needs_review: 0 };
    misclassified.type_pair_verdict_counts = { confirmed: 0, caution: 0, misclassified: 1, missed_candidate: 0, needs_review: 0 };
    assert.equal(await statusForArtifact(misclassified), 200);
    const invalidMisclassified = structuredClone(misclassified);
    invalidMisclassified.packets[0].ai_review.recommended_action = "manual_review";
    assert.equal(await statusForArtifact(invalidMisclassified), 404);
    const needsReview = structuredClone(artifact);
    needsReview.packets[0].ai_review.verdict = "needs_review";
    needsReview.attempted_needs_review_count = 1;
    needsReview.type_pair_needs_review_count = 1;
    needsReview.verdict_counts.caution = 0;
    needsReview.verdict_counts.needs_review = 1;
    needsReview.type_pair_verdict_counts.caution = 0;
    needsReview.type_pair_verdict_counts.needs_review = 1;
    assert.equal(await statusForArtifact(needsReview), 200);
    const needsReviewWithoutManual = structuredClone(needsReview);
    needsReviewWithoutManual.packets[0].ai_review.recommended_action = "keep";
    assert.equal(await statusForArtifact(needsReviewWithoutManual), 404);
    const malformedTypes = structuredClone(artifact);
    malformedTypes.packets[0].type_keys = ["type2"];
    assert.equal(await statusForArtifact(malformedTypes), 404);
    const mismatchedCandidateTypes = structuredClone(artifact);
    mismatchedCandidateTypes.packets[0].candidate_types = [{ type_key: "type2" }];
    assert.equal(await statusForArtifact(mismatchedCandidateTypes), 404);
    const mismatchedPairCount = structuredClone(artifact);
    mismatchedPairCount.packets[0].type_pair_count = 2;
    assert.equal(await statusForArtifact(mismatchedPairCount), 404);
    const mismatchedCompletedCount = structuredClone(artifact);
    mismatchedCompletedCount.completed_review_count = 0;
    assert.equal(await statusForArtifact(mismatchedCompletedCount), 404);
    const forgedPairTotal = structuredClone(artifact);
    forgedPairTotal.type_pair_candidate_total = 2;
    forgedPairTotal.type_pair_expected_total = 2;
    forgedPairTotal.type_pair_reviewed_count = 2;
    assert.equal(await statusForArtifact(forgedPairTotal), 404);
    const twoCardArtifact = (secondCode, secondRank) => {
      const value = structuredClone(artifact);
      const second = structuredClone(value.packets[0]);
      second.security_code = secondCode;
      second.type_key = "type2";
      second.type_keys = ["type2"];
      second.ai_rank = secondRank;
      value.packets.push(second);
      value.candidate_total = 2;
      value.type_pair_unique_company_count = 2;
      value.reviewed_count = 2;
      value.attempted_review_count = 2;
      value.completed_review_count = 2;
      value.reviewed_without_web_search = 2;
      value.type_pair_candidate_total = 2;
      value.type_pair_expected_total = 2;
      value.type_pair_reviewed_count = 2;
      value.verdict_counts.caution = 2;
      value.type_pair_verdict_counts.caution = 2;
      value.ai_action_counts.watchlist = 2;
      value.final_category_counts.observe = 2;
      value.do_not_recommend_buy_count = 2;
      value.watchlist_count = 2;
      return value;
    };
    assert.equal(await statusForArtifact(twoCardArtifact("600339", 2)), 404);
    assert.equal(await statusForArtifact(twoCardArtifact("600340", 1)), 404);
    const orderedTwoCards = twoCardArtifact("600340", 2);
    bindCandidateIdentities(orderedTwoCards);
    assert.equal(await statusForArtifact(orderedTwoCards), 200);
    const wrongScoreOrder = structuredClone(orderedTwoCards);
    wrongScoreOrder.packets[0].ai_review.buy_attractiveness_score = 63;
    assert.equal(await statusForArtifact(wrongScoreOrder), 404);
    const wrongStableOrder = structuredClone(orderedTwoCards);
    wrongStableOrder.packets.reverse();
    wrongStableOrder.packets[0].ai_rank = 1;
    wrongStableOrder.packets[1].ai_rank = 2;
    assert.equal(await statusForArtifact(wrongStableOrder), 404);
    const missingIdentity = structuredClone(artifact);
    delete missingIdentity.candidate_identity_sha256;
    assert.equal(await statusForArtifact(missingIdentity), 404);
    for (const field of ["candidate_identity_sha256", "candidate_universe_identity_sha256", "type_pair_candidate_identity_sha256", "type_pair_universe_identity_sha256"]) {
      const mismatchedIdentity = structuredClone(artifact);
      mismatchedIdentity[field] = "b".repeat(64);
      assert.equal(await statusForArtifact(mismatchedIdentity), 404);
    }
    delete artifact.review_models;
    const missingModelBytes = new TextEncoder().encode(JSON.stringify(artifact));
    objects.set("ai-screening/0123456789abcdef.json", {
      size: missingModelBytes.byteLength,
      arrayBuffer: async () => missingModelBytes.buffer.slice(missingModelBytes.byteOffset, missingModelBytes.byteOffset + missingModelBytes.byteLength),
    });
    response = await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env);
    assert.equal(response.status, 404);
    artifact.review_models = ["opencode-go/ox-alpha-free"];
    artifact.packets[0].ai_review.buy_attractiveness_score = 100;
    const invalidBytes = new TextEncoder().encode(JSON.stringify(artifact));
    objects.set("ai-screening/0123456789abcdef.json", {
      size: invalidBytes.byteLength,
      arrayBuffer: async () => invalidBytes.buffer.slice(invalidBytes.byteOffset, invalidBytes.byteOffset + invalidBytes.byteLength),
    });
    response = await worker.fetch(new Request("https://dashboard.test/api/ai-screening"), env);
    assert.equal(response.status, 404);
    response = await worker.fetch(new Request("https://dashboard.test/api/ai-screening?bad=1"), env);
    assert.equal(response.status, 400);
    response = await worker.fetch(new Request("https://dashboard.test/ai-screening"), env);
    assert.equal(response.status, 200);
    const html = await response.text();
    const nonce = html.match(/<script nonce="([^"]+)">/)?.[1];
    assert.ok(nonce);
    const aiCsp = response.headers.get("content-security-policy") || "";
    assert.ok(html.includes("AI筛查"));
    response = await worker.fetch(new Request("https://dashboard.test/ai-screening-changes"), env);
    assert.equal(response.status, 200);
    const changesHtml = await response.text();
    assert.ok(changesHtml.includes("与昨日比较"));
    assert.ok(changesHtml.includes("变化类型"));
    const changesInlineScript = changesHtml.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)?.[1];
    assert.ok(changesInlineScript);
    assert.doesNotThrow(() => new Function(changesInlineScript));
    assert.ok(html.includes("建议买"));
    assert.ok(html.includes("观察"));
    assert.ok(html.includes("不建议"));
    assert.ok(html.includes("原生搜索事件"));
    assert.ok(!html.includes("财报来源链接"));
    assert.ok(html.includes("完成独立复核"));
    assert.ok(!html.includes("保留引用已绑定搜索结果"));
    assert.ok(html.includes("已移除无效来源"));
    assert.ok(!html.includes("资料时效：非当前/未标注"));
    assert.ok(!html.includes("待核验（未形成买入结论）"));
    assert.ok(aiCsp.includes("script-src 'nonce-" + nonce + "'"));
    const inlineScript = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)?.[1];
    assert.ok(inlineScript);
    assert.ok(inlineScript.includes("research_as_of"));
    assert.ok(inlineScript.includes("function isRuleText"));
    assert.ok(inlineScript.includes("function aiSummary"));
    assert.ok(inlineScript.includes("candidate-rules"));
    assert.ok(html.includes("AI为什么这样判断"));
    assert.ok(html.includes("公司量化事实"));
    assert.ok(html.includes("公司商业画像"));
    assert.ok(html.includes("估值与安全边际"));
    assert.ok(html.includes("主要反证与风险"));
    assert.ok(html.includes("公司资料与来源"));
assert.ok(html.includes("为何进入研究池（仅说明候选范围）"));
assert.ok(!inlineScript.includes("reasonValues=group==='recommend_buy'"));
assert.ok(!inlineScript.includes("确定性规则：'+esc(det.status"));
assert.doesNotThrow(() => new Function(inlineScript));
const elements = new Map();
const fakeDocument = {
  querySelector(selector) {
    if (!elements.has(selector)) {
      elements.set(selector, {
        value: "all",
        innerHTML: "",
        textContent: "",
            hidden: false,
            disabled: false,
            addEventListener() {},
            setAttribute() {},
            removeAttribute() {},
          });
    }
    return elements.get(selector);
  },
};
const pageApi = new Function(
  "document",
  "fetch",
  inlineScript + "; return {card,isRuleText,searchLabel};",
)(
  fakeDocument,
  async () => ({ ok: false, json: async () => ({ error: "audit" }) }),
);
assert.equal(pageApi.isRuleText("规则评分88分"), true);
assert.equal(pageApi.isRuleText("模型已达标"), true);
const renderedCard = pageApi.card({
  ai_rank: 1,
  security_code: "600000",
  name: "审计样本",
  type_key: "type1",
  type_keys: ["type1", "type2"],
  deterministic: { status: "conditional", score: 88 },
  ai_review: {
    final_category: "recommend_buy",
    ai_action: "priority_buy",
    buy_attractiveness_score: 87,
    confidence: "high",
    ai_independent: true,
    freshness_status: "current_or_recent",
    freshness_years: [2026],
    model: "opencode-go/muse-spark-1.2-contributor",
    effort: "xhigh",
    web_search_performed: true,
    web_search_event_verified: true,
    research_source_urls_verified: true,
    research_as_of: "2026-08-15",
        economic_profile: {
              business_model: "家电制造与海外渠道形成主要收入。",
              business_model_source_ids: ["search-business"],
              business_model_sources: [{ id: "search-business", statement: "公司官网披露家电制造与海外渠道业务。", source_ref: "https://example.test/business", source_kind: "company_ir" }],
              business_model_source_quality: "secondary_only",
          business_model_uncertainty: "目前仅有二手来源，业务口径尚待一手资料核验。",
          moat: "规模采购与渠道效率具备优势，但品牌议价力仍需核验。",
      cycle: "地产后周期需求恢复仍有不确定性。",
      fcf_outlook: "经营现金流改善，但资本开支与营运资金仍需跟踪。",
      governance: "分红稳定性与关联交易是资本配置核验重点。",
    },
    valuation: {
      method: "scenario_multiple",
      as_of: "2026-08-13",
      current_price: 9.25,
      pe: 8.89,
      pb: 1.42,
          market_cap: 10050000000,
      scenarios: {
        bear: { value_per_share: 10, upside_pct: 8.1081081081 },
        base: { value_per_share: 12, upside_pct: 29.7297297297 },
        bull: { value_per_share: 15, upside_pct: 62.1621621622 },
      },
      margin_of_safety: 7.5,
      basis: "当前估值低于行业中位数，但悲观需求情景仍需保留折价。",
    },
    web_search_query_count: 3,
    web_search_verified_claim_url_count: 0,
    web_search_dropped_claim_url_count: 0,
    summary: "2026年上半年营收100亿元，同比增长15%。模型已达标。",
    quantitative_facts: ["2026年上半年营收100亿元，同比增长15%。", "type1 已触发"],
    key_strengths: ["2026年上半年经营现金流12亿元。", "规则评分88分"],
    risk_flags: ["2026年上半年应收账款增长35%。"],
    claims: [
      { statement: "2026年上半年营收100亿元", source_ref: "https://example.test/report" },
      { statement: "2026年上半年经营现金流12亿元", source_ref: "https://example.test/cashflow" },
    ],
  },
});
const candidateDetails = renderedCard.match(/<details class="candidate-rules">[\s\S]*?<\/details>/)?.[0] || "";
const defaultCard = renderedCard.replace(candidateDetails, "");
assert.ok(renderedCard.includes('<article class="card ai-card">'));
assert.ok(renderedCard.includes("<strong>审计样本</strong>"));
assert.ok(renderedCard.includes("代码：600000"));
assert.ok(renderedCard.includes('<details class="ai-review-details">'));
assert.ok(renderedCard.includes("查看完整 AI 解释"));
assert.ok(defaultCard.includes("AI独立判断"));
assert.ok(!defaultCard.includes("接近达标"));
assert.ok(defaultCard.includes("原生搜索事件已核验 · 财报来源已核验"));
assert.ok(defaultCard.includes("可点击来源：2"));
assert.ok(defaultCard.includes("公司研究截至：2026-08-15"));
    assert.ok(defaultCard.includes("家电制造与海外渠道形成主要收入"));
        assert.ok(defaultCard.includes("业务口径来源质量"));
        assert.ok(defaultCard.includes("仅二手资料"));
        assert.ok(defaultCard.includes("主营业务资料"));
        assert.ok(defaultCard.includes("查看原始资料"));
    assert.ok(defaultCard.includes("业务口径不确定性"));
assert.ok(defaultCard.includes("估值与安全边际（收盘日 2026-08-13）"));
assert.ok(defaultCard.includes("9.25元"));
assert.ok(defaultCard.includes("情景倍数法"));
assert.ok(defaultCard.includes("悲观情景"));
assert.ok(defaultCard.includes("中性情景"));
assert.ok(defaultCard.includes("乐观情景"));
assert.ok(defaultCard.includes("每股价值 10 元"));
assert.ok(defaultCard.includes("相对现价 +8.108%"));
assert.ok(defaultCard.includes("悲观安全边际"));
    assert.ok(defaultCard.includes("7.5%"));
    assert.ok(defaultCard.includes("100.5亿元"));
    assert.equal((defaultCard.match(/<a rel=/g) || []).length, 3);
assert.ok(!defaultCard.includes("规则评分88分"));
assert.ok(!defaultCard.includes("模型已达标"));
assert.ok(!defaultCard.includes("type1"));
assert.ok(!defaultCard.includes("conditional"));
assert.ok(candidateDetails.includes("规则候选类型：type1 / type2"));
assert.ok(candidateDetails.includes("确定性前筛状态：待确认"));
assert.ok(candidateDetails.includes("这里仅说明公司为何进入 AI 研究范围，不是 AI 的最终结论"));
assert.ok(!candidateDetails.includes("确定性状态：conditional"));
assert.ok(candidateDetails.includes("规则评分88分"));
assert.ok(!candidateDetails.includes("原生搜索事件已核验"));
"""
    validator_path = tmp_path / "ai-screening-worker-validator.mjs"
    validator_path.write_text(validator, encoding="utf-8")
    result = subprocess.run(
        [node, str(validator_path)],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
