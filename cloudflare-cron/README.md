# Cloudflare Cron dispatch for the daily post-close build

A tiny Cloudflare Worker whose Cron Trigger calls the GitHub
`workflow_dispatch` API at **08:15 UTC (16:15 Asia/Shanghai) on weekdays**.
A manual dispatch starts immediately, bypassing the 2-3 hour GitHub
schedule queue, so the site refreshes around 17:15 Beijing time instead
of ~19:45.  The GitHub cron in `mobile-market-data.yml` remains as a
fallback for days when this worker is unavailable.

## Files

- `worker.js` — the scheduled handler (POSTs to the GitHub dispatches API).
- `wrangler.toml` — Worker config and the cron trigger.

## Secrets

`GITHUB_TOKEN` must be a GitHub token with `workflow` scope for the
`Muguett-DBY/quant-buy-signals` repo.  Use `wrangler secret bulk` with a
JSON file — `wrangler secret put` has been observed to silently store an
empty value when fed through a PowerShell pipeline, which makes the
worker's dispatch fail with `401 Bad credentials` while the cron itself
still fires:

```powershell
$env:CLOUDFLARE_API_TOKEN = "<your Cloudflare API token>"
$tok = gh auth token
'{"GITHUB_TOKEN":"' + $tok + '"}' | Set-Content "$env:TEMP\secrets-bulk.json"
npx wrangler secret bulk "$env:TEMP\secrets-bulk.json" --name ds-dcf-dispatch
# verify: Invoke-WebRequest -Method Post https://ds-dcf-dispatch.1203135430.workers.dev/dispatch
# (a temporary POST /dispatch endpoint was used for verification and then removed)
```

Note: the repo's `GITHUB_PAT` in the local TOKEN.txt was 401 (invalid) as
of 2026-08-05; the worker currently uses the `gh` CLI OAuth token, which
has the `workflow` scope.  If `gh` is re-authenticated, re-run the secret
bulk above.

## Deploy

```powershell
Push-Location E:\DEV\DS_DCF\cloudflare-cron
$env:CLOUDFLARE_API_TOKEN = "<your Cloudflare API token>"
npx wrangler deploy
```

Deployed URL: https://ds-dcf-dispatch.1203135430.workers.dev
