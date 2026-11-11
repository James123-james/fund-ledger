"""生成 data-pack.json 骨架。

当前里程碑只做：
  - 只读台账：持仓、权重、汇总
  - 观察池：从 watchlist 读取候选
  - 行情指标：留空（pending），等数据源接入

不写 data/fund.db，不生成周报结论。
"""

import argparse
import json
import os
from datetime import date, datetime

import yaml

from .ledger_adapter import DEFAULT_BASE, LedgerError, snapshot


def current_week(day=None):
    day = day or date.today()
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def load_watchlist(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def build(base, watchlist_path, out_path, week=None):
    watchlist = load_watchlist(watchlist_path)
    ledger = snapshot(base)
    week = week or current_week()
    as_of = ledger.get("as_of") or date.today().isoformat()

    candidates = []
    for item in watchlist.get("directions", []):
        if not item.get("enabled", True):
            continue
        candidates.append(
            {
                "sector": item.get("name", ""),
                "id": item.get("id", ""),
                "kind": item.get("kind", ""),
                "track": "pending",
                "proxy": item.get("proxy", ""),
                "proxy_note": item.get("proxy_note", ""),
                "metrics": {},
                "sources": [],
                "catalysts": [],
                "risks": [],
                "overlap_with_holdings": item.get("existing_overlap", ""),
                "data_status": "pending",
            }
        )

    warnings = ["行情数据尚未接入：candidates.metrics 为空，暂不能评分"]
    if watchlist.get("status") != "active":
        warnings.append("观察池仍是 draft，需确认后再用于正式周报")

    holdings = []
    for row in ledger.get("holdings", []):
        holdings.append({**row, "correlation": {}})

    pack = {
        "week": week,
        "as_of": as_of,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "ledger": base,
            "market": "pending",
        },
        "watchlist": {
            "status": watchlist.get("status", "draft"),
            "updated": watchlist.get("updated", ""),
            "count": len(candidates),
        },
        "regime": {
            "code": "",
            "label": "",
            "confidence": "low",
            "changed": None,
            "notes": [],
        },
        "macro": [],
        "candidates": candidates,
        "holdings": holdings,
        "ledger": {
            "funds": ledger.get("funds", 0),
            "txns": ledger.get("txns", 0),
            "sip_plans": ledger.get("sip_plans", 0),
            "summary": ledger.get("summary", {}),
        },
        "data_quality": {
            "missing": ["行情指标未接入"],
            "stale": [],
            "warnings": warnings,
        },
    }

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(pack, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return out_path, pack


def main():
    ap = argparse.ArgumentParser(description="生成半自动周报的数据包骨架")
    ap.add_argument("--base", default=DEFAULT_BASE, help="台账地址")
    ap.add_argument(
        "--watchlist",
        default="research/config/watchlist.draft.yml",
        help="观察池 YAML",
    )
    ap.add_argument("--out", default=None, help="输出路径")
    ap.add_argument("--week", default=None, help="ISO 周，如 2026-W46")
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


if __name__ == "__main__":
    main()
