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

from app import analytics, db, engine  # noqa: E402

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
        for i in range(730):
            d = base + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            if code == "T001":
                nav = 1.0 + 0.6 * (i / 730) - (0.4 if i > 500 else 0) * ((i - 500) / 230)
            else:
                nav = 1.0 + 0.3 * (i / 730)
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
    for i in range(730):
        d = base + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        conn.execute(
            "INSERT INTO benchmark_nav (secid,date,close) VALUES ('1.000300',?,?)",
            (d.strftime("%Y-%m-%d"), round(4000.0 * (1 + 0.2 * i / 730), 2)),
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
