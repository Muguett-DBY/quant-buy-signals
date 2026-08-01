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
const MAX_MANIFEST_BYTES = 1_000_000;
const MAX_COMPRESSED_ASSET_BYTES = 8_000_000;
const MAX_UNCOMPRESSED_ASSET_BYTES = 24_000_000;
const MAX_DETAIL_COMPRESSED_TOTAL = 48_000_000;
const MAX_DETAIL_UNCOMPRESSED_TOTAL = 144_000_000;
const DETAIL_DOWNLOAD_BATCH_SIZE = 4;
const ASSET_NAMES = {
  catalogue: /^catalog-[0-9a-f]{16}\.json\.gz$/,
  signals: /^signals-[0-9a-f]{16}\.json\.gz$/,
  company_detail: /^company-details-[0-9a-f]{16}-[0-9a-f]{2}\.json\.gz$/,
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

async function readBoundedStream(stream, maxBytes, label) {
  if (!stream || !Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new Error(`${label} stream limit is invalid`);
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      total += chunk.byteLength;
      if (total > maxBytes) throw new Error(`${label} exceeds its byte limit`);
      chunks.push(chunk);
    }
  } catch (error) {
    try { await reader.cancel(error); } catch { /* best-effort cancellation */ }
    throw error;
  }
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output.buffer;
}

async function downloadAsset(name, kind, metadata = {}) {
  if (!ASSET_NAMES[kind]?.test(name)) throw new Error(`invalid ${kind} filename`);
  if (kind !== "signature" && (!Number.isSafeInteger(metadata.size) || metadata.size <= 0 || metadata.size > MAX_COMPRESSED_ASSET_BYTES)) {
    throw new Error(`invalid ${kind} size`);
  }
  if (kind !== "signature" && !HEX64.test(String(metadata.sha256 || ""))) throw new Error(`invalid ${kind} checksum`);
  const response = await fetch(`${SOURCE_ASSET_BASE}${encodeURIComponent(name)}?mirror=${Date.now()}`, {
    cf: { cacheTtl: 0, cacheEverything: false },
  });
  if (!response.ok) throw new Error(`${kind} download HTTP ${response.status}`);
  const maxBytes = kind === "signature" ? 128 : metadata.size;
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > maxBytes || (kind !== "signature" && declaredLength > 0 && declaredLength !== metadata.size)) {
    throw new Error(`${kind} response length mismatch`);
  }
  const bytes = await readBoundedStream(response.body, maxBytes, `${kind} response`);
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

async function verifyManifestSignature(manifestBytes, signatureBytes) {
  const keyBytes = base64Bytes("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAExQ3XrBYfIsZilmdQvnTqIcqo7mCPhRTOnntpt/hqA+mCkHaHRGhjEyd3ek5XNRyjhadmMl364s8MBOjAySPENg==");
  const key = await crypto.subtle.importKey("spki", keyBytes, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, derToP1363(signatureBytes), manifestBytes);
  if (!valid) throw new Error("manifest signature verification failed");
}

function companyDetailAssets(manifest, generationId) {
  const details = manifest?.company_details;
  if (!details) return null;
  const partition = details?.partition;
  const shards = details?.shards;
  if (
    details?.schema_version !== 2
    || details?.record_schema !== "company_detail_v2"
    || !Number.isSafeInteger(details?.company_count)
    || details.company_count < 1
    || partition?.algorithm !== "sha256_code_first_nibble"
    || partition?.shard_count !== 16
    || details?.root_algorithm !== "SHA256_CANONICAL_SHARD_INDEX_V1"
    || !HEX64.test(String(details?.root_sha256 || ""))
    || !Array.isArray(shards)
    || shards.length !== 16
  ) {
    throw new Error("invalid company detail contract");
  }
  const expectedIds = Array.from({ length: 16 }, (_, index) => index.toString(16).padStart(2, "0"));
  let companyCount = 0;
  let compressedTotal = 0;
  let uncompressedTotal = 0;
  const assets = shards.map((metadata, index) => {
    const id = String(metadata?.id || "");
    const filename = String(metadata?.filename || "");
    if (
      id !== expectedIds[index]
      || filename !== `company-details-${generationId}-${id}.json.gz`
      || !ASSET_NAMES.company_detail.test(filename)
      || !Number.isSafeInteger(metadata?.company_count)
      || metadata.company_count < 0
      || !HEX64.test(String(metadata?.sha256 || ""))
      || !HEX64.test(String(metadata?.uncompressed_sha256 || ""))
      || !Number.isSafeInteger(metadata?.size)
      || metadata.size < 1
      || metadata.size > MAX_COMPRESSED_ASSET_BYTES
      || !Number.isSafeInteger(metadata?.uncompressed_size)
      || metadata.uncompressed_size < 1
      || metadata.uncompressed_size > MAX_UNCOMPRESSED_ASSET_BYTES
    ) {
      throw new Error(`invalid company detail shard ${id || index}`);
    }
    companyCount += metadata.company_count;
    compressedTotal += metadata.size;
    uncompressedTotal += metadata.uncompressed_size;
    return { ...metadata, id, filename };
  });
  if (
    companyCount !== details.company_count
    || companyCount !== Number(manifest?.summary?.company_count || 0)
    || compressedTotal > MAX_DETAIL_COMPRESSED_TOTAL
    || uncompressedTotal > MAX_DETAIL_UNCOMPRESSED_TOTAL
  ) {
    throw new Error("company detail coverage mismatch");
  }
  return assets;
}

function generationFromNames(catalogue, signals, signature, detailAssets = []) {
  const c = /^catalog-([0-9a-f]{16})\.json\.gz$/.exec(catalogue);
  const s = /^signals-([0-9a-f]{16})\.json\.gz$/.exec(signals);
  const g = /^manifest-([0-9a-f]{16})\.sig$/.exec(signature);
  if (!c || !s || !g || c[1] !== s[1] || c[1] !== g[1]) {
    throw new Error("manifest assets are not one generation");
  }
  for (const asset of detailAssets) {
    const match = /^company-details-([0-9a-f]{16})-[0-9a-f]{2}\.json\.gz$/.exec(asset.filename);
    if (!match || match[1] !== c[1]) throw new Error("company detail assets are not one generation");
  }
  return c[1];
}

async function downloadDetailAssets(assets) {
  const results = [];
  for (let start = 0; start < assets.length; start += DETAIL_DOWNLOAD_BATCH_SIZE) {
    const batch = assets.slice(start, start + DETAIL_DOWNLOAD_BATCH_SIZE);
    results.push(...await Promise.all(
      batch.map((metadata) => downloadAsset(metadata.filename, "company_detail", metadata)),
    ));
  }
  return results;
}

async function gunzip(bytes, maxBytes) {
  const body = new Response(bytes).body;
  if (!body) throw new Error("company detail compressed stream is unavailable");
  return await readBoundedStream(
    body.pipeThrough(new DecompressionStream("gzip")),
    maxBytes,
    "company detail uncompressed response",
  );
}

async function detailShardId(code) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(code));
  return ((new Uint8Array(digest)[0] >> 4).toString(16)).padStart(2, "0");
}

async function verifyCompanyDetailPayloads(manifest, assets, compressedAssets) {
  if (!assets.length) return;
  if (compressedAssets.length !== assets.length) throw new Error("company detail download coverage mismatch");
  const seenCodes = new Set();
  for (let index = 0; index < assets.length; index += 1) {
    const metadata = assets[index];
    const raw = await gunzip(compressedAssets[index], metadata.uncompressed_size);
    if (raw.byteLength !== metadata.uncompressed_size) throw new Error(`company detail shard ${metadata.id} uncompressed size mismatch`);
    if (await sha256(raw) !== metadata.uncompressed_sha256) throw new Error(`company detail shard ${metadata.id} uncompressed checksum mismatch`);
    let payload;
    try {
      payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    } catch {
      throw new Error(`company detail shard ${metadata.id} JSON is invalid`);
    }
    const companies = payload?.companies;
    if (
      payload?.schema_version !== 2
      || payload?.record_schema !== "company_detail_v2"
      || payload?.product !== "DS_DCF"
      || payload?.generated_at_utc !== manifest.generated_at_utc
      || payload?.market_as_of !== manifest.market_as_of
      || payload?.data_timestamp_utc !== manifest.data_timestamp_utc
      || payload?.shard_id !== metadata.id
      || !Array.isArray(companies)
      || payload?.company_count !== companies.length
      || companies.length !== metadata.company_count
    ) {
      throw new Error(`company detail shard ${metadata.id} payload contract mismatch`);
    }
    const assignments = await Promise.all(companies.map(async (company) => {
      const code = String(company?.code || "");
      if (
        company?.schema_version !== 2
        || !/^[036][0-9]{5}$/.test(code)
        || !company?.types
        || typeof company.types !== "object"
        || Object.keys(company.types).sort().join(",") !== "type1,type2,type3,type4,type5,type6,type7"
      ) {
        throw new Error(`company detail shard ${metadata.id} contains an invalid company`);
      }
      return [code, await detailShardId(code)];
    }));
    for (const [code, assignedShard] of assignments) {
      if (assignedShard !== metadata.id) throw new Error(`company ${code} is assigned to the wrong detail shard`);
      if (seenCodes.has(code)) throw new Error(`company detail code ${code} is duplicated`);
      seenCodes.add(code);
    }
  }
  const details = manifest.company_details;
  const rootContract = {
    partition: { algorithm: "sha256_code_first_nibble", shard_count: 16 },
    record_schema: "company_detail_v2",
    schema_version: 2,
    shards: assets.map((metadata) => ({
      company_count: metadata.company_count,
      id: metadata.id,
      uncompressed_sha256: metadata.uncompressed_sha256,
    })),
  };
  const rootHash = await sha256(new TextEncoder().encode(JSON.stringify(rootContract)));
  if (rootHash !== details.root_sha256 || seenCodes.size !== details.company_count) {
    throw new Error("company detail canonical root or unique company coverage mismatch");
  }
}

async function refresh(env) {
  const response = await fetch(`${SOURCE_MANIFEST}?mirror=${Date.now()}`, { cf: { cacheTtl: 0, cacheEverything: false } });
  if (!response.ok) throw new Error(`manifest download HTTP ${response.status}`);
  const declaredManifestLength = Number(response.headers.get("content-length") || 0);
  if (declaredManifestLength > MAX_MANIFEST_BYTES) throw new Error("manifest is too large");
  const sourceManifestBytes = await readBoundedStream(response.body, MAX_MANIFEST_BYTES, "manifest response");
  const manifestText = new TextDecoder("utf-8", { fatal: true }).decode(sourceManifestBytes);
  const manifest = JSON.parse(manifestText);
  if (manifest.product !== "DS_DCF" || manifest.schema_version !== 1 || manifest.analysis_quality?.ok !== true) {
    throw new Error("manifest quality contract failed");
  }
  const catalogueName = String(manifest.catalogue?.filename || "");
  const signalsName = String(manifest.signals?.filename || "");
  const signatureName = String(manifest.signature?.filename || "");
  const preliminaryGeneration = generationFromNames(catalogueName, signalsName, signatureName);
  const signatureBytes = await downloadAsset(signatureName, "signature");
  await verifyManifestSignature(sourceManifestBytes, signatureBytes);
  const detailAssets = companyDetailAssets(manifest, preliminaryGeneration) || [];
  const generationId = generationFromNames(catalogueName, signalsName, signatureName, detailAssets);
  const [catalogueBytes, signalsBytes] = await Promise.all([
    downloadAsset(catalogueName, "catalogue", manifest.catalogue),
    downloadAsset(signalsName, "signals", manifest.signals),
  ]);
  const detailBytes = await downloadDetailAssets(detailAssets);
  await verifyCompanyDetailPayloads(manifest, detailAssets, detailBytes);
  const now = new Date().toISOString();
  const manifestBytes = sourceManifestBytes;
  const manifestHash = await sha256(manifestBytes);
  const prefix = `generations/${generationId}`;
  const objects = [
    ["manifest.json", manifestBytes, "application/json; charset=utf-8", null, manifestBytes.byteLength, manifestHash],
    [catalogueName, catalogueBytes, "application/json", "gzip", catalogueBytes.byteLength, manifest.catalogue.sha256],
    [signalsName, signalsBytes, "application/json", "gzip", signalsBytes.byteLength, manifest.signals.sha256],
    ...detailAssets.map((metadata, index) => [
      metadata.filename,
      detailBytes[index],
      "application/json",
      "gzip",
      detailBytes[index].byteLength,
      metadata.sha256,
    ]),
    [signatureName, signatureBytes, "application/octet-stream", null, signatureBytes.byteLength, await sha256(signatureBytes)],
  ];
  const summary = manifest.summary || {};
  const sourceCommit = String(manifest.provenance?.source_commit || "");
  const incomingMarketAsOf = String(manifest.market_as_of || "");
  const incomingDataTimestamp = String(manifest.data_timestamp_utc || "");
  const incomingGeneratedAt = String(manifest.generated_at_utc || "");
  const expectedGeneration = {
    generation_id: generationId,
    market_as_of: incomingMarketAsOf,
    data_timestamp_utc: incomingDataTimestamp,
    generated_at_utc: incomingGeneratedAt,
    manifest_sha256: manifestHash,
    company_count: Number(summary.company_count || 0),
    triggered_company_count: Number(summary.triggered_company_count || 0),
    conditional_company_count: Number(summary.conditional_company_count || 0),
    pending_company_count: Number(summary.pending_company_count || 0),
    source_commit: sourceCommit,
  };
  const previous = await env.DB.prepare(
    `SELECT c.generation_id,
            c.updated_at,
            CASE WHEN g.generation_id IS NULL THEN 0 ELSE 1 END AS target_exists
     FROM current_generation AS c
     LEFT JOIN generations AS g ON g.generation_id = c.generation_id
     WHERE c.singleton = 1`,
  ).first();
  const storedGeneration = await env.DB.prepare(
    `SELECT generation_id, market_as_of, data_timestamp_utc, generated_at_utc,
            manifest_sha256, company_count, triggered_company_count,
            conditional_company_count, pending_company_count, source_commit,
            created_at, last_checked_at
     FROM generations
     WHERE generation_id = ?`,
  ).bind(generationId).first();
  const immutableMetadataMatches = !storedGeneration || (
    String(storedGeneration.generation_id || "") === expectedGeneration.generation_id
    && String(storedGeneration.market_as_of || "") === expectedGeneration.market_as_of
    && String(storedGeneration.data_timestamp_utc || "") === expectedGeneration.data_timestamp_utc
    && String(storedGeneration.generated_at_utc || "") === expectedGeneration.generated_at_utc
    && String(storedGeneration.manifest_sha256 || "").toLowerCase() === expectedGeneration.manifest_sha256
    && Number(storedGeneration.company_count) === expectedGeneration.company_count
    && Number(storedGeneration.triggered_company_count) === expectedGeneration.triggered_company_count
    && Number(storedGeneration.conditional_company_count) === expectedGeneration.conditional_company_count
    && Number(storedGeneration.pending_company_count) === expectedGeneration.pending_company_count
    && String(storedGeneration.source_commit || "") === expectedGeneration.source_commit
  );
  if (!immutableMetadataMatches) {
    throw new Error("stored generation metadata does not match the signed manifest");
  }
  const sameGenerationPointer = String(previous?.generation_id || "") === generationId;
  const auditTimestampsComplete = Boolean(
    storedGeneration
    && String(storedGeneration.created_at || "").trim()
    && String(storedGeneration.last_checked_at || "").trim()
  );
  const pointerIsDangling = Boolean(previous?.generation_id) && Number(previous?.target_exists || 0) !== 1;
  const databaseRepairNeeded = pointerIsDangling
    || (sameGenerationPointer && (!storedGeneration || !auditTimestampsComplete || !String(previous?.updated_at || "").trim()))
    || (!previous?.generation_id && Boolean(storedGeneration));
  let repairedObjects = false;
  if (sameGenerationPointer || storedGeneration) {
    for (const [name, body, contentType, contentEncoding, expectedSize, expectedHash] of objects) {
      const key = `${prefix}/${name}`;
      const existing = await env.DATA_BUCKET.head(key);
      if (
        !existing
        || existing.size !== expectedSize
        || String(existing.customMetadata?.sha256 || "").toLowerCase() !== String(expectedHash).toLowerCase()
      ) {
        await env.DATA_BUCKET.put(key, body, {
          httpMetadata: { contentType, ...(contentEncoding ? { contentEncoding } : {}) },
          customMetadata: { sha256: String(expectedHash).toLowerCase() },
        });
        repairedObjects = true;
      }
    }
  } else {
    for (const [name, body, contentType, contentEncoding, _expectedSize, expectedHash] of objects) {
      await env.DATA_BUCKET.put(`${prefix}/${name}`, body, {
        httpMetadata: { contentType, ...(contentEncoding ? { contentEncoding } : {}) },
        customMetadata: { sha256: String(expectedHash).toLowerCase() },
      });
    }
  }
  const generationValues = [
    generationId,
    incomingMarketAsOf,
    incomingDataTimestamp,
    incomingGeneratedAt,
    manifestHash,
    Number(summary.company_count || 0),
    Number(summary.triggered_company_count || 0),
    Number(summary.conditional_company_count || 0),
    Number(summary.pending_company_count || 0),
    sourceCommit,
    now,
    now,
  ];
  const generationStatement = env.DB.prepare(
    `INSERT INTO generations
      (generation_id, market_as_of, data_timestamp_utc, generated_at_utc, manifest_sha256,
       company_count, triggered_company_count, conditional_company_count, pending_company_count,
       source_commit, created_at, last_checked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(generation_id) DO UPDATE SET
       created_at = COALESCE(NULLIF(TRIM(generations.created_at), ''), excluded.created_at),
       last_checked_at = excluded.last_checked_at
     WHERE generations.market_as_of = excluded.market_as_of
       AND generations.data_timestamp_utc = excluded.data_timestamp_utc
       AND generations.generated_at_utc = excluded.generated_at_utc
       AND generations.manifest_sha256 = excluded.manifest_sha256
       AND generations.company_count = excluded.company_count
       AND generations.triggered_company_count = excluded.triggered_company_count
       AND generations.conditional_company_count = excluded.conditional_company_count
       AND generations.pending_company_count = excluded.pending_company_count
       AND generations.source_commit = excluded.source_commit`
  ).bind(...generationValues);
  const pointerStatement = env.DB.prepare(
    `INSERT INTO current_generation(singleton, generation_id, updated_at)
     SELECT 1, ?, ?
     WHERE EXISTS (
       SELECT 1 FROM generations
       WHERE generation_id = ?
         AND market_as_of = ?
         AND data_timestamp_utc = ?
         AND generated_at_utc = ?
         AND manifest_sha256 = ?
          AND company_count = ?
          AND triggered_company_count = ?
          AND conditional_company_count = ?
          AND pending_company_count = ?
          AND source_commit = ?
      )
      AND NOT EXISTS (
        SELECT 1
        FROM current_generation AS current_pointer
        JOIN generations AS served
          ON served.generation_id = current_pointer.generation_id
        WHERE current_pointer.singleton = 1
          AND (
            served.market_as_of > ?
            OR (served.market_as_of = ? AND served.data_timestamp_utc > ?)
            OR (
              served.market_as_of = ?
              AND served.data_timestamp_utc = ?
              AND served.generated_at_utc > ?
            )
          )
      )
      ON CONFLICT(singleton) DO UPDATE SET
        generation_id = excluded.generation_id,
        updated_at = excluded.updated_at`
  ).bind(
    generationId,
    now,
    ...generationValues.slice(0, 10),
    incomingMarketAsOf,
    incomingMarketAsOf,
    incomingDataTimestamp,
    incomingMarketAsOf,
    incomingDataTimestamp,
    incomingGeneratedAt,
  );
  const [generationResult, pointerResult] = await env.DB.batch([generationStatement, pointerStatement]);
  if (
    generationResult?.success !== true
    || pointerResult?.success !== true
    || Number(generationResult?.meta?.changes || 0) !== 1
  ) {
    throw new Error("generation database transaction did not commit one consistent pointer");
  }
  if (Number(pointerResult?.meta?.changes || 0) !== 1) {
    const current = await env.DB.prepare(
      `SELECT g.generation_id, g.market_as_of, g.data_timestamp_utc, g.generated_at_utc
       FROM current_generation AS c
       JOIN generations AS g ON g.generation_id = c.generation_id
       WHERE c.singleton = 1`,
    ).first();
    if (current?.generation_id && current.generation_id !== generationId) {
      return {
        status: "superseded",
        generation_id: generationId,
        market_as_of: manifest.market_as_of,
        current_generation_id: current.generation_id,
        current_market_as_of: current.market_as_of,
      };
    }
    throw new Error("generation database transaction did not commit one consistent pointer");
  }
  const status = sameGenerationPointer
    ? (repairedObjects || databaseRepairNeeded ? "repaired" : "unchanged")
    : (databaseRepairNeeded ? "repaired" : "updated");
  return { status, generation_id: generationId, market_as_of: manifest.market_as_of, company_count: Number(summary.company_count || 0) };
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
