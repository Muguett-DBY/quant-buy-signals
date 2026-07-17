import inspect
import json
import math
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd
import pytest

from ui import buy_types_page
from ui.buy_types_page import (
    TYPE_DIMENSIONS,
    _bear_case_lines,
    _diagnostic_type_label,
    _display_reason,
    _dcf_audit_rows,
    _eligible_analysis_inputs,
    _filter_type_selection,
    _filter_stock_search,
    _filter_numeric_range,
    _format_snapshot_age,
    _format_metric,
    _fmt_score,
    _invalidate_stale_analysis_state,
    _make_narrative,
    _market_coldness_status_message,
    _merge_user_evidence,
    _parse_user_evidence_json,
    _reset_buy_type_filters,
    _render_radar_chart,
    _run_full_analysis,
    _snapshot_reporting_period_contract,
    _spreadsheet_safe_csv,
    _status_icon,
    _type_risk_notice,
    _with_diagnostic_fields,
)
from ui.leaders_page import _add_business_candidate_columns


def test_score_formatter_rejects_non_finite_values():
    assert _fmt_score(None) is None
    assert _fmt_score("nan") is None
    assert _fmt_score(float("inf")) is None
    assert _fmt_score(True) is None
    assert _fmt_score(0) == 0.0


def test_metric_formatter_never_exposes_missing_or_non_finite_values():
    assert _format_metric(None) == "暂无数据"
    assert _format_metric(float("nan")) == "暂无数据"
    assert _format_metric("bad") == "暂无数据"
    assert _format_metric(0.1234, digits=1, scale=100, suffix="%") == "12.3%"


def test_search_treats_user_input_as_literal_text():
    frame = pd.DataFrame(
        [
            {"code": "600000", "name": "浦发银行"},
            {"code": "000001", "name": "平安[银行]"},
        ]
    )
    result = _filter_stock_search(frame, "[")
    assert result["code"].tolist() == ["000001"]


def test_type_filter_keeps_only_selected_signals_plus_true_no_signal_rows():
    frame = pd.DataFrame(
        [
            {"code": "1", "buy_types": ["type1"]},
            {"code": "2", "buy_types": ["type2"]},
            {"code": "3", "buy_types": []},
        ]
    )

    strict = _filter_type_selection(frame, ["type1"], include_no_signal=False)
    inclusive = _filter_type_selection(frame, ["type1"], include_no_signal=True)

    assert strict["code"].tolist() == ["1"]
    assert inclusive["code"].tolist() == ["1", "3"]


def test_stale_analysis_generation_is_removed_fail_closed(monkeypatch):
    frame = pd.DataFrame([{"code": "600519"}])
    state = {
        "buy_types_df": frame,
        "leaders_df": frame,
        "buy_types_dcf_results": {"600519": {}},
        "buy_types_generation_identity": {"code_sha256": "old"},
    }
    monkeypatch.setattr(buy_types_page.st, "session_state", state)
    monkeypatch.setattr(
        buy_types_page,
        "_current_analysis_generation_identity",
        lambda: {"code_sha256": "current"},
    )

    assert _invalidate_stale_analysis_state() is True
    assert "buy_types_df" not in state
    assert "leaders_df" not in state
    assert "buy_types_dcf_results" not in state
    assert "旧分析结果已失效" in state["buy_types_refresh_error"]


def test_current_analysis_generation_is_preserved(monkeypatch):
    frame = pd.DataFrame([{"code": "600519"}])
    identity = {"code_sha256": "current", "snapshot_schema_version": 6}
    state = {
        "buy_types_df": frame,
        "leaders_df": frame,
        "buy_types_generation_identity": dict(identity),
    }
    monkeypatch.setattr(buy_types_page.st, "session_state", state)
    monkeypatch.setattr(buy_types_page, "_current_analysis_generation_identity", lambda: dict(identity))

    assert _invalidate_stale_analysis_state() is False
    assert state["buy_types_df"] is frame


def test_persistent_analysis_cache_restores_only_for_exact_snapshot_and_rule_identity(monkeypatch, tmp_path):
    import data.snapshot
    import engine.buy_screener as bs

    snapshot_path = tmp_path / "market_snapshot.json.gz"
    snapshot_path.write_bytes(b"snapshot-generation-one")
    monkeypatch.setattr(data.snapshot, "DEFAULT_SNAPSHOT_PATH", snapshot_path)
    monkeypatch.setattr(buy_types_page, "_persistent_analysis_cache_enabled", lambda: True)
    identity = {"snapshot_schema_version": 7, "code_sha256": "a" * 64}
    monkeypatch.setattr(buy_types_page, "_current_analysis_generation_identity", lambda: dict(identity))

    scores = bs.screen_all_types(
        {"000001": {}},
        pd.DataFrame(
            [
                {
                    "code": "000001",
                    "name": "样本",
                    "market": "SZ",
                    "price": 10.0,
                    "pe": 10.0,
                    "pb": 1.0,
                    "market_cap": 1_000_000_000.0,
                    "quote_status": "trading",
                    "price_source": "last_trade",
                }
            ]
        ),
    )
    now = time.time()
    state = {key: None for key in buy_types_page._PERSISTED_ANALYSIS_STATE_KEYS}
    state.update(
        {
            "buy_types_df": scores,
            "buy_types_timestamp": now,
            "buy_types_data_timestamp": now,
            "buy_types_retrieved_at": now,
            "buy_types_data_source": "cache",
            "buy_types_snapshot_validation": {},
            "buy_types_snapshot_warning": "",
            "buy_types_cache_diagnostic": {},
            "buy_types_analysis_quality": {"score_rows": 1},
            "buy_types_dcf_results": {},
            "buy_types_dcf_skip_reasons": {},
            "buy_types_dcf_skip_classifications": {},
            "buy_types_eligible_codes": ["000001"],
            "buy_types_analysis_exclusions": {},
            "buy_types_user_evidence": {},
            "buy_types_market_coldness_status": {},
            "buy_types_pipeline_issues": [],
            "buy_types_dcf_audit_frame": pd.DataFrame([{"代码": "000001", "估值状态": "跳过"}]),
            "buy_types_dcf_audit_csv": b"audit",
            "buy_types_analysis_json": b"{}",
            "buy_types_generation_identity": dict(identity),
        }
    )

    assert buy_types_page._save_persistent_analysis_state(state) == "saved"
    session = {}
    monkeypatch.setattr(buy_types_page.st, "session_state", session)
    assert buy_types_page._restore_persistent_analysis_state() == (True, "hit")
    assert session["buy_types_df"].iloc[0]["code"] == "000001"
    assert session["leaders_df"].equals(session["buy_types_df"])

    snapshot_path.write_bytes(b"snapshot-generation-two")
    session.clear()
    restored, reason = buy_types_page._restore_persistent_analysis_state()
    assert restored is False
    assert reason == "snapshot_artifact_mismatch"


def test_reset_buy_type_filters_restores_fresh_session_defaults(monkeypatch):
    state = {
        "cb_type1": False,
        "include_no_signal": True,
        "selected_industries": ["BANK"],
        "score_min": 7.0,
        "enable_pe_filter": True,
        "search_table": "600519",
    }
    monkeypatch.setattr(buy_types_page.st, "session_state", state)

    _reset_buy_type_filters()

    assert all(state[f"cb_{type_key}"] is True for type_key in buy_types_page.TYPE_ORDER)
    assert state["include_no_signal"] is False
    assert state["selected_industries"] == []
    assert state["score_min"] == 0.0
    assert state["enable_pe_filter"] is False
    assert state["search_table"] == ""


def test_diagnostic_fields_are_the_actual_highest_score_not_priority_order():
    frame = pd.DataFrame(
        [
            {
                "type1": {"total": 7.1},
                "type2": {"total": 8.2},
                "type3": {"total": 8.2},
                "type4": {},
                "type5": {},
                "type6": {},
            }
        ]
    )

    result = _with_diagnostic_fields(frame)

    assert result.loc[0, "diagnostic_type"] == "type2"
    assert result.loc[0, "diagnostic_label"] == buy_types_page.TYPE_NAMES["type2"]
    assert result.loc[0, "diagnostic_score"] == 8.2
    assert result.loc[0, "max_score"] == 8.2


def test_eligible_analysis_inputs_exclude_snapshot_ineligible_codes():
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}, {"code": "000002"}]),
        financials={"000001": {}, "000002": {}},
        validation={"eligible_codes": ["000001"], "ineligible_codes": ["000002"]},
    )

    quotes, financials, eligible = _eligible_analysis_inputs(snapshot)

    assert quotes["code"].tolist() == ["000001"]
    assert list(financials) == ["000001"]
    assert eligible == ("000001",)


def test_eligible_analysis_inputs_defensively_exclude_bj_from_precomputed_analysis_views():
    snapshot = SimpleNamespace(
        eligible_codes=("000001", "920001"),
        analysis_quotes=pd.DataFrame(
            [
                {"code": "000001", "market": "SZ"},
                {"code": "920001", "market": "BJ"},
            ]
        ),
        analysis_financials={"000001": {}, "920001": {}},
    )

    quotes, financials, eligible = _eligible_analysis_inputs(snapshot)

    assert quotes["code"].tolist() == ["000001"]
    assert list(financials) == ["000001"]
    assert eligible == ("000001",)


def test_external_evidence_overlay_is_strict_traceable_and_non_mutating():
    original = {"000001": {"income_history": [{"REPORT_DATE": "2025-12-31"}]}}
    payload = {
        "1": {
            "technology_score": 8,
            "technology_score_evidence": {
                "source": "company-announcement",
                "evidence_id": "ann-001",
                "as_of": "2025-12-31",
            },
            "position_size_pct": 3,
            "type6_portfolio_pct": 10,
        }
    }

    merged, normalised = _merge_user_evidence(original, payload)

    assert "technology_score" not in original["000001"]
    assert merged["000001"]["technology_score"] == 8.0
    assert normalised["000001"]["technology_score_evidence"]["evidence_id"] == "ann-001"


def test_external_evidence_overlay_rejects_lookahead_after_the_snapshot_date():
    payload = {
        "000001": {
            "technology_score": 8,
            "technology_score_evidence": {
                "source": "company-announcement",
                "evidence_id": "ann-after-snapshot",
                "as_of": "2026-01-01",
            },
        }
    }

    with pytest.raises(ValueError, match="晚于行情快照日"):
        _merge_user_evidence({"000001": {}}, payload, as_of="2025-12-31")


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"000002": {"position_size_pct": 3}}, "不在本代合法分析集合"),
        ({"000001": {"technology_score": 8}}, "必须同时提供"),
        ({"000001": {"unexpected": 1}}, "不允许"),
    ],
)
def test_external_evidence_overlay_rejects_out_of_scope_or_untraceable_fields(payload, message):
    with pytest.raises(ValueError, match=message):
        _merge_user_evidence({"000001": {}}, payload)


def test_external_evidence_json_parser_is_bounded_and_requires_object():
    assert _parse_user_evidence_json(b'{"000001": {}}') == {"000001": {}}
    with pytest.raises(ValueError, match="顶层"):
        _parse_user_evidence_json(b"[]")
    with pytest.raises(ValueError, match="1MB"):
        _parse_user_evidence_json(b"x" * (buy_types_page.MAX_USER_EVIDENCE_BYTES + 1))

    with pytest.raises(ValueError, match="重复键"):
        _parse_user_evidence_json(b'{"000001": {}, "000001": {}}')

    nested = (
        b'{"000001":'
        + b"[" * (buy_types_page.MAX_USER_EVIDENCE_DEPTH + 1)
        + b"0"
        + b"]" * (buy_types_page.MAX_USER_EVIDENCE_DEPTH + 1)
        + b"}"
    )
    with pytest.raises(ValueError, match="嵌套层级过深"):
        _parse_user_evidence_json(nested)


def test_csv_export_neutralises_spreadsheet_formulas_without_mutating_frame():
    frame = pd.DataFrame([{"名称": '=HYPERLINK("https://example.invalid")', "依据": "  @SUM(1,1)", "数值": 1.0}])

    content = _spreadsheet_safe_csv(frame).decode("utf-8-sig")

    assert "'=HYPERLINK" in content
    assert "'  @SUM" in content
    assert frame.loc[0, "名称"].startswith("=")


def test_dcf_audit_rows_preserve_six_points_parameters_and_skip_reason():
    scores = pd.DataFrame([{"code": "000001", "name": "有效"}, {"code": "000002", "name": "跳过"}])
    rows = _dcf_audit_rows(
        scores,
        {
            "000001": {
                "dcf_points": {
                    "pessimistic": {"lower": 1.0, "upper": 2.0},
                    "neutral": {"lower": 3.0, "upper": 4.0},
                    "optimistic": {"lower": 5.0, "upper": 6.0},
                },
                "params": {"neutral": {"growth": 0.05}},
            }
        },
        {"000002": "现金流为负"},
        skip_classifications={"000002": {"category": "economic_not_applicable", "reason": "现金流为负"}},
    )

    assert rows[0]["pessimistic_lower"] == 1.0
    assert rows[0]["optimistic_upper"] == 6.0
    assert '"growth":0.05' in rows[0]["估值参数（JSON）"]
    assert rows[1]["跳过原因"] == "现金流为负"
    assert rows[1]["跳过分类"] == "economic_not_applicable"


def test_intraday_coldness_notice_persists_candidate_count_limit():
    notice = _market_coldness_status_message(
        {
            "evidence_available": False,
            "evidence_reason": "intraday_before_close",
            "eligible_evidence_count": 0,
            "eligible_companies": 4986,
            "eligible_evidence_coverage": 0.0,
        }
    )

    assert notice is not None
    level, message = notice
    assert level == "warning"
    assert "15:15后刷新" in message
    assert "不代表全市场最终只有这些公司" in message


def test_default_metric_filter_can_keep_triggered_companies_with_missing_metrics():
    frame = pd.DataFrame(
        [
            {"code": "000001", "pe": None},
            {"code": "000002", "pe": 20.0},
            {"code": "000003", "pe": 200.0},
        ]
    )
    inclusive = _filter_numeric_range(frame, "pe", 0.0, 100.0, include_missing=True)
    strict = _filter_numeric_range(frame, "pe", 0.0, 100.0, include_missing=False)

    assert inclusive["code"].tolist() == ["000001", "000002"]
    assert strict["code"].tolist() == ["000002"]


@pytest.mark.parametrize("type_key", list(TYPE_DIMENSIONS))
def test_radar_threshold_has_same_length_as_axes(monkeypatch, type_key):
    captured = []
    monkeypatch.setattr(buy_types_page.st, "plotly_chart", lambda fig, **_: captured.append(fig))
    dims = TYPE_DIMENSIONS[type_key]
    row = {type_key: {"sub_scores": {key: 8.0 for key, _, _ in dims}}}

    _render_radar_chart(type_key, row)

    figure = captured[0]
    assert len(figure.data[0].r) == len(figure.data[0].theta)
    assert len(figure.data[1].r) == len(figure.data[1].theta)
    assert len(figure.data[1].r) == len(dims) + 1
    assert figure.data[1].name == "7分视觉参考（非子项门槛）"


def test_snapshot_age_formatter_is_bounded_and_readable():
    assert _format_snapshot_age(None, now=1_000) == "未知"
    assert _format_snapshot_age(1_100, now=1_000) == "0秒"
    assert _format_snapshot_age(900, now=1_000) == "1分钟"
    assert _format_snapshot_age(1_000, now=1_000 + 2 * 86_400) == "2天"


def test_narrative_uses_explicit_veto_and_trigger_state():
    vetoed = _make_narrative("type5", 8.0, {}, {"_veto": "盈利年数不足"}, [], triggered=True, veto=True)
    observed = _make_narrative("type5", 7.0, {}, {}, [], triggered=False, veto=False)
    assert "一票否决" in vetoed
    assert "触发买入信号" not in vetoed
    assert "未触发" in observed


def test_narrative_handles_missing_dimension_scores():
    narrative = _make_narrative(
        "type1",
        0.0,
        {"1a": None},
        {"1a": "估值数据缺失"},
        [("1a", "买入区深度", 0.3)],
    )
    assert "买入区深度无数据" in narrative


def test_status_icon_gives_veto_precedence_over_trigger():
    assert _status_icon(triggered=True, veto=True) == "🚫"
    assert _status_icon(triggered=True, veto=False) == "✅"
    assert _status_icon(triggered=False, veto=False) == "⬜"


def test_not_applicable_and_insufficient_statuses_are_rendered_without_english_markers():
    assert _status_icon(triggered=False, veto=False, status="not_applicable") == "➖"
    assert _status_icon(triggered=False, veto=False, status="insufficient_evidence") == "❓"

    not_applicable = _make_narrative(
        "type3",
        0.0,
        {},
        {"_scope": "趋势增速不足10%"},
        [],
        status="not_applicable",
    )
    insufficient = _make_narrative(
        "type4",
        0.0,
        {},
        {"_missing": "缺长期趋势或DCF终局证据"},
        [],
        status="insufficient_evidence",
    )

    assert "不适用" in not_applicable
    assert "趋势增速不足10%" in not_applicable
    assert "证据不足" in insufficient
    assert "一票否决" not in not_applicable + insufficient


def test_display_reason_hides_legacy_machine_evidence_ids():
    assert _display_reason("证据:patch6-observable") == "可核验的财务与行业数据"
    assert _display_reason("证据:patch6-type2c-qua") == "量价与换手数据"
    assert _display_reason("PB分位与库存去化") == "PB分位与库存去化"


def test_diagnostic_selection_excludes_na_and_insufficient_frameworks():
    frame = pd.DataFrame(
        [
            {
                "type1": {"total": 10.0, "status": "insufficient_evidence"},
                "type2": {"total": 9.0, "status": "not_applicable"},
                "type3": {"total": 6.0, "status": "observe"},
                "type4": {},
                "type5": {},
                "type6": {},
            }
        ]
    )

    result = _with_diagnostic_fields(frame)

    assert result.loc[0, "diagnostic_type"] == "type3"
    assert result.loc[0, "diagnostic_score"] == 6.0
    assert result.loc[0, "max_score"] == 6.0


def test_type6_risk_notice_is_exposed_to_the_user():
    assert _type_risk_notice("type6", {"_risk": "若判断错最大亏损≤5%"}) == "若判断错最大亏损≤5%"
    assert _type_risk_notice("type5", {"_risk": "unused"}) == ""


def test_bear_case_is_rendered_as_three_auditable_weaknesses():
    lines = _bear_case_lines(
        {
            "bear_case": [
                {"dimension": "1b", "score": 2.0, "reason": "价值陷阱未排除"},
                {"dimension": "1c", "score": 3.5, "reason": "现金流偏弱"},
                {"dimension": "1d", "score": 4.0, "reason": "催化剂不足"},
            ]
        }
    )
    assert lines == [
        "1b 2.0分：价值陷阱未排除",
        "1c 3.5分：现金流偏弱",
        "1d 4.0分：催化剂不足",
    ]


def test_diagnostic_framework_is_separate_from_actual_buy_triggers():
    assert _diagnostic_type_label({"diagnostic_type": "type3"}) == "3️⃣ 可持续高增长"
    assert _diagnostic_type_label({"diagnostic_label": "最高评分框架"}) == "最高评分框架"
    assert _diagnostic_type_label({"primary_type": None, "primary_label": "无触发（不买）"}) == ""


def test_active_market_ui_does_not_load_executable_pickle_cache():
    source = inspect.getsource(_run_full_analysis)
    state_source = inspect.getsource(buy_types_page._build_successful_analysis_state)
    assert "pickle" not in source
    assert "get_market_snapshot" in source
    assert "run_market_analysis" in source
    assert "buy_types_pipeline_issues" in state_source
    assert "buy_types_snapshot_warning" in state_source
    assert "persist_network=False" in source
    assert source.index("run_market_analysis") < source.index("save_market_snapshot(")


def test_business_candidate_columns_handle_rows_with_no_eligible_signal():
    frame = pd.DataFrame(
        [
            {
                "type1": {"total": 8.0, "triggered": False, "veto": False},
                "type2": {"total": 9.0, "triggered": True, "veto": True},
                "type3": {},
                "type4": {},
                "type5": {},
            },
            {
                "type1": {"total": 7.5, "triggered": True, "veto": False},
                "type2": {},
                "type3": {},
                "type4": {},
                "type5": {},
            },
        ]
    )

    result = _add_business_candidate_columns(frame)

    assert math.isnan(result.loc[0, "business_score"])
    assert pd.isna(result.loc[0, "business_type"])
    assert result.loc[1, "business_score"] == 7.5
    assert result.loc[1, "business_type"] == "type1"


class _FakeStatus:
    def write(self, *_args, **_kwargs):
        return None

    def update(self, *_args, **_kwargs):
        return None


def _patch_analysis_ui(monkeypatch, state):
    import data.market_coldness
    import engine.market_coldness

    monkeypatch.setattr(buy_types_page.st, "session_state", state)
    monkeypatch.setattr(buy_types_page.st, "spinner", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(buy_types_page.st, "status", lambda *_args, **_kwargs: _FakeStatus())
    monkeypatch.setattr(buy_types_page.st, "toast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(buy_types_page.st, "error", lambda *_args, **_kwargs: None)
    coldness = SimpleNamespace(
        available=True,
        source="test fixture",
        source_url="https://example.invalid/market-coldness",
        retrieved_at="2026-07-16T00:00:00Z",
        fetched_count=1,
        total_expected=1,
        coverage=SimpleNamespace(to_dict=lambda: {"complete_records": 1}),
        cache_hit=True,
        cache_diagnostic="isolated_test_fixture",
        reason="",
    )
    monkeypatch.setattr(data.market_coldness, "fetch_market_coldness_snapshot", lambda **_kwargs: coldness)
    monkeypatch.setattr(
        engine.market_coldness,
        "build_market_coldness_evidence",
        lambda *_args, **_kwargs: {"000001": {}},
    )


def _strict_ttm_validation(**values):
    validation = {
        "eligible_codes": ["000001"],
        "quotes": 1,
        "matched_financials": 1,
        "financial_coverage": 1.0,
        "reporting_period_contract": {
            "annual_report_date": "2025-12-31",
            "current_interim_report_date": "2026-03-31",
            "prior_interim_report_date": "2025-03-31",
            "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
        },
    }
    validation.update(values)
    return validation


def test_snapshot_reporting_period_contract_is_typed_and_requires_strict_ttm_basis():
    from engine.dcf import ReportingPeriodContract

    contract = _snapshot_reporting_period_contract(SimpleNamespace(validation=_strict_ttm_validation()))
    assert isinstance(contract, ReportingPeriodContract)
    assert contract.annual_report_date == "2025-12-31"
    assert contract.current_interim_report_date == "2026-03-31"
    assert contract.prior_interim_report_date == "2025-03-31"

    invalid = _strict_ttm_validation()
    invalid["reporting_period_contract"]["period_basis"] = "annual_only"
    with pytest.raises(ValueError, match="拒绝年度数据回退"):
        _snapshot_reporting_period_contract(SimpleNamespace(validation=invalid))


def test_missing_snapshot_reporting_contract_fails_closed_before_pipeline_and_preserves_generation(monkeypatch):
    import data.fetcher
    import data.snapshot
    import engine.pipeline

    previous = pd.DataFrame([{"code": "old"}])
    state = {"buy_types_df": previous, "leaders_df": previous}
    _patch_analysis_ui(monkeypatch, state)
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}]),
        financials={"000001": {}},
        source="network",
        data_timestamp=1_000.0,
        warning="",
        validation={"eligible_codes": ["000001"]},
    )
    monkeypatch.setattr(data.fetcher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(data.snapshot, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    analysis_calls = []
    monkeypatch.setattr(
        engine.pipeline,
        "run_market_analysis",
        lambda *_args, **_kwargs: analysis_calls.append(True),
    )
    saves = []
    monkeypatch.setattr(data.snapshot, "save_market_snapshot", lambda *_args, **_kwargs: saves.append(True))

    assert _run_full_analysis(force_refresh=True) is False
    assert analysis_calls == []
    assert saves == []
    assert state["buy_types_df"] is previous
    assert "严格TTM报告期契约" in state["buy_types_refresh_error"]
    assert "拒绝年度数据回退" in state["buy_types_refresh_error"]


def test_failed_pipeline_keeps_the_complete_previous_ui_generation(monkeypatch):
    import data.fetcher
    import data.snapshot
    import engine.pipeline

    previous = pd.DataFrame([{"code": "old"}])
    previous_leaders = pd.DataFrame([{"code": "old-leader"}])
    previous_issues = [{"代码": "old", "阶段": "dcf", "错误": "old-error"}]
    state = {
        "buy_types_df": previous,
        "leaders_df": previous_leaders,
        "buy_types_pipeline_issues": previous_issues,
        "buy_types_data_source": "cache",
    }
    _patch_analysis_ui(monkeypatch, state)
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}]),
        financials={"000001": {}},
        source="network",
        data_timestamp=1_000.0,
        warning="",
        validation=_strict_ttm_validation(),
    )
    monkeypatch.setattr(data.fetcher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(data.snapshot, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    saves = []
    monkeypatch.setattr(data.snapshot, "save_market_snapshot", lambda *_args, **_kwargs: saves.append(True))
    monkeypatch.setattr(
        engine.pipeline,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scoring failed")),
    )

    assert _run_full_analysis(force_refresh=True) is False
    assert state["buy_types_df"] is previous
    assert state["leaders_df"] is previous_leaders
    assert state["buy_types_pipeline_issues"] is previous_issues
    assert state["buy_types_data_source"] == "cache"
    assert saves == []


def test_network_snapshot_is_promoted_only_after_complete_analysis(monkeypatch):
    import data.fetcher
    import data.snapshot
    import engine.pipeline

    state = {}
    _patch_analysis_ui(monkeypatch, state)
    scores = pd.DataFrame([{"code": "000001"}])
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}]),
        financials={"000001": {}},
        source="network",
        data_timestamp=1_000.0,
        warning="",
        validation=_strict_ttm_validation(),
    )
    events = []
    quality = {
        "ok": True,
        "expected_companies": 1,
        "score_rows": 1,
        "score_coverage": 1.0,
        "dcf_attempted": 1,
        "dcf_attempt_coverage": 1.0,
        "dcf_valid": 1,
        "dcf_valid_coverage": 1.0,
        "pipeline_issue_rate": 0.0,
    }
    monkeypatch.setattr(data.fetcher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(data.snapshot, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(engine.pipeline, "validate_market_analysis_quality", lambda *_args, **_kwargs: quality)

    captured = {}

    def analyze(*_args, **kwargs):
        events.append("analysis")
        captured.update(kwargs)
        return SimpleNamespace(
            scores=scores,
            dcf_results={"000001": {"dcf_points": {}, "params": {}}},
            dcf_attempted=1,
            dcf_skipped=0,
            dcf_skip_reasons={},
            issues=[],
            quality=quality,
        )

    monkeypatch.setattr(engine.pipeline, "run_market_analysis", analyze)
    monkeypatch.setattr(
        data.snapshot,
        "save_market_snapshot",
        lambda *_args, **_kwargs: events.append("promotion"),
    )

    assert _run_full_analysis(force_refresh=True) is True
    assert events == ["analysis", "promotion"]
    from engine.dcf import ReportingPeriodContract

    assert isinstance(captured["reporting_period_contract"], ReportingPeriodContract)
    assert captured["eligible_codes"] == ("000001",)
    assert state["buy_types_df"].loc[0, "code"] == "000001"
    assert state["leaders_df"].loc[0, "code"] == "000001"
    assert state["buy_types_analysis_quality"]["score_coverage"] == 1.0
    assert "000001" in state["buy_types_dcf_results"]
    assert isinstance(state["buy_types_dcf_audit_frame"], pd.DataFrame)
    assert isinstance(state["buy_types_dcf_audit_csv"], bytes)
    assert isinstance(state["buy_types_analysis_json"], bytes)
    exported = json.loads(state["buy_types_analysis_json"])
    assert exported["generated_at"] == state["buy_types_timestamp"]
    assert exported["scores"][0]["code"] == "000001"


def test_analysis_quality_failure_blocks_network_promotion(monkeypatch):
    import data.fetcher
    import data.snapshot
    import engine.pipeline

    previous = pd.DataFrame([{"code": "old"}])
    state = {"buy_types_df": previous, "leaders_df": previous}
    _patch_analysis_ui(monkeypatch, state)
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}]),
        financials={"000001": {}},
        source="network",
        data_timestamp=1_000.0,
        warning="",
        validation=_strict_ttm_validation(),
    )
    monkeypatch.setattr(data.fetcher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(data.snapshot, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        engine.pipeline,
        "validate_market_analysis_quality",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("analysis quality gate failed")),
    )
    monkeypatch.setattr(
        engine.pipeline,
        "run_market_analysis",
        lambda *_args, **_kwargs: SimpleNamespace(
            scores=pd.DataFrame([{"code": "000001"}]),
            dcf_results={},
            dcf_attempted=1,
            dcf_skipped=1,
            issues=[],
        ),
    )
    saves = []
    monkeypatch.setattr(data.snapshot, "save_market_snapshot", lambda *_args, **_kwargs: saves.append(True))

    assert _run_full_analysis(force_refresh=True) is False
    assert state["buy_types_df"] is previous
    assert saves == []


def test_post_analysis_state_construction_failure_blocks_network_promotion(monkeypatch):
    import data.fetcher
    import data.snapshot
    import engine.pipeline

    previous = pd.DataFrame([{"code": "old"}])
    state = {"buy_types_df": previous, "leaders_df": previous}
    _patch_analysis_ui(monkeypatch, state)
    snapshot = SimpleNamespace(
        quotes=pd.DataFrame([{"code": "000001"}]),
        financials={"000001": {}},
        source="network",
        data_timestamp=1_000.0,
        warning="",
        validation=_strict_ttm_validation(),
    )
    monkeypatch.setattr(data.fetcher, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(data.snapshot, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        engine.pipeline,
        "validate_market_analysis_quality",
        lambda *_args, **_kwargs: {
            "ok": True,
            "expected_companies": 1,
            "score_rows": 1,
            "score_coverage": 1.0,
            "dcf_attempted": 1,
            "dcf_attempt_coverage": 1.0,
            "dcf_valid": 1,
            "dcf_valid_coverage": 1.0,
            "pipeline_issue_rate": 0.0,
        },
    )
    monkeypatch.setattr(
        engine.pipeline,
        "run_market_analysis",
        lambda *_args, **_kwargs: SimpleNamespace(
            scores=pd.DataFrame([{"code": "000001"}]),
            dcf_results={"000001": {}},
            dcf_attempted=1,
            dcf_skipped=0,
            dcf_skip_reasons={},
            issues=[],
        ),
    )
    monkeypatch.setattr(
        buy_types_page,
        "_build_successful_analysis_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state build failed")),
    )
    saves = []
    monkeypatch.setattr(data.snapshot, "save_market_snapshot", lambda *_args, **_kwargs: saves.append(True))

    assert _run_full_analysis(force_refresh=True) is False
    assert state["buy_types_df"] is previous
    assert saves == []
