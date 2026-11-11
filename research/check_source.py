"""检查 AKShare 对观察池的覆盖情况。

只做只读测试，不写台账、不生成报告。
用法（仓库根目录）：
    python3 -m research.check_source
"""

import argparse
import sys
from datetime import date, timedelta

import yaml

from .build_pack import load_watchlist


def _fmt_date(value):
    return str(value)[:10]


def check_index_pe(ak, name):
    pe = ak.stock_index_pe_lg(symbol=name)
    pb = ak.stock_index_pb_lg(symbol=name)
    if pe is None or len(pe) == 0:
        return None
    cutoff = date.today() - timedelta(days=365 * 5)
    pe_dates = [d for d in pe["日期"] if hasattr(d, "year") and d >= cutoff]
    pb_dates = [d for d in pb["日期"] if hasattr(d, "year") and d >= cutoff] if pb is not None else []
    return {
        "rows": len(pe),
        "rows_5y": len(pe_dates),
        "pb_rows_5y": len(pb_dates),
        "start": _fmt_date(pe.iloc[0]["日期"]),
        "end": _fmt_date(pe.iloc[-1]["日期"]),
        "pe": round(float(pe.iloc[-1]["滚动市盈率"]), 2),
        "pb": round(float(pb.iloc[-1]["市净率"]), 2) if pb is not None and len(pb) else None,
    }


def last_sw_snapshot(ak, max_back=10):
    """找最近有数据的交易日，返回 (date, DataFrame)。"""
    for i in range(max_back):
        d = date.today() - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.index_analysis_daily_sw(symbol="一级行业", start_date=ds, end_date=ds)
            if df is not None and len(df) > 0:
                return ds, df
        except Exception:
            continue
    return None, None


def sw_history_ok(ak, years=5):
    """抽一个 5 年前的交易日，确认申万历史快照存在。"""
    d = date.today() - timedelta(days=365 * years)
    ds = d.strftime("%Y%m%d")
    try:
        df = ak.index_analysis_daily_sw(symbol="一级行业", start_date=ds, end_date=ds)
        return (ds, len(df)) if df is not None and len(df) > 0 else (ds, 0)
    except Exception:
        return (ds, 0)


def check_etf(ak, code, years=5):
    """用新浪 ETF 历史，避开东方财富 push2 的限流。"""
    prefix = "sh" if str(code)[0] in "56" else "sz"
    df = ak.fund_etf_hist_sina(symbol=prefix + str(code))
    if df is None or len(df) == 0:
        return None
    cutoff = date.today() - timedelta(days=365 * years)
    rows = [r for r in df.itertuples(index=False) if str(getattr(r, "date", ""))[:10] >= cutoff.isoformat()]
    if not rows:
        return None
    return {
        "rows": len(df),
        "rows_5y": len(rows),
        "start": _fmt_date(df.iloc[0]["date"]),
        "end": _fmt_date(df.iloc[-1]["date"]),
        "last": round(float(df.iloc[-1]["close"]), 3),
    }


def check_index_price(ak, symbol, years=5):
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or len(df) == 0:
        return None
    cutoff = date.today() - timedelta(days=365 * years)
    rows = [r for r in df.itertuples(index=False) if str(getattr(r, "date", ""))[:10] >= cutoff.isoformat()]
    return {
        "rows": len(df),
        "rows_5y": len(rows),
        "start": _fmt_date(df.iloc[0]["date"]),
        "end": _fmt_date(df.iloc[-1]["date"]),
        "last": round(float(df.iloc[-1]["close"]), 3),
    }


def main():
    ap = argparse.ArgumentParser(description="检查 AKShare 数据覆盖")
    ap.add_argument("--watchlist", default="research/config/watchlist.draft.yml")
    ap.add_argument("--map", dest="source_map", default="research/config/source_map.draft.yml")
    args = ap.parse_args()

    try:
        import akshare as ak
    except ImportError:
        raise SystemExit("AKShare 未安装：python3 -m pip install akshare==1.20.37")

    watchlist = load_watchlist(args.watchlist)
    with open(args.source_map, "r", encoding="utf-8") as fh:
        source_map = yaml.safe_load(fh) or {}
    mappings = source_map.get("mappings", {})
    names = {d["id"]: d.get("name", d["id"]) for d in watchlist.get("directions", [])}

    sw_date, sw_df = last_sw_snapshot(ak)
    sw_hist_date, sw_hist_n = sw_history_ok(ak)
    sw_rows = {}
    if sw_df is not None:
        for _, row in sw_df.iterrows():
            sw_rows[str(row["指数名称"])] = row

    print(f"AKShare {getattr(ak, '__version__', '?')}；申万快照日期：{sw_date or '无'}")
    print(f"申万 5 年前快照：{sw_hist_date}，{sw_hist_n} 个行业")
    print("")
    header = f"{'方向':12} {'模式':10} {'5年估值':22} {'价格历史':18} 状态"
    print(header)
    print("-" * len(header))
    for direction in watchlist.get("directions", []):
        if not direction.get("enabled", True):
            continue
        did = direction.get("id")
        name = names.get(did, did)
        cfg = mappings.get(did, {})
        mode = cfg.get("valuation", "pending")
        estimate = "-"
        prices = "-"
        status = "pending"

        if mode == "index_pe":
            try:
                info = check_index_pe(ak, cfg.get("index_name", ""))
                if info:
                    estimate = f"PE {info['pe']} / PB {info['pb']} · {info['rows_5y']}期"
                    status = "ok" + ("（代理）" if cfg.get("proxy") else "")
                else:
                    status = "无数据"
            except Exception as exc:
                status = f"ERR {type(exc).__name__}"

        elif mode == "sw_proxy":
            industry = cfg.get("sw_industry", "")
            row = sw_rows.get(industry)
            if row is not None:
                estimate = f"{industry} PE {row['市盈率']} / PB {row['市净率']}"
                status = ("ok（5年历史可用，月频拉取）" if sw_hist_n else "ok（快照，5年历史待验证）") + (" 代理" if cfg.get("proxy") else "")
            else:
                status = f"申万行业「{industry}」无数据"

        elif mode == "price":
            code = cfg.get("etf", "")
            symbol = cfg.get("index_symbol", "")
            try:
                if code:
                    info = check_etf(ak, code)
                    label = code
                elif symbol:
                    info = check_index_price(ak, symbol)
                    label = symbol
                else:
                    info = None
                    label = ""
                if info:
                    prices = f"{label} {info['rows_5y']}行 · {info['last']}"
                    status = "Sina价格，无5年估值"
                else:
                    status = "无 ETF/指数价格"
            except Exception as exc:
                status = f"ERR {type(exc).__name__}"

        print(f"{name:12} {mode:10} {estimate:22} {prices:18} {status}")

    print("")
    print("说明：")
    print("- index_pe：有 5 年 PE/PB 历史（部分是指数代理）")
    print("- sw_proxy：申万行业 PE/PB 快照；5 年历史需要按月批量拉取")
    print("- price：只有 ETF 价格历史，可算价格分位，不是估值分位")
    print("- 空白/失败的方向需要换代理或补 Tushare")


if __name__ == "__main__":
    main()
