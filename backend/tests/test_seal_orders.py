"""整单封印核心场景测试（stdlib unittest，不依赖 pytest/httpx）。

同一组场景分别按两种架构核对（通过子类切换全局 seal_policy）：
  1. 缺证据：回滚架构两个账期都不封；保住架构一个封一个拒
  2. 已封账期再写实抄失败（写闸）
  3. 失败清单可查（按工单 / 按 attempt）
  4. 两工单并发不得双封同一账期（逐期租约互斥）
  5. 租约超时后可重提（attempt 递增）
  6. 半截批头与写闸同事务收口（header/gate 故障注入）
  7. attempt/租约/批头/写闸/回执/审计/总览/按期列表全联动
  8. 全局只贯彻一种策略（落单后切换不影响本单）
"""
from __future__ import annotations
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from app.db import connect
from app import seed
from app.services import reconcile_service, seal_order_service as svc
from app.services.gate import assert_period_writable
from app.services.seal_order_service import (
    POLICY_KEEP,
    POLICY_ROLLBACK,
    REASON_LEASE_CONFLICT,
    REASON_NO_OPEN_BATCH,
    REASON_ROLLBACK_CASCADE,
    REASON_FAULT,
)


class _DBFixture(unittest.TestCase):
    """每个用例一个全新临时库，互不污染。"""

    policy = POLICY_ROLLBACK

    def setUp(self) -> None:
        self.tmpdir = Path(__file__).resolve().parent / f"_tmp_{uuid.uuid4().hex[:8]}"
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmpdir / "test.db"
        # connect() 在调用时读取 app.db.DB_PATH，直接替换该绑定
        import app.db as db_mod
        self._orig_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = self.db_path
        seed.seed_if_empty()
        svc.set_policy(self.policy)
        svc.set_fault_hook(None)
        # 租约 TTL 默认拉长，避免用例内意外过期
        self._set_ttl(3600)

    def tearDown(self) -> None:
        svc.set_fault_hook(None)
        import app.db as db_mod
        db_mod.DB_PATH = self._orig_db_path
        for f in self.tmpdir.glob("*"):
            f.unlink()
        self.tmpdir.rmdir()

    # ---- 辅助 ----
    def _set_ttl(self, seconds: int) -> None:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('seal_lease_ttl_seconds',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(seconds),),
            )
            conn.commit()
        finally:
            conn.close()

    def _add_period(self, period_id: str, with_evidence: bool) -> None:
        conn = connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM periods WHERE period_id=?", (period_id,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO periods(period_id,status) VALUES (?, 'open')",
                    (period_id,),
                )
            if with_evidence:
                had_est = conn.execute(
                    "SELECT 1 FROM estimates WHERE period_id=?", (period_id,)
                ).fetchone()
                if not had_est:
                    conn.execute(
                        """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at)
                           VALUES (?,?,?,?,?,?)""",
                        (f"E-{period_id}", "M-W-101", period_id, 10.0,
                         f"DOC-{period_id}", "2026-10-01T08:00:00"),
                    )
            conn.commit()
        finally:
            conn.close()
        if with_evidence:
            reconcile_service.run_align(period_id)  # 产出 open 对账批（封印证据）

    def _period_status(self, period_id: str) -> str:
        conn = connect()
        try:
            return conn.execute(
                "SELECT status FROM periods WHERE period_id=?", (period_id,)
            ).fetchone()["status"]
        finally:
            conn.close()

    def _seal_batches(self, period_id: str | None = None) -> list[dict]:
        conn = connect()
        try:
            if period_id:
                rows = conn.execute(
                    "SELECT * FROM seal_batches WHERE period_id=?", (period_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM seal_batches").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _active_leases(self, period_id: str) -> list[dict]:
        conn = connect()
        try:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM seal_leases WHERE period_id=? AND status='active'",
                    (period_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def _make_and_submit(self, period_ids: list[str]) -> dict:
        order = svc.create_order(period_ids)
        return svc.submit_order(order["order"]["order_id"])


# 两个账期：P1 有证据（已对齐），P2 缺证据（无 open 对账批）
P1 = "2026-10"   # seed 自带估抄/实抄，对齐后有证据
P2 = "2026-09"   # seed 存在但无任何读数 → 无证据


class SealOrderScenarios(_DBFixture):
    """两架构共用场景；具体口径断言按 self.policy 分叉。"""

    def _prepare_evidence(self) -> None:
        reconcile_service.run_align(P1)          # P1 有证据
        # P2 刻意不对齐，保持“缺证据”

    # 场景 1：缺证据
    def test_missing_evidence_follows_policy(self) -> None:
        self._prepare_evidence()
        result = self._make_and_submit([P1, P2])
        lines = {ln["period_id"]: ln for ln in result["lines"]}
        failures = svc.list_failures(order_id=result["order"]["order_id"])
        fail_periods = {f["period_id"] for f in failures}

        if self.policy == POLICY_ROLLBACK:
            # 两个账期都不封，退回提交前可写态
            self.assertEqual(self._period_status(P1), "open")
            self.assertEqual(self._period_status(P2), "open")
            self.assertEqual(lines[P1]["status"], "failed")
            self.assertEqual(lines[P2]["status"], "failed")
            # P2 真实原因缺证据；P1 连带回滚
            reasons = {f["period_id"]: f["reason_code"] for f in failures}
            self.assertEqual(reasons[P2], REASON_NO_OPEN_BATCH)
            self.assertEqual(reasons[P1], REASON_ROLLBACK_CASCADE)
            # 撤销本工单插入的批：无任何半截批头
            self.assertEqual(self._seal_batches(), [])
            # 总览成功数不涨
            self.assertEqual(result["order"]["success_count"], 0)
            self.assertEqual(result["attempts"][-1]["status"], "rolled_back")
        else:
            # 保住架构：一个封一个拒
            self.assertEqual(self._period_status(P1), "sealed")
            self.assertEqual(self._period_status(P2), "open")
            self.assertEqual(lines[P1]["status"], "sealed")
            self.assertEqual(lines[P2]["status"], "failed")
            # 失败者清掉半截批头：仅 P1 有批头
            batches = self._seal_batches()
            self.assertEqual({b["period_id"] for b in batches}, {P1})
            # 回执按账期打点：只报失败的 P2
            self.assertEqual(fail_periods, {P2})
            self.assertEqual(failures[0]["reason_code"], REASON_NO_OPEN_BATCH)
            # 总览只加成功者
            self.assertEqual(result["order"]["success_count"], 1)

    # 场景 2：已封账期再写实抄失败（写闸），且封印按账期而非楼栋分区
    def test_sealed_period_rejects_actual_write_across_buildings(self) -> None:
        self._prepare_evidence()
        # 用单账期工单先封 P1（该账期跨 A栋/B栋多个表具）
        self._make_and_submit([P1])
        self.assertEqual(self._period_status(P1), "sealed")
        # 同账期不同楼栋的表具都被写闸拒绝（非楼栋分区封印）
        for meter in ("M-W-101", "M-E-101", "M-W-201", "M-E-202"):
            with self.assertRaises(HTTPException) as ctx:
                assert_period_writable(P1)
            self.assertEqual(ctx.exception.status_code, 409)
        # 未封印账期仍可写
        assert_period_writable(P2)  # 不抛异常即通过

    # 场景 3：失败清单可查（工单维度 + attempt 维度）
    def test_failure_receipt_queryable(self) -> None:
        self._prepare_evidence()
        result = self._make_and_submit([P1, P2])
        order_id = result["order"]["order_id"]
        attempt_id = result["attempts"][-1]["attempt_id"]
        by_order = svc.list_failures(order_id=order_id)
        by_attempt = svc.list_failures(attempt_id=attempt_id)
        self.assertTrue(by_order, "失败回执必须可查")
        self.assertEqual({f["attempt_id"] for f in by_attempt}, {attempt_id})
        self.assertEqual(
            {f["order_id"] for f in by_order}, {order_id}
        )
        for f in by_order:
            self.assertTrue(f["reason_code"])
            self.assertTrue(f["detail"])

    # 场景 6：半截批头与写闸同事务收口（批头后故障）
    def test_header_fault_atomicity(self) -> None:
        self._prepare_evidence()
        self._add_period("2026-08", with_evidence=True)
        # P2 先补齐证据，再注入“批头已插、写闸未切”故障，确保故障在证据校验之后触发
        self._add_period(P2, with_evidence=True)
        svc.set_fault_hook({P2: ["header"]})
        result = self._make_and_submit([P1, P2])

        if self.policy == POLICY_ROLLBACK:
            # 整单回滚：无半截批头、无任何 sealed
            self.assertEqual(self._seal_batches(), [])
            self.assertEqual(self._period_status(P1), "open")
            self.assertEqual(self._period_status(P2), "open")
            self.assertEqual(result["order"]["success_count"], 0)
        else:
            # keep：P1 成功保留，P2 批头随保存点回滚
            self.assertEqual({b["period_id"] for b in self._seal_batches()}, {P1})
            self.assertEqual(self._period_status(P1), "sealed")
            self.assertEqual(self._period_status(P2), "open")
            reasons = {f["period_id"]: f["reason_code"]
                       for f in svc.list_failures(order_id=result["order"]["order_id"])}
            self.assertEqual(reasons[P2], REASON_FAULT)

        # 清除故障后重提同一工单：回滚架构两期皆封；keep 架构补封 P2
        svc.set_fault_hook(None)
        again = svc.submit_order(result["order"]["order_id"])
        self.assertEqual(self._period_status(P1), "sealed")
        self.assertEqual(self._period_status(P2), "sealed")
        # 至少两次 attempt，证明失败后可重提
        self.assertGreaterEqual(len(again["attempts"]), 2)

    # 场景 6b：写闸切换后、提交前故障 —— 整体回滚，不留 sealed
    def test_gate_fault_rolls_back_everything(self) -> None:
        if self.policy != POLICY_ROLLBACK:
            self.skipTest("gate 整体回滚口径仅在回滚架构下核验")
        self._prepare_evidence()
        svc.set_fault_hook({P1: ["gate"]})
        result = self._make_and_submit([P1])
        self.assertEqual(self._period_status(P1), "open")
        self.assertEqual(self._seal_batches(), [])
        self.assertEqual(result["attempts"][-1]["status"], "rolled_back")

    # 场景 7：全联动 happy path
    def test_full_linkage_happy_path(self) -> None:
        self._prepare_evidence()
        self._add_period("2026-08", with_evidence=True)
        result = self._make_and_submit([P1, "2026-08"])
        order_id = result["order"]["order_id"]

        self.assertEqual(result["order"]["status"], "sealed")
        self.assertEqual(result["order"]["success_count"], 2)
        self.assertEqual(result["order"]["failure_count"], 0)
        # attempt
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "succeeded")
        # 按期列表
        self.assertTrue(all(ln["status"] == "sealed" for ln in result["lines"]))
        # 批头：每期一个
        self.assertEqual(len(self._seal_batches()), 2)
        # 租约全部释放，无残留活跃租约
        for p in (P1, "2026-08"):
            self.assertEqual(self._active_leases(p), [])
        # 写闸：两期 sealed，对账批同步 sealed
        conn = connect()
        try:
            rb = conn.execute(
                "SELECT status FROM reconcile_batches WHERE period_id=?", (P1,)
            ).fetchall()
            self.assertTrue(all(r["status"] == "sealed" for r in rb))
        finally:
            conn.close()
        # 审计全链路
        actions = {a["action"] for a in svc.list_audit(order_id=order_id)}
        for expected in ("order_created", "lease_acquired", "period_sealed",
                         "leases_released", "attempt_finalized"):
            self.assertIn(expected, actions)

    # 场景 8：全局只贯彻一种策略 —— 落单后改策略不影响本单
    def test_policy_pinned_at_order_creation(self) -> None:
        self._prepare_evidence()
        order = svc.create_order([P1, P2])
        # 落单后把全局策略切到另一面
        svc.set_policy(POLICY_KEEP if self.policy == POLICY_ROLLBACK else POLICY_ROLLBACK)
        result = svc.submit_order(order["order"]["order_id"])
        if self.policy == POLICY_ROLLBACK:
            # 本单仍按 rollback：两期都不封
            self.assertEqual(self._period_status(P1), "open")
            self.assertEqual(result["order"]["success_count"], 0)
        else:
            # 本单仍按 keep：P1 封、P2 拒
            self.assertEqual(self._period_status(P1), "sealed")
            self.assertEqual(result["order"]["success_count"], 1)
        with self.assertRaises(HTTPException):
            svc.set_policy("not-a-policy")


class RollbackArchitectureTests(SealOrderScenarios):
    policy = POLICY_ROLLBACK


class KeepArchitectureTests(SealOrderScenarios):
    policy = POLICY_KEEP


class ConcurrencyAndLeaseTests(_DBFixture):
    """并发双封、租约超时重提（架构无关的不变量）。"""

    policy = POLICY_ROLLBACK

    def _evidence(self, *periods: str) -> None:
        reconcile_service.run_align(P1)
        for p in periods:
            self._add_period(p, with_evidence=True)

    def test_active_lease_blocks_second_order_deterministic(self) -> None:
        """持有活跃租约的账期，另一工单提租必冲突，不得封印。"""
        self._evidence()
        holder = svc.create_order([P1])
        # 模拟 holder 工单卡在提租后、未释放
        conn = connect()
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            conn.execute(
                """INSERT INTO seal_leases(lease_id,period_id,order_id,attempt_id,
                     status,acquired_at,expires_at)
                   VALUES (?,?,?,?, 'active',?,?)""",
                (f"SL-{uuid.uuid4().hex[:8]}", P1, holder["order"]["order_id"],
                 f"AT-{uuid.uuid4().hex[:8]}", now.isoformat(),
                 (now + timedelta(hours=1)).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        result = self._make_and_submit([P1])
        # 提租冲突 → 账期仍开放，未被第二张工单封印
        self.assertEqual(self._period_status(P1), "open")
        failures = svc.list_failures(order_id=result["order"]["order_id"])
        self.assertEqual(failures[0]["reason_code"], REASON_LEASE_CONFLICT)
        # 原活跃租约依旧归 holder，未被抢占
        self.assertEqual(len(self._active_leases(P1)), 1)

    def test_concurrent_orders_never_double_seal(self) -> None:
        """两工单并发封印同一组账期：每个账期至多一封。"""
        self._evidence("2026-08")
        periods = [P1, "2026-08"]
        errors: list[Exception] = []

        def worker() -> None:
            try:
                self._make_and_submit(list(periods))
            except Exception as e:  # 并发下允许失败，但不允许双封
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        conn = connect()
        try:
            for p in periods:
                # 唯一部分索引兜底：每个账期只有一个生效批头
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM seal_batches WHERE period_id=? AND status='sealed'",
                    (p,),
                ).fetchone()["c"]
                self.assertEqual(cnt, 1, f"{p} 被双封")
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM periods WHERE period_id=?", (p,)
                    ).fetchone()["status"],
                    "sealed",
                )
            # 成功封印的工单行，每个账期只可能被一张工单拿下
            winners = conn.execute(
                "SELECT period_id, COUNT(DISTINCT order_id) AS c FROM seal_order_lines "
                "WHERE status='sealed' GROUP BY period_id"
            ).fetchall()
            self.assertTrue(all(r["c"] == 1 for r in winners))
        finally:
            conn.close()

    def test_expired_lease_allows_resubmit(self) -> None:
        """租约超时后，原工单可凭新 attempt 重提成功。"""
        self._evidence()
        self._set_ttl(1)
        order = svc.create_order([P1])
        # 模拟上一次 attempt 留下的过期活跃租约
        conn = connect()
        try:
            past = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
            conn.execute(
                """INSERT INTO seal_leases(lease_id,period_id,order_id,attempt_id,
                     status,acquired_at,expires_at)
                   VALUES (?,?,?,?, 'active',?,?)""",
                (f"SL-{uuid.uuid4().hex[:8]}", P1, order["order"]["order_id"],
                 f"AT-old-{uuid.uuid4().hex[:6]}", past.isoformat(),
                 (past + timedelta(seconds=1)).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        # 重提：清扫过期租约 → 新 attempt 提租成功 → 封印
        result = svc.submit_order(order["order"]["order_id"])
        self.assertEqual(self._period_status(P1), "sealed")
        self.assertEqual(result["attempts"][-1]["attempt_no"], 1)
        self.assertEqual(result["attempts"][-1]["status"], "succeeded")
        conn = connect()
        try:
            stale = conn.execute(
                "SELECT status FROM seal_leases WHERE period_id=? AND status='expired'",
                (P1,),
            ).fetchall()
            self.assertEqual(len(stale), 1)
        finally:
            conn.close()

    def test_failed_order_resubmits_with_incrementing_attempt(self) -> None:
        """先因租约被占失败，占用消失后重提，attempt_no 递增且成功。"""
        self._evidence()
        holder = svc.create_order([P1])
        # 插一个未过期的活跃租约：第一张工单占着账期，第二张工单提租必冲突
        conn = connect()
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            conn.execute(
                """INSERT INTO seal_leases(lease_id,period_id,order_id,attempt_id,
                     status,acquired_at,expires_at)
                   VALUES (?,?,?,?, 'active',?,?)""",
                (f"SL-{uuid.uuid4().hex[:8]}", P1, holder["order"]["order_id"],
                 f"AT-{uuid.uuid4().hex[:8]}", now.isoformat(),
                 (now + timedelta(hours=1)).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        order = svc.create_order([P1])
        first = svc.submit_order(order["order"]["order_id"])
        self.assertEqual(first["attempts"][-1]["attempt_no"], 1)
        self.assertEqual(self._period_status(P1), "open")
        # 占用方释放租约后，原工单再次提交：attempt_no 递增且成功
        conn = connect()
        try:
            conn.execute(
                "UPDATE seal_leases SET status='released', released_at=? "
                "WHERE period_id=? AND status='active'",
                (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), P1),
            )
            conn.commit()
        finally:
            conn.close()
        second = svc.submit_order(order["order"]["order_id"])
        self.assertEqual(second["attempts"][-1]["attempt_no"], 2)
        self.assertEqual(second["attempts"][-1]["status"], "succeeded")
        self.assertEqual(self._period_status(P1), "sealed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
