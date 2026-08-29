# 网站运行、审计与发布

## 当前范围

唯一生产产品是 `https://quant.custard.top`。网站由 Cloudflare Pages advanced-mode worker 提供 GET-only 页面与 API；评分、估值和数据抓取只在 GitHub Actions 的受控数据流水线中发生。

`desktop/`、`app.py`、`ui/` 与 `android/` 已搁置，只保留历史实现。它们不进入网站 CI、Cloudflare 部署或市场数据发布门禁。协议中的 `mobile-*`、`mobile-data` 和 `MOBILE_*` 是已上线兼容名称，不代表手机客户端仍在维护。

## 环境与依赖

- 生产与 CI 使用 Python 3.13。
- `requirements-lock.txt` 固定网站运行依赖及传递依赖 SHA256；`requirements-dev-lock.txt` 固定测试工具。
- 安装必须使用 `--require-hashes`，不得关闭 TLS 校验或以 `trusted-host` 绕过证书验证。
- Cloudflare Wrangler 固定版本；GitHub Actions 固定到提交 SHA，不使用浮动 action 标签。

本地网站验证环境：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-bootstrap.txt
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev-lock.txt
.venv\Scripts\python.exe -m pip check
```

## 网站 CI

`.github/workflows/tests.yml` 是唯一权威测试工作流：

- 每次 push 到 `main` 固定运行 Cloudflare web gate、网站 data-integrity gate 和仓库卫生检查，不能由窄提交隐藏此前失败的改动。
- PR 与非 main 分支按路径选择重门禁；未知路径 fail-closed 进入 data-integrity gate。
- 仓库卫生检查始终运行，拒绝私钥、环境文件、缓存、压缩包、coverage 产物和错误行尾。
- Cloudflare gate 校验三个 Worker 语法及网站合同。
- data-integrity gate 只收集网站生产测试；搁置客户端模块用 `--ignore` 隔离，`test_release_zip.py` 中仍与网站审计相关的测试继续运行。
- 活跃的 `data/`、`engine/`、`tools/` 覆盖率不得低于 75%。
- 聚合 job 只接受显式 `true|false` 的门禁选择，并要求所有已选门禁与卫生检查成功。

本地等价检查：

```powershell
.venv\Scripts\python.exe -m pytest `
  --ignore=tests/test_android_release.py `
  --ignore=tests/test_build_desktop.py `
  --ignore=tests/test_desktop_installer.py `
  --ignore=tests/test_desktop_launcher.py `
  --ignore=tests/test_desktop_updater.py `
  --ignore=tests/test_streamlit_app.py `
  --ignore=tests/test_ui.py `
  -m "not desktop and not android and not parked_client"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
node --check cloudflare-cron/worker.js
node --check cloudflare/quant-dashboard/pages_worker.js
node --check cloudflare/quant-dashboard/refresh_worker.js
```

## Cloudflare 网站部署

`.github/workflows/deploy-cloudflare.yml` 只接受成功的 `tests` workflow_run，不提供手动绕过入口。部署前再次确认事件 SHA 是当前远端 `main`，旧排队任务只做成功 no-op。

同一部署依次更新：

1. `quant-market-refresh`：验证签名 Release 数据并镜像到 R2/D1；
2. `ds-dcf-dispatch`：工作日收盘数据构建调度器；
3. Pages advanced-mode worker：公开页面与 GET-only API；
4. `/api/methodology`、`/api/health`、`/api/health?deep=1`、`/api/meta` 与首页在线验收。

Cloudflare Cron 使用 UTC 和无歧义 weekday 名称：调度器为 `15 8 * * MON-FRI`，恢复镜像为 `0 15 * * MON-FRI`。GitHub Actions 自身保留 `17 8 * * 1-5` 作为调度兜底；两种平台的数字星期语义不同，不得互相照抄。

Cloudflare 账户令牌只注入三个 Wrangler 部署 step；refresh key、GitHub dispatch token 和签名私钥也只注入各自消费 step。

## 市场数据生产与发布

`.github/workflows/mobile-market-data.yml` 的兼容文件名保持不变，实际职责是发布网站市场数据。生产顺序为：

1. `preflight`：确认本次 SHA 仍是远端 `main`，并通过 GitHub API 证明同一 SHA 的 `tests.yml` 已成功；休市或已发布交易日可安全 no-op。
2. `build`：加载上次验证快照与合同化缓存，完成抓取、评分、独立审计、签名和完整分片校验。
3. `publish`：先上传不可变 Release 资产，校验当前与上一完整 generation；不覆盖同名不同字节资产。
4. `prepare_pages`：构建包含 manifest、catalogue、signals、16 个详情分片和签名的完整 Pages artifact。
5. `deploy_pages`：切换 stable manifest 前再次确认远端 `main == GITHUB_SHA`；main 已前进时失败关闭，已上传的不可变资产保持无害。
6. `mirror_cloudflare`：调用 refresh Worker，等待 D1/R2 与公开 API 收敛到精确 generation。
7. `verify_cleanup`：通过公开 URL 复核签名、哈希、大小、分片和深度健康，再只清理旧 market generation 资产。
8. `archive_manifest`：将已验证 manifest、签名和 SHA256 记录到 `mobile-data` 审计分支，不修改 `main`。

任一阶段失败都不得用旧数据冒充新数据，也不得把空响应、缺失字段或抓取错误写成数值 0。

## 本地住宅 IP 证据包

GitHub Runner 出口受限时，本地只负责采集慢变证据，不在本机评分、签名或直接写 Cloudflare。内容寻址证据包的标准流程为：

```powershell
.venv\Scripts\python.exe -m tools.evidence_bundle collect `
  --as-of YYYY-MM-DD `
  --codes-file data\cache\tdx3d_gap_codes.json `
  --sources segment,quality,research `
  --segment-provider eastmoney `
  --max-workers 4
.venv\Scripts\python.exe -m tools.evidence_bundle bundle --as-of YYYY-MM-DD
$bundleName = .venv\Scripts\python.exe -m tools.evidence_bundle resolve `
  --pointer build\evidence-bundle\evidence-cache-pointer.json `
  --expected-as-of YYYY-MM-DD
.venv\Scripts\python.exe -m tools.evidence_bundle verify `
  --pointer build\evidence-bundle\evidence-cache-pointer.json `
  --bundle (Join-Path build\evidence-bundle $bundleName) `
  --expected-as-of YYYY-MM-DD

# 审计两个时点之间的申万行业归属变化；输出只用于同行映射检查，不覆盖生产分类
.venv\Scripts\python.exe -m tools.audit_shenwan_industry_history `
  --codes-file data\cache\tdx3d_gap_codes.json `
  --from-as-of YYYY-MM-DD `
  --to-as-of YYYY-MM-DD `
  --output build\audit\shenwan-industry-drift.json
```

发布时必须先上传不可变 `evidence-cache-<sha256>.zip`，从 Release 重新下载并完整验证，最后才原子覆盖 `evidence-cache-pointer.json`。pointer 缺失时流水线可回退 Actions cache；pointer 存在但非法、摘要不符或导入失败时必须失败，不能静默退回。旧 mutable cache ZIP 只能在新证据包已导入、市场数据 generation 已发布且线上深度健康通过后删除。

`a-stock-data` 只作为端点和故障经验参考。生产不整体引入其 Skill、`mootdx` 或关闭 TLS 的实现；新浪财报备用源已经按本项目合同重写，只针对东财留下的精确 TTM 字段缺口调用。Baostock 仅在东财五年估值历史组件不可用时整组件接管；上交所 XBRL/深交所财务指标只填剩余空字段；互动易材料明确为公司自述、不可自动加分。所有新增缓存均保存并重放原始响应及 SHA-256。

## 数据与模型审计

权威数据链要求：

- 行情身份、交易日期/时刻、获取时间、上市日期来源、报告期合同和公司资格可追溯；
- 非金融企业严格按 `FY + current YTD - prior YTD` 重构 TTM 收入与 CFO-Capex；金融企业使用独立 justified P/B；
- CAPEX 必须来自可验证的直接披露或可复算现金流恒等式，负重构值与缺失值不得改成 0；
- 七类型结果保存固定子分、理由、分数边界、硬否决和待补证据；补丁 7 的价格/乐观估值闸门缺口必须由生产重放、独立全市场审计和发布 ZIP 验证共同确认；
- 普通发布允许上游确实不存在的事实继续显示“资料不足”，但不得把未决公司隐藏为确定不符合。

固定种子审计：

```powershell
.venv\Scripts\python.exe -m tools.run_full_audit --seed 20260715 --sample-size 100
.venv\Scripts\python.exe -m tools.run_full_audit --require-complete-evidence --seed 20260715 --sample-size 100
```

`--require-complete-evidence` 是理想零缺口审计，不是日常发布承诺。真实无历史、无盈利、机构不覆盖或需持仓确认的缺口应保持可见，不为降低缺口数而放松模型。

## 发布与回滚验收

提交到 `main` 后按以下顺序收口：

1. 等待同一 SHA 的 `tests`、两个网站门禁和聚合 gate 全部成功；
2. 等待自动 Cloudflare 部署成功，确认不是 stale no-op；
3. 激活并公开复核本地证据包；
4. 激活新证据包后手动运行网站市场数据工作流，`force_fresh_refresh=true`，另外两个模式为 `false`；这会显式重抓最新闭市日并建立新的验证快照。仅重算模型时使用 `rebuild_latest_closed=true`，它没有已验证快照就会拒绝运行，不会偷偷退化成现场抓取；
5. 确认 `/api/meta` 的 `source_commit` 等于最终 SHA，`/api/health?deep=1` 为 `integrity_ok=true`、`stale=false`；
6. 验证 catalogue index、至少一家公司详情、搜索/筛选/详情抽屉和移动视口；
7. 最后删除已无引用的旧 mutable cache ZIP。

Cloudflare 页面代码回滚应回到最近已通过 `tests` 的 main 提交并等待自动部署。市场数据不通过修改 D1 指针手工拼接；保留上一完整 generation，由正式签名发布/镜像流程恢复。

## 发布卫生

仓库不得跟踪私钥、token、`.env`、`__pycache__`、运行缓存、coverage、构建目录、压缩包或本地代理状态。`data/cache/` 仅允许 `.gitkeep`。本地 `build/evidence-bundle/` 与抓取缓存保持 ignored；上传 Release 不等于加入 Git。
