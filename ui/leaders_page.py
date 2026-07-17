"""行业买入候选与个股查询页面。"""

import math

import pandas as pd
import streamlit as st

from ui.buy_types_page import (
    TYPE_DIMENSIONS,
    TYPE_NAMES,
    TYPE_ORDER,
    _bear_case_lines,
    _diagnostic_type_label,
    _display_reason,
    _format_metric,
    _make_narrative,
    _normalise_code,
    _industry_display_name,
    _invalidate_stale_analysis_state,
    _render_analysis_evidence,
    _render_global_status,
    _render_stock_dcf,
    _status_icon,
    _type_status,
    _render_type6_global_notice,
    _type_risk_notice,
    _with_diagnostic_fields,
)


def _display_number(value, digits=1, suffix=""):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "暂无数据"
    if not math.isfinite(number):
        return "暂无数据"
    return f"{number:.{digits}f}{suffix}"


def _display_percent(value):
    try:
        return _display_number(float(value) * 100.0, 1, "%")
    except (TypeError, ValueError):
        return "暂无数据"


def _add_business_candidate_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the best eligible non-VC signal without calling idxmax on all-NA rows."""
    result = frame.copy()
    business_types = ["type1", "type2", "type3", "type4", "type5"]
    eligible_cols = []

    def eligible_score(info):
        if not isinstance(info, dict) or info.get("triggered") is not True or info.get("veto") is True:
            return float("nan")
        try:
            score = float(info.get("total"))
        except (TypeError, ValueError):
            return float("nan")
        return score if math.isfinite(score) else float("nan")

    for type_key in business_types:
        score_col = f"_{type_key}_eligible_score"
        eligible_cols.append(score_col)
        if type_key in result.columns:
            result[score_col] = result[type_key].apply(eligible_score)
        else:
            result[score_col] = float("nan")

    result["business_score"] = result[eligible_cols].max(axis=1, skipna=True)
    result["business_type"] = pd.Series(pd.NA, index=result.index, dtype="string")
    eligible_rows = result["business_score"].notna()
    if eligible_rows.any():
        best_columns = result.loc[eligible_rows, eligible_cols].idxmax(axis=1, skipna=True)
        column_to_type = {f"_{type_key}_eligible_score": type_key for type_key in business_types}
        result.loc[eligible_rows, "business_type"] = best_columns.map(column_to_type)
    return result


def show():
    st.title("🏆 行业买入候选 · 个股查询")

    _invalidate_stale_analysis_state()

    if "buy_types_df" not in st.session_state:
        st.info("👈 请先在「七种买入类型」页面点击「开始分析」加载数据")
        return

    # 行业候选只使用真正触发且未被否决的非 VC 框架。
    base_frame = _with_diagnostic_fields(st.session_state.get("leaders_df", st.session_state["buy_types_df"]))
    df = _add_business_candidate_columns(base_frame)
    _render_global_status(df)
    _render_type6_global_notice()
    _render_analysis_evidence(base_frame)

    # ── TAB 1: Industry Candidates ──
    tab1, tab2 = st.tabs(["🏆 行业买入候选", "🔍 个股查询"])

    with tab1:
        st.caption("每个细分行业最多展示2只已触发且未否决的非VC买入候选；不构成行业地位或投资结论。")

        from data.industry import _INDUSTRY_RULES

        code_to_cn = {c: n for c, n, _ in _INDUSTRY_RULES}
        unclassified_count = int((df["industry"] == "DEFAULT").sum())
        if unclassified_count:
            st.info(
                f"{unclassified_count} 只股票为未分类（低置信度），不进入细分行业候选卡；"
                "仍可在个股查询中查看，其行业依赖评分已保守降级。"
            )
        industries = sorted(industry for industry in df["industry"].dropna().unique() if industry != "DEFAULT")

        cols = st.columns(4)
        for i, ind in enumerate(industries):
            sub = df[(df["industry"] == ind) & df["business_score"].notna()].nlargest(2, "business_score")
            cn_name = _industry_display_name(ind, code_to_cn)
            with cols[i % 4]:
                st.markdown(f"**{cn_name}**")
                if sub.empty:
                    st.caption("暂无非VC买入信号")
                for _, r in sub.iterrows():
                    sc = r.get("business_score", 0)
                    cd = r.get("code", "")
                    nm = r.get("name", "")
                    bt = r.get("buy_types", [])
                    bt_filtered = [t for t in bt if t != "type6"]
                    tag = " ".join([t.replace("type", "") for t in bt_filtered]) if bt_filtered else "-"
                    pe_val = r.get("pe", 0)
                    pe_str = f"PE={_display_number(pe_val, 0)}" if pd.notna(pe_val) and pe_val > 0 else "PE=-"
                    st.write(f"{sc:.1f} | {cd} {nm} | {tag} | {pe_str}")

    # ── TAB 2: Stock Search ──
    with tab2:
        col_code, col_btn, _ = st.columns([1, 0.5, 4])
        with col_code:
            search_code = st.text_input("股票代码", placeholder="如 600519", key="search_code_v2")
        with col_btn:
            search_click = st.button("分析", type="primary", key="search_btn_v2")

        if search_click and search_code:
            code_str = _normalise_code(search_code)
            row_match = df[df["code"].astype(str).str.strip() == code_str]
            if not row_match.empty:
                row = row_match.iloc[0]
                name = row.get("name", "")
                price = row.get("price", 0)
                buy_types = row.get("buy_types", [])

                st.caption(f"**{code_str} {name}**")
                kpi_cols = st.columns(6)
                with kpi_cols[0]:
                    st.metric("股价", _display_number(price, 2))
                with kpi_cols[1]:
                    st.metric("PE", _display_number(row.get("pe"), 1))
                with kpi_cols[2]:
                    st.metric("PB", _display_number(row.get("pb"), 1))
                with kpi_cols[3]:
                    st.metric("ROE", _display_percent(row.get("roe")))
                with kpi_cols[4]:
                    st.metric("负债率", _display_percent(row.get("debt_ratio")))
                with kpi_cols[5]:
                    st.metric("行业", _industry_display_name(row.get("industry", "未知"), code_to_cn))

                if buy_types:
                    labels = " · ".join([TYPE_NAMES.get(t, t) for t in buy_types])
                    st.success(f"🎯 命中: {labels}")
                else:
                    st.info("未命中任何买入类型：不买。")
                diagnostic_label = _diagnostic_type_label(row)
                if diagnostic_label:
                    st.caption(f"最高评分框架（仅用于诊断，不代表买入触发）：{diagnostic_label}")

                bear_lines = _bear_case_lines(row)
                if bear_lines:
                    st.warning("🐻 空头复核——三个最致命漏洞：  \n" + "  \n".join(f"- {line}" for line in bear_lines))

                _render_stock_dcf(code_str)

                for t in TYPE_ORDER:
                    td = row.get(t, {})
                    if not td or not isinstance(td, dict):
                        continue
                    sc = td.get("total", 0)
                    trig = td.get("triggered", False)
                    veto = td.get("veto", False)
                    subs = td.get("sub_scores", {})
                    reasons = td.get("reasons", {})
                    status_code = _type_status(td)
                    dims = TYPE_DIMENSIONS.get(t, [])
                    label = TYPE_NAMES.get(t, t)
                    icon = _status_icon(triggered=trig, veto=veto, status=status_code)
                    score_text = (
                        "不适用"
                        if status_code == "not_applicable"
                        else "证据不足"
                        if status_code == "insufficient_evidence"
                        else f"{_format_metric(sc, digits=2)}分"
                    )
                    with st.expander(f"{icon} {label} — {score_text}", expanded=trig):
                        narrative = _make_narrative(
                            t,
                            sc,
                            subs,
                            reasons,
                            dims,
                            triggered=trig,
                            veto=veto,
                            status=status_code,
                        )
                        if trig and not veto:
                            st.success(narrative)
                        elif status_code == "not_applicable":
                            st.info(narrative)
                        else:
                            st.warning(narrative)
                        risk_notice = _type_risk_notice(t, reasons)
                        if risk_notice:
                            st.error(f"仓位与最大损失约束：{risk_notice}")
                        dim_lines = []
                        for key, name, wt in dims:
                            v = subs.get(key, 0)
                            r = _display_reason(reasons.get(key, ""))
                            value_text = (
                                "不适用"
                                if status_code == "not_applicable"
                                else "证据不足"
                                if status_code == "insufficient_evidence"
                                else f"{_format_metric(v)}分"
                            )
                            dim_lines.append(f"  • {name}({key},权{wt * 100:.0f}%)={value_text} — {r}")
                        st.caption("\n".join(dim_lines))
            else:
                st.warning(f"未找到股票 {code_str}，请检查代码")
        else:
            st.caption("💡 输入股票代码，点击「分析」查看七类型详细评价")
