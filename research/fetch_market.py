"""AKShare 行情抓取与指标计算（MVP）。

数据源：
  - 宽基 PE/PB：乐咕乐股（AKShare stock_index_pe_lg / pb_lg）
  - 申万行业 PE/PB：申万宏源（AKShare index_analysis_daily_sw）
  - ETF 价格：新浪（AKShare fund_etf_hist_sina）

不使用东方财富 push2，避免短时间限流。
只写 research/data/market/，不写台账。
"""

import argparse
import json
import math
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import yaml

CACHE_DIR = os.path.join("research", "data", "market")
WATCHLIST = os.path.join("research", "config", "watchlist.draft.yml")
SOURCE_MAP = os.path.join("research", "config", "source_map.draft.yml")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _dates_5y(series):
    cutoff = date.today() - timedelta(days=365 * 5)
    return [d for d in series if str(d)[:10] >= cutoff.isoformat()]


def pct_rank(values, current):
    nums = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            nums.append(f)
    if not nums or current is None:
        return None
    return round(sum(1 for v in nums if v <= current) / len(nums) * 100, 1)


def price_metrics(closes, daily=True):
    """价格分位 + 动量/波动。

    daily=True 用 20/60 个交易日、252 日回撤；
    daily=False 是月频，只给 1 个月动量、12 个月波动、12 个月回撤，避免把月数当日数。
    """
    nums = [float(x) for x in closes if pd.notna(x)]
    out = {
        "price_percentile_5y": pct_rank(nums, nums[-1] if nums else None),
        "price_period": "daily" if daily else "monthly",
        "momentum_20d": None,
        "volatility_60d": None,
        "max_drawdown_1y": None,
        "momentum_1m": None,
        "volatility_12m": None,
    }
    if daily:
        if len(nums) >= 21 and nums[-21]:
            out["momentum_20d"] = round((nums[-1] / nums[-21] - 1) * 100, 2)
        if len(nums) >= 60:
            rets = [nums[i] / nums[i - 1] - 1 for i in range(len(nums) - 60, len(nums)) if nums[i - 1]]
            if rets:
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
                out["volatility_60d"] = round(math.sqrt(var) * math.sqrt(252) * 100, 2)
        tail = nums[-252:] if len(nums) > 252 else nums
    else:
        if len(nums) >= 2 and nums[-2]:
            out["momentum_1m"] = round((nums[-1] / nums[-2] - 1) * 100, 2)
        if len(nums) >= 13:
            rets = [nums[i] / nums[i - 1] - 1 for i in range(len(nums) - 12, len(nums)) if nums[i - 1]]
            if rets:
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
                out["volatility_12m"] = round(math.sqrt(var) * math.sqrt(12) * 100, 2)
        tail = nums[-12:] if len(nums) > 12 else nums
    if tail:
        peak = tail[0]
        mdd = 0.0
        for x in tail:
            peak = max(peak, x)
            if peak:
                mdd = min(mdd, x / peak - 1)
        out["max_drawdown_1y"] = round(mdd * 100, 2)
    return out


def fetch_index_pe(ak, name):
    pe = ak.stock_index_pe_lg(symbol=name)
    pb = ak.stock_index_pb_lg(symbol=name)
    if pe is None or len(pe) == 0:
        return None
    pe = pe.copy()
    pb = pb.copy() if pb is not None else pd.DataFrame(columns=["日期", "市净率"])
    pe["日期"] = pd.to_datetime(pe["日期"], errors="coerce").dt.date
    pb["日期"] = pd.to_datetime(pb["日期"], errors="coerce").dt.date
    df = pe.merge(pb[["日期", "市净率"]], on="日期", how="left")
    df = df[df["日期"].notna()]
    df = df[df["日期"] >= (date.today() - timedelta(days=365 * 5))]
    df["滚动市盈率"] = pd.to_numeric(df["滚动市盈率"], errors="coerce")
    df["市净率"] = pd.to_numeric(df["市净率"], errors="coerce")
    df["指数"] = pd.to_numeric(df["指数"], errors="coerce")
    return df.sort_values("日期")


def sw_month_dates(ak, months=60):
    df = ak.index_analysis_week_month_sw(symbol="month")
    vals = [str(x)[:10] for x in df["date"].tolist()]
    cutoff = (date.today() - timedelta(days=365 * 5 + 31)).isoformat()
    vals = [d for d in vals if d >= cutoff]
    return vals[-months:]


def _sw_on(ak, ds, back=4):
    d = date.fromisoformat(ds)
    for i in range(back):
        cur = (d - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.index_analysis_daily_sw(symbol="一级行业", start_date=cur, end_date=cur)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            continue
    return None


def fetch_sw_history(ak, months=60):
    rows = []
    dates = sw_month_dates(ak, months)
    for i, ds in enumerate(dates, 1):
        df = _sw_on(ak, ds)
        if df is None:
            print(f"  申万 {ds} 无数据，跳过")
            continue
        for _, r in df.iterrows():
            rows.append(
                {
                    "date": ds,
                    "code": str(r["指数代码"]),
                    "name": r["指数名称"],
                    "close": pd.to_numeric(r["收盘指数"], errors="coerce"),
                    "pe": pd.to_numeric(r["市盈率"], errors="coerce"),
                    "pb": pd.to_numeric(r["市净率"], errors="coerce"),
                    "dividend_yield": pd.to_numeric(r["股息率"], errors="coerce"),
                }
            )
        if i % 10 == 0:
            print(f"  申万月度 {i}/{len(dates)}")
        time.sleep(0.12)
    return pd.DataFrame(rows)


def fetch_sw_current(ak, back=10):
    for i in range(back):
        d = date.today() - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.index_analysis_daily_sw(symbol="一级行业", start_date=ds, end_date=ds)
            if df is not None and len(df) > 0:
                df = df.copy()
                df["date"] = d.isoformat()
                return df
        except Exception:
            continue
    return None


def fetch_etf(ak, code):
    prefix = "sh" if str(code)[0] in "56" else "sz"
    df = ak.fund_etf_hist_sina(symbol=prefix + str(code))
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df[df["date"] >= (date.today() - timedelta(days=365 * 5))]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("date")


def fetch_index_price(ak, symbol):
    """新浪指数日线；用于没有 ETF、但有指数代码的方向。"""
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df[df["date"] >= (date.today() - timedelta(days=365 * 5))]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("date")


def _round(v, n=2):
    try:
        f = float(v)
        return round(f, n) if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def build_metrics(ak, watchlist, source_map, cache_dir):
    mappings = source_map.get("mappings", {})
    directions = {}
    warnings = []

    names = {d["id"]: d.get("name", d["id"]) for d in watchlist.get("directions", [])}
    index_names = sorted(
        {cfg["index_name"] for cfg in mappings.values() if cfg.get("valuation") == "index_pe" and cfg.get("index_name")}
    )
    index_data = {}
    for name in index_names:
        print(f"  宽基估值：{name}")
        last_exc = None
        for attempt in range(3):
            try:
                index_data[name] = fetch_index_pe(ak, name)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if last_exc is not None:
            warnings.append(f"{name} 估值失败（重试 3 次）：{type(last_exc).__name__}")

    etf_codes = sorted(
        {cfg["etf"] for cfg in mappings.values() if cfg.get("valuation") == "price" and cfg.get("etf")}
    )
    etf_data = {}
    for code in etf_codes:
        print(f"  ETF 价格：{code}")
        try:
            etf_data[code] = fetch_etf(ak, code)
        except Exception as exc:
            warnings.append(f"ETF {code} 失败：{type(exc).__name__}")

    index_price_symbols = sorted(
        {cfg["index_symbol"] for cfg in mappings.values() if cfg.get("valuation") == "price" and cfg.get("index_symbol")}
    )
    index_price_data = {}
    for symbol in index_price_symbols:
        print(f"  指数价格：{symbol}")
        try:
            index_price_data[symbol] = fetch_index_price(ak, symbol)
        except Exception as exc:
            warnings.append(f"指数 {symbol} 失败：{type(exc).__name__}")

    print("  申万行业：当前快照")
    sw_current = fetch_sw_current(ak)
    print("  申万行业：5 年月度历史")
    sw_history = fetch_sw_history(ak, months=60)
    if sw_current is not None:
        sw_current.to_csv(os.path.join(cache_dir, "sw_current.csv"), index=False)
    sw_history.to_csv(os.path.join(cache_dir, "sw_history.csv"), index=False)

    # 名称到代码：用当前快照
    name_to_code = {}
    if sw_current is not None:
        for _, r in sw_current.iterrows():
            name_to_code[str(r["指数名称"])] = str(r["指数代码"])

    for direction in watchlist.get("directions", []):
        if not direction.get("enabled", True):
            continue
        did = direction.get("id")
        cfg = mappings.get(did, {})
        mode = cfg.get("valuation", "pending")
        result = {
            "name": names.get(did, did),
            "valuation_mode": mode,
            "proxy": bool(cfg.get("proxy")),
            "note": cfg.get("note", ""),
            "status": "missing",
            "source": "",
            "as_of": "",
            "metrics": {},
        }

        if mode == "index_pe":
            df = index_data.get(cfg.get("index_name", ""))
            if df is not None and len(df):
                row = df.iloc[-1]
                hist = df
                price = price_metrics(hist["指数"].tolist(), daily=False)
                result.update(
                    {
                        "status": "full",
                        "source": "legulegu",
                        "as_of": str(row["日期"]),
                        "metrics": {
                            "pe": _round(row["滚动市盈率"]),
                            "pb": _round(row["市净率"]),
                            "pe_percentile_5y": pct_rank(hist["滚动市盈率"].tolist(), _round(row["滚动市盈率"])),
                            "pb_percentile_5y": pct_rank(hist["市净率"].tolist(), _round(row["市净率"])),
                            **price,
                            "history_points": len(hist),
                            "history_start": str(hist.iloc[0]["日期"]),
                        },
                    }
                )
            else:
                warnings.append(f"{names.get(did, did)} 宽基估值缺失")

        elif mode == "sw_proxy":
            industry = cfg.get("sw_industry", "")
            code = name_to_code.get(industry, "")
            hist = sw_history[sw_history["code"] == code] if code else pd.DataFrame()
            cur = sw_current[sw_current["指数代码"].astype(str) == code] if (sw_current is not None and code) else pd.DataFrame()
            if len(hist) and len(cur):
                row = cur.iloc[-1]
                price = price_metrics(hist["close"].tolist(), daily=False)
                result.update(
                    {
                        "status": "full" if len(hist) >= 36 else "partial",
                        "source": "sws",
                        "as_of": str(row["date"]),
                        "metrics": {
                            "pe": _round(row["市盈率"]),
                            "pb": _round(row["市净率"]),
                            "pe_percentile_5y": pct_rank(hist["pe"].tolist(), _round(row["市盈率"])),
                            "pb_percentile_5y": pct_rank(hist["pb"].tolist(), _round(row["市净率"])),
                            **price,
                            "history_points": len(hist),
                            "history_start": str(hist.iloc[0]["date"]),
                        },
                    }
                )
                if cfg.get("proxy"):
                    warnings.append(f"{names.get(did, did)} 使用「{industry}」行业代理")
            else:
                warnings.append(f"{names.get(did, did)} 申万行业「{industry}」历史或快照缺失")

        elif mode == "price":
            code = cfg.get("etf", "")
            symbol = cfg.get("index_symbol", "")
            df = etf_data.get(code) if code else index_price_data.get(symbol)
            source_name = "sina_etf" if code else "sina_index"
            if df is not None and len(df):
                row = df.iloc[-1]
                result.update(
                    {
                        "status": "price_only",
                        "source": source_name,
                        "as_of": str(row["date"]),
                        "metrics": {
                            **price_metrics(df["close"].tolist(), daily=True),
                            "history_points": len(df),
                            "history_start": str(df.iloc[0]["date"]),
                        },
                    }
                )
                warnings.append(f"{names.get(did, did)} 只有价格分位，没有 PE/PB")
            else:
                warnings.append(f"{names.get(did, did)} 缺 ETF/指数价格代理")

        directions[did] = result

    # 保存宽基与 ETF 原始数据
    for name, df in index_data.items():
        if df is not None:
            df.to_csv(os.path.join(cache_dir, f"index_{name}.csv"), index=False)
    for code, df in etf_data.items():
        if df is not None:
            df.to_csv(os.path.join(cache_dir, f"etf_{code}.csv"), index=False)
    for symbol, df in index_price_data.items():
        if df is not None:
            df.to_csv(os.path.join(cache_dir, f"indexprice_{symbol}.csv"), index=False)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": date.today().isoformat(),
        "sources": {
            "broad_valuation": "legulegu via akshare",
            "industry_valuation": "sws via akshare",
            "price": "sina via akshare",
        },
        "directions": directions,
        "warnings": warnings,
    }


def fetch_all(months=60, cache_dir=CACHE_DIR, watchlist=WATCHLIST, source_map=SOURCE_MAP):
    try:
        import akshare as ak
    except ImportError:
        raise SystemExit("AKShare 未安装：python3 -m pip install -r research/requirements.txt")
    os.makedirs(cache_dir, exist_ok=True)
    watch = _load_yaml(watchlist)
    smap = _load_yaml(source_map)
    metrics = build_metrics(ak, watch, smap, cache_dir)
    path = os.path.join(cache_dir, "metrics.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path, metrics


def main():
    ap = argparse.ArgumentParser(description="抓取 AKShare 行情并计算指标")
    ap.add_argument("--months", type=int, default=60, help="申万月度历史月数")
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--watchlist", default=WATCHLIST)
    ap.add_argument("--source-map", default=SOURCE_MAP)
    args = ap.parse_args()

    path, metrics = fetch_all(args.months, args.cache_dir, args.watchlist, args.source_map)
    ok = sum(1 for d in metrics["directions"].values() if d["status"] == "full")
    price = sum(1 for d in metrics["directions"].values() if d["status"] == "price_only")
    print(f"指标：{path}")
    print(f"方向：{len(metrics['directions'])} 个，full {ok}，price_only {price}")
    for w in metrics["warnings"][:10]:
        print(f"  提示：{w}")


if __name__ == "__main__":
    main()
