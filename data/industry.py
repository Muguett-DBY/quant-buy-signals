"""
自建行业数据库 v3
=================
35个细分行业 + 关键词匹配 + 增速基准 + FCF目标利润率
"""

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any


class IndustryDataError(RuntimeError):
    """Industry source files are missing, corrupt, or semantically empty."""


_INDUSTRY_RULES = [
    # === 金融 (4) ===
    (
        "BANK",
        "银行",
        [
            "银行",
            "招商银行",
            "浦发银行",
            "兴业银行",
            "民生银行",
            "中信银行",
            "光大银行",
            "平安银行",
            "宁波银行",
            "南京银行",
            "北京银行",
            "华夏银行",
            "上海银行",
        ],
    ),
    ("INSURANCE", "保险", ["保险", "人寿", "太保", "中国平安", "新华保险", "中国人保", "中国人寿", "平安", "太平"]),
    (
        "SECURITIES",
        "证券",
        [
            "证券",
            "国泰君安",
            "海通",
            "中信证券",
            "华泰",
            "广发证券",
            "期货",
            "信托",
            "招商证券",
            "申万",
            "东方财富",
            "同花顺",
        ],
    ),
    ("FINANCIAL_OTHER", "其他金融（暂无专属估值模型）", ["融资租赁", "金融控股"]),
    (
        "REAL_ESTATE",
        "房地产",
        [
            "地产",
            "万科",
            "保利",
            "碧桂园",
            "恒大",
            "融创",
            "绿城",
            "招商蛇口",
            "金地",
            "华夏幸福",
            "新城控股",
            "地产开发",
            "产业园区",
        ],
    ),
    # === 消费 (6) ===
    (
        "ALCOHOL",
        "酿酒行业",
        [
            "白酒",
            "茅台",
            "五粮液",
            "泸州老窖",
            "洋河",
            "汾酒",
            "古井贡",
            "水井坊",
            "酒鬼",
            "舍得",
            "老白干",
            "金徽",
            "口子窖",
            "迎驾",
        ],
    ),
    (
        "FOOD_BEV",
        "食品饮料",
        [
            "食品",
            "乳业",
            "乳品",
            "伊利",
            "蒙牛",
            "海天",
            "调味",
            "调味品",
            "榨菜",
            "双汇",
            "金龙鱼",
            "制糖",
            "食糖",
            "饮料",
            "零食",
            "坚果",
            "道道全",
            "千禾",
            "天味",
            "中炬",
            "恒顺",
            "汤臣",
            "妙可",
            "安井",
            "绝味",
            "洽洽",
            "桃李",
            "元祖",
        ],
    ),
    (
        "HOME_APPLIANCE",
        "家电",
        [
            "家电",
            "美的",
            "格力",
            "海尔",
            "老板电器",
            "苏泊尔",
            "电视",
            "冰箱",
            "洗衣机",
            "厨电",
            "小家电",
            "九阳",
            "飞科",
            "长虹",
            "美菱",
            "TCL",
            "海信",
        ],
    ),
    (
        "TEXTILE_APPAREL",
        "纺织服装",
        [
            "服装",
            "纺织",
            "家纺",
            "罗莱",
            "富安娜",
            "水星",
            "雅戈尔",
            "海澜之家",
            "森马",
            "太平鸟",
            "地素",
            "安踏",
            "李宁",
            "特步",
            "361",
        ],
    ),
    (
        "RETAIL",
        "商贸零售",
        [
            "百货",
            "超市",
            "零售",
            "王府井",
            "中免",
            "永辉",
            "家家悦",
            "红旗连锁",
            "周大生",
            "老凤祥",
            "潮宏基",
            "珠宝",
            "首饰",
            "小商品",
        ],
    ),
    (
        "TOURISM_EDU",
        "旅游教育",
        ["旅游", "酒店", "教育", "体育", "健身", "海底捞", "锦江", "首旅", "中青旅", "宋城", "中公", "华图"],
    ),
    # === 医药 (4) ===
    (
        "CHEM_PHARMA",
        "化学制药",
        [
            "制药",
            "化药",
            "恒瑞",
            "复星",
            "科伦",
            "人福",
            "华海",
            "普利",
            "恩华",
            "信立泰",
            "京新",
            "海正",
            "鲁抗",
            "药业",
        ],
    ),
    (
        "BIO_PHARMA",
        "生物制药",
        [
            "生物",
            "生物医药",
            "基因",
            "细胞",
            "疫苗",
            "凯莱英",
            "药明",
            "康龙",
            "泰格",
            "昭衍",
            "美迪西",
            "百济",
            "信达",
            "君实",
        ],
    ),
    (
        "TRAD_CN_MED",
        "中药",
        [
            "中药",
            "片仔癀",
            "云南白药",
            "同仁堂",
            "东阿阿胶",
            "白云山",
            "华润三九",
            "天士力",
            "步长",
            "康恩贝",
            "九芝堂",
            "马应龙",
            "广誉远",
        ],
    ),
    (
        "MEDICAL_SERVICE",
        "医疗服务",
        ["医疗", "爱尔", "通策", "美年", "金域", "迪安", "国际医学", "新里程", "康宁", "体检", "医院", "眼科", "口腔"],
    ),
    # === 科技 (5) ===
    (
        "SOFTWARE",
        "软件互联网",
        [
            "软件",
            "互联网",
            "计算机",
            "数据",
            "人工智能",
            "科大讯飞",
            "用友",
            "金山",
            "浪潮",
            "中软",
            "东软",
            "广联达",
            "恒生电子",
            "石基",
            "卫宁",
        ],
    ),
    (
        "SEMICONDUCTOR",
        "半导体",
        [
            "半导体",
            "芯片",
            "中芯",
            "韦尔",
            "兆易",
            "北方华创",
            "中微",
            "沪硅",
            "卓胜微",
            "汇顶",
            "圣邦",
            "晶晨",
            "寒武纪",
            "海光",
            "龙芯",
        ],
    ),
    (
        "ELEC_COMPONENT",
        "电子元器件",
        [
            "电子",
            "光电",
            "电路",
            "微电子",
            "立讯",
            "歌尔",
            "蓝思",
            "舜宇",
            "大华",
            "海康",
            "宇视",
            "安防",
            "监控",
            "京东方",
            "TCL",
            "深天马",
            "维信诺",
        ],
    ),
    (
        "TELECOM",
        "通信设备",
        ["通信", "中兴", "烽火", "光迅", "亨通", "中天", "星网", "锐捷", "信威", "天线", "基站", "光模块"],
    ),
    (
        "MEDIA",
        "传媒游戏",
        [
            "传媒",
            "出版",
            "游戏",
            "影视",
            "动漫",
            "文化",
            "分众",
            "芒果",
            "光线",
            "华谊",
            "三七",
            "完美",
            "吉比特",
            "世纪华通",
        ],
    ),
    # === 制造 (6) ===
    (
        "AUTO_VEHICLE",
        "汽车整车",
        [
            "整车",
            "比亚迪",
            "长城",
            "上汽",
            "广汽",
            "长安",
            "吉利",
            "赛力斯",
            "北汽蓝谷",
            "江淮",
            "理想",
            "蔚来",
            "小鹏",
            "一汽",
            "东风",
        ],
    ),
    (
        "AUTO_PARTS",
        "汽车零部件",
        [
            "汽配",
            "福耀",
            "潍柴",
            "轮胎",
            "电机",
            "充电",
            "驾驶",
            "均胜",
            "华域",
            "拓普",
            "德赛",
            "星宇",
            "华阳",
            "继峰",
        ],
    ),
    (
        "NEW_ENERGY_VEH",
        "新能源",
        [
            "新能源",
            "宁德",
            "锂电池",
            "光伏",
            "风电",
            "太阳能",
            "储能",
            "充电桩",
            "氢能",
            "隆基",
            "通威",
            "阳光电源",
            "天合",
            "晶澳",
            "亿纬",
            "国轩",
        ],
    ),
    (
        "CONST_MACHINERY",
        "工程机械",
        ["重工", "三一", "中联", "徐工", "柳工", "挖掘", "起重机", "推土", "混凝土", "一重", "二重"],
    ),
    (
        "INDUST_MACHINERY",
        "工业机械",
        [
            "机械",
            "机床",
            "装备",
            "轴承",
            "液压",
            "水泵",
            "阀门",
            "精工",
            "机电",
            "模具",
            "铸造",
            "锻造",
            "焊接",
            "磨床",
            "机器人",
            "汇川",
            "绿的谐波",
            "埃斯顿",
        ],
    ),
    (
        "ELEC_EQUIP",
        "电气设备",
        ["电气", "电机", "变压器", "开关", "继电器", "特变", "正泰", "宏发", "良信", "许继", "国电南瑞", "思源"],
    ),
    # === 材料 (4) ===
    ("STEEL", "钢铁", ["宝钢", "鞍钢", "首钢", "马钢", "华菱", "太钢", "不锈钢", "特钢", "炭素"]),
    (
        "NONFERROUS",
        "有色金属",
        [
            "有色",
            "紫金矿业",
            "稀土",
            "钛业",
            "黄金",
            "白银",
            "矿产",
            "矿石",
            "锂业",
            "锂矿",
            "钴业",
            "镍业",
            "中铝",
            "江铜",
            "云铝",
            "众和",
            "钨业",
        ],
    ),
    (
        "CHEMICAL",
        "化工",
        [
            "化工",
            "化学",
            "化肥",
            "塑料",
            "万华",
            "恒力",
            "荣盛",
            "桐昆",
            "新凤鸣",
            "华鲁",
            "扬农",
            "利尔",
            "树脂",
            "纤维",
            "橡胶",
            "薄膜",
            "有机硅",
            "硅业",
        ],
    ),
    (
        "BUILDING_MATERIAL",
        "建材",
        [
            "水泥",
            "玻璃",
            "海螺",
            "金隅",
            "华新",
            "旗滨",
            "南玻",
            "东方雨虹",
            "北新建材",
            "中国巨石",
            "长海",
            "涂料",
            "管材",
        ],
    ),
    # === 能源 (3) ===
    (
        "OIL_GAS",
        "石油天然气",
        ["石油", "石化", "中石油", "中石化", "中海油", "油气", "燃气", "管网", "杰瑞", "中海油服", "海油工程"],
    ),
    ("COAL", "煤炭", ["煤炭", "神华", "兖矿", "中煤", "陕煤", "潞安", "露天", "焦煤", "煤层"]),
    (
        "POWER_UTILITY",
        "电力公用",
        [
            "电力",
            "电网",
            "发电",
            "华能",
            "华电",
            "三峡",
            "核电",
            "水利",
            "水务",
            "环保",
            "垃圾",
            "废水",
            "绿化",
            "环境",
            "环卫",
            "供热",
            "生态",
            "污水",
            "废气",
        ],
    ),
    # === 基建 (2) ===
    (
        "CONSTRUCTION",
        "建筑装饰",
        [
            "建筑",
            "中国建筑",
            "中国中铁",
            "中国交建",
            "中国电建",
            "中国中车",
            "隧道",
            "地铁",
            "金螳螂",
            "亚厦",
            "江河",
            "中铁",
            "铁建",
        ],
    ),
    (
        "TRANSPORT",
        "交通运输",
        [
            "机场",
            "航空",
            "顺丰",
            "圆通",
            "港口",
            "高速",
            "物流",
            "运输",
            "快递",
            "轨道",
            "招商公路",
            "招商港口",
            "招商轮船",
            "中远海",
            "上港",
            "宁波港",
            "外运",
        ],
    ),
    # === 其他 (3) ===
    (
        "AGRICULTURE",
        "农林牧渔",
        [
            "农业",
            "农牧",
            "牧业",
            "渔业",
            "种业",
            "粮食",
            "生猪",
            "养鸡",
            "饲料",
            "新希望",
            "温氏",
            "森林",
            "种植",
            "养殖",
            "水稻",
            "小麦",
            "兽药",
            "宠物",
        ],
    ),
    (
        "LIGHT_MFG",
        "轻工制造",
        ["家居", "照明", "纸业", "包装", "晨光", "索菲亚", "欧派", "顾家", "尚品", "江山欧派", "蒙娜丽莎", "帝欧"],
    ),
    # These CAPCO-backed service buckets deliberately have no name-keyword
    # fallback.  They are selected only from an authoritative division code.
    ("BUSINESS_SERVICES", "商务服务", []),
    ("PROFESSIONAL_SERVICES", "专业技术服务", []),
    ("ENVIRONMENTAL_SERVICES", "环保与公共设施服务", []),
    ("DIVERSIFIED", "综合", []),
    ("DEFAULT", "综合", []),
]

INDUSTRY_BENCHMARKS = {
    # 金融
    "BANK": {
        "pessimistic_floor": 0.00,
        "neutral_benchmark": 0.05,
        "optimistic_ceiling": 0.12,
        "fcf_margin_target": 0.00,
    },
    "INSURANCE": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.18,
        "fcf_margin_target": 0.00,
    },
    "SECURITIES": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.25,
        "fcf_margin_target": 0.12,
    },
    "FINANCIAL_OTHER": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.03,
        "optimistic_ceiling": 0.10,
        "fcf_margin_target": 0.00,
    },
    "REAL_ESTATE": {
        "pessimistic_floor": -0.20,
        "neutral_benchmark": 0.03,
        "optimistic_ceiling": 0.12,
        "fcf_margin_target": 0.03,
    },
    # 消费
    "ALCOHOL": {
        "pessimistic_floor": 0.02,
        "neutral_benchmark": 0.10,
        "optimistic_ceiling": 0.22,
        "fcf_margin_target": 0.08,
    },
    "FOOD_BEV": {
        "pessimistic_floor": 0.00,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.18,
        "fcf_margin_target": 0.05,
    },
    "HOME_APPLIANCE": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.18,
        "fcf_margin_target": 0.05,
    },
    "TEXTILE_APPAREL": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.04,
    },
    "RETAIL": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.04,
    },
    "TOURISM_EDU": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.04,
    },
    # 医药
    "CHEM_PHARMA": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.05,
    },
    "BIO_PHARMA": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.15,
        "optimistic_ceiling": 0.35,
        "fcf_margin_target": 0.04,
    },
    "TRAD_CN_MED": {
        "pessimistic_floor": 0.00,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.18,
        "fcf_margin_target": 0.06,
    },
    "MEDICAL_SERVICE": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.12,
        "optimistic_ceiling": 0.28,
        "fcf_margin_target": 0.05,
    },
    # 科技
    "SOFTWARE": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.15,
        "optimistic_ceiling": 0.35,
        "fcf_margin_target": 0.04,
    },
    "SEMICONDUCTOR": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.18,
        "optimistic_ceiling": 0.40,
        "fcf_margin_target": 0.03,
    },
    "ELEC_COMPONENT": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.12,
        "optimistic_ceiling": 0.28,
        "fcf_margin_target": 0.04,
    },
    "TELECOM": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.04,
    },
    "MEDIA": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.10,
        "optimistic_ceiling": 0.25,
        "fcf_margin_target": 0.05,
    },
    # 制造
    "AUTO_VEHICLE": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.12,
        "optimistic_ceiling": 0.30,
        "fcf_margin_target": 0.03,
    },
    "AUTO_PARTS": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.10,
        "optimistic_ceiling": 0.25,
        "fcf_margin_target": 0.04,
    },
    "NEW_ENERGY_VEH": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.15,
        "optimistic_ceiling": 0.35,
        "fcf_margin_target": 0.03,
    },
    "CONST_MACHINERY": {
        "pessimistic_floor": -0.20,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.03,
    },
    "INDUST_MACHINERY": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.10,
        "optimistic_ceiling": 0.22,
        "fcf_margin_target": 0.04,
    },
    "ELEC_EQUIP": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.10,
        "optimistic_ceiling": 0.22,
        "fcf_margin_target": 0.04,
    },
    # 材料
    "STEEL": {
        "pessimistic_floor": -0.20,
        "neutral_benchmark": 0.05,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.03,
    },
    "NONFERROUS": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.22,
        "fcf_margin_target": 0.03,
    },
    "CHEMICAL": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.22,
        "fcf_margin_target": 0.03,
    },
    "BUILDING_MATERIAL": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.04,
    },
    # 能源
    "OIL_GAS": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.05,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.04,
    },
    "COAL": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.03,
        "optimistic_ceiling": 0.12,
        "fcf_margin_target": 0.03,
    },
    "POWER_UTILITY": {
        "pessimistic_floor": 0.00,
        "neutral_benchmark": 0.05,
        "optimistic_ceiling": 0.12,
        "fcf_margin_target": 0.03,
    },
    # 基建
    "CONSTRUCTION": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.03,
    },
    "TRANSPORT": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.03,
    },
    # 其他
    "AGRICULTURE": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.03,
    },
    "LIGHT_MFG": {
        "pessimistic_floor": -0.05,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.04,
    },
    "BUSINESS_SERVICES": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.04,
    },
    "PROFESSIONAL_SERVICES": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.04,
    },
    "ENVIRONMENTAL_SERVICES": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.06,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.03,
    },
    "DIVERSIFIED": {
        "pessimistic_floor": -0.15,
        "neutral_benchmark": 0.05,
        "optimistic_ceiling": 0.15,
        "fcf_margin_target": 0.03,
    },
    "DEFAULT": {
        "pessimistic_floor": -0.10,
        "neutral_benchmark": 0.08,
        "optimistic_ceiling": 0.20,
        "fcf_margin_target": 0.03,
    },
}

_BRAND = {
    "招商蛇口": "REAL_ESTATE",
    "招商积余": "REAL_ESTATE",
    "招商公路": "TRANSPORT",
    "招商港口": "TRANSPORT",
    "招商轮船": "TRANSPORT",
    "招商南油": "OIL_GAS",
    "五粮液": "ALCOHOL",
    "山西汾酒": "ALCOHOL",
    "古井贡酒": "ALCOHOL",
    "水井坊": "ALCOHOL",
    "贵州茅台": "ALCOHOL",
    "泸州老窖": "ALCOHOL",
    "洋河股份": "ALCOHOL",
    "云南白药": "TRAD_CN_MED",
    "片仔癀": "TRAD_CN_MED",
    "赛力斯": "AUTO_VEHICLE",
    "北汽蓝谷": "AUTO_VEHICLE",
    "江淮汽车": "AUTO_VEHICLE",
    "比亚迪": "AUTO_VEHICLE",
    "长城汽车": "AUTO_VEHICLE",
    "上汽集团": "AUTO_VEHICLE",
    "宁德时代": "NEW_ENERGY_VEH",
    "隆基绿能": "NEW_ENERGY_VEH",
    "通威股份": "NEW_ENERGY_VEH",
    "阳光电源": "NEW_ENERGY_VEH",
    "亿纬锂能": "NEW_ENERGY_VEH",
    "三一重工": "CONST_MACHINERY",
    "中联重科": "CONST_MACHINERY",
    "徐工机械": "CONST_MACHINERY",
    "万华化学": "CHEMICAL",
    "恒力石化": "CHEMICAL",
    "宝钢股份": "STEEL",
    "海螺水泥": "BUILDING_MATERIAL",
    "中国神华": "COAL",
    "伊利股份": "FOOD_BEV",
    "蒙牛乳业": "FOOD_BEV",
    "海天味业": "FOOD_BEV",
    "金龙鱼": "FOOD_BEV",
    "美的集团": "HOME_APPLIANCE",
    "格力电器": "HOME_APPLIANCE",
    "海尔智家": "HOME_APPLIANCE",
    "恒瑞医药": "CHEM_PHARMA",
    "迈瑞医疗": "MEDICAL_SERVICE",
    "爱尔眼科": "MEDICAL_SERVICE",
    "药明康德": "BIO_PHARMA",
    "康龙化成": "BIO_PHARMA",
    "海康威视": "ELEC_COMPONENT",
    "大华股份": "ELEC_COMPONENT",
    "科大讯飞": "SOFTWARE",
    "用友网络": "SOFTWARE",
    "金山办公": "SOFTWARE",
    "中芯国际": "SEMICONDUCTOR",
    "韦尔股份": "SEMICONDUCTOR",
    "北方华创": "SEMICONDUCTOR",
    "中兴通讯": "TELECOM",
    "立讯精密": "ELEC_COMPONENT",
    "顺丰控股": "TRANSPORT",
    "圆通速递": "TRANSPORT",
    "中远海控": "TRANSPORT",
    "中国建筑": "CONSTRUCTION",
    "中国中铁": "CONSTRUCTION",
    "中国交建": "CONSTRUCTION",
    "万科A": "REAL_ESTATE",
    "保利发展": "REAL_ESTATE",
    "牧原股份": "AGRICULTURE",
    "温氏股份": "AGRICULTURE",
    "新希望": "AGRICULTURE",
    "中国平安": "INSURANCE",
    "中国人寿": "INSURANCE",
    "中国太保": "INSURANCE",
    "中信证券": "SECURITIES",
    "华泰证券": "SECURITIES",
    "国泰君安": "SECURITIES",
    "招商银行": "BANK",
    "工商银行": "BANK",
    "建设银行": "BANK",
    "中国石油": "OIL_GAS",
    "中国石化": "OIL_GAS",
    "中国海油": "OIL_GAS",
    "长江电力": "POWER_UTILITY",
    "华能国际": "POWER_UTILITY",
    "国电电力": "POWER_UTILITY",
    "分众传媒": "MEDIA",
    "芒果超媒": "MEDIA",
    "三七互娱": "MEDIA",
    # 北交所
    "安徽凤凰": "AUTO_PARTS",
    "万达轴承": "INDUST_MACHINERY",
    "惠丰钻石": "NONFERROUS",
    "中航泰达": "ELEC_COMPONENT",
    "倍益康": "MEDICAL_SERVICE",
    "流金科技": "MEDIA",
    "纬达光电": "ELEC_COMPONENT",
    "锦华新材": "CHEMICAL",
    "星昊医药": "CHEM_PHARMA",
    "铜冠矿建": "NONFERROUS",
    "创达新材": "CHEMICAL",
    "鼎佳精密": "INDUST_MACHINERY",
    "丹娜生物": "BIO_PHARMA",
    "凯添燃气": "OIL_GAS",
    "华原股份": "AUTO_PARTS",
    "康美特": "CHEMICAL",
    "齐鲁华信": "SOFTWARE",
    "国义招标": "CONSTRUCTION",
    "诺思兰德": "BIO_PHARMA",
    "科润智控": "ELEC_EQUIP",
    "天工股份": "INDUST_MACHINERY",
    "柏星龙": "LIGHT_MFG",
    "禾昌聚合": "CHEMICAL",
    "隆源股份": "INDUST_MACHINERY",
    "新恒泰": "CHEMICAL",
    "卓兆点胶": "INDUST_MACHINERY",
    "泰凯英": "AUTO_PARTS",
    "世昌股份": "INDUST_MACHINERY",
    "特瑞斯": "OIL_GAS",
    "中诚咨询": "CONSTRUCTION",
    "成电光信": "ELEC_COMPONENT",
    "酉立智能": "INDUST_MACHINERY",
    "精创电气": "ELEC_EQUIP",
    "康普化学": "CHEMICAL",
    "中草香料": "FOOD_BEV",
}

_DATA_DIR = Path(__file__).resolve().parent
INDUSTRY_F10_PATH = _DATA_DIR / "industry_f10.json"
INDUSTRY_EM_MAP_PATH = _DATA_DIR / "industry_em_map.json"
INDUSTRY_CAPCO_PATH = _DATA_DIR / "industry_capco_2025h2.json"
INDUSTRY_NEW_LISTINGS_PATH = _DATA_DIR / "industry_exchange_new_listings_2026.json"

# CAPCO division codes are authoritative business classifications.  Product-
# level F10 labels may retain a more specific existing model bucket, but a
# missing/broad/default F10 label always falls back to this complete map.
_CAPCO_DIVISION_MODEL_MAP = {
    "01": "AGRICULTURE",
    "02": "AGRICULTURE",
    "03": "AGRICULTURE",
    "04": "AGRICULTURE",
    "05": "AGRICULTURE",
    "06": "COAL",
    "07": "OIL_GAS",
    "08": "STEEL",
    "09": "NONFERROUS",
    "10": "BUILDING_MATERIAL",
    "11": "OIL_GAS",
    "13": "FOOD_BEV",
    "14": "FOOD_BEV",
    "15": "FOOD_BEV",
    "17": "TEXTILE_APPAREL",
    "18": "TEXTILE_APPAREL",
    "19": "TEXTILE_APPAREL",
    "20": "LIGHT_MFG",
    "21": "LIGHT_MFG",
    "22": "LIGHT_MFG",
    "23": "LIGHT_MFG",
    "24": "LIGHT_MFG",
    "25": "OIL_GAS",
    "26": "CHEMICAL",
    "27": "CHEM_PHARMA",
    "28": "CHEMICAL",
    "29": "CHEMICAL",
    "30": "BUILDING_MATERIAL",
    "31": "STEEL",
    "32": "NONFERROUS",
    "33": "INDUST_MACHINERY",
    "34": "INDUST_MACHINERY",
    "35": "INDUST_MACHINERY",
    "36": "AUTO_PARTS",
    "37": "INDUST_MACHINERY",
    "38": "ELEC_EQUIP",
    "39": "ELEC_COMPONENT",
    "40": "INDUST_MACHINERY",
    "41": "LIGHT_MFG",
    "42": "POWER_UTILITY",
    "43": "INDUST_MACHINERY",
    "44": "POWER_UTILITY",
    "45": "POWER_UTILITY",
    "46": "POWER_UTILITY",
    "47": "CONSTRUCTION",
    "48": "CONSTRUCTION",
    "49": "CONSTRUCTION",
    "50": "CONSTRUCTION",
    "51": "RETAIL",
    "52": "RETAIL",
    "53": "TRANSPORT",
    "54": "TRANSPORT",
    "55": "TRANSPORT",
    "56": "TRANSPORT",
    "58": "TRANSPORT",
    "59": "TRANSPORT",
    "60": "TRANSPORT",
    "61": "TOURISM_EDU",
    "62": "TOURISM_EDU",
    "63": "TELECOM",
    "64": "SOFTWARE",
    "65": "SOFTWARE",
    "66": "BANK",
    "67": "SECURITIES",
    "68": "INSURANCE",
    "69": "FINANCIAL_OTHER",
    "70": "REAL_ESTATE",
    "71": "BUSINESS_SERVICES",
    "72": "BUSINESS_SERVICES",
    "73": "PROFESSIONAL_SERVICES",
    "74": "PROFESSIONAL_SERVICES",
    "75": "PROFESSIONAL_SERVICES",
    "76": "ENVIRONMENTAL_SERVICES",
    "77": "ENVIRONMENTAL_SERVICES",
    "78": "ENVIRONMENTAL_SERVICES",
    "81": "LIGHT_MFG",
    "83": "TOURISM_EDU",
    "84": "MEDICAL_SERVICE",
    "86": "MEDIA",
    "87": "MEDIA",
    "88": "MEDIA",
    "89": "MEDIA",
    "91": "DIVERSIFIED",
}

# A post-CAPCO listing may disclose a narrower activity than its two-digit
# division.  Keep refinements small and tied to a division whose broad model
# bucket would otherwise erase a material distinction.
_NEW_LISTING_MODEL_REFINEMENTS = {
    "39": {"ELEC_COMPONENT", "SEMICONDUCTOR"},
}
_MIN_CAPCO_RECORDS = 5_000

# Eastmoney's broad "多元金融" bucket contains banks, insurers, capital-market
# firms, generic finance holdings, leasing businesses and even non-financial
# issuers.  It cannot safely select a valuation model by itself.  The CSRC
# industry is sufficiently specific only for these three exact financial
# activities; every other value remains DEFAULT and therefore avoids a false
# bank/securities/insurance P/B branch.
_DIVERSIFIED_FINANCIAL_ZJHY_MAP = {
    # Within the broad "多元金融" bucket this CSRC label also includes
    # financial leasing and holding companies; it is not bank-specific.
    "金融业-货币金融服务": "FINANCIAL_OTHER",
    "金融业-资本市场服务": "SECURITIES",
    "金融业-保险业": "INSURANCE",
    "金融业-其他金融业": "FINANCIAL_OTHER",
    "租赁和商务服务业-租赁业": "FINANCIAL_OTHER",
    "批发和零售业-零售业": "RETAIL",
}

# Resolve known broad-source conflicts only when the more specific CSRC field
# gives an unambiguous activity.  A blanket CSRC override would erase useful
# product-level sectors such as batteries and semiconductors.
_SOURCE_CSRC_REFINEMENTS = {
    ("文教休闲", "教育-教育"): "TOURISM_EDU",
    ("装修装饰", "建筑业-建筑装饰、装修和其他建筑业"): "CONSTRUCTION",
    ("电子化学品", "制造业-化学原料和化学制品制造业"): "CHEMICAL",
}

_CSRC_FALLBACK_MAP = {
    "制造业-非金属矿物制品业": "BUILDING_MATERIAL",
    "制造业-造纸和纸制品业": "LIGHT_MFG",
    "制造业-废弃资源综合利用业": "POWER_UTILITY",
    "制造业-橡胶和塑料制品业": "CHEMICAL",
    "制造业-通用设备制造业": "INDUST_MACHINERY",
    "制造业-专用设备制造业": "INDUST_MACHINERY",
    "制造业-仪器仪表制造业": "INDUST_MACHINERY",
    "制造业-计算机、通信和其他电子设备制造业": "ELEC_COMPONENT",
    "制造业-有色金属冶炼和压延加工业": "NONFERROUS",
    "制造业-铁路、船舶、航空航天和其他运输设备制造业": "INDUST_MACHINERY",
    "制造业-皮革、毛皮、羽毛及其制品和制鞋业": "TEXTILE_APPAREL",
    "制造业-电气机械和器材制造业": "ELEC_EQUIP",
    "信息传输、软件和信息技术服务业-软件和信息技术服务业": "SOFTWARE",
}


_INDUSTRY_GENERATION = 0
_INDUSTRY_GENERATION_LOCK = RLock()


def _file_signature(path: Path) -> tuple[str, int, int]:
    absolute_path = path.absolute()
    try:
        stat = path.stat()
        return str(absolute_path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(absolute_path), -1, -1


def _authoritative_records(capco_payload: Any, new_listing_payload: Any) -> tuple[dict[str, dict], dict]:
    """Validate and merge the periodic CAPCO table with later listing notices."""
    if not isinstance(capco_payload, Mapping) or capco_payload.get("schema_version") != 1:
        raise ValueError("CAPCO industry payload has an unsupported schema")
    source = capco_payload.get("source")
    records = capco_payload.get("records")
    if not isinstance(source, Mapping) or not isinstance(records, Mapping) or len(records) < _MIN_CAPCO_RECORDS:
        raise ValueError("CAPCO industry payload is incomplete")
    if source.get("record_count") != len(records):
        raise ValueError("CAPCO source record_count does not match records")
    source_hash = str(source.get("source_sha256") or "").strip().lower()
    if len(source_hash) != 64 or any(character not in "0123456789abcdef" for character in source_hash):
        raise ValueError("CAPCO source SHA-256 is invalid")

    if not isinstance(new_listing_payload, Mapping) or new_listing_payload.get("schema_version") != 1:
        raise ValueError("new-listing industry payload has an unsupported schema")
    later_records = new_listing_payload.get("records")
    if not isinstance(later_records, Mapping):
        raise ValueError("new-listing industry records are missing")

    merged: dict[str, dict] = {}
    for source_kind, collection in (("capco_periodic", records), ("exchange_new_listing", later_records)):
        for raw_code, raw_record in collection.items():
            code = str(raw_code).strip()
            if len(code) != 6 or not code.isdigit() or not isinstance(raw_record, Mapping):
                raise ValueError(f"invalid authoritative industry identity: {raw_code!r}")
            record = dict(raw_record)
            division_code = str(record.get("division_code") or "").strip()
            model_industry = _CAPCO_DIVISION_MODEL_MAP.get(division_code)
            if source_kind == "exchange_new_listing" and record.get("model_industry") is not None:
                refined_industry = str(record.get("model_industry") or "").strip()
                allowed_refinements = _NEW_LISTING_MODEL_REFINEMENTS.get(division_code, {model_industry})
                if refined_industry not in allowed_refinements:
                    raise ValueError(f"new-listing industry {code} has an unsupported model refinement")
                model_industry = refined_industry
            if (
                not str(record.get("name") or "").strip()
                or model_industry not in INDUSTRY_BENCHMARKS
                or model_industry == "DEFAULT"
            ):
                raise ValueError(f"authoritative industry {code} has no supported model mapping")
            record["model_industry"] = model_industry
            record["source_kind"] = source_kind
            if source_kind == "capco_periodic":
                record.update(
                    {
                        "source_authority": source.get("authority"),
                        "source_title": source.get("title"),
                        "source_url": source.get("source_url"),
                        "source_sha256": source_hash,
                        "published_date": source.get("published_date"),
                        "effective_period": source.get("effective_period"),
                    }
                )
            else:
                item_hash = str(record.get("source_sha256") or "").strip().lower()
                if len(item_hash) != 64 or any(character not in "0123456789abcdef" for character in item_hash):
                    raise ValueError(f"new-listing industry {code} has an invalid source SHA-256")
            previous = merged.get(code)
            if previous is not None and previous != record:
                raise ValueError(f"authoritative industry {code} is duplicated across sources")
            merged[code] = record
    metadata = {
        "capco_records": len(records),
        "new_listing_records": len(later_records),
        "effective_period": source.get("effective_period"),
        "published_date": source.get("published_date"),
        "source_url": source.get("source_url"),
        "source_sha256": source_hash,
    }
    return merged, metadata


@lru_cache(maxsize=8)
def _load_industry_file_generation_cached(
    f10_signature: tuple[str, int, int],
    em_signature: tuple[str, int, int],
    capco_signature: tuple[str, int, int],
    new_listings_signature: tuple[str, int, int],
) -> tuple[dict, dict, dict, dict, str]:
    """Load each JSON generation once; signatures invalidate changed files."""
    try:
        with open(f10_signature[0], "r", encoding="utf-8") as handle:
            f10_cache = json.load(handle)
        with open(em_signature[0], "r", encoding="utf-8") as handle:
            em_map = json.load(handle)
        with open(capco_signature[0], "r", encoding="utf-8") as handle:
            capco_payload = json.load(handle)
        with open(new_listings_signature[0], "r", encoding="utf-8") as handle:
            new_listings_payload = json.load(handle)
        if not isinstance(f10_cache, dict) or not isinstance(em_map, dict):
            raise ValueError("industry JSON roots must be objects")
        if not f10_cache or not em_map:
            raise ValueError("industry JSON sources must not be empty")
        official_records, official_metadata = _authoritative_records(capco_payload, new_listings_payload)
        return f10_cache, em_map, official_records, official_metadata, ""
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        return {}, {}, {}, {}, f"{type(exc).__name__}: {exc}"


@lru_cache(maxsize=16)
def _load_industry_sources_cached(
    f10_path: Path,
    em_path: Path,
    capco_path: Path,
    new_listings_path: Path,
    generation: int,
) -> tuple[dict, dict, dict, dict, str]:
    """Resolve one process generation to an immutable pair of source maps."""
    del generation  # The cache key is the generation boundary; signatures select parsed JSON.
    return _load_industry_file_generation_cached(
        _file_signature(f10_path),
        _file_signature(em_path),
        _file_signature(capco_path),
        _file_signature(new_listings_path),
    )


def _load_industry_sources() -> tuple[dict, dict, dict, dict, str]:
    """Return the current process generation without per-company filesystem I/O."""
    return _load_industry_sources_cached(
        Path(INDUSTRY_F10_PATH),
        Path(INDUSTRY_EM_MAP_PATH),
        Path(INDUSTRY_CAPCO_PATH),
        Path(INDUSTRY_NEW_LISTINGS_PATH),
        _INDUSTRY_GENERATION,
    )


def _refresh_industry_sources() -> tuple[dict, dict, dict, dict, str]:
    """Check source signatures once, then atomically publish the new generation."""
    global _INDUSTRY_GENERATION
    with _INDUSTRY_GENERATION_LOCK:
        generation = _INDUSTRY_GENERATION + 1
        sources = _load_industry_sources_cached(
            Path(INDUSTRY_F10_PATH),
            Path(INDUSTRY_EM_MAP_PATH),
            Path(INDUSTRY_CAPCO_PATH),
            Path(INDUSTRY_NEW_LISTINGS_PATH),
            generation,
        )
        _INDUSTRY_GENERATION = generation
        return sources


def begin_industry_generation() -> int:
    """Refresh source metadata once before a classification/valuation batch."""
    _f10_cache, _em_map, official_records, _official_metadata, error = _refresh_industry_sources()
    if error or not _f10_cache or not _em_map or not official_records:
        raise IndustryDataError(error or "industry sources are empty")
    return _INDUSTRY_GENERATION


def reload_industry_data() -> dict:
    """Explicitly reload JSON files after an administrative data refresh."""
    with _INDUSTRY_GENERATION_LOCK:
        _load_industry_sources_cached.cache_clear()
        _load_industry_file_generation_cached.cache_clear()
    return industry_data_status()


def _fallback_industry(name: str) -> str:
    name_clean = str(name or "").strip().replace(" ", "").replace("\u3000", "")
    if name_clean in _BRAND:
        return _BRAND[name_clean]
    if any(term in name_clean for term in ("生物", "基因", "细胞", "疫苗")):
        return "BIO_PHARMA"
    for industry_code, _industry_name, keywords in _INDUSTRY_RULES:
        if any(keyword in name_clean for keyword in keywords):
            return industry_code
    return "DEFAULT"


def _classify_from_sources(
    code: str,
    name: str,
    f10_cache: Mapping[str, Any],
    em_map: Mapping[str, Any],
    official_records: Mapping[str, Any],
) -> tuple[str, str]:
    code_clean = _normalize_security_code(code)
    entry = f10_cache.get(code_clean)
    meaningful_f10_default = False
    if isinstance(entry, Mapping):
        source_industry = str(entry.get("sshy", "") or "").strip()
        csrc_industry = str(entry.get("zjhy", "") or "").strip()
        if source_industry == "多元金融":
            refined = _DIVERSIFIED_FINANCIAL_ZJHY_MAP.get(csrc_industry, "DEFAULT")
            if refined != "DEFAULT":
                return refined, "f10_specific"
            meaningful_f10_default = True
        refined = _SOURCE_CSRC_REFINEMENTS.get((source_industry, csrc_industry))
        if refined is not None:
            return refined, "f10_specific"
        if source_industry in {"", "--"}:
            csrc_fallback = _CSRC_FALLBACK_MAP.get(csrc_industry)
            if csrc_fallback is not None:
                return csrc_fallback, "f10_specific"
        mapped = em_map.get(source_industry)
        if mapped in INDUSTRY_BENCHMARKS and mapped != "DEFAULT":
            return str(mapped), "f10_specific"
        meaningful_f10_default = meaningful_f10_default or (mapped == "DEFAULT" and source_industry not in {"", "--"})
    official = official_records.get(code_clean)
    if isinstance(official, Mapping):
        model_industry = str(official.get("model_industry") or "").strip()
        source_kind = str(official.get("source_kind") or "").strip()
        if model_industry in INDUSTRY_BENCHMARKS and model_industry != "DEFAULT":
            source = "official_new_listing" if source_kind == "exchange_new_listing" else "official_capco"
            return model_industry, source
    if meaningful_f10_default:
        return "DEFAULT", "f10_default"
    fallback = _fallback_industry(name)
    return fallback, "name_fallback" if fallback != "DEFAULT" else "unclassified"


def _quote_records(quotes: Any) -> list[Mapping[str, Any]]:
    if quotes is None:
        return []
    to_dict = getattr(quotes, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = to_dict()
    else:
        records = list(quotes) if isinstance(quotes, Iterable) else []
    return [record for record in records if isinstance(record, Mapping)]


def _confidence_label(coverage: float) -> str:
    if coverage >= 0.90:
        return "high"
    if coverage >= 0.60:
        return "medium"
    return "low"


def industry_data_status(quotes: Any = None) -> dict:
    """Expose loader health plus current-universe and per-market confidence."""
    f10_cache, em_map, official_records, official_metadata, error = _refresh_industry_sources()
    usable_specific = 0
    mapped_default = 0
    unmapped_source_industries: set[str] = set()
    for entry in f10_cache.values():
        if not isinstance(entry, Mapping):
            continue
        source_industry = str(entry.get("sshy", "") or "").strip()
        csrc_industry = str(entry.get("zjhy", "") or "").strip()
        if source_industry == "多元金融":
            mapped = _DIVERSIFIED_FINANCIAL_ZJHY_MAP.get(csrc_industry, "DEFAULT")
        else:
            mapped = _SOURCE_CSRC_REFINEMENTS.get((source_industry, csrc_industry), em_map.get(source_industry))
            if source_industry in {"", "--"}:
                mapped = _CSRC_FALLBACK_MAP.get(csrc_industry, mapped)
        if mapped in INDUSTRY_BENCHMARKS and mapped != "DEFAULT":
            usable_specific += 1
        elif mapped == "DEFAULT":
            mapped_default += 1
        elif source_industry:
            unmapped_source_industries.add(source_industry)

    loader_ok = not error and bool(f10_cache) and bool(em_map) and bool(official_records) and usable_specific > 0
    if not loader_ok and not error:
        error = "ValueError: industry sources contain no specific usable mappings"
    status: dict[str, Any] = {
        "ok": loader_ok,
        "loader_ok": loader_ok,
        "coverage_ok": loader_ok,
        "error": error,
        "f10_entries": len(f10_cache),
        "usable_f10_entries": usable_specific,
        "default_f10_entries": mapped_default,
        "unusable_f10_entries": len(f10_cache) - usable_specific - mapped_default,
        "unmapped_source_industries": sorted(unmapped_source_industries),
        "industry_mappings": len(em_map),
        "authoritative_records": len(official_records),
        "authoritative_source": official_metadata,
        "quote_universe": 0,
        "authoritative_coverage": 0.0,
        "source_bound_coverage": 0.0,
        "specific_coverage": 0.0,
        "confidence": "unavailable" if not loader_ok else "unknown",
        "market_coverage": {},
        "warnings": [],
    }
    records = _quote_records(quotes)
    if not records or not loader_ok:
        return status

    market_stats: dict[str, dict[str, int]] = {}
    total_specific = 0
    total_authoritative = 0
    total_source_bound = 0
    for record in records:
        market = str(record.get("market", "UNKNOWN") or "UNKNOWN").upper()
        bucket = market_stats.setdefault(
            market,
            {
                "total": 0,
                "authoritative": 0,
                "f10_specific": 0,
                "official_capco": 0,
                "official_new_listing": 0,
                "name_fallback": 0,
                "default": 0,
            },
        )
        bucket["total"] += 1
        normalized_code = _normalize_security_code(str(record.get("code", "")))
        if normalized_code in official_records:
            bucket["authoritative"] += 1
            total_authoritative += 1
        industry_code, source = _classify_from_sources(
            str(record.get("code", "")),
            str(record.get("name", "")),
            f10_cache,
            em_map,
            official_records,
        )
        if source == "f10_specific":
            bucket["f10_specific"] += 1
            total_specific += 1
            total_source_bound += 1
        elif source in {"official_capco", "official_new_listing"}:
            bucket[source] += 1
            total_specific += 1
            total_source_bound += 1
        elif source == "name_fallback" and industry_code != "DEFAULT":
            bucket["name_fallback"] += 1
            total_specific += 1
        else:
            bucket["default"] += 1

    coverage_by_market: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for market, bucket in sorted(market_stats.items()):
        total = max(bucket["total"], 1)
        specific = (
            bucket["f10_specific"] + bucket["official_capco"] + bucket["official_new_listing"] + bucket["name_fallback"]
        )
        coverage = specific / total
        source_bound = specific - bucket["name_fallback"]
        source_bound_coverage = source_bound / total
        authoritative_coverage = bucket["authoritative"] / total
        confidence = _confidence_label(coverage)
        coverage_by_market[market] = {
            **bucket,
            "specific": specific,
            "specific_coverage": coverage,
            "source_bound": source_bound,
            "source_bound_coverage": source_bound_coverage,
            "authoritative_coverage": authoritative_coverage,
            "confidence": confidence,
        }
        if market in {"SH", "SZ"} and source_bound != bucket["total"]:
            warnings.append(
                f"{market} source-bound model industry coverage is incomplete: "
                f"{source_bound}/{bucket['total']} source-bound, {specific}/{bucket['total']} model-mapped"
            )

    overall_coverage = total_specific / max(len(records), 1)
    overall_authoritative_coverage = total_authoritative / max(len(records), 1)
    overall_source_bound_coverage = total_source_bound / max(len(records), 1)
    overall_confidence = _confidence_label(overall_coverage)
    supported_market_coverage = [item for market, item in coverage_by_market.items() if market in {"SH", "SZ"}]
    coverage_ok = bool(supported_market_coverage) and all(
        item["source_bound"] == item["total"] for item in supported_market_coverage
    )
    status.update(
        {
            "ok": loader_ok,
            "coverage_ok": coverage_ok,
            "quote_universe": len(records),
            "authoritative_coverage": overall_authoritative_coverage,
            "source_bound_coverage": overall_source_bound_coverage,
            "specific_coverage": overall_coverage,
            "confidence": overall_confidence,
            "market_coverage": coverage_by_market,
            "warnings": warnings,
        }
    )
    return status


def _normalize_security_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if len(normalized) >= 8 and normalized[:2] in {"SH", "SZ", "BJ", "HK"}:
        normalized = normalized[2:]
    if normalized.endswith(".0") and normalized[:-2].isdigit():
        normalized = normalized[:-2]
    return normalized.zfill(6) if normalized.isdigit() and len(normalized) < 6 else normalized


def classify_industry(code: str, name: str) -> str:
    """Use F10 first and conservative, multi-character rules as fallback.

    A stock-code board prefix is not an industry. Unknown companies therefore
    remain ``DEFAULT`` instead of being systematically labelled software or
    industrial machinery.
    """
    f10_cache, em_map, official_records, _official_metadata, error = _load_industry_sources()
    if error or not f10_cache or not em_map or not official_records:
        raise IndustryDataError(error or "industry sources are empty")
    industry_code, _source = _classify_from_sources(code, name, f10_cache, em_map, official_records)
    return industry_code


_DEFAULT_SINGLE_CLASSIFIER = classify_industry


def classify_industries(companies: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Classify one generation after loading immutable source maps exactly once."""
    prepared_companies = list(companies)
    if classify_industry is not _DEFAULT_SINGLE_CLASSIFIER:
        return {_normalize_security_code(code): classify_industry(code, name) for code, name in prepared_companies}
    f10_cache, em_map, official_records, _official_metadata, error = _load_industry_sources()
    if error or not f10_cache or not em_map or not official_records:
        raise IndustryDataError(error or "industry sources are empty")
    result: dict[str, str] = {}
    for raw_code, raw_name in prepared_companies:
        code = _normalize_security_code(raw_code)
        if not code or code in result:
            raise IndustryDataError(f"invalid or duplicate industry classification identity: {code!r}")
        industry_code, _source = _classify_from_sources(code, str(raw_name or ""), f10_cache, em_map, official_records)
        result[code] = industry_code
    return result


def get_industry_benchmark(industry_code: str) -> dict:
    return INDUSTRY_BENCHMARKS.get(industry_code, INDUSTRY_BENCHMARKS["DEFAULT"])


def get_industry_fcf_margin(industry_code: str) -> float:
    bm = INDUSTRY_BENCHMARKS.get(industry_code, INDUSTRY_BENCHMARKS["DEFAULT"])
    return bm.get("fcf_margin_target", 0.03)


def blend_scenario_growth(company_growth: dict, industry_code: str) -> dict:
    bm = get_industry_benchmark(industry_code)
    pes_company = company_growth.get("pessimistic", 0.05)
    pes_industry = bm["pessimistic_floor"]
    pessimistic = max(min(pes_company, pes_industry), -0.15)
    neu_company = company_growth.get("neutral", 0.08)
    neu_industry = bm["neutral_benchmark"]
    # Real flat/negative company evidence must not be overwritten by an
    # optimistic long-run industry narrative.
    neutral = neu_company if neu_company <= 0 else neu_company * 0.5 + neu_industry * 0.5
    opt_company = company_growth.get("optimistic", 0.15)
    opt_industry = bm["optimistic_ceiling"]
    optimistic = min(opt_company, opt_industry)
    return {
        "pessimistic": round(pessimistic, 4),
        "neutral": round(min(neutral, 0.15), 4),
        "optimistic": round(min(optimistic, 0.25), 4),
    }
