"""计算引擎：持仓成本、盈亏、组合曲线、模板偏离与再平衡。

金额口径：txns.amount 一律是「账户实际发生的现金额」——买入时是从账户扣走的总金额
（已含手续费），卖出时是赎回确认的总金额（未扣赎回费，赎回费另记在 fee）。
因此成本直接累加 amount，不重复加 fee。

成本口径采用移动加权平均法（场外基金业内通用）：
- 买入：成本 += 扣款总额，份额 += 确认份额
- 卖出：按卖出当时均价释放成本，已实现盈亏 += (赎回金额 - 赎回费) - 释放成本
- 红利再投：份额增加、成本不变（摊薄单价）
- 现金分红：成本不变，全额计入已实现盈亏
"""

from collections import defaultdict

from . import db

EPS = 1e-9


# ---------------------------------------------------------------- 基础读取


def load_funds(conn):
    rows = conn.execute(
        "SELECT code, name, category, fund_type, company, is_money, note FROM funds ORDER BY code"
    ).fetchall()
    return {r["code"]: dict(r) for r in rows}


def load_txns(conn, code=None, date_from=None, date_to=None):
    sql = "SELECT * FROM txns WHERE 1=1"
    args = []
    if code:
        sql += " AND code = ?"
        args.append(code)
    if date_from:
        sql += " AND date >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND date <= ?"
        args.append(date_to)
    sql += " ORDER BY date ASC, id ASC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def nav_series(conn, code, date_to=None):
    sql = "SELECT date, nav, acc_nav, pct FROM nav WHERE code = ?"
    args = [code]
    if date_to:
        sql += " AND date <= ?"
        args.append(date_to)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def latest_nav(conn, code):
    row = conn.execute(
        "SELECT date, nav, acc_nav, pct FROM nav WHERE code = ? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def prev_nav(conn, code):
    row = conn.execute(
        "SELECT date, nav, pct FROM nav WHERE code = ? ORDER BY date DESC LIMIT 1 OFFSET 1",
        (code,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 成本计算


def replay(txns):
    """按时间顺序重放流水，返回该基金的持仓状态与逐笔快照。"""
    shares = 0.0
    cost = 0.0
    realized = 0.0
    total_buy = 0.0
    total_sell = 0.0
    total_fee = 0.0
    steps = []

    for t in txns:
        action = t["action"]
        amt = float(t.get("amount") or 0)
        sh = float(t.get("shares") or 0)
        fee = float(t.get("fee") or 0)
        before_shares = shares
        before_cost = cost
        pnl = 0.0

        if action in db.BUY_ACTIONS:
            shares += sh
            # amount 是账户实际扣走的总额（已含手续费），所以成本直接加 amount，
            # 不能再加一次 fee，否则手续费会被重复计入成本
            cost += amt
            total_buy += amt
            total_fee += fee
        elif action in db.SELL_ACTIONS:
            avg = before_cost / before_shares if before_shares > EPS else 0.0
            release = min(sh, before_shares) * avg
            net = amt - fee
            pnl = net - release
            realized += pnl
            cost -= release
            shares -= sh
            total_sell += net
            total_fee += fee
        elif action == "dividend":
            if sh > 0:  # 红利再投
                shares += sh
            else:  # 现金分红
                net = amt - fee
                pnl = net
                realized += pnl
                total_sell += net
                total_fee += fee

        if shares < EPS:
            shares = 0.0
        if cost < EPS:
            cost = 0.0

        steps.append(
            {
                "id": t["id"],
                "date": t["date"],
                "action": action,
                "pnl": round(pnl, 4),
                "shares_after": round(shares, 4),
                "cost_after": round(cost, 4),
            }
        )

    return {
        "shares": shares,
        "cost": cost,
        "realized": realized,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "total_fee": total_fee,
        "steps": steps,
    }


def build_positions(conn, date_to=None):
    """构建全部持仓，附带最新净值、当日涨跌幅、浮动盈亏。"""
    funds = load_funds(conn)
    txns = load_txns(conn, date_to=date_to)
    by_code = defaultdict(list)
    for t in txns:
        by_code[t["code"]].append(t)

    positions = []
    for code, fund in funds.items():
        rows = by_code.get(code, [])
        if not rows:
            continue
        state = replay(rows)
        if state["shares"] <= EPS and abs(state["realized"]) < EPS:
            continue

        ln = latest_nav(conn, code) if date_to is None else _nav_on(conn, code, date_to)
        pv = _nav_before(conn, code, ln["date"]) if ln else None

        nav = float(ln["nav"]) if ln and ln.get("nav") is not None else None
        pct = float(ln["pct"]) if ln and ln.get("pct") is not None else None
        value = state["shares"] * nav if nav is not None else 0.0
        today_pnl = 0.0

        if fund["is_money"]:
            # 货币基金：市值 = 份额（1 元面值）+ 累计万份收益
            accrued, last_day = accrue_money(conn, code, rows, date_to)
            value = state["shares"] + accrued
            today_pnl = last_day if state["shares"] > EPS else 0.0
            pct = None
        elif nav is not None and pv and pv.get("nav"):
            today_pnl = state["shares"] * (nav - float(pv["nav"]))

        unrealized = value - state["cost"]

        avg_cost = state["cost"] / state["shares"] if state["shares"] > EPS else 0.0
        positions.append(
            {
                "code": code,
                "name": fund["name"],
                "category": fund["category"],
                "category_name": db.CATEGORY_NAMES.get(fund["category"], "其他"),
                "fund_type": fund["fund_type"] or "",
                "is_money": bool(fund["is_money"]),
                "shares": round(state["shares"], 4),
                "cost": round(state["cost"], 2),
                "avg_cost": round(avg_cost, 4),
                "nav": nav,
                "nav_date": ln["date"] if ln else None,
                "pct": pct,
                "value": round(value, 2),
                "unrealized": round(unrealized, 2),
                "unrealized_pct": round(unrealized / state["cost"] * 100, 2) if state["cost"] > EPS else 0.0,
                "realized": round(state["realized"], 2),
                "today_pnl": round(today_pnl, 2),
                "total_buy": round(state["total_buy"], 2),
                "total_sell": round(state["total_sell"], 2),
                "total_fee": round(state["total_fee"], 2),
                "closed": state["shares"] <= EPS,
            }
        )

    positions.sort(key=lambda p: (-p["value"], p["code"]))
    return positions


def _nav_on(conn, code, date_str):
    row = conn.execute(
        "SELECT date, nav, acc_nav, pct FROM nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (code, date_str),
    ).fetchone()
    return dict(row) if row else None


def _nav_before(conn, code, date_str):
    row = conn.execute(
        "SELECT date, nav, pct FROM nav WHERE code = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (code, date_str),
    ).fetchone()
    return dict(row) if row else None


def accrue_money(conn, code, txns, date_to=None):
    """货币基金累计收益。

    货币基金净值恒为 1.0000，收益体现在每日万份收益上（相当于每天按份额派息）。
    这里按「持仓份额 × 当日日收益率」逐日累加，得到累计收益金额。
    """
    if not txns:
        return 0.0, 0.0

    sql = "SELECT date, pct FROM nav WHERE code = ? AND pct IS NOT NULL"
    args = [code]
    if date_to:
        sql += " AND date <= ?"
        args.append(date_to)
    sql += " ORDER BY date ASC"
    navs = conn.execute(sql, args).fetchall()
    if not navs:
        return 0.0, 0.0

    deltas = {}
    for t in txns:
        sh = float(t.get("shares") or 0)
        if t["action"] in db.BUY_ACTIONS:
            deltas[t["date"]] = deltas.get(t["date"], 0.0) + sh
        elif t["action"] in db.SELL_ACTIONS:
            deltas[t["date"]] = deltas.get(t["date"], 0.0) - sh
        elif t["action"] == "dividend" and sh > 0:
            deltas[t["date"]] = deltas.get(t["date"], 0.0) + sh

    pending = sorted(deltas.items())
    first = txns[0]["date"]
    shares = 0.0
    income = 0.0
    last_day_income = 0.0
    i = 0
    for r in navs:
        # 只结算「当日之前」的份额变动：买入当天不计息（T+1 确认份额才起息），
        # 这样与组合曲线的口径一致，市值和曲线终点才不会差几分钱
        while i < len(pending) and pending[i][0] < r["date"]:
            shares += pending[i][1]
            i += 1
        if r["date"] >= first and shares > 0:
            day_income = shares * (r["pct"] or 0) / 100.0
            income += day_income
            last_day_income = day_income
    return income, last_day_income


def summarize(positions):
    """汇总组合层面指标。"""
    holding = [p for p in positions if not p["closed"]]
    value = sum(p["value"] for p in holding)
    cost = sum(p["cost"] for p in holding)
    today = sum(p["today_pnl"] for p in holding)
    realized = sum(p["realized"] for p in positions)
    unrealized = value - cost
    total_buy = sum(p["total_buy"] for p in positions)
    total_fee = sum(p["total_fee"] for p in positions)

    prev_value = value - today
    return {
        "value": round(value, 2),
        "cost": round(cost, 2),
        "today_pnl": round(today, 2),
        "today_pct": round(today / prev_value * 100, 3) if prev_value > EPS else 0.0,
        "unrealized": round(unrealized, 2),
        "unrealized_pct": round(unrealized / cost * 100, 2) if cost > EPS else 0.0,
        "realized": round(realized, 2),
        "total_pnl": round(unrealized + realized, 2),
        "total_buy": round(total_buy, 2),
        "total_fee": round(total_fee, 2),
        "holdings": len(holding),
    }


# ---------------------------------------------------------------- 组合曲线


def portfolio_series(conn, date_from, date_to):
    """区间内逐日组合市值、成本、当日盈亏。

    当日盈亏用「日初份额 × 净值变动」计算，避免把当天申购的钱算成收益。
    """
    funds = load_funds(conn)
    txns = load_txns(conn, date_from=date_from, date_to=date_to)
    codes = sorted({t["code"] for t in txns} | set(funds.keys()))

    # 每只基金在区间内的净值表
    navmap = {}
    for code in codes:
        rows = conn.execute(
            "SELECT date, nav, pct FROM nav WHERE code = ? AND date <= ? ORDER BY date ASC",
            (code, date_to),
        ).fetchall()
        if rows:
            navmap[code] = {r["date"]: (float(r["nav"]), r["pct"]) for r in rows}

    # 交易事件
    events = defaultdict(list)
    for t in txns:
        events[t["date"]].append(t)

    # 日期轴：区间内所有净值日的并集，再补上「有交易但没有净值」的日期
    # （例如今天买入、今天净值还没公布，这笔钱也要计入市值，否则曲线终点会和持仓市值对不上）
    all_dates = sorted(
        {d for m in navmap.values() for d in m if date_from <= d <= date_to}
        | {d for d in events if date_from <= d <= date_to}
    )
    if not all_dates:
        return {"points": [], "stats": {}}

    # 初始化：把区间起点前的交易先结算掉
    shares = defaultdict(float)
    cost = defaultdict(float)
    for t in conn.execute(
        "SELECT * FROM txns WHERE date < ? ORDER BY date ASC, id ASC", (date_from,)
    ).fetchall():
        _apply_event(shares, cost, dict(t))

    last_nav = {}
    for code, m in navmap.items():
        prior = [d for d in m if d < date_from]
        if prior:
            last_nav[code] = m[max(prior)][0]

    points = []
    prev_value = None
    money_income = defaultdict(float)
    for d in all_dates:
        # 更新当日净值（含区间前最后一个净值做前向填充）
        for code, m in navmap.items():
            if d in m:
                last_nav[code] = m[d][0]

        start_shares = dict(shares)
        # 当日盈亏：日初份额 × 净值变动
        day_pnl = 0.0
        for code, sh in start_shares.items():
            if sh <= EPS:
                continue
            m = navmap.get(code)
            if not m:
                continue
            cur = m.get(d)
            if not cur:
                continue
            prev_dates = [x for x in m if x < d]
            if not prev_dates:
                continue
            prev_nav = m[max(prev_dates)][0]
            day_pnl += sh * (cur[0] - prev_nav)

        # 货币基金净值恒为 1，收益按万份收益逐日累计
        for code, sh in start_shares.items():
            if sh <= EPS or not (funds.get(code) or {}).get("is_money"):
                continue
            m = navmap.get(code)
            if not m or d not in m:
                continue
            inc = sh * (m[d][1] or 0) / 100.0
            money_income[code] += inc
            day_pnl += inc

        # 应用当日交易
        for t in events.get(d, []):
            _apply_event(shares, cost, t)

        value = sum(shares[c] * last_nav.get(c, 0.0) for c in shares if shares[c] > EPS)
        value += sum(money_income.values())
        total_cost = sum(cost.values())

        points.append(
            {
                "date": d,
                "value": round(value, 2),
                "cost": round(total_cost, 2),
                "pnl": round(day_pnl, 2),
                "pct": round(day_pnl / prev_value * 100, 3) if prev_value and prev_value > EPS else 0.0,
            }
        )
        prev_value = value

    first = points[0]
    last = points[-1]
    # 起点对齐到首次真正有持仓的交易日，避免导出里出现一长串 ¥0.00 的空月份
    for p in points:
        if p["value"] > EPS:
            first = p
            break
    peak = 0.0
    max_dd = 0.0
    for p in points:
        if p["value"] <= EPS and peak <= EPS:
            continue
        peak = max(peak, p["value"])
        if peak > EPS:
            max_dd = min(max_dd, (p["value"] - peak) / peak * 100)

    return {
        "points": points,
        "stats": {
            "start_date": first["date"],
            "end_date": last["date"],
            "start_value": first["value"],
            "end_value": last["value"],
            "change": round(last["value"] - first["value"], 2),
            "max_drawdown": round(max_dd, 2),
            "days": len(points),
        },
    }


def _apply_event(shares, cost, t):
    action = t["action"]
    amt = float(t.get("amount") or 0)
    sh = float(t.get("shares") or 0)
    fee = float(t.get("fee") or 0)
    code = t["code"]

    if action in db.BUY_ACTIONS:
        shares[code] += sh
        cost[code] += amt  # amount 已含手续费
    elif action in db.SELL_ACTIONS:
        before = shares[code]
        avg = cost[code] / before if before > EPS else 0.0
        release = min(sh, before) * avg
        cost[code] -= release
        shares[code] = before - sh
        if shares[code] < EPS:
            shares[code] = 0.0
        if cost[code] < EPS:
            cost[code] = 0.0
    elif action == "dividend" and sh > 0:
        shares[code] += sh


# ---------------------------------------------------------------- 配置分析


def load_templates(conn):
    out = []
    for t in conn.execute("SELECT * FROM templates ORDER BY builtin DESC, id ASC").fetchall():
        items = conn.execute(
            "SELECT category, target FROM template_items WHERE template_id = ? ORDER BY target DESC",
            (t["id"],),
        ).fetchall()
        out.append(
            {
                "id": t["id"],
                "name": t["name"],
                "description": t["description"] or "",
                "threshold": t["threshold"],
                "builtin": bool(t["builtin"]),
                "items": [
                    {
                        "category": i["category"],
                        "category_name": db.CATEGORY_NAMES.get(i["category"], i["category"]),
                        "target": i["target"],
                    }
                    for i in items
                ],
            }
        )
    return out


def analyze_allocation(conn, template_id=None, extra_cash=0.0):
    """实际配置 vs 模板目标，给出偏离与再平衡建议。

    extra_cash > 0 时按「只买不卖」模式，把新资金分配到低配类别。
    """
    positions = [p for p in build_positions(conn) if not p["closed"]]
    total = sum(p["value"] for p in positions)

    by_cat = defaultdict(float)
    funds_by_cat = defaultdict(list)
    for p in positions:
        by_cat[p["category"]] += p["value"]
        funds_by_cat[p["category"]].append(p)

    tpl = None
    if template_id:
        for t in load_templates(conn):
            if str(t["id"]) == str(template_id):
                tpl = t
                break

    rows = []
    if tpl:
        base = total + max(extra_cash, 0.0)
        targets = {i["category"]: i["target"] for i in tpl["items"]}
        th = float(tpl["threshold"])
        for item in tpl["items"]:
            cat = item["category"]
            target = float(item["target"])
            actual_value = by_cat.get(cat, 0.0)
            actual_pct = actual_value / total * 100 if total > EPS else 0.0
            target_value = base * target / 100
            gap = target_value - actual_value
            diff = actual_pct - target

            if abs(diff) <= th:
                status = "正常"
            elif diff > 0:
                status = "超配"
            else:
                status = "低配"

            if extra_cash > 0:
                suggest = max(gap, 0.0)
                side = "买入" if suggest > 0.01 else "不动"
            else:
                suggest = abs(gap)
                side = "买入" if gap > 0.01 else ("卖出" if gap < -0.01 else "不动")

            rows.append(
                {
                    "category": cat,
                    "category_name": item["category_name"],
                    "target_pct": target,
                    "actual_pct": round(actual_pct, 2),
                    "target_value": round(target_value, 2),
                    "actual_value": round(actual_value, 2),
                    "diff": round(diff, 2),
                    "status": status,
                    "suggest_side": side,
                    "suggest_amount": round(suggest, 2),
                }
            )
        # 模板未覆盖的类别
        for cat, val in by_cat.items():
            if cat in targets:
                continue
            rows.append(
                {
                    "category": cat,
                    "category_name": db.CATEGORY_NAMES.get(cat, cat),
                    "target_pct": 0.0,
                    "actual_pct": round(val / total * 100, 2) if total > EPS else 0.0,
                    "target_value": 0.0,
                    "actual_value": round(val, 2),
                    "diff": round(val / total * 100, 2) if total > EPS else 0.0,
                    "status": "模板外",
                    "suggest_side": "卖出" if extra_cash <= 0 else "不动",
                    "suggest_amount": round(val, 2) if extra_cash <= 0 else 0.0,
                }
            )
        rows.sort(key=lambda r: -r["target_pct"])

        # 「新增资金」模式：建议买入总额不能超过手头的新钱，超出则等比缩放
        if extra_cash > 0:
            need = sum(r["suggest_amount"] for r in rows if r["suggest_side"] == "买入")
            if need > extra_cash + 0.01 and need > EPS:
                factor = extra_cash / need
                for r in rows:
                    if r["suggest_side"] == "买入":
                        r["suggest_amount"] = round(r["suggest_amount"] * factor, 2)
            unallocated = extra_cash - sum(
                r["suggest_amount"] for r in rows if r["suggest_side"] == "买入"
            )
        else:
            unallocated = 0.0
    else:
        unallocated = 0.0
        for cat, val in sorted(by_cat.items(), key=lambda x: -x[1]):
            rows.append(
                {
                    "category": cat,
                    "category_name": db.CATEGORY_NAMES.get(cat, cat),
                    "target_pct": None,
                    "actual_pct": round(val / total * 100, 2) if total > EPS else 0.0,
                    "target_value": None,
                    "actual_value": round(val, 2),
                    "diff": None,
                    "status": "",
                    "suggest_side": "",
                    "suggest_amount": 0.0,
                }
            )

    # 类别下的具体基金分配
    alloc_detail = []
    alloc_missing = []
    for r in rows:
        if r["suggest_amount"] <= 0.01 or r["suggest_side"] == "不动":
            continue
        members = funds_by_cat.get(r["category"], [])
        if not members:
            alloc_missing.append(
                {
                    "category_name": r["category_name"],
                    "side": r["suggest_side"],
                    "amount": r["suggest_amount"],
                }
            )
            continue
        if r["suggest_side"] == "买入":
            weights = sum(m["value"] for m in members) or 0.0
            alloc_detail.append(
                {
                    "category_name": r["category_name"],
                    "side": "买入",
                    "items": [
                        {
                            "code": m["code"],
                            "name": m["name"],
                            "amount": round(
                                r["suggest_amount"] * (m["value"] / weights), 2
                            )
                            if weights > EPS
                            else round(r["suggest_amount"] / len(members), 2),
                        }
                        for m in members
                    ],
                }
            )
        else:
            weights = sum(m["value"] for m in members) or 0.0
            alloc_detail.append(
                {
                    "category_name": r["category_name"],
                    "side": "卖出",
                    "items": [
                        {
                            "code": m["code"],
                            "name": m["name"],
                            "amount": round(min(r["suggest_amount"] * (m["value"] / weights), m["value"]), 2),
                        }
                        for m in members
                    ],
                }
            )

    return {
        "template": tpl,
        "total_value": round(total, 2),
        "extra_cash": extra_cash,
        "unallocated": round(unallocated, 2),
        "threshold": float(tpl["threshold"]) if tpl else None,
        "rows": rows,
        "alloc_detail": alloc_detail,
        "alloc_missing": alloc_missing,
    }
