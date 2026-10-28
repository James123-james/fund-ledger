"""导出模块：把选定区间的记录导出成适合 AI 阅读或 Excel 处理的文件。

三种格式：
- md   完整报告，带口径说明，直接喂给 AI 就能读懂
- csv  操作流水单表，UTF-8 BOM，Excel 双击不乱码
- json 全量结构化数据，可再导入或交给程序处理

三档详略：
- brief    概况 + 持仓 + 流水
- standard 追加组合逐月表现与各基金逐月涨跌
- full     追加全部逐日净值与涨跌幅
"""

import csv
import io
import json
from collections import OrderedDict
from datetime import datetime

from . import db, engine


def _fmt(v, digits=2, dash="-"):
    if v is None or v == "":
        return dash
    if isinstance(v, str):
        return v
    return f"{v:,.{digits}f}"


def _money(v, digits=2, dash="—"):
    """金额格式化，负号放在货币符号前面：-¥1,234.56"""
    if v is None or v == "":
        return dash
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(v):,.{digits}f}"


def _pct(v, dash="-"):
    if v is None:
        return dash
    return f"{v:+.2f}%"


def _month_key(d):
    return d[:7]


def _monthly(points):
    """把逐日曲线聚合成逐月：取每月最后一个交易日，并去掉起始空月份。"""
    buckets = OrderedDict()
    for p in points:
        buckets[_month_key(p["date"])] = p
    out = list(buckets.items())
    # 去掉开头连续 value 为 0 的月份（还没建仓）
    while len(out) > 1 and out[0][1].get("value", 0) <= 0.005:
        out.pop(0)
    return out


def _collect(conn, date_from, date_to):
    """汇总导出所需的全部数据。"""
    positions = engine.build_positions(conn)
    txns = engine.load_txns(conn, date_from=date_from, date_to=date_to)
    funds = engine.load_funds(conn)
    summary = engine.summarize(positions)
    series = engine.portfolio_series(conn, date_from, date_to)

    # 每笔流水补上当日涨跌幅
    nav_cache = {}
    for t in txns:
        code = t["code"]
        if code not in nav_cache:
            nav_cache[code] = {
                r["date"]: r
                for r in conn.execute(
                    "SELECT date, nav, pct FROM nav WHERE code = ?", (code,)
                ).fetchall()
            }
        row = nav_cache[code].get(t["date"])
        if row is None:
            # 非交易日顺延到最近一个净值日
            prior = [d for d in nav_cache[code] if d <= t["date"]]
            row = nav_cache[code][max(prior)] if prior else None
        is_money = bool(funds.get(code, {}).get("is_money"))
        t["day_pct"] = (
            float(row["pct"]) if row and row["pct"] is not None and not is_money else None
        )
        t["day_nav"] = float(row["nav"]) if row and row["nav"] is not None else None
        t["fund_name"] = funds.get(code, {}).get("name", code)
        t["category_name"] = db.CATEGORY_NAMES.get(
            funds.get(code, {}).get("category", "other"), "其他"
        )

    return {
        "positions": positions,
        "txns": txns,
        "funds": funds,
        "summary": summary,
        "series": series,
        "date_from": date_from,
        "date_to": date_to,
    }


def to_markdown(conn, date_from, date_to, level="standard", template_id=None, extra_cash=0.0):
    d = _collect(conn, date_from, date_to)
    s = d["summary"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    level_name = {"brief": "精简", "standard": "标准", "full": "完整"}.get(level, level)
    L = []

    L.append("# 基金操作台账")
    L.append("")
    L.append(f"> 生成时间：{now}  ")
    L.append(f"> 统计区间：{date_from} → {date_to}  ")
    L.append(f"> 详略档位：{level_name}  ")
    involved = len({t["code"] for t in d["txns"]})
    L.append(
        f"> 流水条数：{len(d['txns'])} 条 ｜ 区间内涉及基金：{involved} 只 ｜ "
        f"当前持仓市值：{_money(s['value'])}"
    )
    L.append("")
    L.append("---")
    L.append("")

    # 0. 数据说明：让 AI 先建立正确口径
    L.append("## 0. 数据说明（阅读前必看）")
    L.append("")
    L.append("**操作类型定义**")
    L.append("")
    L.append("| 类型 | 含义 | 对持仓的影响 |")
    L.append("| --- | --- | --- |")
    L.append("| 建仓 | 首次买入某只基金 | 份额与成本同时增加 |")
    L.append("| 加仓 | 已有持仓上追加买入 | 份额与成本同时增加 |")
    L.append("| 减仓 | 部分卖出 | 份额减少，按加权均价释放成本，差额计入已实现盈亏 |")
    L.append("| 清仓 | 全部卖出 | 份额归零，剩余成本全部转已实现 |")
    L.append("| 定投 | 定期定额买入 | 与加仓同口径，单独标记便于统计定投效果 |")
    L.append("| 分红 | 现金分红或红利再投 | 现金分红计入已实现；红利再投份额增加、成本不变 |")
    L.append("")
    L.append("**金额与盈亏口径**")
    L.append("")
    L.append("- 金额：买入时是账户实际扣款总额（**已含申购费**），卖出时是赎回确认总额（未扣赎回费）")
    L.append("- 手续费：单独记录。买入费已包含在金额里，不再另计；卖出费从到账金额中扣除")
    L.append("- 成本单价：采用移动加权平均法，= 持仓总成本 ÷ 持仓份额")
    L.append("- 浮动盈亏：市值 − 持仓成本（不含已实现部分）")
    L.append("- 已实现盈亏：历次卖出/分红累计落袋的盈亏，不随当前持仓变动")
    L.append("- 当日涨跌幅：该基金在操作当日的净值增长率，正为涨、负为跌；货币基金填「-」")
    L.append("- 收益率：浮动盈亏 ÷ 持仓成本")
    L.append("")
    L.append("---")
    L.append("")

    # 1. 组合概况
    L.append("## 1. 组合概况")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 持仓市值 | {_money(s['value'])} |")
    L.append(f"| 持仓成本 | {_money(s['cost'])} |")
    L.append(f"| 浮动盈亏 | {_money(s['unrealized'])}（{_pct(s['unrealized_pct'])}） |")
    L.append(f"| 已实现盈亏 | {_money(s['realized'])} |")
    L.append(f"| 累计盈亏 | {_money(s['total_pnl'])} |")
    L.append(f"| 当日盈亏 | {_money(s['today_pnl'])}（{_pct(s['today_pct'])}） |")
    L.append(f"| 累计投入 | {_money(s['total_buy'])} |")
    L.append(f"| 累计手续费 | {_money(s['total_fee'])} |")
    L.append(f"| 持仓只数 | {s['holdings']} |")
    st = d["series"].get("stats") or {}
    if st:
        L.append(f"| 区间市值变化 | {_money(st.get('change'))}（{st.get('start_date')} {_money(st.get('start_value'))} → {st.get('end_date')} {_money(st.get('end_value'))}） |")
        L.append(f"| 区间最大回撤 | {st.get('max_drawdown')}% |")
    L.append("")

    # 2. 当前持仓
    L.append("## 2. 当前持仓")
    L.append("")
    holding = [p for p in d["positions"] if not p["closed"]]
    if holding:
        L.append("| 代码 | 名称 | 类别 | 持有份额 | 成本单价 | 最新净值 | 净值日期 | 市值 | 浮动盈亏 | 收益率 | 已实现盈亏 | 当日涨跌 |")
        L.append("| --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |")
        for p in holding:
            L.append(
                f"| {p['code']} | {p['name']} | {p['category_name']} | {_fmt(p['shares'], 2)} | "
                f"{_fmt(p['avg_cost'], 4)} | {_fmt(p['nav'], 4)} | {p['nav_date'] or '-'} | "
                f"{_fmt(p['value'])} | {_fmt(p['unrealized'])} | {_pct(p['unrealized_pct'])} | "
                f"{_fmt(p['realized'])} | {_pct(p['pct'])} |"
            )
        L.append("")
        L.append(
            f"合计：市值 {_money(sum(p['value'] for p in holding))}，"
            f"成本 {_money(sum(p['cost'] for p in holding))}，"
            f"浮动盈亏 {_money(sum(p['unrealized'] for p in holding))}"
        )
    else:
        L.append("_当前无持仓。_")
    L.append("")

    closed = [p for p in d["positions"] if p["closed"]]
    if closed:
        L.append("### 已清仓基金")
        L.append("")
        L.append("| 代码 | 名称 | 累计买入 | 累计卖出 | 已实现盈亏 | 手续费 |")
        L.append("| --- | --- | ---: | ---: | ---: | ---: |")
        for p in closed:
            L.append(
                f"| {p['code']} | {p['name']} | {_money(p['total_buy'])} | "
                f"{_money(p['total_sell'])} | {_money(p['realized'])} | {_money(p['total_fee'])} |"
            )
        L.append("")

    # 3. 收益质量
    from . import analytics as _an

    ret = _an.irr_table(conn)
    L.append("## 3. 收益质量（累计 vs 年化）")
    L.append("")
    L.append(
        "**累计收益率** = 盈亏 ÷ 投入，不反映时间；**年化 XIRR** 按每笔现金流的实际发生日折现。"
        "持有不足 90 天时年化失真（18 天亏 13% 折算成年化是 −94%），故不给数值。"
    )
    L.append("")
    rr = [r for r in ret["rows"] if (r["invested"] or 0) > 0.01]
    if rr:
        L.append("| 代码 | 名称 | 投入 | 盈亏 | 累计收益率 | 年化 XIRR | 持有天数 | 备注 |")
        L.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for r in rr:
            ann = _pct(r["annualized"]) if r["annualized"] is not None else "—"
            note = r["reason"] or ""
            L.append(
                f"| {r['code']} | {r['name']} | {_money(r['invested'])} | "
                f"{_money(r['pnl'])} | {_pct(r['cumulative'])} | {ann} | {r['days']} | {note} |"
            )
        p = ret["portfolio"]
        pann = _pct(p["annualized"]) if p["annualized"] is not None else "—"
        L.append(
            f"| **合计** | ｜ | {_money(p['invested'])} | {_money(p['pnl'])} | "
            f"{_pct(p['cumulative'])} | **{pann}** | {p['days']} | 估值日 {p['asof']} |"
        )
        L.append("")
        L.append(
            "> 组合层面的年化是把全部现金流合并后求一个内部收益率，不是各基金年化的平均。"
            "两者不可直接比较。"
        )
        L.append("")
    else:
        L.append("_暂无可计算收益率的持仓。_")
        L.append("")

    # 4. 操作流水
    L.append("## 4. 操作流水")
    L.append("")
    if d["txns"]:
        L.append("| # | 日期 | 代码 | 名称 | 操作 | 金额(元) | 份额 | 成交净值 | 手续费(元) | 当日涨跌幅 | 备注 |")
        L.append("| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for i, t in enumerate(d["txns"], 1):
            L.append(
                f"| {i} | {t['date']} | {t['code']} | {t['fund_name']} | "
                f"{db.ACTIONS.get(t['action'], t['action'])} | {_fmt(t['amount'])} | "
                f"{_fmt(t['shares'], 2)} | {_fmt(t['nav'], 4)} | {_fmt(t['fee'])} | "
                f"{_pct(t.get('day_pct'))} | {(t.get('note') or '').replace('|', '/')} |"
            )
        L.append("")
        buys = [t for t in d["txns"] if t["action"] in db.BUY_ACTIONS]
        sells = [t for t in d["txns"] if t["action"] in db.SELL_ACTIONS]
        L.append(
            f"区间内：买入 {len(buys)} 笔共 {_money(sum(t['amount'] for t in buys))}，"
            f"卖出 {len(sells)} 笔共 {_money(sum(t['amount'] - t['fee'] for t in sells))}。"
        )
    else:
        L.append("_该区间内无操作记录。_")
    L.append("")

    # 4. 净值与涨跌幅
    if level in ("standard", "full") and d["series"]["points"]:
        L.append("## 5. 每日净值与涨跌幅")
        L.append("")
        L.append("### 5.1 组合表现（按月末取点）")
        L.append("")
        L.append("| 月份 | 月末市值 | 持仓成本 | 该月最后一日盈亏 |")
        L.append("| --- | ---: | ---: | ---: |")
        for mk, p in _monthly(d["series"]["points"]):
            L.append(f"| {mk} | {_money(p['value'])} | {_money(p['cost'])} | {_money(p['pnl'])} |")
        L.append("")

        if level == "full":
            L.append("### 5.2 组合逐日明细")
            L.append("")
            L.append("| 日期 | 市值 | 成本 | 当日盈亏 | 当日涨跌幅 |")
            L.append("| --- | ---: | ---: | ---: | ---: |")
            for p in d["series"]["points"]:
                L.append(
                    f"| {p['date']} | {_money(p['value'])} | {_money(p['cost'])} | "
                    f"{_money(p['pnl'])} | {_pct(p['pct'])} |"
                )
            L.append("")

        L.append("### 5.3 各基金月度涨跌")
        L.append("")
        codes = [p["code"] for p in holding]
        if codes:
            L.append("| 月份 | " + " | ".join(f"{d['funds'][c]['name']}" for c in codes if c in d["funds"]) + " |")
            L.append("| --- | " + " | ".join("---:" for c in codes if c in d["funds"]) + " |")
            series_by_code = {}
            for c in codes:
                rows = conn.execute(
                    "SELECT date, pct FROM nav WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date ASC",
                    (c, date_from, date_to),
                ).fetchall()
                monthly_pct = OrderedDict()
                for r in rows:
                    if r["pct"] is None:
                        continue
                    k = _month_key(r["date"])
                    monthly_pct[k] = monthly_pct.get(k, 0.0) + float(r["pct"])
                series_by_code[c] = monthly_pct
            months = sorted({k for m in series_by_code.values() for k in m})
            for mk in months:
                cells = []
                for c in codes:
                    if c not in d["funds"]:
                        continue
                    v = series_by_code[c].get(mk)
                    cells.append(_pct(round(v, 2)) if v is not None else "-")
                L.append(f"| {mk} | " + " | ".join(cells) + " |")
            L.append("")
            L.append("_说明：该列为当月各交易日涨跌幅之和，用于观察月度方向，不等于精确月度收益率。_")
            L.append("")

    # 5. 配置分析
    alloc = engine.analyze_allocation(conn, template_id, extra_cash) if template_id else None
    L.append("## 6. 资产配置分析")
    L.append("")
    if alloc and alloc.get("template"):
        tpl = alloc["template"]
        L.append(
            f"参照模板：**{tpl['name']}**（偏离阈值 ±{tpl['threshold']} 个百分点）"
        )
        L.append("")
        if tpl.get("description"):
            L.append(f"> {tpl['description']}")
            L.append("")
        if alloc["extra_cash"]:
            L.append(f"> 已按「新增资金 {_money(alloc['extra_cash'])} 只买不卖」模式计算建议。")
            L.append("")
        L.append("| 类别 | 目标占比 | 实际占比 | 偏离 | 目标金额 | 实际金额 | 状态 | 建议 | 金额 |")
        L.append("| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |")
        for r in alloc["rows"]:
            L.append(
                f"| {r['category_name']} | {r['target_pct']}% | {r['actual_pct']}% | "
                f"{r['diff']:+.2f}pp | {_money(r['target_value'])} | {_money(r['actual_value'])} | "
                f"{r['status']} | {r['suggest_side']} | {_money(r['suggest_amount'])} |"
            )
        L.append("")
        if alloc["alloc_detail"]:
            L.append("**落实到具体基金的操作清单**")
            L.append("")
            for grp in alloc["alloc_detail"]:
                L.append(f"- {grp['category_name']}（{grp['side']}）")
                for it in grp["items"]:
                    if it["amount"] > 0.01:
                        L.append(f"  - {it['code']} {it['name']}：{_money(it['amount'])}")
            L.append("")
        if alloc.get("alloc_missing"):
            L.append("**待补类别（基金库中暂无对应基金）**")
            L.append("")
            for m in alloc["alloc_missing"]:
                L.append(f"- {m['category_name']}：建议{m['side']} {_money(m['amount'])}，需先添加该类别的基金")
            L.append("")
        if alloc.get("unallocated", 0) > 0.01:
            L.append(
                f"> 新增资金尚有 {_money(alloc['unallocated'])} 未分配：各类资产均已达到或超过目标比例。"
            )
            L.append("")
    else:
        L.append("未指定模板，仅列出当前实际配置。")
        L.append("")
        L.append("| 类别 | 实际占比 | 实际金额 |")
        L.append("| --- | ---: | ---: |")
        for r in (alloc["rows"] if alloc else []):
            L.append(f"| {r['category_name']} | {r['actual_pct']}% | {_money(r['actual_value'])} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("## 7. 分析建议提示")
    L.append("")
    L.append("把这份文件交给 AI 时，可以这样提问：")
    L.append("")
    L.append("1. 我在区间内的加仓/减仓时机是否合理？对照当日涨跌幅，找出「追高」和「割肉」的记录")
    L.append("2. 我的实际配置与目标模板偏离多少？现在是否该再平衡")
    L.append("3. 定投的实际成本与一次性买入相比，摊薄效果如何")
    L.append("4. 已实现盈亏与浮动盈亏的比例说明我的交易风格偏短还是偏长")
    L.append("5. 最大回撤发生在哪个阶段，当时我的操作是加仓还是减仓")
    L.append("")
    L.append("_本文件由本地基金台账工具自动生成，数据来源为天天基金公开净值接口，仅供个人复盘参考，不构成投资建议。_")
    L.append("")

    return "\n".join(L)


def to_csv(conn, date_from, date_to):
    d = _collect(conn, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(
        [
            "日期", "基金代码", "基金名称", "资产类别", "操作类型",
            "金额(元)", "份额", "成交净值", "手续费(元)",
            "当日涨跌幅(%)", "当日净值", "备注",
        ]
    )
    for t in d["txns"]:
        w.writerow(
            [
                t["date"],
                t["code"],
                t["fund_name"],
                t["category_name"],
                db.ACTIONS.get(t["action"], t["action"]),
                f"{t['amount']:.2f}",
                f"{t['shares']:.4f}",
                f"{t['nav']:.4f}" if t.get("nav") is not None else "",
                f"{t['fee']:.2f}",
                f"{t['day_pct']:.2f}" if t.get("day_pct") is not None else "",
                f"{t['day_nav']:.4f}" if t.get("day_nav") is not None else "",
                (t.get("note") or "").replace("\n", " "),
            ]
        )
    # UTF-8 BOM，Excel 双击直接正确识别中文
    return "\ufeff" + buf.getvalue()


def to_json(conn, date_from, date_to, level="standard", template_id=None, extra_cash=0.0):
    d = _collect(conn, date_from, date_to)
    payload = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date_from": date_from,
            "date_to": date_to,
            "level": level,
            "action_definitions": db.ACTIONS,
            "note": "成本口径为移动加权平均法；金额不含手续费；当日涨跌幅为该基金操作当日的净值增长率。",
        },
        "summary": d["summary"],
        "positions": d["positions"],
        "transactions": d["txns"],
        "series": {
            "stats": d["series"]["stats"],
            "points": d["series"]["points"] if level == "full" else _monthly(d["series"]["points"]),
        },
    }
    if template_id:
        payload["allocation"] = engine.analyze_allocation(conn, template_id, extra_cash)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build(conn, fmt, date_from, date_to, level="standard", template_id=None, extra_cash=0.0):
    """返回 (文件名, MIME, 文本内容)。"""
    stamp = f"{date_from}_{date_to}"
    if fmt == "md":
        return (
            f"fund-ledger_{stamp}_{level}.md",
            "text/markdown; charset=utf-8",
            to_markdown(conn, date_from, date_to, level, template_id, extra_cash),
        )
    if fmt == "csv":
        return (
            f"fund-ledger_{stamp}.csv",
            "text/csv; charset=utf-8",
            to_csv(conn, date_from, date_to),
        )
    if fmt == "json":
        return (
            f"fund-ledger_{stamp}_{level}.json",
            "application/json; charset=utf-8",
            to_json(conn, date_from, date_to, level, template_id, extra_cash),
        )
    raise ValueError(f"不支持的格式：{fmt}")
