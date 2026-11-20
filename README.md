# 抄表簿

骨架：抄表读数装载 → 估抄/实抄对齐 → 差额处置 → 账期封印。

- 前端 http://127.0.0.1:6010（勿用 6000，浏览器会拦）
- 接口 http://127.0.0.1:11000

## 整单封印（一次工单勾选多个账期）

封印入口只有 `POST /api/seal-orders`（落单）+ `POST /api/seal-orders/{id}/submit`（提交），
后端在同一事务/保存点内逐期处理，前端不允许循环单期调用（旧 `POST /api/seal` 已移除）。

联动要素：

- **attempt**：每次提交生成一个 attempt，失败/租约超时后同工单可重提，`attempt_no` 递增。
- **逐期租约** `seal_leases`：部分唯一索引保证同一账期至多一个活跃租约，两工单并发不得双封；
  带 `expires_at`，超时由下次提交清扫后可重提。
- **批头** `seal_batches`：本工单为每期插入的封印批头，与 `periods` 写闸切换**同事务收口**，
  故障即回滚，不留半截批头（部分唯一索引兜底每期一个生效批头）。
- **写闸**：`periods.status=sealed` 后，估抄/实抄装载口 `assert_period_writable` 一律 409 并审计留痕。
- **失败回执** `seal_failures`：独立记账事务写入，整单回滚后仍可按工单/attempt 查询。
- **审计** `seal_audit`：落单、提租、冲突、封印、失败、回滚、释放、写闸拒绝全链路留痕。
- **总览**：成功封印数由 `periods.status` 派生（回滚不涨、keep 只涨成功者）。
- **按期列表** `seal_order_lines`：每账期一行，状态随 attempt 联动。

### 两种架构（全局只贯彻一种，设置项 `seal_policy`）

| 口径 | rollback（整单回滚，默认） | keep（保住已成功） |
| --- | --- | --- |
| 任一账期失败 | 全部退回提交前可写态、撤销本工单批头 | 成功者保持 sealed，失败者保持可写并清半截批头 |
| 失败回执 | 只报失败清单（真因 + 连带退回 `rollback_cascade`） | 按账期打点，只报失败期 |
| 总览成功数 | 不涨 | 只加成功者 |
| 半截 sealed | 禁止 | 失败期不留，成功期保留 |

策略在落单时固化到工单，落单后切换全局策略不影响在途工单。

## 启动

```powershell
cd backend
C:\Users\18435\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11000 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 6010
```

## 测试

仅依赖标准库 `unittest`（HTTP 用 uvicorn + urllib，无需 pytest/httpx）：

```bash
cd backend
python -m unittest discover -s tests -v
```

- `tests/test_seal_orders.py`：两架构共用同一组场景（缺证据分叉、写闸拒写、失败回执、
  并发不双封、租约超时重提、header/gate 故障同事务收口、全联动、策略固化）。
- `tests/test_http_seal_orders.py`：真实 HTTP 端到端、旧单期端点已 404、写闸 409、总览联动。

