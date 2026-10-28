"""定投计划：持续有效的定期扣款规则，按需补齐到期记录。

和「批量定投」的区别：
- 批量定投（tools / 界面那个）是一次性把一段历史补进去，做完就结束
- 定投计划是一条持续规则，每次打开或点同步时，自动把「上次生成日到今天」
  之间到期的那几期补成正式流水，已存在的不重复生成

幂等性靠唯一约束保证：同一天同一只基金的 sip 记录只允许一条。
"""

from datetime import date, timedelta

from . import db

FREQ_NAMES = {"daily": "每个交易日", "weekly": "每周", "monthly": "每月"}
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 单个计划一次最多补生成多少期，防止误填起始日期导致生成上千条
MAX_CATCHUP = 400


def _trading_days(conn, start, end):
    """A 股交易日列表。取一只非货币基金的净值日——货币基金每个自然日都有净值。"""
    row = conn.execute(
        "SELECT code FROM funds WHERE is_money = 0 "
        "ORDER BY (SELECT COUNT(*) FROM nav WHERE nav.code = funds.code) DESC LIMIT 1"
    ).fetchone()
    if not row:
        return []
    return [
        r["date"]
        for r in conn.execute(
            "SELECT date FROM nav WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            (row["code"], start, end),
        ).fetchall()
    ]


def _due_dates(conn, plan, since, until):
    """算出应生成的扣款日。

    - daily  每个交易日
    - weekly 指定星期几（落在非交易日就顺延到下一个交易日）
    - monthly 每月指定日（同上顺延）

    已存在的同基金同日 sip 记录会被跳过，所以这里的重复不影响结果。
    """
    freq = plan["freq"]
    start = max(since, plan["start_date"])
    end = min(until, plan["end_date"]) if plan["end_date"] else until
    if start > end:
        return []

    calendar = _trading_days(conn, start, end)
    if not calendar:
        return []
    cal_set = set(calendar)

    if freq == "daily":
        return sorted(cal_set)

    out = set()
    if freq == "weekly":
        wd = plan["weekday"] if plan["weekday"] is not None else 0
        cur = date.fromisoformat(start)
        stop = date.fromisoformat(end)
        while cur <= stop:
            if cur.weekday() == wd:
                ds = cur.isoformat()
                if ds in cal_set:
                    out.add(ds)
                else:
                    # 落在周末或假期，顺延到下一个交易日
                    nxt = [d for d in calendar if d > ds]
                    if nxt:
                        out.add(nxt[0])
            cur += timedelta(days=1)
        return sorted(out)

    if freq == "monthly":
        md = plan["monthday"] or 1
        cur = date.fromisoformat(start)
        stop = date.fromisoformat(end)
        seen = set()
        while cur <= stop:
            key = (cur.year, cur.month)
            if key not in seen:
                seen.add(key)
                import calendar as _cal

                last = _cal.monthrange(cur.year, cur.month)[1]
                target = date(cur.year, cur.month, min(md, last))
                ds = target.isoformat()
                # 本月扣款日若早于窗口起点，说明本月已过，跳过
                # （否则会算出过去日期，或错误顺延到窗口首日）
                if ds < start:
                    cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
                    continue
                if ds in cal_set:
                    out.add(ds)
                else:
                    nxt = [d for d in calendar if d >= ds]
                    if nxt:
                        out.add(nxt[0])
            # 跳到下个月
            cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
        return sorted(out)

    return []


def list_plans(conn):
    """列出全部计划，附带已生成期数与下次扣款日。"""
    out = []
    for r in conn.execute(
        "SELECT p.*, f.name AS fund_name, f.is_money FROM sip_plans p "
        "LEFT JOIN funds f ON f.code = p.code ORDER BY p.enabled DESC, p.id"
    ).fetchall():
        p = dict(r)
        p["freq_name"] = FREQ_NAMES.get(p["freq"], p["freq"])
        if p["freq"] == "weekly" and p["weekday"] is not None:
            p["freq_name"] += WEEKDAY_NAMES[p["weekday"]]
        elif p["freq"] == "monthly":
            p["freq_name"] += f" {p['monthday'] or 1} 号"
        # 只统计本计划生成的流水。早期导入的定投记录 plan_id 为空，
        # 无法归属，因此不计入任何一个计划。
        p["generated"] = conn.execute(
            "SELECT COUNT(*) AS c FROM txns WHERE plan_id = ?", (p["id"],)
        ).fetchone()["c"]
        p["next_date"] = _next_date(conn, p)
        out.append(p)
    return out


def _future_due_dates(plan, since, horizon):
    """推算未来的扣款日（无法使用真实交易日历，净值数据不存在未来部分）。

    只用「周一至周五视为交易日」的近似规则，法定假日无法预知。
    仅用于展示「下次扣款日」，不用于生成记录——生成记录一律以真实净值日为准。
    """
    start = max(since, plan["start_date"])
    end = min(horizon, plan["end_date"]) if plan["end_date"] else horizon
    if start > end:
        return []

    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    out = []

    if plan["freq"] == "daily":
        cur = d0
        while cur <= d1:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out

    if plan["freq"] == "weekly":
        wd = plan["weekday"] if plan["weekday"] is not None else 0
        cur = d0
        while cur <= d1:
            if cur.weekday() == wd:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out

    if plan["freq"] == "monthly":
        import calendar as _cal

        md = plan["monthday"] or 1
        cur = d0
        seen = set()
        while cur <= d1:
            key = (cur.year, cur.month)
            if key not in seen:
                seen.add(key)
                last = _cal.monthrange(cur.year, cur.month)[1]
                ds = date(cur.year, cur.month, min(md, last)).isoformat()
                if ds >= start:
                    out.append(ds)
            cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
        return out

    return []


def _next_date(conn, plan):
    """下次扣款日。优先用真实交易日历，拿不到（未来）就按星期推算。"""
    if not plan["enabled"]:
        return None
    today = date.today().isoformat()
    since = plan["last_run"] or plan["start_date"]
    anchor = max(since, today)
    horizon = (date.fromisoformat(anchor) + timedelta(days=60)).isoformat()

    real = _due_dates(conn, plan, anchor, horizon)
    if real:
        return real[0]

    approx = _future_due_dates(plan, anchor, horizon)
    return approx[0] if approx else None


def save_plan(conn, plan_id, payload):
    """新建或更新计划。返回计划 id。"""
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("请选择基金")
    if not conn.execute("SELECT 1 FROM funds WHERE code = ?", (code,)).fetchone():
        raise ValueError(f"基金 {code} 不在基金库中")

    try:
        amount = float(payload.get("amount") or 0)
    except (TypeError, ValueError):
        raise ValueError("每期金额应为数字")
    if amount <= 0:
        raise ValueError("每期金额需大于 0")

    try:
        fee_rate = float(payload.get("fee_rate") or 0)
    except (TypeError, ValueError):
        raise ValueError("费率应为数字")
    if fee_rate < 0 or fee_rate > 10:
        raise ValueError("费率应在 0–10% 之间")

    freq = str(payload.get("freq") or "monthly")
    if freq not in FREQ_NAMES:
        raise ValueError(f"未知周期：{freq}")

    weekday = payload.get("weekday")
    monthday = payload.get("monthday")
    if freq == "weekly":
        try:
            weekday = int(weekday if weekday is not None else 0)
        except (TypeError, ValueError):
            raise ValueError("星期取值不合法")
        if weekday < 0 or weekday > 6:
            raise ValueError("星期应在 0–6 之间")
    else:
        weekday = None

    if freq == "monthly":
        try:
            monthday = int(monthday if monthday is not None else 1)
        except (TypeError, ValueError):
            raise ValueError("日期取值不合法")
        if monthday < 1 or monthday > 31:
            raise ValueError("每月扣款日应在 1–31 之间")
    else:
        monthday = None

    start = str(payload.get("start_date") or "").strip()
    if not start:
        raise ValueError("请填写起始日期")
    date.fromisoformat(start)
    end = str(payload.get("end_date") or "").strip() or None
    if end:
        date.fromisoformat(end)
        if end < start:
            raise ValueError("结束日期不能早于起始日期")

    enabled = 1 if payload.get("enabled", True) else 0
    note = (payload.get("note") or "").strip()[:80]
    now = date.today().isoformat()

    if plan_id:
        if not conn.execute("SELECT 1 FROM sip_plans WHERE id = ?", (plan_id,)).fetchone():
            raise ValueError("计划不存在")
        conn.execute(
            "UPDATE sip_plans SET code=?, amount=?, fee_rate=?, freq=?, weekday=?, "
            "monthday=?, start_date=?, end_date=?, enabled=?, note=? WHERE id=?",
            (code, amount, fee_rate, freq, weekday, monthday, start, end, enabled, note, plan_id),
        )
        pid = plan_id
    else:
        cur = conn.execute(
            "INSERT INTO sip_plans (code, amount, fee_rate, freq, weekday, monthday, "
            "start_date, end_date, enabled, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, amount, fee_rate, freq, weekday, monthday, start, end, enabled, note, now),
        )
        pid = cur.lastrowid
    conn.commit()
    return pid


def delete_plan(conn, plan_id):
    conn.execute("DELETE FROM sip_plans WHERE id = ?", (plan_id,))
    conn.commit()


def run_plan(conn, plan_id, until=None):
    """把一个计划补生成到指定日期（默认今天）。返回新生成的记录。

    幂等：同一天同一基金的 sip 记录已存在就跳过。
    """
    row = conn.execute("SELECT * FROM sip_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return {"plan_id": plan_id, "created": [], "skipped": [], "count": 0,
                "error": "计划不存在"}
    plan = dict(row)
    if not plan["enabled"]:
        return {"plan_id": plan_id, "created": [], "skipped": [], "count": 0,
                "error": "计划已停用"}

    until = until or date.today().isoformat()
    since = plan["last_run"] or plan["start_date"]
    if since > until:
        return {"plan_id": plan_id, "created": [], "skipped": [], "count": 0,
                "note": "已是最新"}

    dates = _due_dates(conn, plan, since, until)[:MAX_CATCHUP]
    fund = conn.execute("SELECT name, rate, is_money FROM funds WHERE code = ?", (plan["code"],)).fetchone()
    if not fund:
        return {"plan_id": plan_id, "created": [], "skipped": [], "count": 0,
                "error": "基金已不在基金库"}

    rate = plan["fee_rate"] / 100.0
    created, skipped, pending = [], [], []
    for ds in dates:
        dup = conn.execute(
            "SELECT id FROM txns WHERE code = ? AND date = ? AND action = 'sip'",
            (plan["code"], ds),
        ).fetchone()
        if dup:
            skipped.append(ds)
            continue
        # 必须当天有净值才能生成。不能退而求其次取前一天的净值——
        # 那会造出「日期是今天、成交净值是昨天」的记录，是最难发现的一类错账。
        # 净值通常在收盘后当晚公布，所以当天多半还没数据，就留到下次再补。
        nav_row = conn.execute(
            "SELECT nav FROM nav WHERE code = ? AND date = ?",
            (plan["code"], ds),
        ).fetchone()
        if not nav_row or nav_row["nav"] is None:
            pending.append(ds)
            continue
        nav = float(nav_row["nav"])
        amount = plan["amount"]
        net = amount / (1 + rate) if rate > 0 else amount
        fee = round(amount - net, 2)
        shares = round(net / nav, 4)
        cur = conn.execute(
            "INSERT INTO txns (code, date, action, amount, shares, nav, fee, plan_id, note, created_at) "
            "VALUES (?,?,'sip',?,?,?,?,?,?,datetime('now'))",
            (plan["code"], ds, round(amount, 2), shares, round(nav, 4), fee,
             plan_id, (plan["note"] or "定投计划")[:60]),
        )
        created.append({"id": cur.lastrowid, "date": ds, "shares": shares, "nav": round(nav, 4),
                        "amount": round(amount, 2), "fee": fee})

    # last_run 只推进到「最后一个已处理完的日期」，且不能越过还缺净值的日期。
    # 否则今天净值没公布就推进到今天，明天再也不会补这一期。
    if pending:
        advance_to = (date.fromisoformat(min(pending)) - timedelta(days=1)).isoformat()
        advance_to = max(advance_to, since)
    else:
        advance_to = until
    conn.execute("UPDATE sip_plans SET last_run = ? WHERE id = ?", (advance_to, plan_id))
    conn.commit()
    return {
        "plan_id": plan_id,
        "fund": fund["name"],
        "created": created,
        "skipped": skipped,
        "pending": pending,
        "count": len(created),
        "last_run": advance_to,
    }


def run_all(conn, until=None):
    """跑所有启用的计划，返回汇总。"""
    results = []
    for row in conn.execute("SELECT id FROM sip_plans WHERE enabled = 1 ORDER BY id").fetchall():
        results.append(run_plan(conn, row["id"], until))
    total = sum(r.get("count", 0) for r in results)
    return {"results": results, "total_created": total}
