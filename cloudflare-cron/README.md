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
`Muguett-DBY/quant-buy-signals` repo.  To (re)set it:

```powershell
$env:CLOUDFLARE_API_TOKEN = "<your Cloudflare API token>"
(gh auth token) | npx wrangler secret put GITHUB_TOKEN
```

Note: the repo's `GITHUB_PAT` in the local TOKEN.txt was 401 (invalid) as
of 2026-08-05; the worker currently uses the `gh` CLI OAuth token, which
has the `workflow` scope.  If `gh` is re-authenticated, re-run the secret
put above.

## Deploy

```powershell
Push-Location E:\DEV\DS_DCF\cloudflare-cron
$env:CLOUDFLARE_API_TOKEN = "<your Cloudflare API token>"
npx wrangler deploy
```

Deployed URL: https://ds-dcf-dispatch.1203135430.workers.dev
