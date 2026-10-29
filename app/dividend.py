"""分红识别：从累计净值与单位净值的差值跳变，反推历史分红。

原理
----
基金的「累计净值」（acc_nav）把历史每次分红都加回去了，所以：

    acc_nav − nav = 该基金成立以来每份累计分红

这个差值只在分红除息日发生跳变，跳变幅度就是当次每份分红额。
不需要任何外部接口，库里的两列净值已经包含全部信息。

两条口径必须分清
----------------
- **除息日**：差值跳变的那一天，当天净值已经扣掉了分红
- **权益登记日**：通常是除息日的上一个交易日，只有当天收盘时持有的份额
  才有权分红

所以分红金额要用「登记日份额 × 每份分红」，不能用除息日份额——
如果用户在除息日当天买入了，那笔买入不该参与这次分红。
"""

from datetime import date, timedelta

from . import db

# 差值跳变的判定阈值。nav 是 4 位小数，差值精度在 1e-4 量级，
# 取 5e-5 既能抓住最小分红（0.0001），又不会因四舍五入误报
JUMP_EPS = 0.00005

# 单次分红超过净值的这个比例，视为可疑（可能是份额折算/拆分被误判为分红）
SUSPICIOUS_RATIO = 0.20

# 判定「已记录」时，允许的金额相对误差
MATCH_TOLERANCE = 0.05


def _nav_rows(conn, code):
    return conn.execute(
        "SELECT date, nav, acc_nav, pct FROM nav "
        "WHERE code = ? AND nav IS NOT NULL AND acc_nav IS NOT NULL ORDER BY date",
        (code,),
    ).fetchall()


def detect_fund(conn, code):
    """扫描单只基金全部分红。返回原始检测结果（未判断是否已记录）。"""
    rows = _nav_rows(conn, code)
    if len(rows) < 2:
        return []

    out = []
    prev_gap = None
    prev_date = None
    for r in rows:
        gap = float(r["acc_nav"]) - float(r["nav"])
        if prev_gap is not None:
            delta = gap - prev_gap
            if delta > JUMP_EPS:
                nav_now = float(r["nav"])
                # 净值相对上一条的跌幅，用于判断是否为拆分导致的误判
                out.append(
                    {
                        "ex_date": r["date"],
                        "prev_date": prev_date,
                        "per_share": round(delta, 4),
                        "nav": round(nav_now, 4),
                        "pct": r["pct"],
                        "gap_before": round(prev_gap, 4),
                        "gap_after": round(gap, 4),
                        "suspicious": nav_now > 0 and delta / nav_now > SUSPICIOUS_RATIO,
                    }
                )
        prev_gap, prev_date = gap, r["date"]
    return out


def _shares_on(conn, code, ds):
    """某日收盘后的持仓份额（含当日买入、扣除当日卖出）。"""
    s = 0.0
    for t in conn.execute(
        "SELECT action, shares FROM txns WHERE code = ? AND date <= ? ORDER BY date, id",
        (code, ds),
    ).fetchall():
        sh = float(t["shares"] or 0)
        if t["action"] in db.BUY_ACTIONS:
            s += sh
        elif t["action"] in db.SELL_ACTIONS:
            s -= sh
        elif t["action"] == "dividend" and sh > 0:
            s += sh
    return max(s, 0.0)


def _prev_trading_day(conn, code, ds):
    """取 ds 之前最近一个有净值的日期。没有则返回 None。"""
    row = conn.execute(
        "SELECT date FROM nav WHERE code = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (code, ds),
    ).fetchone()
    return row["date"] if row else None


def _existing_dividends(conn, code):
    """该基金已有的分红记录。用于判断哪些分红已经入过账。"""
    return conn.execute(
        "SELECT id, date, amount, shares, nav FROM txns "
        "WHERE code = ? AND action = 'dividend' ORDER BY date",
        (code,),
    ).fetchall()


def _match_existing(existing, ex_date, expected_cash):
    """找与该次分红对应的已有记录。

    判定条件：日期相差不超过 15 天，且金额或再投份额能对上。
    金额对不上也算命中，因为用户可能手动改过数值——
    宁可判为「已记录」也不要重复入账。
    """
    ex = date.fromisoformat(ex_date)
    best = None
    for r in existing:
        try:
            gap_days = abs((date.fromisoformat(r["date"]) - ex).days)
        except (ValueError, TypeError):
            continue
        if gap_days > 15:
            continue
        amt = float(r["amount"] or 0)
        if expected_cash <= 0:
            ok = True
        else:
            diff = abs(amt - expected_cash) / expected_cash
            # 红利再投的 amount 记的是再投金额，与现金额一致；
            # 若用户只填了份额没填金额，则按份额对应的净值折算，容差放宽
            ok = diff <= MATCH_TOLERANCE or amt > 0 and diff <= 0.5
        if ok and (best is None or gap_days < best[0]):
            best = (gap_days, r)
    return best[1] if best else None


def scan(conn, code=None, include_recorded=True):
    """扫描分红。默认返回全部（已记录与未记录都列出，供核对）。

    只返回「除息日时确实持有份额」的分红——没持仓的分红与用户无关。
    """
    codes = [code] if code else [
        r["code"] for r in conn.execute("SELECT code FROM funds ORDER BY code").fetchall()
    ]

    items = []
    for c in codes:
        fund = conn.execute("SELECT name, is_money FROM funds WHERE code = ?", (c,)).fetchone()
        if not fund:
            continue
        if fund["is_money"]:
            continue  # 货币基金走万份收益，没有净值型分红

        existing = _existing_dividends(conn, c)
        for d in detect_fund(conn, c):
            record_date = d["prev_date"] or _prev_trading_day(conn, c, d["ex_date"])
            if not record_date:
                continue
            shares = _shares_on(conn, c, record_date)
            if shares <= 0:
                continue  # 登记日没持仓，这次分红与用户无关

            cash = round(shares * d["per_share"], 2)
            # 红利再投：按除息日净值折算新增份额
            reinvest = round(cash / d["nav"], 4) if d["nav"] > 0 else 0.0
            hit = _match_existing(existing, d["ex_date"], cash)

            items.append(
                {
                    "code": c,
                    "name": fund["name"],
                    "ex_date": d["ex_date"],
                    "record_date": record_date,
                    "per_share": d["per_share"],
                    "shares": round(shares, 4),
                    "cash": cash,
                    "nav": d["nav"],
                    "reinvest_shares": reinvest,
                    "suspicious": d["suspicious"],
                    "status": "recorded" if hit else "pending",
                    "existing_id": hit["id"] if hit else None,
                    "existing_amount": round(float(hit["amount"] or 0), 2) if hit else None,
                }
            )

    items.sort(key=lambda x: (x["status"] != "pending", x["ex_date"]), reverse=False)
    pending = [x for x in items if x["status"] == "pending"]
    return {
        "items": items if include_recorded else pending,
        "summary": {
            "total": len(items),
            "pending": len(pending),
            "recorded": len(items) - len(pending),
            "pending_cash": round(sum(x["cash"] for x in pending), 2),
        },
    }


def _find_detected(conn, code, ex_date):
    """在检测结果里找指定的一次分红，用于自动填充金额与份额。"""
    for d in detect_fund(conn, code):
        if d["ex_date"] == ex_date:
            record_date = d["prev_date"] or _prev_trading_day(conn, code, ex_date)
            if not record_date:
                return None
            shares = _shares_on(conn, code, record_date)
            if shares <= 0:
                return None
            cash = round(shares * d["per_share"], 2)
            return {
                "cash": cash,
                "shares": round(shares, 4),
                "nav": d["nav"],
                "reinvest_shares": round(cash / d["nav"], 4) if d["nav"] > 0 else 0.0,
            }
    return None


def apply(conn, payload):
    """把检测到的分红写入流水。

    payload 每项：
        code, ex_date, mode('cash'|'reinvest')
        可选 amount / shares 覆盖计算值（用户手动调整）；
        不传则由检测结果自动计算。
    """
    results = []
    for it in payload:
        code = str(it.get("code") or "").strip()
        ex_date = str(it.get("ex_date") or "").strip()
        mode = str(it.get("mode") or "cash")
        if not code or not ex_date:
            results.append({"ok": False, "error": "缺少基金代码或除息日"})
            continue
        if mode not in ("cash", "reinvest"):
            results.append({"ok": False, "error": f"未知分红方式：{mode}"})
            continue

        fund = conn.execute("SELECT name FROM funds WHERE code = ?", (code,)).fetchone()
        if not fund:
            results.append({"ok": False, "error": f"基金 {code} 不存在"})
            continue

        # 幂等：同基金同除息日附近已有分红记录则跳过
        dup = conn.execute(
            "SELECT id FROM txns WHERE code = ? AND action = 'dividend' "
            "AND date BETWEEN date(?, '-15 day') AND date(?, '+15 day')",
            (code, ex_date, ex_date),
        ).fetchone()
        if dup:
            results.append({"ok": False, "code": code, "ex_date": ex_date,
                            "error": "该次分红已有记录，未重复写入", "existing_id": dup["id"]})
            continue

        detected = _find_detected(conn, code, ex_date)
        if detected is None:
            results.append({"ok": False, "code": code, "ex_date": ex_date,
                            "error": "未在净值数据中检测到该次分红，或当时无持仓"})
            continue

        try:
            amount = float(it["amount"]) if it.get("amount") not in (None, "") else detected["cash"]
        except (TypeError, ValueError):
            results.append({"ok": False, "error": "金额格式不正确"})
            continue
        try:
            shares = float(it["shares"]) if it.get("shares") not in (None, "") else None
        except (TypeError, ValueError):
            results.append({"ok": False, "error": "份额格式不正确"})
            continue

        if amount is None or amount <= 0:
            results.append({"ok": False, "code": code, "ex_date": ex_date,
                            "error": "分红金额必须大于 0"})
            continue

        if mode == "reinvest":
            if shares is None:
                shares = detected["reinvest_shares"]
            if shares <= 0:
                results.append({"ok": False, "code": code, "ex_date": ex_date,
                                "error": "红利再投需要填写新增份额"})
                continue
            nav_row = conn.execute(
                "SELECT nav FROM nav WHERE code = ? AND date = ?", (code, ex_date)
            ).fetchone()
            nav = float(nav_row["nav"]) if nav_row and nav_row["nav"] else detected["nav"]
            note = "红利再投（自动识别）"
        else:
            shares = 0.0
            nav = None
            note = "现金分红（自动识别）"

        cur = conn.execute(
            "INSERT INTO txns (code, date, action, amount, shares, nav, fee, note, created_at) "
            "VALUES (?,?, 'dividend', ?, ?, ?, 0, ?, datetime('now'))",
            (code, ex_date, round(amount, 2), round(shares, 4), nav, note),
        )
        results.append(
            {
                "ok": True,
                "id": cur.lastrowid,
                "code": code,
                "ex_date": ex_date,
                "mode": mode,
                "amount": round(amount, 2),
                "shares": round(shares, 4),
            }
        )

    conn.commit()
    ok = [r for r in results if r.get("ok")]
    return {
        "results": results,
        "written": len(ok),
        "total_amount": round(sum(r["amount"] for r in ok), 2),
    }
