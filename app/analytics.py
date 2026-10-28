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


def build_flows(conn, code=None, date_to=None):
    """构建一组现金流：流出为负、流入为正，末尾追加期末市值。

    返回 (flows, meta)。flows 为 [(date, amount)]，按日期升序。
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

    if terminal_value > EPS:
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
