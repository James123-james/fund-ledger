"""一键生成某周的数据包骨架。

用法（在仓库根目录）：
    python3 -m research.weekly
    python3 -m research.weekly --week 2026-W46
"""

import argparse
import os

from .build_pack import DEFAULT_MACRO, DEFAULT_METRICS, build, current_week
from .fetch_macro import fetch_all as fetch_macro_all
from .fetch_market import CACHE_DIR, SOURCE_MAP, WATCHLIST, fetch_all as fetch_market_all
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
    ap.add_argument("--no-fetch", action="store_true", help="使用已有行情缓存，不抓取")
    ap.add_argument("--months", type=int, default=60, help="申万月度历史月数")
    ap.add_argument("--metrics", default=DEFAULT_METRICS, help="行情指标 JSON")
    ap.add_argument("--macro", default=DEFAULT_MACRO, help="宏观 JSON")
    args = ap.parse_args()

    week = args.week or current_week()
    out = args.out or os.path.join("research", "data", week, "data-pack.json")

    if not args.no_fetch:
        print("抓取宏观...")
        try:
            fetch_macro_all()
        except SystemExit:
            raise
        except Exception as exc:
            print(f"宏观抓取失败，改用已有缓存：{type(exc).__name__}: {exc}")
        print("抓取行情（AKShare：宽基/申万/ETF）...")
        try:
            fetch_market_all(args.months, CACHE_DIR, WATCHLIST, SOURCE_MAP)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"行情抓取失败，改用已有缓存：{type(exc).__name__}: {exc}")

    try:
        path, pack = build(args.base, args.watchlist, out, week, args.metrics, args.macro)
    except LedgerError as exc:
        raise SystemExit(f"[台账] {exc}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"[配置] 找不到文件：{exc.filename}") from exc

    status = {}
    for c in pack.get("candidates", []):
        status[c.get("data_status", "pending")] = status.get(c.get("data_status", "pending"), 0) + 1
    print(f"数据包：{path}")
    print(f"持仓：{len(pack['holdings'])} 只；候选：{len(pack['candidates'])} 个")
    print("行情状态：" + "，".join(f"{k} {v}" for k, v in sorted(status.items())))
    macro_ok = sum(1 for m in pack.get("macro", []) if m.get("status") in ("ok", "manual"))
    print(f"宏观：{macro_ok}/{len(pack.get('macro', []))} 个变量可用")
    print("下一步：用 skill 读数据包写 weekly.md -> 质检 -> 人工决定")


if __name__ == "__main__":
    main()
