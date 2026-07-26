/*
 * Scheduled, read-only data mirror for quant.custard.top.
 *
 * The source manifest and immutable assets are produced and signed by the
 * existing GitHub Actions pipeline.  This worker never calculates scores and
 * never accepts arbitrary upload paths: it validates the signed manifest's
 * shape, every declared SHA-256/size, and then atomically advances the D1
 * pointer after all R2 objects are written.
 */

const SOURCE_MANIFEST = "https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json";
const SOURCE_ASSET_BASE = "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data/";
const HEX64 = /^[0-9a-f]{64}$/;
const ASSET_NAMES = {
  catalogue: /^catalog-[0-9a-f]{16}\.json\.gz$/,
  signals: /^signals-[0-9a-f]{16}\.json\.gz$/,
  signature: /^manifest-[0-9a-f]{16}\.sig$/,
};

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function sha256(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
}

async function downloadAsset(name, kind, metadata = {}) {
  if (!ASSET_NAMES[kind]?.test(name)) throw new Error(`invalid ${kind} filename`);
  if (kind !== "signature" && (!Number.isSafeInteger(metadata.size) || metadata.size <= 0 || metadata.size > 16_000_000)) {
    throw new Error(`invalid ${kind} size`);
  }
  if (kind !== "signature" && !HEX64.test(String(metadata.sha256 || ""))) throw new Error(`invalid ${kind} checksum`);
  const response = await fetch(`${SOURCE_ASSET_BASE}${encodeURIComponent(name)}?mirror=${Date.now()}`, {
    cf: { cacheTtl: 0, cacheEverything: false },
  });
  if (!response.ok) throw new Error(`${metadata.kind} download HTTP ${response.status}`);
  const bytes = await response.arrayBuffer();
  if (kind !== "signature") {
    if (bytes.byteLength !== metadata.size) throw new Error(`${kind} size mismatch`);
    const digest = await sha256(bytes);
    if (digest !== metadata.sha256.toLowerCase()) throw new Error(`${kind} checksum mismatch`);
  } else if (bytes.byteLength < 64 || bytes.byteLength > 128) {
    throw new Error("invalid manifest signature size");
  }
  return bytes;
}

function base64Bytes(value) {
  const raw = atob(value);
  return Uint8Array.from(raw, (item) => item.charCodeAt(0));
}

function derToP1363(value) {
  const bytes = new Uint8Array(value);
  let offset = 0;
  if (bytes[offset++] !== 0x30) throw new Error("invalid signature sequence");
  const readLength = () => {
    let length = bytes[offset++];
    if (length & 0x80) {
      const count = length & 0x7f;
      if (count < 1 || count > 2) throw new Error("invalid signature length");
      length = 0;
      for (let i = 0; i < count; i += 1) length = length * 256 + bytes[offset++];
    }
    return length;
  };
  const sequenceLength = readLength();
  if (sequenceLength !== bytes.length - offset) throw new Error("signature sequence length mismatch");
  const integer = () => {
    if (bytes[offset++] !== 0x02) throw new Error("invalid signature integer");
    const length = readLength();
    const end = offset + length;
    if (end > bytes.length || length < 1) throw new Error("invalid signature integer length");
    while (offset < end - 1 && bytes[offset] === 0) offset += 1;
    const value = bytes.slice(offset, end);
    offset = end;
    if (value.length > 32) throw new Error("signature integer too large");
    const padded = new Uint8Array(32);
    padded.set(value, 32 - value.length);
    return padded;
  };
  const r = integer(); const s = integer();
  if (offset !== bytes.length) throw new Error("signature trailing data");
  const output = new Uint8Array(64); output.set(r); output.set(s, 32); return output;
}

async function verifyManifestSignature(manifestText, signatureBytes) {
  const keyBytes = base64Bytes("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAExQ3XrBYfIsZilmdQvnTqIcqo7mCPhRTOnntpt/hqA+mCkHaHRGhjEyd3ek5XNRyjhadmMl364s8MBOjAySPENg==");
  const key = await crypto.subtle.importKey("spki", keyBytes, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, derToP1363(signatureBytes), new TextEncoder().encode(manifestText));
  if (!valid) throw new Error("manifest signature verification failed");
}

function generationFromNames(catalogue, signals, signature) {
  const c = catalogue.match(ASSET_NAMES.catalogue);
  const s = signals.match(ASSET_NAMES.signals);
  const g = signature.match(ASSET_NAMES.signature);
  if (!c || !s || !g || c[0].slice(8, 24) !== s[0].slice(8, 24) || c[0].slice(8, 24) !== g[0].slice(8, 24)) {
    throw new Error("manifest assets are not one generation");
  }
  return c[0].slice(8, 24);
}

async function refresh(env) {
  const response = await fetch(`${SOURCE_MANIFEST}?mirror=${Date.now()}`, { cf: { cacheTtl: 0, cacheEverything: false } });
  if (!response.ok) throw new Error(`manifest download HTTP ${response.status}`);
  const manifestText = await response.text();
  const manifest = JSON.parse(manifestText);
  if (manifest.product !== "DS_DCF" || manifest.schema_version !== 1 || manifest.analysis_quality?.ok !== true) {
    throw new Error("manifest quality contract failed");
  }
  const catalogueName = String(manifest.catalogue?.filename || "");
  const signalsName = String(manifest.signals?.filename || "");
  const signatureName = String(manifest.signature?.filename || "");
  const generationId = generationFromNames(catalogueName, signalsName, signatureName);
  const [catalogueBytes, signalsBytes, signatureBytes] = await Promise.all([
    downloadAsset(catalogueName, "catalogue", manifest.catalogue),
    downloadAsset(signalsName, "signals", manifest.signals),
    downloadAsset(signatureName, "signature"),
  ]);
  await verifyManifestSignature(manifestText, signatureBytes);
  const now = new Date().toISOString();
  const manifestBytes = new TextEncoder().encode(manifestText);
  const manifestHash = await sha256(manifestBytes);
  const previous = await env.DB.prepare("SELECT generation_id FROM current_generation WHERE singleton = 1").first();
  if (previous?.generation_id === generationId) {
    await env.DB.prepare("UPDATE generations SET last_checked_at = ? WHERE generation_id = ?").bind(now, generationId).run();
    return { status: "unchanged", generation_id: generationId, market_as_of: manifest.market_as_of };
  }

  const prefix = `generations/${generationId}`;
  const objects = [
    ["manifest.json", manifestText, "application/json; charset=utf-8"],
    [catalogueName, catalogueBytes, "application/json", "gzip"],
    [signalsName, signalsBytes, "application/json", "gzip"],
    [signatureName, signatureBytes, "application/octet-stream"],
  ];
  for (const [name, body, contentType, contentEncoding] of objects) {
    await env.DATA_BUCKET.put(`${prefix}/${name}`, body, { httpMetadata: { contentType, ...(contentEncoding ? { contentEncoding } : {}) } });
  }
  const summary = manifest.summary || {};
  const sourceCommit = String(manifest.provenance?.source_commit || "");
  await env.DB.prepare(
    `INSERT INTO generations
      (generation_id, market_as_of, data_timestamp_utc, generated_at_utc, manifest_sha256,
       company_count, triggered_company_count, conditional_company_count, pending_company_count,
       source_commit, created_at, last_checked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    generationId,
    String(manifest.market_as_of || ""),
    String(manifest.data_timestamp_utc || ""),
    String(manifest.generated_at_utc || ""),
    manifestHash,
    Number(summary.company_count || 0),
    Number(summary.triggered_company_count || 0),
    Number(summary.conditional_company_count || 0),
    Number(summary.pending_company_count || 0),
    sourceCommit,
    now,
    now,
  ).run();
  await env.DB.prepare(
    "INSERT INTO current_generation(singleton, generation_id, updated_at) VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET generation_id = excluded.generation_id, updated_at = excluded.updated_at"
  ).bind(generationId, now).run();
  return { status: "updated", generation_id: generationId, market_as_of: manifest.market_as_of, company_count: Number(summary.company_count || 0) };
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/refresh" || request.headers.get("x-refresh-key") !== env.REFRESH_KEY) {
      return json({ error: "not found" }, 404);
    }
    try {
      return json(await refresh(env));
    } catch (error) {
      return json({ status: "error", error: String(error?.message || error) }, 502);
    }
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(refresh(env));
  },
};
