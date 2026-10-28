"""基金台账 · 本地服务

零第三方依赖，只用 Python 标准库。启动后浏览器访问即可，数据存在 data/fund.db。

用法：
    python server.py                    # 默认 127.0.0.1:8787，自动打开浏览器
    python server.py --port 9000
    python server.py --host 0.0.0.0     # 允许同一 WiFi 下的手机访问
"""

import argparse
import json
import mimetypes
import os
import re
import socket
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import analytics, db, engine, exporter, quotes  # noqa: E402

WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "fund.db")

OPEN_BROWSER = True


# ---------------------------------------------------------------- 工具


def today_str():
    return date.today().strftime("%Y-%m-%d")


def parse_date(v, default=None):
    if not v:
        return default
    v = str(v).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{v}")
    datetime.strptime(v, "%Y-%m-%d")
    return v


def to_num(v, default=0.0):
    if v is None or v == "":
        return default
    return float(v)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- 业务


def normalize_txn(payload, conn):
    """校验并补全一条操作记录。"""
    code = str(payload.get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ApiError("基金代码应为 6 位数字")
    fund = conn.execute("SELECT * FROM funds WHERE code = ?", (code,)).fetchone()
    if not fund:
        raise ApiError(f"基金 {code} 不在基金库中，请先添加")

    d = parse_date(payload.get("date"))
    if not d:
        raise ApiError("请填写操作日期")

    action = str(payload.get("action") or "").strip()
    if action not in db.ACTIONS:
        raise ApiError(f"未知操作类型：{action}")

    amount = to_num(payload.get("amount"))
    shares = to_num(payload.get("shares"))
    fee = to_num(payload.get("fee"))
    nav = payload.get("nav")
    nav = float(nav) if nav not in (None, "") else None
    note = (payload.get("note") or "").strip()[:200]

    if amount < 0 or shares < 0 or fee < 0:
        raise ApiError("金额、份额、手续费不能为负数")

    # 成交净值没填就取当日（或之前最近一个）净值
    if nav is None:
        row = conn.execute(
            "SELECT nav FROM nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (code, d),
        ).fetchone()
        nav = float(row["nav"]) if row else None

    if action in db.BUY_ACTIONS:
        if shares <= 0 and amount > 0 and nav:
            rate = float(fund["rate"] or 0) / 100.0
            net = amount / (1 + rate) if rate > 0 else amount
            if fee == 0:
                fee = round(amount - net, 2)
            else:
                net = amount - fee
            shares = round(net / nav, 4)
        if shares <= 0:
            raise ApiError("买入需填写金额与份额（或至少填写金额，并确保当日有净值）")
        if amount <= 0 and nav and shares > 0:
            amount = round(shares * nav, 2)
    elif action in db.SELL_ACTIONS:
        if shares <= 0:
            if action == "close":
                state = engine.replay(
                    engine.load_txns(conn, code=code, date_to=d)
                )
                shares = round(state["shares"], 4)
            elif amount > 0 and nav:
                shares = round(amount / nav, 4)
        if shares <= 0:
            raise ApiError("卖出需填写份额，清仓可留空自动取当前持仓")
        if amount <= 0 and nav:
            amount = round(shares * nav, 2)
    elif action == "dividend":
        if shares <= 0 and amount <= 0:
            raise ApiError("分红需填写金额（现金分红）或份额（红利再投）")

    return {
        "code": code,
        "date": d,
        "action": action,
        "amount": round(amount, 2),
        "shares": round(shares, 4),
        "nav": round(nav, 4) if nav else None,
        "fee": round(fee, 2),
        "note": note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def add_fund(conn, code, category=None):
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ApiError("基金代码应为 6 位数字")

    exist = conn.execute("SELECT code FROM funds WHERE code = ?", (code,)).fetchone()
    prof = quotes.fetch_profile(code)

    cat = category or "stock"
    if cat not in db.CATEGORY_KEYS:
        cat = "other"
    if not category:
        t = prof.get("fund_type") or ""
        if prof["is_money"]:
            cat = "cash"
        elif "债" in t:
            cat = "bond"
        elif "货币" in t:
            cat = "cash"
        elif "QDII" in t.upper() or "海外" in t:
            cat = "overseas"

    now = datetime.now().isoformat(timespec="seconds")
    if exist:
        conn.execute(
            "UPDATE funds SET name = ?, category = COALESCE(?, category) WHERE code = ?",
            (prof["name"], category, code),
        )
    else:
        conn.execute(
            "INSERT INTO funds (code, name, category, fund_type, company, source_rate, rate, is_money, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                code, prof["name"], cat, prof.get("fund_type") or "",
                prof.get("company") or "", prof.get("source_rate"),
                prof.get("rate"), prof["is_money"], now,
            ),
        )
    conn.commit()
    saved = quotes.save_nav(conn, code, prof["nav"], replace=True)
    return {"code": code, "name": prof["name"], "category": cat, "nav_rows": saved}


# ---------------------------------------------------------------- 路由处理


def handle_api(method, path, query, body, conn):
    """返回 (状态码, 响应体字节, 额外响应头)。"""

    # ---- 元信息
    if path == "/api/meta" and method == "GET":
        return 200, {
            "categories": db.CATEGORIES,
            "actions": db.ACTIONS,
            "today": today_str(),
            "version": "1.0",
        }, {}

    if path == "/api/search" and method == "GET":
        kw = (query.get("q") or [""])[0].strip()
        if len(kw) < 1:
            return 200, {"items": []}, {}
        return 200, {"items": quotes.search(kw)}, {}

    # ---- 基金库
    if path == "/api/funds" and method == "GET":
        return 200, {"items": list_funds(conn)}, {}

    if path == "/api/funds" and method == "POST":
        res = add_fund(conn, body.get("code"), body.get("category"))
        return 201, res, {}

    m = re.fullmatch(r"/api/funds/(\d{6})", path)
    if m:
        code = m.group(1)
        if method == "PATCH":
            fields, args = [], []
            if "category" in body:
                if body["category"] not in db.CATEGORY_KEYS:
                    raise ApiError("未知资产类别")
                fields.append("category = ?")
                args.append(body["category"])
            if "note" in body:
                fields.append("note = ?")
                args.append((body["note"] or "")[:200])
            if "name" in body:
                fields.append("name = ?")
                args.append((body["name"] or "").strip()[:60])
            if not fields:
                raise ApiError("没有要更新的字段")
            args.append(code)
            conn.execute(f"UPDATE funds SET {', '.join(fields)} WHERE code = ?", args)
            conn.commit()
            return 200, {"ok": True}, {}
        if method == "DELETE":
            conn.execute("DELETE FROM txns WHERE code = ?", (code,))
            conn.execute("DELETE FROM nav WHERE code = ?", (code,))
            conn.execute("DELETE FROM funds WHERE code = ?", (code,))
            conn.commit()
            return 200, {"ok": True}, {}

    m = re.fullmatch(r"/api/funds/(\d{6})/sync", path)
    if m and method == "POST":
        return 200, quotes.sync_fund(conn, m.group(1), full=bool(body.get("full"))), {}

    if path == "/api/funds/sync-all" and method == "POST":
        results, failed = [], []
        for row in conn.execute("SELECT code FROM funds").fetchall():
            try:
                results.append(quotes.sync_fund(conn, row["code"]))
            except Exception as exc:  # noqa: BLE001
                failed.append({"code": row["code"], "error": str(exc)})
        return 200, {"synced": results, "failed": failed}, {}

    # ---- 操作流水
    if path == "/api/txns" and method == "GET":
        df = parse_date((query.get("from") or [None])[0])
        dt = parse_date((query.get("to") or [None])[0])
        code = (query.get("code") or [None])[0]
        items = engine.load_txns(conn, code=code, date_from=df, date_to=dt)
        funds = engine.load_funds(conn)
        nav_cache = {}
        for t in items:
            c = t["code"]
            if c not in nav_cache:
                nav_cache[c] = {
                    r["date"]: r
                    for r in conn.execute("SELECT date, nav, pct FROM nav WHERE code = ?", (c,)).fetchall()
                }
            row = nav_cache[c].get(t["date"])
            if row is None:
                prior = [x for x in nav_cache[c] if x <= t["date"]]
                row = nav_cache[c][max(prior)] if prior else None
            is_money = bool(funds.get(c, {}).get("is_money"))
            t["day_pct"] = (
                float(row["pct"]) if row and row["pct"] is not None and not is_money else None
            )
            t["fund_name"] = funds.get(c, {}).get("name", c)
            t["category_name"] = db.CATEGORY_NAMES.get(funds.get(c, {}).get("category", "other"), "其他")
        return 200, {"items": items}, {}

    if path == "/api/txns" and method == "POST":
        rec = normalize_txn(body, conn)
        cur = conn.execute(
            "INSERT INTO txns (code, date, action, amount, shares, nav, fee, note, created_at) "
            "VALUES (:code,:date,:action,:amount,:shares,:nav,:fee,:note,:created_at)",
            rec,
        )
        conn.commit()
        return 201, {"id": cur.lastrowid, **rec}, {}

    if path == "/api/txns/batch" and method == "POST":
        return 201, create_sip_batch(conn, body), {}

    m = re.fullmatch(r"/api/txns/(\d+)", path)
    if m:
        tid = int(m.group(1))
        row = conn.execute("SELECT id FROM txns WHERE id = ?", (tid,)).fetchone()
        if not row:
            raise ApiError("记录不存在", 404)
        if method == "PUT":
            rec = normalize_txn(body, conn)
            conn.execute(
                "UPDATE txns SET code=:code, date=:date, action=:action, amount=:amount, "
                "shares=:shares, nav=:nav, fee=:fee, note=:note WHERE id=:id",
                {**rec, "id": tid},
            )
            conn.commit()
            return 200, {"id": tid, **rec}, {}
        if method == "DELETE":
            conn.execute("DELETE FROM txns WHERE id = ?", (tid,))
            conn.commit()
            return 200, {"ok": True}, {}

    # ---- 视图数据
    if path == "/api/overview" and method == "GET":
        positions = engine.build_positions(conn)
        df = (query.get("from") or [None])[0]
        dt = (query.get("to") or [None])[0]
        payload = {
            "summary": engine.summarize(positions),
            "positions": positions,
            "today": today_str(),
        }
        if df or dt:
            payload["series"] = engine.portfolio_series(
                conn, parse_date(df, "2000-01-01"), parse_date(dt, today_str())
            )
        return 200, payload, {}

    if path == "/api/series" and method == "GET":
        df = parse_date((query.get("from") or [None])[0], "2000-01-01")
        dt = parse_date((query.get("to") or [None])[0], today_str())
        return 200, engine.portfolio_series(conn, df, dt), {}

    if path == "/api/analysis" and method == "GET":
        tid = (query.get("template_id") or [None])[0]
        extra = to_num((query.get("extra_cash") or [None])[0], 0.0)
        return 200, engine.analyze_allocation(conn, tid, extra), {}

    # ---- 收益率分析
    if path == "/api/returns" and method == "GET":
        dt = parse_date((query.get("to") or [None])[0])
        return 200, analytics.irr_table(conn, dt), {}

    # ---- 模板
    if path == "/api/templates" and method == "GET":
        return 200, {"items": engine.load_templates(conn)}, {}

    if path == "/api/templates" and method == "POST":
        return 201, save_template(conn, None, body), {}

    m = re.fullmatch(r"/api/templates/(\d+)", path)
    if m:
        tid = int(m.group(1))
        if method == "PUT":
            return 200, save_template(conn, tid, body), {}
        if method == "DELETE":
            row = conn.execute("SELECT builtin FROM templates WHERE id = ?", (tid,)).fetchone()
            if not row:
                raise ApiError("模板不存在", 404)
            conn.execute("DELETE FROM template_items WHERE template_id = ?", (tid,))
            conn.execute("DELETE FROM templates WHERE id = ?", (tid,))
            conn.commit()
            return 200, {"ok": True}, {}

    # ---- 导出
    if path == "/api/export" and method == "GET":
        q = lambda k, dft=None: (query.get(k) or [dft])[0]
        fmt = q("format", "md")
        level = q("level", "standard")
        df = parse_date(q("from"), "2000-01-01")
        dt = parse_date(q("to"), today_str())
        tid = q("template_id")
        extra = to_num(q("extra_cash"), 0.0)
        name, mime, content = exporter.build(conn, fmt, df, dt, level, tid, extra)
        return 200, content.encode("utf-8"), {
            "Content-Type": mime,
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}",
        }

    if path == "/api/backup" and method == "GET":
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        with open(DB_PATH, "rb") as fh:
            raw = fh.read()
        name = f"fund-ledger-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.db"
        return 200, raw, {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename={name}",
        }

    raise ApiError("接口不存在", 404)


def list_funds(conn):
    positions = {p["code"]: p for p in engine.build_positions(conn)}
    out = []
    for row in conn.execute("SELECT * FROM funds ORDER BY code").fetchall():
        f = dict(row)
        p = positions.get(f["code"])
        f["holding"] = bool(p and not p["closed"])
        f["value"] = p["value"] if p else 0.0
        f["shares"] = p["shares"] if p else 0.0
        f["nav"] = p["nav"] if p else None
        f["pct"] = p["pct"] if p else None
        f["nav_date"] = p["nav_date"] if p else None
        f["category_name"] = db.CATEGORY_NAMES.get(f["category"], "其他")
        f["txn_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM txns WHERE code = ?", (f["code"],)
        ).fetchone()["c"]
        out.append(f)
    return out


def save_template(conn, tid, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError("请填写模板名称")
    desc = (body.get("description") or "").strip()[:300]
    try:
        threshold = float(body.get("threshold") or 5)
    except (TypeError, ValueError):
        raise ApiError("偏离阈值应为数字")
    if threshold < 0 or threshold > 100:
        raise ApiError("偏离阈值应在 0–100 之间")

    items = body.get("items") or []
    clean = []
    total = 0.0
    for it in items:
        cat = str(it.get("category") or "").strip()
        if cat not in db.CATEGORY_KEYS:
            raise ApiError(f"未知资产类别：{cat}")
        try:
            target = float(it.get("target") or 0)
        except (TypeError, ValueError):
            raise ApiError("目标比例应为数字")
        if target < 0:
            raise ApiError("目标比例不能为负数")
        if target == 0:
            continue
        clean.append((cat, target))
        total += target
    if not clean:
        raise ApiError("至少配置一个资产类别")
    if abs(total - 100) > 0.5:
        raise ApiError(f"各项比例之和应为 100%，当前合计 {total:.2f}%")

    now = datetime.now().isoformat(timespec="seconds")
    if tid is None:
        dup = conn.execute("SELECT id FROM templates WHERE name = ?", (name,)).fetchone()
        if dup:
            raise ApiError("同名模板已存在")
        cur = conn.execute(
            "INSERT INTO templates (name, description, threshold, builtin, created_at) VALUES (?,?,?,0,?)",
            (name, desc, threshold, now),
        )
        tid = cur.lastrowid
    else:
        if not conn.execute("SELECT id FROM templates WHERE id = ?", (tid,)).fetchone():
            raise ApiError("模板不存在", 404)
        conn.execute(
            "UPDATE templates SET name = ?, description = ?, threshold = ? WHERE id = ?",
            (name, desc, threshold, tid),
        )
        conn.execute("DELETE FROM template_items WHERE template_id = ?", (tid,))
    for cat, target in clean:
        conn.execute(
            "INSERT INTO template_items (template_id, category, target) VALUES (?,?,?)",
            (tid, cat, target),
        )
    conn.commit()
    return {"id": tid, "name": name}


def create_sip_batch(conn, body):
    """按周期批量生成定投记录。"""
    code = str(body.get("code") or "").strip()
    start = parse_date(body.get("start"))
    end = parse_date(body.get("end"))
    if not start or not end:
        raise ApiError("请填写定投起止日期")
    if end < start:
        raise ApiError("结束日期不能早于开始日期")
    freq = str(body.get("freq") or "month")
    amount = to_num(body.get("amount"))
    if amount <= 0:
        raise ApiError("请填写每期定投金额")
    fee_rate = to_num(body.get("fee_rate"), 0.0) / 100.0

    fund = conn.execute("SELECT * FROM funds WHERE code = ?", (code,)).fetchone()
    if not fund:
        raise ApiError("基金不在基金库中")

    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    days = int(body.get("day") or 0)

    dates = []
    cur = s
    if freq == "week":
        while cur <= e:
            if days and cur.weekday() != days:
                cur = cur.fromordinal(cur.toordinal() + 1)
                continue
            dates.append(cur)
            cur = cur.fromordinal(cur.toordinal() + 7)
    elif freq == "biweek":
        while cur <= e:
            if days and cur.weekday() != days:
                cur = cur.fromordinal(cur.toordinal() + 1)
                continue
            dates.append(cur)
            cur = cur.fromordinal(cur.toordinal() + 14)
    else:  # month
        target_day = days or s.day
        y, mo = s.year, s.month
        while True:
            import calendar

            last = calendar.monthrange(y, mo)[1]
            d = min(target_day, last)
            dt = date(y, mo, d)
            if dt > e:
                break
            if dt >= s:
                dates.append(dt)
            mo += 1
            if mo > 12:
                mo = 1
                y += 1

    if not dates:
        raise ApiError("按该周期没有落在区间内的定投日")
    if len(dates) > 500:
        raise ApiError(f"将生成 {len(dates)} 条记录，超过 500 条上限，请缩小范围")

    created, skipped = [], []
    for dt in dates:
        ds = dt.strftime("%Y-%m-%d")
        dup = conn.execute(
            "SELECT id FROM txns WHERE code = ? AND date = ? AND action = 'sip'", (code, ds)
        ).fetchone()
        if dup:
            skipped.append(ds)
            continue
        nav_row = conn.execute(
            "SELECT nav FROM nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1", (code, ds)
        ).fetchone()
        nav = float(nav_row["nav"]) if nav_row else None
        if not nav:
            skipped.append(ds)
            continue
        net = amount / (1 + fee_rate) if fee_rate > 0 else amount
        fee = round(amount - net, 2)
        shares = round(net / nav, 4)
        cur2 = conn.execute(
            "INSERT INTO txns (code, date, action, amount, shares, nav, fee, note, created_at) "
            "VALUES (?,?,'sip',?,?,?,?,?,?)",
            (code, ds, round(amount, 2), shares, round(nav, 4), fee,
             (body.get("note") or "定投").strip()[:60],
             datetime.now().isoformat(timespec="seconds")),
        )
        created.append({"id": cur2.lastrowid, "date": ds, "shares": shares, "nav": round(nav, 4)})
    conn.commit()
    return {"created": created, "skipped": skipped, "count": len(created)}


# ---------------------------------------------------------------- HTTP


class Server(ThreadingHTTPServer):
    """禁止端口复用。

    Windows 上 SO_REUSEADDR 的语义与 Linux 不同：它允许第二个进程绑到已被占用的端口，
    结果就是两个实例同时响应，请求被随机分给新旧进程，看到的数据会前后不一致。
    这里在 Windows 上改用 SO_EXCLUSIVEADDRUSE，端口被占时直接报错。
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 跳过 HTTPServer.server_bind 里的 getfqdn 反查，启动更快
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Handler(BaseHTTPRequestHandler):
    server_version = "FundLedger/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 分发
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        # 静态资源
        if method == "GET" and (path == "/" or path.startswith("/web/") or not path.startswith("/api")):
            return self._serve_static(path)

        body = {}
        if method in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    return self._send_json(400, {"error": f"请求体不是合法 JSON：{exc}"})

        conn = db.connect(DB_PATH)
        try:
            status, payload, headers = handle_api(method, path, query, body, conn)
        except ApiError as exc:
            return self._send_json(exc.status, {"error": exc.message})
        except quotes.QuoteError as exc:
            return self._send_json(502, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            return self._send_json(500, {"error": f"服务端异常：{exc}"})
        finally:
            conn.close()

        if isinstance(payload, bytes):
            self._send_raw(200, payload, headers)
        else:
            self._send_json(status, payload, headers)

    # ---- 响应
    def _send_json(self, status, payload, headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, raw, {"Content-Type": "application/json; charset=utf-8", **(headers or {})})

    def _send_raw(self, status, raw, headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel.startswith("web/"):
            rel = rel[4:]
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not os.path.abspath(target).startswith(os.path.abspath(WEB_DIR)):
            return self._send_raw(403, b"forbidden", {"Content-Type": "text/plain"})
        if not os.path.isfile(target):
            return self._send_raw(404, b"not found", {"Content-Type": "text/plain"})
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            raw = fh.read()
        self._send_raw(200, raw, {"Content-Type": ctype})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main():
    global DB_PATH, OPEN_BROWSER

    ap = argparse.ArgumentParser(description="基金台账 · 本地服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，0.0.0.0 允许局域网访问")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--data", default=None, help="数据目录，默认 ./data")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    if args.data:
        global DATA_DIR
        DATA_DIR = os.path.abspath(args.data)
    DB_PATH = os.path.join(DATA_DIR, "fund.db")
    os.makedirs(DATA_DIR, exist_ok=True)

    db.init(DB_PATH).close()
    OPEN_BROWSER = not args.no_browser

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 端口 {args.port} 无法监听：{exc}")
        print("       换个端口试试：python server.py --port 8899")
        sys.exit(1)

    print("")
    print("  基金台账已启动")
    print(f"  本机访问：{url}")
    if args.host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"  手机访问（同一 WiFi）：http://{ip}:{args.port}")
    print(f"  数据文件：{DB_PATH}")
    print("  关闭窗口或按 Ctrl+C 停止")
    print("")

    if OPEN_BROWSER:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
