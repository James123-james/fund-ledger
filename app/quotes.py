"""净值抓取：对接天天基金（东方财富）公开接口。

两个数据源：
1. pingzhongdata/{code}.js —— 一次请求拿到全量历史净值，含日涨跌幅，首选
2. api.fund.eastmoney.com/f10/lsjz —— 分页接口（每页上限 20 条），作为增量补充

货币基金没有净值，只有「万份收益」。这里把万份收益折算成累计净值曲线，
使货币基金能和其他基金共用同一套成本与收益模型：
    nav[0] = 1
    nav[t] = nav[t-1] * (1 + 万份收益[t] / 10000)
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, date

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://fund.eastmoney.com/"}

TIMEOUT = 20
RETRY = 3


class QuoteError(Exception):
    pass


def _get(url, headers=None, retry=RETRY):
    """带重试的 GET，返回解码后的文本。"""
    last = None
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gbk", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retry - 1:
                time.sleep(0.8 * (attempt + 1))
    raise QuoteError(f"请求失败：{url}（{last}）")


def _ts_to_date(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def search(keyword, limit=12):
    """按名称或代码搜索基金。返回 [{code,name,type,company,rate,source_rate}]"""
    kw = urllib.parse.quote(keyword.strip())
    url = (
        "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        f"?callback=cb&m=1&key={kw}"
    )
    text = _get(url)
    # 去掉 JSONP 外壳：cb({...})
    m = re.search(r"cb\((.*)\)\s*$", text.strip(), re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    out = []
    for item in data.get("Datas") or []:
        if str(item.get("CATEGORYDESC") or "") != "基金":
            continue
        info = item.get("FundBaseInfo") or {}
        code = str(item.get("CODE") or info.get("FCODE") or "").strip()
        if not code or not code.isdigit() or len(code) != 6:
            continue
        name = item.get("NAME") or info.get("SHORTNAME") or code
        ftype = info.get("FTYPE") or ""
        out.append(
            {
                "code": code,
                "name": name,
                "fund_type": ftype,
                "company": info.get("JJGS") or "",
                "rate": _to_float(info.get("RATE") or info.get("FUND_RATE")),
                "source_rate": _to_float(info.get("SOURCERATE")),
                "is_money": 1 if "货币" in ftype else 0,
            }
        )
        if len(out) >= limit:
            break
    return out


def _to_float(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _parse_js_var(text, name):
    """从 pingzhongdata 的 JS 里抠出 `var NAME = <json>;`"""
    m = re.search(rf"var\s+{name}\s*=\s*(.*?);\s*(?:/\*|var\s|$)", text, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def fetch_profile(code):
    """抓取基金基础信息 + 全量净值。

    返回 dict: {code,name,fund_type,company,rate,source_rate,is_money, nav:[{date,nav,pct}]}
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    text = _get(url)
    if "fS_name" not in text:
        raise QuoteError(f"未找到基金 {code}，请确认代码")

    name = re.search(r'var\s+fS_name\s*=\s*"([^"]*)"', text)
    name = name.group(1) if name else code

    def pick(var):
        m = re.search(rf'var\s+{var}\s*=\s*"([^"]*)"', text)
        return _to_float(m.group(1)) if m else None

    source_rate = pick("fund_sourceRate")
    rate = pick("fund_Rate")

    trend = _parse_js_var(text, "Data_netWorthTrend")
    million = _parse_js_var(text, "Data_millionCopiesIncome")
    seven = _parse_js_var(text, "Data_sevenDaysYearIncome")

    rows = []
    if trend:
        # [{x: ms, y: 单位净值, equityReturn: 当日涨跌幅%, unitMoney: 分红}]
        for pt in trend:
            try:
                d = _ts_to_date(pt["x"])
                nav = round(float(pt["y"]), 4)
                pct = _to_float(pt.get("equityReturn"))
                rows.append({"date": d, "nav": nav, "acc_nav": nav, "pct": pct})
            except (KeyError, TypeError, ValueError):
                continue
        # 累计净值另存于 Data_ACWorthTrend，有则覆盖
        ac = _parse_js_var(text, "Data_ACWorthTrend")
        if ac:
            acc_map = {}
            for pt in ac:
                try:
                    acc_map[_ts_to_date(pt[0])] = round(float(pt[1]), 4)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            for r in rows:
                if r["date"] in acc_map:
                    r["acc_nav"] = acc_map[r["date"]]
        is_money = 0
    elif million:
        # 货币基金：净值恒为 1.0000，收益来自每日万份收益
        # pct 存「日收益率百分比」= 万份收益 / 100，供收益累计计算
        is_money = 1
        for pt in million:
            try:
                d = _ts_to_date(pt[0])
                mc = float(pt[1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            rows.append(
                {
                    "date": d,
                    "nav": 1.0,
                    "acc_nav": 1.0,
                    "pct": round(mc / 100.0, 6),
                }
            )
        _ = seven
    else:
        raise QuoteError(f"基金 {code} 暂无净值数据（可能未成立或已清盘）")

    if not rows:
        raise QuoteError(f"基金 {code} 未取到任何净值")

    rows.sort(key=lambda r: r["date"])
    return {
        "code": code,
        "name": name,
        "fund_type": "货币型" if is_money else "",
        "company": "",
        "rate": rate,
        "source_rate": source_rate,
        "is_money": is_money,
        "nav": rows,
    }


def fetch_latest_nav(code, pages=1):
    """增量抓取最近若干页净值，用于日常更新（比全量轻）。"""
    out = []
    headers = dict(HEADERS)
    headers["Referer"] = "https://fundf10.eastmoney.com/"
    for page in range(1, pages + 1):
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz"
            f"?fundCode={code}&pageIndex={page}&pageSize=20"
        )
        text = _get(url, headers=headers)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            break
        items = ((data.get("Data") or {}).get("LSJZList")) or []
        if not items:
            break
        for it in items:
            d = (it.get("FSRQ") or "").strip()
            if not d:
                continue
            out.append(
                {
                    "date": d,
                    "nav": _to_float(it.get("DWJZ")),
                    "acc_nav": _to_float(it.get("LJJZ")),
                    "pct": _to_float(it.get("JZZZL")),
                }
            )
    return out


def save_nav(conn, code, rows, replace=False):
    """把净值写入数据库。replace=True 时先清空该基金旧数据。"""
    if replace:
        conn.execute("DELETE FROM nav WHERE code = ?", (code,))
    conn.executemany(
        "INSERT INTO nav (code, date, nav, acc_nav, pct) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code, date) DO UPDATE SET "
        "nav = excluded.nav, acc_nav = COALESCE(excluded.acc_nav, nav.acc_nav), "
        "pct = COALESCE(excluded.pct, nav.pct)",
        [(code, r["date"], r.get("nav"), r.get("acc_nav"), r.get("pct")) for r in rows],
    )
    conn.commit()
    return len(rows)


def sync_fund(conn, code, full=False):
    """同步单只基金：首次或 full=True 走全量，否则走增量。"""
    row = conn.execute("SELECT code, is_money FROM funds WHERE code = ?", (code,)).fetchone()
    if not row:
        raise QuoteError(f"基金 {code} 不在基金库中")

    has_nav = conn.execute(
        "SELECT COUNT(*) AS c FROM nav WHERE code = ?", (code,)
    ).fetchone()["c"]

    if full or has_nav == 0 or row["is_money"]:
        prof = fetch_profile(code)
        n = save_nav(conn, code, prof["nav"], replace=full or row["is_money"] == 1)
        # 顺手补全名称与费率
        conn.execute(
            "UPDATE funds SET name = COALESCE(NULLIF(name,''), ?), "
            "rate = COALESCE(rate, ?), source_rate = COALESCE(source_rate, ?) WHERE code = ?",
            (prof["name"], prof["rate"], prof["source_rate"], code),
        )
        conn.commit()
        return {"code": code, "added": n, "mode": "full"}

    rows = fetch_latest_nav(code, pages=2)
    n = save_nav(conn, code, rows)
    return {"code": code, "added": n, "mode": "incremental"}


# ---------------------------------------------------------------- 指数基准

# 常用指数，secid 规则：1.=上交所，0.=深交所
INDEX_PRESETS = [
    {"secid": "1.000300", "name": "沪深300"},
    {"secid": "1.000905", "name": "中证500"},
    {"secid": "1.000852", "name": "中证1000"},
    {"secid": "1.000012", "name": "国债指数"},
    {"secid": "1.000922", "name": "中证红利"},
    {"secid": "0.399006", "name": "创业板指"},
    {"secid": "1.000001", "name": "上证指数"},
]


def fetch_index(secid):
    """抓取指数全量日线。返回 (name, [{date, close}])。

    用东方财富的 K 线接口，一次请求拿全部历史，不需要分页。
    fqt=1 表示前复权，指数本身无复权概念，取 1 保证与行情软件一致。
    """
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f58&fields2=f51,f53&klt=101&fqt=1"
        "&beg=0&end=20500101"
    )
    headers = dict(HEADERS)
    headers["Referer"] = "https://quote.eastmoney.com/"
    text = _get(url, headers=headers)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise QuoteError(f"指数接口返回非 JSON：{secid}")

    d = data.get("data")
    if not d or not d.get("klines"):
        raise QuoteError(f"未找到指数 {secid}，请检查 secid")

    rows = []
    for line in d["klines"]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append({"date": parts[0], "close": float(parts[1])})
        except ValueError:
            continue
    if not rows:
        raise QuoteError(f"指数 {secid} 无有效数据")
    return d.get("name") or secid, rows


def save_index(conn, secid, name, rows):
    conn.execute(
        "INSERT INTO benchmarks (secid, name, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(secid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
        (secid, name),
    )
    conn.executemany(
        "INSERT INTO benchmark_nav (secid, date, close) VALUES (?,?,?) "
        "ON CONFLICT(secid, date) DO UPDATE SET close = excluded.close",
        [(secid, r["date"], r["close"]) for r in rows],
    )
    conn.commit()
    return len(rows)


def sync_index(conn, secid, name=None):
    """同步指数。name 为空时用接口返回的名称，再退回 secid。"""
    api_name, rows = fetch_index(secid)
    final = name or (api_name if api_name and not api_name.startswith(tuple("0123456789")) else secid)
    n = save_index(conn, secid, final, rows)
    return {"secid": secid, "name": final, "rows": n}
