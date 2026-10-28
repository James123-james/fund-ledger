"""导入规则示例。

复制本文件为 plan.py，改成自己的记录即可：
    cp tools/plan.example.py tools/plan.py

plan.py 已在 .gitignore 中，不会被提交到公开仓库。

字段说明
--------
FUNDS: 基金代码 -> (资产类别, 说明)
    资产类别取值：stock 股票 / bond 债券 / gold 黄金商品 / cash 现金
                  overseas 海外 / other 其他

PLAN: 每一笔操作，kind 有四种写法
    buy     按金额买入，份额和手续费按当日净值、基金费率自动算
    sell    按金额卖出，份额按当日净值折算
    close   清仓，份额自动取当时持仓
    sip     定投，按 freq 展开成逐笔

    freq 取值：daily 每个交易日 / mon tue wed thu fri 指定星期

可选字段 nav_override：强制指定成交净值，货币基金用 1.0
"""

# 下面这些是常见的宽基与债券基金，仅作示例，不代表任何投资建议
FUNDS = {
    "110022": ("stock", "易方达消费行业股票"),
    "000961": ("stock", "天弘沪深300ETF联接A"),
    "110027": ("bond", "易方达安心回报债券A"),
    "000216": ("gold", "华安黄金ETF联接A"),
    "000071": ("overseas", "华夏恒生ETF联接A"),
    "000198": ("cash", "余额宝（天弘余额宝货币）"),
}

PLAN = [
    # 一次性买入
    {"code": "110022", "kind": "buy", "date": "2025-03-14", "amount": 1000, "note": "建仓"},

    # 分批买入
    {"code": "000961", "kind": "buy", "date": "2025-04-07", "amount": 500, "note": "买入"},
    {"code": "000961", "kind": "buy", "date": "2025-05-08", "amount": 500, "note": "回调补仓"},

    # 按金额卖出
    {"code": "000961", "kind": "sell", "date": "2025-09-12", "amount": 300, "note": "部分止盈"},

    # 定期定额：每月，跑一整年
    {"code": "110027", "kind": "sip", "start": "2025-01-01", "end": "2025-12-31",
     "freq": "mon", "amount": 300, "note": "月度定投"},

    # 每个交易日小额定投，只跑一个月
    {"code": "000216", "kind": "sip", "start": "2025-06-01", "end": "2025-06-30",
     "freq": "daily", "amount": 10, "note": "日定投试验"},

    # 清仓：份额留空，自动取当时持仓
    {"code": "000071", "kind": "buy", "date": "2025-07-15", "amount": 800, "note": "买入"},
    {"code": "000071", "kind": "close", "date": "2025-10-20", "note": "清仓"},

    # 货币基金没有净值波动，用 nav_override 固定为 1.0
    {"code": "000198", "kind": "buy", "date": "2025-12-31", "amount": 5000,
     "note": "年末余额快照", "nav_override": 1.0},
]
