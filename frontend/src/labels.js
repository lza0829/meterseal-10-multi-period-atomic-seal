/** 界面文案映射：库内码 → 中文（页面不直接露英文码） */
export const 表类型 = { water: '水表', electric: '电表', 水表: '水表', 电表: '电表' }
export const 表状态 = { active: '在用', inactive: '停用', 在用: '在用', 停用: '停用' }
export const 账期状态 = { open: '开放', sealed: '已封印', expired: '已作废', 开放: '开放', 已封印: '已封印' }
export const 批状态 = { open: '开放', sealed: '已封印', expired: '已作废' }
export const 工单状态 = {
  sealing: '封印中',
  sealed: '整单已封',
  failed: '整单失败',
  partial: '部分封印',
}
export const 行状态 = {
  pending: '待处理',
  sealed: '已封印',
  failed: '失败',
}
export const 封印策略 = {
  rollback: '整单回滚：任一失败全部退回',
  keep: '保住已成功：成功者保留、失败者可写',
}
export const 失败原因 = {
  period_not_found: '账期不存在',
  already_sealed: '账期已封印',
  no_open_batch: '缺封印证据（无开放对账批）',
  lease_conflict: '账期被其他工单占用（提租冲突）',
  rollback_cascade: '整单回滚连带退回',
  injected_fault: '同事务收口故障（已回滚）',
  other: '其他错误',
}
export const 命运 = {
  pending: '未决',
  accept_short: '认短',
  accept_over: '认溢',
  quarantine: '隔离',
}

export function 标(map, key) {
  if (key == null || key === '') return '—'
  return map[key] || String(key)
}
