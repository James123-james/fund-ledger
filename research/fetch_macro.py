"""宏观数据抓取（MVP）。

自动：
  - 美债 10Y 名义：东方财富中美国债收益率（AKShare bond_zh_us_rate）
  - 布油：新浪外盘期货 OIL
  - 黄金：新浪外盘期货 GC
  - DR007：中国货币网回购定盘利率 FDR007（AKShare repo_rate_hist）
  - 美债 10Y TIPS：FRED DFII10（网络不稳时留空）

手动（research/config/macro_manual.yml）：
  - us10y_tips / dxy 可填最新值和日期，优先于自动值

只写 research/data/macro/，不写台账。
"""

import argparse
import io
import json
import os
import urllib.request
from datetime import date, datetime, timedelta

import pandas as pd
import yaml

CACHE_DIR = os.path.join("research", "data", "macro")
MANUAL = os.path.join("research", "config", "macro_manual.yml")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _past_value(df, date_col, value_col, days):
    if df is None or len(df) == 0:
        return None, None
    d = pd.to_datetime(df[date_col], errors="coerce")
    v = pd.to_numeric(df[value_col], errors="coerce")
    tmp = pd.DataFrame({"d": d, "v": v}).dropna().sort_values("d")
    if len(tmp) == 0:
        return None, None
    last = tmp.iloc[-1]
    target = last["d"] - pd.Timedelta(days=days)
    before = tmp[tmp["d"] <= target]
    if len(before) == 0:
        return float(last["v"]), None
    return float(last["v"]), float(before.iloc[-1]["v"])


def _change(df, date_col, value_col, days, mode):
    latest, past = _past_value(df, date_col, value_col, days)
    if latest is None or past is None:
        return None
    if mode == "pct":
        return round((latest / past - 1) * 100, 2) if past else None
    return round(latest - past, 3)


def _item(key, label, unit, df, date_col, value_col, mode, source, note=""):
    latest, _ = _past_value(df, date_col, value_col, 7)
    out = {
        "key": key,
        "label": label,
        "value": None,
        "unit": unit,
        "change_1w": None,
        "change_1m": None,
        "change_mode": mode,
        "source": source,
        "as_of": "",
        "status": "missing",
        "note": note,
    }
    if latest is not None and df is not None and len(df):
        d = pd.to_datetime(df[date_col], errors="coerce")
        v = pd.to_numeric(df[value_col], errors="coerce")
        valid = pd.DataFrame({"d": d, "v": v}).dropna().sort_values("d")
        out.update(
            {
                "value": round(latest, 3),
                "change_1w": _change(df, date_col, value_col, 7, mode),
                "change_1m": _change(df, date_col, value_col, 30, mode),
                "as_of": str(valid.iloc[-1]["d"].date()) if len(valid) else "",
                "status": "ok",
            }
        )
    return out


def fetch_fred(series_id, timeout=10):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = ["date", series_id]
    df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
    return df.dropna(subset=[series_id])


def fetch_treasury_real():
    """美国财政部每日实际收益率曲线（10Y TIPS 代理）。"""
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        "daily-treasury-rates.csv/2026/all?type=daily_treasury_real_yield_curve"
        "&field_tdr_date_value=2026&page&_format=csv"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    df = pd.read_csv(io.BytesIO(raw))
    df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y", errors="coerce").dt.date
    df["10 YR"] = pd.to_numeric(df["10 YR"], errors="coerce")
    out = df.rename(columns={"Date": "date", "10 YR": "close"})[["date", "close"]]
    return out.dropna().sort_values("date")


def fetch_macro(manual_path=MANUAL):
    manual = _load_yaml(manual_path) if os.path.exists(manual_path) else {}
    items = []
    warnings = []

    # 1) TIPS：优先美国财政部实际收益率曲线，失败再试 FRED
    tips = None
    try:
        tips = fetch_treasury_real()
        items.append(
            _item("us10y_tips", "美债实际利率 10Y", "%", tips, "date", "close", "rate", "美国财政部实际收益率曲线")
        )
    except Exception as exc:
        warnings.append(f"财政部 TIPS 未取到：{type(exc).__name__}")
        try:
            tips = fetch_fred("DFII10", timeout=10)
            items.append(_item("us10y_tips", "美债实际利率 10Y", "%", tips, "date", "DFII10", "rate", "FRED DFII10"))
        except Exception as exc2:
            warnings.append(f"TIPS 未取到：{type(exc2).__name__}；可在 macro_manual.yml 手填")
            items.append(_item("us10y_tips", "美债实际利率 10Y", "%", None, "date", "close", "rate", "美国财政部实际收益率曲线"))

    # 2) 美债 10Y 名义
    try:
        import akshare as ak

        us = ak.bond_zh_us_rate(start_date="20250101")
        items.append(
            _item(
                "us10y_nominal",
                "美债 10Y（名义）",
                "%",
                us,
                "日期",
                "美国国债收益率10年",
                "rate",
                "东方财富 via akshare",
                note="名义利率，不等于 TIPS 实际利率",
            )
        )
    except Exception as exc:
        warnings.append(f"美债名义利率未取到：{type(exc).__name__}")
        items.append(_item("us10y_nominal", "美债 10Y（名义）", "%", None, "日期", "美国国债收益率10年", "rate", "东方财富 via akshare"))

    # 3/4) 布油、黄金（新浪外盘）
    try:
        import akshare as ak

        for key, label, code in [
            ("brent", "布油", "OIL"),
            ("gold", "黄金", "GC"),
        ]:
            try:
                df = ak.futures_foreign_hist(symbol=code)
                items.append(_item(key, label, "USD", df, "date", "close", "pct", f"新浪外盘 {code}"))
            except Exception as exc:
                warnings.append(f"{label}未取到：{type(exc).__name__}")
                items.append(_item(key, label, "USD", None, "date", "close", "pct", f"新浪外盘 {code}"))
    except Exception as exc:
        warnings.append(f"新浪外盘接口不可用：{type(exc).__name__}")

    # 5) DR007（银银间回购定盘利率）
    try:
        import akshare as ak

        df = ak.repo_rate_hist(
            start_date=(date.today() - timedelta(days=31)).strftime("%Y%m%d"),
            end_date=date.today().strftime("%Y%m%d"),
        )
        items.append(_item("dr007", "DR007", "%", df, "date", "FDR007", "rate", "中国货币网 via akshare"))
    except Exception as exc:
        warnings.append(f"DR007 未取到：{type(exc).__name__}")
        items.append(_item("dr007", "DR007", "%", None, "date", "FDR007", "rate", "中国货币网 via akshare"))

    # 6) 手动覆盖/补充 TIPS、DXY
    by_key = {it["key"]: it for it in items}
    for key, label, unit in [("us10y_tips", "美债实际利率 10Y", "%"), ("dxy", "美元指数", "")]:
        cfg = manual.get(key) or {}
        value = cfg.get("value")
        if value is not None:
            as_of = str(cfg.get("as_of") or date.today().isoformat())[:10]
            age = None
            stale = False
            try:
                age = (date.today() - date.fromisoformat(as_of)).days
                stale = age > 7
            except ValueError:
                pass
            if stale:
                warnings.append(f"{label} 手动值已 {age} 天未更新，按 stale 处理")
            by_key[key] = {
                "key": key,
                "label": label,
                "value": value,
                "unit": unit,
                "change_1w": None,
                "change_1m": None,
                "change_mode": "manual",
                "source": cfg.get("source", "manual"),
                "as_of": as_of,
                "status": "manual",
                "age_days": age,
                "stale": stale,
                "note": cfg.get("note", ""),
            }
        elif key not in by_key:
            by_key[key] = {
                "key": key,
                "label": label,
                "value": None,
                "unit": unit,
                "change_1w": None,
                "change_1m": None,
                "change_mode": "manual",
                "source": "manual",
                "as_of": "",
                "status": "missing",
                "age_days": None,
                "stale": None,
                "note": "可在 macro_manual.yml 手填",
            }

    items = [by_key[k] for k in ["us10y_tips", "us10y_nominal", "brent", "gold", "dxy", "dr007"] if k in by_key]
    if any(it["status"] == "missing" for it in items):
        warnings.append("部分宏观变量缺失；本周不据此判断 Regime")
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": date.today().isoformat(),
        "items": items,
        "warnings": warnings,
    }


def fetch_all(cache_dir=CACHE_DIR, manual_path=MANUAL):
    os.makedirs(cache_dir, exist_ok=True)
    data = fetch_macro(manual_path)
    path = os.path.join(cache_dir, "macro.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path, data


def main():
    ap = argparse.ArgumentParser(description="抓取宏观变量")
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--manual", default=MANUAL)
    args = ap.parse_args()
    path, data = fetch_all(args.cache_dir, args.manual)
    print(f"宏观：{path}")
    for it in data["items"]:
        print(f"  {it['label']}: {it['value']} {it['unit']} [{it['status']}] {it['as_of']}")
    for w in data["warnings"]:
        print(f"  提示：{w}")


if __name__ == "__main__":
    main()
