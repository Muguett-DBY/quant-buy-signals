from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from ui import buy_types_page


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _type_info(*, triggered: bool = False) -> dict:
    return {
        "total": 8.0 if triggered else 4.0,
        "triggered": triggered,
        "veto": False,
        "sub_scores": {},
        "reasons": {},
    }


def _sample_frame() -> pd.DataFrame:
    rows = []
    for code, pe in (("000001", 20.0), ("000002", 999_999.0)):
        row = {
            "code": code,
            "name": f"样本{code[-1]}",
            "industry": "BANK",
            "max_score": 8.0,
            "num_types": 1,
            "buy_types": ["type1"],
            "diagnostic_type": "type1",
            "diagnostic_label": "DCF买入区",
            "price": 10.0,
            "pe": pe,
            "pb": 999_999.0,
            "roe": 999.0,
            "debt_ratio": 999.0,
            "bear_case": [],
        }
        for index in range(1, 7):
            row[f"type{index}"] = _type_info(triggered=index == 1)
        rows.append(row)
    return pd.DataFrame(rows)


def _preload_successful_generation(at: AppTest, *, page: str) -> None:
    frame = _sample_frame()
    now = time.time()
    at.session_state["buy_types_df"] = frame
    at.session_state["leaders_df"] = frame
    at.session_state["buy_types_timestamp"] = now
    at.session_state["buy_types_data_timestamp"] = now - 100
    at.session_state["buy_types_data_source"] = "stale_cache"
    at.session_state["buy_types_snapshot_warning"] = "refresh failed: upstream unavailable"
    at.session_state["buy_types_snapshot_validation"] = {
        "quotes": 2,
        "analysis_market_quotes": 2,
        "matched_financials": 2,
        "financial_coverage": 1.0,
        "expected_interim_report_date": "2026-03-31",
        "previous_interim_report_date": "2025-03-31",
        "current_dataset_coverage": {"income_interim": 1.0, "cashflow_interim": 1.0},
        "comparative_interim_coverage": {"income_interim": 0.5, "cashflow_interim": 0.5},
        "comparative_missing_codes": ["000002"],
        "strict_ttm_source_coverage": {
            "population": "SH_SZ_non_financial",
            "denominator": 2,
            "revenue": {"complete": 2, "missing": 0, "coverage": 1.0},
            "fcff": {"complete": 1, "missing": 1, "coverage": 0.5},
        },
    }
    at.session_state["buy_types_analysis_quality"] = {
        "score_coverage": 1.0,
        "dcf_attempt_coverage": 1.0,
        "dcf_valid_coverage": 0.5,
        "pipeline_issue_rate": 0.0,
    }
    at.session_state["buy_types_dcf_results"] = {
        "000001": {
            "current_price": 10.0,
            "zone": "观察区",
            "dcf_points": {
                "pessimistic": {"lower": 8.0, "upper": 9.0},
                "neutral": {"lower": 11.0, "upper": 12.0},
                "optimistic": {"lower": 14.0, "upper": 15.0},
            },
            "params": {"neutral": {"growth": 0.05}},
        }
    }
    at.session_state["buy_types_dcf_skip_reasons"] = {"000002": "现金流证据不足"}
    at.session_state["buy_types_pipeline_issues"] = [{"代码": "000009", "阶段": "dcf", "错误": "sample failure"}]
    at.session_state["buy_types_generation_identity"] = buy_types_page._current_analysis_generation_identity()
    at.session_state["page_radio"] = page


def test_buy_types_page_shows_provenance_and_keeps_extreme_finite_metrics_by_default():
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page="🎯 七种买入类型")

    at.run()

    assert not at.exception
    warnings = [element.value for element in at.warning]
    assert any("当前展示上一份通过校验的完整快照" in value for value in warnings)
    assert any(
        "类型6全局风控" in value
        and "15%" in value
        and "技术突破或商业模式创新" in value
        and "不会据此给出买入信号" in value
        for value in warnings
    )
    assert any("财报覆盖 2/2 (100.0%)" in element.value for element in at.caption)
    assert any(
        "当期 2026-03-31 / 同比 2025-03-31" in element.value
        and "当期覆盖: 利润表 100.0%、现金流量表 100.0%" in element.value
        and "同比覆盖: 利润表 50.0%、现金流量表 50.0%" in element.value
        for element in at.caption
    )
    assert any("缺少同比中期财报证据" in value and "不进入当前估值和评分代" in value for value in warnings)
    assert any(
        "近十二个月数据覆盖（沪深非金融）" in element.value
        and "收入 2/2 (100.0%)" in element.value
        and "自由现金流 1/2 (50.0%)" in element.value
        for element in at.caption
    )

    filter_switches = {
        element.label: element.value for element in at.sidebar.checkbox if element.label.startswith("启用")
    }
    assert filter_switches == {
        "启用 PE 区间筛选": False,
        "启用 PB 区间筛选": False,
        "启用 ROE 区间筛选": False,
        "启用负债率区间筛选": False,
    }
    stock_tables = [
        element.value
        for element in at.dataframe
        if isinstance(element.value, pd.DataFrame)
        and "名称" in element.value.columns
        and "诊断框架" in element.value.columns
    ]
    assert len(stock_tables) == 1
    assert stock_tables[0]["代码"].tolist() == ["000001", "000002"]


def test_search_count_matches_visible_rows_and_reset_restores_defaults():
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page="🎯 七种买入类型")
    at.run()

    search = next(element for element in at.text_input if element.label == "🔎 搜索代码或公司名")
    search.set_value("000001")
    at.run()

    visible_metric = next(element for element in at.metric if element.label == "筛选后显示")
    assert visible_metric.value == "1"
    stock_table = next(
        element.value
        for element in at.dataframe
        if isinstance(element.value, pd.DataFrame)
        and "名称" in element.value.columns
        and "诊断框架" in element.value.columns
    )
    assert stock_table["代码"].tolist() == ["000001"]

    next(element for element in at.sidebar.button if element.label == "重置全部筛选").click()
    at.run()

    search = next(element for element in at.text_input if element.label == "🔎 搜索代码或公司名")
    visible_metric = next(element for element in at.metric if element.label == "筛选后显示")
    assert search.value == ""
    assert visible_metric.value == "2"


def test_stock_lookup_zero_pads_short_numeric_code():
    page = "🏆 行业买入候选 · 个股查询"
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page=page)
    at.run()

    lookup = next(element for element in at.text_input if element.label == "股票代码")
    lookup.set_value("1")
    next(element for element in at.button if element.label == "分析").click()
    at.run()

    assert not at.exception
    assert any("000001 样本1" in element.value for element in at.caption)


def test_stock_lookup_preserves_not_applicable_and_insufficient_evidence_statuses():
    page = "🏆 行业买入候选 · 个股查询"
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page=page)
    frame = at.session_state["buy_types_df"].copy(deep=True)
    frame.at[0, "type3"] = {
        "total": 0.0,
        "triggered": False,
        "veto": False,
        "status": "not_applicable",
        "applicable": False,
        "sub_scores": {},
        "reasons": {"_scope": "趋势增速不足10%"},
    }
    frame.at[0, "type5"] = {
        "total": 0.0,
        "triggered": False,
        "veto": False,
        "status": "insufficient_evidence",
        "applicable": True,
        "sub_scores": {},
        "reasons": {"_missing": "缺少周期位置证据"},
    }
    at.session_state["buy_types_df"] = frame
    at.session_state["leaders_df"] = frame
    at.run()

    lookup = next(element for element in at.text_input if element.label == "股票代码")
    lookup.set_value("000001")
    next(element for element in at.button if element.label == "分析").click()
    at.run()

    assert not at.exception
    expander_labels = [element.label for element in at.expander]
    assert "➖ 3️⃣ 可持续高增长 — 不适用" in expander_labels
    assert "❓ 5️⃣ 强周期底部 — 证据不足" in expander_labels
    assert any("不适用" in element.value for element in at.info)
    assert any("证据不足" in element.value for element in at.warning)
    assert all("0.00分" not in label for label in expander_labels if "可持续高增长" in label or "强周期底部" in label)


def test_industry_candidate_page_repeats_global_status_and_uses_accurate_name():
    page = "🏆 行业买入候选 · 个股查询"
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page=page)

    at.run()

    assert not at.exception
    assert [element.value for element in at.title] == [page]
    assert [element.label for element in at.tabs] == ["🏆 行业买入候选", "🔍 个股查询"]
    assert any("财报覆盖 2/2 (100.0%)" in element.value for element in at.caption)
    warnings = [element.value for element in at.warning]
    assert any("1 只标的发生可追踪估值错误" in value for value in warnings)
    assert any(
        "类型6全局风控" in value
        and "技术突破或商业模式创新" in value
        and "不会据此给出买入信号" in value
        for value in warnings
    )


def test_sidebar_refresh_failure_does_not_delete_last_successful_generation(monkeypatch):
    page = "🏆 行业买入候选 · 个股查询"
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    _preload_successful_generation(at, page=page)
    at.run()
    previous_scores = at.session_state["buy_types_df"].copy(deep=True)
    previous_leaders = at.session_state["leaders_df"].copy(deep=True)
    previous_issues = list(at.session_state["buy_types_pipeline_issues"])
    calls = []

    def failed_refresh(*, force_refresh, user_evidence_payload=None):
        calls.append(force_refresh)
        return False

    monkeypatch.setattr(buy_types_page, "_run_full_analysis", failed_refresh)
    at.sidebar.button[0].click().run()

    assert not at.exception
    assert calls == [True]
    assert at.session_state["buy_types_df"].equals(previous_scores)
    assert at.session_state["leaders_df"].equals(previous_leaders)
    assert at.session_state["buy_types_pipeline_issues"] == previous_issues
    assert at.sidebar.radio[0].value == "🎯 七种买入类型"
