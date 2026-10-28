"""自检脚本：不依赖 pytest，直接 python tests/selftest.py 运行。

覆盖四类分析的口径正确性：
- XIRR 与解析解对齐
- 归因贡献可加总
- 基准模拟按份额守恒
- 对账容差与方向判定

全部用内存库造数据，不碰你的 data/fund.db。
"""

import os
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import analytics, db, engine, sip  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def f(d, a):
    return (date.fromisoformat(d), a)


def expected_annual(d1, d2, mult):
    days = (date.fromisoformat(d2) - date.fromisoformat(d1)).days
    return (mult ** (365.0 / days) - 1) * 100


# ---------------------------------------------------------------- XIRR


def test_xirr():
    print("\n== XIRR ==")
    cases = [
        ("一年翻倍", [f("2024-01-01", -1000), f("2025-01-01", 2000)],
         expected_annual("2024-01-01", "2025-01-01", 2)),
        ("一年腰斩", [f("2024-01-01", -1000), f("2025-01-01", 500)],
         expected_annual("2024-01-01", "2025-01-01", 0.5)),
        ("不赚不亏", [f("2024-01-01", -1000), f("2025-01-01", 1000)], 0.0),
        ("两年翻倍", [f("2023-01-01", -1000), f("2025-01-02", 2000)],
         expected_annual("2023-01-01", "2025-01-02", 2)),
        ("半年+10%", [f("2024-01-01", -1000), f("2024-07-01", 1100)],
         expected_annual("2024-01-01", "2024-07-01", 1.1)),
    ]
    for name, flows, exp in cases:
        r = analytics.solve_irr(flows)
        got = r * 100 if r is not None else None
        ok = got is not None and abs(got - exp) < 0.01
        check(name, ok, f"解得 {got:.4f}% / 精确 {exp:.4f}%" if got is not None else "无解")

    print("\n-- 边界 --")
    for name, flows in [
        ("空现金流", []),
        ("单笔", [f("2024-01-01", -1000)]),
        ("全流出", [f("2024-01-01", -1000), f("2024-06-01", -500)]),
        ("全流入", [f("2024-01-01", 1000), f("2024-06-01", 500)]),
        ("同日同额", [f("2024-01-01", 1000), f("2024-01-01", 1000)]),
    ]:
        try:
            r = analytics.solve_irr(flows)
            check(name + " 不抛异常", r is None, f"返回 {r}")
        except Exception as exc:  # noqa: BLE001
            check(name + " 不抛异常", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 造数据


def make_db(tmp_path):
    """造一个小组合：两只基金，建仓 + 加仓 + 部分卖出。"""
    p = os.path.join(tmp_path, "selftest.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(p + suf):
            os.remove(p + suf)
    conn = db.init(p)

    for code, name in (("T001", "测试基金A"), ("T002", "测试基金B")):
        conn.execute(
            "INSERT INTO funds (code,name,category,is_money,created_at) "
            "VALUES (?,?, 'stock', 0, datetime('now'))",
            (code, name),
        )
        # 造两年净值：A 先涨后跌，B 稳步上涨
        base = date(2024, 1, 1)
        nav = 1.0
        for i in range(1100):
            d = base + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            # 制造 2026 年国庆假期，验证定投会跳过
            if date(2026, 10, 1) <= d <= date(2026, 10, 7):
                continue
            if code == "T001":
                nav = 1.0 + 0.6 * (i / 730) - (0.4 if i > 500 else 0) * ((i - 500) / 230)
            else:
                nav = 1.0 + 0.3 * (i / 1100)
            conn.execute(
                "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES (?,?,?,?,?)",
                (code, d.strftime("%Y-%m-%d"), round(nav, 4), round(nav, 4), 0.0),
            )
    conn.commit()

    def txn(code, d, action, amount, shares, nav, fee=0.0):
        conn.execute(
            "INSERT INTO txns (code,date,action,amount,shares,nav,fee,note,created_at) "
            "VALUES (?,?,?,?,?,?,?,'','')",
            (code, d, action, amount, shares, nav, fee),
        )

    txn("T001", "2024-06-03", "open", 1000, 1000, 1.0)
    txn("T001", "2025-06-02", "add", 500, 400, 1.25)
    txn("T001", "2026-03-02", "reduce", 300, 300, 1.0)
    txn("T002", "2024-06-03", "open", 2000, 2000, 1.0)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 归因


def test_attribution(conn):
    print("\n== 收益归因 ==")
    a = analytics.attribution(conn)
    check("有归因结果", len(a["rows"]) == 2, f"{len(a['rows'])} 只")

    total_pp = sum(r["contribution_pp"] or 0 for r in a["rows"])
    ret = a["summary"]["return_pct"]
    check("贡献可加总", abs(total_pp - ret) < 0.02,
          f"合计 {total_pp:.3f}pp vs 组合 {ret:.2f}%")

    loss_share = sum(r["share_of_loss"] or 0 for r in a["rows"])
    has_loss = any(r["share_of_loss"] is not None for r in a["rows"])
    if has_loss:
        check("亏损占比合计为 100%", abs(loss_share - 100) < 0.1, f"{loss_share:.2f}%")
    else:
        check("无亏损时占比为空", True, "跳过")

    print("\n-- 按类别 --")
    cat_pnl = sum(c["pnl"] for c in a["by_category"])
    check("类别盈亏合计等于总盈亏", abs(cat_pnl - a["total_pnl"]) < 0.05,
          f"{cat_pnl:.2f} vs {a['total_pnl']:.2f}")


# ---------------------------------------------------------------- 基准


def test_benchmark(conn):
    print("\n== 基准对比 ==")
    conn.execute(
        "INSERT INTO benchmarks (secid,name,updated_at) VALUES ('1.000300','沪深300',datetime('now'))"
    )
    base = date(2024, 1, 1)
    for i in range(1100):
        d = base + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        conn.execute(
            "INSERT INTO benchmark_nav (secid,date,close) VALUES ('1.000300',?,?)",
            (d.strftime("%Y-%m-%d"), round(4000.0 * (1 + 0.2 * i / 1100), 2)),
        )
    conn.commit()

    r = analytics.benchmark_compare(conn, "1.000300")
    check("对比成功", not r.get("error"), r.get("error") or r.get("verdict"))
    if r.get("error"):
        return

    m, b = r["mine"], r["benchmark_metrics"]
    check("两边投入一致", abs(m["invested"] - b["invested"]) < 0.01,
          f"{m['invested']} vs {b['invested']}")
    check("基准份额为正", r["benchmark_units"] > 0, f"{r['benchmark_units']:.4f}")
    check("差额有符号", r["diff_cumulative"] is not None,
          f"{r['diff_cumulative']:+.2f}pp" if r["diff_cumulative"] is not None else "无")

    # 手工复核：把每笔买入按当日收盘价换成份额
    closes = {
        row["date"]: float(row["close"])
        for row in conn.execute("SELECT date, close FROM benchmark_nav ORDER BY date")
    }
    flows, _ = analytics.build_flows(conn, include_terminal=False)
    units = 0.0
    for d, cf in flows:
        px = analytics._close_on_or_before(
            {date.fromisoformat(k): v for k, v in closes.items()}, d
        )
        units += -cf / px if cf < 0 else -cf / px
    check("份额手工复核一致", abs(units - r["benchmark_units"]) < 0.01,
          f"手工 {units:.4f} vs 程序 {r['benchmark_units']:.4f}")


# ---------------------------------------------------------------- 对账


def test_reconcile(conn):
    print("\n== 对账校准 ==")
    pos = {p["code"]: p for p in engine.build_positions(conn)}

    exact = analytics.reconcile_one(
        conn, "T002", pos["T002"]["shares"], pos["T002"]["avg_cost"]
    )
    check("完全相同判为一致", exact["ok"] is True)

    bad_shares = analytics.reconcile_one(conn, "T002", pos["T002"]["shares"] * 0.9)
    check("份额差 10% 判为有差异", bad_shares["ok"] is False)
    hi = [x for x in bad_shares["items"] if x["field"] == "份额"][0]
    check("份额差异给出方向提示", "平台" in hi["hint"], hi["hint"][:24])

    bad_cost = analytics.reconcile_one(conn, "T002", None, pos["T002"]["avg_cost"] * 0.95)
    check("成本差 5% 判为有差异", bad_cost["ok"] is False)
    ci = [x for x in bad_cost["items"] if x["field"] == "成本单价"][0]
    check("成本差异给出归因提示", "漏记" in ci["hint"], ci["hint"][:24])

    tiny = analytics.reconcile_one(conn, "T002", pos["T002"]["shares"] * 1.005)
    check("0.5% 差异在容差内", tiny["ok"] is True)

    missing = analytics.reconcile_one(conn, "ZZZZ")
    check("不存在的基金报错", "error" in missing)

    batch = analytics.reconcile_check(conn, [{"code": "T002", "shares": pos["T002"]["shares"]}])
    check("批量对账统计正确", batch["summary"]["checked"] == 1 and batch["summary"]["mismatched"] == 0)


# ---------------------------------------------------------------- 定投计划


def test_sip(conn):
    print("\n== 定投计划 ==")
    # 造一个每交易日 10 元、从 2026-09-29 起的计划
    pid = sip.save_plan(conn, None, {
        "code": "T001", "amount": 10, "fee_rate": 0.1,
        "freq": "daily", "start_date": "2026-09-29", "note": "测试日定投",
    })
    r = sip.run_plan(conn, pid, "2026-10-21")
    dates = [c["date"] for c in r["created"]]
    check("日定投生成记录", len(dates) > 0, f"{len(dates)} 条")
    check("国庆假期被跳过", not any("2026-10-01" <= d <= "2026-10-07" for d in dates),
          f"首末 {dates[0]} → {dates[-1]}" if dates else "无")

    # 幂等
    r2 = sip.run_plan(conn, pid, "2026-10-21")
    check("重复运行不重复生成", r2["count"] == 0, f"新增 {r2['count']} 条")

    # 每期金额与份额的换算：10 元、费率 0.1% → 净额 9.99
    if r["created"]:
        c0 = r["created"][0]
        expect_net = 10 / 1.001
        got_net = c0["shares"] * c0["nav"]
        check("扣费后份额换算正确", abs(got_net - expect_net) < 0.01,
              f"份额×净值 {got_net:.4f} vs 净额 {expect_net:.4f}")

    # 每周：指定周几
    pid_w = sip.save_plan(conn, None, {
        "code": "T002", "amount": 10, "fee_rate": 0,
        "freq": "weekly", "weekday": 0, "start_date": "2026-09-01",
    })
    rw = sip.run_plan(conn, pid_w, "2026-10-01")
    wdates = [c["date"] for c in rw["created"]]
    all_monday = all(date.fromisoformat(d).weekday() == 0 for d in wdates)
    check("每周计划只落在周一", all_monday, f"{len(wdates)} 期")

    # 每月：指定几号
    pid_m = sip.save_plan(conn, None, {
        "code": "T002", "amount": 100, "fee_rate": 0,
        "freq": "monthly", "monthday": 15, "start_date": "2026-06-01",
    })
    rm = sip.run_plan(conn, pid_m, "2026-09-30")
    mdates = [c["date"] for c in rm["created"]]
    check("每月计划按月生成", len(mdates) == 4, f"{len(mdates)} 期: {mdates}")

    # 校验：金额必须为正
    try:
        sip.save_plan(conn, None, {"code": "T001", "amount": 0, "freq": "weekly",
                                   "start_date": "2026-01-01"})
        check("金额为 0 应被拒绝", False)
    except ValueError:
        check("金额为 0 应被拒绝", True)

    # 校验：结束日期不得早于起始
    try:
        sip.save_plan(conn, None, {"code": "T001", "amount": 10, "freq": "weekly",
                                   "start_date": "2026-06-01", "end_date": "2026-01-01"})
        check("结束早于起始应被拒绝", False)
    except ValueError:
        check("结束早于起始应被拒绝", True)

    # 已生成次数按 plan_id 归属，不串台
    plans = {p["id"]: p for p in sip.list_plans(conn)}
    check("生成计数按计划归属", plans[pid]["generated"] == len(r["created"]),
          f"计划 {pid}: {plans[pid]['generated']} vs 实际 {len(r['created'])}")

    # 净值延迟公布时（QDII 常见 T+1/T+2），不能拿前一天净值冒充当天
    # 造一只净值比大盘晚 5 个交易日才公布的基金
    cal = [
        r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM nav WHERE code = 'T001' ORDER BY date"
        ).fetchall()
    ]
    lag_days = set(cal[-5:])          # 这 5 天 T003 还没公布净值
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES ('T003','延迟公布基金','stock',0,datetime('now'))"
    )
    for d in cal:
        if d in lag_days:
            continue
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES ('T003',?,1.5,1.5,0.0)", (d,)
        )
    conn.commit()
    last_nav_day = conn.execute(
        "SELECT MAX(date) AS d FROM nav WHERE code = 'T003'"
    ).fetchone()["d"]

    pid_p = sip.save_plan(conn, None, {
        "code": "T003", "amount": 10, "fee_rate": 0,
        "freq": "daily", "start_date": cal[-10],
    })
    rp = sip.run_plan(conn, pid_p, cal[-1])
    own_nav = {r["date"] for r in conn.execute(
        "SELECT date FROM nav WHERE code = 'T003'").fetchall()}
    check("只在有净值的日期生成",
          all(c["date"] in own_nav for c in rp["created"]),
          f"{len(rp['created'])} 条")
    check("缺净值的日期留待下次", len(rp.get("pending") or []) == 5,
          f"待补 {len(rp.get('pending') or [])} 期，应为 5")
    check("last_run 不越过待补日期", rp["last_run"] <= last_nav_day,
          f"last_run={rp['last_run']} / 最后净值日={last_nav_day}")
    # 净值补齐后应能把这些天补上
    for d in sorted(lag_days):
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES ('T003',?,1.55,1.55,0.0)", (d,)
        )
    conn.commit()
    rp2 = sip.run_plan(conn, pid_p, cal[-1])
    check("净值补齐后能追平", rp2["count"] == 5, f"补生成 {rp2['count']} 条")

    # 停用后不再生成
    sip.save_plan(conn, pid, {"code": "T001", "amount": 10, "fee_rate": 0.1,
                              "freq": "daily", "start_date": "2026-09-29", "enabled": False})
    rd = sip.run_plan(conn, pid, "2026-10-28")
    check("停用计划不再生成", rd["count"] == 0, rd.get("error") or "")


# ---------------------------------------------------------------- 单基金详情


def test_fund_detail(conn):
    print("\n== 单基金详情 ==")
    d = analytics.fund_detail(conn, "T001")
    check("详情返回成功", not d.get("error"), d.get("error") or d["fund"]["name"])
    if d.get("error"):
        return

    check("净值序列非空", len(d["nav"]) > 0, f"{len(d['nav'])} 条")
    check("买卖点已标注", len(d["markers"]) > 0, f"{len(d['markers'])} 个")
    check("分位落在 0-100", all(0 <= m["percentile"] <= 100 for m in d["markers"]))
    check("区间最低最高合理", d["range"]["min"] <= d["range"]["max"],
          f"{d['range']['min']} - {d['range']['max']}")

    # 分位口径复核：用同一区间的净值直接算
    closes = [x["nav"] for x in d["nav"] if x["nav"] is not None]
    lo, hi = min(closes), max(closes)
    m0 = d["markers"][0]
    expect = (m0["nav"] - lo) / (hi - lo) * 100 if hi > lo else 50.0
    check("分位计算正确", abs(m0["percentile"] - expect) < 0.1,
          f"程序 {m0['percentile']} vs 手算 {round(expect, 1)}")

    check("择时结论存在", bool(d["timing"]["verdict"]), d["timing"]["verdict"] or "无")

    # 货币基金净值恒定，分位必须为空而不是假的 50
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES ('M001','测试货币','cash',1,datetime('now'))"
    )
    for d in (x["date"] for x in conn.execute(
            "SELECT DISTINCT date FROM nav WHERE code = 'T001' ORDER BY date").fetchall()):
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES ('M001',?,1.0,1.0,0.0)", (d,)
        )
    conn.execute(
        "INSERT INTO txns (code,date,action,amount,shares,nav,fee,note,created_at) "
        "VALUES ('M001','2026-06-01','open',1000,1000,1.0,0,'','')"
    )
    conn.commit()
    dm = analytics.fund_detail(conn, "M001")
    check("货币基金分位为空", all(m["percentile"] is None for m in dm["markers"]),
          str([m["percentile"] for m in dm["markers"]]))
    check("货币基金择时均值为空",
          dm["timing"]["buy_avg_percentile"] is None and dm["timing"]["sell_avg_percentile"] is None)
    check("货币基金择时结论正确", "分位无意义" in (dm["timing"]["verdict"] or ""),
          dm["timing"]["verdict"] or "无")

    missing = analytics.fund_detail(conn, "ZZZZ")
    check("不存在的基金报错", "error" in missing)


# ---------------------------------------------------------------- 主流程


def main():
    import tempfile

    test_xirr()
    with tempfile.TemporaryDirectory() as tmp:
        conn = make_db(tmp)
        try:
            test_attribution(conn)
            test_benchmark(conn)
            test_reconcile(conn)
            test_sip(conn)
            test_fund_detail(conn)
        finally:
            conn.close()

    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  -", n)
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
