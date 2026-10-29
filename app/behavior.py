"""行为诊断：追涨杀跌、模式分类、持有与定投纪律。

设计原则
--------
**不输出无法解释的单一分数。** 一个「行为分 62 分」除了让人焦虑之外没有
任何指导意义——说不出 62 分比 75 分差在哪，就不知道该改什么。

所以这里输出的是**模式分类**：同样是「高位买入占比 73%」，可能是
- 高位建仓后向下补仓（有纪律，但起点判断错）
- 上涨过程中追着加（真正的追涨）
- 单一高位一笔（样本不足，不能下结论）

三种模式的改进方向完全不同。只报一个百分比会误导。
"""

from collections import defaultdict
from datetime import date, timedelta

from . import db

# 分位阈值。70/30 是常用的「高位/低位」分界
HIGH = 70.0
LOW = 30.0

# 判定趋势所需的最小笔数。少于此数不下「补仓/追涨」结论
MIN_TREND_POINTS = 3

# 模式分类
PATTERN_LABELS = {
    "high_open_pyramid": "高位建仓后向下补仓",
    "chase_up": "上涨中追高加仓",
    "low_accumulate": "低位持续买入",
    "single_high": "单笔高位买入",
    "single_low": "单笔低位买入",
    "single_mid": "单笔中位买入",
    "two_down": "两笔买入，位置下移",
    "two_up": "两笔买入，位置上移",
    "mixed": "位置分散，无明显模式",
    "insufficient": "样本不足",
}


def _fund_buys(conn, code):
    """该基金的主动买入明细（排除定投，定投是机械执行不代表判断）。

    返回 [{date, action, amount, percentile}]
    """
    rows = conn.execute(
        "SELECT id, date, action, amount FROM txns "
        "WHERE code = ? AND action IN ('open','add') ORDER BY date",
        (code,),
    ).fetchall()
    if not rows:
        return []

    navs = conn.execute(
        "SELECT date, nav FROM nav WHERE code = ? AND nav IS NOT NULL ORDER BY date",
        (code,),
    ).fetchall()
    if not navs:
        return []
    closes = {r["date"]: float(r["nav"]) for r in navs}
    lo = min(closes.values())
    hi = max(closes.values())
    if hi <= lo:
        return []

    def pct_of(ds):
        if ds in closes:
            return (closes[ds] - lo) / (hi - lo) * 100
        prior = [d for d in closes if d <= ds]
        if not prior:
            return None
        return (closes[max(prior)] - lo) / (hi - lo) * 100

    out = []
    for r in rows:
        p = pct_of(r["date"])
        if p is None:
            continue
        out.append(
            {
                "id": r["id"],
                "date": r["date"],
                "action": r["action"],
                "amount": round(float(r["amount"] or 0), 2),
                "percentile": round(p, 1),
            }
        )
    return out


def _classify(buys):
    """给一只基金的买入序列打模式标签。

    判定顺序很重要：
    1. 笔数不足 → insufficient
    2. 单笔 → single_high / single_low / single_mid
    3. 分位整体递减（末笔显著低于首笔）→ 向下补仓，若无高位开仓则为低位持续
    4. 分位整体递增或末笔很高 → 追高
    5. 其余 → mixed
    """
    n = len(buys)
    if n == 0:
        return "insufficient", None
    if n < MIN_TREND_POINTS:
        ps = [b["percentile"] for b in buys]
        avg = sum(ps) / len(ps)
        # 两笔时方向仍然可读：明确走低说明在补仓，明确走高说明在追加
        if n == 2:
            if ps[1] < ps[0] - 5:
                return "two_down", avg
            if ps[1] > ps[0] + 5:
                return "two_up", avg
        p = buys[-1]["percentile"]
        if p >= HIGH:
            return "single_high", buys[-1]["percentile"]
        if p <= LOW:
            return "single_low", buys[-1]["percentile"]
        return "single_mid", buys[-1]["percentile"]

    ps = [b["percentile"] for b in buys]
    first, last = ps[0], ps[-1]
    avg = sum(ps) / len(ps)

    # 高位开仓 + 后续分位走低 → 向下补仓（有纪律但起点错）
    if first >= HIGH and last < first - 10:
        return "high_open_pyramid", avg

    # 末笔仍高于起点且整体偏高 → 追高
    if last >= first - 5 and avg >= HIGH:
        return "chase_up", avg

    # 整体在低位 → 低位持续买入
    if avg <= LOW + 10:
        return "low_accumulate", avg

    # 整体高位但无明确趋势
    if avg >= HIGH:
        return "chase_up", avg

    return "mixed", avg


def chase_index(conn, date_to=None):
    """追涨杀跌诊断。

    核心区分：**主动买入** 与 **定投** 必须分开统计。
    定投是机械执行的，把它混进来会稀释主动决策的真实倾向——
    实测数据里合并统计是 65% 高位，只看主动买入是 73%，
    而定投本身只有 26% 高位。混在一起会让结论变模糊。
    """
    funds = conn.execute("SELECT code, name, category, is_money FROM funds ORDER BY code").fetchall()

    act_all = []
    sip_all = []
    per_fund = []

    for f in funds:
        if f["is_money"]:
            continue  # 货币基金净值恒定，分位无意义
        buys = _fund_buys(conn, f["code"])
        if buys:
            label, avg = _classify(buys)
            per_fund.append(
                {
                    "code": f["code"],
                    "name": f["name"],
                    "category": f["category"],
                    "count": len(buys),
                    "amount": round(sum(b["amount"] for b in buys), 2),
                    "avg_percentile": round(avg, 1) if avg is not None else None,
                    "pattern": label,
                    "pattern_name": PATTERN_LABELS.get(label, label),
                    "first_percentile": buys[0]["percentile"],
                    "last_percentile": buys[-1]["percentile"],
                    "points": [{"date": b["date"], "amount": b["amount"],
                                "percentile": b["percentile"], "action": b["action"]}
                               for b in buys],
                }
            )
            act_all.extend(buys)

        # 定投单独统计
        sip_rows = conn.execute(
            "SELECT id, date, amount FROM txns WHERE code = ? AND action = 'sip' ORDER BY date",
            (f["code"],),
        ).fetchall()
        if sip_rows:
            navs = conn.execute(
                "SELECT date, nav FROM nav WHERE code = ? AND nav IS NOT NULL ORDER BY date",
                (f["code"],),
            ).fetchall()
            closes = {r["date"]: float(r["nav"]) for r in navs}
            if closes:
                lo, hi = min(closes.values()), max(closes.values())
                if hi > lo:
                    for r in sip_rows:
                        ds = r["date"]
                        if ds in closes:
                            p = (closes[ds] - lo) / (hi - lo) * 100
                        else:
                            prior = [d for d in closes if d <= ds]
                            if not prior:
                                continue
                            p = (closes[max(prior)] - lo) / (hi - lo) * 100
                        sip_all.append(
                            {"code": f["code"], "date": ds,
                             "amount": round(float(r["amount"] or 0), 2),
                             "percentile": round(p, 1)}
                        )

    def agg(rows):
        if not rows:
            return {"count": 0, "amount": 0.0, "high_pct": None, "low_pct": None,
                    "weighted_percentile": None, "high_amount": 0.0, "low_amount": 0.0}
        tot = sum(r["amount"] for r in rows)
        hi_rows = [r for r in rows if r["percentile"] >= HIGH]
        lo_rows = [r for r in rows if r["percentile"] <= LOW]
        hi_amt = sum(r["amount"] for r in hi_rows)
        lo_amt = sum(r["amount"] for r in lo_rows)
        w = sum(r["percentile"] * r["amount"] for r in rows) / tot if tot > 0 else None
        return {
            "count": len(rows),
            "amount": round(tot, 2),
            "high_amount": round(hi_amt, 2),
            "low_amount": round(lo_amt, 2),
            # 金额加权比笔数加权更真实：同样 10 元，买 100 次和买 1 次不一样
            "high_pct": round(hi_amt / tot * 100, 1) if tot > 0 else None,
            "low_pct": round(lo_amt / tot * 100, 1) if tot > 0 else None,
            "weighted_percentile": round(w, 1) if w is not None else None,
        }

    active = agg(act_all)
    sip = agg(sip_all)

    # 主导模式：只从「有方向性」的模式里选。
    # single_high 这类是单笔事实，不构成行为模式，参与评选会让结论变成
    # 「你的主导模式是单笔高位买入」——等于什么都没说。
    directional = {"high_open_pyramid", "chase_up", "low_accumulate", "two_down", "two_up", "mixed"}
    by_pattern = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for p in per_fund:
        by_pattern[p["pattern"]]["count"] += p["count"]
        by_pattern[p["pattern"]]["amount"] += p["amount"]
    dominant = None
    pool = {k: v for k, v in by_pattern.items() if k in directional}
    if pool:
        k = max(pool, key=lambda x: pool[x]["amount"])
        tot_amt = sum(v["amount"] for v in pool.values()) or 1
        dominant = {
            "pattern": k,
            "pattern_name": PATTERN_LABELS.get(k, k),
            "amount": round(pool[k]["amount"], 2),
            "share": round(pool[k]["amount"] / tot_amt * 100, 1),
        }

    # 结论：必须基于金额加权分位，且样本够
    verdict = None
    advice = None
    if active["count"] == 0:
        verdict = "还没有主动买入记录"
    elif active["count"] < MIN_TREND_POINTS:
        verdict = f"主动买入仅 {active['count']} 笔，样本不足以判断倾向"
    elif active["weighted_percentile"] is None:
        verdict = "分位数据不足"
    else:
        wp = active["weighted_percentile"]
        if wp >= HIGH:
            verdict = f"主动买入集中在净值高位，金额加权平均分位 {wp}%"
            advice = "你买得不便宜。下一步该看的是「首次建仓时机」而不是「加不加仓」"
        elif wp <= LOW:
            verdict = f"主动买入集中在净值低位，金额加权平均分位 {wp}%"
            advice = "买入位置偏低位，这部分行为是正向的"
        else:
            verdict = f"主动买入位置分散，金额加权平均分位 {wp}%"
            advice = "没有明显的追高或抄底倾向"

    return {
        "active": active,
        "sip": sip,
        "per_fund": sorted(per_fund, key=lambda x: -x["amount"]),
        "dominant": dominant,
        "verdict": verdict,
        "advice": advice,
        "thresholds": {"high": HIGH, "low": LOW},
        "gap": {
            "high_pct": round((active["high_pct"] or 0) - (sip["high_pct"] or 0), 1)
            if active["high_pct"] is not None and sip["high_pct"] is not None
            else None,
            "weighted": round((active["weighted_percentile"] or 0) - (sip["weighted_percentile"] or 0), 1)
            if active["weighted_percentile"] is not None and sip["weighted_percentile"] is not None
            else None,
        },
    }


# ---------------------------------------------------------------- 定投纪律性

# 达标率的判定阈值。差 1 期以内不算失约（转账延迟、节假日手工补单都很常见）
DISCIPLINE_TOLERANCE = 1


def discipline(conn, until=None):
    """定投纪律性：每期该扣多少、实际扣了多少、缺了哪几期。

    只统计已结束与进行中的计划，且只看到「该计划自己声明的周期」，
    不掺入定投以外的操作。
    """
    from . import sip

    until = until or date.today().isoformat()
    plans = sip.list_plans(conn)

    rows = []
    for p in plans:
        expected = sip._due_dates(
            conn,
            p,
            p["start_date"],
            min(until, p["end_date"]) if p["end_date"] else until,
        )
        actual = [
            r["date"]
            for r in conn.execute(
                "SELECT date FROM txns WHERE plan_id = ? ORDER BY date", (p["id"],)
            ).fetchall()
        ]
        have = set(actual)
        missing = [d for d in expected if d not in have]
        extra = [d for d in actual if d not in set(expected)]
        rate = (
            round((len(expected) - len(missing)) / len(expected) * 100, 1)
            if expected
            else None
        )
        rows.append(
            {
                "id": p["id"],
                "code": p["code"],
                "name": p["fund_name"],
                "freq_name": p["freq_name"],
                "amount": p["amount"],
                "start_date": p["start_date"],
                "end_date": p["end_date"],
                "enabled": bool(p["enabled"]),
                "expected": len(expected),
                "actual": len(actual),
                "missing": missing,
                "extra": extra,
                "rate": rate,
                "expected_amount": round(len(expected) * p["amount"], 2),
                "actual_amount": round(
                    sum(
                        float(r["amount"] or 0)
                        for r in conn.execute(
                            "SELECT amount FROM txns WHERE plan_id = ?", (p["id"],)
                        ).fetchall()
                    ),
                    2,
                ),
            }
        )

    # 汇总：只算启用中的计划，停用的历史片段不参与「当前是否守约」
    live = [r for r in rows if r["enabled"]]
    exp = sum(r["expected"] for r in live)
    miss = sum(len(r["missing"]) for r in live)
    overall = round((exp - miss) / exp * 100, 1) if exp else None

    verdict = None
    if not live:
        verdict = "没有进行中的定投计划"
    elif exp == 0:
        verdict = "进行中的计划还没有到期扣款"
    elif overall is not None:
        if overall >= 100:
            verdict = f"全部 {exp} 期准时扣款，纪律性满分"
        elif overall >= 100 - (DISCIPLINE_TOLERANCE / max(exp, 1) * 100):
            verdict = f"{exp} 期中缺 {miss} 期，基本守约"
        else:
            verdict = f"{exp} 期中缺 {miss} 期，达标率 {overall}%"

    return {
        "rows": rows,
        "live_expected": exp,
        "live_missing": miss,
        "overall_rate": overall,
        "verdict": verdict,
        "until": until,
    }


# ---------------------------------------------------------------- 持有行为

# 卖出笔数低于此值时不给「平均持有天数」结论——2 笔算出来的均值没有意义
MIN_SELL_SAMPLES = 10

# 短期持有阈值：少于这个天数就卖出，视为偏短线
SHORT_HOLD_DAYS = 30


def holding(conn, until=None):
    """持有行为：平均持有天数、短线比例、卖出时的分位。

    样本不足时明确说「不足」而不是给一个数字。用 2 笔卖出算平均持有天数，
    标准误会大于均值本身，结论不可用。
    """
    sells = conn.execute(
        "SELECT t.id, t.code, t.date, t.action, t.amount, t.shares, t.reason, f.name "
        "FROM txns t JOIN funds f ON f.code = t.code "
        "WHERE t.action IN ('reduce','close') ORDER BY t.date"
    ).fetchall()

    detail = []
    for s in sells:
        first = conn.execute(
            "SELECT MIN(date) d FROM txns WHERE code = ? AND action IN ('open','add','sip')",
            (s["code"],),
        ).fetchone()["d"]
        if not first:
            continue
        days = (date.fromisoformat(s["date"]) - date.fromisoformat(first)).days

        # 卖出时该基金的分位
        navs = conn.execute(
            "SELECT date, nav FROM nav WHERE code = ? AND nav IS NOT NULL ORDER BY date",
            (s["code"],),
        ).fetchall()
        pct = None
        if navs:
            closes = {r["date"]: float(r["nav"]) for r in navs}
            lo, hi = min(closes.values()), max(closes.values())
            if hi > lo:
                if s["date"] in closes:
                    pct = round((closes[s["date"]] - lo) / (hi - lo) * 100, 1)
                else:
                    prior = [d for d in closes if d <= s["date"]]
                    if prior:
                        pct = round((closes[max(prior)] - lo) / (hi - lo) * 100, 1)

        detail.append(
            {
                "id": s["id"],
                "code": s["code"],
                "name": s["name"],
                "date": s["date"],
                "action": s["action"],
                "action_name": db.ACTIONS.get(s["action"], s["action"]),
                "amount": round(float(s["amount"] or 0), 2),
                "days": days,
                "percentile": pct,
                "reason": s["reason"] or "",
                "short": days < SHORT_HOLD_DAYS,
            }
        )

    n = len(detail)
    if n == 0:
        return {
            "samples": 0, "avg_days": None, "short_ratio": None,
            "sells": [], "sufficient": False,
            "verdict": "还没有卖出记录",
        }

    avg = round(sum(d["days"] for d in detail) / n, 1)
    short_n = len([d for d in detail if d["short"]])
    sufficient = n >= MIN_SELL_SAMPLES

    if not sufficient:
        verdict = (
            f"仅 {n} 笔卖出记录，样本不足以评估持有习惯"
            f"（至少需要 {MIN_SELL_SAMPLES} 笔）。下面的明细可看，但不建议据此下结论。"
        )
    elif avg < SHORT_HOLD_DAYS:
        verdict = f"平均持有 {avg} 天，偏短线操作，{short_n}/{n} 笔在 30 天内卖出"
    elif short_n / n > 0.5:
        verdict = f"平均持有 {avg} 天，但 {short_n}/{n} 笔属短线卖出，行为不稳定"
    else:
        verdict = f"平均持有 {avg} 天，整体偏中长期"

    return {
        "samples": n,
        "avg_days": avg,
        "short_ratio": round(short_n / n * 100, 1),
        "short_count": short_n,
        "min_samples": MIN_SELL_SAMPLES,
        "sufficient": sufficient,
        "sells": detail,
        "verdict": verdict,
    }


# ---------------------------------------------------------------- 月度事实卡

def monthly(conn, month=None):
    """月度事实卡：当月发生了什么，只陈述事实不下人格结论。

    刻意不做「投资人格标签」（稳健型/激进型）。理由：7 个月的数据不足以
    支撑人格判断，而一旦被贴上标签，人容易自我固化——反而阻碍改进。
    这里只给可核对的事实。
    """
    from . import engine

    if not month:
        row = conn.execute("SELECT MAX(date) d FROM txns").fetchone()
        month = (row["d"] or date.today().isoformat())[:7]
    # 只查长度不够：'2026/10' 也是 7 个字符，会在后面 int() 处才炸。
    # 用正则先卡格式，错误信息才准确。
    import re as _re
    if not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("月份格式应为 YYYY-MM，例如 2026-10")

    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1).isoformat()
    last = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()

    # 当月流水
    rows = conn.execute(
        "SELECT t.*, f.name AS fund_name FROM txns t JOIN funds f ON f.code = t.code "
        "WHERE t.date BETWEEN ? AND ? ORDER BY t.date, t.id",
        (first, last),
    ).fetchall()

    buy_amt = sum(float(r["amount"] or 0) for r in rows if r["action"] in db.BUY_ACTIONS)
    sell_amt = sum(float(r["amount"] or 0) for r in rows if r["action"] in db.SELL_ACTIONS)
    div_amt = sum(
        float(r["amount"] or 0) for r in rows
        if r["action"] == "dividend" and float(r["shares"] or 0) == 0
    )
    fee_amt = sum(float(r["fee"] or 0) for r in rows)

    # 月末市值：用该月最后一个净值日
    positions = engine.build_positions(conn, date_to=last)
    value = sum(p["value"] for p in positions if not p["closed"])
    cost = sum(p["cost"] for p in positions if not p["closed"])
    month_pnl = value - cost

    # 上月末市值，用于算当月变化
    prev_last = (date(y, m, 1) - timedelta(days=1)).isoformat()
    prev_positions = engine.build_positions(conn, date_to=prev_last)
    prev_value = sum(p["value"] for p in prev_positions if not p["closed"])
    change = value - prev_value if prev_value else None

    # 操作明细
    ops = []
    for r in rows:
        ops.append(
            {
                "date": r["date"],
                "name": r["fund_name"],
                "code": r["code"],
                "action": r["action"],
                "action_name": db.ACTIONS.get(r["action"], r["action"]),
                "amount": round(float(r["amount"] or 0), 2),
                "reason": r["reason"] or "",
                "auto": bool(r["plan_id"]),
            }
        )

    manual = [o for o in ops if not o["auto"] and o["action"] in ("open", "add", "reduce", "close")]
    filled = len([o for o in manual if o["reason"]])

    # 理由填写率：这是「投资日记」能否长期成立的关键指标
    reason_rate = round(filled / len(manual) * 100, 1) if manual else None

    # 当月印象最深的一笔：金额最大的主动操作
    top = max(manual, key=lambda o: o["amount"]) if manual else None

    return {
        "month": month,
        "range": {"from": first, "to": last},
        "buy_amount": round(buy_amt, 2),
        "sell_amount": round(sell_amt, 2),
        "dividend_amount": round(div_amt, 2),
        "fee": round(fee_amt, 2),
        "value": round(value, 2),
        "cost": round(cost, 2),
        "unrealized": round(month_pnl, 2),
        "value_change": round(change, 2) if change is not None else None,
        "txn_count": len(rows),
        "manual_count": len(manual),
        "auto_count": len(ops) - len(manual),
        "ops": ops,
        "top_op": top,
        "reason_rate": reason_rate,
        "reason_filled": filled,
        "has_data": len(rows) > 0 or value > 0,
    }


def monthly_list(conn, limit=12):
    """有哪些月份有数据，供选择器使用。"""
    rows = conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txns ORDER BY m DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["m"] for r in rows]
