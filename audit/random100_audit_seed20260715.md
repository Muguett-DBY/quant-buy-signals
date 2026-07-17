# 固定随机 100 家公司审计

- seed: `20260715`
- sample_size: `100`
- eligible_universe_size: `4986`
- data_timestamp_utc: `2026-07-17T09:26:35.967382+00:00`
- dcf_valid: `41`
- dcf_skipped_with_reason: `59`
- pipeline_issues: `0`
- engine_self_check_errors: `0`
- same_source_scoring_replay_errors: `0`
- same_source_valuation_replay_errors: `0`
- independent_check_errors: `0`
- triggered_by_type: `{'type1': 1, 'type2': 7, 'type3': 0, 'type4': 0, 'type5': 0, 'type6': 0, 'type7': 0}`
- snapshot_content_sha256: `4c33c1589e05b80bbf6492de7841113da75bfbfc6ce9000cd504c82160ff18dd`
- snapshot_artifact_sha256: `99853144F09BC9965FB7B66A5C734505B3E13D042DE81E0A0A8F2970A3295D5D`
- code_sha256: `a15f20b7ccbe0d19dbd117c3324d4f6f570d31e46b7d834716f923a12586d998`
- rules_sha256: `055d82930ec230b5c7ed14ee7d411b309169948b338eca7431243abcd0ddd9a6`
- dependency_manifest_sha256: `d9c6ce4d509cebc544c5ed6ef64207ca54a9303e97a0e419ac28c337dc80bef1`
- industry_sha256: `e5a71f39c525a1c3c7e7bd947354dad357d5f33e863b5a6f682271dfa9a1e631`
- patch6_source: `{'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'a3c174fe036d97898c0768aa8e0c07060b0a1697b25db58d680f3824a4b8ff56'}`
- type7_source_documents: `{'template1': {'path_at_model_authoring': 'E:\\模板汇总MD\\第1模板.md', 'sha256': '98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d'}, 'template5': {'path_at_model_authoring': 'E:\\模板汇总MD\\第5模板.md', 'sha256': '37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946'}, 'patch5': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁5.md', 'sha256': '8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4'}, 'patch6': {'path_at_model_authoring': 'E:\\模板汇总MD\\补丁6.md', 'sha256': 'a3c174fe036d97898c0768aa8e0c07060b0a1697b25db58d680f3824a4b8ff56'}}`
- risk_parameter_sources: `{'model_as_of': '2026-07-15', 'risk_free_rate_as_of': '2026-07-15', 'risk_free_rate_source': 'ChinaBond China Government Bond Yield Curve 10Y', 'risk_free_rate_source_url': 'https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN', 'equity_risk_premium_as_of': '2026-04-01', 'equity_risk_premium_basis': 'china_rating_based_total_erp', 'equity_risk_premium_source_url': 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx', 'ctrypremApr26.xlsx': '2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e', 'industry_data_as_of': '2026-01-05', 'betaChina.xls': 'ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9', 'waccChina.xls': '525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6'}`
- scoring_verification_scope: `{'same_source_replay': 'recomputes every published field from reordered production inputs', 'same_source_valuation_replay': 'recomputes valuation existence, payloads, skip reasons and sampled issues', 'independent_runtime_checks': 'recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding', 'business_rule_oracle': 'fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py'}`
- git: `{'commit': 'ee2f5486b6f0904aafd08e134912c9a3c004201f', 'dirty': False}`

## 公司明细

| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |
|---|---|---|---|---|---:|---|---|---|
| 000628 | 高新发展 | CONSTRUCTION | 无触发（不买） | 2️⃣ 两热一冷 | 3.0 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.4倍 |
| 000717 | 中南股份 | STEEL | 无触发（不买） | 5️⃣ 强周期底部 | 4.1 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.0分:周期阶段不符合；_condition 2.0分:须满足5a≥7且5c≥5；5a 2.0分:最新报告期利润为负 |
| 000719 | 中原传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 3.53分:产业与公司热度平均须>4；2a 3.53分:产业聚合增速2.6%；2b 4.0分:拐点1项:现金流支撑 |
| 000830 | 鲁西化工 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.0 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 2.78分:产业聚合增速1.3% |
| 001260 | 坤泰股份 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 5.3分:产业聚合增速6.1% |
| 001285 | 瑞立科密 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.3分:产业聚合增速6.0% |
| 001301 | 尚太科技 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.78分:产业与公司热度平均须>4；2a 2.78分:产业聚合增速1.3%；2b 4.0分:最新同口径利润明显下滑 |
| 001311 | 多利科技 | AUTO_PARTS | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:年度利润明显下滑；2a 5.31分:产业聚合增速6.1%；2c 8.0分:证据:patch6-type2c-qua |
| 001360 | 南矿集团 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive | 2b 4.0分:最新同口径利润明显下滑；2a 6.01分:产业聚合增速8.5%；2d 7.81分:当前PB/行业0.7倍 |
| 002086 | 东方海洋 | AGRICULTURE | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | 2a 3.8分:产业聚合增速3.0%；2b 5.0分:拐点2项:现金流支撑+最新同口径利润增；2d 5.37分:当前PB/行业1.1倍 |
| 002170 | 芭田股份 | CHEMICAL | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.4 | type2 | 有效 | 2a 2.79分:产业聚合增速1.3%；2c 7.0分:证据:patch6-type2c-qua；2b 10.0分:拐点6项:营收加速+净利率连升 |
| 002179 | 中航光电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:拐点1项:现金流支撑；2c 6.5分:证据:patch6-type2c-qua；2a 7.5分:产业聚合增速16.3% |
| 002191 | 劲嘉股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 有效 | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点1项:最新同口径营收增；2a 3.13分:产业聚合增速1.9% |
| 002268 | 电科网安 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.6 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.85分:产业聚合增速8.0% |
| 002298 | 中电鑫龙 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.5分:产业与公司热度平均须>4；2b 1.5分:拐点证据不足；2a 5.47分:产业聚合增速6.7% |
| 002335 | 科华数据 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.9 |  | 有效 | 2d 4.98分:当前PB/行业1.2倍；2a 5.48分:产业聚合增速6.7%；2c 7.5分:证据:patch6-type2c-qua |
| 002337 | 赛象科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 6.3 |  | 跳过:ttm_fcff_nonpositive_normalised | 2b 4.0分:拐点2项:最新同口径营收增+最新同口径利；2a 6.01分:产业聚合增速8.5%；2c 7.9分:证据:patch6-type2c-qua |
| 002386 | 天原股份 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.87分:产业与公司热度平均须>4；2a 2.87分:产业聚合增速1.5%；2b 3.5分:拐点2项:最新同口径营收增+最新同口径利 |
| 002482 | 广田集团 | CONSTRUCTION | 无触发（不买） | 6️⃣ VC属性 | 4.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅1项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-5.0% |
| 002522 | 浙江众成 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 有效 | _veto 2.81分:产业与公司热度平均须>4；2a 2.81分:产业聚合增速1.3%；2b 3.5分:拐点2项:现金流支撑+最新同口径营收增 |
| 002545 | 东方铁塔 | STEEL | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 有效 | _veto 0.95分:产业与公司热度平均须>4；2a 0.95分:产业聚合增速-8.2%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶 |
| 002555 | 三七互娱 | MEDIA | 无触发（不买） | 1️⃣ 估值买入区 | 4.2 |  | 有效 | _veto 1.5分:买入区深度不足；_condition 1.5分:须进入模型买入区；1a 1.5分:远离买入区351% |
| 002607 | 中公教育 | TOURISM_EDU | 无触发（不买） | 2️⃣ 两热一冷 | 3.2 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业5.3倍 |
| 002662 | 峰璟股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.69分:产业聚合增速7.4% |
| 002807 | 江阴银行 | BANK | 1️⃣ 估值买入区 | 1️⃣ 估值买入区 | 8.4 | type1 | 有效 | 1d 3.0分:金融回归2项；1b 9.0分:银行监管满分4项；1a 9.5分:买入区内折价70% |
| 002851 | 麦格米特 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 2.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.2倍 |
| 002867 | 周大生 | RETAIL | 无触发（不买） | 2️⃣ 两热一冷 | 3.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.18分:产业与公司热度平均须>4；_condition 1.18分:估值须合理或满足强周期修正；2a 1.18分:产业聚合增速-6.6% |
| 002878 | 元隆雅图 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 4.7 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 3.46分:产业聚合增速2.4% |
| 002900 | 哈三联 | CHEM_PHARMA | 无触发（不买） | 6️⃣ VC属性 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:仅1项核心证据≥5；_condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流降幅≥50% |
| 002917 | 金奥博 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 2.8分:产业聚合增速1.3% |
| 002971 | 和远气体 | CHEMICAL | 无触发（不买） | 5️⃣ 强周期底部 | 4.4 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须满足5a≥7且5c≥5；5e 2.0分:周期估值不便宜；5a 4.0分:最新同口径利润降幅≥20% |
| 003004 | 声迅股份 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ VC属性 | 6.5 |  | 跳过:ttm_fcff_nonpositive | _condition 5.3分:须确认实际仓位符合建议上限；6b 5.3分:证据:patch6-observable；6a 6.0分:产业增速16.3% |
| 300034 | 钢研高纳 | NONFERROUS | 无触发（不买） | 6️⃣ VC属性 | 5.7 |  | 有效 | _condition 3.0分:须确认实际仓位符合建议上限；6d 3.0分:最新经营现金流仍负但改善；6c 5.5分:证据:patch6-observable |
| 300113 | 顺网科技 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 3.46分:产业聚合增速2.4% |
| 300153 | 科泰电源 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.6 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG4.9；2a 5.46分:产业聚合增速6.6% |
| 300302 | 同有科技 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 2.9 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业4.1倍 |
| 300346 | 南大光电 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 4.1 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG5.4；2c 3.4分:证据:patch6-type2c-qua |
| 300418 | 昆仑万维 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:ttm_fcff_nonpositive | _condition 4.58分:估值须合理或满足强周期修正；2d 4.58分:当前PB/行业1.3倍；2a 5.79分:产业聚合增速7.8% |
| 300421 | 力星股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:nonpositive_pessimistic_equity_value | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG25.6；2b 2.0分:最新同口径利润恶化 |
| 300444 | 双杰电气 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.45分:产业聚合增速6.6% |
| 300591 | 万里马 | TEXTILE_APPAREL | 无触发（不买） | 6️⃣ VC属性 | 4.9 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _condition 2.8分:须确认实际仓位符合建议上限；6b 2.8分:证据:patch6-observable；6a 3.0分:产业增速0.4% |
| 300653 | 正海生物 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 2.79分:产业聚合增速1.3% |
| 300681 | 英搏尔 | AUTO_PARTS | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.6 | type2 | 跳过:mixed_profit_cycle_unsupported_by_fcff | 2a 5.27分:产业聚合增速5.9%；2c 6.9分:证据:patch6-type2c-qua；2b 9.0分:拐点4项:营收加速+现金流支撑 |
| 300730 | 科创信息 | SOFTWARE | 无触发（不买） | 6️⃣ VC属性 | 5.0 |  | 跳过:ttm_fcff_nonpositive | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速7.9% |
| 300796 | 贝斯美 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 2.8分:产业聚合增速1.3% |
| 300957 | 贝泰妮 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 5.3 |  | 有效 | 2a 3.1分:产业聚合增速1.8%；2d 5.47分:当前PB/行业1.1倍；2b 6.0分:拐点3项:现金流支撑+最新同口径营收增 |
| 300964 | 本川智能 | ELEC_COMPONENT | 无触发（不买） | 6️⃣ VC属性 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径经营现金流转负；6b 3.9分:证据:patch6-observable |
| 301004 | 嘉益股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:拐点1项:现金流支撑 |
| 301072 | 中捷精工 | AUTO_VEHICLE | 无触发（不买） | 6️⃣ VC属性 | 4.5 |  | 跳过:ttm_fcff_nonpositive_normalised | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速7.4% |
| 301080 | 百普赛斯 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 有效 | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:归母利润趋势PEG23.9；2a 2.78分:产业聚合增速1.3% |
| 301087 | 可孚医疗 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 有效 | _condition 2.78分:估值须合理或满足强周期修正；2a 2.78分:产业聚合增速1.3%；2d 3.74分:归母利润趋势PEG2.1 |
| 301097 | 天益医疗 | MEDICAL_SERVICE | 无触发（不买） | 6️⃣ VC属性 | 5.3 |  | 跳过:ttm_fcff_nonpositive | _condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速1.3%；6d 4.0分:最新同口径经营现金流同比下降 |
| 301396 | 宏景科技 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 跳过:ttm_fcff_nonpositive | _condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业10.2倍；2c 4.3分:证据:patch6-type2c-qua |
| 301418 | 协昌科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:年度利润暴跌；2c 6.0分:证据:patch6-type2c-qua；2a 6.01分:产业聚合增速8.5% |
| 301551 | 无线传媒 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；_condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化 |
| 600009 | 上海机场 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 6.7 |  | 有效 | 2a 4.35分:产业聚合增速3.9%；2b 6.0分:最新同口径经营现金流下滑,拐点封顶；2c 7.3分:证据:patch6-type2c-qua |
| 600028 | 中国石化 | OIL_GAS | 无触发（不买） | 2️⃣ 两热一冷 | 4.5 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.75分:产业与公司热度平均须>4；2a 1.75分:产业聚合增速-2.0%；2b 2.0分:最新同口径现金恶化 |
| 600095 | 湘财股份 | SECURITIES | 无触发（不买） | 无可完整诊断框架 |  |  | 跳过:financial_current_attributable_profit_deterioration |  |
| 600109 | 国金证券 | SECURITIES | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.4 | type2 | 有效 | 2c 5.9分:证据:patch6-type2c-qua；2d 7.76分:金融PB/同行0.7倍；2b 8.0分:金融回归4项 |
| 600161 | 天坛生物 | BIO_PHARMA | 无触发（不买） | 2️⃣ 两热一冷 | 4.1 |  | 跳过:ttm_fcff_nonpositive | _veto 1.26分:产业与公司热度平均须>4；2a 1.26分:产业聚合增速-5.9%；2b 1.5分:拐点证据不足 |
| 600308 | 华泰股份 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 跳过:ttm_fcff_nonpositive | _veto 0.0分:产业与公司热度平均须>4；2b 0.0分:拐点证据不足；2a 3.15分:产业聚合增速1.9% |
| 600352 | 浙江龙盛 | CHEMICAL | 无触发（不买） | 2️⃣ 两热一冷 | 4.3 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 2.83分:产业聚合增速1.4% |
| 600456 | 宝钛股份 | NONFERROUS | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点证据不足；2a 6.52分:产业聚合增速10.3% |
| 600640 | 国脉文化 | MEDIA | 无触发（不买） | 6️⃣ VC属性 | 5.4 |  | 有效 | _veto 3.0分:仅1项核心证据≥5；_condition 3.0分:须确认实际仓位符合建议上限；6a 3.0分:产业增速2.5% |
| 600666 | 奥瑞德 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 跳过:ttm_fcff_nonpositive | _condition 1.62分:估值须合理或满足强周期修正；2d 1.62分:当前PB/行业2.4倍；2a 5.83分:产业聚合增速7.9% |
| 600844 | 金煤科技 | CHEMICAL | 无触发（不买） | 6️⃣ VC属性 | 3.5 |  | 跳过:ttm_fcff_nonpositive | _veto 0.5分:仅1项核心证据≥5；_condition 0.5分:须确认实际仓位符合建议上限；6b 0.5分:证据:patch6-observable |
| 600863 | 华能蒙电 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 3.5 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.95分:产业与公司热度平均须>4；2a 1.95分:产业聚合增速-0.4%；2b 2.0分:拐点1项:现金流支撑 |
| 600900 | XD长江电 | POWER_UTILITY | 无触发（不买） | 2️⃣ 两热一冷 | 4.4 |  | 跳过:nonpositive_pessimistic_equity_value | _veto 1.92分:产业与公司热度平均须>4；_condition 1.92分:估值须合理或满足强周期修正；2a 1.92分:产业聚合增速-0.6% |
| 600973 | 宝胜股份 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 6.0 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | 2b 4.0分:拐点2项:最新同口径营收增+最新同口径利；2a 5.49分:产业聚合增速6.7%；2d 7.31分:当前PB/行业0.8倍 |
| 600980 | 北矿科技 | NONFERROUS | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.4 | type2 | 有效 | 2d 4.58分:归母利润趋势PEG1.9；2a 6.51分:产业聚合增速10.3%；2c 8.0分:证据:patch6-type2c-qua |
| 601117 | 中国化学 | CONSTRUCTION | 无触发（不买） | 1️⃣ 估值买入区 | 6.9 |  | 有效 | 1d 1.0分:最新报告期经营现金流为负；1b 6.0分:五项满分2项；1c 9.1分:FCF12.6%;末129.34亿 |
| 601228 | 广州港 | TRANSPORT | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 4.36分:产业聚合增速3.9% |
| 601899 | 紫金矿业 | NONFERROUS | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 8.4 | type2 | 跳过:nonpositive_pessimistic_equity_value | 2a 6.55分:产业聚合增速10.4%；2c 7.1分:证据:patch6-type2c-qua；2b 10.0分:拐点6项:营收加速+净利率连升 |
| 601949 | 中国出版 | MEDIA | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 3.55分:产业聚合增速2.6% |
| 603015 | 弘讯科技 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 有效 | 2b 2.0分:拐点1项:现金流支撑；2d 6.0分:当前PB/行业1.0倍；2a 6.01分:产业聚合增速8.5% |
| 603029 | 天鹅股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.9 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 1.91分:估值须合理或满足强周期修正；2d 1.91分:归母利润趋势PEG3.3；2a 6.01分:产业聚合增速8.5% |
| 603070 | 万控智造 | ELEC_EQUIP | 无触发（不买） | 2️⃣ 两热一冷 | 5.4 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:拐点1项:现金流支撑；2a 5.47分:产业聚合增速6.7% |
| 603111 | 康尼机电 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 有效 | 2b 2.0分:最新同口径现金恶化；2a 6.01分:产业聚合增速8.5%；2c 6.5分:证据:patch6-type2c-qua |
| 603167 | 渤海轮渡 | TRANSPORT | 无触发（不买） | 1️⃣ 估值买入区 | 4.5 |  | 有效 | _veto 1.0分:买入区深度不足；_condition 1.0分:须进入模型买入区；1d 1.0分:最新同口径利润降幅≥20% |
| 603279 | 景津装备 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 有效 | 2b 2.0分:拐点1项:现金流支撑；2a 6.02分:产业聚合增速8.6%；2c 7.7分:证据:patch6-type2c-qua |
| 603636 | 南威软件 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径现金恶化；2a 5.85分:产业聚合增速8.0% |
| 603697 | 有友食品 | FOOD_BEV | 无触发（不买） | 2️⃣ 两热一冷 | 6.2 |  | 有效 | 2a 1.83分:产业聚合增速-1.4%；2c 5.8分:证据:patch6-type2c-qua；2d 6.35分:归母利润趋势PEG1.5 |
| 603787 | 新日股份 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 5.8 |  | 跳过:ttm_fcff_nonpositive | 2b 2.0分:最新同口径现金恶化；2a 6.02分:产业聚合增速8.6%；2c 7.7分:证据:patch6-type2c-qua |
| 603937 | 丽岛新材 | NONFERROUS | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.4 | type2 | 跳过:mixed_profit_cycle_unsupported_by_fcff | 2a 6.51分:产业聚合增速10.3%；2b 7.0分:拐点3项:营收加速+最新同口径营收增；2c 7.4分:证据:patch6-type2c-qua |
| 605009 | 豪悦护理 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 3.04分:产业与公司热度平均须>4；2a 3.04分:产业聚合增速1.7%；2b 4.0分:年度利润明显下滑 |
| 605118 | 力鼎光电 | ELEC_COMPONENT | 2️⃣ 两热一冷 | 2️⃣ 两热一冷 | 7.9 | type2 | 有效 | 2d 5.24分:归母利润趋势PEG1.7；2a 7.49分:产业聚合增速16.3%；2c 8.0分:证据:patch6-type2c-qua |
| 605133 | 嵘泰股份 | AUTO_VEHICLE | 无触发（不买） | 2️⃣ 两热一冷 | 5.5 |  | 跳过:ttm_fcff_nonpositive_normalised | _condition 2.91分:估值须合理或满足强周期修正；2d 2.91分:归母利润趋势PEG2.6；2a 5.68分:产业聚合增速7.4% |
| 605318 | 法狮龙 | BUILDING_MATERIAL | 无触发（不买） | 6️⃣ VC属性 | 3.9 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅0项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-7.9% |
| 605500 | 森林包装 | LIGHT_MFG | 无触发（不买） | 2️⃣ 两热一冷 | 5.1 |  | 跳过:ttm_fcff_nonpositive | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:年度利润暴跌；2a 3.06分:产业聚合增速1.8% |
| 688091 | 上海谊众 | CHEM_PHARMA | 无触发（不买） | 2️⃣ 两热一冷 | 4.9 |  | 跳过:ttm_fcff_nonpositive | _condition 1.48分:估值须合理或满足强周期修正；2d 1.48分:当前PB/行业2.5倍；2a 2.55分:产业聚合增速0.9% |
| 688292 | 浩瀚深度 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.2 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:产业与公司热度平均须>4；2b 1.0分:拐点1项:最新同口径营收增；2a 5.83分:产业聚合增速7.9% |
| 688315 | 诺禾致源 | MEDICAL_SERVICE | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 有效 | _veto 2.78分:产业与公司热度平均须>4；2a 2.78分:产业聚合增速1.3%；2b 3.5分:拐点2项:现金流支撑+最新同口径营收增 |
| 688408 | 中信博 | NEW_ENERGY_VEH | 无触发（不买） | 2️⃣ 两热一冷 | 4.6 |  | 跳过:ttm_fcff_nonpositive | _veto 1.44分:产业与公司热度平均须>4；2a 1.44分:产业聚合增速-4.5%；2b 2.0分:年度利润暴跌 |
| 688579 | 地纬智能 | SOFTWARE | 无触发（不买） | 2️⃣ 两热一冷 | 5.7 |  | 有效 | _veto 2.0分:产业与公司热度平均须>4；2b 2.0分:最新同口径利润恶化；2a 5.83分:产业聚合增速7.9% |
| 688596 | 正帆科技 | INDUST_MACHINERY | 无触发（不买） | 6️⃣ VC属性 | 5.5 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:须确认实际仓位符合建议上限；6d 2.0分:最新同口径利润转负；6b 5.4分:证据:patch6-observable |
| 688662 | 富信科技 | ELEC_COMPONENT | 无触发（不买） | 2️⃣ 两热一冷 | 3.2 |  | 跳过:ttm_fcff_nonpositive | _veto 1.0分:市场周期不够冷；_condition 1.0分:估值须合理或满足强周期修正；2d 1.0分:当前PB/行业3.3倍 |
| 688679 | 通源环境 | POWER_UTILITY | 无触发（不买） | 6️⃣ VC属性 | 3.8 |  | 跳过:mixed_profit_cycle_unsupported_by_fcff | _veto 1.0分:仅0项核心证据≥5；_condition 1.0分:须确认实际仓位符合建议上限；6a 1.0分:产业增速-0.4% |
| 688698 | 伟创电气 | INDUST_MACHINERY | 无触发（不买） | 2️⃣ 两热一冷 | 4.8 |  | 跳过:ttm_fcff_nonpositive | _condition 2.0分:估值须合理或满足强周期修正；2b 2.0分:最新同口径现金恶化；2d 3.67分:归母利润趋势PEG2.2 |
| 688796 | 百奥赛图 | MEDICAL_SERVICE | 无触发（不买） | 1️⃣ 估值买入区 | 4.5 |  | 有效 | _veto 0.3分:买入区深度不足；_condition 0.3分:须进入模型买入区；1c 0.3分:FCF0.4%;末2.99亿 |
| 688811 | 有研复材 | NONFERROUS | 无触发（不买） | 1️⃣ 估值买入区 | 3.9 |  | 有效 | _veto 0.1分:买入区深度不足；_condition 0.1分:须进入模型买入区；1c 0.1分:FCF0.1%;末6160.03万 |
