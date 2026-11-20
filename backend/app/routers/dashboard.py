from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import get_setting

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    return {"ok": True, "product": "抄表簿"}


@router.get("/overview")
def overview():
    conn = connect()
    try:
        open_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='open'"
        ).fetchone()["c"]
        sealed_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='sealed'"
        ).fetchone()["c"]
        meters = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE fate='pending'"
        ).fetchone()["c"]
        current = get_setting("current_period", "")
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        # 工单总览：成功封印数仍以 periods.status 派生为准（整单回滚不涨、keep 只涨成功者）
        order_stats = conn.execute(
            """SELECT
                 COUNT(*) AS order_total,
                 COALESCE(SUM(success_count),0) AS sealed_line_total,
                 COALESCE(SUM(failure_count),0) AS failed_line_total
               FROM seal_orders"""
        ).fetchone()
        recent_orders = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM seal_orders ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        return {
            "current_period": current,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "pending_variances": pending,
            "recent_batches": batches,
            "seal_order_total": order_stats["order_total"],
            "sealed_line_total": order_stats["sealed_line_total"],
            "failed_line_total": order_stats["failed_line_total"],
            "recent_seal_orders": recent_orders,
        }
    finally:
        conn.close()
