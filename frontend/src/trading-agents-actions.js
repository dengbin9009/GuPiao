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
