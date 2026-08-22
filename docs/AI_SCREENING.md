# Local AI screening

## 资料时效门禁

AI 事实陈述必须区分“报告期”与“网页发布时间”。对 `market_as_of=2026` 的快照，
陈述中只有 2025/2026 期间信息才算 current/recent；只引用 2024 年或更早事实的
卡片会自动扣分并降为“观察·需更新资料”，不会继续显示为当前建议买入。历史年报仍
保留为背景证据，但页面会明确标注其时效，不能把旧资料冒充当前状态。重新运行本地
批次时，必须要求模型搜索最新年报/季报并在 `claims[].statement` 中写明报告期；
URL 路径中的发布日期或股票代码不算数据年份。

AI screening is an optional second-pass research overlay.  The seven
deterministic buy rules remain the factual baseline, but they are not a hard
gate on the AI opinion: a current, well-supported near-qualified company may
receive an independent `priority_buy` recommendation even when its
deterministic status is `conditional` or `observe`.  The page shows both
states separately.  The model cannot change deterministic scores, bounds,
vetoes, or buy types.

## Candidate packet

Build the complete local queue from a validated published catalogue:

```powershell
python -m tools.build_ai_screening `
  --snapshot tmp/run-31607003760-build/catalog-734b00931803b2d2.json.gz `
  --rules-root 'E:\模板汇总MD' `
  --out build\ai-screening-full
```

The queue includes deterministic triggers, Type7 boundary rows, conditional or
pending rows that can still cross the threshold, and the relevant rule excerpts.
The packet is compact: the selected type is complete, while other types are a
summary only.  The full queue is local and is not uploaded by this command.
Patch 7's common buy gate and the matching type section are injected; its sell
gate is intentionally excluded because this page ranks new buy candidates, not
post-purchase holding or exit decisions.

## OpenCode CLI / OpenCode Go 联网模型

The production-friendly runner uses the OpenCode CLI with an explicit
OpenCode Go model.  The current refresh uses `opencode-go/ox-alpha-free`.
Its isolated project
config allows only the harness `websearch` tool; shell, file writes, reads and
other MCP tools are denied, and the unrelated global `context7`, `gh_grep` and
`playwright` MCP servers are disabled for this isolated run.  `--model` is
required so a missing argument can never silently fall back to a paid model.
A bounded pilot can be run as follows:

```powershell
python -m tools.run_ai_screening_batch `
  --candidates build\ai-screening-pilot\ai-screening-candidates.jsonl `
  --out build\ai-screening-pilot\opencode-reviews.jsonl `
  --backend opencode `
  --batch-size 10 `
  --session-batches 4 `
  --limit 12 `
  --model opencode-go/ox-alpha-free `
  --effort max `
  --root .
```

`tools/opencode-screening/opencode.json` is the trust boundary for this pass.
The model may propose sources, but a URL is not authoritative merely because it
is syntactically valid; the source fields remain advisory claims and should be
reviewed before a strong verdict is published.

Sending company data and rule excerpts to the provider is an external data
transfer.  Run the command only after explicit user authorization.  Keys stay
in Reasonix configuration and are never written to the packet.

For the complete queue, use the batch runner instead of starting one model
session per company. It keeps shared rule fragments in the prompt prefix,
reviews a bounded group, and writes validated JSONL; a failed group remains
explicit. In the normal full run, a rejected group is bisected until the bad
row is isolated; only an irreducible one-row failure remains in the error
JSONL. `--fail-fast` keeps the stricter one-shot behavior for pilots:

```powershell
python -m tools.run_ai_screening_batch `
  --candidates build\ai-screening-full\ai-screening-candidates.jsonl `
  --out build\ai-screening-full\opencode-reviews.jsonl `
  --backend opencode `
  --batch-size 40 `
  --session-batches 4 `
  --model opencode-go/ox-alpha-free `
  --effort max `
  --root .
```

For a weekend refresh, `--allow-unsearched` is an explicit local-only mode:
the model may provide an opinion from the packet and the injected knowledge
base, but the runner clears claims and records the row as unsearched. The
public page labels this mixed result; it never presents it as web evidence.
Verified OpenCode-search rows take precedence over local rows when assembling
the final queue. Use the identity-checked assembler when several resumable
shards are available:

```powershell
python -m tools.assemble_ai_screening_reviews `
  --candidates build\ai-screening-full\ai-screening-candidates.jsonl `
  --reviews build\ai-screening-full\search-0.jsonl build\ai-screening-full\local-0.jsonl `
  --out build\ai-screening-full\opencode-reviews.jsonl
```

The assembler requires every candidate pair exactly once. A missing review is
a hard release error, never silently dropped or upgraded to `confirmed` by a
fallback.

The runner does not trust `web_search_performed` in the model JSON.  For every
company it requires a completed OpenCode `websearch` event whose query contains
the code or company name.  Every cited URL must also occur in the returned tool
results.  The resulting review persists the matched query and URL proof for the
release audit.

When the full queue is split across parallel runners, merge the shards through
the identity-checked merger rather than concatenating files manually:

```powershell
python -m tools.merge_ai_screening_reviews `
  --candidates build\ai-screening-full\ai-screening-candidates.jsonl `
  --reviews build\ai-screening-full\part-0.jsonl build\ai-screening-full\part-1.jsonl `
  --out build\ai-screening-full\opencode-reviews.jsonl
```

## Merge and preview

```powershell
python -m tools.build_ai_screening `
  --snapshot <validated-snapshot.json.gz> `
  --rules-root 'E:\模板汇总MD' `
  --out build\ai-screening-full `
  --review-mode opencode_mixed_review `
  --review-jsonl build\ai-screening-full\opencode-reviews.jsonl

python -m tools.calibrate_ai_screening_ranking `
  --source build\ai-screening-full\ai-screening.json `
  --output build\ai-screening-full\ai-screening-calibrated.json

python -m tools.audit_ai_screening_sources `
  --merged build\ai-screening-full\ai-screening-calibrated.json `
  --output build\ai-screening-full\source-audit.json

python -m tools.render_ai_screening_preview `
  build\ai-screening-full\ai-screening-calibrated.json `
  build\ai-screening-full\ai-screening.html
```

The static preview is intentionally local.  The Cloudflare route reads only a
generation-bound artifact; it does not call the model, search the web, or
accept credentials at request time.  The website route is now
`/ai-screening`, backed by `GET /api/ai-screening`.  The Pages Worker reads
only `ai-screening/<generation_id>.json` from the existing R2 bucket.  Missing
or mismatched artifacts produce a clear empty state and do not affect the
deterministic dashboard.

Before an artifact is made public, reduce the merged local result to the
strict public contract:

```powershell
python -m tools.publish_ai_screening `
  --merged build\ai-screening-full\ai-screening-calibrated.json `
  --output build\ai-screening-full\ai-screening-public.json `
  --expected-generation <generation-id> `
  --expected-market-as-of <YYYY-MM-DD> `
  --source-audit build\ai-screening-full\source-audit.json
```

Pass `--source-audit <json>` only after the separate URL audit has really run;
the OpenCode search flag alone is not a reachability or issuer-identity audit.

The resulting overlay keeps deterministic status/bounds and AI claims only;
`ai_is_advisory=true` and `auto_buy_promotion=false` are required.  Upload it
to the R2 key matching the generation after checking its SHA-256 through the
normal release process.  Never upload `ai-screening-input.json`, the full rule
packets, or provider credentials.
