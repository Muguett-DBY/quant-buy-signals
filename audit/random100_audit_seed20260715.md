# 固定随机 100 家公司审计

- seed: `20260715`
- sample_size: `100`
- eligible_universe_size: `4987`
- data_timestamp_utc: `2026-07-26T19:30:19.418835+00:00`
- dcf_valid: `34`
- dcf_skipped_with_reason: `66`
- pipeline_issues: `0`
- engine_self_check_errors: `0`
- same_source_scoring_replay_errors: `0`
- same_source_valuation_replay_errors: `0`
- independent_check_errors: `0`
- triggered_by_type: `{'type1': 1, 'type2': 2, 'type3': 0, 'type4': 0, 'type5': 1, 'type6': 0, 'type7': 0}`
- snapshot_content_sha256: `8f8524c263edebd4421f1b0dfc5f65f1eb9dae35829a9f836050df72115de4c3`
- snapshot_artifact_sha256: `3FAA7C1523099E651A2045B14BD59E24CFA2679B11E05B25DB3A5D7C4F72CD77`
- code_sha256: `e7d69d6f05b838b7bbbb29577abd417d58c3f6c1967f822e643c49ebfe7c78d4`
- rules_sha256: `f3db36120faabde0813230a13d192703db92fc0e4e4dcc7905d78343e9eb6dca`
- dependency_manifest_sha256: `ce9e42dd9b32345e611e8dc6cf36b88958ca0648fbb89290af36fb73597a5802`
- industry_sha256: `e5a71f39c525a1c3c7e7bd947354dad357d5f33e863b5a6f682271dfa9a1e631`
- patch6_source: `{'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}`
- type7_source_documents: `{'template1': {'path_at_model_authoring': 'E:\\模板汇总MD\\第1模板.md', 'sha256': '98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d'}, 'template5': {'path_at_model_authoring': 'E:\\模板汇总MD\\第5模板.md', 'sha256': '37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946'}, 'patch5': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁5.md', 'sha256': '8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4'}, 'patch6': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}}`
- risk_parameter_sources: `{'model_as_of': '2026-07-15', 'risk_free_rate_as_of': '2026-07-15', 'risk_free_rate_source': 'ChinaBond China Government Bond Yield Curve 10Y', 'risk_free_rate_source_url': 'https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN', 'equity_risk_premium_as_of': '2026-04-01', 'equity_risk_premium_basis': 'china_rating_based_total_erp', 'equity_risk_premium_source_url': 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx', 'ctrypremApr26.xlsx': '2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e', 'industry_data_as_of': '2026-01-05', 'betaChina.xls': 'ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9', 'waccChina.xls': '525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6'}`
- scoring_verification_scope: `{'same_source_replay': 'recomputes every published field from reordered production inputs', 'same_source_valuation_replay': 'recomputes valuation existence, payloads, skip reasons and sampled issues', 'independent_runtime_checks': 'recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding', 'business_rule_oracle': 'fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py'}`
- git: `{'commit': '578f1cba5ffe8e39138b4108c300f62368dc4c55', 'dirty': False}`

## 公司明细

| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |
|---|---|---|---|---|---:|---|---|---|
| 000628 | 高新发展 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 3.0 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.7倍 |
| 000717 | 中南股份 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5c 4.0分:资产负债表稳健1项；5b 6.0分:PB45%/0.78;冷7;利10；5d 6.0分:历史利润振幅67.4倍 |
| 000719 | 中原传媒 | MEDIA | 无触发（不买） | 1️⃣ 估值买入区 | 5.3 |  | 有效 | _veto 1.0分:买入区深度不足；_condition 1.0分:须进入模型买入区；1d 1.0分:最新报告期经营现金流为负 |
| 000830 | 鲁西化工 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.7 |  | 跳过:nonpositive_pessimistic_equity_value | 5e 5.0分:10年均利PE12.2倍；5c 6.0分:资产负债表稳健2项；5d 6.0分:历史利润振幅18.3倍 |
| 001260 | 坤泰股份 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 5.3分:产业聚合增速6.1% |
| 001285 | 瑞立科密 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.3分:产业聚合增速6.0% |
| 001301 | 尚太科技 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _veto 2.78分:产业与公司热度平均须>4；2a 2.78分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 001311 | 多利科技 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:年度利润明显下滑；2a 5.31分:产业聚合增速6.1%；2c 8.0分:量价冷度;60日-38.1%… |
| 001360 | 南矿集团 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径利润明显下滑；2a 6.01分:产业聚合增速8.5%；2d 7.82分:当前PB/行业0.7倍 |
| 002088 | 鲁阳节能 | BUILDING_MATERIAL | 5️⃣ 强周期底部 | 5️⃣ 强周期底部 | 7.5 | type5 | 有效 | 5d 6.0分:历史利润振幅13.6倍；5a 7.0分:大宗行业/毛利/利润周期；5e 7.0分:10年均利PE10.3倍 |
| 002171 | 楚江新材 | NONFERROUS | 无触发（不买） | 2️⃣ 两热一冷 | 6.4 |  | 跳过:ttm_fcff_nonpositive | 2b 3.5分:拐点1项:最新同口径营收增；2a 6.5分:产业聚合增速10.2%；2c 7.7分:量价冷度;60日-22.7%… |
| 002180 | 奔图科技 | SOFTWARE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径营收降幅≥50%；6b 4.2分:研发与经营数据 |
| 002192 | 融捷股份 | NONFERROUS | 无触发（不买） | 5️⃣ 强周期底部 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 5e 1.0分:10年均利PE51.2倍；5b 3.5分:PB41%/4.11;冷1;利8；5d 6.0分:历史利润振幅459.9倍 |
| 002268 | 电科网安 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.85分:产业聚合增速8.0% |
| 002298 | 中电鑫龙 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点证据不足；2d 5.41分:当前PB/行业1.1倍 |
| 002335 | 科华数据 | ELEC_EQUIP | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.0 | type2 | 有效 | 2d 5.12分:当前PB/行业1.2倍；2a 5.48分:产业聚合增速6.7%；2c 7.6分:量价冷度;60日-31.9%… |
| 002337 | 赛象科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:拐点2项:最新同口径营收增+最新同口径…；2a 6.01分:产业聚合增速8.5%；2c 8.0分:量价冷度;60日-32.1%… |
| 002386 | 天原股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.87分:产业与公司热度平均须>4；2a 2.87分:产业聚合增速1.5%；2b 3.5分:拐点2项:最新同口径营收增+最新同口径… |
| 002482 | 广田集团 | CONSTRUCTION | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 002522 | 浙江众成 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.1 |  | 有效 | 5e 1.0分:10年均利PE39.0倍；5d 5.0分:历史利润振幅4.9倍；5b 6.0分:PB18%/1.69;冷8;毛10 |
| 002545 | 东方铁塔 | STEEL | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | _veto 0.95分:产业与公司热度平均须>4；2a 0.95分:产业聚合增速-8.2%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶 |
| 002555 | 三七互娱 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 5.3 |  | 有效 | _condition 4.53分:补全全部缺失证据后仍至少一套不超过70；7b 4.53分:产业质量估值45.32；7a 5.29分:长期质量回报52.85 |
| 002607 | 中公教育 | TOURISM_EDU | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.72分:补全全部缺失证据后仍至少一套不超过70；7a 3.72分:长期质量回报37.18；7c 3.72分:商业安全37.19；边际7.3 |
| 002661 | 克明食品 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.84分:产业与公司热度平均须>4；2a 1.84分:产业聚合增速-1.3%；2b 2.0分:最新同口径利润恶化 |
| 002805 | 丰元股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5%；6d 5.0分:最新同口径利润改善 |
| 002849 | 威星智能 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 有效 | _condition 4.0分:须确认实际仓位符合建议上限；6d 4.0分:最新同口径利润同比下降；6b 4.7分:研发与经营数据 |
| 002865 | 钧达股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _veto 1.47分:产业与公司热度平均须>4；_condition 1.47分:估值须合理或满足强周期修正；2a 1.47分:产业聚合增速-4.3% |
| 002876 | 三利谱 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:年度利润明显下滑；2c 5.9分:量价冷度;60日-12.6%…；2a 7.5分:产业聚合增速16.3% |
| 002897 | 意华股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _veto 1.44分:产业与公司热度平均须>4；2a 1.44分:产业聚合增速-4.5%；2b 4.0分:最新同口径利润明显下滑 |
| 002915 | 中欣氟材 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负 |
| 002969 | 嘉美包装 | LIGHT_MFG | 无触发（不买） | 1️⃣ 估值买入区 | 3.9 |  | 有效 | _veto 0.7分:买入区深度不足；_condition 0.7分:须进入模型买入区；1c 0.7分:FCF1.1%;末1.24亿 |
| 003002 | 壶化股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.81分:产业与公司热度平均须>4；2a 2.81分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 300032 | 金龙机电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2d 5.43分:当前PB/行业1.1倍；2a 7.5分:产业聚合增速16.4% |
| 300111 | 向日葵 | CHEM_PHARMA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 300150 | 世纪瑞尔 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2a 7.5分:产业聚合增速16.3%；2c 8.0分:量价冷度;60日-27.7%… |
| 300300 | 海峡创新 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业5.4倍；2b 4.0分:最新同口径经营现金流明显下滑 |
| 300345 | 华民股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 3.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:仅0项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5% |
| 300417 | 南华仪器 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径经营现金流同比下降；6b 5.8分:研发与经营数据 |
| 300420 | 五洋自控 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 2.2分:市场周期不够冷；2c 2.2分:量价冷度;60日70.6%…；2b 3.5分:拐点1项:现金流支撑 |
| 300443 | 金雷股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 6.1 |  | 跳过:ttm_fcff_nonpositive_normalised | 2a 1.44分:产业聚合增速-4.5%；2b 7.0分:拐点3项:营收加速+最新同口径营收增；2c 7.4分:量价冷度;60日-32.5%… |
| 300590 | 移为通信 | TELECOM | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:年度利润暴跌；2a 4.2分:产业聚合增速3.7% |
| 300652 | 雷迪克 | AUTO_VEHICLE | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _condition 4.14分:补全全部缺失证据后仍至少一套不超过70；7b 4.14分:产业质量估值41.41；7c 4.88分:商业安全48.85；边际5.9 |
| 300680 | 隆盛科技 | AUTO_VEHICLE | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.1 | type2 | 跳过:nonpositive_pessimistic_equity_value | 2a 5.68分:产业聚合增速7.4%；2b 6.0分:拐点2项:利润连升+现金流支撑；2c 8.0分:量价冷度;60日-32.2%… |
| 300729 | 乐歌股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 2.94分:产业聚合增速1.6% |
| 300795 | 米奥会展 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 有效 | _condition 4.12分:补全全部缺失证据后仍至少一套不超过70；7b 4.12分:产业质量估值41.22；7a 4.84分:长期质量回报48.38 |
| 300957 | 贝泰妮 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | 2a 3.09分:产业聚合增速1.8%；2d 5.15分:当前PB/行业1.2倍；2b 6.0分:拐点3项:现金流支撑+最新同口径营收增 |
| 300964 | 本川智能 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流转负；6b 3.9分:研发与经营数据 |
| 301004 | 嘉益股份 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 5.4 |  | 有效 | _condition 4.28分:补全全部缺失证据后仍至少一套不超过70；7b 4.28分:产业质量估值42.77；7a 5.87分:长期质量回报58.73 |
| 301072 | 中捷精工 | AUTO_VEHICLE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.5 |  | 跳过:ttm_fcff_nonpositive_normalised | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速7.4% |
| 301080 | 百普赛斯 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG25.8；2a 2.78分:产业聚合增速1.3% |
| 301087 | 可孚医疗 | MEDICAL_SERVICE | 无触发（不买） | 7️⃣ 优质股权型 | 4.7 |  | 有效 | _condition 4.38分:补全全部缺失证据后仍至少一套不超过70；7b 4.38分:产业质量估值43.76；7a 4.78分:长期质量回报47.77 |
| 301097 | 天益医疗 | MEDICAL_SERVICE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.3%；6d 4.0分:最新同口径经营现金流同比下降 |
| 301395 | 仁信新材 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点证据不足；2a 2.81分:产业聚合增速1.3% |
| 301413 | 安培龙 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG9.0；2b 2.0分:最新同口径利润恶化 |
| 301550 | 斯菱智驱 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业5.7倍；2b 3.5分:拐点2项:现金流支撑+最新同口径营收增 |
| 600008 | 首创环保 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 4.1 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.95分:产业与公司热度平均须>4；2a 1.95分:产业聚合增速-0.4%；2b 2.5分:拐点1项:现金流支撑 |
| 600027 | 华电国际 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.92分:产业与公司热度平均须>4；2a 1.92分:产业聚合增速-0.6%；2b 4.0分:最新同口径经营现金流明显下滑 |
| 600094 | 大名城 | REAL_ESTATE | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 0.5分:产业与公司热度平均须>4；2a 0.5分:产业聚合增速-19.1%；2b 2.0分:最新同口径现金恶化 |
| 600108 | 亚盛集团 | AGRICULTURE | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.5分:产业与公司热度平均须>4；2b 2.5分:拐点1项:现金流支撑；2a 3.8分:产业聚合增速3.0% |
| 600160 | 巨化股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 6.9 |  | 跳过:ttm_fcff_nonpositive | 2a 2.76分:产业聚合增速1.3%；2c 4.7分:量价冷度;60日12.1%…；2b 10.0分:拐点4项:净利率连升+利润连升 |
| 600307 | 酒钢宏兴 | STEEL | 无触发（不买） | 2️⃣ 两热一冷 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _veto 0.98分:产业与公司热度平均须>4；_condition 0.98分:估值须合理或满足强周期修正；2a 0.98分:产业聚合增速-8.1% |
| 600351 | 亚宝药业 | TRAD_CN_MED | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 1.62分:产业与公司热度平均须>4；2a 1.62分:产业聚合增速-3.0%；2c 5.6分:量价冷度;60日-3.5%… |
| 600455 | 博通股份 | TOURISM_EDU | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _veto 1.74分:产业与公司热度平均须>4；2a 1.74分:产业聚合增速-2.0%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶 |
| 600639 | 浦东金桥 | REAL_ESTATE | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _veto 0.5分:产业与公司热度平均须>4；2a 0.5分:产业聚合增速-19.3%；2b 2.0分:最新同口径现金恶化 |
| 600665 | 天地源 | REAL_ESTATE | 无触发（不买） | 2️⃣ 两热一冷 | 3.0 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 0.5分:产业与公司热度平均须>4；_condition 0.5分:估值须合理或满足强周期修正；2b 0.5分:拐点证据不足 |
| 600843 | 上工申贝 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径现金恶化；2a 6.01分:产业聚合增速8.5%；2c 8.0分:量价冷度;60日-25.9%… |
| 600862 | 中航高科 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 有效 | 2b 2.5分:拐点1项:现金流支撑；2d 5.35分:当前PB/行业1.1倍；2a 6.02分:产业聚合增速8.6% |
| 600897 | 厦门空港 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 4.36分:产业聚合增速3.9% |
| 600971 | 恒源煤电 | COAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.0 |  | 跳过:nonpositive_pessimistic_equity_value | 5c 4.0分:资产负债表稳健1项；5b 5.7分:PB24%/0.77;冷1;毛10；5d 6.0分:历史利润振幅71.6倍 |
| 600979 | 广安爱众 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 4.0 |  | 跳过:ttm_fcff_nonpositive | _veto 0.5分:产业与公司热度平均须>4；2b 0.5分:拐点证据不足；2a 1.94分:产业聚合增速-0.4% |
| 601116 | 三江购物 | RETAIL | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.17分:产业与公司热度平均须>4；2a 1.17分:产业聚合增速-6.7%；2b 4.0分:最新同口径经营现金流明显下滑 |
| 601226 | 华电科工 | CONST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.1 |  | 跳过:ttm_fcff_nonpositive | _condition 1.7分:估值须合理或满足强周期修正；2d 1.7分:归母利润趋势PEG3.5；2a 5.6分:产业聚合增速7.1% |
| 601898 | 中煤能源 | COAL | 无触发（不买） | 5️⃣ 强周期底部 | 5.8 |  | 有效 | 5b 1.7分:PB72%/1.11;冷1;毛2；5e 5.0分:10年均利PE16.8倍；5d 6.0分:历史利润振幅9.6倍 |
| 601939 | 建设银行 | BANK | 1️⃣ 估值买入区 | 1️⃣ 估值买入区 | 8.4 | type1 | 有效 | 1d 3.0分:金融回归2项；1b 9.0分:银行监管满分4项；1a 9.5分:买入区内折价56% |
| 603014 | 威高血净 | MEDICAL_SERVICE | 无触发（不买） | 7️⃣ 优质股权型 | 4.7 |  | 有效 | _condition 3.96分:补全全部缺失证据后仍至少一套不超过70；7b 3.96分:产业质量估值39.63；7c 4.95分:商业安全49.53；边际9.4 |
| 603028 | 赛福天 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径利润恶化；2d 4.66分:当前PB/行业1.3倍 |
| 603069 | 海汽集团 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.9倍 |
| 603110 | 东方材料 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 5.6 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | 5e 1.0分:10年均利PE97.6倍；5b 4.0分:PB41%/5.41;冷1;利10；5d 5.0分:历史利润振幅4.4倍 |
| 603166 | 福达股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 6.0 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.68分:产业聚合增速7.4% |
| 603278 | 大业股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径利润恶化；2a 6.02分:产业聚合增速8.6%；2c 8.0分:量价冷度;60日-31.1%… |
| 603633 | 徕木股份 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:ttm_fcff_nonpositive | _veto 0.0分:产业与公司热度平均须>4；2b 0.0分:拐点证据不足；2a 5.3分:产业聚合增速6.0% |
| 603696 | 安记食品 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 有效 | _condition 3.63分:补全全部缺失证据后仍至少一套不超过70；7a 3.63分:长期质量回报36.26；7c 3.77分:商业安全37.67；边际8.4 |
| 603786 | 科博达 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 6.1 |  | 有效 | 2b 4.0分:最新同口径利润明显下滑；2a 5.68分:产业聚合增速7.4%；2d 7.64分:归母利润趋势PEG1.2 |
| 603936 | 博敏电子 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径利润恶化；2c 5.6分:量价冷度;60日-13.6%…；2a 7.5分:产业聚合增速16.3% |
| 605008 | 长鸿高科 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.5 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 605117 | 德业股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 有效 | 2a 1.43分:产业聚合增速-4.6%；2c 4.9分:量价冷度;60日-10.8%…；2b 8.0分:拐点4项:利润连升+现金流支撑 |
| 605128 | 上海沿浦 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.68分:产业聚合增速7.4% |
| 605305 | 中际联合 | NEW_ENERGY_VEH | 无触发（不买） | 7️⃣ 优质股权型 | 5.4 |  | 有效 | _condition 4.84分:补全全部缺失证据后仍至少一套不超过70；7b 4.84分:产业质量估值48.39；7c 5.65分:商业安全56.46；边际9.5 |
| 605499 | 东鹏饮料 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 6.0 |  | 有效 | _condition 5.08分:补全全部缺失证据后仍至少一套不超过70；7b 5.08分:产业质量估值50.81；7a 6.41分:长期质量回报64.08 |
| 688090 | 瑞松科技 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.7分:须确认实际仓位符合建议上限；6b 3.7分:研发与经营数据；6d 5.0分:最新同口径利润改善 |
| 688291 | 金橙子 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 跳过:ttm_fcff_nonpositive | 2d 5.15分:当前PB/行业1.2倍；2a 5.83分:产业聚合增速7.9%；2b 6.0分:最新同口径营收下滑,拐点封顶 |
| 688314 | 康拓医疗 | MEDICAL_SERVICE | 无触发（不买） | 7️⃣ 优质股权型 | 5.3 |  | 有效 | _condition 4.51分:补全全部缺失证据后仍至少一套不超过70；7b 4.51分:产业质量估值45.09；7c 5.57分:商业安全55.69；边际10.0 |
| 688403 | 汇成股份 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 2.0分:市场周期不够冷；2b 2.0分:最新同口径利润恶化；2c 2.5分:量价冷度;60日49.1%… |
| 688578 | 艾力斯 | CHEM_PHARMA | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 有效 | _veto 2.36分:市场周期不够冷；2a 2.36分:产业聚合增速0.6%；2c 3.0分:量价冷度;60日31.6%… |
| 688595 | 芯海科技 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.8 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50%；6a 8.0分:产业高速21.3% |
| 688661 | 和林微纳 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.2 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负；6b 5.2分:研发与经营数据 |
| 688678 | 福立旺 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.9 |  | 跳过:ttm_fcff_nonpositive | 2b 5.5分:拐点3项:营收加速+最新同口径营收增；2a 7.5分:产业聚合增速16.3%；2c 7.5分:量价冷度;60日-31.6%… |
| 688697 | 纽威数控 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 跳过:ttm_fcff_nonpositive | _condition 4.2分:补全全部缺失证据后仍至少一套不超过70；7b 4.2分:产业质量估值41.96；7a 5.27分:长期质量回报52.72 |
| 688795 | 摩尔线程 | SEMICONDUCTOR | 无触发（不买） | 7️⃣ 优质股权型 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _condition 4.03分:补全全部缺失证据后仍至少一套不超过70；7c 4.03分:商业安全40.26；边际6.8；7b 4.21分:产业质量估值42.06 |
| 688809 | 强一股份 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 6.1 |  | 跳过:ttm_fcff_nonpositive | 2c 3.6分:量价冷度;60日2.1%…；2b 4.0分:最新同口径经营现金流明显下滑；2a 8.07分:产业聚合增速21.2% |
