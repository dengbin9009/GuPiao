import assert from 'node:assert/strict'
import test from 'node:test'

import {
  activateQuantStrategy,
  pauseQuantStrategy,
  queueQuantBacktest,
  runQuantDryRun,
  saveQuantStrategy,
} from '../src/quant-strategy-actions.js'


const harness = () => {
  const calls = []
  return {
    calls,
    request: async (path, options = {}) => {
      calls.push(`${options.method || 'GET'} ${path}`)
      return { task_id: 17 }
    },
    reload: async () => calls.push('reload'),
    select: async (key) => calls.push(`select:${key}`),
    notify: (message) => calls.push(message),
  }
}


test('queues point-in-time backtest before reloading selected strategy', async () => {
  const state = harness()

  await queueQuantBacktest({
    ...state,
    key: 'multi_factor_core',
    startDate: '2024-01-01',
    endDate: '2026-07-23',
  })

  assert.deepEqual(state.calls, [
    'POST /quant-strategies/multi_factor_core/backtests',
    'reload',
    'select:multi_factor_core',
    '真实点时数据回测任务已创建',
  ])
})


test('dry run, activate, pause, and save each reload authoritative state', async () => {
  for (const [action, endpoint, message, extra] of [
    [runQuantDryRun, 'dry-run', '无下单演练已通过', {}],
    [activateQuantStrategy, 'activate', '模拟自动计划已启用', {}],
    [pauseQuantStrategy, 'pause', '策略已暂停', {}],
    [saveQuantStrategy, '', '策略参数已保存，需重新回测和演练', { parameters: { prefilter_size: 500 } }],
  ]) {
    const state = harness()
    await action({ ...state, key: 'multi_factor_core', ...extra })
    const suffix = endpoint ? `/${endpoint}` : ''
    const method = action === saveQuantStrategy ? 'PUT' : 'POST'
    assert.deepEqual(state.calls, [
      `${method} /quant-strategies/multi_factor_core${suffix}`,
      'reload',
      'select:multi_factor_core',
      message,
    ])
  }
})
