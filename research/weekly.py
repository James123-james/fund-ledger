"""一键生成某周的数据包骨架。

用法（在仓库根目录）：
    python3 -m research.weekly
    python3 -m research.weekly --week 2026-W46
"""

import argparse
import os

from .build_pack import build, current_week
from .ledger_adapter import DEFAULT_BASE, LedgerError


def main():
    ap = argparse.ArgumentParser(description="半自动周报：生成数据包")
    ap.add_argument("--week", default=None, help="ISO 周，如 2026-W46")
    ap.add_argument("--base", default=DEFAULT_BASE, help="台账地址")
    ap.add_argument(
        "--watchlist",
        default="research/config/watchlist.draft.yml",
        help="观察池 YAML",
    )
    ap.add_argument("--out", default=None, help="输出路径")
    args = ap.parse_args()

    week = args.week or current_week()
    out = args.out or os.path.join("research", "data", week, "data-pack.json")
    try:
        path, pack = build(args.base, args.watchlist, out, week)
    except LedgerError as exc:
        raise SystemExit(f"[台账] {exc}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"[配置] 找不到文件：{exc.filename}") from exc

    print(f"数据包：{path}")
    print(f"持仓：{len(pack['holdings'])} 只；候选：{len(pack['candidates'])} 个")
    print("行情：pending（下一步接入数据源）")
    print("下一步：接行情指标 -> 生成 weekly.md -> 质检 -> 人工决定")


if __name__ == "__main__":
    main()
