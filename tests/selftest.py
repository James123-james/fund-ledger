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

from app import analytics, backup, db, dividend, engine, sip  # noqa: E402

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


# ---------------------------------------------------------------- 历史定投归类


def test_adopt(tmpdir):
    print("\n== 历史定投归类 ==")
    import os

    p = os.path.join(tmpdir, "adopt.db")
    conn = db.init(p)
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES ('AD1','归类测试','stock',0,datetime('now'))"
    )
    # 造 2026-08-03 ~ 2026-10-30 的交易日历，并挖掉国庆（10-01~10-07）
    cal = []
    d = date(2026, 8, 3)
    while d <= date(2026, 10, 30):
        if d.weekday() < 5 and not (date(2026, 10, 1) <= d <= date(2026, 10, 7)):
            cal.append(d.isoformat())
        d += timedelta(days=1)
    for ds in cal:
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES ('AD1',?,1.0,1.0,0)", (ds,)
        )
    conn.commit()

    # 前段：交易日日投；后段：每周一（跳过国庆那个周一）
    daily = [d for d in cal if "2026-08-03" <= d <= "2026-09-16"]
    mondays = [d for d in cal if date.fromisoformat(d).weekday() == 0
               and "2026-09-21" <= d <= "2026-10-26"]
    for ds in daily + mondays:
        conn.execute(
            "INSERT INTO txns (code,date,action,amount,shares,nav,fee,note,created_at) "
            "VALUES ('AD1',?,'sip',10,10,1.0,0,'','')", (ds,)
        )
    conn.commit()

    # 模式识别
    segs = sip.split_patterns(sorted(daily + mondays))
    check("切出两个片段", len(segs) == 2, f"{len(segs)} 段")
    check("前段识别为日投", segs[0][0] == "daily", segs[0][0])
    check("后段识别为周投", segs[1][0] == "weekly", segs[1][0])
    check("周投星期正确", segs[1][1] == 0, f"weekday={segs[1][1]}")

    # 日投不应被误判为周投：只给连续交易日
    only_daily = sorted(daily)
    segs2 = sip.split_patterns(only_daily)
    check("连续日投不被误判", len(segs2) == 1 and segs2[0][0] == "daily",
          str([(x[0], len(x[2])) for x in segs2]))

    # 单笔不成模式
    segs3 = sip.split_patterns([daily[0]])
    check("单笔归为日投", segs3[0][0] == "daily", segs3[0][0])

    # 预演不写库
    prev = sip.adopt_history(conn, ["AD1"], dry_run=True)
    check("预演返回两个计划", len(prev["plans"]) == 2, str(len(prev["plans"])))
    check("预演不写库",
          conn.execute("SELECT COUNT(*) c FROM sip_plans WHERE code='AD1'").fetchone()["c"] == 0)

    # 假期模式判定：周一段缺了国庆那个周一 → skip
    wk = [x for x in prev["plans"] if x["freq"] == "weekly"][0]
    check("识别出假期跳过", wk["holiday_mode"] == "skip", wk["holiday_mode"])

    # 执行
    res = sip.adopt_history(conn, ["AD1"])
    check("创建两个计划", res["created"] == 2, str(res["created"]))
    check("绑定全部流水", res["bound"] == len(daily) + len(mondays),
          f"{res['bound']} vs {len(daily)+len(mondays)}")

    tot = conn.execute("SELECT COUNT(*) c FROM txns WHERE code='AD1'").fetchone()["c"]
    bnd = conn.execute(
        "SELECT COUNT(*) c FROM txns WHERE code='AD1' AND plan_id IS NOT NULL"
    ).fetchone()["c"]
    check("无未绑定流水", tot == bnd, f"{bnd}/{tot}")

    # 已结束片段停用、进行中片段启用
    plans = [dict(r) for r in conn.execute(
        "SELECT * FROM sip_plans WHERE code='AD1' ORDER BY start_date").fetchall()]
    check("前段已停用", plans[0]["enabled"] == 0)
    check("后段启用中", plans[1]["enabled"] == 1)
    check("前段有结束日", plans[0]["end_date"] is not None, str(plans[0]["end_date"]))
    check("后段无结束日", plans[1]["end_date"] is None)

    # 幂等：重复执行不产生重复计划
    sip.adopt_history(conn, ["AD1"])
    n = conn.execute("SELECT COUNT(*) c FROM sip_plans WHERE code='AD1'").fetchone()["c"]
    check("重复归类不重复建计划", n == 2, f"{n} 个")
    bnd2 = conn.execute(
        "SELECT COUNT(*) c FROM txns WHERE code='AD1' AND plan_id IS NOT NULL"
    ).fetchone()["c"]
    check("重复归类仍全部绑定", bnd2 == tot, f"{bnd2}/{tot}")

    # 归类后补生成不应重复写入
    before = conn.execute("SELECT COUNT(*) c FROM txns WHERE code='AD1'").fetchone()["c"]
    sip.run_all(conn, until="2026-10-26")
    after = conn.execute("SELECT COUNT(*) c FROM txns WHERE code='AD1'").fetchone()["c"]
    check("归类后补生成不重复", after == before, f"{before} → {after}")

    conn.close()


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


# ---------------------------------------------------------------- 分红识别


def _make_dividend_fund(conn, code="DIV1", div_dates=("2026-07-02", "2026-08-04")):
    """造一只净值恒定 1.6、按指定日期每次分红 0.01 的基金。

    分红表现为「累计净值增加而单位净值不变」，正是真实数据的形态。
    """
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES (?,?,'stock',0,datetime('now'))", (code, "分红测试基金")
    )
    days = ["2026-06-01", "2026-06-02", "2026-06-03",
            "2026-07-01", "2026-07-02", "2026-07-03",
            "2026-08-03", "2026-08-04", "2026-08-05"]
    acc = 1.0
    for d in days:
        if d in div_dates:
            acc = round(acc + 0.01, 4)
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES (?,?,?,?,0)",
            (code, d, 1.6, acc),
        )
    conn.execute(
        "INSERT INTO txns (code,date,action,amount,shares,nav,fee,note,created_at) "
        "VALUES (?,'2026-06-01','open',1600,1000,1.6,0,'','')", (code,)
    )
    conn.commit()


def test_dividend(conn):
    print("\n== 分红识别 ==")
    _make_dividend_fund(conn)

    found = dividend.detect_fund(conn, "DIV1")
    check("检测到两次分红", len(found) == 2, f"{len(found)} 次")
    check("每份金额正确", all(abs(d["per_share"] - 0.01) < 1e-6 for d in found),
          str([d["per_share"] for d in found]))
    check("除息日正确",
          [d["ex_date"] for d in found] == ["2026-07-02", "2026-08-04"],
          str([d["ex_date"] for d in found]))

    r = dividend.scan(conn, "DIV1")
    check("扫描结果为未记录", r["summary"]["pending"] == 2, str(r["summary"]))
    it = r["items"][0]
    check("登记日为除息日前一交易日", it["record_date"] == "2026-07-01", it["record_date"])
    check("登记日份额正确", abs(it["shares"] - 1000) < 0.01, str(it["shares"]))
    check("应得现金 = 份额 × 每份", abs(it["cash"] - 10.0) < 0.01, str(it["cash"]))
    check("再投份额 = 现金 ÷ 净值", abs(it["reinvest_shares"] - 6.25) < 0.01,
          str(it["reinvest_shares"]))

    # 现金分红
    res = dividend.apply(conn, [{"code": "DIV1", "ex_date": "2026-07-02", "mode": "cash"}])
    check("现金分红写入成功", res["written"] == 1, str(res["results"][0].get("error") or "OK"))
    pos = {p["code"]: p for p in engine.build_positions(conn)}["DIV1"]
    check("现金分红不增份额", abs(pos["shares"] - 1000) < 0.01, str(pos["shares"]))
    check("现金分红计入已实现", abs(pos["realized"] - 10.0) < 0.01, str(pos["realized"]))
    check("现金分红不改成本", abs(pos["cost"] - 1600) < 0.01, str(pos["cost"]))

    # 幂等
    res2 = dividend.apply(conn, [{"code": "DIV1", "ex_date": "2026-07-02", "mode": "cash"}])
    check("同日重复入账被拒", res2["written"] == 0, str(res2["results"][0].get("error")))

    # 红利再投
    res3 = dividend.apply(conn, [{"code": "DIV1", "ex_date": "2026-08-04", "mode": "reinvest"}])
    check("红利再投写入成功", res3["written"] == 1, str(res3["results"][0].get("error") or "OK"))
    pos = {p["code"]: p for p in engine.build_positions(conn)}["DIV1"]
    check("再投增加份额", pos["shares"] > 1006, str(pos["shares"]))
    check("再投成本不变", abs(pos["cost"] - 1600) < 0.01, str(pos["cost"]))
    check("再投落袋为 0", abs(pos["realized"] - 10.0) < 0.01,
          f"{pos['realized']}（仅前一笔现金分红）")

    # 已记录状态
    r2 = dividend.scan(conn, "DIV1")
    check("入账后标记为已记录", r2["summary"]["recorded"] == 2, str(r2["summary"]))

    # 无持仓期间的分红不应提示
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES ('DIV2','无持仓基金','stock',0,datetime('now'))"
    )
    acc = 1.0
    for d in ("2025-01-02", "2025-01-03"):
        if d == "2025-01-03":
            acc = 1.01
        conn.execute(
            "INSERT INTO nav (code,date,nav,acc_nav,pct) VALUES ('DIV2',?,1.5,?,0)", (d, acc)
        )
    conn.commit()
    r3 = dividend.scan(conn, "DIV2")
    check("无持仓的分红不提示", r3["summary"]["total"] == 0, str(r3["summary"]))

    # 货币基金不参与净值型分红
    r4 = dividend.scan(conn, "M001")
    check("货币基金被跳过", r4["summary"]["total"] == 0, str(r4["summary"]))


# ---------------------------------------------------------------- 自动备份


def test_backup(tmpdir):
    print("\n== 自动备份 ==")
    import os

    p = os.path.join(tmpdir, "bk.db")
    conn = db.init(p)
    conn.execute(
        "INSERT INTO funds (code,name,category,is_money,created_at) "
        "VALUES ('BK1','备份测试','stock',0,datetime('now'))"
    )
    conn.commit()

    r = backup.ensure_daily(p)
    check("首次自动备份成功", r.get("created") is True, str(r.get("name") or r.get("reason")))

    r2 = backup.ensure_daily(p)
    check("同日不重复备份", r2.get("created") is False, str(r2.get("reason")))

    # 备份内容必须包含最新写入（WAL 模式下直接复制文件会丢这部分）
    d = backup.backup_dir(p)
    f = os.path.join(d, r["name"])
    c2 = db.connect(f)
    n = c2.execute("SELECT COUNT(*) FROM funds WHERE code='BK1'").fetchone()[0]
    c2.close()
    check("备份包含最新写入", n == 1, f"{n} 条")

    # 保留上限
    for i in range(backup.KEEP + 5):
        open(os.path.join(d, f"fund-2020{i:04d}.db"), "w").write("x")
    backup._prune(p)
    after = len(backup._list_backups(p))
    check("超出上限自动清理", after == backup.KEEP, f"剩 {after} 份 / 上限 {backup.KEEP}")

    st = backup.stats(p)
    check("统计份数正确", st["count"] == backup.KEEP, str(st["count"]))
    # 文件名带日期，字典序即时间序；items 反转后第一条应是字典序最大的那个
    allnames = sorted(backup._list_backups(p))
    check("最新一份排在最前", st["items"][0]["name"] == allnames[-1],
          f"{st['items'][0]['name']} vs 应为 {allnames[-1]}")
    conn.close()


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
            test_dividend(conn)
            test_backup(tmp)
            test_adopt(tmp)
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
