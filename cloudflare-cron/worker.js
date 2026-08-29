/**
 * Daily post-close dispatch for the DS_DCF mobile market build.
 *
 * Cloudflare Cron Trigger (08:15 UTC = 16:15 Asia/Shanghai, weekdays)
 * calls the GitHub Actions workflow_dispatch API directly.  A manual
 * dispatch starts immediately (no GitHub schedule queue), so the site
 * refreshes around 17:15 Beijing time instead of ~19:45.
 *
 * The GitHub cron in mobile-market-data.yml stays as a fallback for
 * days when this worker is unavailable.
 */
const DISPATCH_ATTEMPTS = 3;
const DISPATCH_TIMEOUT_MS = 20_000;

async function dispatch(env) {
  if (!env.GITHUB_TOKEN) throw new Error("dispatch token is not configured");
  const url = "https://api.github.com/repos/Muguett-DBY/quant-buy-signals/actions/workflows/mobile-market-data.yml/dispatches";
  let lastStatus = 0;
  let lastError = null;
  for (let attempt = 0; attempt < DISPATCH_ATTEMPTS; attempt += 1) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
          "User-Agent": "ds-dcf-dispatch-cron",
        },
        body: JSON.stringify({ ref: "main" }),
        signal: AbortSignal.timeout(DISPATCH_TIMEOUT_MS),
      });
      if (res.ok) return;
      lastStatus = res.status;
      try { await res.body?.cancel(); } catch { /* best-effort release before retry */ }
      if (![408, 425, 429].includes(res.status) && res.status < 500) break;
    } catch (error) {
      lastError = error;
    }
    if (attempt < DISPATCH_ATTEMPTS - 1) await new Promise((resolve) => setTimeout(resolve, 500 * (2 ** attempt)));
  }
  throw new Error(`GitHub dispatch failed with HTTP ${lastStatus || "network error"}`, { cause: lastError });
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatch(env).catch((error) => {
      console.error(JSON.stringify({ event: "github_dispatch_failed", error: String(error?.message || error) }));
    }));
  },
};
