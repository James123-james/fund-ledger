"""收益率分析：XIRR 年化、收益归因、基准对比、对账校准。

与 engine.py 的分工：
- engine.py 负责「现在值多少钱」（持仓、成本、盈亏）
- 本模块负责「这笔钱投得值不值」（时间加权后的年化、相对基准、贡献拆解）
"""

from collections import defaultdict
from datetime import date

from . import db

EPS = 1e-9

# 持有期短于这个天数时不给年化，样本太短会得出荒谬数字（如 18 天亏 13% → 年化 -94%）
MIN_ANNUALIZE_DAYS = 90

# 求解区间。上限 10 表示年化 1000%，超过这个值的解没有现实意义，视为不收敛
RATE_GRID = [
    -0.95, -0.9, -0.8, -0.7, -0.6, -0.5, -0.4, -0.3, -0.2, -0.1,
    0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0,
]


# ---------------------------------------------------------------- 现金流


def valuation_date(conn, date_to=None):
    """估值日：所有流水日与净值日中的最晚一天。

    必须晚于或等于全部现金流日期，否则折现会出现「负时间指数」，
    指数函数反向放大导致求解器跑飞（这正是余额宝算出 +1000% 的根因）。
    """
    row = conn.execute(
        "SELECT MAX(d) AS d FROM ("
        "  SELECT MAX(date) AS d FROM txns"
        "  UNION ALL SELECT MAX(date) FROM nav"
        ")"
    ).fetchone()
    latest = row["d"] if row and row["d"] else date.today().strftime("%Y-%m-%d")
    if date_to and date_to < latest:
        return date_to
    return latest


def build_flows(conn, code=None, date_to=None, include_terminal=True):
    """构建一组现金流：流出为负、流入为正，末尾追加期末市值。

    返回 (flows, meta)。flows 为 [(date, amount)]，按日期升序。
    include_terminal=False 时不含期末市值，供基准模拟使用——
    基准的期末市值要按自己的份额算，不能直接套用组合的。
    """
    from . import engine

    asof = valuation_date(conn, date_to)
    asof_d = date.fromisoformat(asof)

    if code:
        codes = [code]
    else:
        codes = [r["code"] for r in conn.execute("SELECT code FROM funds").fetchall()]

    txns = engine.load_txns(conn, date_to=asof)
    if code:
        txns = [t for t in txns if t["code"] == code]

    flows = []
    for t in txns:
        d = date.fromisoformat(t["date"])
        if d > asof_d:
            continue
        amt = float(t["amount"] or 0)
        fee = float(t["fee"] or 0)
        if t["action"] in db.BUY_ACTIONS:
            flows.append((d, -amt))
        elif t["action"] in db.SELL_ACTIONS:
            flows.append((d, amt - fee))
        elif t["action"] == "dividend":
            if float(t["shares"] or 0) > 0:
                continue  # 红利再投不产生现金流
            flows.append((d, amt - fee))

    # 期末市值
    positions = engine.build_positions(conn, date_to=asof if date_to else None)
    if code:
        positions = [p for p in positions if p["code"] == code]
    terminal_value = sum(p["value"] for p in positions if not p["closed"])

    if include_terminal and terminal_value > EPS:
        flows.append((asof_d, terminal_value))

    flows.sort(key=lambda x: x[0])
    meta = {
        "asof": asof,
        "terminal_value": round(terminal_value, 2),
        "flows": len(flows),
        "involved": codes,
    }
    return flows, meta


# ---------------------------------------------------------------- 求解


def _npv(rate, flows):
    """净现值。返回 None 表示该利率下数值溢出。"""
    base = flows[0][0]
    total = 0.0
    for d, cf in flows:
        years = (d - base).days / 365.0
        try:
            total += cf / (1.0 + rate) ** years
        except (OverflowError, ZeroDivisionError):
            return None
    return total


def solve_irr(flows):
    """求内部收益率。找不到解返回 None。

    先用网格扫描定位符号变化区间，再在区间内二分。
    比直接用牛顿法稳健：现金流方向多次翻转时牛顿法容易发散。
    """
    if len(flows) < 2:
        return None
    has_neg = any(cf < 0 for _, cf in flows)
    has_pos = any(cf > 0 for _, cf in flows)
    if not (has_neg and has_pos):
        return None  # 全同号，IRR 无意义

    prev_rate = None
    prev_val = None
    for rate in RATE_GRID:
        val = _npv(rate, flows)
        if val is None:
            prev_rate, prev_val = None, None
            continue
        if prev_val is not None and (prev_val == 0 or prev_val * val < 0):
            lo, hi = prev_rate, rate
            f_lo = prev_val
            for _ in range(200):
                mid = (lo + hi) / 2.0
                f_mid = _npv(mid, flows)
                if f_mid is None:
                    break
                if abs(f_mid) < 1e-9:
                    return mid
                if f_lo * f_mid < 0:
                    hi = mid
                else:
                    lo, f_lo = mid, f_mid
                if hi - lo < 1e-9:
                    break
            return (lo + hi) / 2.0
        prev_rate, prev_val = rate, val
    return None


def irr_report(conn, code=None, date_to=None):
    """单只基金或整个组合的收益率报告。

    同时给出两个口径：
    - cumulative  累计收益率 = 累计盈亏 ÷ 累计投入（不看时间）
    - annualized  XIRR 年化（按现金流实际发生日折现）
    持有不足 MIN_ANNUALIZE_DAYS 天时不给年化，只标注原因。
    """
    from . import engine

    flows, meta = build_flows(conn, code=code, date_to=date_to)
    if len(flows) < 2:
        return {"cumulative": None, "annualized": None, "reason": "现金流不足", **meta}

    first_d, _ = flows[0]
    last_d, _ = flows[-1]
    days = (last_d - first_d).days

    # 累计口径
    invested = sum(-cf for _, cf in flows if cf < 0)
    returned = sum(cf for _, cf in flows if cf > 0)
    pnl = returned - invested
    cumulative = (pnl / invested * 100) if invested > EPS else None

    result = {
        "cumulative": round(cumulative, 2) if cumulative is not None else None,
        "annualized": None,
        "pnl": round(pnl, 2),
        "invested": round(invested, 2),
        "days": days,
        "reason": None,
        **meta,
    }

    if days < MIN_ANNUALIZE_DAYS:
        result["reason"] = f"持有 {days} 天，不足 {MIN_ANNUALIZE_DAYS} 天，年化无参考意义"
        return result

    rate = solve_irr(flows)
    if rate is None:
        result["reason"] = "现金流方向无法收敛出年化"
        return result

    result["annualized"] = round(rate * 100, 2)
    return result


def portfolio_irr(conn, date_to=None):
    """组合层面：合并全部现金流后求一个整体年化。"""
    return irr_report(conn, code=None, date_to=date_to)


def irr_table(conn, date_to=None):
    """逐基金 + 组合的收益率表，供界面与导出使用。"""
    from . import engine

    positions = engine.build_positions(conn, date_to=date_to)
    rows = []
    for p in positions:
        rep = irr_report(conn, code=p["code"], date_to=date_to)
        rows.append(
            {
                "code": p["code"],
                "name": p["name"],
                "closed": p["closed"],
                "value": p["value"],
                "cumulative": rep["cumulative"],
                "annualized": rep["annualized"],
                "days": rep["days"],
                "reason": rep["reason"],
                "pnl": rep["pnl"],
                "invested": rep["invested"],
            }
        )
    rows.sort(key=lambda r: (r["closed"], -(r["value"] or 0)))
    return {"rows": rows, "portfolio": portfolio_irr(conn, date_to=date_to)}


# ---------------------------------------------------------------- 收益归因


def attribution(conn, date_to=None):
    """把组合盈亏拆解到每只基金的贡献。

    三层拆解：
    - contribution_pp  对组合收益率的百分点贡献。各基金相加恰好等于组合
                       总收益率，可直接看出「谁把收益拉低了多少个点」
    - share_of_loss    亏损基金的亏损额占组合总亏损的比例
    - by_category      按资产类别汇总，区分「配错类别」与「选错基金」

    为什么不用「盈亏 ÷ 组合净盈亏」：总盈亏为负时，盈利基金算出来是负数，
    既不直观也无法相加。改用「盈亏 ÷ 组合总投入」后天然可加总。
    """
    from . import engine

    positions = engine.build_positions(conn, date_to=date_to)
    positions = [p for p in positions if p["total_buy"] > EPS]
    if not positions:
        return {"rows": [], "summary": {}, "by_category": [], "total_pnl": 0.0}

    total_pnl = sum(p["unrealized"] + p["realized"] for p in positions)
    total_invested = sum(p["total_buy"] for p in positions)
    total_loss = sum(
        abs(p["unrealized"] + p["realized"])
        for p in positions
        if (p["unrealized"] + p["realized"]) < 0
    )
    total_gain = sum(
        p["unrealized"] + p["realized"]
        for p in positions
        if (p["unrealized"] + p["realized"]) > 0
    )

    rows = []
    for p in positions:
        pnl = p["unrealized"] + p["realized"]
        rows.append(
            {
                "code": p["code"],
                "name": p["name"],
                "category": p["category"],
                "category_name": p["category_name"],
                "closed": p["closed"],
                "invested": round(p["total_buy"], 2),
                "unrealized_pct": p["unrealized_pct"],
                "unrealized": round(p["unrealized"], 2),
                "realized": round(p["realized"], 2),
                "pnl": round(pnl, 2),
                "contribution_pp": round(pnl / total_invested * 100, 3)
                if total_invested > EPS
                else None,
                "share_of_loss": round(abs(pnl) / total_loss * 100, 2)
                if pnl < 0 and total_loss > EPS
                else None,
                "share_of_gain": round(pnl / total_gain * 100, 2)
                if pnl > 0 and total_gain > EPS
                else None,
            }
        )
    rows.sort(key=lambda r: -r["pnl"])

    # 按类别汇总
    by_cat = defaultdict(lambda: {"pnl": 0.0, "invested": 0.0, "count": 0})
    for r in rows:
        c = by_cat[r["category"]]
        c["pnl"] += r["pnl"]
        c["invested"] += r["invested"]
        c["count"] += 1
    cat_rows = [
        {
            "category": k,
            "category_name": db.CATEGORY_NAMES.get(k, k),
            "pnl": round(v["pnl"], 2),
            "invested": round(v["invested"], 2),
            "count": v["count"],
            "return_pct": round(v["pnl"] / v["invested"] * 100, 2) if v["invested"] > EPS else 0.0,
        }
        for k, v in by_cat.items()
    ]
    cat_rows.sort(key=lambda r: r["pnl"])

    worst = rows[0] if rows and rows[-1]["pnl"] < 0 else (rows[-1] if rows else None)
    losers = [r for r in rows if r["pnl"] < 0]
    gainers = [r for r in rows if r["pnl"] > 0]
    _ = worst

    return {
        "rows": rows,
        "total_pnl": round(total_pnl, 2),
        "total_invested": round(total_invested, 2),
        "total_loss": round(-total_loss, 2),
        "total_gain": round(total_gain, 2),
        "by_category": cat_rows,
        "summary": {
            "invested": round(total_invested, 2),
            "return_pct": round(total_pnl / total_invested * 100, 2)
            if total_invested > EPS
            else 0.0,
            "losers": len(losers),
            "gainers": len(gainers),
            "worst": min(losers, key=lambda r: r["pnl"])["code"] if losers else None,
            "best": max(gainers, key=lambda r: r["pnl"])["code"] if gainers else None,
            "top_loss_share": max(
                (r["share_of_loss"] for r in losers if r["share_of_loss"] is not None),
                default=None,
            ),
        },
    }


# ---------------------------------------------------------------- 对账校准


# 差异容忍度：份额或成本价相对偏差超过这个比例才提示
RECONCILE_TOLERANCE = 0.01


def reconcile_one(conn, code, platform_shares=None, platform_avg_cost=None):
    """把一只基金的账面值与你从平台看到的数字比对。

    自建账本最危险的失败模式是静默错误：录入时份额或成本填错一位，
    之后所有盈亏都是错的，而且看不出来。对账就是主动暴露这类偏差。

    返回差异明细，不做任何自动修正——改不改由人决定。
    """
    from . import engine

    pos = {p["code"]: p for p in engine.build_positions(conn)}.get(code)
    if not pos:
        return {"code": code, "ok": False, "error": "该基金没有持仓记录"}

    mine_shares = pos["shares"]
    mine_cost = pos["avg_cost"]
    items = []
    ok = True

    if platform_shares is not None and platform_shares >= 0:
        diff = mine_shares - platform_shares
        pct = (diff / platform_shares * 100) if platform_shares > EPS else None
        within = pct is not None and abs(pct) <= RECONCILE_TOLERANCE * 100
        if abs(diff) > EPS and not within:
            ok = False
        items.append(
            {
                "field": "份额",
                "mine": round(mine_shares, 4),
                "platform": round(platform_shares, 4),
                "diff": round(diff, 4),
                "diff_pct": round(pct, 3) if pct is not None else None,
                "within": within,
                "hint": _share_hint(diff, mine_shares, platform_shares),
            }
        )

    if platform_avg_cost is not None and platform_avg_cost > 0:
        diff = mine_cost - platform_avg_cost
        pct = (diff / platform_avg_cost * 100) if platform_avg_cost > EPS else None
        within = pct is not None and abs(pct) <= RECONCILE_TOLERANCE * 100
        if abs(diff) > EPS and not within:
            ok = False
        items.append(
            {
                "field": "成本单价",
                "mine": round(mine_cost, 4),
                "platform": round(platform_avg_cost, 4),
                "diff": round(diff, 4),
                "diff_pct": round(pct, 3) if pct is not None else None,
                "within": within,
                "hint": _cost_hint(diff, platform_avg_cost),
            }
        )

    return {
        "code": code,
        "name": pos["name"],
        "ok": ok,
        "items": items,
        "value": pos["value"],
        "cost": pos["cost"],
        "shares": round(mine_shares, 4),
        "avg_cost": round(mine_cost, 4),
        "tolerance_pct": RECONCILE_TOLERANCE * 100,
    }


def _share_hint(diff, mine, platform):
    if mine > platform:
        return "你的份额多于平台，可能漏记了一笔卖出，或多记了买入份额"
    return "你的份额少于平台，可能漏记了买入或定投，或多记了一笔卖出"


def _cost_hint(diff, platform_cost):
    if diff > 0:
        return (
            "你的成本高于平台。常见原因：分红方式不同（平台红利再投会摊薄单价）、"
            "漏记了红利再投、或手续费被重复计入"
        )
    return (
        "你的成本低于平台。常见原因：漏记申购手续费、或有红包/费率优惠未记录"
    )


def reconcile_check(conn, payload):
    """批量对账。payload 形如 [{code, shares, avg_cost}, ...]"""
    results = []
    for item in payload:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        shares = item.get("shares")
        cost = item.get("avg_cost")
        results.append(
            reconcile_one(
                conn,
                code,
                float(shares) if shares not in (None, "") else None,
                float(cost) if cost not in (None, "") else None,
            )
        )
    bad = [r for r in results if not r.get("ok")]
    return {
        "items": results,
        "summary": {
            "checked": len(results),
            "matched": len(results) - len(bad),
            "mismatched": len(bad),
            "tolerance_pct": RECONCILE_TOLERANCE * 100,
        },
    }


# ---------------------------------------------------------------- 基准对比


def _close_on_or_before(closes, ds):
    """取 ds 当天或之前最近的收盘价。非交易日自动向前顺延。"""
    if ds in closes:
        return closes[ds]
    prior = [d for d in closes if d <= ds]
    return closes[max(prior)] if prior else None


def benchmark_compare(conn, secid="1.000300", date_to=None):
    """同样现金流、换成基准指数，做等口径对比。

    这是「如果每笔钱都在同一时点买沪深300，现在会怎样」。比单纯看
    指数涨幅更有意义，因为它复现了你的定投节奏与加减仓时点——
    差别只剩「买什么」这一项，排除了择时差异。

    卖出也会同步模拟：你在某天赎回多少，基准就在同一天卖出同等金额。
    """
    row = conn.execute("SELECT name FROM benchmarks WHERE secid = ?", (secid,)).fetchone()
    if not row:
        return {"error": f"基准 {secid} 未同步，请先更新"}
    bench_name = row["name"]

    nav_rows = conn.execute(
        "SELECT date, close FROM benchmark_nav WHERE secid = ? ORDER BY date", (secid,)
    ).fetchall()
    if not nav_rows:
        return {"error": f"基准 {secid} 没有价格数据"}
    # 统一用 date 对象做键：build_flows 返回的是 date，字符串比较会 TypeError
    closes = {date.fromisoformat(r["date"]): float(r["close"]) for r in nav_rows}

    flows_all, meta = build_flows(conn, date_to=date_to)
    cash_flows, _ = build_flows(conn, date_to=date_to, include_terminal=False)
    asof = meta["asof"]
    my_terminal = meta["terminal_value"]

    if not cash_flows:
        return {"error": "没有可对比的现金流"}

    # 模拟基准持仓
    units = 0.0
    skipped = []
    for d, cf in cash_flows:
        px = _close_on_or_before(closes, d)
        if px is None or px <= 0:
            skipped.append(d)
            continue
        if cf < 0:
            units += -cf / px
        else:
            units -= cf / px
            if units < 0:
                units = 0.0

    last_px = _close_on_or_before(closes, date.fromisoformat(asof))
    if last_px is None:
        return {"error": "基准在估值日之前没有价格"}
    bench_terminal = units * last_px
    bench_flows = cash_flows + ([(date.fromisoformat(asof), bench_terminal)] if bench_terminal > EPS else [])

    # 两边的累计与年化
    def _metrics(flows, terminal):
        invested = sum(-cf for _, cf in flows if cf < 0)
        returned = sum(cf for _, cf in flows if cf > 0)
        pnl = returned - invested
        days = (flows[-1][0] - flows[0][0]).days if len(flows) > 1 else 0
        ann = None
        if days >= MIN_ANNUALIZE_DAYS:
            r = solve_irr(flows)
            ann = round(r * 100, 2) if r is not None else None
        return {
            "invested": round(invested, 2),
            "terminal": round(terminal, 2),
            "pnl": round(pnl, 2),
            "cumulative": round(pnl / invested * 100, 2) if invested > EPS else None,
            "annualized": ann,
            "days": days,
        }

    mine = _metrics(flows_all, my_terminal)
    bench = _metrics(bench_flows, bench_terminal)

    # 指数在区间内的原始涨跌，作为背景参考
    first_px = _close_on_or_before(closes, cash_flows[0][0])
    index_return = round((last_px / first_px - 1) * 100, 2) if first_px else None

    diff_cum = None
    if mine["cumulative"] is not None and bench["cumulative"] is not None:
        diff_cum = round(mine["cumulative"] - bench["cumulative"], 2)
    diff_ann = None
    if mine["annualized"] is not None and bench["annualized"] is not None:
        diff_ann = round(mine["annualized"] - bench["annualized"], 2)

    return {
        "benchmark": {"secid": secid, "name": bench_name},
        "asof": asof,
        "mine": mine,
        "benchmark_metrics": bench,
        "diff_cumulative": diff_cum,
        "diff_annualized": diff_ann,
        "index_return": index_return,
        "benchmark_units": round(units, 4),
        "benchmark_price": {"first": first_px, "last": last_px},
        "skipped_dates": skipped[:10],
        "verdict": _verdict(diff_cum),
    }


def _verdict(diff):
    if diff is None:
        return None
    if diff > 2:
        return "跑赢基准"
    if diff < -2:
        return "跑输基准"
    return "与基准基本持平"


def benchmark_series(conn, secid="1.000300", date_from=None, date_to=None):
    """把基准换算成与组合同起点归一化的曲线，供图上叠加。

    以两条曲线的第一个共同交易日为 100，比较相对走势。
    """
    from . import engine

    rows = conn.execute(
        "SELECT date, close FROM benchmark_nav WHERE secid = ? AND date >= ? AND date <= ? ORDER BY date",
        (secid, date_from or "2000-01-01", date_to or "2100-01-01"),
    ).fetchall()
    if not rows:
        return {"points": []}
    base = float(rows[0]["close"])
    return {
        "points": [
            {"date": r["date"], "value": round(float(r["close"]) / base * 100, 2)}
            for r in rows
        ]
    }
