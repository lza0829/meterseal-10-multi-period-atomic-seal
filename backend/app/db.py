from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 保留 sqlite3 默认的延迟事务语义（应用层既有服务依赖隐式 BEGIN + commit）；
    # busy_timeout 让并发提租在写锁上等待而非立即报 locked。
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn
