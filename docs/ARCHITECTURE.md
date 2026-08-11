# DS_DCF 架构总览

> 主攻方向：**网站**（quant.custard.top）。手机 app 与桌面版已搁置，代码保留但不迭代。

## 数据流（一句话）

```
Cloudflare ds-dcf-dispatch（08:15 UTC）→ GitHub Actions (mobile-market-data.yml)
→ 抓数据/评分 → 签名并发布不可变资产
→ Cloudflare refresh worker 验签并镜像到 R2/D1
→ Cloudflare Pages worker (只读 API) → 网站 (index 先行, 按需拉详情)
```

`ds-dcf-dispatch` 只提前触发收盘构建；GitHub Actions 自身的 08:17 UTC schedule 是兜底。
Cloudflare Pages 请求不依赖该调度器，它失效只会影响当日数据构建的及时性。

`cloudflare/quant-dashboard/market_signing_public_key.txt` 是网站发布链的仓库内公钥信任锚。
GitHub Actions 的签名后自检和发布前复验都读取该文件；refresh worker 因边缘运行时不能读取
仓库文件，内嵌完全相同的公钥。私钥只保存在 GitHub Secret 中。

## 冻结的移动端兼容名称

网站已经是唯一活跃产品，但以下名字属于现有线上发布协议，继续保留并不表示 Android
仍拥有数据链：

- 工作流文件 `mobile-market-data.yml`、Release tag `mobile-market-data`；
- 公共路由 `/mobile-data`、审计分支 `mobile-data`；
- `MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64` 等 Secret/环境变量；
- `mobile-market-data-*` artifact、cache key 和内部目录名；
- `publish_mobile_snapshot` 等已被生产数据资产引用的工具名。

这些名字不得仅为美观而重命名；迁移它们需要独立的兼容发布方案。新的网站代码、文档和
信任关系不得再依赖 `android/` 目录。

## 目录职责

| 目录 | 职责 | 状态 |
|---|---|---|
| `engine/` | **评分核心**（七类型量化买入 + DCF 估值） | 活跃 |
| `data/` | **数据抓取/缓存/证据**（东财/新浪/通达信/巨潮） | 活跃 |
| `tools/` | 发布流水线（publish_mobile_snapshot、run_full_audit、签名） | 活跃 |
| `cloudflare/quant-dashboard/` | **网站**（Pages worker + R2 + D1，只读 API） | 活跃 · 主攻 |
| `cloudflare-cron/` | GitHub Actions 收盘构建调度加速器（GitHub schedule 兜底） | 活跃 · 辅助 |
| `tests/` | pytest 网站数据/评分/发布契约（冻结客户端测试保留 marker） | 活跃 |
| `audit/` | 随机 100 审计种子 + 发布审计 | 保留 |
| `desktop/` | Streamlit 桌面版（app.py 入口） | **搁置** |
| `android/` | Android app | **搁置** |
| `scripts/` | 本地维护脚本（TCP 回填等，手动跑） | 活跃 |
| `ui/` | Streamlit 客户端页（buy_types/leaders） | **搁置** |
| `docs/` | MODEL.md（量化口径）、OPERATIONS.md、本文件 | 活跃 |

## engine/ 核心模块

| 文件 | 行数 | 职责 |
|---|---|---|
| `buy_screener.py` | ~8000 | **七类型筛选主逻辑**（评分/决策/补丁1-7）——巨型文件，改动需全量测试 |
| `audit.py` | ~7000 | 发布审计/重放校验——巨型文件 |
| `quantitative_evidence.py` | ~4600 | 量化证据评分（moat/增长/质量等子分） |
| `quality_equity.py` | ~3300 | 优质股权（type7 早期） |
| `type7_patch6.py` | ~3100 | type7 补丁6 分类 |
| `dcf.py` / `scenarios.py` / `risk.py` | — | DCF 估值/情景/风险 |
| `pipeline.py` | ~1600 | 流水线编排 |

## data/ 核心模块

| 文件 | 职责 |
|---|---|
| `fetcher.py` | 行情+财务抓取编排（东财主源 + 新浪兜底） |
| `datacenter.py` | 东财数据中心接口 |
| `sina_financial.py` | 新浪财务兜底（TTM 补缺 + 年度历史回填） |
| `tdx_segment.py` | **仅本地可选**的通达信 TCP 主营构成采集器（`mootdx` 不进入生产 Runner） |
| `growth_evidence.py` | type3 增长证据（segment/并购） |
| `quality_history.py` | type7 长期市场历史 |
| `snapshot.py` / `mobile_snapshot.py` | 快照组装/网站发布投影（后者保留兼容命名） |
| `cache.py` | SafeFileCache（gzip + 内容寻址） |

## 关键设计

- **content-addressed**：每个 generation 有 16 位哈希，manifest/catalogue/详情分片/签名绑定同一哈希
- **证据契约**：所有评分输入带 provenance（source/SHA-256），audit 独立重放校验
- **补丁系统**：补丁1-7 叠加在七类型规则上（见 docs/MODEL.md），总闸门在汇总后置过滤
- **数据源降级**：生产 Runner 使用东财/新浪及已校验缓存；通达信 TCP 只在可信本地机器采集，
  通过内容寻址 evidence bundle 交给 Runner 校验和导入，绝不从本地直接发布 Cloudflare。

## 已知巨型文件改造风险

`engine/buy_screener.py` 等拆分需谨慎：大量测试直接 import 内部函数（如
`_apply_patch7_total_gate`），拆分必须同步更新测试 + 全量回归。建议在无
发布窗口压力时单独做，一次一个文件。

## 本地维护命令

```powershell
# 网站数据/评分全量门禁
.venv\Scripts\python.exe -m pytest -m "not desktop and not android and not parked_client" -q

# 触发线上模型重建（复用最新已收盘快照）
# GitHub → Actions → publish website market data → workflow_dispatch
# → rebuild_latest_closed=true

# 本地采集 segment / quality / research（日期必须显式指定）
$asOf = 'YYYY-MM-DD'
.venv\Scripts\python.exe -m tools.evidence_bundle collect `
  --as-of $asOf `
  --codes-file data\cache\tdx3d_gap_codes.json `
  --sources segment,quality,research

# 可选通达信采集：mootdx 只安装在本地，不加入 requirements 或 GitHub Runner
.venv\Scripts\python.exe -m pip install mootdx
.venv\Scripts\python.exe scripts\_tdx_segment_backfill.py `
  --as-of $asOf `
  --codes-file data\cache\tdx3d_gap_codes.json

# 生成并在本地复核内容寻址 ZIP + pointer；先上传 immutable ZIP，最后更新 pointer
.venv\Scripts\python.exe -m tools.evidence_bundle bundle --as-of $asOf
$pointer = Get-Content build\evidence-bundle\evidence-cache-pointer.json | ConvertFrom-Json
.venv\Scripts\python.exe -m tools.evidence_bundle verify `
  --pointer build\evidence-bundle\evidence-cache-pointer.json `
  --bundle (Join-Path build\evidence-bundle $pointer.bundle.path)

# GitHub Release 是唯一交接点：immutable ZIP 先上传，pointer 最后原子切换
gh release upload mobile-market-data `
  (Join-Path build\evidence-bundle $pointer.bundle.path) `
  --repo Muguett-DBY/quant-buy-signals
gh release upload mobile-market-data `
  build\evidence-bundle\evidence-cache-pointer.json `
  --clobber --repo Muguett-DBY/quant-buy-signals
```
