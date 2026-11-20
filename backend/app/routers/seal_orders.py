"""整单封印路由：唯一封印入口（服务端一次工单处理多个账期）。

不提供“按期循环单封”的对外接口，前端禁止用循环单期调用冒充整单。
"""
from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db import connect
from app.services import seal_order_service as svc

router = APIRouter(tags=["seal-orders"])


class CreateOrderIn(BaseModel):
    period_ids: list[str] = Field(min_length=1)


class PolicyIn(BaseModel):
    policy: str


@router.post("/seal-orders")
def create_order(body: CreateOrderIn):
    return svc.create_order(body.period_ids)


@router.post("/seal-orders/{order_id}/submit")
def submit_order(order_id: str):
    return svc.submit_order(order_id)


@router.get("/seal-orders")
def list_orders():
    return svc.list_orders()


@router.get("/seal-orders/{order_id}")
def get_order(order_id: str):
    return svc.get_order(order_id)


@router.get("/seal-orders/{order_id}/failures")
def order_failures(order_id: str):
    return svc.list_failures(order_id=order_id)


@router.get("/seal-orders/{order_id}/audit")
def order_audit(order_id: str):
    return svc.list_audit(order_id=order_id)


@router.get("/seal-failures")
def all_failures(attempt_id: str | None = None):
    return svc.list_failures(attempt_id=attempt_id)


@router.get("/seal-policy")
def get_policy():
    return {"policy": svc.get_policy()}


@router.put("/seal-policy")
def put_policy(body: PolicyIn):
    svc.set_policy(body.policy)
    return {"policy": body.policy}


@router.get("/leases")
def list_leases():
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM seal_leases ORDER BY acquired_at DESC"
            ).fetchall()
        ]
    finally:
        conn.close()


@router.get("/seal-batches")
def list_seal_batches():
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM seal_batches ORDER BY created_at DESC"
            ).fetchall()
        ]
    finally:
        conn.close()
