# DS_DCF

DS_DCF 是面向 A 股的多情景估值与七类型量化诊断工具。它把每种框架拆成 0–10 分子指标、固定权重、明确否决条件和可追溯证据；“诊断最高分”不等于买入信号。

> 本项目只提供研究筛选与模型复算，不构成投资建议。外部行情、财报和行业映射可能延迟、缺失或修订；任何输出都需要结合公告原文、组合约束和人工复核。

## 网站（主攻方向）

- **线上站点**：https://quant.custard.top
- **架构**：Cloudflare 工作日 16:15 调度（GitHub 16:17 兜底）→ GitHub Actions 生成并签名不可变数据代 → Release → refresh Worker 验签后写入 R2/D1 → Pages worker 只读 API（GET-only）→ 网站按需拉取
- **代码**：`cloudflare/quant-dashboard/`（Pages API + R2/D1 镜像）、`cloudflare-cron/`（收盘数据构建调度加速器）、发布流水线 `tools/publish_mobile_snapshot.py`
- **量化口径**：见 `docs/MODEL.md`；**架构总览**：见 `docs/ARCHITECTURE.md`

交易所休市时，本次运行会记录原因后成功结束且不发布新数据；交易日若来源未刷新、数据并非当日、收盘时间证据不足或质量检查失败，则失败关闭并保留上一完整 generation。

手机 app（`android/`）与桌面版（`desktop/` + `app.py`）已搁置，代码保留但不迭代。

## 环境

- Python 3.13（与网站数据生产流水线一致）
- GitHub Actions `windows-latest` 是数据/评分的权威验证环境
- 生产依赖根及显式安全版本下限固定在 `requirements.txt`，完整传递依赖及制品 SHA256 固定在 `requirements-lock.txt`

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
if (-not $env:PIP_INDEX_URL) { $env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple" }
python -m pip install --require-hashes -r requirements-dev-lock.txt
```

## 网站本地验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_cloudflare_dashboard.py tests/test_website_ci.py -q
node --check cloudflare/quant-dashboard/pages_worker.js
node --check cloudflare/quant-dashboard/refresh_worker.js
```

网站不在本机运行 Streamlit；权威页面是 Cloudflare Pages advanced-mode worker。本地只做语法、合同与数据流水线验证，生产发布仍由 GitHub Actions 验证后执行。

首次分析会从外部数据源拉取行情与财报。schema 8 快照要求每条行情同时具备可解析的源交易日期、源时刻、获取时间和带来源绑定的上市日期状态，并保存整代唯一的 `reporting_period_contract`；该合同固定完整年度、当前累计报告期及上年同期，供非金融企业严格重构 TTM 收入与现金流。每个 TTM 资本开支组件还必须绑定来源披露或可复算的详细现金流恒等式；未知空值不会被填成 0。schema 8 还要求每家公司明确保存 Type3 深度取数需要的商誉与并购现金流字段是否由源接口返回；源字段真实留空时仍保持未知，绝不擅自填成 0。上市日期只用于区分“上市前不存在”与“上市后年度缺口”，没有可靠上市日期时保持未知，不会猜测。产品投资范围明确限定为沪深 A 股；北交所记录即使出现在原始源中也统一标记 `unsupported_market`，不进入 DCF/PB、七类型评分、严格 TTM 覆盖率分母或随机 100 抽样。只有通过数据完整性、股票资格边界、严格 TTM 源覆盖、完整分析质量闸门和并发代检查的候选快照才会替换上一代；“刷新数据”不会预先删除可用结果。schema 4 缺少报告期合同，schema 5 未强制资本开支来源证明，schema 6 缺少独立上市日期来源证明，schema 7 又没有强制保存 Type3 补充字段，均不能冒充 schema 8 继续分析。

沪深行业归属优先使用中国上市公司协会公布的证监会行业分类结果，并由代码排序 PDF 的 SHA256、统计期和逐股票记录共同绑定；统计期后上市公司使用交易所上市文件逐只补录。实时 F10 只用于比官方统计期更新的明确行业字段或更细分类，名称关键字不能把未知公司伪装成来源完备。行业健康度同时报告“官方权威覆盖”和“可绑定来源覆盖”，后者未达到 100% 时整代不能进入自动评分。

对于批量接口遗漏、但交易所季度现金流量表能明确证明本期资本开支为零的极少数记录，程序只接受版本化的逐代码、逐报告期白名单，并绑定公告 HTTPS 地址、PDF SHA256、页码和说明；公告上期比较列绝不回填本期。

行情获取时间不等于成交时间。系统分别保存上游交易日期/时刻与本机获取时间；周末、休市或上游缓存响应不会因为本次刚获取就被表述为实时成交。

## 已搁置客户端（仅保留历史实现）

`desktop/`、`app.py`、`ui/` 与 `android/` 只保留历史代码和兼容协议，不属于当前网站交付，也不进入网站 CI、Cloudflare 部署或市场数据发布门禁。现阶段不构建 EXE、安装器或 APK；若未来恢复客户端开发，应另行恢复其独立 CI 与发布验收，不能借用网站绿灯。

数据协议中的 `mobile-*`、`mobile-data`、`MOBILE_*` 等名字为已上线兼容标识，当前消费者是网站数据服务；保留名称不表示手机客户端仍在维护。

## 验证

交易日 16:15 前，两市持续到 15:30 的盘后固定价格交易及延迟行情尚未完成稳定汇总，量比和当日换手率不能作为可决策的收盘证据，Type2“市场周期冷度”会统一保持证据不足。界面会持久提示当前买入信号数量不包含可能依赖收盘冷度的公司；16:15 后重新刷新才可解读为当日七类现有证据下的候选数。

```powershell
.venv\Scripts\python.exe -m pytest -m "not desktop and not android and not parked_client" --cov --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m bandit -q -r config.py data engine tools --severity-level medium --confidence-level medium
python -m pip_audit --strict --progress-spinner off --require-hashes -r requirements-lock.txt
python -m pip check
```

覆盖率配置要求活跃网站数据/评分代码总覆盖率不低于 75%。CI 使用与生产一致的 Python 3.13：纯 Cloudflare 改动走轻量网页门禁，数据、评分或未分类改动一律 fail-closed 走完整网站数据门禁。桌面与 Android 专属测试保留在仓库，但不再阻塞网站发布。GitHub Actions 固定到提交 SHA，不使用可移动标签；依赖安装强制校验 lock 中的 SHA256，行尾策略由 `.gitattributes` 与 CI 共同检查。

网站门禁覆盖数据分页与部分失败、schema 8 快照/CAS 完整性、上市日期来源与历史窗口、Type3 补充字段、严格 TTM 报告期与资本开支来源绑定、非金融 DCF 与金融 justified P/B 公式及边界、七类型权重与否决规则、Type7 归类/12个子指标/前置路径重放、签名发布合同、Cloudflare 读取链与固定种子随机样本审计。

## 模型边界

- 非金融 DCF 的生产输入必须按 `完整年度 + 当前累计期 - 上年同期累计期` 分别重构 TTM 收入和 `经营现金流-资本开支` 代理，再以 `[FY-1, FY, TTM]` 归一化现金流；缺任一同口径组件时不回退到年度输入。该代理不是严格会计 FCFF，不能替代完整三表预测。
- 金融企业采用 P/B 情景估值，ROE 使用期初/期末平均归母权益；银行的净息差/资本充足/不良/拨备、保险的偿付能力/新业务价值/退保率、券商的风险覆盖/资本杠杆/流动性/稳定资金均保留数据商字段、公式和缺失状态。金融评分和估值均不使用工业企业 FCF/OCF 代理，缺监管字段不会按 0 分伪装成否决。
- 缺失、非有限或跨期数据不会被自动替换成“看起来合理”的正数。
- 定性、监管或公司治理证据缺失表示“证据不足”，不是已经证实公司不合格；只有可追溯证据命中规则时才形成相应硬否决。
- 前六类总分达到 7.0 后仍必须通过对应的一票否决和附加条件；每条展示理由最多 20 个字符，超长理由会在完整语义片段后显示省略号，完整数值与公式保留在结构化结果中。
- Type6 只表示风险投资候选属性：高景气技术型市值上限为 300 亿元，平稳产业反转型为 100 亿元，并必须遵守单票 `≤5%`、同类组合 `≤15%` 的硬上限及最大损失披露。
- Type7 先按周期敏感度、科技属性和需求稳定性归为弱周期、强科技或强周期，再用该类别自己的12个子指标计算商业模式、护城河和长期成长；三项证据完整且算术平均严格大于 `7.000` 才完成“优质股权质量认证”。行业只能作为低上限的间接判断，不能单独冒充商品驱动、专利、产品迭代、复购或宏观敏感度证据。强周期的商业模式或护城河低于5分会否决。质量认证不等于当前买点：弱周期还要核对模板5估值三项、补丁5安全边际与第一类价格位置；强科技还要完成科技股东文化路径、补丁5安全边际，并要求三维分别不低于7分、价格处于近五年市净率20%低分位；强周期必须完整通过第五类、核对带息债务减货币资金后的净债，并同时满足当前PB不高于1.20和近五年PB分位不高于20%。弱周期与强周期要求最新连续3至5年自由现金流中至少60%为正且最新一年为正；强科技也可由最近三年自由现金流严格逐年改善、最新经营现金流为正且按最近改善速度预计两年内转正来证明清晰转正路径。无论其他类型是否触发，第七类形成当前买点都必须通过自己的类别价格检查。旧的三份百分制账本仅保留为不可决策的历史诊断，不能再触发第七类。

完整公式、单位、原始规则文件哈希、风险参数源哈希、七类权重/否决规则与已知局限见 [模型、评分与数据口径](docs/MODEL.md)。运行维护、依赖更新、固定种子审计和发布卫生流程见 [运行、审计与发布](docs/OPERATIONS.md)。

## 许可

本项目采用 [PolyForm Noncommercial 1.0.0](LICENSE) 源码可见许可，可用于个人研究、测试及其他非商业目的；商业使用需要另行取得书面许可。它不是 OSI 意义的开源许可证。公开 GitHub 仓库允许平台用户按 GitHub 服务条款查看和 fork；公开可见不等于获得商业使用权。
