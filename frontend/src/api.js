const base = '/api'

async function req(path, opts = {}) {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const msg = (data && data.detail) || text || res.statusText
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

export const api = {
  overview: () => req('/overview'),
  meters: () => req('/meters'),
  estimates: (periodId) => req('/estimates' + (periodId ? `?period_id=${periodId}` : '')),
  actuals: (periodId) => req('/actuals' + (periodId ? `?period_id=${periodId}` : '')),
  postEstimate: (body) => req('/estimates', { method: 'POST', body: JSON.stringify(body) }),
  postActual: (body) => req('/actuals', { method: 'POST', body: JSON.stringify(body) }),
  periods: () => req('/periods'),
  align: (period_id) => req('/align', { method: 'POST', body: JSON.stringify({ period_id }) }),
  batches: (periodId) => req('/batches' + (periodId ? `?period_id=${periodId}` : '')),
  variances: (q) => {
    const sp = new URLSearchParams(q || {})
    const s = sp.toString()
    return req('/variances' + (s ? `?${s}` : ''))
  },
  setFate: (id, fate) => req(`/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate }) }),
  // 整单封印：一次工单勾选多个账期，服务端整单处理（非前端循环单期）
  createSealOrder: (periodIds) => req('/seal-orders', { method: 'POST', body: JSON.stringify({ period_ids: periodIds }) }),
  submitSealOrder: (orderId) => req(`/seal-orders/${orderId}/submit`, { method: 'POST' }),
  sealOrders: () => req('/seal-orders'),
  sealOrder: (orderId) => req(`/seal-orders/${orderId}`),
  sealOrderFailures: (orderId) => req(`/seal-orders/${orderId}/failures`),
  sealOrderAudit: (orderId) => req(`/seal-orders/${orderId}/audit`),
  sealPolicy: () => req('/seal-policy'),
  putSealPolicy: (policy) => req('/seal-policy', { method: 'PUT', body: JSON.stringify({ policy }) }),
  leases: () => req('/leases'),
  sealBatches: () => req('/seal-batches'),
  settings: () => req('/settings'),
  putSetting: (key, value) => req(`/settings/${key}`, { method: 'PUT', body: JSON.stringify({ value }) }),
}
