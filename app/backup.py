"""自动备份：按日留存数据库快照，防止误删或文件损坏导致账本丢失。

为什么不能直接复制 .db 文件
--------------------------
数据库开启了 WAL 模式，最新的写入可能还在 `fund.db-wal` 里没落到主文件。
直接复制 `fund.db` 会得到一个缺少最近若干次修改的旧版本。

必须用 sqlite3 的 backup API——它会加锁并生成一致快照，
等价于 SQLite 官方的 `.backup` 命令。
"""

import os
import sqlite3
from datetime import datetime

# 保留的备份份数。按日备份，30 份约等于一个月的回滚空间
KEEP = 30

BACKUP_DIRNAME = "backups"


def backup_dir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_DIRNAME)


def _list_backups(db_path):
    d = backup_dir(db_path)
    if not os.path.isdir(d):
        return []
    names = [
        n for n in os.listdir(d)
        if n.startswith("fund-") and n.endswith(".db")
    ]
    return sorted(names)


def _prune(db_path, keep=KEEP):
    """只保留最近 keep 份。文件名带日期，字典序即时间序。"""
    names = _list_backups(db_path)
    removed = []
    for n in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(backup_dir(db_path), n))
            removed.append(n)
        except OSError:
            pass
    return removed


def make(db_path, tag=None):
    """生成一份备份。返回 (文件名, 字节数)，失败返回 (None, 错误信息)。

    同一天重复调用会覆盖当天那份（文件名只带日期），
    这样一天内多次启动不会堆积一堆近似快照。
    """
    if not os.path.isfile(db_path):
        return None, "数据库文件不存在"

    d = backup_dir(db_path)
    os.makedirs(d, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d")
    suffix = f"-{tag}" if tag else ""
    name = f"fund-{stamp}{suffix}.db"
    target = os.path.join(d, name)

    tmp = target + ".tmp"
    try:
        src = sqlite3.connect(db_path)
        try:
            # 先把 WAL 里的内容落盘，保证快照包含最新写入
            try:
                src.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, target)
    except (sqlite3.Error, OSError) as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None, f"备份失败：{exc}"

    _prune(db_path)
    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    return name, size


def ensure_daily(db_path):
    """今天还没有备份就做一份。启动时调用，幂等。

    返回 dict：{created: bool, name, size} 或 {created: False, reason}
    """
    today = datetime.now().strftime("%Y%m%d")
    existing = [n for n in _list_backups(db_path) if n.startswith(f"fund-{today}")]
    if existing:
        return {"created": False, "reason": "今日已有备份", "name": existing[-1]}

    name, info = make(db_path)
    if name is None:
        return {"created": False, "reason": info}
    return {"created": True, "name": name, "size": info}


def stats(db_path):
    """备份目录概况，供设置页展示。"""
    d = backup_dir(db_path)
    names = _list_backups(db_path)
    total = 0
    items = []
    for n in names:
        p = os.path.join(d, n)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        total += sz
        items.append({"name": n, "size": sz,
                      "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    return {
        "dir": d,
        "count": len(names),
        "total_size": total,
        "keep": KEEP,
        "items": list(reversed(items)),  # 最新的在前
    }


def restore_hint(db_path):
    """恢复说明，避免用户不知道该怎么用这些备份。"""
    return (
        f"恢复方法：关闭程序，把 {BACKUP_DIRNAME}/ 下想恢复的那份文件"
        f"复制为 fund.db 覆盖原文件，再重新启动。"
    )
