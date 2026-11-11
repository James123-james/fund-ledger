"""方向代理与现有持仓的相关性。

数据：
  - 方向：ETF/指数日线（新浪，AKShare）
  - 持仓：基金台账 /api/funds/<code>/detail 的净值序列

输出：
  research/data/correlation/correlation.json
  每个方向的 60/120 日收益相关性、最大相关持仓、是否 >0.7
"""

import argparse
import json
import os
import urllib.request
from datetime import date, timedelta

import pandas as pd
import yaml

from .ledger_adapter import DEFAULT_BASE, LedgerError, load_holdings

CACHE = os.path.join("research", "data", "correlation")
PROXIES = os.path.join("research", "config", "correlation_proxies.yml")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _returns(df, date_col="date", value_col="close"):
    if df is None or len(df) == 0:
        return None
    d = pd.to_datetime(df[date_col], errors="coerce")
    v = pd.to_numeric(df[value_col], errors="coerce")
    tmp = pd.DataFrame({"d": d, "v": v}).dropna().sort_values("d")
    if len(tmp) < 5:
        return None
    s = tmp.set_index("d")["v"]
    return s.pct_change().dropna()


def fetch_proxy(ak, spec):
    kind, symbol = spec.split(":", 1)
    if kind == "etf":
        prefix = "sh" if str(symbol)[0] in "56" else "sz"
        df = ak.fund_etf_hist_sina(symbol=prefix + str(symbol))
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.sort_values("date")
    if kind == "index":
        df = ak.stock_zh_index_daily(symbol=symbol)
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.sort_values("date")
    raise ValueError(f"未知代理类型：{spec}")


def fetch_fund_nav(base, code, days=400):
    end = date.today()
    start = end - timedelta(days=days)
    url = f"{base.rstrip('/')}/api/funds/{code}/detail?from={start.isoformat()}&to={end.isoformat()}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    nav = data.get("nav") or []
    if not nav:
        return None
    df = pd.DataFrame(nav)
    if "date" not in df or "nav" not in df:
        return None
    return _returns(df, "date", "nav")


def corr(proxy_ret, fund_ret, window):
    if proxy_ret is None or fund_ret is None:
        return None
    merged = pd.concat([proxy_ret.rename("p"), fund_ret.rename("f")], axis=1, join="inner").dropna()
    if len(merged) < max(20, window // 2):
        return None
    tail = merged.tail(window)
    if len(tail) < max(20, window // 2):
        return None
    return round(float(tail["p"].corr(tail["f"])), 3)


def run(base=DEFAULT_BASE, proxy_path=PROXIES, cache_dir=CACHE):
    try:
        import akshare as ak
    except ImportError:
        raise SystemExit("AKShare 未安装：python3 -m pip install -r research/requirements.txt")

    os.makedirs(cache_dir, exist_ok=True)
    proxies = _load_yaml(proxy_path).get("proxies", {})
    holdings = load_holdings(base)["holdings"]

    fund_rets = {}
    warnings = []
    for h in holdings:
        if h.get("category") == "cash":
            continue
        try:
            fund_rets[h["code"]] = fetch_fund_nav(base, h["code"])
        except Exception as exc:
            warnings.append(f"基金 {h['code']} 净值失败：{type(exc).__name__}")
            fund_rets[h["code"]] = None

    directions = {}
    for did, spec in proxies.items():
        try:
            proxy_df = fetch_proxy(ak, spec)
            proxy_ret = _returns(proxy_df)
        except Exception as exc:
            warnings.append(f"{did} 代理 {spec} 失败：{type(exc).__name__}")
            proxy_ret = None

        rows = []
        for h in holdings:
            if h.get("category") == "cash":
                continue
            fr = fund_rets.get(h["code"])
            c60 = corr(proxy_ret, fr, 60)
            c120 = corr(proxy_ret, fr, 120)
            if c60 is None and c120 is None:
                continue
            rows.append(
                {
                    "code": h["code"],
                    "name": h["name"],
                    "category": h.get("category"),
                    "corr_60d": c60,
                    "corr_120d": c120,
                }
            )
        rows.sort(key=lambda r: (r["corr_120d"] is None, -(r["corr_120d"] or -9)))
        valid120 = [r["corr_120d"] for r in rows if r["corr_120d"] is not None]
        max120 = max(valid120) if valid120 else None
        top = next((r for r in rows if r["corr_120d"] == max120), None) if max120 is not None else None
        directions[did] = {
            "proxy": spec,
            "status": "ok" if max120 is not None else "missing",
            "max_corr_120d": max120,
            "max_holding": {"code": top["code"], "name": top["name"]} if top else None,
            "overlap": bool(max120 is not None and max120 >= 0.7),
            "holdings": rows,
        }
        if proxy_ret is None:
            directions[did]["status"] = "missing"

    data = {
        "as_of": date.today().isoformat(),
        "windows": [60, 120],
        "directions": directions,
        "warnings": warnings,
    }
    path = os.path.join(cache_dir, "correlation.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path, data


def main():
    ap = argparse.ArgumentParser(description="计算方向与持仓相关性")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--proxies", default=PROXIES)
    ap.add_argument("--cache-dir", default=CACHE)
    args = ap.parse_args()
    try:
        path, data = run(args.base, args.proxies, args.cache_dir)
    except LedgerError as exc:
        raise SystemExit(f"[台账] {exc}") from exc
    print(f"相关性：{path}")
    for did, row in data["directions"].items():
        top = row.get("max_holding") or {}
        print(f"  {did:18} max120 {row.get('max_corr_120d')} ({top.get('name','-')}) overlap={row.get('overlap')}")
    for w in data["warnings"][:5]:
        print(f"  提示：{w}")


if __name__ == "__main__":
    main()
