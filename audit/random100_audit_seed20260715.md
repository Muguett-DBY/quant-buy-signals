# 固定随机 100 家公司审计

- seed: `20260715`
- sample_size: `100`
- eligible_universe_size: `4989`
- data_timestamp_utc: `2026-07-22T20:14:17.763520+00:00`
- dcf_valid: `35`
- dcf_skipped_with_reason: `65`
- pipeline_issues: `0`
- engine_self_check_errors: `0`
- same_source_scoring_replay_errors: `0`
- same_source_valuation_replay_errors: `0`
- independent_check_errors: `0`
- triggered_by_type: `{'type1': 0, 'type2': 3, 'type3': 0, 'type4': 0, 'type5': 1, 'type6': 0, 'type7': 0}`
- snapshot_content_sha256: `c097f54ac70ca141d12089f7650b1a217d6aada6af84ed60653a0348d0f066eb`
- snapshot_artifact_sha256: `B2A983330EC3EF0EFC7AB12AA90D0E53852B65FFA86035166E1E731DD51E7583`
- code_sha256: `aaf9a1e67f4896e9d92e490d3660a75c69a45745ba5de5c4ac663864b0e2e2b2`
- rules_sha256: `a2ccb5812e8922ddcbf2ab076be34e7a3206f9cf049d81cec86ee7128887b783`
- dependency_manifest_sha256: `cdb3206b03327eace4819610c9faa3e0dbd8bb615e091a3f8022d9f748d46199`
- industry_sha256: `e5a71f39c525a1c3c7e7bd947354dad357d5f33e863b5a6f682271dfa9a1e631`
- patch6_source: `{'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}`
- type7_source_documents: `{'template1': {'path_at_model_authoring': 'E:\\模板汇总MD\\第1模板.md', 'sha256': '98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d'}, 'template5': {'path_at_model_authoring': 'E:\\模板汇总MD\\第5模板.md', 'sha256': '37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946'}, 'patch5': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁5.md', 'sha256': '8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4'}, 'patch6': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}}`
- risk_parameter_sources: `{'model_as_of': '2026-07-15', 'risk_free_rate_as_of': '2026-07-15', 'risk_free_rate_source': 'ChinaBond China Government Bond Yield Curve 10Y', 'risk_free_rate_source_url': 'https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN', 'equity_risk_premium_as_of': '2026-04-01', 'equity_risk_premium_basis': 'china_rating_based_total_erp', 'equity_risk_premium_source_url': 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx', 'ctrypremApr26.xlsx': '2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e', 'industry_data_as_of': '2026-01-05', 'betaChina.xls': 'ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9', 'waccChina.xls': '525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6'}`
- scoring_verification_scope: `{'same_source_replay': 'recomputes every published field from reordered production inputs', 'same_source_valuation_replay': 'recomputes valuation existence, payloads, skip reasons and sampled issues', 'independent_runtime_checks': 'recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding', 'business_rule_oracle': 'fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py'}`
- git: `{'commit': '130700902bcbc953ca0e599138b1fda803d29624', 'dirty': False}`

## 公司明细

| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |
|---|---|---|---|---|---:|---|---|---|
| 000628 | 高新发展 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 2.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.8倍 |
| 000717 | 中南股份 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5c 4.0分:资产负债表稳健1项；5b 6.0分:PB46%/0.78;冷7;利10；5d 6.0分:历史利润振幅67.4倍 |
| 000719 | 中原传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 3.53分:产业与公司热度平均须>4；2a 3.53分:产业聚合增速2.6%；2b 4.0分:拐点1项:现金流支撑 |
| 000830 | 鲁西化工 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5e 5.0分:10年均利PE12.4倍；5b 6.0分:PB36%/1.24;冷7;毛10；5c 6.0分:资产负债表稳健2项 |
| 001260 | 坤泰股份 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 5.3分:产业聚合增速6.1% |
| 001285 | 瑞立科密 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.3分:产业聚合增速6.0% |
| 001301 | 尚太科技 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.78分:产业与公司热度平均须>4；2a 2.78分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 001311 | 多利科技 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:年度利润明显下滑；2a 5.31分:产业聚合增速6.1%；2c 8.0分:量价冷度;60日-36.2%;YTD-4 |
| 001360 | 南矿集团 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径利润明显下滑；2a 6.01分:产业聚合增速8.5%；2c 7.8分:量价冷度;60日-36.4%;YTD-3 |
| 002086 | 东方海洋 | AGRICULTURE | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | 2a 3.8分:产业聚合增速3.0%；2b 5.0分:拐点2项:现金流支撑+最新同口径利润增；2d 5.48分:当前PB/行业1.1倍 |
| 002170 | 芭田股份 | CHEMICAL | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.3 | type2 | 有效 | 2a 2.79分:产业聚合增速1.3%；2c 6.3分:量价冷度;60日-15.0%;YTD-4；2b 10.0分:拐点6项:营收加速+净利率连升 |
| 002179 | 中航光电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:拐点1项:现金流支撑；2c 5.6分:量价冷度;60日-2.8%;YTD-1.；2d 7.26分:当前PB/行业0.8倍 |
| 002191 | 劲嘉股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 有效 | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点1项:最新同口径营收增；2a 3.13分:产业聚合增速1.9% |
| 002268 | 电科网安 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.85分:产业聚合增速8.0% |
| 002298 | 中电鑫龙 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点证据不足；2a 5.47分:产业聚合增速6.7% |
| 002335 | 科华数据 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.8 |  | 有效 | 2d 4.75分:当前PB/行业1.3倍；2a 5.48分:产业聚合增速6.7%；2c 7.3分:量价冷度;60日-33.7%;YTD-1 |
| 002337 | 赛象科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:拐点2项:最新同口径营收增+最新同口径利；2a 6.01分:产业聚合增速8.5%；2c 8.0分:量价冷度;60日-31.1%;YTD-3 |
| 002386 | 天原股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.87分:产业与公司热度平均须>4；2a 2.87分:产业聚合增速1.5%；2b 3.5分:拐点2项:最新同口径营收增+最新同口径利 |
| 002482 | 广田集团 | CONSTRUCTION | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 002522 | 浙江众成 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.1 |  | 有效 | 5e 1.0分:10年均利PE38.8倍；5d 5.0分:历史利润振幅4.9倍；5b 6.0分:PB18%/1.68;冷7;毛10 |
| 002545 | 东方铁塔 | STEEL | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _veto 0.95分:产业与公司热度平均须>4；2a 0.95分:产业聚合增速-8.2%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶 |
| 002555 | 三七互娱 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 5.3 |  | 有效 | _condition 4.54分:补全全部缺失证据后仍至少一套不超过70；7b 4.54分:第5模板45.38；7a 5.29分:第1模板52.93 |
| 002607 | 中公教育 | TOURISM_EDU | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.72分:补全全部缺失证据后仍至少一套不超过70；7a 3.72分:第1模板37.18；7b 3.79分:第5模板37.93 |
| 002661 | 克明食品 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.84分:产业与公司热度平均须>4；2a 1.84分:产业聚合增速-1.3%；2b 2.0分:最新同口径利润恶化 |
| 002805 | 丰元股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5%；6d 5.0分:最新同口径利润改善 |
| 002849 | 威星智能 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 有效 | _condition 4.0分:须确认实际仓位符合建议上限；6d 4.0分:最新同口径利润同比下降；6b 4.7分:研发与经营数据 |
| 002865 | 钧达股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 3.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.47分:产业与公司热度平均须>4；_condition 1.47分:估值须合理或满足强周期修正；2a 1.47分:产业聚合增速-4.3% |
| 002876 | 三利谱 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:年度利润明显下滑；2c 5.9分:量价冷度;60日-11.7%;YTD6.；2a 7.49分:产业聚合增速16.3% |
| 002897 | 意华股份 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 1.44分:产业与公司热度平均须>4；2a 1.44分:产业聚合增速-4.5%；2b 4.0分:最新同口径利润明显下滑 |
| 002915 | 中欣氟材 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负 |
| 002969 | 嘉美包装 | LIGHT_MFG | 无触发（不买） | 1️⃣ 估值买入区 | 3.9 |  | 有效 | _veto 0.7分:买入区深度不足；_condition 0.7分:须进入模型买入区；1c 0.7分:FCF1.1%;末1.24亿 |
| 003002 | 壶化股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.81分:产业与公司热度平均须>4；2a 2.81分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 300032 | 金龙机电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2d 5.59分:当前PB/行业1.1倍；2a 7.5分:产业聚合增速16.3% |
| 300111 | 向日葵 | CHEM_PHARMA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 300150 | 世纪瑞尔 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 有效 | 2b 2.0分:最新同口径利润恶化；2a 7.49分:产业聚合增速16.3%；2c 8.0分:量价冷度;60日-33.7%;YTD-3 |
| 300299 | 富春股份 | MEDIA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速2.5%；6d 6.0分:利润改善43.9% |
| 300343 | 联创股份 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50% |
| 300416 | 苏试试验 | PROFESSIONAL_SERVICES | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2d 5.52分:当前PB/行业1.1倍 |
| 300418 | 昆仑万维 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _condition 4.62分:估值须合理或满足强周期修正；2d 4.62分:当前PB/行业1.3倍；2a 5.79分:产业聚合增速7.8% |
| 300442 | 润泽科技 | SOFTWARE | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 8.0 | type2 | 跳过:ttm_fcff_nonpositive | 2a 5.82分:产业聚合增速7.9%；2c 6.1分:量价冷度;60日-28.3%;YTD30；2b 10.0分:拐点5项:营收加速+净利率连升 |
| 300589 | 江龙船艇 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _veto 4.0分:仅1项核心证据≥5；_condition 4.0分:须确认实际仓位符合建议上限；6d 4.0分:最新经营现金流仍负但改善 |
| 300651 | 金陵体育 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 有效 | 2a 3.09分:产业聚合增速1.8%；2d 5.49分:当前PB/行业1.1倍；2c 7.0分:量价冷度;60日-44.1%;YTD-2 |
| 300679 | 电连技术 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.1 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20%；6a 6.0分:产业增速16.2% |
| 300727 | 润禾材料 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 2.8分:估值须合理或满足强周期修正；2a 2.8分:产业聚合增速1.3%；2d 3.15分:归母利润趋势PEG2.4 |
| 300793 | 佳禾智能 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:年度利润暴跌；2a 7.49分:产业聚合增速16.3%；2c 8.0分:量价冷度;60日-28.2%;YTD-3 |
| 300955 | 嘉亨家化 | LIGHT_MFG | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.8% |
| 300962 | 中金辐照 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 4.5 |  | 跳过:ttm_fcff_nonpositive | _condition 3.73分:补全全部缺失证据后仍至少一套不超过70；7b 3.73分:第5模板37.33；7a 4.78分:第1模板47.81 |
| 301002 | 崧盛股份 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.4 |  | 跳过:ttm_fcff_nonpositive | _condition 4.98分:估值须合理或满足强周期修正；2d 4.98分:当前PB/行业1.2倍；2a 5.46分:产业聚合增速6.6% |
| 301070 | 开勒股份 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20% |
| 301078 | 孩子王 | RETAIL | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 1.16分:产业与公司热度平均须>4；2a 1.16分:产业聚合增速-6.7%；2b 4.0分:最新同口径经营现金流明显下滑 |
| 301085 | 亚康股份 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:年度利润暴跌 |
| 301095 | 广立微 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 6.6 |  | 跳过:ttm_fcff_nonpositive | 2b 5.5分:拐点3项:营收加速+最新同口径营收增；2c 5.9分:量价冷度;60日-14.0%;YTD0.；2d 7.29分:当前PB/行业0.8倍 |
| 301392 | 汇成真空 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.7 |  | 有效 | _condition 3.7分:须确认实际仓位符合建议上限；6c 3.7分:经营效率与现金流数据；6b 4.6分:研发与经营数据 |
| 301399 | 英特科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径利润恶化；2a 6.01分:产业聚合增速8.5%；2d 7.74分:当前PB/行业0.7倍 |
| 301539 | 宏鑫科技 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点1项:最新同口径营收增；2d 5.15分:当前PB/行业1.2倍 |
| 600006 | 东风股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:最新同口径利润增；2a 5.71分:产业聚合增速7.5% |
| 600025 | 华能水电 | POWER_UTILITY | 无触发（不买） | 7️⃣ 优质股权型 | 4.5 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 4.0分:补全全部缺失证据后仍至少一套不超过70；7b 4.0分:第5模板40.00；7a 4.67分:第1模板46.72 |
| 600088 | 中视传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:年度利润暴跌 |
| 600105 | 永鼎股份 | TELECOM | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _condition 3.18分:估值须合理或满足强周期修正；2d 3.18分:归母利润趋势PEG2.4；2b 4.0分:最新同口径利润明显下滑 |
| 600158 | 中体产业 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点2项:最新同口径营收增+最新同口径利；2a 3.59分:产业聚合增速2.6% |
| 600303 | 曙光股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 6.0 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径营收明显下滑；2a 5.68分:产业聚合增速7.4%；2d 7.41分:当前PB/行业0.8倍 |
| 600348 | 华阳股份 | COAL | 无触发（不买） | 5️⃣ 强周期底部 | 5.5 |  | 跳过:ttm_fcff_nonpositive | 5b 4.0分:PB40%/1.12;冷1;利8；5c 4.0分:资产负债表稳健1项；5d 6.0分:历史利润振幅16.4倍 |
| 600449 | 宁夏建材 | BUILDING_MATERIAL | 5️⃣ 强周期底部 | 5️⃣ 强周期底部 | 7.0 | type5 | 有效 | 5b 6.0分:PB3%/0.72;冷5;毛8；5d 6.0分:历史利润振幅16.7倍；5a 7.0分:大宗行业/毛利/利润周期 |
| 600637 | XD东方明 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 3.53分:产业聚合增速2.5% |
| 600663 | 陆家嘴 | REAL_ESTATE | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.6分:补全全部缺失证据后仍至少一套不超过70；7b 3.6分:第5模板35.97；7c 3.86分:补丁538.63；安全边际6.1 |
| 600839 | 四川长虹 | HOME_APPLIANCE | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 4.66分:产业聚合增速4.4% |
| 600860 | 京城股份 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50%；6b 4.3分:研发与经营数据 |
| 600894 | 广日股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 有效 | 2b 3.5分:拐点2项:最新同口径营收增+最新同口径利；2a 6.02分:产业聚合增速8.6%；2c 7.4分:量价冷度;60日-20.4%;YTD-1 |
| 600969 | 郴电国际 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 6.6 |  | 跳过:nonpositive_pessimistic_equity_value | 2a 1.94分:产业聚合增速-0.4%；2c 6.6分:量价冷度;60日-17.4%;YTD-1；2d 8.95分:当前PB/行业0.5倍 |
| 600976 | 健民集团 | TRAD_CN_MED | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 有效 | _veto 1.62分:产业与公司热度平均须>4；2a 1.62分:产业聚合增速-3.0%；2b 4.0分:拐点1项:现金流支撑 |
| 601112 | 振石股份 | BUILDING_MATERIAL | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:ttm_fcff_nonpositive | _condition 3.38分:补全全部缺失证据后仍至少一套不超过70；7b 3.38分:第5模板33.81；7c 4.12分:补丁541.15；安全边际5.9 |
| 601222 | 林洋能源 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:年度利润暴跌；2a 6.03分:产业聚合增速8.6%；2c 6.2分:量价冷度;60日-12.4%;YTD0. |
| 601888 | 中国中免 | TOURISM_EDU | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | 2b 3.0分:拐点2项:现金流支撑+最新同口径利润增；2a 5.11分:产业聚合增速5.4%；2c 7.1分:量价冷度;60日-15.4%;YTD-4 |
| 601929 | 吉视传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 3.47分:产业聚合增速2.5% |
| 603012 | 创力集团 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.2 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20% |
| 603026 | 石大胜华 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化 |
| 603067 | 振华股份 | CHEMICAL | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 跳过:ttm_fcff_nonpositive | _condition 4.17分:补全全部缺失证据后仍至少一套不超过70；7b 4.17分:第5模板41.68；7a 5.09分:第1模板50.90 |
| 603108 | 润达医疗 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点1项:最新同口径利润增；2a 2.84分:产业聚合增速1.4% |
| 603163 | 圣晖集成 | CONSTRUCTION | 无触发（不买） | 7️⃣ 优质股权型 | 5.0 |  | 有效 | _condition 4.79分:补全全部缺失证据后仍至少一套不超过70；7b 4.79分:第5模板47.90；7a 5.09分:第1模板50.91 |
| 603276 | 恒兴新材 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 6.6 |  | 跳过:ttm_fcff_nonpositive_normalised | 2a 2.81分:产业聚合增速1.3%；2c 7.0分:量价冷度;60日-20.2%;YTD-1；2d 7.36分:当前PB/行业0.8倍 |
| 603629 | 利通电子 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 7.1 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.8分:市场周期不够冷；2c 2.8分:量价冷度;60日42.5%;YTD529；2a 7.49分:产业聚合增速16.3% |
| 603690 | 至纯科技 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50%；6c 3.6分:经营效率与现金流数据 |
| 603778 | 国晟科技 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 3.5 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:仅0项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5% |
| 603931 | 格林达 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 5.7 |  | 有效 | 5e 1.0分:10年均利PE62.7倍；5b 3.5分:PB52%/3.64;冷1;毛8；5d 5.0分:历史利润振幅3.0倍 |
| 605006 | 山东玻纤 | BUILDING_MATERIAL | 无触发（不买） | 2️⃣ 两热一冷 | 3.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.01分:市场周期不够冷；_condition 1.01分:估值须合理或满足强周期修正；2a 1.01分:产业聚合增速-7.9% |
| 605111 | 新洁能 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 有效 | _veto 2.0分:市场周期不够冷；2c 2.0分:量价冷度;60日48.1%;YTD57.；2b 3.5分:拐点2项:现金流支撑+最新同口径营收增 |
| 605122 | 四方新材 | BUILDING_MATERIAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点1项:最新同口径利润增；2a 1.02分:产业聚合增速-7.9% |
| 605300 | 佳禾食品 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 有效 | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点1项:最新同口径利润增；2a 1.84分:产业聚合增速-1.3% |
| 605399 | 晨光新材 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.7 |  | 跳过:ttm_fcff_nonpositive | 5e 5.0分:10年均利PE17.4倍；5c 6.0分:资产负债表稳健2项；5d 6.0分:历史利润振幅38.0倍 |
| 688088 | 虹软科技 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.83分:产业聚合增速7.9% |
| 688289 | 圣湘生物 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.77分:产业与公司热度平均须>4；2a 2.77分:产业聚合增速1.3%；2b 3.5分:拐点1项:现金流支撑 |
| 688312 | 燕麦科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 有效 | 2d 5.09分:归母利润趋势PEG1.8；2b 6.0分:最新同口径利润下滑,拐点封顶；2a 6.01分:产业聚合增速8.5% |
| 688400 | 凌云光 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | _condition 4.54分:估值须合理或满足强周期修正；2d 4.54分:当前PB/行业1.4倍；2c 5.3分:量价冷度;60日-5.1%;YTD5.5 |
| 688576 | 西山科技 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.79分:产业与公司热度平均须>4；2a 2.79分:产业聚合增速1.3%；2b 3.0分:拐点2项:现金流支撑+最新同口径营收增 |
| 688592 | 司南导航 | SOFTWARE | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.0 | type2 | 跳过:ttm_fcff_nonpositive | 2a 5.83分:产业聚合增速7.9%；2b 7.0分:拐点3项:营收加速+最新同口径营收增；2d 7.34分:当前PB/行业0.8倍 |
| 688659 | 元琛科技 | POWER_UTILITY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.5 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-0.4%；6d 2.0分:最新同口径经营现金流转负 |
| 688676 | 金盘科技 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化 |
| 688695 | 中创股份 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点证据不足；2a 5.83分:产业聚合增速7.9% |
| 688790 | 昂瑞微 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.2 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径利润降幅≥20%；6c 5.3分:经营效率与现金流数据 |
| 688807 | 优迅股份 | SEMICONDUCTOR | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG22.7；2c 5.1分:量价冷度;60日-22.7%;YTD39 |
