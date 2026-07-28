# 固定随机 100 家公司审计

- seed: `20260715`
- sample_size: `100`
- eligible_universe_size: `4982`
- data_timestamp_utc: `2026-07-28T12:27:27.050159+00:00`
- dcf_valid: `38`
- dcf_skipped_with_reason: `62`
- pipeline_issues: `0`
- engine_self_check_errors: `0`
- same_source_scoring_replay_errors: `0`
- same_source_valuation_replay_errors: `0`
- independent_check_errors: `0`
- triggered_by_type: `{'type1': 0, 'type2': 3, 'type3': 0, 'type4': 0, 'type5': 1, 'type6': 0, 'type7': 0}`
- snapshot_content_sha256: `252381f93d90869230ffa8900b5fa4188da25f00b5e6b4d71a216e9c352c2d93`
- snapshot_artifact_sha256: `573FB2BCD604796A5B75A182AAF76763E873D835415333F9B944169D15D4A580`
- code_sha256: `e0360a318a201c88770e8d99ea5bbe45728d68c554218cd6e2a66b8ec7f0de95`
- rules_sha256: `5eb2b0894d59f6bfb676a4c84c142e0296c5510d3960a3b4470f6a169f11a99e`
- dependency_manifest_sha256: `eb984ae26b0b0fbb21ad4899c47ab703f7e449c96ef226c3cd223f342f6c2b7a`
- industry_sha256: `e5a71f39c525a1c3c7e7bd947354dad357d5f33e863b5a6f682271dfa9a1e631`
- patch6_source: `{'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}`
- type7_source_documents: `{'template1': {'path_at_model_authoring': 'E:\\模板汇总MD\\第1模板.md', 'sha256': '98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d'}, 'template5': {'path_at_model_authoring': 'E:\\模板汇总MD\\第5模板.md', 'sha256': '37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946'}, 'patch5': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁5.md', 'sha256': '8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4'}, 'patch6': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6'}}`
- risk_parameter_sources: `{'model_as_of': '2026-07-15', 'risk_free_rate_as_of': '2026-07-15', 'risk_free_rate_source': 'ChinaBond China Government Bond Yield Curve 10Y', 'risk_free_rate_source_url': 'https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN', 'equity_risk_premium_as_of': '2026-04-01', 'equity_risk_premium_basis': 'china_rating_based_total_erp', 'equity_risk_premium_source_url': 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx', 'ctrypremApr26.xlsx': '2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e', 'industry_data_as_of': '2026-01-05', 'betaChina.xls': 'ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9', 'waccChina.xls': '525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6'}`
- scoring_verification_scope: `{'same_source_replay': 'recomputes every published field from reordered production inputs', 'same_source_valuation_replay': 'recomputes valuation existence, payloads, skip reasons and sampled issues', 'independent_runtime_checks': 'recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding', 'business_rule_oracle': 'fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py'}`
- git: `{'commit': '936632df901fa680f1cf99ebe304dc3761d81305', 'dirty': False}`

## 公司明细

| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |
|---|---|---|---|---|---:|---|---|---|
| 000628 | 高新发展 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 3.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.37分:产业与公司热度平均须>4；_condition 1.37分:估值须合理或满足强周期修正；2a 1.37分:产业聚合增速-5.0% |
| 000717 | 中南股份 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5c 4.0分:资产负债表稳健1项；5b 6.0分:PB48%/0.78;冷7;利10；5d 6.0分:历史利润振幅67.4倍 |
| 000719 | 中原传媒 | MEDIA | 无触发（不买） | 1️⃣ 估值买入区 | 5.3 |  | 有效 | _veto 1.0分:买入区深度不足；_condition 1.0分:须进入模型买入区；1d 1.0分:最新报告期经营现金流为负 |
| 000830 | 鲁西化工 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.2 |  | 跳过:nonpositive_pessimistic_equity_value | 5e 5.0分:10年均利PE12.3倍；5b 6.0分:PB33%/1.23;冷8;毛10；5c 6.0分:资产负债表稳健2项 |
| 001260 | 坤泰股份 | AUTO_PARTS | 无触发（不买） | 7️⃣ 优质股权型 | 4.6 |  | 有效 | _condition 4.11分:补全全部缺失证据后仍至少一套不超过70；7b 4.11分:产业质量估值41.06；7c 4.6分:商业安全45.98；边际8.2 |
| 001285 | 瑞立科密 | AUTO_PARTS | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 有效 | _condition 4.3分:补全全部缺失证据后仍至少一套不超过70；7b 4.3分:产业质量估值43.02；7c 4.75分:商业安全47.53；边际9.1 |
| 001301 | 尚太科技 | CHEMICAL | 无触发（不买） | 7️⃣ 优质股权型 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _condition 3.94分:补全全部缺失证据后仍至少一套不超过70；7b 3.94分:产业质量估值39.36；7c 4.34分:商业安全43.38；边际6.9 |
| 001311 | 多利科技 | AUTO_PARTS | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _condition 3.21分:补全全部缺失证据后仍至少一套不超过70；7b 3.21分:产业质量估值32.05；7c 3.87分:商业安全38.72；边际7.2 |
| 001360 | 南矿集团 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.3 |  | 跳过:ttm_fcff_nonpositive | _condition 2.83分:补全全部缺失证据后仍至少一套不超过70；7b 2.83分:产业质量估值28.30；7a 3.46分:长期质量回报34.64 |
| 002088 | 鲁阳节能 | BUILDING_MATERIAL | 5️⃣ 强周期底部 | 5️⃣ 强周期底部 | 7.5 | type5 | 有效 | 5d 6.0分:历史利润振幅13.6倍；5a 7.0分:大宗行业/毛利/利润周期；5e 7.0分:10年均利PE10.3倍 |
| 002171 | 楚江新材 | NONFERROUS | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新同口径经营现金流降幅≥20% |
| 002180 | 奔图科技 | SOFTWARE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径营收降幅≥50%；6b 4.2分:研发与经营数据 |
| 002192 | 融捷股份 | NONFERROUS | 无触发（不买） | 5️⃣ 强周期底部 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 5e 1.0分:10年均利PE53.7倍；5b 3.5分:PB45%/4.31;冷1;利8；5d 6.0分:历史利润振幅459.9倍 |
| 002268 | 电科网安 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:拐点1项:现金流支撑 |
| 002298 | 中电鑫龙 | ELEC_EQUIP | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流转负 |
| 002335 | 科华数据 | ELEC_EQUIP | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.2 | type2 | 有效 | 2a 5.48分:产业聚合增速6.7%；2d 6.32分:自身五年PB/PE分位46%；2c 7.6分:量价冷度;60日-32.1%… |
| 002337 | 赛象科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.4 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 3.23分:补全全部缺失证据后仍至少一套不超过70；7c 3.23分:商业安全32.33；边际5.8；7b 3.27分:产业质量估值32.74 |
| 002386 | 天原股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.0 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2d 2.0分:缺公司自身五年PE/PB分位 |
| 002482 | 广田集团 | CONSTRUCTION | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 002522 | 浙江众成 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 6.1 |  | 有效 | 5e 1.0分:10年均利PE42.8倍；5d 5.0分:历史利润振幅4.9倍；5b 6.0分:PB25%/1.85;冷7;毛10 |
| 002545 | 东方铁塔 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 5.4 |  | 有效 | 5e 1.0分:10年均利PE46.4倍；5b 1.7分:PB89%/2.31;冷1;毛2；5d 6.0分:历史利润振幅8.8倍 |
| 002555 | 三七互娱 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 5.3 |  | 有效 | _condition 4.53分:补全全部缺失证据后仍至少一套不超过70；7b 4.53分:产业质量估值45.32；7a 5.28分:长期质量回报52.84 |
| 002607 | 中公教育 | TOURISM_EDU | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.72分:补全全部缺失证据后仍至少一套不超过70；7a 3.72分:长期质量回报37.18；7c 3.72分:商业安全37.19；边际7.3 |
| 002661 | 克明食品 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 3.6 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.18分:补全全部缺失证据后仍至少一套不超过70；7b 3.18分:产业质量估值31.84；7c 3.7分:商业安全37.00；边际6.2 |
| 002805 | 丰元股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-4.5%；6d 5.0分:最新同口径利润改善 |
| 002850 | 科达利 | NEW_ENERGY_VEH | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 有效 | _condition 4.49分:补全全部缺失证据后仍至少一套不超过70；7b 4.49分:产业质量估值44.90；7c 4.78分:商业安全47.82；边际9.3 |
| 002866 | 传艺科技 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.5 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50%；6b 4.1分:研发与经营数据 |
| 002877 | 智能自控 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.1 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50%；6b 3.8分:研发与经营数据 |
| 002899 | 英派斯 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 4.2 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.7分:补全全部缺失证据后仍至少一套不超过70；7b 3.7分:产业质量估值37.00；7c 4.22分:商业安全42.22；边际7.1 |
| 002916 | 深南电路 | ELEC_COMPONENT | 无触发（不买） | 无可完整诊断框架 |  |  | 跳过:ttm_fcff_nonpositive |  |
| 002970 | 锐明技术 | SOFTWARE | 无触发（不买） | 7️⃣ 优质股权型 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _condition 4.5分:补全全部缺失证据后仍至少一套不超过70；7b 4.5分:产业质量估值44.99；7c 5.22分:商业安全52.21；边际9.0 |
| 003003 | 天元股份 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.18分:补全全部缺失证据后仍至少一套不超过70；7b 3.18分:产业质量估值31.79；7c 3.82分:商业安全38.20；边际7.6 |
| 300033 | 同花顺 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 6.9 |  | 有效 | _condition 4.41分:估值须合理或满足强周期修正；2d 4.41分:自身五年PB/PE分位66%；2a 5.8分:产业聚合增速7.8% |
| 300112 | 万讯自控 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50%；6c 4.7分:经营效率与现金流数据 |
| 300151 | 昌红科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.6 |  | 跳过:ttm_fcff_nonpositive | _condition 3.45分:补全全部缺失证据后仍至少一套不超过70；7b 3.45分:产业质量估值34.54；7a 3.7分:长期质量回报36.96 |
| 300302 | 同有科技 | SOFTWARE | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:ttm_fcff_nonpositive | _condition 3.53分:补全全部缺失证据后仍至少一套不超过70；7c 3.53分:商业安全35.26；边际6.0；7a 3.96分:长期质量回报39.59 |
| 300346 | 南大光电 | ELEC_COMPONENT | 无触发（不买） | 7️⃣ 优质股权型 | 5.2 |  | 有效 | _condition 4.82分:补全全部缺失证据后仍至少一套不超过70；7b 4.82分:产业质量估值48.20；7c 5.04分:商业安全50.38；边际8.6 |
| 300418 | 昆仑万维 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _condition 2.76分:估值须合理或满足强周期修正；2d 2.76分:自身五年PB分位82%；2a 5.79分:产业聚合增速7.8% |
| 300421 | 力星股份 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.67分:补全全部缺失证据后仍至少一套不超过70；7b 3.67分:产业质量估值36.71；7c 3.92分:商业安全39.19；边际6.0 |
| 300444 | 双杰电气 | ELEC_EQUIP | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50% |
| 300591 | 万里马 | TEXTILE_APPAREL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.9 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 2.8分:须确认实际仓位符合建议上限；6b 2.8分:研发与经营数据；6a 3.0分:产业增速0.5% |
| 300654 | 世纪天鸿 | MEDIA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.0 |  | 有效 | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流转负 |
| 300682 | 朗新科技 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径利润恶化 |
| 300731 | 科创新源 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负；6a 3.0分:产业增速1.3% |
| 300797 | 钢研纳克 | PROFESSIONAL_SERVICES | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.2 | type2 | 有效 | 2a 5.49分:产业聚合增速6.7%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶；2c 8.0分:量价冷度;60日-25.5%… |
| 300960 | 通业科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _condition 4.35分:补全全部缺失证据后仍至少一套不超过70；7b 4.35分:产业质量估值43.51；7c 4.84分:商业安全48.36；边际6.7 |
| 300967 | 晓鸣股份 | AGRICULTURE | 无触发（不买） | 7️⃣ 优质股权型 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _condition 3.98分:补全全部缺失证据后仍至少一套不超过70；7b 3.98分:产业质量估值39.82；7c 4.22分:商业安全42.18；边际7.0 |
| 301008 | 宏昌科技 | HOME_APPLIANCE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润降幅≥50% |
| 301077 | 星华新材 | CHEMICAL | 无触发（不买） | 7️⃣ 优质股权型 | 4.3 |  | 有效 | _condition 4.08分:补全全部缺失证据后仍至少一套不超过70；7b 4.08分:产业质量估值40.77；7c 4.24分:商业安全42.37；边际8.4 |
| 301085 | 亚康股份 | SOFTWARE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.3 |  | 有效 | _veto 1.7分:仅1项核心证据≥5；_condition 1.7分:须确认实际仓位符合建议上限；6b 1.7分:研发与经营数据 |
| 301091 | 深城交 | CONSTRUCTION | 无触发（不买） | 7️⃣ 优质股权型 | 3.1 |  | 跳过:ttm_fcff_nonpositive | _condition 2.56分:补全全部缺失证据后仍至少一套不超过70；7b 2.56分:产业质量估值25.60；7a 3.34分:长期质量回报33.36 |
| 301101 | 明月镜片 | LIGHT_MFG | 无触发（不买） | 7️⃣ 优质股权型 | 4.9 |  | 有效 | _condition 4.38分:补全全部缺失证据后仍至少一套不超过70；7b 4.38分:产业质量估值43.75；7c 4.89分:商业安全48.88；边际9.3 |
| 301399 | 英特科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.2 |  | 跳过:ttm_fcff_nonpositive | _condition 2.78分:补全全部缺失证据后仍至少一套不超过70；7b 2.78分:产业质量估值27.81；7c 3.3分:商业安全33.01；边际7.0 |
| 301428 | 世纪恒通 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 3.6 |  | 跳过:ttm_fcff_nonpositive | _veto 0.0分:产业与公司热度平均须>4；_condition 0.0分:估值须合理或满足强周期修正；2b 0.0分:拐点证据不足 |
| 301556 | 托普云农 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化 |
| 600012 | 皖通高速 | TRANSPORT | 无触发（不买） | 7️⃣ 优质股权型 | 4.3 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.75分:补全全部缺失证据后仍至少一套不超过70；7b 3.75分:产业质量估值37.50；7a 4.57分:长期质量回报45.69 |
| 600031 | 三一重工 | CONST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 有效 | _condition 4.48分:补全全部缺失证据后仍至少一套不超过70；7b 4.48分:产业质量估值44.77；7c 4.74分:商业安全47.41；边际8.6 |
| 600098 | 广州发展 | POWER_UTILITY | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:ttm_fcff_nonpositive | _condition 3.54分:补全全部缺失证据后仍至少一套不超过70；7b 3.54分:产业质量估值35.40；7c 3.78分:商业安全37.79；边际5.4 |
| 600113 | 浙江东日 | BUSINESS_SERVICES | 无触发（不买） | 1️⃣ 估值买入区 | 3.7 |  | 有效 | _veto 0.1分:买入区深度不足；_condition 0.1分:须进入模型买入区；1c 0.1分:FCF0.1%;末2352.50万 |
| 600166 | 福田汽车 | AUTO_VEHICLE | 无触发（不买） | 7️⃣ 优质股权型 | 3.9 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.64分:补全全部缺失证据后仍至少一套不超过70；7b 3.64分:产业质量估值36.37；7c 4.03分:商业安全40.31；边际6.5 |
| 600312 | 平高电气 | ELEC_EQUIP | 无触发（不买） | 7️⃣ 优质股权型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 4.26分:补全全部缺失证据后仍至少一套不超过70；7b 4.26分:产业质量估值42.61；7c 4.92分:商业安全49.23；边际7.9 |
| 600356 | 恒丰纸业 | LIGHT_MFG | 无触发（不买） | 1️⃣ 估值买入区 | 4.4 |  | 有效 | _veto 1.5分:买入区深度不足；_condition 1.5分:须进入模型买入区；1a 1.5分:远离买入区144% |
| 600460 | 士兰微 | SEMICONDUCTOR | 无触发（不买） | 7️⃣ 优质股权型 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _condition 4.26分:补全全部缺失证据后仍至少一套不超过70；7c 4.26分:商业安全42.61；边际6.2；7a 4.64分:长期质量回报46.41 |
| 600643 | 爱建集团 | FINANCIAL_OTHER | 无触发（不买） | 无可完整诊断框架 |  |  | 跳过:unsupported_financial_valuation_model |  |
| 600671 | 天目药业 | TRAD_CN_MED | 无触发（不买） | 7️⃣ 优质股权型 | 3.4 |  | 跳过:ttm_fcff_nonpositive | _condition 3.13分:补全全部缺失证据后仍至少一套不超过70；7c 3.13分:商业安全31.27；边际5.2；7b 3.38分:产业质量估值33.75 |
| 600847 | 万里股份 | NEW_ENERGY_VEH | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 3.2 |  | 跳过:ttm_fcff_nonpositive | _veto 0.9分:仅1项核心证据≥5；_condition 0.9分:须确认实际仓位符合建议上限；6b 0.9分:研发与经营数据 |
| 600866 | 星湖科技 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:ttm_fcff_nonpositive | _condition 3.32分:补全全部缺失证据后仍至少一套不超过70；7b 3.32分:产业质量估值33.17；7c 4.05分:商业安全40.55；边际7.1 |
| 600905 | 三峡能源 | POWER_UTILITY | 无触发（不买） | 7️⃣ 优质股权型 | 3.5 |  | 跳过:ttm_fcff_nonpositive | _condition 3.03分:补全全部缺失证据后仍至少一套不超过70；7b 3.03分:产业质量估值30.26；7c 3.75分:商业安全37.52；边际5.2 |
| 600977 | 中国电影 | MEDIA | 无触发（不买） | 7️⃣ 优质股权型 | 3.3 |  | 跳过:ttm_fcff_nonpositive | _condition 2.96分:补全全部缺失证据后仍至少一套不超过70；7b 2.96分:产业质量估值29.58；7a 3.15分:长期质量回报31.48 |
| 600983 | 惠而浦 | HOME_APPLIANCE | 无触发（不买） | 7️⃣ 优质股权型 | 4.4 |  | 有效 | _condition 3.9分:补全全部缺失证据后仍至少一套不超过70；7b 3.9分:产业质量估值38.95；7a 4.54分:长期质量回报45.41 |
| 601126 | 四方股份 | ELEC_EQUIP | 无触发（不买） | 7️⃣ 优质股权型 | 5.7 |  | 有效 | _condition 5.07分:补全全部缺失证据后仍至少一套不超过70；7b 5.07分:产业质量估值50.67；7c 5.87分:商业安全58.68；边际10.2 |
| 601233 | 桐昆股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 6.0 |  | 跳过:ttm_fcff_nonpositive | _condition 2.72分:估值须合理或满足强周期修正；2a 2.72分:产业聚合增速1.2%；2d 4.48分:自身五年PB/PE分位65% |
| 601908 | 京运通 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 3.2 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.46分:产业与公司热度平均须>4；_condition 1.46分:估值须合理或满足强周期修正；2a 1.46分:产业聚合增速-4.3% |
| 601958 | 金钼股份 | NONFERROUS | 无触发（不买） | 7️⃣ 优质股权型 | 6.1 |  | 有效 | _condition 6.04分:补全全部缺失证据后仍至少一套不超过70；7b 6.04分:产业质量估值60.41；7c 6.09分:商业安全60.94；边际11.5 |
| 603018 | 华设集团 | CONSTRUCTION | 无触发（不买） | 1️⃣ 估值买入区 | 4.5 |  | 有效 | _veto 0.9分:买入区深度不足；_condition 0.9分:须进入模型买入区；1d 0.9分:最新报告期经营现金流为负 |
| 603032 | 德新科技 | NEW_ENERGY_VEH | 无触发（不买） | 1️⃣ 估值买入区 | 4.4 |  | 有效 | _veto 0.3分:买入区深度不足；_condition 0.3分:须进入模型买入区；1c 0.3分:FCF0.4%;末8655.68万 |
| 603073 | 彩蝶实业 | TEXTILE_APPAREL | 无触发（不买） | 7️⃣ 优质股权型 | 3.7 |  | 跳过:ttm_fcff_nonpositive | _condition 3.31分:补全全部缺失证据后仍至少一套不超过70；7b 3.31分:产业质量估值33.14；7c 3.9分:商业安全39.03；边际7.4 |
| 603115 | 海星股份 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 6.0 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:市场周期不够冷；_condition 2.0分:估值须合理或满足强周期修正；2d 2.0分:缺公司自身五年PE/PB分位 |
| 603170 | 宝立食品 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 5.5 |  | 有效 | _condition 4.52分:补全全部缺失证据后仍至少一套不超过70；7b 4.52分:产业质量估值45.23；7a 5.91分:长期质量回报59.05 |
| 603282 | 亚光股份 | INDUST_MACHINERY | 无触发（不买） | 1️⃣ 估值买入区 | 4.4 |  | 有效 | _veto 0.0分:买入区深度不足；_condition 0.0分:须进入模型买入区；1d 0.0分:最新同口径利润转负 |
| 603639 | 海利尔 | CHEMICAL | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.5 |  | 有效 | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.3% |
| 603700 | 宁水集团 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:ttm_fcff_nonpositive | _condition 3.23分:补全全部缺失证据后仍至少一套不超过70；7b 3.23分:产业质量估值32.25；7c 4.05分:商业安全40.48；边际7.1 |
| 603797 | 联泰环保 | POWER_UTILITY | 无触发（不买） | 7️⃣ 优质股权型 | 3.8 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 3.21分:补全全部缺失证据后仍至少一套不超过70；7b 3.21分:产业质量估值32.07；7a 3.95分:长期质量回报39.52 |
| 603948 | 建业股份 | CHEMICAL | 无触发（不买） | 1️⃣ 估值买入区 | 5.0 |  | 有效 | _veto 1.5分:买入区深度不足；_condition 1.5分:须进入模型买入区；1a 1.5分:远离买入区48% |
| 605018 | 长华集团 | AUTO_VEHICLE | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.0 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速7.4%；6b 3.4分:研发与经营数据 |
| 605128 | 上海沿浦 | AUTO_VEHICLE | 无触发（不买） | 7️⃣ 优质股权型 | 5.0 |  | 有效 | _condition 4.66分:补全全部缺失证据后仍至少一套不超过70；7b 4.66分:产业质量估值46.60；7c 4.87分:商业安全48.73；边际8.6 |
| 605151 | 西上海 | AUTO_PARTS | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.0 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速6.0%；6b 4.4分:研发与经营数据 |
| 605337 | 李子园 | FOOD_BEV | 无触发（不买） | 7️⃣ 优质股权型 | 4.2 |  | 跳过:ttm_fcff_nonpositive | _condition 3.57分:补全全部缺失证据后仍至少一套不超过70；7b 3.57分:产业质量估值35.73；7a 4.3分:长期质量回报43.02 |
| 605566 | 福莱蒽特 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 3.7 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化 |
| 688096 | 京源环保 | POWER_UTILITY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 4.1 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-0.4%；6d 2.0分:最新同口径利润转负 |
| 688297 | 中无人机 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 6.2 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 3.3分:须确认实际仓位符合建议上限；6c 3.3分:经营效率与现金流数据；6b 5.9分:研发与经营数据 |
| 688319 | 欧林生物 | BIO_PHARMA | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 5.1 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.5%；6d 4.0分:最新同口径经营现金流同比下降 |
| 688416 | 恒烁股份 | SEMICONDUCTOR | 无触发（不买） | 6️⃣ 高风险早期/困境型 | 8.1 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 7.0分:须确认实际仓位符合建议上限；6b 7.0分:研发与经营数据；6a 8.0分:产业高速21.3% |
| 688583 | 思看科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 4.8 |  | 有效 | _condition 4.01分:补全全部缺失证据后仍至少一套不超过70；7b 4.01分:产业质量估值40.12；7c 5.15分:商业安全51.50；边际8.7 |
| 688600 | 皖仪科技 | INDUST_MACHINERY | 无触发（不买） | 7️⃣ 优质股权型 | 4.4 |  | 有效 | _condition 4.24分:补全全部缺失证据后仍至少一套不超过70；7b 4.24分:产业质量估值42.35；7c 4.37分:商业安全43.70；边际7.7 |
| 688668 | 鼎通科技 | TELECOM | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _condition 1.78分:估值须合理或满足强周期修正；2d 1.78分:自身五年PB/PE分位92%；2a 4.19分:产业聚合增速3.7% |
| 688683 | 莱尔科技 | ELEC_COMPONENT | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.5 | type2 | 跳过:ttm_fcff_nonpositive | 2d 4.63分:自身五年PB/PE分位64%；2a 7.5分:产业聚合增速16.3%；2c 8.0分:量价冷度;60日-35.7%… |
| 688702 | 盛科通信 | SEMICONDUCTOR | 无触发（不买） | 7️⃣ 优质股权型 | 4.0 |  | 跳过:ttm_fcff_nonpositive | _condition 3.65分:补全全部缺失证据后仍至少一套不超过70；7b 3.65分:产业质量估值36.53；7c 3.95分:商业安全39.50；边际6.3 |
| 688800 | 瑞可达 | ELEC_COMPONENT | 无触发（不买） | 7️⃣ 优质股权型 | 4.7 |  | 跳过:ttm_fcff_nonpositive | _condition 4.39分:补全全部缺失证据后仍至少一套不超过70；7b 4.39分:产业质量估值43.93；7c 4.64分:商业安全46.37；边际5.8 |
| 688819 | 天能股份 | NEW_ENERGY_VEH | 无触发（不买） | 1️⃣ 估值买入区 | 6.7 |  | 有效 | 1d 0.4分:最新同口径利润降幅≥50%；1b 7.0分:五项满分3项；1a 7.5分:买入区内折价14% |
