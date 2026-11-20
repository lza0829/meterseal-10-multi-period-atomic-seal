from __future__ import annotations
from app.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  meter_id TEXT PRIMARY KEY,
  building TEXT NOT NULL,
  unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS periods (
  period_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT,
  batch_no TEXT
);
CREATE TABLE IF NOT EXISTS estimates (
  estimate_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS actuals (
  actual_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  batch_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0,
  variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variances (
  variance_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  estimate_qty REAL,
  actual_qty REAL,
  delta REAL NOT NULL,
  fate TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 工单：一次勾选多个账期去封印的整单
CREATE TABLE IF NOT EXISTS seal_orders (
  order_id TEXT PRIMARY KEY,
  order_no TEXT NOT NULL,
  policy TEXT NOT NULL,                 -- 落单时贯彻的全局策略：rollback / keep
  status TEXT NOT NULL,                 -- sealing / sealed / failed / partial
  period_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finalized_at TEXT
);
-- 按期列表：工单内每个账期一行，状态随 attempt 联动
CREATE TABLE IF NOT EXISTS seal_order_lines (
  line_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending/sealed/failed
  reason_code TEXT,
  sealed_batch_id TEXT,
  attempt_id TEXT,
  UNIQUE(order_id, period_id)
);
-- 一次提交 = 一个 attempt；同一工单可多次 attempt（租约超时/失败后重提）
CREATE TABLE IF NOT EXISTS seal_attempts (
  attempt_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,                 -- running / succeeded / failed / rolled_back
  policy TEXT NOT NULL,
  acquired_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT,
  UNIQUE(order_id, attempt_no)
);
-- 逐期租约：装载口并发互斥的凭据，部分唯一索引保证同账期至多一个活跃租约
CREATE TABLE IF NOT EXISTS seal_leases (
  lease_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  status TEXT NOT NULL,                 -- active / released / expired
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_lease
  ON seal_leases(period_id) WHERE status = 'active';
-- 本工单插入的封印批头：与 periods 写闸切换同事务，成功才可见
CREATE TABLE IF NOT EXISTS seal_batches (
  seal_batch_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  reconcile_batch_id TEXT NOT NULL,     -- 绑定的对账批（封印证据）
  status TEXT NOT NULL DEFAULT 'sealed',
  created_at TEXT NOT NULL
);
-- 一个账期只允许有一个生效封印批头（半截批头随事务回滚，不留库）
CREATE UNIQUE INDEX IF NOT EXISTS ux_sealed_batch_period
  ON seal_batches(period_id) WHERE status = 'sealed';
-- 失败回执：独立记账事务写入，整单回滚后仍可查
CREATE TABLE IF NOT EXISTS seal_failures (
  failure_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  period_id TEXT,
  reason_code TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
-- 全链路审计（attempt/租约/批头/写闸/回滚/装载口拒绝）
CREATE TABLE IF NOT EXISTS seal_audit (
  audit_id TEXT PRIMARY KEY,
  order_id TEXT,
  attempt_id TEXT,
  period_id TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
"""


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        if n:
            return
        meters = [
            ("M-W-101", "A栋", "101", "water"),
            ("M-W-102", "A栋", "102", "water"),
            ("M-E-101", "A栋", "101", "electric"),
            ("M-W-201", "B栋", "201", "water"),
            ("M-E-201", "B栋", "201", "electric"),
            ("M-E-202", "B栋", "202", "electric"),
        ]
        conn.executemany(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) VALUES (?,?,?,?, 'active')",
            meters,
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-10','open')"
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-09','open')"
        )
        # estimates for 2026-10
        est = [
            ("E1", "M-W-101", "2026-10", 12.0, "EST-101", "2026-10-01T08:00:00"),
            ("E2", "M-W-102", "2026-10", 15.0, "EST-102", "2026-10-01T08:00:00"),
            ("E3", "M-E-101", "2026-10", 220.0, "EST-E101", "2026-10-01T08:00:00"),
            ("E4", "M-W-201", "2026-10", 18.0, "EST-201", "2026-10-01T08:00:00"),
            ("E5", "M-E-201", "2026-10", 310.0, "EST-E201", "2026-10-01T08:00:00"),
            ("E6", "M-E-202", "2026-10", 280.0, "EST-E202", "2026-10-01T08:00:00"),
        ]
        conn.executemany(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) VALUES (?,?,?,?,?,?)",
            est,
        )
        # actuals: most match; two variance
        act = [
            ("A1", "M-W-101", "2026-10", 12.0, "2026-10-28T10:00:00", "R01", "ACT-101", "2026-10-28T10:05:00"),
            ("A2", "M-W-102", "2026-10", 14.0, "2026-10-28T10:10:00", "R01", "ACT-102", "2026-10-28T10:15:00"),  # short
            ("A3", "M-E-101", "2026-10", 220.0, "2026-10-28T11:00:00", "R02", "ACT-E101", "2026-10-28T11:05:00"),
            ("A4", "M-W-201", "2026-10", 18.0, "2026-10-28T11:20:00", "R02", "ACT-201", "2026-10-28T11:25:00"),
            ("A5", "M-E-201", "2026-10", 330.0, "2026-10-28T12:00:00", "R03", "ACT-E201", "2026-10-28T12:05:00"),  # over
            ("A6", "M-E-202", "2026-10", 280.0, "2026-10-28T12:20:00", "R03", "ACT-E202", "2026-10-28T12:25:00"),
        ]
        conn.executemany(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at) VALUES (?,?,?,?,?,?,?,?)",
            act,
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('current_period','2026-10')"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('product_title','抄表簿')"
        )
        # 全局只贯彻一种封印口径：rollback=整单回滚，keep=保住已成功者
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('seal_policy','rollback')"
        )
        # 逐期租约超时秒数（测试可经 settings 覆盖）
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('seal_lease_ttl_seconds','60')"
        )
        # 界面不露英文：表类型仍存 water/electric 码，前端映射为中文
        conn.commit()
    finally:
        conn.close()
