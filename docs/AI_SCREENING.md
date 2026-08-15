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

## OpenCode Go Plan / DeepSeek V4 Flash Max

The configured Reasonix provider is `opencode-go/deepseek-v4-flash`.  A bounded
pilot can be run as follows:

```powershell
python -m tools.run_ai_screening_reasonix `
  --candidates build\ai-screening-pilot\ai-screening-candidates.jsonl `
  --protocol tools\ai_screening_reasonix_prompt.md `
  --out build\ai-screening-pilot\reasonix-reviews.jsonl `
  --limit 12 `
  --model opencode-go/deepseek-v4-flash `
  --effort max `
  --max-steps 6 `
  --permission-mode dontAsk `
  --allowed-tools= `
  --ablate none `
  --preset balanced `
  --reasonix-dir tools\reasonix-opencode-go
```

The isolated project config pins the working OpenCode Go endpoint
(`https://opencode.ai/zen/go/v1`) and enables the provider-native read-only
web search.  Do not add MCP or writer tools to this pass: they are a separate
capability surface and can make the provider reject the request.  The model
may propose sources, but a URL is not authoritative merely because it is
syntactically valid; the bounded source-audit step must check it before a
strong verdict is published.

Sending company data and rule excerpts to the provider is an external data
transfer.  Run the command only after explicit user authorization.  Keys stay
in Reasonix configuration and are never written to the packet.

For the complete queue, use the batch runner instead of starting one model
session per company. It keeps shared rule fragments in the prompt prefix,
reviews a bounded group, and writes validated JSONL; a failed group remains
explicitly pending and can be retried later:

```powershell
python -m tools.run_ai_screening_batch `
  --candidates build\ai-screening-full\ai-screening-candidates.jsonl `
  --out build\ai-screening-full\reasonix-reviews.jsonl `
  --batch-size 10 `
  --model opencode-go/deepseek-v4-flash `
  --effort max `
  --max-steps 3 `
  --permission-mode dontAsk `
  --allowed-tools= `
  --ablate none `
  --reasonix-dir tools\reasonix-opencode-go
```

`tools.prepare_ai_screening_overlay` merges completed reviews with every
selected candidate. A missing review is published as `needs_review`, never
dropped and never upgraded to `confirmed` by a fallback.

## Merge and preview

```powershell
python -m tools.build_ai_screening `
  --snapshot <validated-snapshot.json.gz> `
  --rules-root 'E:\模板汇总MD' `
  --out build\ai-screening `
  --review-jsonl build\ai-screening\reasonix-reviews.jsonl

python -m tools.render_ai_screening_preview `
  build\ai-screening\ai-screening.json `
  build\ai-screening\ai-screening.html
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
  --merged build\ai-screening\ai-screening.json `
  --output build\ai-screening\ai-screening-public.json `
  --expected-generation <generation-id> `
  --expected-market-as-of <YYYY-MM-DD> `
  --source-audit build\ai-screening\source-audit.json
```

The resulting overlay keeps deterministic status/bounds and AI claims only;
`ai_is_advisory=true` and `auto_buy_promotion=false` are required.  Upload it
to the R2 key matching the generation after checking its SHA-256 through the
normal release process.  Never upload `ai-screening-input.json`, the full rule
packets, or provider credentials.
