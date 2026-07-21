# 运行、审计与发布

## 环境与依赖

生产代码的依赖根是 `numpy`、`orjson`、`pandas`、`plotly`、`requests` 和 `streamlit`；`pillow` 作为 Streamlit 图像链的显式安全版本下限一并固定。`orjson` 用于带格式标识和校验和的快照编码，不是可选的静默加速器。`requirements.txt` 固定这些版本，`requirements-lock.txt` 是生产环境的完整 SHA256 哈希锁；测试和开发分别使用 `requirements-test.txt`、`requirements-dev.txt`，解析结果统一写入 `requirements-dev-lock.txt` 哈希锁。`build`、`pyinstaller`、`setuptools` 和 `wheel` 是显式发布依赖，不能只依赖当前环境恰好带入的传递关系。

依赖升级必须作为显式维护动作完成：

1. 在干净的 Python 3.12 环境安装 `requirements-dev.txt` 中固定版本的 `pip-tools`，执行 `python -m piptools compile --generate-hashes --no-emit-index-url --no-emit-trusted-host --resolver=backtracking --output-file=requirements-lock.txt requirements.txt`，再执行 `python -m piptools compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host --resolver=backtracking --output-file=requirements-dev-lock.txt requirements-dev.txt`；解析时可以通过环境变量选择 HTTPS 镜像，但锁文件不得内嵌 `--index-url` 或 `--trusted-host`，否则会覆盖运行者的 `PIP_INDEX_URL`。不要从包含无关工具的全局环境直接复制整个 `pip freeze`。
2. 执行 Python 3.11、3.12、3.13、3.14 测试矩阵、覆盖率、Ruff、Bandit、`pip-audit` 和 `pip check`。
3. 在四个 Python 版本上分别以 `pip install --require-hashes -r ...-lock.txt` 验证单一锁文件。若任一版本不能解析，必须为每个 Python 版本生成独立哈希锁并在 CI 中显式选择，不能降级为无哈希约束。
4. 记录直接依赖和传递依赖变化；只有全部验证通过才提交 lock 文件。

`run.bat` 创建/复用 `.venv`，优先通过 Windows `py` launcher 从受支持的 Python 3.14、3.13、3.12、3.11 中选择解释器；没有 `py` 时才验证 PATH 中的 `python`。脚本以两个锁文件内容、解释器路径和 Python 版本生成依赖指纹：首次启动、指纹变化或 `pip check` 失败时，先用 `requirements-bootstrap.txt` 的 SHA256 哈希锁安装固定 pip，再通过 `.venv\Scripts\python.exe` 使用 `--require-hashes` 安装生产锁；指纹未变且依赖一致时跳过重复安装。脚本始终设置 `PIP_REQUIRE_VIRTUALENV=true`。若调用方没有设置标准 `PIP_INDEX_URL`，桌面脚本默认使用 `https://pypi.tuna.tsinghua.edu.cn/simple`；已有 `PIP_INDEX_URL` 原样优先，可用于企业镜像或官方索引。HTTPS 镜像不配置 `trusted-host`，不能以国内网络兼容为由关闭证书验证、哈希校验或虚拟环境隔离。如果已有 `.venv` 使用了不支持的 Python，脚本会停止，不会退回全局安装。桌面脚本固定绑定 `127.0.0.1`；不得把无认证的刷新/分析界面直接监听到全网卡。共享部署必须显式增加认证、网络访问控制和计算资源限额。

wheel 构建使用 `pyproject.toml` 中固定的 setuptools/wheel 后端，并必须包含 `tools.run_full_audit`、F10/映射文件、官方零收入证明、`data/industry_capco_2025h2.json` 与 `data/industry_exchange_new_listings_2026.json`。CI 会在仓库目录之外安装生成的 wheel，再调用行业加载器并核验官方记录数、来源元数据和审计入口，防止源码目录意外掩盖缺失的包数据。

## 数据与分析晋级

发布 wheel 和桌面包除既有官方零收入证明外，还必须携带 `data/financial_zero_capex_evidence.json`。该文件只允许逐代码、逐 Q1 报告期的显式零，绑定巨潮 HTTPS PDF、SHA256、页码和陈述；任何普通空值、比较列金额或未列入白名单的公司都不得借此转成零。

新抓取数据依次经过：

1. 行情身份、源交易日期/时刻、获取时间、带来源和获取时间的上市日期状态、价格/市值单位和市场覆盖校验；
2. 财报字段、报告日期、连续年度、逐公司当期核心财报、Type3 商誉/并购现金流源字段存在性和行业数据校验；行业层分别核验官方权威覆盖、可绑定来源覆盖和模型行业覆盖，并生成 schema 8 的整代 `reporting_period_contract`；资本开支直接披露或详细现金流恒等式推导均保存可复算来源证明；银行、保险、证券年度专属指标分别保存数据商标准化字段、公式推导、监管来源、规则版本和显式缺失状态；
3. 生成 `eligible_codes` 及逐公司排除原因，并在沪深非金融分母上检查严格 TTM 收入与 FCFF 源覆盖；
4. 仅对 eligible universe 执行对应估值模型：非金融企业必须按 `FY + current YTD - prior YTD` 重构 TTM 收入及 CFO-Capex，并在 `[FY-1, FY, TTM]` 上归一化；金融企业走独立 justified P/B；
5. 检查评分覆盖、估值尝试/有效覆盖、异常率及相对上一代退化；
6. 在同一晋级锁内同时校验上一代 `data_timestamp` 与规范化 payload SHA256 两个令牌，再执行 compare-and-swap 晋级。

任一步失败都保留最后成功代。已有活动代时，调用者不能省略任一 CAS 令牌；同时间戳异内容、较旧代、获取时间倒退或令牌不匹配都会拒绝晋级。原始行情可以保留停牌、风险警示、退市整理或北交所记录，但这些记录不得进入自动买入分析；北交所和金融企业也不进入严格 TTM 源覆盖分母。`retrieved_at` 仅是获取时间，schema 8 另存上游 `source_trade_date`、`quote_tick_time`、上市日期来源、报告期合同、资本开支来源证明，以及 Type3 商誉/并购现金流字段是否由源接口返回；本次刚获取不等于刚成交，源字段存在但值为空也只能保持未知。schema 4 缺少报告期合同，schema 5 未强制资本开支来源证明，schema 6 缺少独立上市日期来源证明，schema 7 缺少 Type3 补充字段存在性，均不能由 schema 8 加载器继续用于分析。

## 固定种子审计

仓库提供可直接复现的全链路命令；默认复用仍有效的 schema 8 快照，增加 `--refresh` 才强制抓取新行情并在整代源质量门通过后晋级新一代。投资分析、估值、七类型评分和随机审计的 eligible universe 只包含沪深 A 股；北交所即使存在于原始源中也不参与上述分析：

```powershell
python -m tools.run_full_audit --seed 20260715 --sample-size 100
python -m tools.run_full_audit --refresh --seed 20260715 --sample-size 100
```

底层 `engine.audit.audit_random_sample()` 强制要求调用者传入快照验证生成的沪深 `eligible_codes`；命令行入口负责整代源质量门、候选代 CAS 晋级、快照 SHA256 和审计产物写入，任一独立不变量失败时退出码为 1。

JSON 产物包含完整七类型子分/依据、Type7 三个百分制账本及前置证据、证据完整性状态、估值模型与六点区间（非金融 DCF 参数或金融 P/B 参数）、严格 TTM 三组件、报告期合同、`[FY-1, FY, TTM]` 归一化依据、Type 4 独立十年面、逐公司跳过原因、管线问题、质量指标，以及快照内容、外部快照文件、代码、规则、行业数据、依赖、Git 和运行时哈希。引擎自身 validator、评分全字段重放及估值存在性/完整 payload/跳过原因重放都明确标为“同源”；另一个独立实现重算权重总分、Type7 模板权重/严格交集/安全边际、触发与优先级关系、空头证据排序、估值区间、关键公式和来源绑定。财务证据到每个业务子分的规则正确性由 `tests/test_buy_screener_rules.py` 和 `tests/test_quality_equity.py` 中固定预期、边界和反例向量验证，不能把同源重放误称为这部分的独立证明。

解读候选数量时必须同时给出 eligible universe、估值成功/跳过数、各框架证据完整覆盖和具体跳过原因。“证据不足”表示当前数据链不能完成判断，不得统计成“公司确定不符合”；同样，总分达到 7.0 但证据不完整、命中硬否决或未满足附加条件，也不得统计成已触发候选。系统不会因为 TTM 组件缺失而使用年度现金流补位。

命令行摘要会列出所有非经济性估值例外的代码与原因（数据缺失、源内矛盾、模型暂不支持或内部异常），避免把正常的经济不适用与需要处理的数据问题混在一起。交易日 15:15 前的同日量价批次不会产生 Type2 自动触发；界面必须持久说明此时候选数不包含依赖收盘冷度的公司。

## 本地发布验证

发布验证必须在由 `requirements-dev-lock.txt` 新建的干净 Python 3.12 虚拟环境中执行，不能以全局环境“恰好能运行”代替锁文件验证：

```powershell
if (-not $env:PIP_INDEX_URL) { $env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple" }
python -m pip install --require-hashes -r requirements-bootstrap.txt
python -m pip install --require-hashes -r requirements-dev-lock.txt
python -m pip check
python -m pytest --cov --cov-report=term-missing --cov-report=xml --cov-fail-under=75
python -m ruff check .
python -m ruff format --check .
python -m bandit -q -r app.py config.py data desktop engine ui tools --severity-level medium --confidence-level medium
python -m pip_audit --strict --progress-spinner off --require-hashes -r requirements-lock.txt
python -m pip_audit --strict --progress-spinner off --require-hashes -r requirements-dev-lock.txt
python -m build --wheel --no-isolation
$desktopSigningKey = 'C:\safe\ds-dcf-desktop-update-signing-key.properties'
python -m tools.build_desktop --signing-private-key $desktopSigningKey
```

使用 `python -m tools.build_desktop --desktop` 交付时，所有版本化制品必须进入桌面 `6BUYING_POINT/<版本>/`：应用目录固定为 `app/`，并同放可双击的 `DS_DCF-v<版本>-windows-x64-installer.exe`、便携 ZIP 和该版本更新清单；稳定快捷方式固定放在 `6BUYING_POINT/DS_DCF.lnk`。安装器必须在不需要管理员权限的当前用户目录内先完成内置 ZIP 的字节数、SHA256、内部版本清单与安全路径校验，才允许首次安装。不得重新把版本目录、ZIP、安装器或快捷方式散放到桌面根目录。

构建后还必须把唯一 wheel 安装到仓库外的空目录，并从该目录导入 `data.industry` 与 `tools.run_full_audit`，确认行业 JSON 和审计入口确实打入制品。wheel 是库/导入完整性制品，不是桌面运行包；根目录 `.streamlit/config.toml` 仅由受控的源码桌面包携带。随后检查 `git status --short`、`git ls-files --eol` 和下述禁入清单；固定种子沪深 eligible universe 审计必须在代码树干净且提交已确定时生成。最终源码压缩包必须从干净提交的 tracked files 构造，并执行 `python -m tools.verify_release_zip <zip路径>`；该检查会验证禁入文件、行尾、schema、沪深随机 100 身份及审计哈希与包内代码/规则/行业/依赖完全一致，禁止直接压缩含缓存的工作目录。

Windows 运行制品由 `python -m tools.build_desktop --signing-private-key <仓库外桌面私钥>` 使用固定版本 PyInstaller 构建。桌面私钥必须独立于 Android 市场数据私钥；可用 `pwsh -NoProfile -File tools/generate_p256_signing_key.ps1 -Output <仓库外路径> -EnvironmentVariableName DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64` 一次性生成，生成器拒绝覆盖已有文件且只向标准输出写公钥。构建器依次运行 EXE 的 `--version`、资源 `--health-check`，并让冻结 EXE 真正拉起 Streamlit 子进程、等待本机健康端点返回 `ok`；三项全部通过后才生成包含内部发布清单的便携 ZIP。随后构建一份单文件安装器，安装器内嵌同一 ZIP 与更新清单，并实际执行 `--version` 和只读 `--verify-bundle`。公开 CI 的 Windows/Python 3.12 门禁使用 `python -m tools.build_desktop --ci-smoke --output-root build/ci-smoke-output --work-root build/ci-smoke-work` 复用上述可执行文件与安装器冒烟链，但该模式只允许 `GITHUB_ACTIONS=true`，拒绝桌面交付、发布 URL 和任何签名输入，并且不会生成可发布更新清单或签名；正式默认路径仍在缺少私钥时失败。`--desktop` 仅把已经验证的目录、ZIP、安装器、更新清单和稳定快捷方式复制到当前用户桌面。桌面启动器只监听 `127.0.0.1`，缓存保存在 `%LOCALAPPDATA%\DS_DCF\cache`。上次成功分析结果仅在快照制品 SHA256、规则状态哈希、schema 和公司身份全部一致时恢复；任何不一致都会拒绝复用。更新必须来自配置的 HTTPS 清单，先以应用内固定桌面公钥验证独立签名，再按包大小和 SHA256 校验 ZIP 路径、大小、压缩率、内部版本和 EXE 身份，并安装到桌面 `6BUYING_POINT/<版本>/app/`，同时保留经校验的便携 ZIP。环境变量、`6BUYING_POINT/update_config.json`、随包配置按此顺序选择更新源；已有目标版本还会逐文件复核，任何额外、缺失或被篡改文件都会拒绝当成已安装版本。不得用 HTTP、无签名、无哈希下载或原地覆盖旧版本替代该流程。

证监会行业源只能由 `tools/build_official_industry_source.py` 从官方代码排序 PDF 确定性生成。生成器会核验 PDF SHA256、最少记录数、代码唯一性和门类代码映射；期后新股的补录 JSON 必须保存交易所文件 URL、SHA256、页码和行业代码。任何手工改写但无法回溯到这些来源的数据都不能进入发布包。

Type 7 的四份权威规则源位于模型编制环境的 `E:\模板汇总MD`：`第1模板.md`=`98D8A101A08CDB122AFD23C793FAA3EDF5E4E426EAE09E7FC20901476EA95B1D`，`第5模板.md`=`37A9CD43633BCD0BC1F2811738D48A7D1CFF659E5EF11B6FD9152F2ED0686946`，`补丁5.md`=`8E1C5114BE74254D686AC2B65EC7B3563E09F6C3B3F9A82B43E4D60A84CA42A4`，`补丁6.md`=`AA6A5B27E279B324A304A6BEA2C6FBA9AF6DC015F81ADB758329137B4E28B8F6`。路径只作编制记录，审计与发布校验绑定全部四个哈希。

## 发布卫生

发行包不得包含：

- `.reasonix/` 内部运行日志与状态；
- `__pycache__/`、`.pyc`、`.pyo`；
- `data/cache/` 下除 `.gitkeep` 外的运行时缓存；
- pickle、parquet、本地 secrets、coverage 和临时文件。

`.gitignore` 只防止新文件加入，不能移除已经跟踪的历史文件。发布前必须先从 Git 索引移除既有运行时产物，同时保留需要的本地缓存；CI 会检查并拒绝任何仍被跟踪的产物。

`.gitattributes` 规定源码、Markdown、JSON、YAML、TOML 和文本文件在仓库及工作树中统一为 LF，Windows `bat/cmd` 启动器工作树使用 CRLF，图片、字体、Office、PDF、pickle 和 parquet 明确按二进制处理。首次引入该策略时应由维护者执行一次 `git add --renormalize .` 并人工检查 diff；CI 会使用 `git ls-files --eol` 拒绝违反策略的已跟踪文件。
