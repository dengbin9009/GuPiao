async function operate({
  key,
  request,
  reload,
  select,
  notify,
  suffix,
  method = 'POST',
  body,
  message,
}) {
  await request(`/quant-strategies/${key}${suffix}`, {
    method,
    body: JSON.stringify(body ?? {}),
  })
  await reload()
  await select(key)
  notify(message)
}


export async function queueQuantBacktest(options) {
  return operate({
    ...options,
    suffix: '/backtests',
    body: { start_date: options.startDate, end_date: options.endDate },
    message: '真实点时数据回测任务已创建',
  })
}


export async function runQuantDryRun(options) {
  return operate({
    ...options,
    suffix: '/dry-run',
    message: '无下单演练已通过',
  })
}


export async function activateQuantStrategy(options) {
  return operate({
    ...options,
    suffix: '/activate',
    message: '模拟自动计划已启用',
  })
}


export async function pauseQuantStrategy(options) {
  return operate({
    ...options,
    suffix: '/pause',
    message: '策略已暂停',
  })
}


export async function saveQuantStrategy(options) {
  return operate({
    ...options,
    suffix: '',
    method: 'PUT',
    body: { mode: 'SIMULATION', parameters: options.parameters },
    message: '策略参数已保存，需重新回测和演练',
  })
}
