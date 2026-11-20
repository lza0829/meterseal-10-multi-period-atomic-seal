from __future__ import annotations
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from app.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _audit_gate_denial(period_id: str, kind: str) -> None:
    """写闸拒绝留痕：seal_audit 为可选审计表，缺失时不阻断装载主流程。"""
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seal_audit'"
        ).fetchone()
        if not exists:
            return
        conn.execute(
            """INSERT INTO seal_audit(audit_id,order_id,attempt_id,period_id,
                 action,detail,created_at)
               VALUES (?,?,?,?, 'load_denied',?,?)""",
            (f"AU-{uuid.uuid4().hex[:10]}", None, None, period_id, kind, _now()),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def assert_period_writable(period_id: str) -> None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"账期不存在: {period_id}")
        if row["status"] == "sealed":
            _audit_gate_denial(period_id, "estimate_or_actual")
            raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")
    finally:
        conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()
