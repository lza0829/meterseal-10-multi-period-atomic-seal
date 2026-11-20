<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { 账期状态, 工单状态, 行状态, 封印策略, 失败原因, 标 } from '../labels'

const periods = ref([])
const checked = ref(new Set())
const policy = ref('rollback')
const orders = ref([])
const current = ref(null)
const failures = ref([])
const audit = ref([])
const msg = ref('')
const err = ref('')
const busy = ref(false)

const selectable = computed(() => periods.value.filter(p => p.status !== 'sealed'))
const checkedList = computed(() =>
  periods.value.filter(p => checked.value.has(p.period_id)).map(p => p.period_id)
)

async function refresh() {
  periods.value = await api.periods()
  orders.value = await api.sealOrders()
  const pol = await api.sealPolicy()
  policy.value = pol.policy
  // 清理已不存在或已封印的勾选项
  const openIds = new Set(selectable.value.map(p => p.period_id))
  checked.value = new Set([...checked.value].filter(id => openIds.has(id)))
  if (current.value) {
    current.value = await api.sealOrder(current.value.order.order_id)
    failures.value = await api.sealOrderFailures(current.value.order.order_id)
    audit.value = await api.sealOrderAudit(current.value.order.order_id)
  }
}

function toggle(id) {
  const next = new Set(checked.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  checked.value = next
}

async function switchPolicy() {
  err.value = ''; msg.value = ''
  try {
    const next = policy.value === 'rollback' ? 'keep' : 'rollback'
    await api.putSealPolicy(next)
    policy.value = next
    msg.value = `全局口径已切换为：${标(封印策略, next)}（仅影响此后新落工单）`
  } catch (e) { err.value = e.message }
}

async function submitWholeOrder() {
  err.value = ''; msg.value = ''
  const ids = checkedList.value
  if (!ids.length) { err.value = '请至少勾选一个账期'; return }
  busy.value = true
  try {
    // 一次工单整单提交：服务端在同一事务/保存点内逐期处理，前端不循环单期调用
    const created = await api.createSealOrder(ids)
    const result = await api.submitSealOrder(created.order.order_id)
    current.value = result
    failures.value = await api.sealOrderFailures(created.order.order_id)
    audit.value = await api.sealOrderAudit(created.order.order_id)
    const o = result.order
    msg.value = `工单 ${o.order_no} 完成：成功 ${o.success_count} / 失败 ${o.failure_count}（${标(工单状态, o.status)}）`
    checked.value = new Set()
    await refresh()
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

async function openOrder(id) {
  err.value = ''
  try {
    current.value = await api.sealOrder(id)
    failures.value = await api.sealOrderFailures(id)
    audit.value = await api.sealOrderAudit(id)
  } catch (e) { err.value = e.message }
}

async function resubmit() {
  if (!current.value) return
  busy.value = true; err.value = ''
  try {
    // 租约超时/失败后重提：同工单生成新 attempt
    current.value = await api.submitSealOrder(current.value.order.order_id)
    failures.value = await api.sealOrderFailures(current.value.order.order_id)
    audit.value = await api.sealOrderAudit(current.value.order.order_id)
    msg.value = '已用新 attempt 重提'
    await refresh()
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>
<template>
  <div>
    <h1>账期封印（整单）</h1>
    <p class="sub">一次工单勾选多个账期，服务端整单处理；封印后该期估抄、实抄禁止再写</p>

    <div class="panel" style="border-top:none;padding-top:0">
      <div class="row">
        <button class="secondary" @click="switchPolicy">当前口径：{{ 标(封印策略, policy) }}（点击切换）</button>
        <button class="warn" :disabled="busy || !checkedList.length" @click="submitWholeOrder">
          {{ busy ? '处理中…' : `整单封印已选 ${checkedList.length} 期` }}
        </button>
        <button class="secondary" @click="refresh">刷新</button>
      </div>
      <table>
        <thead><tr><th>勾选</th><th>账期</th><th>状态</th><th>封印时刻</th><th>绑定批</th></tr></thead>
        <tbody>
          <tr v-for="p in periods" :key="p.period_id">
            <td>
              <input
                type="checkbox"
                style="min-width:auto"
                :disabled="p.status === 'sealed'"
                :checked="checked.has(p.period_id)"
                @change="toggle(p.period_id)"
              />
            </td>
            <td>{{ p.period_id }}</td>
            <td><span class="tag" :class="p.status">{{ 标(账期状态, p.status) }}</span></td>
            <td>{{ p.sealed_at || '—' }}</td>
            <td>{{ p.batch_no || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>

    <div class="panel" v-if="current">
      <h3>工单 {{ current.order.order_no }}
        <span class="tag" :class="current.order.status">{{ 标(工单状态, current.order.status) }}</span>
      </h3>
      <p class="sub">
        口径 {{ 标(封印策略, current.order.policy) }} · 成功 {{ current.order.success_count }} ·
        失败 {{ current.order.failure_count }}
        <button class="secondary" style="margin-left:0.8rem" :disabled="busy" @click="resubmit">重提（新 attempt）</button>
      </p>

      <h4 style="margin:0.6rem 0 0.3rem">按期列表</h4>
      <table>
        <thead><tr><th>账期</th><th>行状态</th><th>原因</th><th>封印批头</th><th>attempt</th></tr></thead>
        <tbody>
          <tr v-for="ln in current.lines" :key="ln.line_id">
            <td>{{ ln.period_id }}</td>
            <td><span class="tag" :class="ln.status">{{ 标(行状态, ln.status) }}</span></td>
            <td>{{ ln.reason_code ? 标(失败原因, ln.reason_code) : '—' }}</td>
            <td>{{ ln.sealed_batch_id || '—' }}</td>
            <td>{{ ln.attempt_id || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h4 style="margin:0.8rem 0 0.3rem">Attempt 记录</h4>
      <table>
        <thead><tr><th>#</th><th>attempt</th><th>状态</th><th>成功</th><th>失败</th><th>起止</th></tr></thead>
        <tbody>
          <tr v-for="a in current.attempts" :key="a.attempt_id">
            <td>{{ a.attempt_no }}</td>
            <td>{{ a.attempt_id }}</td>
            <td>{{ a.status }}</td>
            <td>{{ a.success_count }}</td>
            <td>{{ a.failure_count }}</td>
            <td>{{ a.started_at }} → {{ a.finished_at || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h4 style="margin:0.8rem 0 0.3rem">失败回执</h4>
      <table v-if="failures.length">
        <thead><tr><th>账期</th><th>原因码</th><th>说明</th><th>attempt</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="f in failures" :key="f.failure_id">
            <td>{{ f.period_id || '—' }}</td>
            <td>{{ 标(失败原因, f.reason_code) }}</td>
            <td>{{ f.detail }}</td>
            <td>{{ f.attempt_id }}</td>
            <td>{{ f.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="sub">无失败记录</p>

      <h4 style="margin:0.8rem 0 0.3rem">审计链路（{{ audit.length }}）</h4>
      <p class="sub" style="max-height:180px;overflow:auto">
        <span v-for="a in audit" :key="a.audit_id" style="display:block">
          {{ a.created_at }} · {{ a.action }} · {{ a.period_id || '-' }} · {{ a.detail || '' }}
        </span>
      </p>
    </div>

    <div class="panel">
      <h3>历史工单</h3>
      <table>
        <thead><tr><th>工单号</th><th>口径</th><th>状态</th><th>成功</th><th>失败</th><th>账期数</th><th></th></tr></thead>
        <tbody>
          <tr v-for="o in orders" :key="o.order_id">
            <td>{{ o.order_no }}</td>
            <td>{{ 标(封印策略, o.policy) }}</td>
            <td><span class="tag" :class="o.status">{{ 标(工单状态, o.status) }}</span></td>
            <td>{{ o.success_count }}</td>
            <td>{{ o.failure_count }}</td>
            <td>{{ o.period_count }}</td>
            <td><button class="secondary" @click="openOrder(o.order_id)">查看</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
