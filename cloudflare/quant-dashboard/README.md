# quant.custard.top

This directory contains the read-only Cloudflare dashboard and its scheduled
mirror.  The dashboard never calculates a score.  It reads the signed,
content-addressed mobile catalogue produced by GitHub Actions, stores each
generation immutably in R2, and advances a D1 pointer only after all three
assets and the ECDSA manifest signature pass validation.

- `schema.sql` is applied once to the D1 database.
- `refresh_worker.js` is deployed as `quant-market-refresh` with a weekday
  `45 8 * * 1-5` UTC trigger (16:45 Beijing), after the 16:17 GitHub refresh.
- `pages_worker.js` is the Pages advanced-mode worker.  Its API is GET-only:
  `/api/health`, `/api/meta`, `/api/manifest`, `/api/catalogue-index`,
  `/api/company/{code}`, and the backward-compatible `/api/catalogue` used by
  existing mobile clients.  The website loads the compact index first and
  fetches one full company record only when its detail drawer is opened.

The public page intentionally distinguishes “已触发”, “待确认”, “观察”,
“资料不足”, “不适用” and “不符合硬条件”; missing evidence is never shown as
a numeric zero.
