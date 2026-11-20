"""整单封印服务：一次工单勾选多个账期去封印。

联动要素（缺一不可）：
  attempt    —— 每次提交生成一个 attempt，租约超时/失败后可重提（attempt_no 递增）
  逐期租约   —— seal_leases，部分唯一索引保证同账期至多一个活跃租约（装载口互斥）
  批头       —— seal_batches，本工单为每个账期插入的封印批头
  写闸       —— periods.status 切 sealed，估抄/实抄装载口据此拒写
  失败回执   —— seal_failures，独立记账事务写入，整单回滚后仍可查
  审计       —— seal_audit，全链路留痕
  总览       —— 成功封印数由 periods.status 派生（失败不涨/只涨成功者）
  按期列表   —— seal_order_lines，每个账期一行，状态随 attempt 联动

两套架构由全局设置 seal_policy 决定，全局只贯彻一种：
  rollback —— 整单回滚：任一账期失败，本工单全部账期退回提交前可写态、撤销已插批头，
              回执只报失败清单，总览不涨，禁止半截 sealed。
  keep     —— 保住已成功：成功者保持 sealed 且继续拒写，失败者保持可写并清掉半截批头，
              回执按账期打点，总览只加成功者。

批头插入与写闸切换严格在同一事务内收口（keep 架构用 SAVEPOINT 逐期收口）。
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.db import connect
from app.services.gate import get_setting

POLICY_ROLLBACK = "rollback"
POLICY_KEEP = "keep"
_POLICIES = (POLICY_ROLLBACK, POLICY_KEEP)

# 失败原因码（回执/审计统一口径）
REASON_PERIOD_NOT_FOUND = "period_not_found"
REASON_ALREADY_SEALED = "already_sealed"
REASON_NO_OPEN_BATCH = "no_open_batch"
REASON_LEASE_CONFLICT = "lease_conflict"
REASON_ROLLBACK_CASCADE = "rollback_cascade"
REASON_FAULT = "injected_fault"
REASON_OTHER = "other"


class SealServiceError(Exception):
    """领域失败：携带面向回执的原因码。"""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return _now_dt().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def get_policy() -> str:
    p = get_setting("seal_policy", POLICY_ROLLBACK)
    if p not in _POLICIES:
        raise SealServiceError(REASON_OTHER, f"非法封印策略: {p}")
    return p


def set_policy(policy: str) -> None:
    if policy not in _POLICIES:
        raise HTTPException(400, "策略必须是 rollback 或 keep")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('seal_policy',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (policy,),
        )
        conn.commit()
    finally:
        conn.close()


def _lease_ttl_seconds(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='seal_lease_ttl_seconds'"
    ).fetchone()
    try:
        return int(row["value"]) if row else 60
    except (TypeError, ValueError):
        return 60


def _audit(conn, *, action: str, order_id=None, attempt_id=None, period_id=None, detail=None) -> None:
    conn.execute(
        """INSERT INTO seal_audit(audit_id,order_id,attempt_id,period_id,action,detail,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (_new_id("AU"), order_id, attempt_id, period_id, action, detail, _now()),
    )


# 测试用故障注入钩子：{period_id: [钩子名,...]}；钩子名 header / gate
# header = 批头插入后、写闸切换前抛错；gate = 写闸切换后、提交前抛错
_FAULT_HOOK: dict[str, list[str]] = {}


def set_fault_hook(hook: dict[str, list[str]] | None) -> None:
    _FAULT_HOOK.clear()
    if hook:
        for k, v in hook.items():
            _FAULT_HOOK[k] = list(v)


def _consume_fault(period_id: str, hook_name: str) -> None:
    hooks = _FAULT_HOOK.get(period_id)
    if hooks and hook_name in hooks:
        hooks.remove(hook_name)
        raise SealServiceError(REASON_FAULT, f"注入故障@{hook_name}:{period_id}")


def create_order(period_ids: list[str]) -> dict:
    """落单：去重后的账期清单生成一张工单（含按期行），尚未提交 attempt。"""
    if not period_ids:
        raise HTTPException(400, "工单至少包含一个账期")
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for p in period_ids:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    conn = connect()
    try:
        missing = [
            p for p in uniq
            if not conn.execute("SELECT 1 FROM periods WHERE period_id=?", (p,)).fetchone()
        ]
        if missing:
            raise HTTPException(404, f"账期不存在: {','.join(missing)}")

        policy = get_policy()
        order_id = _new_id("SO")
        now = _now()
        conn.execute(
            """INSERT INTO seal_orders(order_id,order_no,policy,status,period_count,created_at)
               VALUES (?,?,?, 'sealing', ?,?)""",
            (order_id, order_id, policy, len(uniq), now),
        )
        for p in uniq:
            conn.execute(
                """INSERT INTO seal_order_lines(line_id,order_id,period_id,status)
                   VALUES (?,?,?, 'pending')""",
                (_new_id("SOL"), order_id, p),
            )
        _audit(conn, action="order_created", order_id=order_id,
               detail=f"policy={policy};periods={','.join(uniq)}")
        conn.commit()
        return get_order(order_id)
    finally:
        conn.close()


def _sweep_expired_leases(conn, now: datetime) -> int:
    """把到期的活跃租约置 expired，使对应账期可被重新提租。"""
    cur = conn.execute(
        "UPDATE seal_leases SET status='expired', released_at=? "
        "WHERE status='active' AND expires_at<=?",
        (now.isoformat(), now.isoformat()),
    )
    return cur.rowcount


def _acquire_leases(conn, *, order_id, attempt_id, period_ids, now, ttl_seconds):
    """逐期提租。返回 (acquired, conflicts)；冲突原因 lease_conflict。"""
    acquired: list[str] = []
    conflicts: list[str] = []
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    for p in period_ids:
        try:
            conn.execute(
                """INSERT INTO seal_leases(lease_id,period_id,order_id,attempt_id,
                     status,acquired_at,expires_at)
                   VALUES (?,?,?,?, 'active',?,?)""",
                (_new_id("SL"), p, order_id, attempt_id, now.isoformat(), expires),
            )
            acquired.append(p)
            _audit(conn, action="lease_acquired", order_id=order_id,
                   attempt_id=attempt_id, period_id=p)
        except Exception:
            # 部分唯一索引 ux_active_lease 冲突：该账期正被别的工单占着
            conflicts.append(p)
            _audit(conn, action="lease_conflict", order_id=order_id,
                   attempt_id=attempt_id, period_id=p)
    return acquired, conflicts


def _seal_one_period(conn, *, period_id, order_id, attempt_id, now) -> str:
    """在当前事务内对单个账期收口：校验证据 → 插批头 → 切写闸（同事务）。

    返回封印批头 ID。任一步抛错由调用方按策略决定回滚范围。
    """
    period = conn.execute(
        "SELECT * FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not period:
        raise SealServiceError(REASON_PERIOD_NOT_FOUND, f"账期不存在: {period_id}")
    if period["status"] == "sealed":
        raise SealServiceError(REASON_ALREADY_SEALED, f"账期已封印: {period_id}")

    # 封印证据：必须存在 open 对账批（缺证据即拒封）
    batch = conn.execute(
        """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
           ORDER BY created_at DESC LIMIT 1""",
        (period_id,),
    ).fetchone()
    if not batch:
        raise SealServiceError(REASON_NO_OPEN_BATCH, f"无 open 对账批（缺证据）: {period_id}")

    seal_batch_id = _new_id("SB")
    # 1) 本工单插入封印批头
    conn.execute(
        """INSERT INTO seal_batches(seal_batch_id,period_id,order_id,attempt_id,
             reconcile_batch_id,status,created_at)
           VALUES (?,?,?,?,?, 'sealed',?)""",
        (seal_batch_id, period_id, order_id, attempt_id, batch["batch_id"], now),
    )
    # 故障点：批头已插、写闸未切 —— 用于证明同事务回滚不留半截批头
    _consume_fault(period_id, "header")

    # 2) 写闸切换 + 对账批转 sealed（与批头同一事务收口）
    conn.execute(
        "UPDATE periods SET status='sealed', sealed_at=?, batch_no=? WHERE period_id=?",
        (now, seal_batch_id, period_id),
    )
    conn.execute(
        "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?",
        (batch["batch_id"],),
    )
    # 故障点：写闸已切、事务未提交 —— 用于证明提交前崩溃整体回滚
    _consume_fault(period_id, "gate")

    conn.execute(
        "UPDATE seal_order_lines SET status='sealed', sealed_batch_id=?, attempt_id=? "
        "WHERE order_id=? AND period_id=?",
        (seal_batch_id, attempt_id, order_id, period_id),
    )
    _audit(conn, action="period_sealed", order_id=order_id,
           attempt_id=attempt_id, period_id=period_id,
           detail=f"seal_batch={seal_batch_id};reconcile_batch={batch['batch_id']}")
    return seal_batch_id


def _record_failure(conn, *, order_id, attempt_id, period_id, reason_code, detail) -> None:
    conn.execute(
        """INSERT INTO seal_failures(failure_id,order_id,attempt_id,period_id,
             reason_code,detail,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (_new_id("FL"), order_id, attempt_id, period_id, reason_code, detail, _now()),
    )
    conn.execute(
        "UPDATE seal_order_lines SET status='failed', reason_code=?, attempt_id=? "
        "WHERE order_id=? AND period_id=?",
        (reason_code, attempt_id, order_id, period_id),
    )
    _audit(conn, action="period_failed", order_id=order_id,
           attempt_id=attempt_id, period_id=period_id, detail=f"{reason_code}:{detail}")


def submit_order(order_id: str) -> dict:
    """提交工单：生成 attempt、逐期提租、按全局策略执行整单封印。

    所有需要在回滚后仍可查的信息（失败回执、审计、attempt/line 终态）都在
    独立的“记账事务”里落库，绝不与被回滚的领域事务共生死。
    """
    conn = connect()
    try:
        order = conn.execute(
            "SELECT * FROM seal_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(404, "工单不存在")
        policy = order["policy"]

        # 只处理尚未 sealed 的账期行；已 sealed 的行是历史 attempt 的成果，保留
        lines = conn.execute(
            "SELECT * FROM seal_order_lines WHERE order_id=? ORDER BY line_id",
            (order_id,),
        ).fetchall()
        targets = [r["period_id"] for r in lines if r["status"] != "sealed"]
        if not targets:
            raise HTTPException(409, "工单内账期均已封印，无可提交项")

        now = _now_dt()
        prev_no = conn.execute(
            "SELECT COALESCE(MAX(attempt_no),0) AS m FROM seal_attempts WHERE order_id=?",
            (order_id,),
        ).fetchone()["m"]
        attempt_no = prev_no + 1
        attempt_id = _new_id("AT")

        # ---- 记账事务①：建 attempt + 清过期租约 ----
        conn.execute(
            """INSERT INTO seal_attempts(attempt_id,order_id,attempt_no,status,policy,
                 started_at) VALUES (?,?,?,?, 'running',?)""",
            (attempt_id, order_id, attempt_no, policy, now.isoformat()),
        )
        swept = _sweep_expired_leases(conn, now)
        if swept:
            _audit(conn, action="leases_swept", order_id=order_id,
                   attempt_id=attempt_id, detail=f"expired={swept}")
        conn.commit()

        # ---- 租约事务②：逐期提租（装载口互斥）----
        ttl = _lease_ttl_seconds(conn)
        acquired, conflicts = _acquire_leases(
            conn, order_id=order_id, attempt_id=attempt_id,
            period_ids=targets, now=now, ttl_seconds=ttl,
        )
        conflict_failures = [
            (p, REASON_LEASE_CONFLICT, f"账期正被其他工单占用: {p}") for p in conflicts
        ]
        conn.commit()

        domain_failures: list[tuple] = []  # (period_id, reason, detail)
        sealed_now: list[str] = []

        # 回滚架构下，租约冲突同样属于“任一账期失败”：已提到的租约对应账期也不得封印，
        # 整单退回，成功者数为 0。keep 架构下冲突仅该期失败，其余照常。
        if policy == POLICY_ROLLBACK and conflict_failures and acquired:
            for p in acquired:
                domain_failures.append(
                    (p, REASON_ROLLBACK_CASCADE,
                     "他期租约冲突，整单回滚连带退回: " + p)
                )
            acquired = []

        if acquired:
            # ---- 领域事务③：批头 + 写闸同事务收口 ----
            if policy == POLICY_ROLLBACK:
                # 任一账期失败：整单一律回滚。真正失败的期记其原因，
                # 其余已提租期记 rollback_cascade（连带退回，本身无过错）。
                failing: tuple | None = None
                try:
                    for p in acquired:
                        _seal_one_period(
                            conn, period_id=p, order_id=order_id,
                            attempt_id=attempt_id, now=now.isoformat(),
                        )
                        sealed_now.append(p)
                    conn.commit()  # 整单一个原子提交
                except SealServiceError as e:
                    failing = (e.reason_code, e.detail)
                except Exception as e:  # 含注入故障
                    failing = (
                        getattr(e, "reason_code", REASON_OTHER),
                        getattr(e, "detail", str(e)),
                    )
                if failing is not None:
                    conn.rollback()  # 全部账期退回提交前可写态，批头一并撤销
                    reason, detail = failing
                    # 真正触发失败的账期（其 period_id 出现在 detail 中）记真实原因；
                    # 其余账期为整单回滚连带退回，本身无过错。
                    for p in acquired:
                        if p in detail:
                            domain_failures.append((p, reason, detail))
                        else:
                            domain_failures.append(
                                (p, REASON_ROLLBACK_CASCADE, f"整单回滚连带退回: {p}")
                            )
                    sealed_now = []
            else:  # POLICY_KEEP：SAVEPOINT 逐期收口，失败只回滚该期
                for p in acquired:
                    conn.execute("SAVEPOINT sp_seal")
                    try:
                        _seal_one_period(
                            conn, period_id=p, order_id=order_id,
                            attempt_id=attempt_id, now=now.isoformat(),
                        )
                        conn.execute("RELEASE SAVEPOINT sp_seal")
                        conn.commit()  # 成功者立即落库，保持 sealed
                        sealed_now.append(p)
                    except SealServiceError as e:
                        conn.execute("ROLLBACK TO SAVEPOINT sp_seal")
                        conn.execute("RELEASE SAVEPOINT sp_seal")
                        domain_failures.append((p, e.reason_code, e.detail))
                    except Exception as e:
                        conn.execute("ROLLBACK TO SAVEPOINT sp_seal")
                        conn.execute("RELEASE SAVEPOINT sp_seal")
                        reason = getattr(e, "reason_code", REASON_OTHER)
                        detail = getattr(e, "detail", str(e))
                        domain_failures.append((p, reason, detail))

        # ---- 释放本 attempt 仍活跃的租约 ----
        conn.execute(
            "UPDATE seal_leases SET status='released', released_at=? "
            "WHERE attempt_id=? AND status='active'",
            (_now(), attempt_id),
        )
        _audit(conn, action="leases_released", order_id=order_id,
               attempt_id=attempt_id, detail=f"count={len(acquired)}")

        # ---- 记账事务④：失败回执 + 行/工单/attempt 终态（回滚后仍可查）----
        all_failures = conflict_failures + domain_failures
        for p, reason, detail in all_failures:
            _record_failure(
                conn, order_id=order_id, attempt_id=attempt_id,
                period_id=p, reason_code=reason, detail=detail,
            )

        success_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM seal_order_lines WHERE order_id=? AND status='sealed'",
            (order_id,),
        ).fetchone()["c"]
        failed_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM seal_order_lines WHERE order_id=? AND status='failed'",
            (order_id,),
        ).fetchone()["c"]
        pending_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM seal_order_lines WHERE order_id=? AND status='pending'",
            (order_id,),
        ).fetchone()["c"]

        if all_failures and not success_lines:
            order_status = "failed"
        elif all_failures and success_lines:
            order_status = "partial"
        else:
            order_status = "sealed"

        rollback_hit = policy == POLICY_ROLLBACK and (
            domain_failures or conflict_failures
        )
        if rollback_hit:
            attempt_status = "rolled_back"
        elif all_failures:
            attempt_status = "failed"
        else:
            attempt_status = "succeeded"

        conn.execute(
            """UPDATE seal_attempts SET status=?, success_count=?, failure_count=?,
                 finished_at=? WHERE attempt_id=?""",
            (attempt_status, len(sealed_now), len(all_failures), _now(), attempt_id),
        )
        conn.execute(
            """UPDATE seal_orders SET status=?, success_count=?, failure_count=?,
                 finalized_at=? WHERE order_id=?""",
            (order_status, success_lines, failed_lines, _now(), order_id),
        )
        _audit(
            conn, action="attempt_finalized", order_id=order_id, attempt_id=attempt_id,
            detail=(
                f"policy={policy};status={attempt_status};sealed_now={len(sealed_now)};"
                f"failures={len(all_failures)};pending={pending_lines}"
            ),
        )
        conn.commit()
        return get_order(order_id)
    finally:
        conn.close()


def get_order(order_id: str) -> dict:
    conn = connect()
    try:
        order = conn.execute(
            "SELECT * FROM seal_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(404, "工单不存在")
        lines = conn.execute(
            "SELECT * FROM seal_order_lines WHERE order_id=? ORDER BY line_id",
            (order_id,),
        ).fetchall()
        attempts = conn.execute(
            "SELECT * FROM seal_attempts WHERE order_id=? ORDER BY attempt_no",
            (order_id,),
        ).fetchall()
        return {
            "order": dict(order),
            "lines": [dict(r) for r in lines],
            "attempts": [dict(r) for r in attempts],
        }
    finally:
        conn.close()


def list_orders() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM seal_orders ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_failures(order_id: str | None = None, attempt_id: str | None = None) -> list[dict]:
    """失败回执查询：可按工单或某次 attempt 打点查询。"""
    conn = connect()
    try:
        sql = "SELECT * FROM seal_failures WHERE 1=1"
        params: list = []
        if order_id:
            sql += " AND order_id=?"
            params.append(order_id)
        if attempt_id:
            sql += " AND attempt_id=?"
            params.append(attempt_id)
        sql += " ORDER BY created_at"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def list_audit(order_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM seal_audit WHERE 1=1"
        params: list = []
        if order_id:
            sql += " AND order_id=?"
            params.append(order_id)
        sql += " ORDER BY created_at, audit_id"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
