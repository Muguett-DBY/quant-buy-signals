const METHODOLOGY_VERSION="patch6-seven-types-2026-08-01-classified-type7-v4";
const METHODOLOGY_LABEL="七类量化买入方法（2026年8月）";
const CATALOGUE_INDEX_CONTRACT_VERSION=2;
const METHODOLOGY={
  type1:{
    summary:"用经过校验的估值结果判断现价是否进入买入区，同时排除价值陷阱并检查现金回报与回归动力。",
    applicability:"适用于能够形成可核验估值基准的公司；金融股使用专属市净率与监管口径。",
    trigger:"证据完整、加权总分至少7分，并且价格深度与价值陷阱检查不触发硬性否决。",
    dimensions:{
      "1a":{weight:30,meaning:"现价相对经校验买入区的位置。",data:"当前收盘价、悲观估值、买入区上下沿。",direction:"进入买入区且折价越深，分数越高；明显高于买入区会触发价格否决。"},
      "1b":{weight:35,meaning:"便宜是否来自经营、负债、现金流或监管风险。",data:"盈利趋势、负债与流动性、经营现金流、资本回报；金融股另看监管指标。",direction:"通过的风险检查越多，分数越高；证据完整且不超过3分会否决。"},
      "1c":{weight:20,meaning:"估值之外仍保留多少可兑现的安全垫。",data:"自由现金流收益率与最新自由现金流；金融股使用合理市净率安全边际。",direction:"自由现金流收益率或估值安全边际越高，分数越高。"},
      "1d":{weight:15,meaning:"价格向价值回归是否存在可验证动力。",data:"收入、利润、利润率、经营现金流的连续变化及最新同口径报告。",direction:"可重复验证的改善信号越多，分数越高；最新恶化会封顶。"}
    }
  },
  type2:{
    summary:"寻找产业回暖、公司出现拐点，但市场情绪仍冷且估值合理的错配机会。",
    applicability:"适用于能够取得行业同行、公司财务拐点、独立量价冷度和自身估值历史的公司。",
    trigger:"证据完整、加权总分至少7分，两项热度平均大于4、市场冷度大于3，并满足估值条件。",
    dimensions:{
      "2a":{weight:25,meaning:"公司所在产业是否正在升温。",data:"剔除本公司后的同行聚合收入增速与样本覆盖；金融股使用行业专属周期指标。",direction:"产业增速越高，分数越高；不是用本公司增长代替行业增长。"},
      "2b":{weight:30,meaning:"公司经营是否已经从低位改善。",data:"连续收入、利润率、利润、现金流和最新同口径同比。",direction:"加速、连续改善和现金流支撑越多，分数越高；最新恶化会封顶。"},
      "2c":{weight:25,meaning:"市场交易是否仍处于相对冷清阶段。",data:"独立的价格区间、成交与量价冷度证据。",direction:"越冷分数越高；不允许用低PE或低PB重复代替市场冷度。"},
      "2d":{weight:20,meaning:"当前估值在公司自身历史中是否合理。",data:"公司自身近五年PE和PB分布、当前PE/PB与历史分位。",direction:"处于自身历史较低分位时分数更高；不能用同行估值替代自身历史。"}
    }
  },
  type3:{
    summary:"判断高增长是否由护城河、现金质量、资本回报和可持续证据共同支撑。",
    applicability:"适用于非金融公司；趋势增长不足10%时仍会如实评分，但无法满足可持续高增长的核心条件。",
    trigger:"证据完整、长期趋势增速至少10%、加权总分至少7分，护城河不能过弱；泡沫过高会压低或否决结果。",
    dimensions:{
      "3a":{weight:25,meaning:"增长是否建立在可持续竞争优势上。",data:"毛利率、投入资本回报率、现金转化及可追溯护城河证据。",direction:"竞争优势越强且证据越直接，分数越高；仅有财务代理会受到证据上限。"},
      "3b":{weight:20,meaning:"增长是否转化为真实现金且没有依赖过度杠杆或并购。",data:"经营现金流、利润、负债、稳定性、商誉和收购现金历史。",direction:"现金转化好、杠杆稳、增长稳定且并购依赖低时分数更高。"},
      "3c":{weight:20,meaning:"新增资本是否创造超过资金成本的回报。",data:"同口径投入资本回报率与加权资金成本。",direction:"投入资本回报率减资金成本的差额越大，分数越高。"},
      "3d":{weight:25,meaning:"当前增长能否延续而不是一次性跳升。",data:"连续收入历史、业务分部增长、外部可核验增长依据和行业空间。",direction:"多年连续、来源分散且外部证据完整时分数更高。"},
      "3e":{weight:10,meaning:"产业和股价是否已经透支增长预期。",data:"产业热度、估值与股价隐含增长证据。",direction:"泡沫越低分数越高；产业与股价泡沫过高会限制总分。"}
    }
  },
  type4:{
    summary:"衡量赛道空间、盈利厚度、护城河耐久度与长期估值是否形成长坡厚雪。",
    applicability:"适用于经营具有经济意义且能够形成长期增长与终局估值证据的非金融公司。",
    trigger:"证据完整、加权总分至少7分；护城河过弱或产业与股价双重泡沫会否决。",
    dimensions:{
      "4a":{weight:25,meaning:"企业可持续增长的时间和空间有多长。",data:"多年收入趋势、行业同行增长和可追溯赛道空间证据。",direction:"增长连续、行业空间充足且证据直接时分数更高。"},
      "4b":{weight:25,meaning:"盈利能力、现金质量和资本回报是否足够厚。",data:"净利率、毛利率、经营现金流转化、投入资本回报率及最新报告。",direction:"四项质量越高分数越高；最新利润或现金流恶化会封顶。"},
      "4c":{weight:20,meaning:"竞争优势能否跨周期保持。",data:"多年盈利、毛利、现金与资本回报稳定性以及可追溯护城河依据。",direction:"历史越长、波动越小且证据越直接，分数越高。"},
      "4d":{weight:15,meaning:"现价相对十年中性终局价值是否合理。",data:"当前收盘价与经校验的十年中性终局折现价值。",direction:"现价相对终局价值越低，分数越高。"},
      "4e":{weight:8,meaning:"产业是否处于不可持续的景气或资本泡沫。",data:"行业收入、利润、供需和可追溯产业泡沫证据。",direction:"产业泡沫风险越低，分数越高。"},
      "4f":{weight:7,meaning:"股价已经提前计入多少年的乐观增长。",data:"当前价格与乐观估值上沿反推的隐含增长年数。",direction:"透支年数越少，分数越高；与产业泡沫同时过高会否决。"}
    }
  },
  type5:{
    summary:"只在确认属于强周期行业后，寻找周期底部、存活能力、上行弹性和正常化估值。",
    applicability:"仅适用于商品价格、产能或金融周期能够被外部证据确认的强周期公司。",
    trigger:"强周期属性（5a）至少7分才进入本模型；其余子项证据完整后，五项加权总分至少7分即触发。抗周期能力（5c）只按20%权重进入总分，不另设5分门槛，也不存在3分否决线。",
    dimensions:{
      "5a":{weight:35,meaning:"公司是否真正具有强周期属性并接近周期低位。",data:"商品价格或产能周期、毛利率与利润波动、金融行业专属周期指标。",direction:"外部周期证据越强、低位或回升越明确，分数越高。"},
      "5b":{weight:25,meaning:"价格、估值、盈利和库存等底部信号是否共振。",data:"五年PB分位、市场冷度、财务底部与行业周期信号。",direction:"独立底部信号越多，分数越高；单一便宜信号不能确认底部。"},
      "5c":{weight:20,meaning:"公司能否安全穿越低迷周期。",data:"负债、现金、利息保障、经营现金流；金融股使用监管资本指标。",direction:"资产负债表越稳健，分数越高。"},
      "5d":{weight:10,meaning:"周期反转时盈利修复空间有多大。",data:"完整周期中的历史利润峰谷与行业产能弹性。",direction:"经完整历史确认的峰谷弹性越大，分数越高。"},
      "5e":{weight:10,meaning:"按正常化而非单年峰值利润计算是否便宜。",data:"五至十年平均利润、正常化市盈率或周期行业适用估值。",direction:"正常化估值越低，分数越高。"}
    }
  },
  type6:{
    summary:"对小市值高增长科技或困境反转公司进行高风险筛选，并强制执行仓位纪律。",
    applicability:"仅适用于模型规定的早期高增长科技、亏损或低利润率、以及小市值困境反转子类型。",
    trigger:"证据完整、加权总分至少7分，前四项至少两项达到5分，并确认单票与组合仓位上限。",
    dimensions:{
      "6a":{weight:25,meaning:"所在产业是否出现足以支持早期公司的爆发增长。",data:"剔除本公司后的行业聚合增长与样本覆盖。",direction:"产业增长越快且覆盖越充分，分数越高。"},
      "6b":{weight:20,meaning:"核心技术是否真实、可追溯且难以复制。",data:"研发、专利、产品验证和公告或报告中的直接技术证据。",direction:"只有可追溯原始资料可给正式精确分；仅凭模型间接推断时最多计4分，只用于发现待核验方向，不算核心达标。"},
      "6c":{weight:15,meaning:"商业模式是否带来可验证的单位经济或扩张优势。",data:"客户、收入模式、单位经济、复购与可追溯商业模式证据。",direction:"只有可追溯原始资料可给正式精确分；仅凭模型间接推断时最多计4分，只用于发现待核验方向，不算核心达标。"},
      "6d":{weight:25,meaning:"亏损或低迷经营是否正在发生真实反转。",data:"多年利润、净利率、现金流及最新同口径同比。",direction:"连续改善或扭亏越明确，分数越高；最新恶化会封顶。"},
      "6e":{weight:15,meaning:"高风险标的是否满足可承受的仓位纪律。",data:"用户确认的单票仓位、同类组合仓位与最坏归零损失。",direction:"这是行动确认项；未确认时不能用自动估分替代。"}
    }
  },
  type7:{
    summary:"先判断公司属于弱周期、强科技还是强周期，再用该类别自己的商业模式、护城河和长期成长公式评价优质股权。",
    applicability:"适用于非金融公司；金融股需要尚未建立的专属优质股权模型。",
    trigger:"质量认证要求三项证据完整且算术平均严格大于7.000，强周期商业模式或护城河低于5分即否决，强科技三项还必须各自不低于7分；当前买点还须通过未来自由现金流、本类别的买点条件和第七类自身价格检查，其他买入情况已触发也不能免除。弱周期和强周期要求最新自由现金流为正且连续3至5年正值占比至少60%；强科技也可由最近3年自由现金流严格逐年改善、最新经营现金流为正、按最近改善速度预计不超过2年转正来证明清晰转正路径。弱周期价格位置分至少3分；强科技近五年市净率分位不高于20%；强周期当前市净率不高于1.20且近五年分位不高于20%。其中1.20和20%是程序对“接近净资产”和“处于历史底部区”的量化定义。",
    dimensions:{
      "7a":{weight:100/3,meaning:"当前公司类别下的商业模式是否优秀。",data:"弱周期看定价权、现金转化、复购和轻资产；强科技看研发转化、收入质量、边际成本和现金流拐点；强周期看成本曲线、一体化、现金转化和产能纪律。",direction:"本类别的四个子指标按固定权重加总为商业模式分。"},
      "7b":{weight:100/3,meaning:"当前公司类别下的护城河是否持久。",data:"弱周期看品牌、转换成本、牌照和时间；强科技看专利、人才、网络和平台；强周期看资源、成本领先、区位和穿越周期。",direction:"本类别的四个子指标按固定权重加总为护城河分；强周期低于5分即否决。"},
      "7c":{weight:100/3,meaning:"当前公司类别下的长期成长是否成立。",data:"弱周期看量价、品类、通胀传导和确定性；强科技看S曲线、市场空间、非线性期权和颠覆风险；强周期看低成本扩张、一体化、商品趋势和确定性。",direction:"本类别的四个子指标按固定权重加总为长期成长分。"}
    }
  }
};

const INDEX_HTML = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>量化买入情况看板</title>
<link rel="icon" href="data:,"><style>
:root{color-scheme:light;--ink:#172033;--muted:#64748b;--line:#dbe3ef;--bg:#f5f7fb;--blue:#1d4ed8;--blue-soft:#dbeafe;--green:#166534;--amber:#92400e}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}body.drawer-open{overflow:hidden}header{background:linear-gradient(135deg,#102a63,#1d4ed8);color:#fff;padding:28px max(20px,calc((100vw - 1240px)/2)) 24px}header h1{margin:0 0 4px;font-size:clamp(24px,4vw,36px)}header p{margin:0;color:#dbeafe}.wrap{max-width:1240px;margin:auto;padding:18px 20px 48px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-bottom:16px}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:0 5px 18px #102a630c}.card small{color:var(--muted);display:block}.card strong{font-size:24px;display:block;margin-top:3px}.panel{margin-bottom:16px}.panel-title{font-size:15px;margin:0 0 10px}.filters{display:grid;grid-template-columns:2fr repeat(5,minmax(120px,1fr));gap:10px}.filters label{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:4px;min-width:0}.filters input,.filters select{border:1px solid #cbd5e1;border-radius:9px;background:#fff;padding:9px;color:var(--ink);min-width:0;width:100%;max-width:100%;font:inherit}.filter-hint{font-size:11px;line-height:1.35;color:#1d4ed8;min-height:1.35em}.filters select:disabled{background:#f1f5f9;color:#64748b}.coverage{display:grid;grid-template-columns:repeat(7,minmax(110px,1fr));gap:10px}.coverage button{background:#f8fafc;border:1px solid var(--line);text-align:left;padding:10px}.coverage button:hover{background:#eff6ff;border-color:#93c5fd}.coverage-label{display:flex;justify-content:space-between;gap:8px;font-size:12px}.coverage-track{display:block;height:7px;background:#e2e8f0;border-radius:99px;overflow:hidden;margin:7px 0 4px}.coverage-fill{display:block;height:100%;background:linear-gradient(90deg,#2563eb,#38bdf8);border-radius:inherit}.coverage-breakdown{display:grid;gap:2px;margin-top:5px}.coverage-breakdown span{font-size:11px;line-height:1.35}.coverage-triggered{color:var(--green)}.coverage-conditional{color:var(--amber)}.coverage-evidence{color:#475569}.coverage-help{margin:0 0 10px;color:var(--muted);font-size:12px}.coverage small{color:var(--muted)}button{border:0;border-radius:9px;background:#e2e8f0;color:var(--ink);padding:9px 12px;cursor:pointer;font:inherit}button:hover{background:#cbd5e1}button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible,summary:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}.meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px}.table-wrap{overflow-x:auto;overflow-y:visible;border:1px solid var(--line);border-radius:12px;overscroll-behavior-x:contain;overscroll-behavior-y:auto;touch-action:pan-x pan-y}.table{border-collapse:collapse;width:100%;min-width:960px;background:#fff}.table th,.table td{padding:10px 9px;border-bottom:1px solid #edf1f7;text-align:left;white-space:nowrap}.table th{background:#f8fafc;color:#475569;font-size:12px;position:sticky;top:0;z-index:1}.table tbody tr{cursor:pointer}.table tbody tr:hover{background:#eff6ff}.status{border-radius:999px;padding:3px 7px;font-size:12px;display:inline-block;margin:1px}.s-triggered{background:#dcfce7;color:#166534}.s-conditional,.s-observe{background:#fef3c7;color:#92400e}.s-vetoed{background:#fee2e2;color:#991b1b}.s-insufficient_evidence,.s-not_applicable,.s-not_triggered{background:#e2e8f0;color:#475569}.pager{display:flex;justify-content:space-between;align-items:center;margin-top:12px}.drawer{position:fixed;inset:0;background:#0f172a70;display:none;align-items:flex-end;justify-content:center;padding:12px;z-index:10;overflow-y:auto;overscroll-behavior-y:contain;touch-action:pan-y}.drawer.open{display:flex}.drawer-card{background:#fff;border-radius:16px 16px 0 0;max-width:960px;width:100%;max-height:calc(100dvh - 24px);overflow-y:auto;overscroll-behavior-y:contain;touch-action:pan-y;-webkit-overflow-scrolling:touch}.drawer-head{display:flex;justify-content:space-between;gap:12px;align-items:start;position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:16px 20px;z-index:4}.drawer-head h2{margin:0 0 4px}.detail-rows{padding:0 20px max(20px,env(safe-area-inset-bottom))}.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:14px 0 10px}.fact{border:1px solid var(--line);border-radius:9px;background:#f8fafc;padding:8px 10px;min-width:0}.fact small{display:block;color:var(--muted);font-size:11px}.fact strong{display:block;overflow-wrap:anywhere}.detail-help{margin:0 0 10px;color:var(--muted);font-size:13px}.type-nav{position:sticky;top:78px;z-index:3;display:flex;gap:6px;overflow-x:auto;padding:8px 0;background:#fff;border-bottom:1px solid var(--line);scrollbar-width:thin}.type-nav button{flex:0 0 auto;padding:6px 9px;background:#eff6ff;color:#1e40af}.type-row{display:grid;grid-template-columns:1fr auto;gap:5px 14px;border-top:1px solid var(--line);padding:16px 0;scroll-margin-top:132px}.type-row:first-of-type{border-top:0}.type-row>p{margin:2px 0;color:var(--muted);font-size:13px;grid-column:1/-1;white-space:pre-wrap}.type-method{grid-column:1/-1;border-left:3px solid #bfdbfe;background:#eff6ff;padding:8px 10px;border-radius:0 8px 8px 0;font-size:12px;color:#334155}.type-method strong{display:block;color:#1e3a8a}.scope-note{grid-column:1/-1;color:#475569;background:#f8fafc;border:1px solid var(--line);padding:8px 10px;border-radius:8px;font-size:12px}.model-gap{color:#991b1b;background:#fff7ed;border-color:#fdba74}.evidence-notes{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 10px;border:1px solid #dbeafe;background:#f8fbff;border-radius:8px;padding:8px 10px;font-size:12px}.evidence-note{display:grid;grid-template-columns:72px 1fr;gap:6px;min-width:0}.evidence-note strong{color:#475569}.evidence-note span{overflow-wrap:anywhere}.dimensions{grid-template-columns:1fr 1fr;gap:8px 12px;margin-top:6px;align-items:start}.dimension{border:1px solid #dbe3ef;border-radius:9px;background:#f8fafc;overflow:hidden}.dimension[open]{background:#fff;border-color:#bfdbfe}.dimension summary{cursor:pointer;list-style:none;padding:9px 10px;display:grid;grid-template-columns:1fr auto 96px;gap:2px 10px;align-items:center}.dimension summary::-webkit-details-marker{display:none}.dimension summary:before{content:"＋";grid-column:2;grid-row:1/3;color:#2563eb;font-weight:700}.dimension[open] summary:before{content:"－"}.dimension-title{font-size:12px;color:var(--muted)}.dimension-score{display:block;font-weight:700}.dimension-evidence{grid-column:1/-1;color:#475569;font-size:12px;overflow-wrap:anywhere}.dimension-body{border-top:1px solid #e2e8f0;padding:8px 10px;font-size:12px}.dimension-body dl{display:grid;grid-template-columns:76px 1fr;gap:4px 8px;margin:0}.dimension-body dt{color:var(--muted)}.dimension-body dd{margin:0;overflow-wrap:anywhere}.contribution{color:var(--green);font-weight:600}.gate-result{color:#1e40af;font-weight:600}.missing-text{color:#991b1b}.notice{color:#92400e;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:10px;margin-top:12px}
.type7-method-detail{grid-column:1/-1;border:1px solid #bfdbfe;background:#f8fbff;border-radius:10px;padding:10px;display:grid;gap:8px}
.verdict{border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:10px;grid-column:1/-1;background:#fff}
.verdict-banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.verdict-tag{font-size:15px;font-weight:700;padding:5px 12px;border-radius:999px;color:#fff}
.verdict-tag.buy{background:#16a34a}.verdict-tag.watch{background:#d97706}.verdict-tag.avoid{background:#dc2626}.verdict-tag.gap{background:#7c3aed}.verdict-tag.neutral{background:#64748b}
.verdict-note{color:var(--muted);font-size:13px}
.type-minis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.type-mini{border:1px solid var(--line);border-radius:8px;padding:8px;cursor:pointer;background:#fafbff;display:grid;gap:5px;text-align:left}
.type-mini:hover{border-color:var(--blue)}
.type-mini small{color:var(--muted);font-size:11px}
.type-mini strong{font-size:13px}
.score-bar{position:relative;height:7px;border-radius:4px;background:#e2e8f0;overflow:hidden}
.score-bar i{position:absolute;left:0;top:0;bottom:0;border-radius:4px}
.dimension .score-bar{width:96px;flex:none;margin-left:auto}
.dimension-summary{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dimension-summary .dimension-title{flex:1;min-width:120px}
.dimension-summary .dimension-evidence{flex-basis:100%}.type7-method-detail h4,.type7-method-detail h5{margin:0;color:#1e3a8a}.type7-overview,.type7-conclusion,.type7-section-help{margin:0;font-size:12px}.type7-classification{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.type7-classification .type7-atom-group{min-width:0}.type7-gates{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.type7-gate{border:1px solid var(--line);border-radius:8px;background:#fff;padding:7px;font-size:12px}.type7-gate strong,.type7-gate span{display:block}.type7-atom-group{border:1px solid var(--line);border-radius:8px;background:#fff;padding:0 8px}.type7-atom-group summary{cursor:pointer;padding:7px 0;font-weight:600}.type7-atom{display:grid;grid-template-columns:minmax(120px,1fr) auto;gap:2px 8px;border-top:1px solid #edf1f7;padding:7px 0;font-size:12px}.type7-atom small{grid-column:1/-1;color:var(--muted)}.type7-atom-detail{grid-column:1/-1;display:grid;gap:2px;color:#475569}.type7-inputs{overflow-wrap:anywhere}.type7-old-data{grid-column:1/-1;color:#991b1b;background:#fff7ed;border:1px solid #fdba74;border-radius:8px;padding:9px 10px;font-size:12px}
@media(max-width:1000px){.coverage{grid-template-columns:repeat(4,1fr)}}
@media(max-width:850px){.filters{grid-template-columns:1fr 1fr}.filters label:first-child{grid-column:1/-1}.dimensions,.evidence-notes{grid-template-columns:1fr}.type7-classification,.type7-gates{grid-template-columns:1fr}}
@media(max-width:720px){header{padding:22px 16px 19px}.wrap{padding:14px 12px 36px}.cards{grid-template-columns:repeat(2,1fr);gap:8px}.card,.panel{padding:12px;border-radius:12px}.coverage{grid-template-columns:1fr 1fr}.filters{grid-template-columns:1fr}.filters label:first-child{grid-column:auto}.filters input,.filters select{font-size:16px}.table{min-width:0}.table th:nth-child(3),.table td:nth-child(3),.table th:nth-child(4),.table td:nth-child(4),.table th:nth-child(7),.table td:nth-child(7){display:none}.table th,.table td{padding:9px 7px}.table th:nth-child(6),.table td:nth-child(6){white-space:normal}.drawer{padding:0}.drawer-card{max-height:100dvh;border-radius:16px 16px 0 0}.drawer-head{padding:12px 14px}.drawer-head h2{font-size:19px}.detail-rows{padding:0 14px max(18px,env(safe-area-inset-bottom))}.facts{grid-template-columns:1fr 1fr}.type-nav{top:70px}.dimensions{grid-template-columns:1fr}.type-row{scroll-margin-top:124px}.pager{gap:8px}.pager button{padding:8px 10px}}
</style></head><body><header id="pageHeader"><h1>量化买入情况看板</h1><p>沪深市场七类量化买入情况</p></header><main class="wrap" id="mainContent"><section class="cards" id="cards"></section><section class="panel"><div class="meta" id="meta">正在读取数据…</div><div class="notice" id="notice" hidden></div></section><section class="panel"><h2 class="panel-title">七类命中、待确认与资料缺口分布</h2><p class="coverage-help">“已触发”才是实际信号；“待确认”须满足附加条件；“资料缺口”包含仍有可核验资料缺口的公司，其中“结论待定”和“补齐后仍可能触发”是更小的子集，均不是买入信号。</p><div class="coverage" id="coverage"></div></section><section class="panel"><div class="filters"><label>代码/名称<input id="q" autocomplete="off" inputmode="search" placeholder="例如 600519 或 贵州茅台"></label><label>市场<select id="market"><option value="">全部市场</option><option value="SH">沪市</option><option value="SZ">深市</option></select></label><label>买入类型<select id="type"><option value="">全部七类</option></select></label><label id="statusLabel">当前类型状态<select id="status"><option value="triggered">实际命中</option><option value="">全部适用状态</option></select><small class="filter-hint" id="statusHint" aria-live="polite"></small></label><label>行业<select id="industry"><option value="">全部行业</option></select></label><label>排序<select id="sort"><option value="score">当前类型/诊断分高到低</option><option value="name">名称</option><option value="code">代码</option></select></label></div></section><section class="panel" id="resultsPanel"><div class="meta" id="resultMeta" tabindex="-1" aria-live="polite"></div><div class="table-wrap"><table class="table"><thead><tr><th>代码</th><th>名称</th><th>市场</th><th>行业</th><th id="scoreHeading">综合诊断分</th><th id="typeHeading">主要买入情况</th><th id="statusHeading">七类状态</th></tr></thead><tbody id="rows"></tbody></table></div><div class="pager"><span id="pageInfo" aria-live="polite"></span><span><button id="prev">上一页</button> <button id="next">下一页</button></span></div></section></main><div class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="detailTitle"><article class="drawer-card" id="drawerCard"><div class="drawer-head"><div><h2 id="detailTitle"></h2><div class="meta" id="detailMeta"></div></div><button id="close">关闭</button></div><div class="detail-rows" id="detailRows"></div></article></div><script>
const TYPE_NAMES={type1:"1️⃣ 估值买入区",type2:"2️⃣ 两热一冷",type3:"3️⃣ 可持续高增长",type4:"4️⃣ 长坡厚雪",type5:"5️⃣ 强周期底部",type6:"6️⃣ 高风险早期/困境型",type7:"7️⃣ 优质股权型"};
const STATUS_NAMES={triggered:"已触发",conditional:"待确认",observe:"观察",insufficient_evidence:"资料不足",evidence_gap:"有资料缺口",vetoed:"不符合硬条件",not_triggered:"未触发",not_applicable:"不适用",blocked:"市场状态阻断"};
const TYPE_DIMENSIONS={type1:[["1a","买入区深度"],["1b","价值陷阱排查"],["1c","安全边际厚度"],["1d","催化剂/回归动力"]],type2:[["2a","产业周期热度"],["2b","公司周期拐点"],["2c","市场周期冷度"],["2d","估值合理性"]],type3:[["3a","护城河支撑度"],["3b","增长质量"],["3c","资本回报率"],["3d","增长可持续性"],["3e","产业/股价泡沫"]],type4:[["4a","坡的长度"],["4b","雪的厚度"],["4c","护城河耐久度"],["4d","估值合理性"],["4e","产业泡沫防范"],["4f","股价泡沫防范"]],type5:[["5a","强周期属性"],["5b","底部信号"],["5c","抗周期能力"],["5d","上行弹性"],["5e","正常化盈利估值"]],type6:[["6a","产业爆发"],["6b","技术壁垒"],["6c","模式创新"],["6d","困境反转"],["6e","仓位风控"]],type7:[["7a","本类别的商业模式"],["7b","本类别的护城河"],["7c","本类别的长期成长"]]};
const METHODOLOGY=__QUANT_METHODOLOGY_JSON__;
const METHODOLOGY_VERSION="__QUANT_METHODOLOGY_VERSION__";
const METHODOLOGY_LABEL="__QUANT_METHODOLOGY_LABEL__";
const CATALOGUE_INDEX_CONTRACT_VERSION=__CATALOGUE_INDEX_CONTRACT_VERSION__;
const DECISION_BASIS_NAMES={full_evidence:"证据完整",scope_exclusion:"超出适用范围",confirmed_veto:"已有证据足以否决",conservative_upper_bound:"即使缺失项取最高分也不会触发",action_condition:"等待仓位确认",market_block:"交易状态阻断",unresolved_missing_evidence:"缺失资料仍可能改变结论"};
const EVIDENCE_META_NAMES={_scope:"适用边界",_veto:"否决原因",_missing:"待补资料",_condition:"附加条件",_downgrade:"降级原因",_risk:"风险说明",_blocked:"交易状态",_adjustment:"样本调整",_coverage:"数据覆盖",_profile:"公司画像",_score_quality:"评分质量",_4f_formula:"股价透支计算"};
const $=id=>document.getElementById(id);const pageSize=50;let data=[],page=0,generationId="",marketAsOf="",sourceVersion="",dimensionScoresAvailable=false,dimensionEstimatesAvailable=false,type7MethodDetailAvailable=false,returnFocus=null,composing=false,renderFrame=0,activeDetailRequest=0,activeDetailCode="",detailAbort=null;const detailCache=new Map();
function market(code){return String(code).startsWith("6")?"沪市":"深市"}
function score(row,type=""){const result=type?row.types?.[type]:null;const exact=finiteNumber(type?result?.score:row.diagnostic_score);if(exact!==null)return exact;const diagnostic=type==="type6"?finiteNumber(result?.diagnostic_score):null;if(diagnostic!==null)return diagnostic;const upper=finiteNumber(result?.score_upper_bound??result?.decision?.score_upper_bound);return type&&upper!==null?upper:-1}
function scoreLabel(typeKey,typeResult){if(typeResult===undefined){typeResult=typeKey;typeKey=typeResult?.diagnostic_score!==undefined?"type6":""}if(!typeResult||typeResult.status==="not_applicable")return"";if(typeKey==="type7"&&!Object.prototype.hasOwnProperty.call(typeResult,"quality_complete"))return"旧数据待刷新";const digits=typeKey==="type7"?3:1,hasMissing=typeResult.has_missing_dimensions===true||(typeResult.decision?.missing_dimensions||[]).length>0;const exact=finiteNumber(typeResult.score);if(exact!==null&&!hasMissing)return exact.toFixed(digits)+"分";const diagnostic=typeKey==="type6"?finiteNumber(typeResult.diagnostic_score):null;if(diagnostic!==null)return"模型诊断 "+diagnostic.toFixed(1)+"分（未确认仓位，不构成信号）";const lower=finiteNumber(typeResult.score_lower_bound??typeResult.decision?.score_lower_bound),upper=finiteNumber(typeResult.score_upper_bound??typeResult.decision?.score_upper_bound);if(lower!==null&&upper!==null&&lower<=upper)return Math.abs(upper-lower)<.05?upper.toFixed(digits)+"分":"参考范围 "+lower.toFixed(digits)+"–"+upper.toFixed(digits)+"分";return"资料未齐，暂不显示精确分"}
function investorActionDimensions(typeKey,value){if(typeKey!=="type6")return new Set();const declared=Array.isArray(value?.investor_action_dimensions)?value.investor_action_dimensions.filter(dimension=>dimension==="6e"):[],missing=Array.isArray(value?.decision?.missing_dimensions)?value.decision.missing_dimensions:[],legacy=value?.action_required==="position_confirmation"&&missing.includes("6e")?["6e"]:[];return new Set([...declared,...legacy])}
function formatBeijing(value){const parsed=new Date(String(value||""));if(!Number.isFinite(parsed.getTime()))return"—";return new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(parsed).replaceAll("/","-")+"（北京时间）"}
function fillOptions(){for(const [k,v] of Object.entries(TYPE_NAMES)){const o=document.createElement("option");o.value=k;o.textContent=v;$("type").append(o)}for(const [k,v] of Object.entries(STATUS_NAMES)){if([...$("status").options].some(o=>o.value===k))continue;const o=document.createElement("option");o.value=k;o.textContent=v;$("status").append(o)}for(const v of [...new Set(data.map(r=>r.industry).filter(Boolean))].sort((a,b)=>a.localeCompare(b,"zh-CN"))){const o=document.createElement("option");o.value=v;o.textContent=v;$("industry").append(o)}}
function typeStatusMatches(typeState,status){return status==="evidence_gap"?typeState?.has_evidence_gap===true:typeState?.status===status}
function rowMatches(r,{q,m,t,s,i}){const typeState=t?(r.types?.[t]||null):null;const typeMatches=!t||(typeState&&(q||s?(!s||typeStatusMatches(typeState,s)):typeState.status!=="not_applicable"));const statusMatches=!s||t||Object.values(r.types||{}).some(value=>typeStatusMatches(value,s));return(!q||r._search.includes(q))&&(!m||r._market===m)&&(!i||r.industry===i)&&typeMatches&&statusMatches}
function filtered(){const q=$("q").value.trim().toLowerCase(),m=$("market").value,t=$("type").value,s=q?"":$("status").value,i=$("industry").value;const out=data.filter(r=>rowMatches(r,{q,m,t,s,i}));out.sort((a,b)=>$("sort").value==="name"?String(a.name).localeCompare(String(b.name),"zh-CN"):$("sort").value==="code"?String(a.code).localeCompare(String(b.code)):score(b,t)-score(a,t));return out}
function badge(status,text="",typeKey=""){const span=document.createElement("span");span.className="status s-"+status;let label=text||STATUS_NAMES[status]||"资料异常";if(typeKey==="type5"&&["observe","not_triggered","conditional"].includes(status))label=(text?"5️⃣ ":"")+"适用·谨慎相位";span.textContent=label;return span}
function coverageEvidenceStats(record,indexRecord={}){return{evidenceMissing:Number(indexRecord?.evidence_missing??record?.insufficient_evidence??0),potentiallyTriggerable:Number(indexRecord?.potentially_triggerable??0),decisionUnresolved:Number(indexRecord?.decision_unresolved??0),actionConfirmation:Number(indexRecord?.action_confirmation??0)}}
function conditionalCoverageLabel(typeKey,conditional,actionConfirmation){const confirmed=Math.max(0,Math.min(conditional,actionConfirmation)),other=Math.max(0,conditional-confirmed);if(typeKey==="type6"&&confirmed>0&&other>0)return"待确认仓位 "+confirmed.toLocaleString()+" 家；待满足其它条件 "+other.toLocaleString()+" 家";if(typeKey==="type6"&&confirmed>0)return"待确认仓位 "+confirmed.toLocaleString()+" 家";return"待满足附加条件 "+conditional.toLocaleString()+" 家"}
function renderCoverage(summary,indexCoverage={}){
  const total=Number(summary.company_count||data.length)||1,coverage=summary.type_coverage||{};
  const nodes=Object.entries(TYPE_NAMES).map(([key,name])=>{
    const record=coverage[key]||{},evidence=coverageEvidenceStats(record,indexCoverage?.[key]);
    const triggered=Number(record.triggered||0),conditional=Number(record.conditional||0),qualityCertified=key==="type7"?Number(summary.type7_quality_certified_company_count??record.quality_certified??0):0,visibleCount=triggered+conditional;
    const conditionalLabel=conditionalCoverageLabel(key,conditional,evidence.actionConfirmation),qualityLabel=key==="type7"?"质量已达标 "+qualityCertified.toLocaleString()+" 家":"",evidenceLabel="资料缺口 "+evidence.evidenceMissing.toLocaleString()+" 家（其中结论待定 "+evidence.decisionUnresolved.toLocaleString()+" 家，其中补齐后仍可能触发 "+evidence.potentiallyTriggerable.toLocaleString()+" 家）";
    const preferredStatus=triggered>0?"triggered":conditional>0?"conditional":evidence.evidenceMissing>0?"evidence_gap":"";
    const button=document.createElement("button");button.type="button";button.dataset.type=key;button.dataset.status=preferredStatus;button.setAttribute("aria-label",name+"，已触发 "+triggered+" 家，"+(qualityLabel?qualityLabel+"，":"")+conditionalLabel+"，"+evidenceLabel);
    const label=document.createElement("span");label.className="coverage-label coverage-triggered";label.append(document.createTextNode(name.replace(/^[^ ]+ /,"")),document.createTextNode(triggered.toLocaleString()+" 家已触发"));
    const track=document.createElement("span"),fill=document.createElement("span");track.className="coverage-track";fill.className="coverage-fill";fill.style.width=Math.max(0,Math.min(100,visibleCount/total*100))+"%";if(visibleCount===0)fill.hidden=true;track.append(fill);
    const breakdown=document.createElement("span");breakdown.className="coverage-breakdown";
    if(qualityLabel){const qualityText=document.createElement("span");qualityText.className="coverage-triggered";qualityText.textContent=qualityLabel;breakdown.append(qualityText)}
    const conditionalText=document.createElement("span");conditionalText.className="coverage-conditional";conditionalText.textContent=conditionalLabel;
    const evidenceText=document.createElement("span");evidenceText.className="coverage-evidence";evidenceText.textContent=evidenceLabel;breakdown.append(conditionalText,evidenceText);button.append(label,track,breakdown);return button
  });
  $("coverage").replaceChildren(...nodes)
}
function syncSearchStatus(){const searching=$("q").value.trim().length>0;$("status").disabled=searching;$("statusHint").textContent=searching?"代码/名称搜索会跨全部状态，状态筛选暂时停用。":""}
function render(){syncSearchStatus();const t=$("type").value,out=filtered(),pages=Math.max(1,Math.ceil(out.length/pageSize));page=Math.min(page,pages-1);const slice=out.slice(page*pageSize,(page+1)*pageSize);$("scoreHeading").textContent=t?TYPE_NAMES[t]+"分数或范围":"综合诊断分";$("typeHeading").textContent=t?"当前类型状态":"主要买入情况";$("statusHeading").textContent=t?"查看明细":"七类状态";const fragment=document.createDocumentFragment();for(const r of slice){const tr=document.createElement("tr");tr.tabIndex=0;tr.dataset.code=r.code;tr.setAttribute("aria-label","查看 "+r.name+" "+r.code+" 的七类明细");const displayedScore=t?(scoreLabel(t,r.types?.[t])||"—"):(score(r)>=0?score(r).toFixed(1):"—");for(const value of [r.code,r.name,r._market==="SH"?"沪市":"深市",r.industry||"—",displayedScore]){const td=document.createElement("td");td.textContent=value;tr.append(td)}const td=document.createElement("td");if(t){td.append(badge(r.types?.[t]?.status||"invalid","",t))}else{td.textContent=r.primary_label||"—"}tr.append(td);const statuses=document.createElement("td");if(t){statuses.textContent="点击查看完整依据"}else{for(const k of Object.keys(TYPE_NAMES)){const v=r.types?.[k];if(v)statuses.append(badge(v.status,k.slice(4)+" "+(STATUS_NAMES[v.status]||"异常"),k))}}tr.append(statuses);fragment.append(tr)}$("rows").replaceChildren(fragment);const hasQuery=$("q").value.trim().length>0;if(out.length)$("resultMeta").textContent="筛选后 "+out.length.toLocaleString()+" 家，当前显示 "+slice.length+" 家；"+(hasQuery?"代码/名称搜索已跨全部状态。":"")+"点击公司查看七类完整明细。";else if(hasQuery)$("resultMeta").textContent="已跨全部状态搜索，但没有找到匹配该代码或名称的公司。";else if(t&&$("status").value==="triggered")$("resultMeta").textContent=TYPE_NAMES[t]+"当前确实为 0 家命中；可切换状态查看未触发原因。";else $("resultMeta").textContent="当前筛选条件没有公司，请调整市场、行业或状态。";$("pageInfo").textContent="第 "+(page+1)+" / "+pages+" 页";$("prev").disabled=page===0;$("next").disabled=page>=pages-1}
function scheduleRender(){cancelAnimationFrame(renderFrame);renderFrame=requestAnimationFrame(()=>{page=0;render()})}
function changePage(delta){page+=delta;render();$("resultsPanel").scrollIntoView({block:"start"});$("resultMeta").focus({preventScroll:true})}
function finiteNumber(value){if(value===null||value===undefined||value===""||typeof value==="boolean")return null;const number=Number(value);return Number.isFinite(number)?number:null}
function metricText(value,digits=2){const number=finiteNumber(value);return number===null?"—":number.toFixed(digits)}
function marketCapText(value){const number=finiteNumber(value);return number!==null&&number>0?(number/100000000).toLocaleString("zh-CN",{maximumFractionDigits:1})+"亿元":"—"}
function publicReasonText(value){
  let text=String(value||"").trim();if(!text)return"";
  const exact={missing_quote:"未取得有效行情",invalid_price_or_market_cap:"当前价格或总市值无效",invalid_derived_shares:"无法根据价格和总市值推算有效股本",valuation_returned_non_mapping:"估值模型未返回可用结果",valuation_evidence_invalid:"估值结果未通过源数据一致性核验",mixed_profit_cycle_unsupported_by_fcff:"当前盈利周期不适合使用现金流估值",nonpositive_pessimistic_equity_value:"悲观情景下的股权价值不为正",internal_error:"计算过程发生内部异常，相关结果未被采用",source_missing:"所需源数据暂时缺失",inconsistent_source:"不同来源的数据存在矛盾",model_unsupported:"当前估值模型暂不支持该公司",economic_not_applicable:"当前经济条件不适用该估值模型",derived_proxy:"根据财务表现间接判断"};
  const lowered=text.toLowerCase();if(exact[lowered])return exact[lowered];if(lowered.startsWith("valuation_exception:"))return"估值计算发生内部异常，相关结果未被采用";
  for(const [legacy,readable] of [["第1模板","长期质量与回报评分"],["第5模板","产业质量与估值评分"],["补丁5安全边际","安全边际"],["补丁5","商业质量与安全边际评分"],["补丁6","七类型规则"],["模板25","金融公司估值方法"],["投入回报增长模板","可持续高增长型"],["小盘高风险模板","小盘高风险型"],["增长模板","增长型"]])text=text.replaceAll(legacy,readable);
  const internal=/(?:\\bpatch\\d+(?:[-_][a-z0-9]+)+\\b|\\btype[1-7](?:[a-z0-9_-]*)\\b|\\b(?:model_id|schema_version|derived_proxy|reported_formula|formula_version|validation_status|source_rule|evidence_level)\\b|\\b(?:model|schema|formula|proxy|validation|evidence)\\s*=|\\b(?:[a-z][a-z0-9]*_){1,}[a-z0-9]+\\b|\\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}-v\\d+\\b|\\b[a-z_][a-z0-9_]*\\s*[-+*/><=]\\s*[a-z_(][a-z0-9_(]*|\\b(?:BM|MOAT|PRECONDITION|VALUATION)\\b)/i;
  text=text.replace(/\\s*[（(]\\s*(?:证据|evidence)\\s*[:：][^）)]*[）)]/gi,note=>internal.test(note)?"":note).trim().replace(/[；;，,]+$/,"");
  if(!internal.test(text))return text;
  const readableSegments=text.split(/[；;\\n]+/).map(segment=>segment.trim()).filter(segment=>segment&&!internal.test(segment));if(readableSegments.length)return readableSegments.join("；");
  const firstInternal=text.match(internal),prefix=firstInternal&&firstInternal.index>0?text.slice(0,firstInternal.index).replace(/[（(【\\s:=：；;，,]+$/,"").replace(/(?:证据|evidence)$/i,"").trim():"";if(/[\\u3400-\\u9fff]/.test(prefix))return prefix;
  if(/type2c/i.test(text)||text.includes("量价"))return"量价与换手数据";return"可核验的财务与行业数据";
}
function publicClassName(value){const text=String(value||"").trim();return({C:"强周期",T:"强科技",N:"弱周期",W:"弱周期"})[text]||text}
function cleanedEstimateReason(value){return publicReasonText(String(value||"").replace(/^未确认估算，不用于触发[；;]?/,"").trim())}
function addFact(box,label,value){const item=document.createElement("div");item.className="fact";const key=document.createElement("small");key.textContent=label;const text=document.createElement("strong");text.textContent=value;item.append(key,text);box.append(item)}
function addDefinition(list,label,value,className=""){const term=document.createElement("dt");term.textContent=label;const detail=document.createElement("dd");detail.textContent=value;if(className)detail.className=className;list.append(term,detail)}
function setBackgroundInert(inert){for(const id of ["pageHeader","mainContent"]){const node=$(id);if(inert)node.setAttribute("inert","");else node.removeAttribute("inert")}}
function drawerFocusable(){return [...$("drawer").querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])')].filter(node=>node.getClientRects().length>0)}
function trapDrawerFocus(event){if(event.key!=="Tab"||!$("drawer").classList.contains("open"))return;const focusable=drawerFocusable();if(!focusable.length){event.preventDefault();$("close").focus();return}const first=focusable[0],last=focusable[focusable.length-1],active=document.activeElement;if(event.shiftKey&&(active===first||!$("drawer").contains(active))){event.preventDefault();last.focus()}else if(!event.shiftKey&&(active===last||!$("drawer").contains(active))){event.preventDefault();first.focus()}}
function openDrawer(title){if(!$("drawer").classList.contains("open"))returnFocus=document.activeElement;$("detailTitle").textContent=title;$("detailMeta").textContent="正在读取完整明细…";$("detailRows").textContent="";$("drawerCard").scrollTop=0;$("drawer").classList.add("open");$("drawer").setAttribute("aria-busy","true");document.body.classList.add("drawer-open");setBackgroundInert(true);$("close").focus()}
function closeDrawer(){activeDetailRequest++;activeDetailCode="";if(detailAbort){detailAbort.abort();detailAbort=null}$("drawer").classList.remove("open");$("drawer").removeAttribute("aria-busy");document.body.classList.remove("drawer-open");setBackgroundInert(false);const target=returnFocus;returnFocus=null;if(target&&target.isConnected&&typeof target.focus==="function")target.focus()}
function isModelCoverageGap(typeKey,value,method){if(value?.status!=="not_applicable")return false;const explanation=[value?.reason,value?.reasons?._scope,method?.applicability].filter(Boolean).join("；");return typeKey==="type7"&&/金融/.test(explanation)||/(尚未建立|暂无|缺少|未覆盖|未支持).{0,12}(专属)?模型|专属.{0,12}模型.{0,8}(尚未建立|暂无|缺少)/.test(explanation)}
function renderType7MethodDetail(value){
  const method=value?.method_detail;if(!method)return null;
  const box=document.createElement("details");box.className="type7-method-detail";
  const heading=document.createElement("summary");heading.textContent="第七类完整量化明细";box.append(heading);
  if(method.status==="outdated"){
    const warning=document.createElement("p");warning.className="type7-conclusion missing-text";warning.textContent=String(method.conclusion||"数据版本过旧，请刷新。");box.append(warning);return box;
  }
  let built=false;
  const scoreOrRange=(exact,lower,upper)=>{const score=finiteNumber(exact),low=finiteNumber(lower),high=finiteNumber(upper);if(score!==null)return score.toFixed(3)+"分";if(low!==null&&high!==null)return low.toFixed(3)+"至"+high.toFixed(3)+"分（资料未齐，仅表示范围）";return"资料不足"};
  const overview=document.createElement("p");overview.className="type7-overview";
  const secondary=Array.isArray(method.secondary_features)?method.secondary_features.map(publicClassName).filter(Boolean):[],possibleSecondary=Array.isArray(method.possible_secondary_features)?method.possible_secondary_features.map(publicClassName).filter(Boolean):[],possibleClasses=Array.isArray(method.possible_classifications)?method.possible_classifications.map(publicClassName).filter(Boolean):[];
  const classText=(method.classification_complete===true?"确定归类：":"暂定归类：")+(publicClassName(method.classification)||"资料不足")+(method.classification_complete!==true&&possibleClasses.length?"；仍可能归为"+possibleClasses.join("或"):"");
  const stateText="质量认证："+(method.quality_certified===true?"已达标":method.quality_complete===true?"未达标":"待补资料")+"；当前买点："+(method.buy_ready===true?"已满足":"未满足");
  overview.textContent=classText+(secondary.length?"；兼具"+secondary.join("、")+"特征":"")+(possibleSecondary.length?"；另有待确认的"+possibleSecondary.join("、")+"特征":"")+"；三项算术平均："+scoreOrRange(method.mean_score,method.mean_lower_bound,method.mean_upper_bound)+"；"+stateText+"。";box.append(overview);
  const buildContent=()=>{
  const scoreOutOf=(exact,lower,upper,maximum)=>{const score=finiteNumber(exact),low=finiteNumber(lower),high=finiteNumber(upper),max=finiteNumber(maximum);if(score!==null&&max!==null)return score.toFixed(2)+" / "+max.toFixed(2)+"分";if(low!==null&&high!==null&&max!==null)return low.toFixed(2)+"至"+high.toFixed(2)+" / "+max.toFixed(2)+"分（资料未齐）";return"资料不足"};
  const classificationScores=Array.isArray(method.classification_scores)?method.classification_scores:[];
  if(classificationScores.length){
    const classificationHeading=document.createElement("h5");classificationHeading.textContent="公司类别是怎样算出来的";
    const classificationHelp=document.createElement("p");classificationHelp.className="type7-section-help";classificationHelp.textContent="下面同时列出强周期、强科技和弱周期特征的总分及各自四项依据。带有“间接判断”的项目会明确说明使用了行业资料还是财务表现。";
    const classificationBox=document.createElement("div");classificationBox.className="type7-classification";
    for(const route of classificationScores){
      const group=document.createElement("details");group.className="type7-atom-group";group.open=true;
      const title=document.createElement("summary");title.textContent=(publicClassName(route?.name)||"公司类别")+"："+scoreOutOf(route?.score,route?.score_lower_bound,route?.score_upper_bound,route?.max_score)+(route?.selected===true?" · 当前采用":"");group.append(title);
      const interpretation=document.createElement("p");interpretation.className="type7-section-help";interpretation.textContent=String(route?.interpretation||"");group.append(interpretation);
      for(const component of Array.isArray(route?.items)?route.items:[]){
        const row=document.createElement("div");row.className="type7-atom";
        const name=document.createElement("span");name.textContent=String(component?.name||"分类项目");
        const scoreText=document.createElement("strong");scoreText.textContent=scoreOutOf(component?.score,component?.score_lower_bound,component?.score_upper_bound,component?.max_score);
        const detail=document.createElement("div");detail.className="type7-atom-detail"+(component?.complete===true?"":" missing-text");
        const proxyKinds=[];if(component?.uses_industry_proxy===true)proxyKinds.push("使用行业资料间接判断");if(component?.uses_financial_proxy===true)proxyKinds.push("使用财务或经营结果间接判断");
        const basis=document.createElement("span");basis.textContent="资料性质："+String(component?.evidence_basis||"待核验资料")+(proxyKinds.length?"（"+proxyKinds.join("；")+"）":"（直接计算或直接资料）");
        const explanation=document.createElement("span");explanation.textContent="实际依据："+String(component?.evidence_explanation||"暂无说明");detail.append(basis,explanation);
        const missingInputs=Array.isArray(component?.missing_inputs)?component.missing_inputs.map(String).filter(Boolean):[];if(missingInputs.length){const missing=document.createElement("span");missing.textContent="仍缺资料："+missingInputs.join("、");detail.append(missing)}
        row.append(name,scoreText,detail);group.append(row)
      }
      classificationBox.append(group)
    }
    box.append(classificationHeading,classificationHelp,classificationBox)
  }
  const gates=document.createElement("div");gates.className="type7-gates";
  for(const gate of Array.isArray(method.gates)?method.gates:[]){const item=document.createElement("div");item.className="type7-gate";const name=document.createElement("strong");name.textContent=String(gate?.name||"前置检查");const status=document.createElement("span");status.className=gate?.status==="通过"?"gate-result":gate?.status==="未通过"?"missing-text":"";status.textContent=String(gate?.status||"待补资料");const detail=document.createElement("small");detail.textContent=String(gate?.detail||"暂无补充说明");item.append(name,status,detail);gates.append(item)}
  if(gates.childElementCount)box.append(gates);
  for(const dimension of Array.isArray(method.dimensions)?method.dimensions:[]){
    const group=document.createElement("details");group.className="type7-atom-group";
    const title=document.createElement("summary");const coverage=finiteNumber(dimension?.coverage);title.textContent=String(dimension?.name||"本类别三项")+"："+scoreOrRange(dimension?.score,dimension?.score_lower_bound,dimension?.score_upper_bound)+" · 证据覆盖"+(coverage===null?"未知":(coverage*100).toFixed(1)+"%");group.append(title);
    for(const atom of Array.isArray(dimension?.items)?dimension.items:[]){
      const row=document.createElement("div");row.className="type7-atom";
      const name=document.createElement("span"),weight=finiteNumber(atom?.weight_percent);name.textContent=String(atom?.name||"未命名子指标")+(weight===null?"":"（权重"+weight.toFixed(weight%1?1:0)+"%）");
      const scoreText=document.createElement("strong");scoreText.textContent=scoreOrRange(atom?.score,atom?.score_lower_bound,atom?.score_upper_bound);
      const detail=document.createElement("div");detail.className="type7-atom-detail"+(atom?.complete===true?"":" missing-text");
      const evidence=document.createElement("span"),adjustment=String(atom?.adjustment_note||"");evidence.textContent="资料性质："+String(atom?.evidence||"待核验资料")+(adjustment?"；"+adjustment:"");detail.append(evidence);
      const calculation=document.createElement("span");calculation.textContent="计算规则："+String(atom?.calculation||"暂无公开计算规则");detail.append(calculation);
      const inputs=Array.isArray(atom?.inputs)?atom.inputs:[];if(inputs.length){const inputLine=document.createElement("span");inputLine.className="type7-inputs";inputLine.textContent="实际计算数据："+inputs.map(input=>String(input?.name||"输入")+"="+String(input?.value??"待补")).join("；");detail.append(inputLine)}
      const missingInputs=Array.isArray(atom?.missing_inputs)?atom.missing_inputs.map(String).filter(Boolean):[];if(missingInputs.length){const missing=document.createElement("span");missing.textContent="仍缺资料："+missingInputs.join("、");detail.append(missing)}
      const contribution=finiteNumber(atom?.weighted_contribution),cap=finiteNumber(atom?.score_cap);if(contribution!==null||cap!==null){const limits=document.createElement("span");limits.textContent=(contribution!==null?"加权贡献："+contribution.toFixed(3)+"分":"")+(contribution!==null&&cap!==null?"；":"")+(cap!==null?"仅使用间接资料时，本项最多计"+cap.toFixed(1)+"分":"");detail.append(limits)}
      row.append(name,scoreText,detail);group.append(row)
    }
    box.append(group);
  }
  if(Array.isArray(method.failures)&&method.failures.length){const failures=document.createElement("p");failures.className="type7-conclusion missing-text";failures.textContent="未通过项目："+method.failures.map(String).join("；");box.append(failures)}
  const conclusion=document.createElement("p");conclusion.className="type7-conclusion";conclusion.textContent=String(method.conclusion||"暂无结论说明");box.append(conclusion);
  };
  box.addEventListener("toggle",()=>{if(box.open&&!built){built=true;buildContent()}});
  return box;
}
function scoreBar(scoreValue,{tone=null,heightClass=""}={}){
  const bar=document.createElement("span");bar.className="score-bar"+(heightClass?" "+heightClass:"");
  const fill=document.createElement("i");
  const value=finiteNumber(scoreValue);
  if(value!==null){
    const pct=Math.max(0,Math.min(100,value*10));
    fill.style.width=pct.toFixed(1)+"%";
    const toneColor=tone||(value>=7?"#16a34a":value>=5?"#d97706":"#dc2626");
    fill.style.background=toneColor;
  }
  bar.append(fill);return bar;
}
function renderVerdict(r){
  const wrap=document.createElement("div");wrap.className="verdict";
  const banner=document.createElement("div");banner.className="verdict-banner";
  const types=r.types||{};
  const order=Object.keys(TYPE_NAMES);
  const rows=order.map(key=>[key,types[key]||{}]).filter(([,v])=>v&&v.status);
  const statusOf=key=>(types[key]||{}).status||"";
  const triggered=rows.filter(([,v])=>v.status==="triggered");
  const conditional=rows.filter(([,v])=>v.status==="conditional");
  const vetoed=rows.filter(([,v])=>v.status==="vetoed");
  const insufficient=rows.filter(([,v])=>v.status==="insufficient_evidence");
  const observed=rows.filter(([,v])=>v.status==="observe");
  const names=keys=>keys.map(([key])=>(TYPE_NAMES[key]||key).replace(/^[^\s]+\s+/,"").trim()).join("、");
  let tagText,tone,note;
  if(triggered.length){
    tagText="可买候选";tone="buy";
    note="已触发"+(triggered.length>1?""+triggered.length+"类":"")+"："+names(triggered)+(vetoed.length?"；另有否决项："+names(vetoed)+"，请先阅读否决原因。":"。点击下方类型卡片可查看触发依据与买点。");
  }else if(conditional.length){
    tagText="待确认";tone="watch";
    note="满足前置条件但还需确认："+names(conditional)+"。点击对应类型查看附加条件与仓位确认要求。";
  }else if(vetoed.length&&!insufficient.length&&!observed.length){
    tagText="不建议";tone="avoid";
    note="硬性否决："+names(vetoed)+"。系统明确排除，即使其他维度表现良好也不构成买入信号。";
  }else if(insufficient.length){
    tagText="资料不足";tone="gap";
    note="仍有资料缺口"+(insufficient.length>1?"（"+insufficient.length+"类）":"")+"："+names(insufficient)+(observed.length?"；另有观察项："+names(observed):"")+"。补齐可核验资料前无法给出可靠结论。";
  }else if(observed.length){
    tagText="观察";tone="neutral";
    note="处于观察区："+names(observed)+"，未达到买入条件。";
  }else{
    tagText="观察";tone="neutral";
    note="当前七类均未触发，也无否决项。";
  }
  const tag=document.createElement("span");tag.className="verdict-tag "+tone;tag.textContent=tagText;
  banner.append(tag);
  const noteEl=document.createElement("span");noteEl.className="verdict-note";noteEl.textContent=note;
  banner.append(noteEl);wrap.append(banner);
  const minis=document.createElement("div");minis.className="type-minis";
  for(const [key,name] of Object.entries(TYPE_NAMES)){
    const v=types[key]||{};const button=document.createElement("button");button.type="button";button.className="type-mini";
    button.dataset.detailType=key;
    const label=document.createElement("small");label.textContent=name;
    const strong=document.createElement("strong");strong.append(badge(v.status||"invalid","",key));
    const scoreText=scoreLabel(key,v);
    if(scoreText){const scoreEl=document.createElement("span");scoreEl.textContent=scoreText;strong.append(document.createTextNode(" "+scoreText))}
    const bar=scoreBar(v.score);
    button.append(label,strong,bar);
    minis.append(button);
  }
  wrap.append(minis);
  wrap.addEventListener("click",event=>{const mini=event.target.closest("button[data-detail-type]");if(!mini)return;const target=$("detail-"+mini.dataset.detailType);if(target)$("drawerCard").scrollTo({top:Math.max(0,target.offsetTop-124),behavior:"smooth"})});
  return wrap;
}
function renderDetail(r){
  $("detailTitle").textContent=r.name+"（"+r.code+"）";
  $("detailMeta").textContent=market(r.code)+" · "+(r.industry||"行业未知")+" · 综合诊断分 "+(score(r)>=0?score(r).toFixed(1):"—")+" · 数据日期 "+(marketAsOf||"—");
  const box=$("detailRows"),fragment=document.createDocumentFragment();
  const facts=document.createElement("div");facts.className="facts";
  const priceText=metricText(r.price);addFact(facts,"收盘价",priceText==="—"?priceText:"¥"+priceText);
  addFact(facts,"市盈率 PE",metricText(r.pe));
  addFact(facts,"市净率 PB",metricText(r.pb));
  addFact(facts,"总市值",marketCapText(r.market_cap));
  addFact(facts,"行情日期",String(r.source_trade_date||marketAsOf||"—"));
  addFact(facts,"可追溯版本",sourceVersion||"—");
  for(const history of Array.isArray(r.annual_history)?r.annual_history:[]){const display=String(history?.display||"").trim(),basis=String(history?.basis||"").trim();if(display)addFact(facts,String(history?.name||"年度历史"),display+(basis?"（"+basis+"）":""))}
  const help=document.createElement("p");help.className="detail-help";help.textContent="前六类展示已公开的公司证据摘要；第七类可展开查看实际计算数据、计算方法、权重、分数范围与缺失项。年度历史只在现有证据能够确认起止年份和连续年数时展示，不会根据行情日期倒推。";
  const nav=document.createElement("nav");nav.className="type-nav";nav.setAttribute("aria-label","跳转到七类买入情况");
  for(const [key,name] of Object.entries(TYPE_NAMES)){const button=document.createElement("button");button.type="button";button.dataset.detailType=key;button.textContent=key.slice(4);button.setAttribute("aria-label","跳转到"+name);nav.append(button)}
  fragment.append(renderVerdict(r),facts,help,nav);
  for(const [k,n] of Object.entries(TYPE_NAMES)){
    const v=r.types?.[k]||{},method=METHODOLOGY[k]||{dimensions:{}},type7OldData=k==="type7"&&!(type7MethodDetailAvailable||Object.prototype.hasOwnProperty.call(v,"quality_complete"));
    const article=document.createElement("section");article.className="type-row";article.id="detail-"+k;
    const title=document.createElement("strong");title.textContent=n;
    const right=document.createElement("span");right.append(badge(v.status||"invalid","",k));
    const typeScoreLabel=type7OldData?"":scoreLabel(k,v);if(typeScoreLabel)right.append(document.createTextNode(k==="type7"?" 三项算术平均 "+typeScoreLabel:" "+typeScoreLabel));
    const reason=document.createElement("p");
    const decisionBasis=DECISION_BASIS_NAMES[v.decision?.decision_basis]||"";
    const reasonParts=[type7OldData?"服务器仍是旧版第七类数据，等待新数据刷新后再显示结论":publicReasonText(v.reason)||"暂无公司层面补充说明",decisionBasis?"判定依据："+decisionBasis:"",v.evidence_gap?"资料缺口："+publicReasonText(v.evidence_gap):""].filter(Boolean);
    reason.textContent=reasonParts.join("；");
    const typeMethod=document.createElement("div");typeMethod.className="type-method";
    const methodTitle=document.createElement("strong");methodTitle.textContent="这类模型在看什么";
      const methodText=document.createElement("span");methodText.textContent=(method.summary||"")+" 触发阈值："+(method.trigger||"—")+" 计算规则："+(k==="type7"?"7a、7b、7c分别对应本类别的商业模式、护城河、长期成长；质量认证取三项算术平均并严格大于7.000，当前买点还要检查该类别的买点条件和价格。":"类型总分＝各子项分数×对应权重后相加。");
    typeMethod.append(methodTitle,methodText);
    const scope=document.createElement("div");scope.className="scope-note";
    const coverageGap=isModelCoverageGap(k,v,method);
    scope.textContent=v.status==="not_applicable"?(coverageGap?"模型覆盖缺口："+(publicReasonText(v.reason)||method.applicability||"当前没有适用的专属模型")+"。这不是公司得0分，也不是已被否决；需要建立相应专属模型后才能评价。":"为什么不适用："+(publicReasonText(v.reason)||method.applicability||"超出模型适用范围")+"。这是公司的适用边界，不是缺失公司资料，系统不会用0分伪装成否决。"):"适用范围："+(method.applicability||"—");
    if(coverageGap)scope.classList.add("model-gap");
    if(type7OldData){const warning=document.createElement("div");warning.className="type7-old-data";warning.textContent="旧数据待刷新：本代数据没有第七类12项分类量化明细。为避免把旧70分制误读成新规则，当前隐藏旧分数和子项。";article.append(title,right,reason,typeMethod,scope,warning);fragment.append(article);continue}
    const evidenceNotes=document.createElement("div");evidenceNotes.className="evidence-notes";
    const dimensionKeys=new Set((TYPE_DIMENSIONS[k]||[]).map(([key])=>key));
    for(const [key,value] of Object.entries(v.reasons||{})){const publicValue=publicReasonText(value);if(dimensionKeys.has(key)||!EVIDENCE_META_NAMES[key]||!publicValue)continue;const item=document.createElement("div");item.className="evidence-note";const label=document.createElement("strong");label.textContent=EVIDENCE_META_NAMES[key];const text=document.createElement("span");text.textContent=publicValue;item.append(label,text);evidenceNotes.append(item)}
    const dimensions=document.createElement("div");dimensions.className="dimensions";
    const subScores=v.sub_scores||{},subReasons=v.sub_score_reasons||{},estimatedScores=v.estimated_sub_scores||{},estimatedReasons=v.estimated_sub_score_reasons||{},missing=new Set(v.decision?.missing_dimensions||[]),investorActions=investorActionDimensions(k,v);
    for(const [dimension,label] of (TYPE_DIMENSIONS[k]||[])){
      const item=document.createElement("details");item.className="dimension";
      const dimensionMethod=method.dimensions?.[dimension]||{},weight=Number(dimensionMethod.weight||0),type7MeanMember=k==="type7";
      const summary=document.createElement("summary");
      const name=document.createElement("span");name.className="dimension-title";name.textContent=dimension+" · "+label+(type7MeanMember?" · 算术平均权重33.3%":" · 权重"+weight.toFixed(weight%1?1:0)+"%");
      const value=document.createElement("span");value.className="dimension-score";
      const hasScore=Object.prototype.hasOwnProperty.call(subScores,dimension)&&finiteNumber(subScores[dimension])!==null;
      const hasEstimate=Object.prototype.hasOwnProperty.call(estimatedScores,dimension)&&finiteNumber(estimatedScores[dimension])!==null;
      const missingScore=missing.has(dimension)||!hasScore,positionInstruction=investorActions.has(dimension)&&missing.has(dimension),positionAction=positionInstruction&&v.status==="conditional",inactivePositionAction=positionInstruction&&!positionAction,dataMissingScore=missingScore&&!positionInstruction;
      value.textContent=v.status==="not_applicable"?"不适用":positionAction?"待仓位确认":inactivePositionAction?"当前无需确认":dataMissingScore?(dimensionEstimatesAvailable&&hasEstimate?"未核验参考 "+Number(estimatedScores[dimension]).toFixed(1)+"分":dimensionScoresAvailable?"资料不足":"数据版本过旧"):Number(subScores[dimension]).toFixed(type7MeanMember?3:1)+"分";
      const evidence=document.createElement("span");evidence.className="dimension-evidence";
      const guidance=v.position_guidance||{};
      const currentEvidence=positionAction?[guidance.recommendation,guidance.hard_caps,guidance.worst_case_loss].map(publicReasonText).filter(Boolean).join("；")||"请先确认单票仓位、同类组合仓位与最坏归零损失":inactivePositionAction?"当前未达到进入仓位确认的前置分数条件":publicReasonText(v.reasons?.[dimension]||subReasons[dimension])||cleanedEstimateReason(estimatedReasons[dimension]);
      evidence.textContent=currentEvidence||(v.status==="not_applicable"?"本公司不进入该模型的计分阶段":dataMissingScore?"尚未取得可核验的该项资料":dimensionScoresAvailable?"服务器未提供有效说明":"请等待最新数据发布");
      if(dataMissingScore&&v.status!=="not_applicable")evidence.classList.add("missing-text");
      const dimensionBar=scoreBar((hasScore&&!dataMissingScore&&v.status!=="not_applicable")?subScores[dimension]:null);
      summary.append(name,value,dimensionBar,evidence);
      let bodyBuilt=false;
      const buildBody=()=>{
        const body=document.createElement("div");body.className="dimension-body";const definitions=document.createElement("dl");
        addDefinition(definitions,"指标含义",dimensionMethod.meaning||"—");
        addDefinition(definitions,"所需数据",dimensionMethod.data||"—");
        addDefinition(definitions,"评分方向",dimensionMethod.direction||"—");
        const currentBasis=currentEvidence||(v.status==="not_applicable"?"模型未进入计分，不需要补资料":dataMissingScore?"待补齐原始资料":"—");
        addDefinition(definitions,"公司实际输入",currentBasis,dataMissingScore&&v.status!=="not_applicable"?"missing-text":"");
        const historySummary=(Array.isArray(r.annual_history)?r.annual_history:[]).map(history=>String(history?.name||"年度历史")+"："+String(history?.display||"未公开")).join("；");
        addDefinition(definitions,"数据批次","行情日期："+String(r.source_trade_date||marketAsOf||"未公开")+(historySummary?"；"+historySummary:"；该公司的连续年度范围未随现有证据提供")+"。各子指标可能只使用其中一部分年度，以该项说明为准。");
        addDefinition(definitions,"来源追溯",sourceVersion?"公开详情未附该子指标的单独来源链接；可追溯数据版本："+sourceVersion+"。":"公开详情未附该子指标的单独来源链接或版本号。");
        addDefinition(definitions,"触发阈值",type7MeanMember?"该维度与另外两维取算术平均；平均值必须严格大于7.000。":"该子项按0至10分进入加权总分；类型触发规则："+(method.trigger||"—"));
        addDefinition(definitions,"计算方式",type7MeanMember?"第七类三项平均中的贡献＝该维度分÷3。":"类型总分中的该项贡献＝子项分数×"+weight.toFixed(weight%1?1:0)+"%。");
        if(hasScore&&!dataMissingScore&&!positionInstruction&&v.status!=="not_applicable")addDefinition(definitions,type7MeanMember?"均值贡献":"总分贡献",(Number(subScores[dimension])*weight/100).toFixed(type7MeanMember?3:2)+"分（"+Number(subScores[dimension]).toFixed(type7MeanMember?3:1)+" × "+weight.toFixed(weight%1?1:0)+"%）","contribution");
        else if(hasEstimate&&dataMissingScore)addDefinition(definitions,"参考说明","该值只帮助定位缺口，不参与触发或否决。");
        body.append(definitions);item.append(body);
      };
      item.addEventListener("toggle",()=>{if(item.open&&!bodyBuilt){bodyBuilt=true;buildBody()}});
      item.append(summary);dimensions.append(item);
    }
    article.append(title,right,reason,typeMethod,scope);if(evidenceNotes.childElementCount)article.append(evidenceNotes);const type7MethodDetail=k==="type7"?renderType7MethodDetail(v):null;if(type7MethodDetail)article.append(type7MethodDetail);article.append(dimensions);fragment.append(article);
  }
  box.replaceChildren(fragment);$("drawerCard").scrollTop=0;$("drawer").removeAttribute("aria-busy");
}
async function detailByCode(code){
  const indexRow=data.find(row=>row.code===code);if(!indexRow)return;
  const requestId=++activeDetailRequest;activeDetailCode=code;if(detailAbort)detailAbort.abort();detailAbort=new AbortController();
  openDrawer(indexRow.name+"（"+indexRow.code+"）");
  try{
    let company=detailCache.get(code);
    if(!company){const response=await fetch("/api/company/"+encodeURIComponent(code)+"?generation_id="+generationId,{cache:"force-cache",signal:detailAbort.signal});if(!response.ok)throw new Error("公司明细读取 HTTP "+response.status);company=(await response.json()).company;if(!company)throw new Error("服务器未返回公司明细");detailCache.set(code,company)}
    if(requestId!==activeDetailRequest||activeDetailCode!==code||!$("drawer").classList.contains("open"))return;
    type7MethodDetailAvailable=Object.prototype.hasOwnProperty.call(company?.types?.type7||{},"quality_complete")&&Boolean(company?.types?.type7?.method_detail);
    renderDetail(company);
  }catch(error){
    if(error?.name==="AbortError"||requestId!==activeDetailRequest)return;
    $("drawer").removeAttribute("aria-busy");$("detailMeta").textContent="明细读取失败";$("detailRows").textContent=String(error?.message||error);
  }
}
async function load(){try{const manifestResponse=await fetch("/api/manifest",{cache:"no-store"});if(!manifestResponse.ok)throw new Error("清单读取 HTTP "+manifestResponse.status);const m=await manifestResponse.json();generationId=encodeURIComponent(String(m.generation_id||m.provenance?.generation_id||""));marketAsOf=String(m.market_as_of||"");sourceVersion=String(m.provenance?.source_commit||"").slice(0,12);const indexResponse=await fetch("/api/catalogue-index?generation_id="+generationId+"&index_contract="+CATALOGUE_INDEX_CONTRACT_VERSION,{cache:"force-cache"});if(!indexResponse.ok)throw new Error("公司索引读取 HTTP "+indexResponse.status);const c=await indexResponse.json();if(Number(c.index_contract)!==CATALOGUE_INDEX_CONTRACT_VERSION)throw new Error("公司索引版本不匹配，请刷新页面");dimensionScoresAvailable=m.capabilities?.dimension_scores===true&&c.capabilities?.dimension_scores===true;dimensionEstimatesAvailable=m.capabilities?.dimension_score_estimates===true&&c.capabilities?.dimension_score_estimates===true;data=(Array.isArray(c.companies)?c.companies:[]).map(r=>({...r,_search:(String(r.code)+" "+String(r.name)).toLowerCase(),_market:String(r.code).startsWith("6")?"SH":"SZ"}));fillOptions();const s=m.summary||{},summary=c.summary||{};renderCoverage(s,summary.type_coverage||{});const evidenceGapCompanyCount=Number(summary.evidence_gap_company_count??s.insufficient_company_count??summary.insufficient_company_count??0),decisionRelevantGapCount=Number(summary.decision_relevant_gap_company_count??summary.insufficient_company_count??0),actionConfirmationCount=Number(summary.action_confirmation_company_count??s.action_confirmation_company_count??0);const cards=[["全市场公司",s.company_count||data.length],["至少一种已触发",s.triggered_company_count||0],["待满足附加条件",s.conditional_company_count||0],["其中待确认仓位",actionConfirmationCount],["有客观资料缺口",evidenceGapCompanyCount],["缺口可能改变结论",decisionRelevantGapCount],["七类触发次数",Object.values(s.type_coverage||{}).reduce((n,v)=>n+Number(v.triggered||0),0)]];$("cards").replaceChildren(...cards.map(([label,value])=>{const d=document.createElement("div");d.className="card";const sm=document.createElement("small");sm.textContent=label;const st=document.createElement("strong");st.textContent=Number(value||0).toLocaleString();d.append(sm,st);return d}));$("meta").textContent="数据日期："+(m.market_as_of||"—")+" · 更新时间："+formatBeijing(m.data_timestamp_utc)+" · 来源版本："+(sourceVersion||"—")+" · 量化口径："+METHODOLOGY_LABEL;if(!dimensionScoresAvailable){$("notice").hidden=false;$("notice").textContent="当前服务器数据版本较旧，尚未包含子指标分数，请等待下一次数据发布。"}render()}catch(error){$("meta").textContent="数据读取失败："+String(error?.message||error);$("notice").hidden=false;$("notice").textContent="请稍后重试；如果持续失败，请检查数据发布状态。"}}
$("q").addEventListener("compositionstart",()=>{composing=true});$("q").addEventListener("compositionend",()=>{composing=false;scheduleRender()});$("q").addEventListener("input",()=>{syncSearchStatus();if(!composing)scheduleRender()});for(const id of ["market","type","status","industry","sort"])$(id).addEventListener("change",()=>{page=0;render()});$("coverage").addEventListener("click",event=>{const button=event.target.closest("button[data-type]");if(!button)return;$("type").value=button.dataset.type;$("status").value=button.dataset.status||"";page=0;render();$("resultMeta").scrollIntoView({block:"center"})});$("rows").addEventListener("click",event=>{const row=event.target.closest("tr[data-code]");if(row)detailByCode(row.dataset.code)});$("rows").addEventListener("keydown",event=>{if(event.key!=="Enter"&&event.key!==" ")return;const row=event.target.closest("tr[data-code]");if(row){event.preventDefault();detailByCode(row.dataset.code)}});$("detailRows").addEventListener("click",event=>{const button=event.target.closest("button[data-detail-type]");if(!button)return;const target=$("detail-"+button.dataset.detailType);if(target)$("drawerCard").scrollTo({top:Math.max(0,target.offsetTop-124),behavior:"smooth"})});$("prev").onclick=()=>changePage(-1);$("next").onclick=()=>changePage(1);$("close").onclick=closeDrawer;$("drawer").onclick=event=>{if(event.target===$("drawer"))closeDrawer()};document.addEventListener("keydown",event=>{if(event.key==="Escape"&&$("drawer").classList.contains("open")){event.preventDefault();closeDrawer();return}trapDrawerFocus(event)});load();
</script></body></html>`
  .replace("__QUANT_METHODOLOGY_JSON__",JSON.stringify(METHODOLOGY))
  .replace("__QUANT_METHODOLOGY_VERSION__",METHODOLOGY_VERSION)
  .replace("__QUANT_METHODOLOGY_LABEL__",METHODOLOGY_LABEL)
  .replace("__CATALOGUE_INDEX_CONTRACT_VERSION__",String(CATALOGUE_INDEX_CONTRACT_VERSION));

const JSON_HEADERS={"content-type":"application/json; charset=utf-8","cache-control":"no-store"};
const MAX_MANIFEST_BYTES=1024*1024;
const MAX_COMPRESSED_ASSET_BYTES=8*1024*1024;
const MAX_UNCOMPRESSED_ASSET_BYTES=24*1024*1024;
// Pages advanced mode deploys this Worker as one file, so the official closure
// periods from tools/china_a_share_trading_calendar.json are embedded here.
// Add each newly published exchange calendar explicitly; unlisted years fall
// back to weekdays only and therefore fail closed on an unknown weekday holiday.
const A_SHARE_EXCHANGE_CLOSURES=Object.freeze({
  2026:Object.freeze([
    Object.freeze(["2026-01-01","2026-01-03"]),
    Object.freeze(["2026-02-15","2026-02-23"]),
    Object.freeze(["2026-04-04","2026-04-06"]),
    Object.freeze(["2026-05-01","2026-05-05"]),
    Object.freeze(["2026-06-19","2026-06-21"]),
    Object.freeze(["2026-09-25","2026-09-27"]),
    Object.freeze(["2026-10-01","2026-10-07"]),
  ]),
});
const BEIJING_UTC_OFFSET_MS=8*60*60*1000;
const CURRENT_SESSION_EXPECTED_AFTER_BEIJING_MINUTE=18*60;
const MAX_TRADING_DATA_AGE_HOURS=14*24;
const EXPECTED_TRADING_DAY_LOOKBACK=20;
function json(value,status=200,headers={}){return new Response(JSON.stringify(value),{status,headers:{...JSON_HEADERS,...headers}})}
async function currentGeneration(env,generationId=""){if(generationId)return await env.DB.prepare("SELECT * FROM generations WHERE generation_id=?").bind(generationId).first();return await env.DB.prepare("SELECT g.* FROM current_generation c JOIN generations g ON g.generation_id=c.generation_id WHERE c.singleton=1").first()}
async function sha256Hex(bytes){const digest=await crypto.subtle.digest("SHA-256",bytes);return Array.from(new Uint8Array(digest),value=>value.toString(16).padStart(2,"0")).join("")}
async function generationManifestRecord(env,generation){
  if(!generation)return{manifest:null,object:null,ok:false};
  const object=await env.DATA_BUCKET.get("generations/"+generation.generation_id+"/manifest.json");
  if(!object)return{manifest:null,object:null,ok:false};
  if(object.size<1||object.size>MAX_MANIFEST_BYTES)return{manifest:null,object,ok:false};
  const bytes=await object.arrayBuffer(),expected=String(generation.manifest_sha256||"").toLowerCase(),marker=String(object.customMetadata?.sha256||"").toLowerCase();
  if(object.size!==bytes.byteLength||!/^[0-9a-f]{64}$/.test(expected))return{manifest:null,object,ok:false};
  const actual=await sha256Hex(bytes);
  if(actual!==expected||(marker&&marker!==expected))return{manifest:null,object,ok:false};
  let manifest;try{manifest=JSON.parse(new TextDecoder().decode(bytes))}catch{return{manifest:null,object,ok:false}}
  if(manifest?.company_details&&marker!==expected)return{manifest:null,object,ok:false};
  return{manifest,object,ok:true};
}
async function generationManifest(env,generation){const record=await generationManifestRecord(env,generation);if(record.object&&!record.ok)throw new Error("数据清单对象完整性校验失败");return record.manifest}
function declaredAsset(manifest,key){const value=manifest?.[key];const filename=String(value?.filename||""),sha256=String(value?.sha256||"").toLowerCase(),size=Number(value?.size),uncompressedSize=Number(value?.uncompressed_size);return{filename,sha256:/^[0-9a-f]{64}$/.test(sha256)?sha256:"",size:Number.isSafeInteger(size)&&size>0&&size<=MAX_COMPRESSED_ASSET_BYTES?size:null,uncompressed_size:Number.isSafeInteger(uncompressedSize)&&uncompressedSize>0&&uncompressedSize<=MAX_UNCOMPRESSED_ASSET_BYTES?uncompressedSize:null}}
function declaredCompanyDetails(manifest,generationId){
  const details=manifest?.company_details;if(!details)return null;
  const shards=details.shards,expectedIds=Array.from({length:16},(_,index)=>index.toString(16).padStart(2,"0"));
  if(details.schema_version!==2||details.record_schema!=="company_detail_v2"||details.partition?.algorithm!=="sha256_code_first_nibble"||details.partition?.shard_count!==16||details.root_algorithm!=="SHA256_CANONICAL_SHARD_INDEX_V1"||!/^[0-9a-f]{64}$/.test(String(details.root_sha256||""))||!Number.isSafeInteger(details.company_count)||details.company_count<1||!Array.isArray(shards)||shards.length!==16)throw new Error("公司详情分片清单无效");
  let companyCount=0;
  const validated=shards.map((entry,index)=>{const id=String(entry?.id||""),filename=String(entry?.filename||""),size=Number(entry?.size);if(id!==expectedIds[index]||filename!==`company-details-${generationId}-${id}.json.gz`||!Number.isSafeInteger(entry?.company_count)||entry.company_count<0||!Number.isSafeInteger(size)||size<1||!/^[0-9a-f]{64}$/.test(String(entry?.sha256||""))||!/^[0-9a-f]{64}$/.test(String(entry?.uncompressed_sha256||"")))throw new Error("公司详情分片元数据无效："+(id||index));companyCount+=entry.company_count;return{...entry,id,filename,size}});
  if(companyCount!==details.company_count||companyCount!==Number(manifest?.summary?.company_count||0))throw new Error("公司详情分片覆盖数量不一致");
  return validated;
}
function boundedDataAgeHours(timestamp,nowMs=Date.now()){const parsed=Date.parse(String(timestamp||""));if(!Number.isFinite(parsed))return null;const hours=(nowMs-parsed)/3600000;return Number.isFinite(hours)?Math.max(0,Math.round(hours*10)/10):null}
function validIsoDate(value){const text=String(value||"");if(!/^\d{4}-\d{2}-\d{2}$/.test(text))return false;const parsed=Date.parse(text+"T00:00:00Z");return Number.isFinite(parsed)&&new Date(parsed).toISOString().slice(0,10)===text}
function shiftIsoDate(value,days){if(!validIsoDate(value)||!Number.isSafeInteger(days))return null;return new Date(Date.parse(value+"T00:00:00Z")+days*86400000).toISOString().slice(0,10)}
function beijingClock(nowMs=Date.now()){if(!Number.isFinite(nowMs))return null;const local=new Date(nowMs+BEIJING_UTC_OFFSET_MS);if(!Number.isFinite(local.getTime()))return null;return{date:local.toISOString().slice(0,10),minute:local.getUTCHours()*60+local.getUTCMinutes()}}
function isExpectedTradingDay(value){if(!validIsoDate(value))return false;const weekday=new Date(value+"T00:00:00Z").getUTCDay();if(weekday===0||weekday===6)return false;const closures=A_SHARE_EXCHANGE_CLOSURES[Number(value.slice(0,4))]||[];return!closures.some(([start,end])=>value>=start&&value<=end)}
function latestExpectedClosedTradingDate(nowMs=Date.now()){
  const clock=beijingClock(nowMs);if(!clock)return null;
  let candidate=clock.minute>=CURRENT_SESSION_EXPECTED_AFTER_BEIJING_MINUTE?clock.date:shiftIsoDate(clock.date,-1);
  for(let offset=0;offset<=EXPECTED_TRADING_DAY_LOOKBACK;offset++){
    if(candidate&&isExpectedTradingDay(candidate))return candidate;
    candidate=shiftIsoDate(candidate,-1);
  }
  return null;
}
function tradingCalendarCoverage(expectedMarketAsOf){const year=Number(String(expectedMarketAsOf||"").slice(0,4));return A_SHARE_EXCHANGE_CLOSURES[year]?"交易所公告日历":"仅周末规则（该年份节假日表尚未登记）"}
function tradingDataFreshness(marketAsOf,timestamp,nowMs=Date.now()){
  const expectedMarketAsOf=latestExpectedClosedTradingDate(nowMs),dataAgeHours=boundedDataAgeHours(timestamp,nowMs),marketDateValid=validIsoDate(marketAsOf),marketDateCurrent=Boolean(marketDateValid&&expectedMarketAsOf&&marketAsOf===expectedMarketAsOf),withinHardAge=Boolean(dataAgeHours!==null&&dataAgeHours<=MAX_TRADING_DATA_AGE_HOURS);
  let staleReason=null;
  if(!marketDateValid)staleReason="市场日期无效";
  else if(!expectedMarketAsOf)staleReason="无法计算最近应完成的交易日";
  else if(marketAsOf<expectedMarketAsOf)staleReason="尚未覆盖最近应完成的交易日";
  else if(marketAsOf>expectedMarketAsOf)staleReason="市场日期晚于最近已收盘交易日";
  else if(!withinHardAge)staleReason="数据生成时间超过14天安全上限";
  return{stale:Boolean(staleReason),stale_reason:staleReason,data_age_hours:dataAgeHours,expected_market_as_of:expectedMarketAsOf,market_date_current:marketDateCurrent,calendar_coverage:tradingCalendarCoverage(expectedMarketAsOf),hard_age_limit_hours:MAX_TRADING_DATA_AGE_HOURS};
}
function limitedGzipStream(bytes,label){let total=0;const source=new Response(bytes).body;if(!source)throw new Error(label+"流不可用");return source.pipeThrough(new DecompressionStream("gzip")).pipeThrough(new TransformStream({transform(chunk,controller){const length=Number(chunk?.byteLength||0);if(!Number.isSafeInteger(length)||length<0||total+length>MAX_UNCOMPRESSED_ASSET_BYTES)throw new Error(label+"解压后超过安全上限");total+=length;controller.enqueue(chunk)}}))}
const SHARD_CACHE_LIMIT=4,shardCache=new Map();
function shardCacheKey(generationId,shardId){return "https://shard-cache.invalid/company-shard-v1/"+generationId+"/"+shardId}
function cachedShardPayload(generationId,shardId){const key=generationId+"/"+shardId;if(!shardCache.has(key))return null;const entry=shardCache.get(key);shardCache.delete(key);shardCache.set(key,entry);return entry}
function rememberShardPayload(generationId,shardId,payload){const key=generationId+"/"+shardId;shardCache.set(key,payload);while(shardCache.size>SHARD_CACHE_LIMIT)shardCache.delete(shardCache.keys().next().value)}
async function verifiedCompressedAsset(env,prefix,metadata,label){if(!metadata.filename||!metadata.sha256||metadata.size===null)throw new Error(label+"清单元数据无效");const object=await env.DATA_BUCKET.get(prefix+metadata.filename);if(!object)throw new Error(label+"对象缺失");if(object.size!==metadata.size)throw new Error(label+"对象大小不一致");const bytes=await object.arrayBuffer();if(bytes.byteLength!==metadata.size||await sha256Hex(bytes)!==metadata.sha256)throw new Error(label+"正文完整性校验失败");return{object,bytes}}
async function readVerifiedGzipJson(bytes,metadata,label,uncompressedSha256=""){const raw=await new Response(limitedGzipStream(bytes,label)).arrayBuffer();if(raw.byteLength>MAX_UNCOMPRESSED_ASSET_BYTES||(metadata.uncompressed_size!==null&&raw.byteLength!==metadata.uncompressed_size))throw new Error(label+"解压后大小不一致");if(uncompressedSha256&&await sha256Hex(raw)!==uncompressedSha256)throw new Error(label+"解压正文完整性校验失败");try{return JSON.parse(new TextDecoder().decode(raw))}catch{throw new Error(label+"JSON无效")}}
async function readCatalogue(env,prefix,manifest){const metadata=declaredAsset(manifest,"catalogue"),asset=await verifiedCompressedAsset(env,prefix,metadata,"目录");if(manifest?.company_details&&String(asset.object.customMetadata?.sha256||"").toLowerCase()!==metadata.sha256)throw new Error("目录对象校验标记不一致");const catalogue=await readVerifiedGzipJson(asset.bytes,metadata,"目录");if(!catalogue||!Array.isArray(catalogue.companies))throw new Error("目录结构无效");return catalogue}
async function companyDetailShardId(code){const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(code));return((new Uint8Array(digest)[0]>>4).toString(16)).padStart(2,"0")}
async function readCompanyDetail(env,prefix,manifest,generationId,code){
  const shards=declaredCompanyDetails(manifest,generationId);if(!shards)return null;
  const shardId=await companyDetailShardId(code),metadata=shards.find(entry=>entry.id===shardId);if(!metadata)throw new Error("公司详情分片未声明");
  let payload=cachedShardPayload(generationId,shardId);
  if(payload===null){
    const edgeCache=typeof caches!=="undefined"?caches.default:null,cacheKey=shardCacheKey(generationId,shardId);
    if(edgeCache){try{const hit=await edgeCache.match(cacheKey);if(hit)payload=await hit.json()}catch{}}
  }
  if(payload===null){
    const asset=await verifiedCompressedAsset(env,prefix,{filename:metadata.filename,sha256:String(metadata.sha256||"").toLowerCase(),size:Number.isSafeInteger(metadata.size)&&metadata.size>0&&metadata.size<=MAX_COMPRESSED_ASSET_BYTES?metadata.size:null,uncompressed_size:Number.isSafeInteger(metadata.uncompressed_size)&&metadata.uncompressed_size>0&&metadata.uncompressed_size<=MAX_UNCOMPRESSED_ASSET_BYTES?metadata.uncompressed_size:null},"公司详情");
    if(String(asset.object.customMetadata?.sha256||"").toLowerCase()!==String(metadata.sha256||"").toLowerCase())throw new Error("公司详情对象校验标记不一致");
    payload=await readVerifiedGzipJson(asset.bytes,{uncompressed_size:metadata.uncompressed_size},"公司详情",String(metadata.uncompressed_sha256||"").toLowerCase());
    if(payload?.schema_version!==2||payload?.record_schema!=="company_detail_v2"||payload?.product!=="DS_DCF"||payload?.shard_id!==shardId||!Array.isArray(payload?.companies)||payload.company_count!==payload.companies.length||payload.company_count!==metadata.company_count)throw new Error("公司详情分片结构无效");
    rememberShardPayload(generationId,shardId,payload);
    const edgeCache=typeof caches!=="undefined"?caches.default:null;
    if(edgeCache){try{await edgeCache.put(shardCacheKey(generationId,shardId),new Response(JSON.stringify(payload),{headers:{"content-type":"application/json","cache-control":"public, max-age=3600"}}))}catch{}}
  }
  const company=payload.companies.find(value=>String(value?.code||"")===code);return company||null;
}
function investorActionDimensions(typeKey,value){if(typeKey!=="type6")return new Set();const decision=value?.decision||{},missing=Array.isArray(decision.missing_dimensions)?decision.missing_dimensions:[],declared=Array.isArray(value?.investor_action_dimensions)?value.investor_action_dimensions.filter(dimension=>dimension==="6e"):[],legacy=value?.action_required==="position_confirmation"&&missing.includes("6e")?["6e"]:[];return new Set([...declared,...legacy])}
function typeDataGap(typeKey,value){const decision=value?.decision||{},missing=Array.isArray(decision.missing_dimensions)?decision.missing_dimensions:[],actionDimensions=investorActionDimensions(typeKey,value),dataMissing=missing.filter(dimension=>!actionDimensions.has(dimension)),actionOnly=missing.length>0&&dataMissing.length===0,actionRequired=actionDimensions.has("6e")&&missing.includes("6e")&&value?.status==="conditional",declaredIncomplete=value?.applicable===true&&value?.evidence_complete===false,hasGap=dataMissing.length>0||(!actionOnly&&(declaredIncomplete||Boolean(String(value?.evidence_gap||"").trim())));return{has_gap:hasGap,decision_relevant:hasGap&&decision.decision_complete===false,action_required:actionRequired}}
function compactType(typeKey,value){const decision=value?.decision||{},gap=typeDataGap(typeKey,value);return{status:String(value?.status||"invalid"),score:value?.score??null,...(typeKey==="type6"&&value?.diagnostic_score!==undefined?{diagnostic_score:value.diagnostic_score}:{}),...(typeKey==="type7"&&value?.quality_complete!==undefined?{quality_complete:value.quality_complete===true,quality_certified:value.quality_certified===true,buy_ready:value.buy_ready===true}:{}),score_lower_bound:decision.score_lower_bound??null,score_upper_bound:decision.score_upper_bound??null,has_missing_dimensions:Array.isArray(decision.missing_dimensions)&&decision.missing_dimensions.length>0,has_evidence_gap:gap.has_gap}}
function compactCompany(company){const types={};for(const key of Object.keys(company?.types||{}))types[key]=compactType(key,company.types[key]);return{code:String(company?.code||""),name:String(company?.name||""),industry:String(company?.industry||""),diagnostic_score:company?.diagnostic_score??null,primary_label:String(company?.primary_label||""),types}}
function typeCoverageEvidence(typeKey,value){const state=typeDataGap(typeKey,value),decision=value?.decision||{},unresolved=state.has_gap&&value?.status!=="triggered"&&value?.status!=="conditional"&&decision.decision_complete===false&&decision.decision_basis==="unresolved_missing_evidence";return{...state,decision_unresolved:unresolved,potentially_triggerable:unresolved&&decision.potentially_triggerable===true}}
function catalogueGapSummary(companies){let insufficient=0,evidenceGap=0,decisionRelevant=0,boundedGap=0,actionConfirmation=0;const typeCoverage={};for(const company of companies){const entries=Object.entries(company?.types||{}),values=entries.map(([,value])=>value),states=entries.map(([key,value])=>{const state=typeCoverageEvidence(key,value),coverage=typeCoverage[key]||(typeCoverage[key]={evidence_missing:0,decision_unresolved:0,potentially_triggerable:0,action_confirmation:0});if(state.has_gap)coverage.evidence_missing++;if(state.decision_unresolved)coverage.decision_unresolved++;if(state.potentially_triggerable)coverage.potentially_triggerable++;if(state.action_required)coverage.action_confirmation++;return state}),hasInsufficient=values.some(value=>value?.status==="insufficient_evidence"),hasGap=states.some(state=>state.has_gap),hasRelevant=states.some(state=>state.has_gap&&state.decision_relevant),hasAction=states.some(state=>state.action_required);if(hasInsufficient)insufficient++;if(hasGap)evidenceGap++;if(hasRelevant)decisionRelevant++;if(hasGap&&!hasRelevant)boundedGap++;if(hasAction)actionConfirmation++}return{insufficient_company_count:insufficient,evidence_gap_company_count:evidenceGap,decision_relevant_gap_company_count:decisionRelevant,bounded_gap_company_count:boundedGap,action_confirmation_company_count:actionConfirmation,type_coverage:typeCoverage}}
function headSafeResponse(request,response){return request.method==="HEAD"?new Response(null,{status:response.status,statusText:response.statusText,headers:response.headers}):response}
function canonicalGenerationRequest(url){const entries=[...url.searchParams.entries()];return entries.length===0||(entries.length===1&&entries[0][0]==="generation_id"&&/^[0-9a-f]{16}$/.test(entries[0][1]))}
function generationCacheRequest(request,generationId){const cacheUrl=new URL(request.url);cacheUrl.search="";cacheUrl.searchParams.set("generation_id",generationId);return new Request(cacheUrl.toString(),{method:"GET"})}
function canonicalCatalogueIndexRequest(url){const entries=[...url.searchParams.entries()];return entries.length===2&&entries.every(([key])=>key==="generation_id"||key==="index_contract")&&url.searchParams.getAll("generation_id").length===1&&Boolean(url.searchParams.get("generation_id"))&&url.searchParams.getAll("index_contract").length===1&&url.searchParams.get("index_contract")===String(CATALOGUE_INDEX_CONTRACT_VERSION)}
function catalogueIndexCacheRequest(request,generationId){const cacheUrl=new URL(request.url);cacheUrl.search="";cacheUrl.searchParams.set("generation_id",generationId);cacheUrl.searchParams.set("index_contract",String(CATALOGUE_INDEX_CONTRACT_VERSION));return new Request(cacheUrl.toString(),{method:"GET"})}
async function immutableProjection(request,builder,cacheRequest=request){const edgeCache=typeof caches!=="undefined"?caches.default:null,cacheKey=new Request(cacheRequest.url,{method:"GET"});if(edgeCache){const hit=await edgeCache.match(cacheKey);if(hit)return headSafeResponse(request,hit)}const payload=await builder(),status=payload?.error?404:200,response=json(payload,status,{"cache-control":"public, max-age=31536000, immutable"});if(edgeCache&&status===200)await edgeCache.put(cacheKey,response.clone());return headSafeResponse(request,response)}
export default{
  async fetch(request,env){
    if(request.method!=="GET"&&request.method!=="HEAD")return json({error:"只读接口不接受写请求"},405);
    const url=new URL(request.url),path=url.pathname;
    try{
      if(path==="/"||path==="/index.html")return new Response(INDEX_HTML,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"no-store"}});
      if(path==="/api/methodology")return json({schema_version:1,methodology_version:METHODOLOGY_VERSION,qualify_threshold:7,types:METHODOLOGY},200,{"cache-control":"public, max-age=86400"});
      const requestedGeneration=url.searchParams.get("generation_id")||"";
      if(path==="/api/catalogue-index"&&!canonicalCatalogueIndexRequest(url))return headSafeResponse(request,json({error:"公司索引请求参数无效，请刷新页面"},400));
      if((path==="/api/manifest"||path==="/api/catalogue"||/^\/api\/company\/[036][0-9]{5}$/.test(path))&&!canonicalGenerationRequest(url))return headSafeResponse(request,json({error:"数据版本请求参数无效，请刷新页面"},400));
      const generation=await currentGeneration(env,requestedGeneration);
      if(path==="/api/health"){
        const manifestRecord=await generationManifestRecord(env,generation),manifest=manifestRecord.manifest;
        const catalogue=declaredAsset(manifest,"catalogue"),signals=declaredAsset(manifest,"signals");
        const signatureName=String(manifest?.signature?.filename||"");
        const prefix=generation?"generations/"+generation.generation_id+"/":"";
        const detailAssets=generation&&manifest?declaredCompanyDetails(manifest,generation.generation_id):null;
        const [catalogueObject,signalsObject,signatureObject,...detailObjects]=generation&&manifest?await Promise.all([catalogue.filename?env.DATA_BUCKET.head(prefix+catalogue.filename):null,signals.filename?env.DATA_BUCKET.head(prefix+signals.filename):null,signatureName?env.DATA_BUCKET.head(prefix+signatureName):null,...(detailAssets||[]).map(entry=>env.DATA_BUCKET.head(prefix+entry.filename))]):[null,null,null];
        const detailsDeclared=Array.isArray(detailAssets),catalogueOk=Boolean(catalogueObject&&catalogue.size===catalogueObject.size&&(!detailsDeclared||String(catalogueObject.customMetadata?.sha256||"").toLowerCase()===catalogue.sha256)),signalsOk=Boolean(signalsObject&&signals.size===signalsObject.size&&(!detailsDeclared||String(signalsObject.customMetadata?.sha256||"").toLowerCase()===signals.sha256)),signatureOk=Boolean(signatureObject&&signatureObject.size>=64&&signatureObject.size<=128);
        const detailsOk=!detailsDeclared||Boolean(detailAssets.length===16&&detailObjects.every((object,index)=>object&&object.size===detailAssets[index].size&&String(object.customMetadata?.sha256||"").toLowerCase()===String(detailAssets[index].sha256||"").toLowerCase())),detailBytes=detailObjects.reduce((total,object)=>total+Number(object?.size||0),0);
        const freshness=tradingDataFreshness(generation?.market_as_of,generation?.data_timestamp_utc);
        return json({ok:Boolean(generation&&manifestRecord.ok&&catalogueOk&&signalsOk&&signatureOk&&detailsOk),...freshness,freshness_basis:"北京时间18:00前要求覆盖上一交易日，18:00后要求覆盖当日已收盘交易日；已登记交易所休市日顺延，并保留14天绝对安全上限",generation_id:generation?.generation_id||null,market_as_of:generation?.market_as_of||null,updated_at:generation?.data_timestamp_utc||null,data_generated_at:generation?.data_timestamp_utc||null,generation_published_at:generation?.created_at||null,last_mirror_check_at:generation?.last_checked_at||null,manifest_ok:manifestRecord.ok,manifest_bytes:manifestRecord.object?.size||0,catalogue_bytes:catalogueObject?.size||0,signals_bytes:signalsObject?.size||0,signature_bytes:signatureObject?.size||0,company_details_declared:detailsDeclared,company_details_ready:detailsDeclared&&detailsOk,company_detail_shards:detailAssets?.length||0,company_detail_bytes:detailBytes});
      }
      if(path==="/api/meta")return json(generation||{ok:false,error:"尚未完成首次数据同步"});
      if(!generation)return json({error:"尚未完成首次数据同步"},503);
      const prefix="generations/"+generation.generation_id+"/";
      if(path==="/api/manifest"){
        const manifest=await generationManifest(env,generation);if(!manifest)return json({error:"数据对象缺失"},503);return json({...manifest,generation_id:generation.generation_id},200,{"cache-control":requestedGeneration?"public, max-age=31536000, immutable":"no-store"});
      }
      if(path==="/api/catalogue"){
        const manifest=await generationManifest(env,generation),metadata=declaredAsset(manifest,"catalogue"),catalogue=await readCatalogue(env,prefix,manifest);return headSafeResponse(request,json(catalogue,200,{"cache-control":requestedGeneration?"public, max-age=31536000, immutable":"no-store",etag:metadata.sha256}));
      }
      if(path==="/api/catalogue-index"){
        const manifest=await generationManifest(env,generation);const build=async()=>{const catalogue=await readCatalogue(env,prefix,manifest);return{index_contract:CATALOGUE_INDEX_CONTRACT_VERSION,generation_id:generation.generation_id,capabilities:catalogue.capabilities||{},summary:{company_count:catalogue.companies.length,...catalogueGapSummary(catalogue.companies)},companies:catalogue.companies.map(compactCompany)}};return immutableProjection(request,build,catalogueIndexCacheRequest(request,generation.generation_id));
      }
      const companyMatch=path.match(/^\/api\/company\/([036][0-9]{5})$/);
      if(companyMatch){
        const code=companyMatch[1],manifest=await generationManifest(env,generation);const build=async()=>{let company=await readCompanyDetail(env,prefix,manifest,generation.generation_id,code),detailContract="company_detail_v2",capabilities=manifest?.capabilities||{};if(!manifest?.company_details){const catalogue=await readCatalogue(env,prefix,manifest);company=catalogue.companies.find(value=>String(value?.code||"")===code);capabilities=catalogue.capabilities||capabilities;detailContract="legacy_catalogue"}if(!company)return{error:"未找到该公司",code};return{generation_id:generation.generation_id,market_as_of:generation.market_as_of,methodology_version:METHODOLOGY_VERSION,detail_contract:detailContract,capabilities,company}};if(requestedGeneration)return await immutableProjection(request,build,generationCacheRequest(request,generation.generation_id));const payload=await build();return headSafeResponse(request,json(payload,payload.error?404:200));
      }
      return new Response("Not Found",{status:404});
    }catch(error){
      return json({error:String(error?.message||error)},500);
    }
  },
};
