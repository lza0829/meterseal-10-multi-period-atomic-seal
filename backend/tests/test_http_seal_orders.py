"""真实 HTTP 集成测试：uvicorn 起服务 + urllib 调用（不依赖 httpx）。

核验：
  - 整单封印唯一入口 POST /api/seal-orders + /submit 的端到端链路
  - 旧单期 POST /api/seal 已移除（404），杜绝前端循环单期冒充整单
  - 已封账期实抄装载口返回 409（写闸）
  - 失败回执 /api/seal-orders/{id}/failures 可查
  - 总览 /api/overview 的工单成功数随策略口径联动
  - 策略 GET/PUT /api/seal-policy
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import uvicorn

from app import seed
from app.main import app
from app.services import reconcile_service
from app.db import connect

import unittest


def _request(method: str, path: str, body=None, expect: int = 200):
    url = f"http://127.0.0.1:11099{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        if expect is not None and e.code != expect:
            raise AssertionError(f"{method} {path} 期望 {expect} 实得 {e.code}: {payload}")
        return e.code, payload


class HttpSealOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = Path(__file__).resolve().parent / f"_http_{uuid.uuid4().hex[:8]}"
        cls.tmpdir.mkdir(parents=True, exist_ok=True)
        import app.db as db_mod
        cls._orig = db_mod.DB_PATH
        db_mod.DB_PATH = cls.tmpdir / "http.db"
        seed.seed_if_empty()
        cls.config = uvicorn.Config(app, host="127.0.0.1", port=11099, log_level="error")
        cls.server = uvicorn.Server(cls.config)
        cls.thread = __import__("threading").Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(50):
            try:
                _request("GET", "/api/health")
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        import app.db as db_mod
        db_mod.DB_PATH = cls._orig
        for f in cls.tmpdir.glob("*"):
            f.unlink()
        cls.tmpdir.rmdir()

    def test_01_policy_default_rollback_and_switch(self) -> None:
        _, payload = _request("GET", "/api/seal-policy")
        self.assertEqual(payload["policy"], "rollback")
        _request("PUT", "/api/seal-policy", {"policy": "keep"})
        _, payload = _request("GET", "/api/seal-policy")
        self.assertEqual(payload["policy"], "keep")
        # 非法策略 400
        _request("PUT", "/api/seal-policy", {"policy": "nope"}, expect=400)
        _request("PUT", "/api/seal-policy", {"policy": "rollback"})

    def test_02_legacy_single_seal_endpoint_removed(self) -> None:
        _request("POST", "/api/seal", {"period_id": "2026-10"}, expect=404)

    def test_03_full_order_rollback_on_missing_evidence(self) -> None:
        # P1 对齐有证据，P2 无证据
        reconcile_service.run_align("2026-10")
        _, order = _request("POST", "/api/seal-orders",
                            {"period_ids": ["2026-10", "2026-09"]})
        order_id = order["order"]["order_id"]
        _, result = _request("POST", f"/api/seal-orders/{order_id}/submit")
        # 回滚架构：两期都不封
        self.assertEqual(result["order"]["success_count"], 0)
        statuses = {ln["period_id"]: ln["status"] for ln in result["lines"]}
        self.assertEqual(set(statuses.values()), {"failed"})
        # 失败回执可查
        _, failures = _request("GET", f"/api/seal-orders/{order_id}/failures")
        self.assertTrue(failures)
        self.assertIn("2026-09", {f["period_id"] for f in failures})
        # 审计可查
        _, audit = _request("GET", f"/api/seal-orders/{order_id}/audit")
        self.assertTrue(any(a["action"] == "attempt_finalized" for a in audit))

    def test_04_writable_gate_blocks_actual_on_sealed(self) -> None:
        # keep 策略下一单封存 2026-10
        _request("PUT", "/api/seal-policy", {"policy": "keep"})
        _, order = _request("POST", "/api/seal-orders", {"period_ids": ["2026-10"]})
        _, result = _request("POST", f"/api/seal-orders/{order['order']['order_id']}/submit")
        self.assertEqual(result["order"]["status"], "sealed")
        # 已封账期写实抄 → 409
        code, payload = _request(
            "POST", "/api/actuals",
            {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 99.0,
             "occurred_at": "2026-10-29T10:00:00", "doc_no": "ACT-X"},
            expect=409,
        )
        self.assertEqual(code, 409)
        self.assertIn("封印", payload["detail"])
        # 未封账期仍可写实抄
        _request(
            "POST", "/api/actuals",
            {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 5.0,
             "occurred_at": "2026-09-29T10:00:00", "doc_no": "ACT-Y"},
        )
        _request("PUT", "/api/seal-policy", {"policy": "rollback"})

    def test_05_overview_reflects_sealed_counts(self) -> None:
        _, overview = _request("GET", "/api/overview")
        # keep 架构下成功封存 1 期；总览派生 sealed_periods 与工单成功行一致
        self.assertGreaterEqual(overview["sealed_periods"], 1)
        self.assertIn("seal_order_total", overview)
        self.assertIn("recent_seal_orders", overview)
        self.assertGreaterEqual(overview["sealed_line_total"], 1)

    def test_06_concurrent_leases_visible_and_no_double_seal(self) -> None:
        _, leases = _request("GET", "/api/leases")
        self.assertIsInstance(leases, list)
        _, batches = _request("GET", "/api/seal-batches")
        # 2026-10 只有一个生效封印批头
        sealed_for_p1 = [b for b in batches if b["period_id"] == "2026-10"]
        self.assertEqual(len(sealed_for_p1), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
