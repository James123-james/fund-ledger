"""SQLite 数据层：建表、连接、内置模板种子数据。

数据全部保存在单个 .db 文件里，备份 = 复制该文件。
"""

import os
import sqlite3
from datetime import datetime

# 资产类别：模板偏离分析按此归类
CATEGORIES = [
    {"key": "stock", "name": "股票", "color": "#c8503c"},
    {"key": "bond", "name": "债券", "color": "#3d6d9e"},
    {"key": "gold", "name": "黄金/商品", "color": "#c08a2e"},
    {"key": "cash", "name": "现金", "color": "#6f8a7a"},
    {"key": "overseas", "name": "海外", "color": "#7a5fa8"},
    {"key": "other", "name": "其他", "color": "#8a938d"},
]
CATEGORY_KEYS = [c["key"] for c in CATEGORIES]
CATEGORY_NAMES = {c["key"]: c["name"] for c in CATEGORIES}

# 操作类型
ACTIONS = {
    "open": "建仓",
    "add": "加仓",
    "reduce": "减仓",
    "close": "清仓",
    "sip": "定投",
    "dividend": "分红",
}
ACTION_KEYS = list(ACTIONS.keys())
BUY_ACTIONS = {"open", "add", "sip"}
SELL_ACTIONS = {"reduce", "close"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'stock',
    fund_type   TEXT,
    company     TEXT,
    source_rate REAL,              -- 原始申购费率 %
    rate        REAL,              -- 实际申购费率 %
    is_money    INTEGER DEFAULT 0, -- 1 = 货币基金（净值由万份收益折算）
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nav (
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,         -- YYYY-MM-DD
    nav     REAL,                  -- 单位净值
    acc_nav REAL,                  -- 累计净值
    pct     REAL,                  -- 当日涨跌幅 %
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_nav_date ON nav(date);

CREATE TABLE IF NOT EXISTS txns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    date       TEXT NOT NULL,
    action     TEXT NOT NULL,
    amount     REAL NOT NULL DEFAULT 0,  -- 申购金额 / 赎回金额（不含费）
    shares     REAL NOT NULL DEFAULT 0,  -- 确认份额
    nav        REAL,                     -- 成交净值
    fee        REAL NOT NULL DEFAULT 0,  -- 手续费（申购费/赎回费）
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_code_date ON txns(code, date);
CREATE INDEX IF NOT EXISTS idx_txn_date ON txns(date);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    threshold   REAL NOT NULL DEFAULT 5.0,  -- 偏离阈值（百分点）
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    category    TEXT NOT NULL,
    target      REAL NOT NULL,      -- 目标权重 %
    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS benchmarks (
    secid      TEXT PRIMARY KEY,   -- 东方财富 secid，如 1.000300
    name       TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_nav (
    secid TEXT NOT NULL,
    date  TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (secid, date)
);
CREATE INDEX IF NOT EXISTS idx_bnav_date ON benchmark_nav(date);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# 内置模板：不写死，用户可改可删
BUILTIN_TEMPLATES = [
    {
        "name": "永久投资组合",
        "description": "Harry Browne 提出。四类资产各 25%，任一类别偏离区间即再平衡，追求任何经济环境下都不崩。",
        "threshold": 5.0,
        "items": {"stock": 25, "bond": 25, "gold": 25, "cash": 25},
    },
    {
        "name": "股债 60/40",
        "description": "最经典的两资产配置。股票 60% 求增长，债券 40% 做减震，每年或偏离超阈值时再平衡。",
        "threshold": 5.0,
        "items": {"stock": 60, "bond": 40},
    },
    {
        "name": "全天候（简化）",
        "description": "Ray Dalio 全天候的简化版，按经济四象限配置。原版含 55% 长债，此处合并为债券一档。",
        "threshold": 5.0,
        "items": {"stock": 30, "bond": 55, "gold": 15},
    },
    {
        "name": "核心卫星",
        "description": "70% 宽基指数做核心仓位稳住贝塔，30% 海外/行业基金做卫星博取超额。",
        "threshold": 5.0,
        "items": {"stock": 70, "overseas": 30},
    },
    {
        "name": "三基金组合",
        "description": "Bogleheads 极简版：本国股票 + 国际股票 + 债券，三只基金搞定全部配置。",
        "threshold": 5.0,
        "items": {"stock": 50, "overseas": 30, "bond": 20},
    },
]


def connect(db_path):
    """打开连接，启用外键与 Row 工厂。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(db_path):
    """建表并写入内置模板（幂等）。"""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    _seed_templates(conn)
    return conn


def _seed_templates(conn):
    """只在首次运行时写入内置模板；用户删掉后不会再被塞回来。"""
    row = conn.execute("SELECT v FROM settings WHERE k = 'seeded'").fetchone()
    if row:
        return
    now = datetime.now().isoformat(timespec="seconds")
    for tpl in BUILTIN_TEMPLATES:
        cur = conn.execute(
            "INSERT INTO templates (name, description, threshold, builtin, created_at) VALUES (?,?,?,?,?)",
            (tpl["name"], tpl["description"], tpl["threshold"], 1, now),
        )
        tid = cur.lastrowid
        for cat, target in tpl["items"].items():
            conn.execute(
                "INSERT INTO template_items (template_id, category, target) VALUES (?,?,?)",
                (tid, cat, target),
            )
    conn.execute("INSERT OR REPLACE INTO settings (k, v) VALUES ('seeded', ?)", (now,))
    conn.commit()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT v FROM settings WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (k, v) VALUES (?,?)", (key, str(value)))
    conn.commit()
