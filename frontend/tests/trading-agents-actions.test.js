import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createTradingAgentsBatch,
  runProbabilityPortfolioDryRun,
} from '../src/trading-agents-actions.js'

test('only reports batch creation after configuration, batch creation, and reload succeed', async () => {
  const calls = []

  await createTradingAgentsBatch({
    saveConfiguration: async () => calls.push('save'),
    createBatch: async () => {
      calls.push('create')
      return { id: 42 }
    },
    reload: async () => calls.push('reload'),
    notify: (message) => calls.push(message),
  })

  assert.deepEqual(calls, ['save', 'create', 'reload', 'TradingAgents 分析批次已创建'])
})

test('does not report batch creation when the batch API rejects the request', async () => {
  const calls = []

  await assert.rejects(
    createTradingAgentsBatch({
      saveConfiguration: async () => calls.push('save'),
      createBatch: async () => {
        calls.push('create')
        throw new Error('公司公告数据已过期')
      },
      reload: async () => calls.push('reload'),
      notify: (message) => calls.push(message),
    }),
    /公司公告数据已过期/,
  )

  assert.deepEqual(calls, ['save', 'create'])
})

test('probability dry run selects the created audit after reloading state', async () => {
  const calls = []

  await runProbabilityPortfolioDryRun({
    createDryRun: async () => {
      calls.push('create')
      return { summary: { portfolio_run_id: 17 } }
    },
    reload: async () => calls.push('reload'),
    selectRun: async (id) => calls.push(`select:${id}`),
    notify: (message) => calls.push(message),
  })

  assert.deepEqual(calls, [
    'create',
    'reload',
    'select:17',
    '概率组合无下单演练已完成',
  ])
})
