"""把历史定投流水归类成正式计划。

解决的问题
----------
早期导入的定投记录没有 plan_id，导致三件事做不了：
- 计划卡片上「已生成 N 次」统计不到
- 无法做「定投纪律性」分析（没有计划就没有偏离度）
- 后续自动补生成会与历史记录脱节

用法
----
    python tools/adopt_sip.py --dry-run     # 先看方案，不写库
    python tools/adopt_sip.py               # 确认后执行
    python tools/adopt_sip.py --code 004253 # 只处理某只基金

判定逻辑
--------
按「基金 + 金额」分组，再按周期特征切片：
- 末尾若存在「同一星期几、间隔为 7 的倍数」的连续段 → 周定投
- 若存在「日号相同、间隔 28-31 天」的连续段 → 月定投
- 其余归为日定投

片段中已结束的设为停用，最后一段（仍在进行）设为启用。

工具可重复执行：每次先清空该基金的旧计划与绑定，再重建。
"""

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import backup, db, sip  # noqa: E402

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def freq_label(p):
    if p["freq"] == "daily":
        return "每交易日"
    if p["freq"] == "weekly":
        return "每" + WEEKDAY[p["weekday"]] if p["weekday"] is not None else "每周"
    if p["freq"] == "monthly":
        return f"每月 {p['monthday']} 号"
    return p["freq"]


def show(plans):
    cur = None
    for p in plans:
        if p["code"] != cur:
            cur = p["code"]
            print(f"\n{p['code']}  {p['fund_name']}")
        hm = "（假期跳过）" if p["holiday_mode"] == "skip" else ""
        end = p["end_date"] or "继续"
        state = "进行中" if p["enabled"] else "已结束"
        print(
            f"  {freq_label(p):10s}{hm:10s}{p['amount']:>8.2f} 元/期  "
            f"{p['start_date']} → {end}  {p['count']:>3d} 笔  {state}"
        )


def main():
    ap = argparse.ArgumentParser(description="把历史定投流水归类成正式计划")
    ap.add_argument("--data", default=None, help="数据目录，默认 ./data")
    ap.add_argument("--code", action="append", help="只处理指定基金，可重复")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不写库")
    ap.add_argument("--no-backup", action="store_true", help="跳过执行前的自动备份")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data) if args.data else os.path.join(BASE, "data")
    db_path = os.path.join(data_dir, "fund.db")
    if not os.path.isfile(db_path):
        raise SystemExit(f"找不到数据库：{db_path}")

    conn = db.init(db_path)
    preview = sip.adopt_history(conn, args.code, dry_run=True)
    plans = preview["plans"]

    if not plans:
        print("没有找到可归类的定投流水。")
        conn.close()
        return

    print(f"检测到 {len(plans)} 个计划片段，覆盖 {sum(p['count'] for p in plans)} 笔流水：")
    show(plans)
    print()

    if args.dry_run:
        print("（--dry-run，未写库）")
        conn.close()
        return

    if not args.no_backup:
        name, size = backup.make(db_path, tag="before-adopt")
        if name:
            print(f"已备份：{name}（{size // 1024} KB）")
        else:
            print(f"[警告] 备份失败：{size}")
            if input("继续执行？(y/N) ").strip().lower() != "y":
                conn.close()
                return

    result = sip.adopt_history(conn, args.code)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    print(f"完成：创建 {result['created']} 个计划，绑定 {result['bound']} 笔流水。")


if __name__ == "__main__":
    main()
