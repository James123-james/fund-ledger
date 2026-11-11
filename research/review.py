"""4 周复盘汇总：运行次数/耗时/数据问题/触发/行为。

用法（仓库根目录）：
    python3 -m research.review

读取：
    reports/run-log.csv
    reports/tracking.csv

只读汇总，不改文件。
"""

import csv
import os
from statistics import mean

RUN_LOG = os.path.join("reports", "run-log.csv")
TRACKING = os.path.join("reports", "tracking.csv")


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh) if any((v or "").strip() for v in r.values())]


def _num(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _yes(value):
    return str(value or "").strip().lower() in {"y", "yes", "是", "1", "true"}


def main():
    runs = _rows(RUN_LOG)
    track = _rows(TRACKING)

    print("=" * 48)
    print("4 周复盘汇总（W46–W50）")
    print("=" * 48)

    if runs:
        minutes = [_num(r.get("run_minutes")) for r in runs]
        minutes = [m for m in minutes if m is not None]
        read_minutes = [_num(r.get("read_minutes")) for r in runs]
        read_minutes = [m for m in read_minutes if m is not None]
        data_issues = [
            r for r in runs if str(r.get("data_issues", "")).strip() not in {"", "0", "0次", "无"}
        ]
        read_done = [r for r in runs if _yes(r.get("report_read")) or "部分" in str(r.get("report_read", ""))]

        print(f"运行：{len(runs)} 次 / 计划 5 次")
        print(f"每次耗时：{round(mean(minutes), 1)} 分钟" if minutes else "每次耗时：未填")
        print(f"读报告：{round(mean(read_minutes), 1)} 分钟" if read_minutes else "读报告：未填")
        print(f"数据问题：{len(data_issues)} 次")
        print(f"报告看完/部分：{len(read_done)} / {len(runs)} 次")

        clarity = [_num(r.get("clarity")) for r in runs]
        clarity = [x for x in clarity if x is not None]
        control = [_num(r.get("impulse_control")) for r in runs]
        control = [x for x in control if x is not None]
        print(f"清晰度：{round(mean(clarity), 1)} / 5" if clarity else "清晰度：未填")
        print(f"冲动控制：{round(mean(control), 1)} / 5" if control else "冲动控制：未填")

        print("")
        print("行为计数")
        for key, label in [("impulse", "无触发操作"), ("chase", "追涨"), ("overlap_buy", "硬买高重叠")]:
            n = sum(1 for r in runs if _yes(r.get(key)))
            print(f"  {label}：{n} 次")
        actions = [str(r.get("actions", "")).strip() for r in runs]
        print("  记录的操作：" + ("、".join(a for a in actions if a) or "无"))

        print("")
        print("逐周")
        for r in runs:
            print(
                f"  {r.get('week','?'):8} {r.get('run_date','?'):10} "
                f"{r.get('run_minutes','?')} 分钟；触发：{r.get('triggered','-') or '-'}；"
                f"操作：{r.get('actions','-') or '-'}"
            )
    else:
        print("run-log.csv 还没有数据；每周跑完填一行。")

    print("")
    print("触发/判断")
    if track:
        counts = {}
        for r in track:
            counts[r.get("status", "pending")] = counts.get(r.get("status", "pending"), 0) + 1
        print("  状态：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))
        for r in track:
            print(
                f"  {r.get('direction','?'):6} 复盘 {r.get('review_date','?'):10} "
                f"状态 {r.get('status','pending'):8} 判断 {r.get('judgment','待定')} "
                f"结果 {r.get('outcome','-') or '-'}"
            )
        hit = sum(1 for r in track if r.get("status") == "hit")
        miss = sum(1 for r in track if r.get("status") == "miss")
        if hit + miss:
            print(f"  触发命中率：{hit}/{hit + miss} = {round(hit / (hit + miss) * 100, 1)}%")
        else:
            print("  触发命中率：还没有 hit/miss，继续观察")
    else:
        print("tracking.csv 还没有数据。")

    print("")
    print("提醒：4 周样本只看流程，不足以证明策略有效；不要因为短期盈亏改规则。")


if __name__ == "__main__":
    main()
