# quant.custard.top

This directory contains the read-only Cloudflare dashboard and its scheduled
mirror.  The dashboard never calculates a score.  It reads the signed,
content-addressed mobile catalogue produced by GitHub Actions, stores each
generation immutably in R2, and advances a D1 pointer only after the manifest,
catalogue, signal index, all 16 company-detail shards, and the ECDSA signature
pass size, hash, schema, partition, and coverage validation.

- `migrations/` is the authoritative, idempotent D1 schema history and is
  applied by the production deployment before either Worker changes.  The
  top-level `schema.sql` remains a readable snapshot of the current schema.
- `refresh_worker.js` is deployed as `quant-market-refresh`.  The GitHub
  publication workflow calls its key-protected `/refresh` endpoint immediately
  after the stable Pages manifest is deployed; the weekday `0 15 * * MON-FRI` UTC
  trigger (23:00 Beijing) remains a recovery-only mirror check after the 16:15
  Cloudflare dispatch and 16:45 GitHub fallback, leaving room for delayed
  GitHub runners.  It never calculates scores.
- `pages_worker.js` is the Pages advanced-mode worker.  Its API is GET-only:
  `/api/health`, `/api/meta`, `/api/manifest`, `/api/catalogue-index`,
  `/api/company/{code}`, and the backward-compatible `/api/catalogue` used by
  existing mobile clients.  The website loads the compact index first and
  fetches one full company record only when its detail drawer is opened.  The
  compact-index route requires a generation ID and its published
  `index_contract` version, so a dashboard upgrade cannot reuse an old
  one-year immutable projection.  Before parsing or serving a catalogue or a
  company-detail shard, the worker checks the downloaded R2 body against the
  manifest SHA-256 (and the declared decompressed size); R2 object metadata
  alone is not treated as proof of content integrity.
- `ai_screening_seed.json` is published directly to its generation-bound R2
  object by the dedicated AI workflow.  It does not redeploy Pages or either
  Worker.  Publication requires every candidate and type pair, every search
  event, every HTTPS research source, and every company-level semantic source
  check to pass; access/parser warnings are build-time diagnostics and are not
  a public artifact state.

The public page intentionally distinguishes “已触发”, “待确认”, “观察”,
“资料不足”, “不适用” and “不符合硬条件”; missing evidence is never shown as
a numeric zero.  Company details show every factual input exposed by the
signed public detail contract, the available market date and source version,
the applicable threshold, and the calculation rule.  If a per-indicator
financial period or source link is not present in that contract, the page says
so explicitly instead of inferring one.  A company outside a framework's
economic scope is also kept separate from a framework that has no dedicated
model yet.

The drawer traps keyboard focus while open and restores focus to the company
row on close.  Name/code searches deliberately span every status and disable
the status selector while the query is active.  Type 7 first classifies the
company, calculates 12 class-specific items, then uses the arithmetic mean
of business model, moat and long-term growth for quality certification.  The
page separately shows the class-specific current-buy gates; technology
companies require all three dimensions at least 7 and a five-year P/B
percentile no higher than 20%.  Type 7 always applies its own class-specific
price gate even when another framework also triggers.  It shows ranges,
rather than zeroes, whenever a required item is incomplete.
