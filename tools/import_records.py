"""把手写理财记录批量导入基金台账。

导入规则写在 tools/plan.py 里（个人记录，不入库）。首次使用先复制示例：
    cp tools/plan.example.py tools/plan.py

用法：
    python tools/import_records.py               # 导入到 data/fund.db
    python tools/import_records.py --dry-run     # 只预演，不写库
    python tools/import_records.py --reset       # 先清空这些基金的旧流水再导入
    python tools/import_records.py --data demo   # 导入到演示库
    python tools/import_records.py --plan 路径.py  # 指定其它规则文件
"""

import argparse
import os
import sys
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import db, quotes  # noqa: E402

# ---------------------------------------------------------------- 载入规则


def load_plan(path=None):
    """从 plan.py 载入 FUNDS 与 PLAN。

    默认找 tools/plan.py，不存在时退回 tools/plan.example.py 并给出提示。
    """
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    if path:
        target = os.path.abspath(path)
        if not os.path.isfile(target):
            raise SystemExit(f"找不到规则文件：{target}")
    else:
        real = os.path.join(here, "plan.py")
        example = os.path.join(here, "plan.example.py")
        target = real if os.path.isfile(real) else example
        if target == example:
            print("提示：未找到 tools/plan.py，本次使用示例规则 plan.example.py")
            print("      想导入自己的记录，先执行：cp tools/plan.example.py tools/plan.py")
            print()

    spec = importlib.util.spec_from_file_location("fund_plan", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    funds = getattr(mod, "FUNDS", None)
    plan = getattr(mod, "PLAN", None)
    if not isinstance(funds, dict) or not funds:
        raise SystemExit(f"{target} 里没有有效的 FUNDS")
    if not isinstance(plan, list) or not plan:
        raise SystemExit(f"{target} 里没有有效的 PLAN")
    return funds, plan, target


WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ---------------------------------------------------------------- 工具


def a_share_calendar(conn, start, end):
    """A 股交易日历。

    取一只非货币基金（货币基金每个自然日都有净值，不能当日历）的净值日。
    优先用股票型，没有就退到任意非货币基金。
    """
    row = conn.execute(
        "SELECT code FROM funds WHERE is_money = 0 "
        "AND (category = 'stock' OR category = 'overseas') "
        "ORDER BY (SELECT COUNT(*) FROM nav WHERE nav.code = funds.code) DESC LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT code FROM funds WHERE is_money = 0 LIMIT 1").fetchone()
    if not row:
        return []
    return [
        r["date"]
        for r in conn.execute(
            "SELECT date FROM nav WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            (row["code"], start, end),
        ).fetchall()
    ]


def nav_on_or_after(conn, code, ds):
    """取 >= ds 的第一个净值日与净值，用于把非交易日顺延到下一交易日。"""
    r = conn.execute(
        "SELECT date, nav FROM nav WHERE code = ? AND date >= ? AND nav IS NOT NULL "
        "ORDER BY date LIMIT 1",
        (code, ds),
    ).fetchone()
    return (r["date"], float(r["nav"])) if r else (None, None)


def held_shares(conn, code, ds, planned=None):
    """算出某日收盘后的持仓份额（用于清仓）。

    planned 是本次待插入的记录——预演时它们还没进库，必须一起算进来，
    否则同一批里的「先买入、后清仓」会算成 0 持仓。
    """
    rows = [
        dict(x) for x in conn.execute(
            "SELECT * FROM txns WHERE code = ? AND date <= ? ORDER BY date ASC, id ASC",
            (code, ds),
        ).fetchall()
    ]
    if planned:
        rows += [dict(x) for x in planned if x["code"] == code and x["date"] <= ds]
        rows.sort(key=lambda x: x["date"])

    total = 0.0
    for t in rows:
        if t["action"] in db.BUY_ACTIONS:
            total += float(t["shares"] or 0)
        elif t["action"] in db.SELL_ACTIONS:
            total -= float(t["shares"] or 0)
        elif t["action"] == "dividend" and (t["shares"] or 0) > 0:
            total += float(t["shares"] or 0)
    return max(total, 0.0)


def expand_sip(conn, item):
    """把一条定投规则展开成逐笔日期。只取该基金真有净值的交易日。"""
    code = item["code"]
    cal = a_share_calendar(conn, item["start"], item["end"])
    own = {
        r["date"]
        for r in conn.execute(
            "SELECT date FROM nav WHERE code = ? AND date >= ? AND date <= ?",
            (code, item["start"], item["end"]),
        ).fetchall()
    }
    days = [d for d in cal if d in own]

    freq = item["freq"]
    if freq == "daily":
        return days
    wd = WEEKDAY.get(freq)
    if wd is None:
        return []
    return [d for d in days if date.fromisoformat(d).weekday() == wd]


# ---------------------------------------------------------------- 主流程


def ensure_funds(conn):
    """确保基金与其净值都在库里。

    净值属于参考数据，即使是 dry-run 也照常抓取——交易日历要靠它算，
    否则没法预演出正确的记录。
    """
    added = []
    for code, (cat, _desc) in FUNDS.items():
        row = conn.execute("SELECT code FROM funds WHERE code = ?", (code,)).fetchone()
        if row:
            conn.execute("UPDATE funds SET category = ? WHERE code = ?", (cat, code))
            conn.commit()
            continue
        prof = quotes.fetch_profile(code)
        conn.execute(
            "INSERT INTO funds (code, name, category, fund_type, source_rate, rate, is_money, created_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (code, prof["name"], cat, prof.get("fund_type") or "",
             prof.get("source_rate"), prof.get("rate"), prof["is_money"]),
        )
        n = quotes.save_nav(conn, code, prof["nav"], replace=True)
        added.append((code, f"{prof['name']}，{n} 条净值"))
        conn.commit()
    return added


def build_rows(conn):
    """展开成待插入的流水，按时间排序。返回 (rows, warnings)。"""
    rows, warnings = [], []

    # 先把非定投的记录按日期排好，定投单独展开
    events = []
    for item in PLAN:
        if item["kind"] == "sip":
            dates = expand_sip(conn, item)
            if not dates:
                warnings.append(f"{item['code']} 定投 {item['start']}→{item['end']} 没找到交易日")
            for d in dates:
                events.append({**item, "date": d, "kind": "sip"})
        else:
            events.append(dict(item))

    events.sort(key=lambda x: (x["date"], x["code"], x["kind"]))

    for ev in events:
        code = ev["code"]
        fund = conn.execute("SELECT * FROM funds WHERE code = ?", (code,)).fetchone()
        if not fund:
            warnings.append(f"{code} 不在基金库，跳过 {ev['date']}")
            continue

        want = ev["date"]
        eff, nav = nav_on_or_after(conn, code, want)
        if ev.get("nav_override"):
            eff, nav = want, float(ev["nav_override"])
        if not nav or not eff:
            warnings.append(f"{code} {want} 之后无净值，跳过")
            continue
        if eff != want:
            warnings.append(
                f"{code} {want} 非交易日（{date.fromisoformat(want).strftime('%a')}），"
                f"顺延到 {eff}"
            )

        rate = float(fund["rate"] or 0) / 100.0
        kind = ev["kind"]
        note = ev.get("note", "")
        if eff != want:
            note = f"{note}｜原记录 {want}，按下一交易日净值确认"

        if kind in ("buy", "sip"):
            amount = round(float(ev["amount"]), 2)
            net = amount / (1 + rate) if rate > 0 else amount
            fee = round(amount - net, 2)
            shares = round(net / nav, 4)
            action = "open" if kind == "buy" else "sip"
            rows.append({
                "code": code, "date": eff,
                "action": "sip" if kind == "sip" else _pick_open_or_add(rows, code),
                "amount": amount, "shares": shares, "nav": round(nav, 4),
                "fee": fee, "note": note,
            })
        elif kind == "sell":
            amount = round(float(ev["amount"]), 2)
            shares = round(amount / nav, 4)
            rows.append({
                "code": code, "date": eff, "action": "reduce",
                "amount": amount, "shares": shares, "nav": round(nav, 4),
                "fee": 0.0, "note": note or "卖出",
            })
        elif kind == "close":
            shares = round(held_shares(conn, code, eff, rows), 4)
            if shares <= 0:
                warnings.append(f"{code} {eff} 清仓但无持仓可清")
                continue
            rows.append({
                "code": code, "date": eff, "action": "close",
                "amount": round(shares * nav, 2), "shares": shares,
                "nav": round(nav, 4), "fee": 0.0, "note": note or "清仓",
            })

    return rows, warnings


def _pick_open_or_add(rows, code):
    """这只基金之前没有任何买入记录就用「建仓」，否则「加仓」。"""
    for r in rows:
        if r["code"] == code and r["action"] in db.BUY_ACTIONS:
            return "add"
    return "open"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(BASE, "data"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="先清空这几只基金的旧流水")
    ap.add_argument("--plan", default=None, help="规则文件路径，默认 tools/plan.py")
    args = ap.parse_args()

    global FUNDS, PLAN
    FUNDS, PLAN, plan_path = load_plan(args.plan)

    db_path = os.path.join(os.path.abspath(args.data), "fund.db")
    conn = db.init(db_path)

    print(f"规则文件：{plan_path}")
    print(f"数据库：{db_path}  ({len(FUNDS)} 只基金 / {len(PLAN)} 条规则)")
    print()

    added = ensure_funds(conn)
    if added:
        print("新增基金：")
        for c, n in added:
            print(f"  {c}  {n}")
        print()

    if args.reset and not args.dry_run:
        codes = list(FUNDS.keys())
        q = ",".join("?" * len(codes))
        n = conn.execute(f"DELETE FROM txns WHERE code IN ({q})", codes).rowcount
        conn.commit()
        print(f"已清空这 {len(codes)} 只基金的旧流水：{n} 条\n")

    rows, warnings = build_rows(conn)

    print(f"待写入 {len(rows)} 条流水：")
    per = {}
    for r in rows:
        per.setdefault(r["code"], []).append(r)
    for code, rs in per.items():
        names = {x["name"] for x in conn.execute("SELECT name FROM funds WHERE code=?", (code,))}
        total_buy = sum(x["amount"] for x in rs if x["action"] in db.BUY_ACTIONS)
        total_sell = sum(x["amount"] - x["fee"] for x in rs if x["action"] in db.SELL_ACTIONS)
        print(f"  {code} {list(names)[0] if names else '':26s} {len(rs):3d} 笔  "
              f"买 {total_buy:>9,.2f}  卖 {total_sell:>8,.2f}")
    print()

    if warnings:
        print("需要留意：")
        for w in warnings:
            print("  ·", w)
        print()

    if args.dry_run:
        print("（--dry-run：流水未写入，但基金与净值已下载落库——计算交易日历需要）")
        return

    for r in rows:
        conn.execute(
            "INSERT INTO txns (code, date, action, amount, shares, nav, fee, note, created_at) "
            "VALUES (:code,:date,:action,:amount,:shares,:nav,:fee,:note,datetime('now'))",
            r,
        )
    conn.commit()
    print(f"已写入 {len(rows)} 条。")
    conn.close()


if __name__ == "__main__":
    main()
