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
export default {
  async scheduled(event, env, ctx) {
    const owner = "Muguett-DBY";
    const repo = "quant-buy-signals";
    const workflow = "mobile-market-data.yml";
    const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
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
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`GitHub dispatch failed: ${res.status} ${body}`);
    }
    return new Response("dispatched", { status: 202 });
  },
};
