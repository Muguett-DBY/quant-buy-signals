# DS_DCF 架构总览

> 主攻方向：**网站**（quant.custard.top）。手机 app 与桌面版已搁置，代码保留但不迭代。

## 数据流（一句话）

```
GitHub Actions (mobile-market-data.yml) → 抓数据/评分 → 签名 catalogue (R2)
→ Cloudflare Pages worker (只读 API) → 网站 (index 先行, 按需拉详情)
```

## 目录职责

| 目录 | 职责 | 状态 |
|---|---|---|
| `engine/` | **评分核心**（七类型量化买入 + DCF 估值） | 活跃 |
| `data/` | **数据抓取/缓存/证据**（东财/新浪/通达信/巨潮） | 活跃 |
| `tools/` | 发布流水线（publish_mobile_snapshot、run_full_audit、签名） | 活跃 |
| `cloudflare/quant-dashboard/` | **网站**（Pages worker + R2 + D1，只读 API） | 活跃 · 主攻 |
| `cloudflare-cron/` | 网站恢复性镜像 worker | 活跃 |
| `tests/` | pytest 全套（约 2000+） | 活跃 |
| `audit/` | 随机 100 审计种子 + 发布审计 | 保留 |
| `desktop/` | Streamlit 桌面版（app.py 入口） | **搁置** |
| `android/` | Android app | **搁置** |
| `scripts/` | 本地维护脚本（TCP 回填等，手动跑） | 活跃 |
| `ui/` | 网页辅助页（buy_types/leaders） | 活跃 |
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
| `tdx_segment.py` | **通达信 TCP 主营构成兜底**（破东财限流，2026-08 新增） |
| `growth_evidence.py` | type3 增长证据（segment/并购） |
| `quality_history.py` | type7 长期市场历史 |
| `snapshot.py` / `mobile_snapshot.py` | 快照组装/移动端展示 |
| `cache.py` | SafeFileCache（gzip + 内容寻址） |

## 关键设计

- **content-addressed**：每个 generation 有 16 位哈希，manifest/catalogue/详情分片/签名绑定同一哈希
- **证据契约**：所有评分输入带 provenance（source/SHA-256），audit 独立重放校验
- **补丁系统**：补丁1-7 叠加在七类型规则上（见 docs/MODEL.md），总闸门在汇总后置过滤
- **数据源降级**：东财（限流）→ 新浪（TTM/历史）→ 通达信 TCP（主营构成）→ 缓存/预加载

## 已知巨型文件改造风险

`engine/buy_screener.py` 等拆分需谨慎：大量测试直接 import 内部函数（如
`_apply_patch7_total_gate`），拆分必须同步更新测试 + 全量回归。建议在无
发布窗口压力时单独做，一次一个文件。

## 本地维护命令

```powershell
# 全量测试
.venv\Scripts\python.exe -m pytest tests/ -q

# 触发线上重建（复用已收盘快照 + 强刷财务源）
# GitHub → Actions → mobile-market-data → workflow_dispatch → force_gap_refresh=true

# 通达信主营构成回填（住宅 IP，本地跑）
.venv\Scripts\python.exe scripts\_tdx_segment_backfill.py
```
