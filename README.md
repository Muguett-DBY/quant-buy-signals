# DS_DCF · Quant Buy Signals

**A personal research tool for comparing A-share valuations, company evidence, and screening results.**

[Open the dashboard](https://quant.custard.top) · [Model](docs/MODEL.md) · [Architecture](docs/ARCHITECTURE.md) · [Operations](docs/OPERATIONS.md)

`Python` · `Cloudflare` · `Financial data` · `Research`

## What it does

- Runs valuation scenarios and seven screening frameworks.
- Keeps source evidence, data gaps, and audit trails alongside results.
- Publishes verified market snapshots to a read-only web dashboard.

The website is the active product. Android and desktop/Streamlit clients are retained as historical code and are no longer the main development path.

## Set up the research environment

Use Python **3.13**, matching CI. From the repository root on Windows:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-bootstrap.txt
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev-lock.txt
.venv\Scripts\python.exe -m pip check
```

This prepares the local validation environment; it does not deploy or refresh the live dashboard. See [Operations](docs/OPERATIONS.md) for website checks and the controlled data-publication workflow.

## Repository map

| Area | Purpose |
| --- | --- |
| `engine/` | Valuation and screening rules |
| `data/` | Data providers, evidence, and snapshots |
| `cloudflare/quant-dashboard/` | Web dashboard and read-only API |
| `tools/` | Auditing and publication tools |
| `tests/` | Data, model, and release-contract checks |

For personal learning and research only, not investment advice. Public data may be delayed, incomplete, or incorrect; verify it before relying on any result.

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE). Commercial use requires separate permission.
