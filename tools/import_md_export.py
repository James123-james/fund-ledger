#!/usr/bin/env python3
"""从基金台账的「完整」Markdown 导出重建数据库。

只解析重建账本必须的几张表：
  - 组合概况（用来核对总额）
  - 当前持仓（基金、类别、最新净值）
  - 收益归因（给已清仓基金补类别）
  - 操作流水（全部买卖、分红、定投）

不导入：
  - 定投计划的规则（历史流水完整保留，计划可在界面里再建）
  - 每日净值序列（导出里只有组合层面的逐日数据，没有各基金逐日净值；
    联网后点「更新净值」可以补齐）

用法：
    python3 tools/import_md_export.py 导出.md --out /tmp/fund.db
    python3 tools/import_md_export.py 导出.md --out /tmp/fund.db --reset

建议先 --out 到临时文件，核对无误后再用服务端的「导入账本」接口替换。
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import db, engine  # noqa: E402

ACTION_MAP = {
    "建仓": "open",
    "加仓": "add",
    "减仓": "reduce",
    "清仓": "close",
    "定投": "sip",
    "分红": "dividend",
}

CATEGORY_MAP = {
    "股票": "stock",
    "债券": "bond",
    "黄金/商品": "gold",
    "现金": "cash",
    "海外": "overseas",
    "其他": "other",
}


# ---------------------------------------------------------------- 解析


def read_lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def section(lines, prefix, stop_prefixes=("## ",)):
    start = None
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            start = i
            break
    if start is None:
        raise SystemExit(f"找不到章节：{prefix}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if any(lines[j].startswith(p) for p in stop_prefixes):
            end = j
            break
    return lines[start:end]


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(block):
    """返回 [(表头列表, [dict, ...]), ...]，跳过表头分隔行。"""
    out = []
    i = 0
    while i < len(block):
        line = block[i].strip()
        if (
            line.startswith("|")
            and i + 1 < len(block)
            and re.fullmatch(r"\|[\s:|-]+\|", block[i + 1].strip() or "")
        ):
            header = split_row(line)
            rows = []
            j = i + 2
            while j < len(block) and block[j].strip().startswith("|"):
                cells = split_row(block[j])
                if len(cells) == len(header):
                    rows.append(dict(zip(header, cells)))
                j += 1
            out.append((header, rows))
            i = j
        else:
            i += 1
    return out


def find_table(tables, *keys):
    for header, rows in tables:
        if all(k in header for k in keys):
            return header, rows
    return None, []


def num(value, default=None):
    if value is None:
        return default
    text = str(value).strip().replace("¥", "").replace(",", "").replace(" ", "")
    if text in ("", "—", "-", "–", "--"):
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return default
    return float(m.group(0))


def pct(value, default=None):
    if value is None:
        return default
    text = str(value).strip().replace("%", "").replace("+", "")
    if text in ("", "—", "-", "–", "--"):
        return default
    return num(text, default)


def text(value):
    s = (value or "").strip()
    return "" if s in ("—", "-", "–", "--", "") else s


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser(description="从完整 Markdown 导出重建账本数据库")
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--reset", action="store_true", help="目标库已有数据时先清空")
    args = ap.parse_args()

    lines = read_lines(args.source)

    # 1) 概况
    overview_rows = parse_tables(section(lines, "## 1."))[0][1]
    expected = {}
    for row in overview_rows:
        label = row.get("指标", "")
        if label:
            expected[label] = row.get("数值", "")

    # 2) 当前持仓 + 已清仓
    holding_block = section(lines, "## 2.")
    holding_tables = parse_tables(holding_block)
    _, holdings = find_table(holding_tables, "持有份额", "最新净值")
    _, cleared = find_table(holding_tables, "累计买入", "累计卖出")
    if not holdings:
        raise SystemExit("没有解析到「当前持仓」表")

    # 3) 收益归因里的类别（覆盖已清仓基金）
    attr_tables = parse_tables(section(lines, "### 3.2", ("### ", "## ")))
    _, attr_rows = find_table(attr_tables, "合计盈亏", "类别")

    # 4) 操作流水
    txn_tables = parse_tables(section(lines, "## 4."))
    _, txn_rows = find_table(txn_tables, "日期", "成交净值", "操作")
    if not txn_rows:
        raise SystemExit("没有解析到「操作流水」表")

    # ---- 汇总基金与类别
    funds = {}

    def put_fund(code, name, category_text=""):
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            return
        name = (name or "").strip()
        cat = CATEGORY_MAP.get(category_text.strip(), "")
        if not cat:
            if "货币" in name:
                cat = "cash"
            elif "债" in name:
                cat = "bond"
            elif "黄金" in name:
                cat = "gold"
            else:
                cat = "stock"
        cur = funds.setdefault(code, {"name": name, "category": cat})
        if name:
            cur["name"] = name
        if cat:
            cur["category"] = cat

    for row in holdings:
        put_fund(row.get("代码"), row.get("名称"), row.get("类别", ""))
    for row in cleared:
        put_fund(row.get("代码"), row.get("名称"))
    for row in attr_rows:
        put_fund(row.get("代码"), row.get("名称"), row.get("类别", ""))
    for row in txn_rows:
        put_fund(row.get("代码"), row.get("名称"))

    # ---- 目标库
    out = os.path.abspath(args.out)
    if os.path.exists(out):
        try:
            probe = sqlite3.connect(out)
            existing = probe.execute("SELECT COUNT(*) FROM txns").fetchone()[0]
            probe.close()
        except sqlite3.Error:
            existing = 0
        if existing and not args.reset:
            raise SystemExit(f"{out} 里已有 {existing} 笔流水；要覆盖请加 --reset")
    if args.reset and os.path.exists(out):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(out + suffix)
            except OSError:
                pass

    conn = db.init(out)
    try:
        if args.reset:
            conn.execute("DELETE FROM txns")
            conn.execute("DELETE FROM nav")
            conn.execute("DELETE FROM funds")
            conn.commit()

        now = datetime.now().isoformat(timespec="seconds")
        for code, info in sorted(funds.items()):
            is_money = 1 if ("货币" in info["name"]) else 0
            conn.execute(
                "INSERT OR REPLACE INTO funds "
                "(code, name, category, fund_type, company, source_rate, rate, is_money, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    info["name"] or code,
                    info["category"],
                    "货币型" if is_money else None,
                    None,
                    0.0,
                    0.0,
                    is_money,
                    "",
                    now,
                ),
            )

        # ---- 流水
        inserted = 0
        for row in txn_rows:
            action_name = (row.get("操作") or "").strip()
            action = ACTION_MAP.get(action_name)
            if not action:
                raise SystemExit(f"未知操作类型：{action_name}")
            code = (row.get("代码") or "").strip()
            date = (row.get("日期") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise SystemExit(f"日期格式不对：{date}")
            amount = num(row.get("金额(元)"), 0.0)
            shares = num(row.get("份额"), 0.0)
            nav = num(row.get("成交净值"))
            fee = num(row.get("手续费(元)"), 0.0)
            note = text(row.get("备注"))
            reason = text(row.get("决策理由"))
            conn.execute(
                "INSERT INTO txns (code, date, action, amount, shares, nav, fee, plan_id, reason, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (code, date, action, amount, shares, nav, fee, None, reason, note, now),
            )
            inserted += 1

        # ---- 最新净值：只写持仓表里的最新一条，供当前市值计算
        nav_rows = 0
        for row in holdings:
            code = (row.get("代码") or "").strip()
            nav = num(row.get("最新净值"))
            date = (row.get("净值日期") or "").strip()
            if not code or nav is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                continue
            fund = funds.get(code, {})
            pct_val = pct(row.get("当日涨跌"))
            if "货币" in (fund.get("name") or ""):
                # 货币基金净值恒为 1，收益体现在「万份收益」。
                # 导出只给了累计收益（浮动盈亏），折算成一条日收益率，
                # 让当前市值与导出完全一致；这不是原始净值曲线。
                shares = num(row.get("持有份额"), 0.0)
                accrued = num(row.get("浮动盈亏"), 0.0)
                pct_val = round(accrued / shares * 100, 6) if shares else 0.0
            conn.execute(
                "INSERT OR REPLACE INTO nav (code, date, nav, acc_nav, pct) VALUES (?,?,?,?,?)",
                (code, date, nav, None, pct_val),
            )
            nav_rows += 1

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    # ---- 核对
    conn = db.connect(out)
    try:
        positions = engine.build_positions(conn)
        summary = engine.summarize(positions)
        pos_by_code = {p["code"]: p for p in positions}
    finally:
        conn.close()

    print(f"写入：{len(funds)} 只基金，{inserted} 笔流水，{nav_rows} 条最新净值")
    print(f"输出：{out}")
    print("")
    print("核对（导出 → 重建）：")
    checks = [
        ("持仓市值", "value"),
        ("持仓成本", "cost"),
        ("浮动盈亏", "unrealized"),
        ("已实现盈亏", "realized"),
        ("累计盈亏", "total_pnl"),
        ("累计投入", "total_buy"),
        ("累计手续费", "total_fee"),
    ]
    # 导出里的份额只有 2 位小数，逐笔重放会累积几分钱误差；
    # 成本、已实现、投入、手续费必须完全对上，市值类允许 0.10 以内。
    tol_by_label = {"持仓市值": 0.10, "浮动盈亏": 0.10, "累计盈亏": 0.10}
    bad = 0
    for label, key in checks:
        want = num(expected.get(label))
        got = summary.get(key)
        if want is None:
            continue
        diff = abs(got - want)
        tol = tol_by_label.get(label, 0.02)
        mark = "OK" if diff <= tol else "差异"
        if diff > tol:
            bad += 1
        print(f"  {label:6}  {want:>10.2f} → {got:>10.2f}  {mark}")

    print("")
    print("逐只持仓：")
    for row in holdings:
        code = (row.get("代码") or "").strip()
        want_shares = num(row.get("持有份额"), 0.0)
        want_value = num(row.get("市值"), 0.0)
        p = pos_by_code.get(code)
        got_shares = p["shares"] if p else 0.0
        got_value = p["value"] if p else 0.0
        ok = abs(got_shares - want_shares) <= 0.03 and abs(got_value - want_value) <= 0.10
        print(
            f"  {code} {row.get('名称','')[:18]:18} 份额 {want_shares:>9.2f} → {got_shares:>9.2f}"
            f"  市值 {want_value:>8.2f} → {got_value:>8.2f}  {'OK' if ok else '差异'}"
        )
        if not ok:
            bad += 1

    # 当日盈亏依赖前一日净值，导出里没有各基金前一日净值，单独提示
    want_today = num(expected.get("当日盈亏"))
    got_today = summary.get("today_pnl")
    if want_today is not None and abs(got_today - want_today) > 0.05:
        print("")
        print(f"提示：当日盈亏未复现（导出 {want_today:.2f} → 重建 {got_today:.2f}），"
              "因为需要各基金前一交易日的净值；不影响持仓、成本和累计盈亏。")

    if bad:
        print("")
        print(f"有 {bad} 项与导出不一致，先别替换正在用的账本。")
        sys.exit(2)
    print("")
    print("成本、已实现盈亏、投入、手续费与导出完全一致；")
    print("市值差几分钱是导出份额只保留 2 位小数造成的，可以替换。")


if __name__ == "__main__":
    main()
