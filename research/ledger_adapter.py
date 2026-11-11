"""只读台账适配器。

只调用基金台账的 GET 接口，不写 data/fund.db，也不做模板偏离分析。
服务没启动时抛出 LedgerError，提示先启动台账。
"""

import json
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 10


class LedgerError(RuntimeError):
    """台账不可用或返回了非预期内容。"""


def _get(base, path, timeout=DEFAULT_TIMEOUT):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LedgerError(
            f"连不上台账 {url}：{exc}。先启动基金台账服务（bash start-phone.sh）。"
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"台账返回的不是 JSON：{url}") from exc


def load_overview(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT):
    return _get(base, "/api/overview", timeout)


def load_funds(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT):
    return _get(base, "/api/funds", timeout).get("items", [])


def load_txns(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT):
    return _get(base, "/api/txns", timeout).get("items", [])


def load_sip_plans(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT):
    return _get(base, "/api/sip-plans", timeout).get("items", [])


def load_holdings(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT, include_closed=False):
    """当前持仓 + 权重。

    权重按未清仓持仓的市值合计计算；不含已清仓基金。
    """
    data = load_overview(base, timeout)
    positions = data.get("positions") or []
    live = [p for p in positions if not p.get("closed")]
    total = sum(float(p.get("value") or 0) for p in live)

    rows = []
    for p in positions:
        if p.get("closed") and not include_closed:
            continue
        value = float(p.get("value") or 0)
        rows.append(
            {
                "code": p.get("code"),
                "name": p.get("name"),
                "category": p.get("category"),
                "category_name": p.get("category_name"),
                "shares": p.get("shares"),
                "cost": p.get("cost"),
                "value": p.get("value"),
                "unrealized": p.get("unrealized"),
                "realized": p.get("realized"),
                "nav": p.get("nav"),
                "nav_date": p.get("nav_date"),
                "weight": round(value / total * 100, 2) if total > 0 else 0.0,
            }
        )
    rows.sort(key=lambda r: float(r.get("value") or 0), reverse=True)
    return {"as_of": data.get("today"), "summary": data.get("summary") or {}, "holdings": rows}


def snapshot(base=DEFAULT_BASE, timeout=DEFAULT_TIMEOUT):
    """给数据包用的一份只读快照。"""
    holdings = load_holdings(base, timeout)
    funds = load_funds(base, timeout)
    txns = load_txns(base, timeout)
    plans = load_sip_plans(base, timeout)
    return {
        "as_of": holdings["as_of"],
        "summary": holdings["summary"],
        "holdings": holdings["holdings"],
        "funds": len(funds),
        "txns": len(txns),
        "sip_plans": len(plans),
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
