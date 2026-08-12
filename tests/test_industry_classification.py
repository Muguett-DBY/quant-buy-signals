from __future__ import annotations

import builtins
import json

import pandas as pd
import pytest

import data.industry as industry


def test_broad_alcohol_cohort_is_not_mislabeled_as_white_liquor_only():
    labels = {code: name for code, name, _keywords in industry._INDUSTRY_RULES}

    assert labels["ALCOHOL"] == "酿酒行业"


def test_batch_industry_classification_matches_single_lookup_and_rejects_duplicate_identity():
    companies = [("600519", "贵州茅台"), ("000001", "平安银行")]

    assert industry.classify_industries(companies) == {
        code: industry.classify_industry(code, name) for code, name in companies
    }
    with pytest.raises(industry.IndustryDataError, match="duplicate"):
        industry.classify_industries([companies[0], companies[0]])


def _set_sources(monkeypatch, tmp_path, f10, mapping):
    f10_path = tmp_path / "f10.json"
    map_path = tmp_path / "map.json"
    capco_path = tmp_path / "capco.json"
    new_listings_path = tmp_path / "new_listings.json"
    f10_path.write_text(json.dumps(f10, ensure_ascii=False), encoding="utf-8")
    map_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    capco_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "authority": "test",
                    "title": "test",
                    "source_url": "https://example.invalid/test.pdf",
                    "effective_period": "test",
                    "published_date": "2026-01-01",
                    "source_sha256": "0" * 64,
                    "record_count": 1,
                },
                "records": {
                    "999998": {
                        "name": "权威占位",
                        "section_code": "I",
                        "section_name": "信息传输、软件和信息技术服务业",
                        "subclass_code": "",
                        "subclass_name": "",
                        "division_code": "65",
                        "division_name": "软件和信息技术服务业",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    new_listings_path.write_text(json.dumps({"schema_version": 1, "records": {}}), encoding="utf-8")
    monkeypatch.setattr(industry, "INDUSTRY_F10_PATH", f10_path)
    monkeypatch.setattr(industry, "INDUSTRY_EM_MAP_PATH", map_path)
    monkeypatch.setattr(industry, "INDUSTRY_CAPCO_PATH", capco_path)
    monkeypatch.setattr(industry, "INDUSTRY_NEW_LISTINGS_PATH", new_listings_path)
    monkeypatch.setattr(industry, "_MIN_CAPCO_RECORDS", 1)
    industry._load_industry_sources_cached.cache_clear()
    return f10_path, map_path


def test_industry_json_generation_is_loaded_once(monkeypatch, tmp_path):
    f10_path, map_path = _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "银行"}},
        {"银行": "BANK"},
    )
    real_open = builtins.open
    opened = []

    def counting_open(path, *args, **kwargs):
        if str(path) in {str(f10_path), str(map_path)}:
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    for _ in range(20):
        assert industry.classify_industry("SZ000001", "任意名称") == "BANK"
    assert opened.count(str(f10_path)) == 1
    assert opened.count(str(map_path)) == 1


def test_classification_reuses_generation_without_filesystem_metadata_calls(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "银行"}},
        {"银行": "BANK"},
    )
    real_resolve = industry.Path.resolve
    real_stat = industry.Path.stat
    resolve_calls = []
    stat_calls = []

    def counting_resolve(path, *args, **kwargs):
        resolve_calls.append(str(path))
        return real_resolve(path, *args, **kwargs)

    def counting_stat(path, *args, **kwargs):
        stat_calls.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(industry.Path, "resolve", counting_resolve)
    monkeypatch.setattr(industry.Path, "stat", counting_stat)
    for _ in range(20):
        assert industry.classify_industry("SZ000001", "任意名称") == "BANK"

    assert resolve_calls == []
    assert len(stat_calls) == 4


def test_corrupt_industry_json_is_visible_and_never_silently_falls_back(monkeypatch, tmp_path):
    f10_path = tmp_path / "f10.json"
    map_path = tmp_path / "map.json"
    f10_path.write_text("not-json", encoding="utf-8")
    map_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(industry, "INDUSTRY_F10_PATH", f10_path)
    monkeypatch.setattr(industry, "INDUSTRY_EM_MAP_PATH", map_path)
    industry._load_industry_sources_cached.cache_clear()
    status = industry.industry_data_status()
    assert not status["ok"]
    assert "JSONDecodeError" in status["error"]
    assert status["usable_f10_entries"] == 0
    with pytest.raises(industry.IndustryDataError, match="JSONDecodeError"):
        industry.classify_industry("999999", "某某银行")


def test_single_character_and_board_prefix_false_positives_are_removed(monkeypatch, tmp_path):
    _set_sources(monkeypatch, tmp_path, {"000001": {"sshy": "银行"}}, {"银行": "BANK"})
    assert industry.classify_industry("999991", "奥美森") == "DEFAULT"
    assert industry.classify_industry("999992", "建邦科技") == "DEFAULT"
    assert industry.classify_industry("920999", "未知股份") == "DEFAULT"
    assert industry.classify_industry("300999", "未知股份") == "DEFAULT"
    assert industry.classify_industry("002999", "未知股份") == "DEFAULT"


def test_meaningful_multi_character_fallbacks_and_biopharma_order(monkeypatch, tmp_path):
    _set_sources(monkeypatch, tmp_path, {"000001": {"sshy": "银行"}}, {"银行": "BANK"})
    assert industry.classify_industry("999991", "华夏生物医药") == "BIO_PHARMA"
    assert industry.classify_industry("999990", "华夏生物制药") == "BIO_PHARMA"
    assert industry.classify_industry("999992", "未来水泵股份") == "INDUST_MACHINERY"
    assert industry.classify_industry("999993", "山河农业集团") == "AGRICULTURE"
    assert industry.classify_industry("999994", "银河饮料") == "FOOD_BEV"


def test_f10_mapping_must_point_to_a_known_industry(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "未知外部行业"}},
        {"未知外部行业": "NOT_A_MODEL_INDUSTRY"},
    )
    assert industry.classify_industry("000001", "某某银行") == "BANK"


def test_placeholder_source_industry_does_not_suppress_name_fallback(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "--"}},
        {"--": "DEFAULT", "银行": "BANK"},
    )
    assert industry.classify_industry("000001", "未来银行") == "BANK"


def test_meaningful_default_mapping_remains_conservative(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "综合"}},
        {"综合": "DEFAULT", "银行": "BANK"},
    )
    assert industry.classify_industry("000001", "未来银行") == "DEFAULT"


def test_diversified_finance_requires_specific_csrc_evidence(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {
            "000001": {"sshy": "银行", "zjhy": "金融业-货币金融服务"},
            "000415": {"sshy": "多元金融", "zjhy": "租赁和商务服务业-租赁业"},
            "002423": {"sshy": "多元金融", "zjhy": "金融业-保险业"},
            "600318": {"sshy": "多元金融", "zjhy": "金融业-货币金融服务"},
            "600927": {"sshy": "多元金融", "zjhy": "金融业-资本市场服务"},
            "603123": {"sshy": "多元金融", "zjhy": "批发和零售业-零售业"},
            "600390": {"sshy": "多元金融", "zjhy": "金融业-其他金融业"},
        },
        {"银行": "BANK", "多元金融": "SECURITIES"},
    )

    assert industry.classify_industry("000415", "渤海租赁") == "FINANCIAL_OTHER"
    assert industry.classify_industry("603123", "翠微股份") == "RETAIL"
    assert industry.classify_industry("600390", "五矿资本") == "FINANCIAL_OTHER"
    assert industry.classify_industry("002423", "中粮资本") == "INSURANCE"
    assert industry.classify_industry("600318", "新力金融") == "FINANCIAL_OTHER"
    assert industry.classify_industry("600927", "永安期货") == "SECURITIES"

    status = industry.industry_data_status()
    assert status["usable_f10_entries"] == 7
    assert status["default_f10_entries"] == 0


def test_specific_csrc_refines_known_broad_source_conflicts(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {
            "002607": {"sshy": "文教休闲", "zjhy": "教育-教育"},
            "002482": {"sshy": "装修装饰", "zjhy": "建筑业-建筑装饰、装修和其他建筑业"},
            "300655": {"sshy": "电子化学品", "zjhy": "制造业-化学原料和化学制品制造业"},
        },
        {
            "文教休闲": "LIGHT_MFG",
            "装修装饰": "BUILDING_MATERIAL",
            "电子化学品": "ELEC_COMPONENT",
        },
    )

    assert industry.classify_industry("002607", "中公教育") == "TOURISM_EDU"
    assert industry.classify_industry("002482", "广田集团") == "CONSTRUCTION"
    assert industry.classify_industry("300655", "晶瑞电材") == "CHEMICAL"


def test_placeholder_source_uses_specific_csrc_before_company_name(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {
            "688816": {"sshy": "--", "zjhy": "制造业-仪器仪表制造业"},
            "603334": {"sshy": "--", "zjhy": "制造业-废弃资源综合利用业"},
            "301633": {"sshy": "--", "zjhy": "信息传输、软件和信息技术服务业-软件和信息技术服务业"},
        },
        {"--": "DEFAULT"},
    )

    assert industry.classify_industry("688816", "易思维") == "INDUST_MACHINERY"
    assert industry.classify_industry("603334", "丰倍生物") == "POWER_UTILITY"
    assert industry.classify_industry("301633", "港迪技术") == "SOFTWARE"


def test_industry_files_hot_reload_when_generation_changes(monkeypatch, tmp_path):
    _f10_path, map_path = _set_sources(
        monkeypatch,
        tmp_path,
        {"000001": {"sshy": "银行"}},
        {"银行": "BANK"},
    )
    assert industry.classify_industry("000001", "未知") == "BANK"

    map_path.write_text(json.dumps({"银行": "INSURANCE"}), encoding="utf-8")
    industry.begin_industry_generation()

    assert industry.classify_industry("000001", "未知") == "INSURANCE"


def test_industry_status_excludes_bj_from_supported_market_coverage_gate(monkeypatch, tmp_path):
    _set_sources(
        monkeypatch,
        tmp_path,
        {"600001": {"sshy": "银行"}},
        {"银行": "BANK"},
    )
    quotes = pd.DataFrame(
        [
            {"code": "600001", "name": "已映射", "market": "SH"},
            {"code": "920001", "name": "未知股份", "market": "BJ"},
        ]
    )

    status = industry.industry_data_status(quotes)

    assert status["loader_ok"]
    assert status["quote_universe"] == 2
    assert status["market_coverage"]["SH"]["confidence"] == "high"
    assert status["market_coverage"]["BJ"]["confidence"] == "low"
    assert status["coverage_ok"]


def test_production_authoritative_sources_cover_known_f10_gap_and_post_period_listings():
    industry.reload_industry_data()
    expected = {
        "000158": "SOFTWARE",
        "000620": "REAL_ESTATE",
        "603698": "INDUST_MACHINERY",
        "301277": "CHEM_PHARMA",
        "301398": "INDUST_MACHINERY",
        "001220": "TRANSPORT",
        "001396": "PROFESSIONAL_SERVICES",
        "301449": "PROFESSIONAL_SERVICES",
        "688712": "INDUST_MACHINERY",
        "001232": "ELEC_COMPONENT",
        "301677": "INDUST_MACHINERY",
        "301707": "SEMICONDUCTOR",
        "301717": "ELEC_COMPONENT",
        "603468": "INDUST_MACHINERY",
        "688825": "SEMICONDUCTOR",
        "688828": "INDUST_MACHINERY",
    }
    quotes = pd.DataFrame(
        [{"code": code, "name": "source-bound", "market": "SH" if code.startswith("6") else "SZ"} for code in expected]
    )

    assert {code: industry.classify_industry(code, "不可用于名称猜测") for code in expected} == expected
    status = industry.industry_data_status(quotes)
    assert status["coverage_ok"]
    assert status["source_bound_coverage"] == 1.0
    assert status["authoritative_coverage"] == 1.0


def test_growth_blending_never_turns_flat_or_declining_company_positive(monkeypatch):
    monkeypatch.setitem(
        industry.INDUSTRY_BENCHMARKS,
        "TEST_GROWTH",
        {
            "pessimistic_floor": -0.15,
            "neutral_benchmark": 0.20,
            "optimistic_ceiling": 0.25,
            "fcf_margin_target": 0.04,
        },
    )

    flat = industry.blend_scenario_growth(
        {"pessimistic": -0.03, "neutral": 0.0, "optimistic": 0.03},
        "TEST_GROWTH",
    )
    declining = industry.blend_scenario_growth(
        {"pessimistic": -0.15, "neutral": -0.10, "optimistic": -0.05},
        "TEST_GROWTH",
    )

    assert flat["neutral"] == 0.0
    assert declining["neutral"] == -0.10
    assert declining["optimistic"] == -0.05


def test_pessimistic_industry_blend_has_one_consistent_global_floor(monkeypatch):
    monkeypatch.setitem(
        industry.INDUSTRY_BENCHMARKS,
        "TEST_DOWNTURN",
        {
            "pessimistic_floor": -0.20,
            "neutral_benchmark": 0.05,
            "optimistic_ceiling": 0.10,
            "fcf_margin_target": 0.04,
        },
    )

    result = industry.blend_scenario_growth(
        {"pessimistic": -0.05, "neutral": 0.02, "optimistic": 0.05},
        "TEST_DOWNTURN",
    )

    assert result["pessimistic"] == -0.15
