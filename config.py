"""DS_DCF 估值模型与数据请求的共享参数。

这里只保存被运行代码实际引用的跨模块常量；评分规则等领域逻辑由对应
引擎模块负责，不能假定修改本文件就会改变系统的全部行为。
"""

import os
from pathlib import Path


def _runtime_cache_directory() -> Path:
    """Return the writable cache root selected by the desktop launcher."""
    override = str(os.environ.get("DS_DCF_CACHE_DIR") or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError("DS_DCF_CACHE_DIR must be an absolute path")
        return candidate.resolve()
    return Path(__file__).resolve().parent / "data" / "cache"


CACHE_DIRECTORY = _runtime_cache_directory()

# ============================================================
# DCF 核心参数
# ============================================================

# CAPM 基线使用版本化的一手源快照，运行时不依赖网络。无风险利率取
# 中债国债收益率曲线 10 年期（2026-07-15）；ERP 取 Damodaran
# 2026-04-01 国家风险溢价工作簿中 China 的 rating-based total ERP。
# 两个日期分别保留，不能把较新的国债日期冒充 ERP 日期。
#
# 中债：https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN
# ERP：https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx
MODEL_RISK_DATA_AS_OF = "2026-07-15"
RISK_FREE_RATE = 0.017406
RISK_FREE_RATE_AS_OF = "2026-07-15"
RISK_FREE_RATE_TENOR = "10Y"
RISK_FREE_RATE_SOURCE = "ChinaBond China Government Bond Yield Curve"
RISK_FREE_RATE_SOURCE_URL = "https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN"

EQUITY_RISK_PREMIUM = 0.05799671740067751
EQUITY_RISK_PREMIUM_AS_OF = "2026-04-01"
EQUITY_RISK_PREMIUM_BASIS = "china_rating_based_total_erp"
EQUITY_RISK_PREMIUM_SOURCE = "Damodaran Country Risk Premiums"
EQUITY_RISK_PREMIUM_SOURCE_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx"
EQUITY_RISK_PREMIUM_SOURCE_SHA256 = "2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e"
MARGINAL_TAX_RATE = 0.25

# 行业 Beta 与债务成本仍来自 2026-01-05 的 China 行业工作簿。该日期
# 与上面的 CAPM 市场输入分开披露，避免把混合日期伪装成同一快照。
INDUSTRY_RISK_DATA_AS_OF = "2026-01-05"
INDUSTRY_BETA_SOURCE_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betaChina.xls"
INDUSTRY_BETA_SOURCE_SHA256 = "ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9"
INDUSTRY_WACC_SOURCE_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/waccChina.xls"
INDUSTRY_WACC_SOURCE_SHA256 = "525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6"

# 非金融企业使用行业去杠杆、剔除现金后的纯业务 Beta，再按公司
# 市值 D/E 重新加杠杆。多个 Damodaran 行业合并时按样本公司数加权。
DEFAULT_UNLEVERED_BETA = 1.591983  # Total Market (without financials)
INDUSTRY_UNLEVERED_BETA = {
    "REAL_ESTATE": 0.996072,
    "ALCOHOL": 1.692483,
    "FOOD_BEV": 1.129723,
    "HOME_APPLIANCE": 1.611911,
    "TEXTILE_APPAREL": 1.358888,
    "RETAIL": 1.333868,
    "TOURISM_EDU": 1.390260,
    "CHEM_PHARMA": 1.501292,
    "BIO_PHARMA": 1.740639,
    "TRAD_CN_MED": 1.501292,
    "MEDICAL_SERVICE": 1.202405,
    "SOFTWARE": 2.165853,
    "SEMICONDUCTOR": 1.841735,
    "ELEC_COMPONENT": 2.135111,
    "TELECOM": 1.902322,
    "MEDIA": 1.857500,
    "AUTO_VEHICLE": 2.030586,
    "AUTO_PARTS": 2.152515,
    "NEW_ENERGY_VEH": 1.794047,
    "CONST_MACHINERY": 2.022843,
    "INDUST_MACHINERY": 2.022843,
    "ELEC_EQUIP": 1.898317,
    "STEEL": 1.367789,
    "NONFERROUS": 1.892408,
    "CHEMICAL": 1.584395,
    "BUILDING_MATERIAL": 1.583035,
    "OIL_GAS": 1.130274,
    "COAL": 1.242105,
    "POWER_UTILITY": 0.608665,
    "CONSTRUCTION": 1.151598,
    "TRANSPORT": 1.080857,
    "AGRICULTURE": 1.108697,
    "LIGHT_MFG": 1.627538,
    # CAPCO service/diversified divisions use the matching 2026-01-05
    # Damodaran China rows, not the Total Market default.  Values are the
    # source workbook's cash-corrected unlevered betas.
    "BUSINESS_SERVICES": 1.894422,
    "PROFESSIONAL_SERVICES": 1.894422,
    "ENVIRONMENTAL_SERVICES": 1.137426,
    "DIVERSIFIED": 1.150707,
    "DEFAULT": DEFAULT_UNLEVERED_BETA,
}

# 金融企业的存款/保单负债是经营原料，不按工业企业去杠杆；
# justified P/B 模型直接使用对应行业已加杠杆 Beta 求股权成本。
INDUSTRY_FINANCIAL_LEVERED_BETA = {
    "BANK": 0.755892,
    "INSURANCE": 1.832525,
    "SECURITIES": 2.030850,
}

# 行业税前债务成本，同样已换算为人民币口径。当公司没有可靠的
# 实际借款成本时使用；不再使用任意的“无风险利率 + 300bp”。
DEFAULT_PRETAX_COST_OF_DEBT = 0.049229
INDUSTRY_PRETAX_COST_OF_DEBT = {
    "REAL_ESTATE": 0.051217,
    "ALCOHOL": 0.049229,
    "FOOD_BEV": 0.049170,
    "HOME_APPLIANCE": 0.049786,
    "TEXTILE_APPAREL": 0.049390,
    "RETAIL": 0.050600,
    "TOURISM_EDU": 0.050930,
    "CHEM_PHARMA": 0.049229,
    "BIO_PHARMA": 0.051459,
    "TRAD_CN_MED": 0.049229,
    "MEDICAL_SERVICE": 0.049229,
    "SOFTWARE": 0.051459,
    "SEMICONDUCTOR": 0.051459,
    "ELEC_COMPONENT": 0.051459,
    "TELECOM": 0.051353,
    "MEDIA": 0.050852,
    "AUTO_VEHICLE": 0.051459,
    "AUTO_PARTS": 0.051459,
    "NEW_ENERGY_VEH": 0.048944,
    "CONST_MACHINERY": 0.051459,
    "INDUST_MACHINERY": 0.051459,
    "ELEC_EQUIP": 0.049229,
    "STEEL": 0.049229,
    "NONFERROUS": 0.049229,
    "CHEMICAL": 0.049229,
    "BUILDING_MATERIAL": 0.049229,
    "OIL_GAS": 0.049048,
    "COAL": 0.049229,
    "POWER_UTILITY": 0.049229,
    "CONSTRUCTION": 0.049229,
    "TRANSPORT": 0.048297,
    "AGRICULTURE": 0.049229,
    "LIGHT_MFG": 0.049229,
    # RMB-converted costs from the same dated Damodaran WACC workbook:
    # (1 + source cost) * (1 + China inflation) / (1 + US inflation) - 1.
    "BUSINESS_SERVICES": 0.051459,
    "PROFESSIONAL_SERVICES": 0.051459,
    "ENVIRONMENTAL_SERVICES": 0.049229,
    "DIVERSIFIED": 0.049229,
    "DEFAULT": DEFAULT_PRETAX_COST_OF_DEBT,
}

# 为旧调用方保留的别名；新估值路径必须显式区分去杠杆与已加杠杆 Beta。
DEFAULT_BETA = DEFAULT_UNLEVERED_BETA

# 预测期（年）
FORECAST_YEARS = 5
# 补丁6“情况四”要求把当前估值与 10 年终局作比较。该口径与模板25
# 的五年显式预测期并存，不能用同一个未标注的 ``dcf_points`` 冒充。
LONG_HORIZON_FORECAST_YEARS = 10

# 永续增长率
TERMINAL_GROWTH = {
    "pessimistic": 0.000,  # 0% — 悲观情景默认零增长
    "neutral": 0.010,  # 1% — 略低于长期GDP
    "optimistic": 0.020,  # 2% — 接近长期名义GDP
}

# ============================================================
# 三情景参数调整幅度
# ============================================================

# WACC 调整（每个情景的基准 WACC 偏移）
SCENARIO_WACC_SHIFT = {
    "pessimistic": +0.010,  # 基准 WACC + 1%
    "neutral": 0.000,  # 基准 WACC
    "optimistic": -0.005,  # 基准 WACC - 0.5%
}

# 上下沿 WACC 微调（同一情景内上下沿的 WACC 差）
BAND_WACC_DELTA = 0.005  # ±0.5%

# 营收增长率：从历史数据自动提取，但可设上下限
GROWTH_CAP_MIN = -0.10  # 最低 -10%
GROWTH_CAP_MAX = 0.50  # 最高 +50%

# DCF 营收增长趋势回看窗口。它只控制增长率估计，不是财务历史抓取年数；
# 年报证据抓取窗口由 ``data.datacenter.ANNUAL_HISTORY_YEARS`` 单独定义为 10 年。
GROWTH_LOOKBACK_YEARS = 5

# 向后兼容旧调用方；新代码应使用语义明确的 ``GROWTH_LOOKBACK_YEARS``。
HISTORY_YEARS = GROWTH_LOOKBACK_YEARS

# FCF 利润率长期均衡值 (成熟企业长期FCF利润率通常3-6%，防止周期高点被当永久状态)
FCF_MARGIN_LONG_TERM = 0.04  # 4%

# ============================================================
# 模板25 核心阈值
# ============================================================

# FCF 利润率不设统一正值地板。亏损或微利企业不应被模型人为抬到 5%。
FCF_MARGIN_FLOOR = 0.0

# 深度安全边际：股价 ≤ 悲观值 × DEEP_SAFETY_RATIO
DEEP_SAFETY_RATIO = 0.80

# 泡沫高估：股价 ≥ 乐观值 × BUBBLE_RATIO
BUBBLE_RATIO = 1.20

# ============================================================
# 数据获取参数
# ============================================================

# 并发请求数
CONCURRENCY = 20

# 单次请求超时（秒）
REQUEST_TIMEOUT = 15

# 缓存有效期（秒），默认 24 小时
CACHE_TTL_SECONDS = 86400
