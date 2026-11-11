"""催化登记表读取与校验。

不自动抓新闻：财联社/新浪快讯只给最近几十条，无法可靠覆盖 30 天。
这里只校验人工/研究确认的条目，避免用当天快讯冒充 30 日催化。
"""

import argparse
import json
import os
from datetime import date, datetime, timedelta

import yaml

DEFAULT = os.path.join("research", "config", "catalysts.draft.yml")
CACHE = os.path.join("research", "data", "catalysts")
TYPES = {"policy", "data", "event"}
STATUSES = {"confirmed", "candidate"}


def load_registry(path=DEFAULT):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def collect(registry, as_of=None, window_days=None):
    as_of = as_of or date.today()
    window_days = window_days or int(registry.get("window_days", 30))
    start = as_of - timedelta(days=window_days)
    end = as_of + timedelta(days=window_days)

    directions = {}
    warnings = []
    total = 0
    confirmed = 0

    for did, block in (registry.get("directions") or {}).items():
        items = []
        for raw in (block or {}).get("items", []) or []:
            d = _date(raw.get("date"))
            title = str(raw.get("title") or "").strip()
            source = str(raw.get("source") or "").strip()
            typ = str(raw.get("type") or "").strip()
            stat = str(raw.get("status") or "candidate").strip()
            if not d or not title or not source:
                warnings.append(f"{did}: 条目缺日期/标题/来源，已跳过")
                continue
            if d < start or d > end:
                warnings.append(f"{did}: 「{title}」不在 {window_days} 日窗口，已跳过")
                continue
            if typ not in TYPES:
                warnings.append(f"{did}: 「{title}」类型 {typ or '空'} 不规范")
                typ = "event"
            if stat not in STATUSES:
                stat = "candidate"
            items.append(
                {
                    "date": d.isoformat(),
                    "type": typ,
                    "title": title,
                    "source": source,
                    "url": str(raw.get("url") or ""),
                    "status": stat,
                    "note": str(raw.get("note") or ""),
                    "timing": "upcoming" if d > as_of else "past",
                    "age_days": (as_of - d).days,
                }
            )
            total += 1
            if stat == "confirmed":
                confirmed += 1
        items.sort(key=lambda x: x["date"], reverse=True)
        directions[did] = {
            "status": "ok" if items else "none",
            "items": items[:3],
            "count": len(items),
        }

    if total == 0:
        warnings.append(f"催化登记表为空：0 个方向有 {window_days} 日催化")
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "total": total,
        "confirmed": confirmed,
        "directions": directions,
        "warnings": warnings,
    }


def run(registry_path=DEFAULT, cache_dir=CACHE, as_of=None):
    os.makedirs(cache_dir, exist_ok=True)
    data = collect(load_registry(registry_path), as_of=as_of)
    path = os.path.join(cache_dir, "catalysts.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path, data


def main():
    ap = argparse.ArgumentParser(description="校验催化登记表")
    ap.add_argument("--registry", default=DEFAULT)
    ap.add_argument("--cache-dir", default=CACHE)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()
    as_of = _date(args.as_of) if args.as_of else None
    path, data = run(args.registry, args.cache_dir, as_of)
    print(f"催化：{path}")
    print(f"总计 {data['total']} 条，confirmed {data['confirmed']} 条，窗口 {data['window_days']} 天")
    for w in data["warnings"][:10]:
        print(f"  提示：{w}")


if __name__ == "__main__":
    main()
