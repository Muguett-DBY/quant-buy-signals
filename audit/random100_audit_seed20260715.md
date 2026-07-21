# 固定随机 100 家公司审计

- seed: `20260715`
- sample_size: `100`
- eligible_universe_size: `4988`
- data_timestamp_utc: `2026-07-20T11:00:58.096507+00:00`
- dcf_valid: `35`
- dcf_skipped_with_reason: `65`
- pipeline_issues: `0`
- engine_self_check_errors: `0`
- same_source_scoring_replay_errors: `0`
- same_source_valuation_replay_errors: `0`
- independent_check_errors: `0`
- triggered_by_type: `{'type1': 0, 'type2': 5, 'type3': 0, 'type4': 0, 'type5': 0, 'type6': 0, 'type7': 0}`
- snapshot_content_sha256: `33db229fd81aab91f4e05a92fdce0676a961130155e7cb3d6b67d9cd58d96aee`
- snapshot_artifact_sha256: `DDD63A6DB7380298CB5B5C5F7285013AFA967564B01FBD220F3352E6804DB9A1`
- code_sha256: `af61e2bb44968164bbe4aa73e0861d6ce445ae56c37ebe5dfe9e2a662fef775c`
- rules_sha256: `15356ae1a44481d1c306eefd74089bd6b2c81c068c74c35a4ea9044617c7d6f0`
- dependency_manifest_sha256: `cca31e524fa93a1879fe3bd281a8c7b900a5861c16c46ce0a44205f7ed8a6cc6`
- industry_sha256: `e5a71f39c525a1c3c7e7bd947354dad357d5f33e863b5a6f682271dfa9a1e631`
- patch6_source: `{'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}`
- type7_source_documents: `{'template1': {'path_at_model_authoring': 'E:\\模板汇总MD\\第1模板.md', 'sha256': '98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d'}, 'template5': {'path_at_model_authoring': 'E:\\模板汇总MD\\第5模板.md', 'sha256': '37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946'}, 'patch5': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁5.md', 'sha256': '8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4'}, 'patch6': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}}`
- risk_parameter_sources: `{'model_as_of': '2026-07-15', 'risk_free_rate_as_of': '2026-07-15', 'risk_free_rate_source': 'ChinaBond China Government Bond Yield Curve 10Y', 'risk_free_rate_source_url': 'https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN', 'equity_risk_premium_as_of': '2026-04-01', 'equity_risk_premium_basis': 'china_rating_based_total_erp', 'equity_risk_premium_source_url': 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx', 'ctrypremApr26.xlsx': '2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e', 'industry_data_as_of': '2026-01-05', 'betaChina.xls': 'ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9', 'waccChina.xls': '525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6'}`
- scoring_verification_scope: `{'same_source_replay': 'recomputes every published field from reordered production inputs', 'same_source_valuation_replay': 'recomputes valuation existence, payloads, skip reasons and sampled issues', 'independent_runtime_checks': 'recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding', 'business_rule_oracle': 'fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py'}`
- git: `{'commit': '129aecfd77c423286d3962bbe5ac879d6a7d7425', 'dirty': False}`

## 公司明细

| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |
|---|---|---|---|---|---:|---|---|---|
| 000628 | 高新发展 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 2.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.7倍 |
| 000717 | 中南股份 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5c 4.0分:资产负债表稳健1项；5b 6.0分:PB48%/0.78;冷7;利10；5d 6.0分:历史利润振幅67.4倍 |
| 000719 | 中原传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 3.53分:产业与公司热度平均须>4；2a 3.53分:产业聚合增速2.6%；2b 4.0分:拐点1项:现金流支撑 |
| 000830 | 鲁西化工 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.7 |  | 跳过:nonpositive_pessimistic_equity_value | 5e 5.0分:10年均利PE12.1倍；5c 6.0分:资产负债表稳健2项；5d 6.0分:历史利润振幅18.3倍 |
| 001260 | 坤泰股份 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 5.3分:产业聚合增速6.1% |
| 001285 | 瑞立科密 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.3分:产业聚合增速6.0% |
| 001301 | 尚太科技 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.78分:产业与公司热度平均须>4；2a 2.78分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 001311 | 多利科技 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:年度利润明显下滑；2a 5.31分:产业聚合增速6.1%；2c 8.0分:量价冷度;60日-38.9%;YTD-3 |
| 001360 | 南矿集团 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径利润明显下滑；2a 6.01分:产业聚合增速8.5%；2c 7.8分:量价冷度;60日-38.2%;YTD-3 |
| 002086 | 东方海洋 | AGRICULTURE | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 跳过:ttm_fcff_nonpositive | 2a 3.8分:产业聚合增速3.0%；2b 5.0分:拐点2项:现金流支撑+最新同口径利润增；2d 5.5分:当前PB/行业1.1倍 |
| 002170 | 芭田股份 | CHEMICAL | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.3 | type2 | 有效 | 2a 2.79分:产业聚合增速1.3%；2c 6.5分:量价冷度;60日-18.8%;YTD-8；2b 10.0分:拐点6项:营收加速+净利率连升 |
| 002179 | 中航光电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:拐点1项:现金流支撑；2c 6.5分:量价冷度;60日-10.1%;YTD-7；2d 7.41分:当前PB/行业0.8倍 |
| 002191 | 劲嘉股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 有效 | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点1项:最新同口径营收增；2a 3.13分:产业聚合增速1.9% |
| 002268 | 电科网安 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.85分:产业聚合增速8.0% |
| 002298 | 中电鑫龙 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点证据不足；2a 5.47分:产业聚合增速6.7% |
| 002335 | 科华数据 | ELEC_EQUIP | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.0 | type2 | 有效 | 2d 5.0分:当前PB/行业1.2倍；2a 5.48分:产业聚合增速6.7%；2c 7.6分:量价冷度;60日-36.4%;YTD-2 |
| 002337 | 赛象科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:拐点2项:最新同口径营收增+最新同口径利；2a 6.01分:产业聚合增速8.5%；2d 7.86分:当前PB/行业0.7倍 |
| 002386 | 天原股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.87分:产业与公司热度平均须>4；2a 2.87分:产业聚合增速1.5%；2b 3.5分:拐点2项:最新同口径营收增+最新同口径利 |
| 002482 | 广田集团 | CONSTRUCTION | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 002522 | 浙江众成 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.1 |  | 有效 | 5e 1.0分:10年均利PE40.1倍；5d 5.0分:历史利润振幅4.9倍；5b 6.0分:PB20%/1.74;冷7;毛10 |
| 002545 | 东方铁塔 | STEEL | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 有效 | _veto 0.95分:产业与公司热度平均须>4；2a 0.95分:产业聚合增速-8.2%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶 |
| 002555 | 三七互娱 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 5.3 |  | 有效 | _condition 4.54分:补全全部缺失证据后仍至少一套不超过70；7b 4.54分:第5模板45.38；7a 5.29分:第1模板52.92 |
| 002607 | 中公教育 | TOURISM_EDU | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.72分:补全全部缺失证据后仍至少一套不超过70；7a 3.72分:第1模板37.18；7b 3.79分:第5模板37.93 |
| 002661 | 克明食品 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.84分:产业与公司热度平均须>4；2a 1.84分:产业聚合增速-1.3%；2b 2.0分:最新同口径利润恶化 |
| 002805 | 丰元股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5%；6d 5.0分:最新同口径利润改善 |
| 002849 | 威星智能 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 有效 | _condition 4.0分:须确认实际仓位符合建议上限；6d 4.0分:最新同口径利润同比下降；6b 4.7分:研发与经营数据 |
| 002865 | 钧达股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 3.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.47分:产业与公司热度平均须>4；_condition 1.47分:估值须合理或满足强周期修正；2a 1.47分:产业聚合增速-4.3% |
| 002876 | 三利谱 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.4 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:年度利润明显下滑；2c 6.1分:量价冷度;60日-18.4%;YTD1.；2a 7.49分:产业聚合增速16.3% |
| 002897 | 意华股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _veto 1.44分:产业与公司热度平均须>4；2a 1.44分:产业聚合增速-4.5%；2b 4.0分:最新同口径利润明显下滑 |
| 002915 | 中欣氟材 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负 |
| 002969 | 嘉美包装 | LIGHT_MFG | 无触发（不买） | 1️⃣ 估值买入区 | 3.9 |  | 有效 | _veto 0.7分:买入区深度不足；_condition 0.7分:须进入模型买入区；1c 0.7分:FCF1.1%;末1.24亿 |
| 003002 | 壶化股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 有效 | _veto 2.81分:产业与公司热度平均须>4；2a 2.81分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 300032 | 金龙机电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2d 5.39分:当前PB/行业1.1倍；2a 7.5分:产业聚合增速16.3% |
| 300111 | 向日葵 | CHEM_PHARMA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 300150 | 世纪瑞尔 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2a 7.49分:产业聚合增速16.3%；2c 8.0分:量价冷度;60日-30.7%;YTD-2 |
| 300299 | 富春股份 | MEDIA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速2.5%；6d 6.0分:利润改善43.9% |
| 300343 | 联创股份 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50% |
| 300416 | 苏试试验 | PROFESSIONAL_SERVICES | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.58分:产业聚合增速7.0% |
| 300418 | 昆仑万维 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _condition 4.39分:估值须合理或满足强周期修正；2d 4.39分:当前PB/行业1.4倍；2a 5.79分:产业聚合增速7.8% |
| 300442 | 润泽科技 | SOFTWARE | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 8.0 | type2 | 跳过:ttm_fcff_nonpositive | 2a 5.82分:产业聚合增速7.9%；2c 6.3分:量价冷度;60日-25.8%;YTD26；2b 10.0分:拐点5项:营收加速+净利率连升 |
| 300589 | 江龙船艇 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _veto 4.0分:仅1项核心证据≥5；_condition 4.0分:须确认实际仓位符合建议上限；6d 4.0分:最新经营现金流仍负但改善 |
| 300651 | 金陵体育 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 6.5 |  | 有效 | 2a 3.09分:产业聚合增速1.8%；2d 5.82分:当前PB/行业1.0倍；2c 7.4分:量价冷度;60日-47.9%;YTD-2 |
| 300679 | 电连技术 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20%；6a 6.0分:产业增速16.2% |
| 300727 | 润禾材料 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 6.4 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 2.8分:估值须合理或满足强周期修正；2a 2.8分:产业聚合增速1.3%；2d 3.38分:归母利润趋势PEG2.3 |
| 300793 | 佳禾智能 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:年度利润暴跌；2a 7.49分:产业聚合增速16.3%；2c 7.9分:量价冷度;60日-28.7%;YTD-3 |
| 300955 | 嘉亨家化 | LIGHT_MFG | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.8% |
| 300962 | 中金辐照 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 4.5 |  | 跳过:ttm_fcff_nonpositive | _condition 3.73分:补全全部缺失证据后仍至少一套不超过70；7b 3.73分:第5模板37.33；7a 4.78分:第1模板47.79 |
| 301002 | 崧盛股份 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | _condition 5.0分:估值须合理或满足强周期修正；2d 5.0分:当前PB/行业1.2倍；2a 5.46分:产业聚合增速6.6% |
| 301070 | 开勒股份 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20% |
| 301078 | 孩子王 | RETAIL | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 有效 | _veto 1.16分:产业与公司热度平均须>4；2a 1.16分:产业聚合增速-6.7%；2b 4.0分:最新同口径经营现金流明显下滑 |
| 301085 | 亚康股份 | SOFTWARE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.3 |  | 有效 | _veto 1.7分:仅1项核心证据≥5；_condition 1.7分:须确认实际仓位符合建议上限；6b 1.7分:研发与经营数据 |
| 301095 | 广立微 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 6.6 |  | 跳过:ttm_fcff_nonpositive | 2b 5.5分:拐点3项:营收加速+最新同口径营收增；2c 6.0分:量价冷度;60日-21.1%;YTD-4；2d 7.12分:当前PB/行业0.8倍 |
| 301393 | 昊帆生物 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _condition 2.81分:估值须合理或满足强周期修正；2a 2.81分:产业聚合增速1.3%；2c 4.4分:量价冷度;60日3.2%;YTD15.6 |
| 301408 | 华人健康 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 有效 | 2a 2.75分:产业聚合增速1.3%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶；2c 7.0分:量价冷度;60日-26.0%;YTD-1 |
| 301548 | 崇德科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.7 |  | 有效 | _condition 3.0分:估值须合理或满足强周期修正；2d 3.0分:归母利润趋势PEG2.5；2a 6.01分:产业聚合增速8.5% |
| 600007 | 中国国贸 | REAL_ESTATE | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 有效 | _condition 4.26分:补全全部缺失证据后仍至少一套不超过70；7b 4.26分:第5模板42.57；7a 4.71分:第1模板47.09 |
| 600026 | 中远海能 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 4.37分:估值须合理或满足强周期修正；2a 4.37分:产业聚合增速3.9%；2b 4.5分:拐点3项:现金流支撑+最新同口径营收增 |
| 600089 | 特变电工 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径经营现金流明显下滑；2a 5.66分:产业聚合增速7.3%；2c 7.0分:量价冷度;60日-29.4%;YTD-1 |
| 600106 | 重庆路桥 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点证据不足；2a 4.36分:产业聚合增速3.9% |
| 600159 | 大龙地产 | REAL_ESTATE | 无触发（不买） | 2️⃣ 两热一冷 | 3.0 |  | 跳过:ttm_fcff_nonpositive | _veto 0.0分:产业与公司热度平均须>4；2b 0.0分:拐点证据不足；2a 0.5分:产业聚合增速-19.2% |
| 600305 | 恒顺醋业 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _condition 1.83分:估值须合理或满足强周期修正；2a 1.83分:产业聚合增速-1.3%；2d 2.9分:归母利润趋势PEG2.6 |
| 600350 | 山东高速 | TRANSPORT | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.38分:补全全部缺失证据后仍至少一套不超过70；7b 3.38分:第5模板33.76；7c 4.12分:补丁541.22；安全边际6.1 |
| 600452 | 涪陵电力 | POWER_UTILITY | 无触发（不买） | 1️⃣ 估值买入区 | 4.6 |  | 有效 | _veto 1.1分:买入区深度不足；_condition 1.1分:须进入模型买入区；1c 1.1分:FCF1.7%;末5.51亿 |
| 600638 | 新黄浦 | REAL_ESTATE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 0.5分:产业与公司热度平均须>4；2a 0.5分:产业聚合增速-19.2%；2b 6.0分:最新同口径营收下滑,拐点封顶 |
| 600664 | 哈药股份 | CHEM_PHARMA | 无触发（不买） | 7️⃣ 优质股权型 | 4.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.63分:补全全部缺失证据后仍至少一套不超过70；7b 3.63分:第5模板36.27；7a 4.08分:第1模板40.80 |
| 600841 | 动力新科 | AUTO_VEHICLE | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.1 | type2 | 有效 | 2a 5.7分:产业聚合增速7.5%；2b 6.5分:拐点3项:利润连升+最新同口径营收增；2c 7.9分:量价冷度;60日-42.9%;YTD-1 |
| 600861 | 北京人力 | BUSINESS_SERVICES | 无触发（不买） | 无可完整诊断框架 |  |  | 有效 |  |
| 600895 | 张江高科 | REAL_ESTATE | 无触发（不买） | 7️⃣ 优质股权型 | 3.4 |  | 跳过:ttm_fcff_nonpositive | _condition 3.11分:补全全部缺失证据后仍至少一套不超过70；7b 3.11分:第5模板31.10；7c 3.31分:补丁533.13；安全边际4.7 |
| 600970 | 中材国际 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 1.37分:产业与公司热度平均须>4；2a 1.37分:产业聚合增速-5.1%；2b 4.0分:最新同口径利润明显下滑 |
| 600977 | 中国电影 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 跳过:ttm_fcff_nonpositive | 2a 3.52分:产业聚合增速2.5%；2b 6.0分:拐点3项:营收加速+现金流支撑；2d 7.01分:当前PB/行业0.8倍 |
| 601113 | 华鼎股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 2.84分:产业聚合增速1.4% |
| 601225 | 陕西煤业 | COAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.3 |  | 有效 | 5b 4.0分:PB73%/2.37;冷1;毛10；5e 5.0分:10年均利PE14.3倍；5d 6.0分:历史利润振幅12.8倍 |
| 601890 | 亚星锚链 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径现金恶化；2a 6.01分:产业聚合增速8.5%；2d 6.11分:归母利润趋势PEG1.5 |
| 601933 | 永辉超市 | RETAIL | 无触发（不买） | 7️⃣ 优质股权型 | 3.2 |  | 跳过:ttm_fcff_nonpositive | _condition 2.82分:补全全部缺失证据后仍至少一套不超过70；7b 2.82分:第5模板28.24；7a 3.1分:第1模板31.04 |
| 603013 | 亚普股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 6.7 |  | 有效 | _condition 2.0分:估值须合理或满足强周期修正；2d 2.0分:归母利润趋势PEG3.3；2a 5.69分:产业聚合增速7.4% |
| 603027 | 千禾味业 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 4.4 |  | 有效 | _condition 3.64分:补全全部缺失证据后仍至少一套不超过70；7b 3.64分:第5模板36.38；7a 4.61分:第1模板46.05 |
| 603068 | 博通集成 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.6 |  | 有效 | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50%；6b 7.2分:研发与经营数据 |
| 603109 | 神驰机电 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径利润恶化；2a 6.01分:产业聚合增速8.5%；2c 8.0分:量价冷度;60日-33.5%;YTD-3 |
| 603165 | 荣晟环保 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点1项:最新同口径营收增；2a 3.09分:产业聚合增速1.8% |
| 603277 | 银都股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2a 6.02分:产业聚合增速8.6%；2d 7.16分:当前PB/行业0.8倍 |
| 603630 | 拉芳家化 | LIGHT_MFG | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 3.0分:仅0项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.8% |
| 603693 | 江苏新能 | POWER_UTILITY | 无触发（不买） | 7️⃣ 优质股权型 | 4.5 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.98分:补全全部缺失证据后仍至少一套不超过70；7b 3.98分:第5模板39.81；7a 4.7分:第1模板46.98 |
| 603779 | 威龙股份 | ALCOHOL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.2% |
| 603933 | 睿能科技 | SEMICONDUCTOR | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.4 | type2 | 跳过:ttm_fcff_nonpositive | 2b 5.5分:拐点3项:营收加速+最新同口径营收增；2c 7.9分:量价冷度;60日-31.7%;YTD-3；2a 8.08分:产业聚合增速21.3% |
| 605007 | 五洲特纸 | LIGHT_MFG | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _veto 2.5分:仅1项核心证据≥5；_condition 2.5分:须确认实际仓位符合建议上限；6b 2.5分:研发与经营数据 |
| 605116 | 奥锐特 | CHEM_PHARMA | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 2.52分:产业与公司热度平均须>4；2a 2.52分:产业聚合增速0.9%；2b 4.0分:最新同口径利润明显下滑 |
| 605123 | 派克新材 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径经营现金流明显下滑；2a 6.02分:产业聚合增速8.6%；2d 7.78分:当前PB/行业0.7倍 |
| 605303 | 园林股份 | CONSTRUCTION | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 3.4 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 605488 | 福莱新材 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.5 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50% |
| 688089 | 嘉必优 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 有效 | _veto 1.83分:产业与公司热度平均须>4；2a 1.83分:产业聚合增速-1.3%；2b 2.0分:最新同口径利润恶化 |
| 688290 | 景业智能 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 2.8分:须确认实际仓位符合建议上限；6c 2.8分:经营效率与现金流数据；6d 3.0分:最新同口径经营现金流降幅≥20% |
| 688313 | 仕佳光子 | TELECOM | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 跳过:ttm_fcff_nonpositive | _condition 4.38分:补全全部缺失证据后仍至少一套不超过70；7b 4.38分:第5模板43.78；7c 5.08分:补丁550.84；安全边际7.8 |
| 688401 | 路维光电 | SEMICONDUCTOR | 无触发（不买） | 7️⃣ 优质股权型 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _condition 4.69分:补全全部缺失证据后仍至少一套不超过70；7b 4.69分:第5模板46.93；7c 5.23分:补丁552.34；安全边际7.2 |
| 688577 | 浙海德曼 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG5.5；2a 6.01分:产业聚合增速8.5% |
| 688593 | 新相微 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.0 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50%；6b 5.5分:研发与经营数据 |
| 688660 | 电气风电 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 3.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.43分:产业与公司热度平均须>4；_condition 1.43分:估值须合理或满足强周期修正；2a 1.43分:产业聚合增速-4.6% |
| 688677 | 海泰新光 | MEDICAL_SERVICE | 无触发（不买） | 7️⃣ 优质股权型 | 5.6 |  | 有效 | _condition 4.77分:补全全部缺失证据后仍至少一套不超过70；7b 4.77分:第5模板47.68；7c 5.95分:补丁559.54；安全边际9.8 |
| 688696 | 极米科技 | HOME_APPLIANCE | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 跳过:ttm_fcff_nonpositive | 2b 3.5分:拐点1项:净利率连升；2a 4.72分:产业聚合增速4.5%；2c 8.0分:量价冷度;60日-30.2%;YTD-4 |
| 688793 | 倍轻松 | HOME_APPLIANCE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 688808 | 联讯仪器 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 5.0 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 4.52分:补全全部缺失证据后仍至少一套不超过70；7b 4.52分:第5模板45.24；7a 5.11分:第1模板51.05 |
