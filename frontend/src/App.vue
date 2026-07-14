<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Activity, Bell, BookOpenCheck, ChevronRight, CircleDollarSign, Database,
  Gauge, Heart, LayoutDashboard, LogOut, Play, Plus, RefreshCw, Search,
  Settings2, ShieldAlert, Trash2, TrendingUp, WalletCards, X
} from 'lucide-vue-next'

const api = async (path, options = {}) => {
  const response = await fetch(`/api${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  })
  if (response.status === 204) return null
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || '请求失败')
  return body
}

const nav = [
  ['dashboard', '总览', LayoutDashboard],
  ['watchlist', '特别关注', Heart],
  ['strategies', '策略中心', TrendingUp],
  ['backtests', '历史回测', BookOpenCheck],
  ['trading', '账户与交易', WalletCards],
  ['risk', '风控与网关', ShieldAlert],
  ['notifications', '通知', Bell]
]

const active = ref('dashboard')
const authenticated = ref(false)
const loading = ref(false)
const error = ref('')
const toast = ref('')
const loginForm = reactive({ username: 'admin', password: '' })
const searchQuery = ref('')
const searchResults = ref([])
const dashboard = ref(null)
const watchlist = ref([])
const strategies = ref([])
const strategyConfigs = ref([])
const strategySchedules = ref([])
const strategyForm = reactive({ name: '一夜持股法 · 自定义', mode: 'SIMULATION', max_candidates: 3, target_position_pct: 0.2 })
const runs = ref([])
const backtests = ref([])
const selectedBacktest = ref(null)
const account = ref(null)
const orders = ref([])
const positions = ref([])
const riskSettings = ref([])
const riskEvents = ref([])
const gateways = ref([])
const liveAccounts = ref([])
const dataSources = ref([])
const realtimeStatus = ref([])
const channels = ref([])
const deliveries = ref([])
const marketCalendar = ref(null)
const simulationAccounts = ref([])
const agentsReadiness = ref(null)
const agentsProfiles = ref({ analysis_profiles: {}, position_mappings: {} })
const agentsBatches = ref([])
const selectedAgentsBatch = ref(null)
const expandedAgentReportId = ref(null)
const agentsForm = reactive({
  analysis_profile: 'a_share_balanced', position_mapping: 'fixed_rating',
  quick_model: 'gpt-5.4-mini', deep_model: 'gpt-5.2', prefilter_size: 100,
  top_n: 10, max_positions: 5, max_llm_calls: 100, max_input_tokens: 1000000,
  max_output_tokens: 150000, worker_concurrency: 2, candidate_timeout_seconds: 480,
  max_position_pct: 0.2, max_total_exposure_pct: 0.6,
  snapshot_quote_max_age_seconds: 600, daily_max_age_days: 7,
  event_max_age_seconds: 1800, enrichment_enabled: true, enrichment_timeout_seconds: 45,
  analysis_deadline: '14:42', rebalance_time: '14:45', latest_rebalance_time: '14:50',
  dry_run: true, simulation_account_id: null
})
const backtestForm = reactive({ start_date: '2025-01-01', end_date: '2025-12-31', initial_cash: 10000 })
const notificationForm = reactive({ type: 'email', name: '', recipient: '', secret_ref: '', event_types: ['order_failure', 'circuit_breaker'] })

const formatMoney = (value) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 2 }).format(value || 0)
const formatPct = (value) => `${((value || 0) * 100).toFixed(2)}%`
const shortTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const durationText = (start, end) => {
  if (!start || !end) return '—'
  const seconds = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)}分${seconds % 60}秒`
}
const activeTitle = computed(() => nav.find(([key]) => key === active.value)?.[1] || '总览')
const runResult = (run) => run?.summary?.reason || run?.summary?.symbol || run?.error_message || '—'

const notify = (message) => {
  toast.value = message
  setTimeout(() => { toast.value = '' }, 2400)
}

const loadAll = async () => {
  loading.value = true
  error.value = ''
  try {
    const [d, w, s, c, sch, r, b, a, o, p, rs, re, g, ds, rt, ch, nd, la, cal, sa, ar, ap, ab] = await Promise.all([
      api('/dashboard'), api('/watchlist'), api('/strategies'), api('/strategy-configs'),
      api('/strategy-schedules'), api('/strategy-runs'), api('/backtests'), api('/simulation/account'), api('/orders'),
      api('/positions'), api('/risk/settings'), api('/risk/events'), api('/gateways'),
      api('/market-data/sources'), api('/market-data/realtime-status'), api('/notifications/channels'), api('/notifications/deliveries'),
      api('/live/accounts'), api('/market-data/calendar'), api('/simulation/accounts'),
      api('/trading-agents/readiness'), api('/trading-agents/profiles'), api('/trading-agents/batches')
    ])
    dashboard.value = d; watchlist.value = w; strategies.value = s; strategyConfigs.value = c; strategySchedules.value = sch
    runs.value = r; backtests.value = b; account.value = a; orders.value = o; positions.value = p
    riskSettings.value = rs; riskEvents.value = re; gateways.value = g; dataSources.value = ds
    realtimeStatus.value = rt
    channels.value = ch; deliveries.value = nd
    liveAccounts.value = la
    marketCalendar.value = cal
    simulationAccounts.value = sa; agentsReadiness.value = ar; agentsProfiles.value = ap; agentsBatches.value = ab
    const agentsConfig = c.find(item => item.strategy_key === 'trading_agents_auto')
    if (agentsConfig) Object.assign(agentsForm, agentsConfig.parameters || {}, { simulation_account_id: agentsConfig.simulation_account_id })
    authenticated.value = true
  } catch (err) {
    if (String(err.message).includes('登录')) authenticated.value = false
    else error.value = err.message
  } finally {
    loading.value = false
  }
}

const login = async () => {
  loading.value = true; error.value = ''
  try {
    await api('/auth/login', { method: 'POST', body: JSON.stringify(loginForm) })
    await loadAll()
  } catch (err) { error.value = err.message } finally { loading.value = false }
}

const logout = async () => {
  await api('/auth/logout', { method: 'POST' })
  authenticated.value = false
}

const searchStocks = async () => {
  if (!searchQuery.value.trim()) { searchResults.value = []; return }
  searchResults.value = await api(`/stocks/search?q=${encodeURIComponent(searchQuery.value)}`)
}

const addStock = async (stock) => {
  try {
    await api('/watchlist', { method: 'POST', body: JSON.stringify({ symbol: stock.symbol }) })
    searchResults.value = []; searchQuery.value = ''; watchlist.value = await api('/watchlist')
    notify(`已关注 ${stock.name}`)
  } catch (err) { error.value = err.message }
}

const removeStock = async (id) => {
  await api(`/watchlist/${id}`, { method: 'DELETE' })
  watchlist.value = await api('/watchlist')
}

const refreshWatchlist = async () => {
  try {
    await api('/watchlist/refresh', { method: 'POST' })
    watchlist.value = await api('/watchlist')
    notify('特别关注行情已刷新')
  } catch (err) { error.value = err.message }
}

const syncStocks = async () => {
  try {
    await api('/market-data/stocks/sync', { method: 'POST' })
    await loadAll()
    notify('股票主数据同步完成')
  } catch (err) { error.value = err.message }
}

const syncEvents = async () => {
  try {
    await api('/market-data/events/sync', { method: 'POST' })
    await loadAll()
    notify('公司事件同步完成')
  } catch (err) { error.value = err.message }
}

const refreshRealtimeStatus = async () => {
  try {
    const result = await api('/market-data/realtime-poll', { method: 'POST' })
    await loadAll()
    notify(`实时轮询完成，更新 ${result.updated} 条，缺失 ${result.missing} 条，错误 ${result.errors} 次`)
  } catch (err) { error.value = err.message }
}

const ensureConfig = async () => {
  const existing = strategyConfigs.value.find(item => item.strategy_key === 'overnight_hold')
  if (existing) return existing
  const config = await api('/strategy-configs', {
    method: 'POST',
    body: JSON.stringify({
      strategy_key: 'overnight_hold',
      name: strategyForm.name,
      mode: strategyForm.mode,
      parameters: {
        max_candidates: Number(strategyForm.max_candidates),
        target_position_pct: Number(strategyForm.target_position_pct),
      }
    })
  })
  strategyConfigs.value = await api('/strategy-configs')
  return config
}

const saveAgentsConfig = async () => {
  try {
    const { simulation_account_id, ...parameters } = agentsForm
    await api('/trading-agents/config', {
      method: 'PUT', body: JSON.stringify({ parameters, simulation_account_id })
    })
    await loadAll(); notify('TradingAgents 配置已保存')
  } catch (err) { error.value = err.message; throw err }
}

const runAgentsBatch = async () => {
  try {
    await saveAgentsConfig()
    await api('/trading-agents/batches', { method: 'POST', body: '{}' })
    await loadAll(); notify('TradingAgents 分析批次已创建')
  } catch (err) { error.value = err.message }
}

const selectAgentsBatch = async (id) => {
  try { selectedAgentsBatch.value = await api(`/trading-agents/batches/${id}`); expandedAgentReportId.value = null }
  catch (err) { error.value = err.message }
}

const cancelAgentsBatch = async (id) => {
  try {
    await api(`/trading-agents/batches/${id}/cancel`, { method: 'POST' })
    await loadAll(); selectedAgentsBatch.value = null; notify('批次已取消')
  } catch (err) { error.value = err.message }
}

const dryRunAgentsBatch = async (id) => {
  try {
    await api(`/trading-agents/batches/${id}/dry-run`, { method: 'POST' })
    await loadAll(); selectedAgentsBatch.value = await api(`/trading-agents/batches/${id}`)
    notify('无下单演练已完成')
  } catch (err) { error.value = err.message }
}

const createStrategyConfig = async () => {
  try {
    await api('/strategy-configs', {
      method: 'POST',
      body: JSON.stringify({
        strategy_key: 'overnight_hold',
        name: strategyForm.name,
        mode: strategyForm.mode,
        parameters: {
          max_candidates: Number(strategyForm.max_candidates),
          target_position_pct: Number(strategyForm.target_position_pct),
        }
      })
    })
    await loadAll()
    notify('策略配置已创建')
  } catch (err) { error.value = err.message }
}

const runStrategy = async () => {
  try {
    const config = await ensureConfig()
    await api(`/strategy-configs/${config.id}/run`, { method: 'POST' })
    await loadAll(); notify('策略运行完成')
  } catch (err) { error.value = err.message }
}

const toggleSchedule = async (schedule) => {
  try {
    await api(`/strategy-schedules/${schedule.id}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: !schedule.enabled })
    })
    strategySchedules.value = await api('/strategy-schedules')
    notify(schedule.enabled ? '调度已停用' : '调度已启用')
  } catch (err) { error.value = err.message }
}

const runBacktest = async () => {
  try {
    const run = await api('/backtests', {
      method: 'POST',
      body: JSON.stringify({ strategy_key: 'overnight_hold', timeframe: '1m', ...backtestForm, parameters: {} })
    })
    backtests.value = await api('/backtests')
    selectedBacktest.value = await api(`/backtests/${run.id}`)
    notify('回测完成')
  } catch (err) { error.value = err.message }
}

const selectBacktest = async (id) => {
  try {
    error.value = ''
    selectedBacktest.value = await api(`/backtests/${id}`)
  } catch (err) { error.value = err.message }
}

const emergencyStop = async () => {
  await api('/risk/emergency-stop', { method: 'POST' })
  await loadAll(); notify('紧急停止已启用')
}

const syncLiveAccounts = async () => {
  try {
    liveAccounts.value = await api('/live/accounts/sync', { method: 'POST' })
    await loadAll(); notify('真实盘账户同步完成')
  } catch (err) { error.value = err.message }
}

const toggleLiveAccount = async (account) => {
  await api(`/live/accounts/${account.id}`, {
    method: 'PUT', body: JSON.stringify({ enabled: !account.enabled })
  })
  await loadAll()
}

const setLiveMode = async (enabled) => {
  if (enabled && !window.confirm('确认启用真实盘交易？')) return
  try {
    await api('/live/mode', {
      method: 'PUT', body: JSON.stringify({ enabled, confirmation: enabled ? 'ENABLE LIVE' : '' })
    })
    await loadAll(); notify(enabled ? '真实盘已启用' : '真实盘已关闭')
  } catch (err) { error.value = err.message }
}

const addChannel = async () => {
  try {
    await api('/notifications/channels', { method: 'POST', body: JSON.stringify(notificationForm) })
    channels.value = await api('/notifications/channels')
    notificationForm.name = ''; notificationForm.recipient = ''; notificationForm.secret_ref = ''
    notify('通知渠道已添加')
  } catch (err) { error.value = err.message }
}

const testChannel = async (id) => {
  await api(`/notifications/channels/${id}/test`, { method: 'POST' })
  deliveries.value = await api('/notifications/deliveries'); notify('测试通知已进入投递记录')
}

onMounted(async () => {
  try {
    await loadAll()
  } catch { authenticated.value = false }
})
</script>

<template>
  <div v-if="!authenticated" class="login-shell">
    <section class="login-panel">
      <div class="brand-lockup"><span class="brand-mark">GP</span><div><strong>GuPiao</strong><small>量化交易控制台</small></div></div>
      <form @submit.prevent="login" class="login-form">
        <label>管理员账号<input v-model="loginForm.username" autocomplete="username" /></label>
        <label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary wide" :disabled="loading"><Activity :size="17" />{{ loading ? '正在登录' : '登录' }}</button>
      </form>
    </section>
  </div>

  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand-lockup"><span class="brand-mark">GP</span><div><strong>GuPiao</strong><small>量化交易</small></div></div>
      <nav>
        <button v-for="[key, label, Icon] in nav" :key="key" :class="{ active: active === key }" @click="active = key">
          <component :is="Icon" :size="18" /><span>{{ label }}</span>
        </button>
      </nav>
      <div class="sidebar-foot">
        <div class="mode-chip"><span class="status-dot"></span>{{ dashboard?.mode === 'LIVE' ? '真实盘' : '模拟盘' }}</div>
        <button class="icon-action" title="退出登录" @click="logout"><LogOut :size="18" /></button>
      </div>
    </aside>

    <main class="main-content">
      <header class="topbar">
        <div><p class="eyebrow">GU PIAO / {{ dashboard?.mode || 'SIMULATION' }}</p><h1>{{ activeTitle }}</h1></div>
        <div class="top-actions">
          <span :class="['connection', error ? 'danger' : '']"><span></span>{{ error ? '需要处理' : '系统正常' }}</span>
          <button class="icon-action" title="刷新" @click="loadAll"><RefreshCw :size="18" :class="{ spin: loading }" /></button>
        </div>
      </header>

      <div v-if="error" class="alert danger"><ShieldAlert :size="18" /><span>{{ error }}</span><button title="关闭" @click="error = ''"><X :size="17" /></button></div>

      <section v-if="active === 'dashboard'" class="page-stack">
        <div class="metric-grid">
          <article><span>模拟总资产</span><strong>{{ formatMoney(account?.total_asset) }}</strong><small>可用 {{ formatMoney(account?.available_cash) }}</small></article>
          <article><span>持仓盈亏</span><strong :class="(account?.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'">{{ formatMoney(account?.unrealized_pnl) }}</strong><small>已实现 {{ formatMoney(account?.realized_pnl) }}</small></article>
          <article><span>特别关注</span><strong>{{ dashboard?.watchlist_count || 0 }}</strong><small>只股票</small></article>
          <article><span>策略运行</span><strong>{{ runs.length }}</strong><small>累计运行记录</small></article>
        </div>

        <div class="split-grid">
          <section class="panel">
            <div class="section-head"><div><h2>特别关注</h2><span>最新行情</span></div><button class="text-action" @click="active='watchlist'">查看全部<ChevronRight :size="16" /></button></div>
            <div class="table-wrap"><table><thead><tr><th>股票</th><th>现价</th><th>涨跌</th><th>成交额</th></tr></thead><tbody>
              <tr v-for="item in watchlist.slice(0,5)" :key="item.id"><td><strong>{{ item.stock.name }}</strong><small>{{ item.stock.symbol }}</small></td><td>{{ item.stock.last_price?.toFixed(2) }}</td><td :class="item.stock.change_pct >= 0 ? 'positive' : 'negative'">{{ item.stock.change_pct?.toFixed(2) }}%</td><td>{{ formatMoney(item.stock.turnover_amount) }}</td></tr>
              <tr v-if="!watchlist.length"><td colspan="4" class="empty">暂无特别关注股票</td></tr>
            </tbody></table></div>
          </section>
          <section class="panel">
            <div class="section-head"><div><h2>系统状态</h2><span>数据与交易通道</span></div><Gauge :size="19" /></div>
            <div class="status-list">
              <div v-for="source in dataSources" :key="source.id"><span><Database :size="16" />{{ source.provider }}</span><b :class="source.healthy ? 'ok' : 'muted'">{{ source.healthy ? '正常' : '未配置' }}</b></div>
              <div v-for="gateway in gateways" :key="gateway.id"><span><Activity :size="16" />{{ gateway.name }}</span><b :class="gateway.healthy ? 'ok' : 'muted'">{{ gateway.healthy ? '已连接' : '未连接' }}</b></div>
            </div>
          </section>
        </div>

        <section class="panel action-band"><div><Activity :size="20" /><span><strong>一夜持股法</strong><small>模拟盘 · 1分钟行情 · 默认风控</small></span></div><button class="primary" @click="runStrategy"><Play :size="17" />手动运行</button></section>
      </section>

      <section v-else-if="active === 'watchlist'" class="page-stack">
        <section class="toolbar-band">
          <div class="search-box"><Search :size="18" /><input v-model="searchQuery" @input="searchStocks" placeholder="搜索股票名称、代码或拼音" /></div>
          <div v-if="searchResults.length" class="search-results">
            <button v-for="stock in searchResults" :key="stock.symbol" @click="addStock(stock)"><span><strong>{{ stock.name }}</strong><small>{{ stock.symbol }}</small></span><Plus :size="17" /></button>
          </div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>特别关注列表</h2><span>{{ watchlist.length }} 只股票</span></div><button class="secondary" @click="refreshWatchlist"><RefreshCw :size="15" />刷新行情</button></div>
          <div class="table-wrap"><table><thead><tr><th>股票</th><th>现价</th><th>涨跌幅</th><th>状态</th><th></th></tr></thead><tbody>
            <tr v-for="item in watchlist" :key="item.id"><td><strong>{{ item.stock.name }}</strong><small>{{ item.stock.symbol }}</small></td><td>{{ item.stock.last_price == null ? '暂无行情' : item.stock.last_price.toFixed(2) }}</td><td :class="item.stock.change_pct == null ? 'muted' : item.stock.change_pct >= 0 ? 'positive' : 'negative'">{{ item.stock.change_pct == null ? '—' : `${item.stock.change_pct.toFixed(2)}%` }}</td><td><span :class="['tag', item.stock.quote_updated_at ? '' : 'danger-tag']">{{ item.stock.quote_updated_at ? item.stock.status : '数据缺失' }}</span></td><td><button class="icon-action danger-text" title="取消关注" @click="removeStock(item.id)"><Trash2 :size="17" /></button></td></tr>
            <tr v-if="!watchlist.length"><td colspan="5" class="empty">暂无特别关注股票</td></tr>
          </tbody></table></div>
        </section>
      </section>

      <section v-else-if="active === 'strategies'" class="page-stack">
        <section class="toolbar-band">
          <button class="secondary" @click="syncStocks"><RefreshCw :size="15" />同步股票主数据</button>
          <button class="secondary" @click="syncEvents"><RefreshCw :size="15" />同步公司事件</button>
          <button class="secondary" @click="refreshRealtimeStatus"><RefreshCw :size="15" />轮询实时报价</button>
          <span class="tag">{{ marketCalendar?.is_trading_day ? '交易日' : '非交易日' }}</span>
          <span class="tag">实时报价 {{ dataSources.find(source => source.provider === 'akshare')?.stale_after_seconds || 15 }} 秒过期</span>
        </section>
        <section class="toolbar-band form-band">
          <label>配置名称<input v-model="strategyForm.name" /></label>
          <label>模式<select v-model="strategyForm.mode"><option value="SIMULATION">模拟盘</option><option value="LIVE">真实盘</option></select></label>
          <label>最大候选<input v-model.number="strategyForm.max_candidates" type="number" min="1" max="10" /></label>
          <label>仓位占比<input v-model.number="strategyForm.target_position_pct" type="number" min="0.05" max="1" step="0.05" /></label>
          <button class="primary" @click="createStrategyConfig"><Plus :size="15" />创建策略配置</button>
        </section>
        <section class="strategy-row" v-for="strategy in strategies" :key="strategy.id">
          <div class="strategy-main"><span class="strategy-icon"><TrendingUp :size="21" /></span><div><h2>{{ strategy.name }}</h2><p>{{ strategy.key }} · {{ strategy.version }} · {{ strategy.required_timeframes.join(' / ') }}</p></div></div>
          <div v-if="strategy.key === 'trading_agents_auto'" class="strategy-stats"><span>分析时间<b>13:30</b></span><span>调仓时间<b>14:45</b></span><span>最大持仓<b>5</b></span></div>
          <div v-else class="strategy-stats"><span>入场窗口<b>14:45-14:55</b></span><span>次日退出<b>09:35-09:45</b></span><span>最大候选<b>3</b></span></div>
          <button v-if="strategy.key === 'trading_agents_auto'" class="primary" :disabled="!agentsReadiness?.ready" @click="runAgentsBatch"><Play :size="17" />创建分析批次</button>
          <button v-else class="primary" @click="runStrategy"><Play :size="17" />运行模拟</button>
        </section>
        <div class="split-grid agents-grid">
          <section class="panel">
            <div class="section-head"><div><h2>TradingAgents 配置</h2><span>独立模拟账户</span></div><Settings2 :size="19" /></div>
            <div class="config-grid">
              <label>分析档位<select v-model="agentsForm.analysis_profile"><option v-for="(profile, key) in agentsProfiles.analysis_profiles" :key="key" :value="key">{{ profile.label }}</option></select></label>
              <label>仓位映射<select v-model="agentsForm.position_mapping"><option v-for="(label, key) in agentsProfiles.position_mappings" :key="key" :value="key">{{ label }}</option></select></label>
              <label>模拟账户<select v-model.number="agentsForm.simulation_account_id"><option v-for="item in simulationAccounts" :key="item.id" :value="item.id" :disabled="!item.available_for_trading_agents">{{ item.name }} · {{ formatMoney(item.total_asset) }}{{ item.available_for_trading_agents ? '' : ' · 已占用' }}</option></select></label>
              <label>快速模型<input v-model="agentsForm.quick_model" /></label>
              <label>深度模型<input v-model="agentsForm.deep_model" /></label>
              <label>Top N<input v-model.number="agentsForm.top_n" type="number" min="1" max="20" /></label>
              <label>预筛数量<input v-model.number="agentsForm.prefilter_size" type="number" min="10" /></label>
              <label>最大持仓<input v-model.number="agentsForm.max_positions" type="number" min="1" max="5" /></label>
              <label>并发数<input v-model.number="agentsForm.worker_concurrency" type="number" min="1" max="8" /></label>
              <label>单股超时<input v-model.number="agentsForm.candidate_timeout_seconds" type="number" min="60" step="30" /></label>
              <label>调用预算<input v-model.number="agentsForm.max_llm_calls" type="number" min="1" /></label>
              <label>输入 Token<input v-model.number="agentsForm.max_input_tokens" type="number" min="1000" step="1000" /></label>
              <label>输出 Token<input v-model.number="agentsForm.max_output_tokens" type="number" min="1000" step="1000" /></label>
              <label>补充数据超时<input v-model.number="agentsForm.enrichment_timeout_seconds" type="number" min="10" step="5" /></label>
            </div>
            <div class="control-row"><label class="check-control"><input v-model="agentsForm.enrichment_enabled" type="checkbox" />冻结 Yahoo 补充数据</label><label class="check-control"><input v-model="agentsForm.dry_run" type="checkbox" />无下单演练</label><button class="primary" @click="saveAgentsConfig"><Settings2 :size="16" />保存配置</button></div>
          </section>
          <section class="panel">
            <div class="section-head"><div><h2>就绪状态</h2><span>自动计划默认关闭</span></div><Activity :size="19" /></div>
            <div class="status-list">
              <div><span>OpenAI 密钥</span><b :class="agentsReadiness?.openai_configured ? 'ok' : 'negative'">{{ agentsReadiness?.openai_configured ? '已配置' : '未配置' }}</b></div>
              <div><span>兼容接口</span><b :class="agentsReadiness?.custom_endpoint_configured ? 'ok' : ''">{{ agentsReadiness?.custom_endpoint_configured ? '已配置' : '官方默认' }}</b></div>
              <div><span>固定依赖</span><b :class="agentsReadiness?.dependency_version_valid && agentsReadiness?.dependency_commit_valid ? 'ok' : 'negative'">{{ agentsReadiness?.dependency_version_valid && agentsReadiness?.dependency_commit_valid ? `v${agentsReadiness.dependency_version} · ${agentsReadiness.dependency_commit?.slice(0, 7)}` : agentsReadiness?.dependency_installed ? '版本或提交不符' : '未安装' }}</b></div>
              <div><span>模拟盘隔离</span><b :class="agentsReadiness?.simulation_only ? 'ok' : 'negative'">{{ agentsReadiness?.simulation_only ? '通过' : '未通过' }}</b></div>
              <div><span>完整演练</span><b :class="agentsReadiness?.dry_run_validated ? 'ok' : 'muted'">{{ agentsReadiness?.dry_run_validated ? `批次 #${agentsReadiness.last_dry_run_batch_id}` : '尚未完成' }}</b></div>
              <div><span>自动调度</span><b :class="agentsReadiness?.automation_ready ? 'ok' : 'muted'">{{ agentsReadiness?.automation_ready ? '可以启用' : '保持关闭' }}</b></div>
            </div>
            <button class="primary wide" :disabled="!agentsReadiness?.ready" @click="runAgentsBatch"><Play :size="17" />创建分析批次</button>
          </section>
        </div>
        <section class="panel">
          <div class="section-head"><div><h2>TradingAgents 批次</h2><span>候选、评级、预算与订单审计</span></div><RefreshCw :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>交易日</th><th>状态</th><th>档位</th><th>进度</th><th>调用</th><th>Token</th><th>耗时</th><th>订单</th></tr></thead><tbody>
            <tr v-for="batch in agentsBatches" :key="batch.id" @click="selectAgentsBatch(batch.id)"><td>#{{ batch.id }}</td><td>{{ batch.trading_date }}</td><td><span :class="['tag', ['failed','blocked','cancelled'].includes(batch.status) ? 'danger-tag' : '']">{{ batch.status }}</span></td><td>{{ batch.analysis_profile }}</td><td>{{ batch.analysis_status_counts?.completed || 0 }}/{{ batch.required_symbols?.length || 0 }}</td><td>{{ batch.llm_calls }}</td><td>{{ (batch.tokens_in || 0) + (batch.tokens_out || 0) }}</td><td>{{ durationText(batch.started_at, batch.completed_at) }}</td><td>{{ batch.order_ids?.length || 0 }}</td></tr>
            <tr v-if="!agentsBatches.length"><td colspan="9" class="empty">暂无 TradingAgents 批次</td></tr>
          </tbody></table></div>
        </section>
        <section v-if="selectedAgentsBatch" class="panel">
          <div class="section-head"><div><h2>批次 #{{ selectedAgentsBatch.id }}</h2><span>{{ selectedAgentsBatch.snapshot_sha256 || '无快照哈希' }}</span></div><div class="top-actions"><button v-if="selectedAgentsBatch.status === 'ready' && agentsForm.dry_run" class="primary" @click="dryRunAgentsBatch(selectedAgentsBatch.id)"><Play :size="15" />执行演练</button><button v-if="['pending','processing','ready'].includes(selectedAgentsBatch.status)" class="secondary" @click="cancelAgentsBatch(selectedAgentsBatch.id)"><X :size="15" />取消批次</button></div></div>
          <div class="table-wrap"><table><thead><tr><th>排名</th><th>股票</th><th>评级</th><th>AI 仓位</th><th>调用</th><th>Token</th><th>耗时</th><th>状态</th><th>报告</th></tr></thead><tbody>
            <template v-for="item in selectedAgentsBatch.analyses || []" :key="item.id"><tr><td>{{ item.rank || '持仓' }}</td><td><strong>{{ item.name }}</strong><small>{{ item.symbol }}</small></td><td>{{ item.rating || '—' }}</td><td>{{ item.ai_target_weight == null ? '—' : formatPct(item.ai_target_weight) }}</td><td>{{ item.stats?.llm_calls || 0 }}</td><td>{{ (item.stats?.tokens_in || 0) + (item.stats?.tokens_out || 0) }}</td><td>{{ durationText(item.started_at, item.finished_at) }}</td><td>{{ item.status }}</td><td><button class="secondary" :disabled="!item.report" @click="expandedAgentReportId = expandedAgentReportId === item.id ? null : item.id">{{ expandedAgentReportId === item.id ? '收起' : '查看' }}</button></td></tr><tr v-if="expandedAgentReportId === item.id"><td colspan="9"><pre class="agent-report">{{ item.report }}</pre></td></tr></template>
          </tbody></table></div>
          <div v-if="selectedAgentsBatch.portfolio_decision" class="decision-band"><span><strong>目标组合</strong><small>{{ selectedAgentsBatch.portfolio_decision.rationale }}</small></span><span v-for="(weight, symbol) in selectedAgentsBatch.portfolio_decision.target_weights" :key="symbol" class="tag">{{ symbol }} {{ formatPct(weight) }}</span></div>
          <div v-if="selectedAgentsBatch.orders?.length" class="table-wrap"><table><thead><tr><th>订单</th><th>股票</th><th>方向</th><th>数量</th><th>状态</th><th>提交时间</th></tr></thead><tbody><tr v-for="order in selectedAgentsBatch.orders" :key="order.id"><td>#{{ order.id }}</td><td><strong>{{ order.name }}</strong><small>{{ order.symbol }}</small></td><td>{{ order.side === 'buy' ? '买入' : '卖出' }}</td><td>{{ order.quantity }}</td><td><span class="tag">{{ order.status }}</span></td><td>{{ shortTime(order.submitted_at) }}</td></tr></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>策略配置</h2><span>内置策略实例与模式</span></div><TrendingUp :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>名称</th><th>模式</th><th>最大候选</th><th>仓位占比</th><th>状态</th></tr></thead><tbody>
            <tr v-for="config in strategyConfigs" :key="config.id"><td>#{{ config.id }}</td><td>{{ config.name }}</td><td>{{ config.mode }}</td><td>{{ config.parameters?.max_candidates ?? '—' }}</td><td>{{ formatPct(config.parameters?.target_position_pct) }}</td><td><span :class="['tag', config.enabled ? '' : 'danger-tag']">{{ config.enabled ? '启用' : '停用' }}</span></td></tr>
            <tr v-if="!strategyConfigs.length"><td colspan="6" class="empty">暂无策略配置</td></tr>
          </tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>调度控制</h2><span>交易日与入场/退出窗口</span></div><span class="tag">{{ marketCalendar?.is_trading_day ? '交易日' : '非交易日' }}</span></div>
          <div class="table-wrap"><table><thead><tr><th>触发类型</th><th>时间</th><th>状态</th><th>上次窗口</th><th></th></tr></thead><tbody>
            <tr v-for="schedule in strategySchedules" :key="schedule.id"><td>{{ schedule.trigger_type }}</td><td>{{ schedule.run_time }}</td><td><span :class="['tag', schedule.enabled ? '' : 'danger-tag']">{{ schedule.enabled ? '已启用' : '已停用' }}</span></td><td>{{ schedule.last_scheduled_for || '—' }}</td><td><button class="secondary" @click="toggleSchedule(schedule)">{{ schedule.enabled ? '停用' : '启用' }}</button></td></tr>
            <tr v-if="!strategySchedules.length"><td colspan="5" class="empty">暂无调度配置</td></tr>
          </tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>市场数据状态</h2><span>实时行情与同步新鲜度</span></div><Database :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>数据源</th><th>健康</th><th>最近报价</th><th>最近检查</th><th>错误</th></tr></thead><tbody>
            <tr v-for="source in dataSources" :key="source.id"><td>{{ source.provider }}</td><td><span :class="['tag', source.healthy ? '' : 'danger-tag']">{{ source.healthy ? '正常' : '异常' }}</span></td><td>{{ shortTime(source.last_quote_at) }}</td><td>{{ shortTime(source.last_checked_at) }}</td><td>{{ source.last_error || '—' }}</td></tr>
          </tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>关注股实时状态</h2><span>尾盘策略的报价新鲜度</span></div><Activity :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>股票</th><th>报价时间</th><th>状态</th></tr></thead><tbody>
            <tr v-for="item in realtimeStatus" :key="item.symbol"><td><strong>{{ item.name }}</strong><small>{{ item.symbol }}</small></td><td>{{ shortTime(item.quote_at) }}</td><td><span :class="['tag', item.stale ? 'danger-tag' : '']">{{ item.stale ? '已过期' : '新鲜' }}</span></td></tr>
            <tr v-if="!realtimeStatus.length"><td colspan="3" class="empty">暂无关注股实时报价</td></tr>
          </tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>最近运行</h2><span>策略执行与风控结果</span></div><Activity :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>模式</th><th>状态</th><th>开始时间</th><th>结果</th></tr></thead><tbody>
            <tr v-for="run in runs" :key="run.id"><td>#{{ run.id }}</td><td><span class="tag">{{ run.mode }}</span></td><td>{{ run.status }}</td><td>{{ shortTime(run.started_at) }}</td><td>{{ runResult(run) }}</td></tr>
            <tr v-if="!runs.length"><td colspan="5" class="empty">暂无运行记录</td></tr>
          </tbody></table></div>
        </section>
      </section>

      <section v-else-if="active === 'backtests'" class="page-stack">
        <section class="toolbar-band form-band">
          <label>开始日期<input v-model="backtestForm.start_date" type="date" /></label><label>结束日期<input v-model="backtestForm.end_date" type="date" /></label><label>初始资金<input v-model.number="backtestForm.initial_cash" type="number" min="1000" step="1000" /></label>
          <button class="primary" @click="runBacktest"><Play :size="17" />开始回测</button>
        </section>
        <section class="panel">
          <div class="section-head"><div><h2>回测记录</h2><span>1分钟数据 · A股交易规则</span></div><BookOpenCheck :size="19" /></div>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>区间</th><th>累计收益</th><th>最大回撤</th><th>夏普</th><th>胜率</th><th>状态</th></tr></thead><tbody>
            <tr v-for="item in backtests" :key="item.id" @click="selectBacktest(item.id)"><td>#{{ item.id }}</td><td>{{ item.start_date }} → {{ item.end_date }}</td><td :class="(item.metrics?.cumulative_return || 0) >= 0 ? 'positive' : 'negative'">{{ formatPct(item.metrics?.cumulative_return) }}</td><td class="negative">{{ formatPct(item.metrics?.max_drawdown) }}</td><td>{{ item.metrics?.sharpe_ratio ?? '—' }}</td><td>{{ formatPct(item.metrics?.win_rate) }}</td><td><span class="tag">{{ item.status }}</span></td></tr>
            <tr v-if="!backtests.length"><td colspan="7" class="empty">暂无回测记录</td></tr>
          </tbody></table></div>
        </section>
        <section class="panel" v-if="selectedBacktest">
          <div class="section-head"><div><h2>回测详情</h2><span>#{{ selectedBacktest.id }} · {{ selectedBacktest.symbol || selectedBacktest.benchmark_symbol }}</span></div><BookOpenCheck :size="19" /></div>
          <div class="status-list">
            <div><span>累计收益</span><b :class="(selectedBacktest.metrics?.cumulative_return || 0) >= 0 ? 'ok' : 'negative'">{{ formatPct(selectedBacktest.metrics?.cumulative_return) }}</b></div>
            <div><span>年化收益</span><b>{{ formatPct(selectedBacktest.metrics?.annualized_return) }}</b></div>
            <div><span>换手率</span><b>{{ formatPct(selectedBacktest.metrics?.turnover) }}</b></div>
            <div><span>暴露度</span><b>{{ formatPct(selectedBacktest.metrics?.exposure) }}</b></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>曲线时间</th><th>权益</th></tr></thead><tbody>
            <tr v-for="point in selectedBacktest.equity_curve || []" :key="point.timestamp"><td>{{ shortTime(point.timestamp) }}</td><td>{{ formatMoney(point.equity) }}</td></tr>
            <tr v-if="!(selectedBacktest.equity_curve || []).length"><td colspan="2" class="empty">暂无曲线数据</td></tr>
          </tbody></table></div>
          <div class="table-wrap"><table><thead><tr><th>方向</th><th>股票</th><th>数量</th><th>价格</th><th>时间</th><th>原因</th></tr></thead><tbody>
            <tr v-for="trade in selectedBacktest.trades || []" :key="trade.id"><td :class="trade.side === 'buy' ? 'positive' : 'negative'">{{ trade.side }}</td><td>{{ trade.symbol }}</td><td>{{ trade.quantity }}</td><td>{{ trade.fill_price }}</td><td>{{ shortTime(trade.filled_at) }}</td><td>{{ trade.reason }}</td></tr>
            <tr v-if="!(selectedBacktest.trades || []).length"><td colspan="6" class="empty">暂无成交明细</td></tr>
          </tbody></table></div>
        </section>
      </section>

      <section v-else-if="active === 'trading'" class="page-stack">
        <div class="metric-grid three"><article><span>现金余额</span><strong>{{ formatMoney(account?.cash_balance) }}</strong><small>模拟账户</small></article><article><span>总资产</span><strong>{{ formatMoney(account?.total_asset) }}</strong><small>初始 {{ formatMoney(account?.initial_cash) }}</small></article><article><span>持仓数量</span><strong>{{ positions.length }}</strong><small>可卖数量遵循 T+1</small></article></div>
        <section class="panel"><div class="section-head"><div><h2>当前持仓</h2><span>模拟盘</span></div><CircleDollarSign :size="19" /></div><div class="table-wrap"><table><thead><tr><th>股票</th><th>数量</th><th>可卖</th><th>成本</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody><tr v-for="p in positions" :key="p.id"><td>{{ p.symbol }}</td><td>{{ p.quantity }}</td><td>{{ p.available_quantity }}</td><td>{{ p.average_cost.toFixed(3) }}</td><td>{{ formatMoney(p.market_value) }}</td><td :class="p.unrealized_pnl >= 0 ? 'positive' : 'negative'">{{ formatMoney(p.unrealized_pnl) }}</td></tr><tr v-if="!positions.length"><td colspan="6" class="empty">暂无持仓</td></tr></tbody></table></div></section>
        <section class="panel"><div class="section-head"><div><h2>订单记录</h2><span>模拟盘与真实盘统一视图</span></div><WalletCards :size="19" /></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>股票</th><th>方向</th><th>数量</th><th>模式</th><th>状态</th><th>时间</th></tr></thead><tbody><tr v-for="o in orders" :key="o.id"><td>#{{ o.id }}</td><td>{{ o.symbol }}</td><td :class="o.side === 'buy' ? 'positive' : 'negative'">{{ o.side }}</td><td>{{ o.quantity }}</td><td>{{ o.mode }}</td><td><span class="tag">{{ o.status }}</span></td><td>{{ shortTime(o.created_at) }}</td></tr><tr v-if="!orders.length"><td colspan="7" class="empty">暂无订单</td></tr></tbody></table></div></section>
      </section>

      <section v-else-if="active === 'risk'" class="page-stack">
        <section class="risk-banner"><div><ShieldAlert :size="22" /><span><strong>{{ dashboard?.mode === 'LIVE' ? '真实盘已启用' : '真实盘默认关闭' }}</strong><small>只有健康、授权且通过风控的 BrokerAdapter 才能下单</small></span></div><div class="top-actions"><button class="secondary" @click="setLiveMode(dashboard?.mode !== 'LIVE')">{{ dashboard?.mode === 'LIVE' ? '关闭真实盘' : '启用真实盘' }}</button><button class="danger-button" @click="emergencyStop"><ShieldAlert :size="17" />紧急停止</button></div></section>
        <div class="split-grid">
          <section class="panel"><div class="section-head"><div><h2>风控配置</h2><span>百分比与绝对值取更低者</span></div><Settings2 :size="19" /></div><div class="status-list"><div v-for="risk in riskSettings" :key="risk.id"><span><b>{{ risk.mode }}</b></span><span>单笔 {{ formatMoney(risk.max_order_notional_abs) }} / {{ formatPct(risk.max_order_notional_pct) }} · 总敞口 {{ formatPct(risk.max_total_exposure_pct) }}</span></div></div></section>
          <section class="panel"><div class="section-head"><div><h2>交易网关</h2><span>跨平台适配器</span></div><Activity :size="19" /></div><div class="status-list"><div v-for="g in gateways" :key="g.id"><span><b>{{ g.name }}</b><small>{{ g.adapter_name }} · {{ g.type }} · {{ g.platform }} · {{ (g.capabilities || []).join(' / ') || '—' }}</small></span><b :class="g.healthy ? 'ok' : 'muted'">{{ g.status === 'healthy' ? '健康' : '未连接' }}</b></div></div></section>
        </div>
        <section class="panel"><div class="section-head"><div><h2>真实盘账户</h2><span>仅保存券商账号掩码</span></div><button class="secondary" @click="syncLiveAccounts"><RefreshCw :size="15" />同步账户</button></div><div class="table-wrap"><table><thead><tr><th>券商</th><th>账户</th><th>币种</th><th>市场权限</th><th>能力</th><th>权限</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="account in liveAccounts" :key="account.id"><td>{{ account.broker }}</td><td><strong>{{ account.account_alias }}</strong><small>{{ account.account_no_masked }}</small></td><td>{{ account.currency }}</td><td>{{ (account.market_permissions || []).join(' / ') || '—' }}</td><td>{{ (account.account_capabilities || []).join(' / ') || '—' }}</td><td>{{ account.read_only ? '只读' : '可交易' }}</td><td><span :class="['tag', account.enabled ? '' : 'danger-tag']">{{ account.enabled ? '已启用' : '未启用' }}</span></td><td><button class="secondary" @click="toggleLiveAccount(account)">{{ account.enabled ? '停用' : '启用' }}</button></td></tr><tr v-if="!liveAccounts.length"><td colspan="8" class="empty">暂无真实盘账户</td></tr></tbody></table></div></section>
        <section class="panel"><div class="section-head"><div><h2>风控事件</h2><span>最近 200 条</span></div><ShieldAlert :size="19" /></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>模式</th><th>类型</th><th>说明</th></tr></thead><tbody><tr v-for="item in riskEvents" :key="item.id"><td>{{ shortTime(item.created_at) }}</td><td>{{ item.mode }}</td><td><span class="tag danger-tag">{{ item.event_type }}</span></td><td>{{ item.message }}</td></tr><tr v-if="!riskEvents.length"><td colspan="4" class="empty">暂无风控事件</td></tr></tbody></table></div></section>
      </section>

      <section v-else-if="active === 'notifications'" class="page-stack">
        <section class="toolbar-band form-band notification-form">
          <label>渠道<select v-model="notificationForm.type"><option value="email">邮件</option><option value="wecom">企业微信</option></select></label><label>名称<input v-model="notificationForm.name" placeholder="交易告警" /></label><label>接收方<input v-model="notificationForm.recipient" placeholder="邮箱或群名称" /></label><label>密钥引用<input v-model="notificationForm.secret_ref" placeholder="env:SMTP_PASSWORD" /></label><button class="primary" @click="addChannel"><Plus :size="17" />添加</button>
        </section>
        <section class="panel"><div class="section-head"><div><h2>通知渠道</h2><span>邮件与企业微信</span></div><Bell :size="19" /></div><div class="channel-list"><div v-for="ch in channels" :key="ch.id"><span class="channel-icon"><Bell :size="17" /></span><span><strong>{{ ch.name }}</strong><small>{{ ch.type }} · {{ ch.recipient }}</small></span><button class="secondary" @click="testChannel(ch.id)"><Play :size="15" />测试</button></div><p v-if="!channels.length" class="empty">暂无通知渠道</p></div></section>
        <section class="panel"><div class="section-head"><div><h2>投递记录</h2><span>失败不阻塞交易</span></div><Activity :size="19" /></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>级别</th><th>状态</th><th>主题</th></tr></thead><tbody><tr v-for="d in deliveries" :key="d.id"><td>{{ shortTime(d.created_at) }}</td><td>{{ d.event_type }}</td><td>{{ d.severity }}</td><td><span class="tag">{{ d.status }}</span></td><td>{{ d.subject }}</td></tr><tr v-if="!deliveries.length"><td colspan="5" class="empty">暂无投递记录</td></tr></tbody></table></div></section>
      </section>
    </main>
    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>
