export async function createTradingAgentsBatch({
  saveConfiguration,
  createBatch,
  reload,
  notify,
}) {
  await saveConfiguration()
  await createBatch()
  await reload()
  notify('TradingAgents 分析批次已创建')
}

export async function runProbabilityPortfolioDryRun({
  createDryRun,
  reload,
  selectRun,
  notify,
}) {
  const run = await createDryRun()
  await reload()
  await selectRun(run.summary.portfolio_run_id)
  notify('概率组合无下单演练已完成')
}
