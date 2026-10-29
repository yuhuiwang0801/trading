import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import ReversalResearch from './ReversalResearch'
import ActiveStrategy, { ReversalCandidates } from './ActiveStrategy'
import ManagedPolicy, { managedPolicyBlockers } from './ManagedPolicy'
import { formatNumber as num, formatMoney as money, formatDetailValue, formatDetailJson } from './format'
import {
  Activity, ArrowDownLeft, ArrowRight, ArrowUpRight, Bell, Check,
  ChevronDown, ChevronRight, Clock3, 
  FlaskConical, Globe2, LayoutDashboard, Loader2, Menu, MoreHorizontal,
  Play, Radio, RefreshCw, Search, Settings2, ShieldCheck, 
  Square, Wallet, X, AlertTriangle,
  ScanLine, Layers, FileText, Link2, LockKeyhole,
} from 'lucide-react'

type Item = Record<string, any>
type Page = 'overview' | 'screener' | 'portfolio' | 'signals' | 'orders' | 'strategy' | 'audit' | 'settings'
type Snapshot = {
  account: Item; settings: Item; candidates: Item[]; positions: Item[]; signals: Item[];
  intents: Item[]; orders: Item[]; fills: Item[]; audit: Item[]; runs: Item[];
  market: Item; provider: Item; worker: Item; backtests: Item[]; jobs: Item[]; market_verification: Item | null; equity_curve: Item[]; demo_clock?: string; data_timestamp?: string;
}
const empty: Snapshot = { account: {}, settings: { mode: 'paper', params: {} }, candidates: [], positions: [], signals: [], intents: [], orders: [], fills: [], audit: [], runs: [], market: {}, provider: {}, worker: {}, backtests: [], jobs: [], market_verification: null, equity_curve: [] }
const nav: { id: Page; zh: string; en: string; icon: typeof Activity }[] = [
  { id: 'overview', zh: '概览', en: 'Overview', icon: LayoutDashboard },
  { id: 'screener', zh: '选股', en: 'Screener', icon: ScanLine },
  { id: 'portfolio', zh: '持仓', en: 'Portfolio', icon: Wallet },
  { id: 'signals', zh: '信号', en: 'Signals', icon: Radio },
  { id: 'orders', zh: '订单', en: 'Orders', icon: Layers },
  { id: 'strategy', zh: '策略与回测', en: 'Strategy & backtests', icon: FlaskConical },
  { id: 'audit', zh: '日志', en: 'Audit trail', icon: FileText },
]
const n = (value: any) => Number(value ?? 0)
const filled = (order: Item) => ['filled', 'cancelled', 'canceled', 'rejected', 'expired', 'abandoned', 'preview_blocked', 'failed'].includes(String(order.status).toLowerCase())
const statusKey = (value: any) => String(value || '').toLowerCase()

const liveNoteLabels: Record<string, [string, string]> = {
  SERVER_WRITE_DISABLED: ['当前服务暂不支持开启实盘', 'Live activation is unavailable'], LIVE_SCOPE_NOT_CONFIGURED: ['交易范围尚未保存', 'Trading scope has not been saved'],
  ACCOUNT_NOT_RECONCILED: ['真实账户尚未同步', 'Live account has not been synchronized'], ACCOUNT_SNAPSHOT_STALE: ['真实账户快照已过期', 'Live account snapshot has expired'],
  ACCOUNT_INCOMPATIBLE: ['账户尚不符合策略要求', 'Account does not meet strategy requirements'], ACCOUNT_CHANGED: ['账户已变化，请重新核对', 'The account has changed; review it again'],
  NO_UNLEVERAGED_BUYING_POWER: ['可用现金购买力不足', 'Insufficient unleveraged buying power'], SCOPE_PREVIEW_REQUIRED: ['请重新检查 V2 策略的实盘配置', 'Recheck the V2 live configuration'],
  SCOPE_PREVIEW_STALE: ['请重新检查 V2 策略的实盘配置', 'Recheck the V2 live configuration'], UNKNOWN_INSTRUCTION: ['存在结果待确认的订单，已阻止新提交', 'An order outcome is unknown; new submissions are blocked'],
  broker_order_alert: ['券商返回订单限制，请查看详情', 'The broker returned an order alert; review the details'], position_closing_only: ['此证券目前仅允许平仓', 'This security currently allows closing positions only'],
  quote_unavailable: ['券商报价暂不可用', 'Broker quote is unavailable'], quote_not_tradable: ['当前报价不满足交易条件', 'The quote does not meet trading conditions'],
  market_disclosure_unavailable: ['券商行情说明缺失', 'Broker market-data disclosure is missing'],
  fractional_not_tradable: ['此证券暂不支持金额或零股交易', 'Amount or fractional trading is unavailable for this security'],
  fractional_value_below_minimum: ['按当前盘口估算，零股金额不足 $1', 'The fractional order is worth less than $1 at the current quote'],
  worker_restart_required: ['后台版本需要更新，请重启全部服务', 'Restart all services to load the current worker version'], worker_offline: ['后台巡检服务不在线', 'The background worker is offline'],
  worker_ledger_mismatch: ['后台执行记录不一致，请重启全部服务', 'Execution records differ across services; restart all services'],
  worker_source_mismatch: ['后台尚未使用真实行情', 'The background worker is not using real market data'],
  real_market_source_required: ['请先接入真实行情', 'Connect a real market-data feed first'], authorization_required: ['请先完成 Robinhood 授权', 'Complete Robinhood authorization first'],
  UI_CHECK_PENDING: ['正在刷新账户和开启条件', 'Refreshing the account and activation checks'],
  UI_CHECK_FAILED: ['账户检查未完成，请检查授权并重新检查', 'Account verification failed; review authorization and check again'],
  UI_CONNECTION_LOST: ['服务状态待确认，请重新检查', 'Service status is unconfirmed; check again'],
  UI_CAPABILITY_UNAVAILABLE: ['当前服务暂不支持开启实盘', 'Live activation is unavailable'],
  UI_SCOPE_DIRTY: ['交易范围有未保存修改，请返回保存后再检查', 'The scope has unsaved changes; go back, save it, and check again'],
  UI_SCOPE_CHANGED: ['交易范围已变化，请重新检查并核对', 'The saved scope changed; check and review it again'],
  UI_POLICY_UNCONFIRMED: ['策略自动交易规则尚未确认，请重新检查', 'The strategy-managed execution rules are unconfirmed; check again'],
  UI_POLICY_CHANGED: ['策略的股票范围或仓位规则已变化，请重新检查', 'The strategy universe or allocation rules changed; check again'],
  STRATEGY_POLICY_CHANGED: ['策略规则已更新，请重新检查后确认开启', 'The strategy policy changed; check again before activation'],
  EXTERNAL_ORDER_ACTIVITY: ['发现账户外部订单变化，需要完成账户核对', 'External order activity requires account reconciliation'],
  EXTERNAL_POSITION_ACTIVITY: ['发现账户外部持仓变化，需要完成账户核对', 'External position activity requires account reconciliation'],
  UI_ACCOUNT_CHANGED: ['真实账户已变化，请重新检查并核对', 'The live account changed; check and review it again'],
  UI_CHECK_STALE: ['开启检查已过期，请重新检查', 'Activation checks expired; check again'],
  UI_NOT_READY: ['开启条件尚未全部通过，请重新检查', 'Activation checks are incomplete; check again'],
  selected_strategy_scope_mismatch: ['选用策略与确认的交易范围不一致，请重新检查', 'The selected strategy differs from the confirmed scope; check again'],
  reversal_daily_data_not_ready: ['V2 每日行情和基本面资料尚未就绪，请在策略页查看更新状态', 'V2 daily data and fundamentals are not ready; review the Strategy page'],
  UI_STRATEGY_UNCONFIRMED: ['当前策略状态待确认，请刷新后重新检查', 'Confirm the active strategy, then check again'],
  UI_STRATEGY_CHANGED: ['选用策略已变化，请重新检查', 'The selected strategy changed; check again'],
  UI_ALREADY_ARMED: ['实盘已开启，请关闭此窗口查看运行状态', 'Live automation is already active; close this dialog to view its status'],
}

function Pill({ children, tone = 'neutral', dot = false }: { children: ReactNode; tone?: string; dot?: boolean }) {
  return <span className={`pill ${tone}`}>{dot && <span className="status-dot" />}{children}</span>
}
function Empty({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <div className="empty-state"><strong>{title}</strong>{description && <p>{description}</p>}{action}</div>
}
export default function App() {
  const [language, setLanguage] = useState<'zh' | 'en'>(() => localStorage.getItem('northstar-language') === 'en' ? 'en' : 'zh')
  const [page, setPage] = useState<Page>(() => { const hash = window.location.hash.slice(1); return [...nav.map(x => x.id), 'settings'].includes(hash) ? hash as Page : 'overview' })
  const [baseState, setBaseState] = useState<Snapshot>(empty)
  const [liveWorkspace, setLiveWorkspace] = useState<Item | null>(null)
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null)
  const [query, setQuery] = useState('')
  const [mobileMenu, setMobileMenu] = useState(false)
  const [timezone, setTimezone] = useState('America/New_York')
  const [clock, setClock] = useState(new Date())
  const [detail, setDetail] = useState<{ title: string; item: Item; informational?: boolean } | null>(null)
  const [notifications, setNotifications] = useState(false)
  const [filter, setFilter] = useState('all')
  const [brokerConnection, setBrokerConnection] = useState<Item | null>(null)
  const [brokerStatusError, setBrokerStatusError] = useState(false)
  const [liveState, setLiveState] = useState<Item | null>(null)
  const [liveStatusError, setLiveStatusError] = useState(false)
  const [strategyState, setStrategyState] = useState<Item | null>(null)
  const [strategyStatusError, setStrategyStatusError] = useState(false)
  const [liveArmConfirm, setLiveArmConfirm] = useState<Item | null>(null)
  // Trading pages use the live ledger exclusively, even when it is unavailable.
  const state: Snapshot = { ...baseState, candidates: strategyState?.active_strategy?.id === 'reversal_v2' ? strategyState?.runtime?.candidates || [] : [], account: liveWorkspace?.account || {}, positions: liveWorkspace?.positions || [], orders: liveWorkspace?.orders || [], fills: liveWorkspace?.fills || [], signals: (liveWorkspace?.intents || []).map((intent: Item) => ({ ...intent, source_kind: 'live_execution_signal', price: intent.evidence?.signal_price })), intents: liveWorkspace?.intents || [], audit: liveWorkspace?.audit || [], runs: liveWorkspace?.runs || [], equity_curve: [] }
  const [authenticationRequired, setAuthenticationRequired] = useState(false)
  const [sessionToken, setSessionToken] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  const nowDate = clock.toLocaleDateString(language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', weekday: 'short', timeZone: timezone })
  const currentTime = clock.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: timezone })
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const dateTime = (value: any, full = false) => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', { timeZone: timezone, ...(full ? { month: 'short', day: 'numeric' } : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) }
  const liveNote = (value: any) => { const message = typeof value === 'string' ? value : value?.message || value?.reason || value?.code; if (typeof message !== 'string') return t('请查看券商检查结果', 'Review the broker check result'); return liveNoteLabels[message] ? t(...liveNoteLabels[message]) : message }
  const request = useCallback(async (path: string, method = 'GET', body?: Item) => {
    const response = await fetch(`/api${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(90000) })
    const data = await response.json().catch(() => ({}))
    if (response.status === 401 && path === '/state') setAuthenticationRequired(true)
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.error || data.message || JSON.stringify(data.detail) || `HTTP ${response.status}`)
    return data
  }, [])
  const refresh = useCallback(async () => {
    try {
      const data = await request('/state')
      setBaseState({ ...empty, ...data, account: data.account || {}, settings: { mode: 'paper', params: {}, ...data.settings }, market: data.market || {}, provider: data.provider || {}, worker: data.worker || {} })
      setConnected(true); setError('')
      setAuthenticationRequired(false)
    } catch (err) { setConnected(false); setError(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }, [request])
  const refreshBrokerConnection = useCallback(async () => {
    try {
      const data = await request('/broker/connection')
      setBrokerConnection(data)
      setBrokerStatusError(false)
      return data
    } catch {
      setBrokerStatusError(true)
      return null
    }
  }, [request])
  const refreshLive = useCallback(async () => {
    try {
      const [data, workspace] = await Promise.all([request('/live'), request('/live/workspace')])
      setLiveState(data)
      setLiveWorkspace(workspace)
      setLiveStatusError(false)
      return data
    } catch {
      setLiveStatusError(true)
      return null
    }
  }, [request])
  const refreshStrategies = useCallback(async () => {
    try {
      const data = await request('/strategies')
      setStrategyState(data); setStrategyStatusError(false)
      return data
    } catch { setStrategyStatusError(true); return null }
  }, [request])
  useEffect(() => {
    if (!connected) return
    void refreshStrategies()
    const timer = window.setInterval(() => { void refreshStrategies() }, 15000)
    return () => window.clearInterval(timer)
  }, [connected, refreshStrategies])
  useEffect(() => {
    if (!connected) return
    void refreshLive()
    const timer = window.setInterval(() => { void refreshLive() }, 15000)
    return () => window.clearInterval(timer)
  }, [connected, refreshLive])
  useEffect(() => {
    if (page !== 'settings' || !connected) return
    void refreshBrokerConnection()
    const timer = window.setInterval(() => { void refreshBrokerConnection() }, 15000)
    return () => window.clearInterval(timer)
  }, [page, connected, refreshBrokerConnection])
  useEffect(() => {
    const url = new URL(window.location.href)
    const result = url.searchParams.get('broker')
    if (!['connected', 'authorization_failed'].includes(result || '')) return
    setPage('settings')
    setToast({
      message: result === 'connected'
        ? language === 'zh' ? 'Robinhood 已连接，可以检查 V2 实盘条件。' : 'Robinhood connected. Check V2 live readiness.'
        : language === 'zh' ? 'Robinhood 授权未完成，请重新连接。' : 'Robinhood authorization was not completed. Reconnect to try again.',
      error: result !== 'connected',
    })
    url.searchParams.delete('broker')
    url.hash = 'settings'
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
  }, [language])
  useEffect(() => {
    void refresh()
    const source = new EventSource('/api/events')
    source.onmessage = () => { void refresh() }
    source.addEventListener('state', () => { void refresh() })
    source.addEventListener('update', () => { void refresh() })
    const timer = window.setInterval(() => { void refresh() }, 15000)
    return () => { source.close(); window.clearInterval(timer) }
  }, [refresh])
  useEffect(() => { const timer = window.setInterval(() => setClock(new Date()), 1000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.slice(1)
      setPage([...nav.map(item => item.id), 'settings'].includes(hash) ? hash as Page : 'overview')
      setQuery(''); setFilter('all'); setMobileMenu(false)
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  useEffect(() => { localStorage.setItem('northstar-language', language); document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en' }, [language])
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 5500); return () => window.clearTimeout(timer) }, [toast])
  useEffect(() => { const listener = (e: KeyboardEvent) => { if (e.key === 'Escape') { if (detail) setDetail(null); else if (busy !== 'live-arm') setLiveArmConfirm(null); setNotifications(false) } if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); searchRef.current?.focus() } }; window.addEventListener('keydown', listener); return () => window.removeEventListener('keydown', listener) }, [busy, detail])
  const navigate = (next: Page) => { setPage(next); setQuery(''); setFilter('all'); setMobileMenu(false); window.location.hash = next }
  const act = async (key: string, path: string, body?: Item, method = 'POST', message?: string) => {
    setBusy(key)
    try { const result = await request(path, method, body); await refresh(); setToast({ message: message || t('已更新', 'Updated') }); return result }
    catch (err) { setToast({ message: err instanceof Error ? err.message : String(err), error: true }); return null }
    finally { setBusy('') }
  }
  const connectBroker = async () => {
    setBusy('broker-connect')
    try {
      const result = await request('/broker/connect', 'POST', {})
      const url = new URL(result.authorization_url)
      if (url.protocol !== 'https:' || (url.hostname !== 'robinhood.com' && !url.hostname.endsWith('.robinhood.com'))) throw new Error('Invalid authorization URL')
      window.location.assign(url.href)
    } catch {
      setToast({ message: t('暂时无法启动 Robinhood 授权，请稍后重试。', 'Robinhood authorization could not be started. Please try again.'), error: true })
      setBusy('')
      void refreshBrokerConnection()
    }
  }
  const verifyLive = async () => {
    setBusy('live-verify')
    try {
      const data = await request('/live/verify', 'POST', {})
      setLiveState(data)
      setLiveStatusError(false)
      await refreshLive()
      setToast({ message: t('真实账户与实盘检查已更新。', 'Live account and readiness checks refreshed.') })
    } catch (err) {
      setToast({ message: err instanceof Error ? err.message : t('实盘检查未完成，请重试。', 'Live checks could not be completed. Please retry.'), error: true })
      await refreshLive()
    } finally { setBusy('') }
  }
  const disarmLive = async () => {
    setBusy('live-disarm')
    try {
      await request('/live/disarm', 'POST', {})
      await refreshLive()
      setToast({ message: t('已停止实盘新提交，已有订单和持仓保留。', 'New live submissions stopped. Existing orders and positions remain.') })
    } catch (err) {
      setToast({ message: err instanceof Error ? err.message : t('停止状态待确认，请刷新检查。', 'Stop status is unconfirmed. Refresh to check.'), error: true })
    } finally { setBusy('') }
  }
  const beginLiveActivation = async () => {
    const selected = strategyState?.active_strategy
    if (busy || liveState?.armed || strategyStatusError || selected?.id !== 'reversal_v2') return
    setBusy('live-activation-check')
    setLiveArmConfirm({ checking: true, strategy_id: selected.id, strategy_version: selected.version })
    try {
      const data = await request('/live/prepare-strategy', 'POST', { strategy_id: selected.id })
      setLiveState(data)
      setLiveStatusError(false)
      setLiveArmConfirm(current => current ? { checking: false, verified_at: Date.now(), strategy_id: selected.id, strategy_version: selected.version, config: data.config, policy: data.config?.strategy_policy || data.strategy_policy, configuration_revision: data.configuration_revision, account_masked: data.account_masked } : null)
      await Promise.all([refreshLive(), refreshStrategies()])
    } catch (err) {
      setLiveArmConfirm(current => current ? { ...current, checking: false, verification_failed: true, error: err instanceof Error ? err.message : '' } : null)
    } finally { setBusy('') }
  }
  const liveActivationBlockers = (now = Date.now()) => {
    const blockers: string[] = Array.isArray(liveState?.readiness?.blockers) ? [...liveState.readiness.blockers] : []
    const recent = (value: any, milliseconds: number) => { const timestamp = typeof value === 'number' ? value : Date.parse(value || ''); return Number.isFinite(timestamp) && timestamp <= now && now - timestamp <= milliseconds }
    if (!liveArmConfirm || liveArmConfirm.checking) blockers.push('UI_CHECK_PENDING')
    if (liveArmConfirm?.verification_failed) blockers.push('UI_CHECK_FAILED')
    if (!connected || liveStatusError || !liveState) blockers.push('UI_CONNECTION_LOST')
    if (strategyStatusError || strategyState?.active_strategy?.id !== 'reversal_v2') blockers.push('UI_STRATEGY_UNCONFIRMED')
    if (!liveArmConfirm?.config?.strategy_id || liveArmConfirm.config.strategy_id !== strategyState?.active_strategy?.id || liveArmConfirm.strategy_id !== strategyState?.active_strategy?.id || liveArmConfirm.strategy_version !== strategyState?.active_strategy?.version) blockers.push('UI_STRATEGY_CHANGED')
    if (liveState?.activation_available !== true || liveState?.activation_mode !== 'ui_confirmation') blockers.push('UI_CAPABILITY_UNAVAILABLE')
    if (liveState?.armed === true) blockers.push('UI_ALREADY_ARMED')
    if (!Number.isInteger(liveArmConfirm?.configuration_revision) || liveArmConfirm?.configuration_revision !== liveState?.configuration_revision) blockers.push('UI_SCOPE_CHANGED')
    blockers.push(...managedPolicyBlockers(liveArmConfirm, liveState, strategyState?.strategies?.find((row: Item) => row.id === strategyState?.active_strategy?.id)?.execution_policy))
    if (!liveArmConfirm?.account_masked) blockers.push('ACCOUNT_NOT_RECONCILED')
    else if (liveArmConfirm.account_masked !== liveState?.account_masked) blockers.push('UI_ACCOUNT_CHANGED')
    if (!recent(liveState?.checked_at, 60000)) blockers.push(liveState?.checked_at ? 'ACCOUNT_SNAPSHOT_STALE' : 'ACCOUNT_NOT_RECONCILED')
    if (liveArmConfirm && !liveArmConfirm.checking && !liveArmConfirm.verification_failed && !recent(liveArmConfirm.verified_at, 60000)) blockers.push('UI_CHECK_STALE')
    if (liveState?.readiness?.ready !== true || liveState?.readiness?.checks?.some((check: Item) => check.status !== 'passed')) {
      if (!blockers.length) blockers.push('UI_NOT_READY')
    }
    return [...new Set(blockers)]
  }
  const armLive = async () => {
    if (busy || !liveArmConfirm || liveActivationBlockers(Date.now()).length) return
    setBusy('live-arm')
    try {
      await request('/live/arm', 'POST', { confirmation: 'ENABLE LIVE AUTO TRADING', configuration_revision: liveArmConfirm.configuration_revision })
      setLiveArmConfirm(null)
      await refreshLive()
      setToast({ message: t('实盘自动交易已启用，请留意运行状态。', 'Live automation armed. Monitor the execution status.') })
    } catch (err) {
      setToast({ message: err instanceof Error ? err.message : t('实盘未启用，请重新检查交易条件。', 'Live automation was not armed. Review readiness checks.'), error: true })
      setLiveArmConfirm(current => current ? { ...current, verification_failed: true } : null)
      await refreshLive()
    } finally { setBusy('') }
  }
  const refreshStrategyData = async () => {
    setBusy('strategy-refresh')
    try {
      await request('/strategies/refresh', 'POST', {})
      await refreshStrategies()
      setToast({ message: t('数据与信号更新已排队，不会提交订单。', 'Data and signal update queued. No orders will be submitted.') })
    } catch (err) { setToast({ message: err instanceof Error ? err.message : String(err), error: true }) }
    finally { setBusy('') }
  }
  const inspect = (title: string, item: Item) => setDetail({ title, item })
  const inspectInfo = (title: string, item: Item) => setDetail({ title, item, informational: true })
  const search = (items: Item[]) => query ? items.filter(item => JSON.stringify(item).toLowerCase().includes(query.toLowerCase())) : items
  const signalStatus = (value: any) => {
    const labels: Record<string, [string, string]> = { abandoned: ['未提交', 'Not submitted'], preview_blocked: ['预览拦截', 'Preview blocked'], awaiting_approval: ['待券商审批', 'Awaiting approval'], submitted: ['已提交', 'Submitted'], cancel_rejected: ['撤单被拒', 'Cancel rejected'], prepared: ['待预览', 'Prepared'], previewed: ['预览通过', 'Previewed'], market_closed: ['休市', 'Market closed'], degraded: ['行情异常', 'Data unavailable'], interrupted: ['已中断', 'Interrupted'], new: ['新信号', 'New'], read: ['已读', 'Read'], ignored: ['已忽略', 'Ignored'], expired: ['已失效', 'Expired'], pending: ['待处理', 'Pending'], queued: ['排队中', 'Queued'], failed: ['失败', 'Failed'], approved: ['风控通过', 'Approved'], blocked: ['已阻止', 'Blocked'], rejected: ['已拒绝', 'Rejected'], pending_submit: ['待提交', 'Pending'], pending_submission: ['待提交', 'Pending'], submitting: ['提交中', 'Submitting'], pending_approval: ['待审批', 'Approval required'], submission_unknown: ['结果未知', 'Unknown'], unknown: ['结果未知', 'Unknown'], accepted: ['已接受', 'Accepted'], open: ['已接受', 'Open'], partially_filled: ['部分成交', 'Partially filled'], filled: ['全部成交', 'Filled'], cancel_pending: ['撤单中', 'Cancel pending'], cancelling: ['撤单中', 'Cancelling'], cancelled: ['已撤销', 'Cancelled'], canceled: ['已撤销', 'Cancelled'], confirmed: ['已确认', 'Confirmed'], completed: ['已完成', 'Complete'], success: ['成功', 'Success'], paused: ['已暂停', 'Paused'], running: ['运行中', 'Running'], idle: ['等待中', 'Idle'] }
    const key = String(value || 'pending').toLowerCase(); const label = labels[key]
    return <Pill tone={['filled', 'approved', 'success', 'confirmed', 'completed'].includes(key) ? 'green' : ['blocked', 'rejected', 'submission_unknown', 'unknown', 'failed'].includes(key) ? 'red' : ['pending_approval', 'awaiting_approval', 'partially_filled', 'cancel_pending'].includes(key) ? 'amber' : ['accepted', 'submitted', 'new', 'running', 'open'].includes(key) ? 'blue' : 'neutral'} dot>{label ? t(...label) : String(value || '—')}</Pill>
  }
  const eventName = (a: Item) => {
    const key = a.event || a.event_type || a.action || a.type || ''
    const names: Record<string, [string, string]> = {
      SCAN_COMPLETE: ['巡检完成', 'Scan completed'], SCAN_DATA_GATE: ['交易条件检查', 'Trading conditions checked'],
      SCAN_FAILED: ['巡检失败', 'Scan failed'], RECONCILIATION: ['账户核对', 'Account reconciled'],
      CANDIDATE_POOL_UPDATED: ['候选池更新', 'Shortlist updated'], CASH_SETTLED: ['资金结算', 'Cash settled'],
      FILL_APPLIED: ['成交入账', 'Fill recorded'], INTENT_PERSISTED: ['生成订单意图', 'Order intent created'],
      ORDER_SUBMISSION_STARTED: ['提交订单', 'Order submission started'], ORDER_SUBMISSION_RESULT: ['订单提交结果', 'Submission result'],
      CANCEL_REQUESTED: ['申请撤单', 'Cancellation requested'], CANCEL_RESPONSE: ['撤单结果', 'Cancellation result'],
      CANCEL_AWAITING_RECONCILIATION: ['撤单等待核对', 'Cancellation awaiting reconciliation'],
      RISK_DECISION: ['风控检查', 'Risk check'], SIGNAL_GENERATED: ['产生信号', 'Signal generated'],
      SIGNAL_STATUS_CHANGED: ['信号状态更新', 'Signal updated'], SETTINGS_CHANGED: ['设置已更新', 'Settings updated'],
      POSITION_ADOPTED: ['持仓纳入管理', 'Position adopted'], PROTECTION_RAISED: ['保护线上调', 'Protection raised'],
      SUBMISSION_RECONCILED: ['订单已核对', 'Order reconciled'], RECOVERY_SUBMISSION_UNKNOWN: ['提交结果待核对', 'Submission needs reconciliation'],
      RECOVERY_UNSUBMITTED_CANCELLED: ['未提交订单已取消', 'Unsubmitted order cancelled'],
    }
    return names[key] ? t(...names[key]) : key
  }
  const categoryName = (key: string) => ({ system: t('系统', 'System'), business: t('交易', 'Trading'), decision: t('策略', 'Strategy') } as Record<string, string>)[key] || key
  const symbolBadge = (symbol: any, name?: any) => <div className="symbol-cell"><span className={`ticker-logo ticker-${String(symbol || '').length % 4}`}>{String(symbol || '?').slice(0, 1)}</span><div><strong>{symbol || '—'}</strong>{name && <span>{name}</span>}</div></div>
  const button = (label: string, onClick: () => void, icon: typeof Activity = ArrowRight, kind = 'secondary', key = '') => { const Icon = icon; return <button className={`button ${kind}`} onClick={onClick} disabled={Boolean(busy)}>{busy && busy === key ? <Loader2 size={15} className="spin" /> : <Icon size={15} />}{label}</button> }
  const account = state.account, market = state.market
  const openOrders = state.orders.filter(o => !filled(o))
  const managedPositions = state.positions.filter(p => n(p.managed_quantity) > 0)
  const lastRun = state.runs[0]
  const lastSuccess = lastRun?.checked_at || lastRun?.completed_at || lastRun?.started_at
  const alerts = state.audit.filter(a => ['error', 'warning', 'critical'].includes(a.level) || /error|blocked|stale|unknown|fail/i.test(a.event || a.event_type || ''))
  const headerTitle = page === 'settings' ? t('设置', 'Settings') : t(nav.find(item => item.id === page)!.zh, nav.find(item => item.id === page)!.en)
  const activeStrategy = strategyState?.active_strategy?.id === 'reversal_v2' ? strategyState.active_strategy : null
  const activeStrategyName = (!strategyStatusError && activeStrategy?.name) || t('选用状态待确认', 'Selection unconfirmed')
  const strategyProps = { language, state: strategyState, error: strategyStatusError, busy: Boolean(busy), onRefresh: () => void refreshStrategyData() }
  const liveLabel = liveStatusError || !liveState ? t('实盘状态待确认', 'Live status unconfirmed') : liveState.armed ? t('实盘运行中', 'Live active') : t('实盘未开启', 'Live off')
  const ownership = (p: Item) => p.management === 'mismatch' ? t('数量待核对', 'Reconcile quantity') : p.management === 'partial' ? t('部分由策略管理', 'Partly managed') : p.managed ? t('策略管理', 'Strategy managed') : t('外部持仓 · 只读', 'External · read only')
  const accountNotice = () => <p className="account-freshness">Robinhood · {liveStatusError || !connected ? t('连接中断，数据待刷新', 'Connection lost; refresh required') : liveWorkspace?.account_status === 'fresh' ? t('账户已同步', 'Account synchronized') : liveWorkspace?.account_status === 'stale' ? t('上次账户快照，点击刷新更新', 'Last account snapshot; refresh to update') : liveWorkspace?.account_status === 'unauthorized' ? t('请在设置中连接账户', 'Connect your account in Settings') : t('等待同步账户', 'Waiting for account sync')} {dateTime(liveWorkspace?.checked_at, true)}</p>

  function overview() {
    return <><PageHeading title={t('概览', 'Overview')} actions={button(t('刷新账户', 'Refresh account'), () => void verifyLive(), RefreshCw, 'secondary', 'live-verify')} />
      <div className="overview-runbar"><div className="runbar-status"><span className={`status-dot ${liveState?.armed && !liveStatusError ? 'green-dot' : 'gray-dot'}`} /><strong>{liveLabel}</strong><button className="text-button" onClick={() => navigate('strategy')}>{activeStrategyName}</button><span>{t('最近巡检', 'Last scan')} {dateTime(lastSuccess)}</span></div><div className="runbar-actions">{liveState?.armed ? button(t('停止实盘', 'Stop live trading'), () => void disarmLive(), Square, 'danger', 'live-disarm') : button(t('设置实盘', 'Set up live trading'), () => navigate('settings'), Settings2)}</div></div>
      <div className="stats-grid overview-stats">{[[t('账户总值', 'Account value'), money(account.equity)], [t('可用于策略', 'Available to strategy'), money(account.execution_buying_power)], [t('现金余额', 'Cash balance'), money(account.cash)], [t('持仓股票', 'Holdings'), liveWorkspace?.account_status && !['unavailable', 'unauthorized'].includes(liveWorkspace.account_status) ? String(state.positions.length) : '—']].map(([label, value]) => <div className="stat-card" key={label}><div className="stat-label">{label}</div><div className="stat-value">{value}</div></div>)}</div>
      {accountNotice()}
      <section className="panel overview-ledger"><div className="panel-heading"><h2>{t('持仓', 'Positions')}<span className="count">{state.positions.length}</span></h2><button className="text-button" onClick={() => navigate('portfolio')}>{t('查看全部', 'View all')}<ArrowRight size={14} /></button></div>{state.positions.length ? <div className="table-scroll"><table><thead><tr><th>{t('股票', 'Symbol')}</th><th className="right">{t('数量', 'Quantity')}</th><th className="right">{t('平均成本', 'Average cost')}</th><th>{t('管理归属', 'Ownership')}</th></tr></thead><tbody>{state.positions.slice(0, 5).map(p => <tr key={p.symbol} onClick={() => inspect(p.symbol, p)}><td>{symbolBadge(p.symbol)}</td><td className="right mono">{num(p.quantity)}</td><td className="right mono">{money(p.avg_price ?? p.average_price)}</td><td><Pill>{ownership(p)}</Pill></td></tr>)}</tbody></table></div> : <Empty title={t('暂无已同步持仓', 'No synchronized holdings')} />}</section>
      <section className="panel overview-ledger lower-panel"><div className="panel-heading"><h2>{t('未完成订单', 'Working orders')}<span className="count">{openOrders.length}</span></h2><button className="text-button" onClick={() => navigate('orders')}>{t('查看全部', 'View all')}<ArrowRight size={14} /></button></div>{openOrders.length ? <div className="table-scroll"><table><tbody>{openOrders.slice(0, 5).map(o => <tr key={o.id} onClick={() => inspect(t('实盘订单', 'Live order'), o)}><td>{symbolBadge(o.symbol)}</td><td>{statusKey(o.side) === 'buy' ? t('买入', 'Buy') : t('卖出', 'Sell')}</td><td className="mono">{num(o.quantity)}</td><td>{signalStatus(o.status)}</td><td>{dateTime(o.created_at, true)}</td></tr>)}</tbody></table></div> : <div className="compact-empty">{t('暂无本系统实盘挂单', 'No working system live orders')}</div>}</section></>
  }

  function screener() {
    return <><PageHeading title={t('选股', 'Market screener')} /><ReversalCandidates {...strategyProps} query={query} onInspect={candidate => inspect(t('V2 入选依据 · ', 'V2 selection rationale · ') + candidate.symbol, candidate)} /></>
  }

  function portfolio() {
    const positions = search(state.positions).filter(p => filter !== 'managed' || n(p.managed_quantity) > 0)
    return <><PageHeading title={t('持仓', 'Portfolio')} actions={button(t('刷新账户', 'Refresh account'), () => void verifyLive(), RefreshCw, 'secondary', 'live-verify')} />{accountNotice()}
      <section className="panel"><div className="panel-heading"><div className="tabs"><button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>{t('全部持仓', 'All holdings')}<span>{state.positions.length}</span></button><button className={filter === 'managed' ? 'active' : ''} onClick={() => setFilter('managed')}>{t('策略管理', 'Strategy managed')}<span>{managedPositions.length}</span></button></div></div>
      {positions.length ? <div className="table-scroll"><table className="roomy-table"><thead><tr><th>{t('股票', 'Symbol')}</th><th className="right">{t('数量 / 可卖', 'Qty / sellable')}</th><th className="right">{t('平均成本', 'Average cost')}</th><th>{t('管理归属', 'Ownership')}</th><th className="right">{t('策略股数 / 保护线', 'Managed qty / protection')}</th><th /></tr></thead><tbody>{positions.map(p => <tr key={p.symbol}><td>{symbolBadge(p.symbol)}</td><td className="right mono">{num(p.quantity)}<small> / {num(p.available_quantity)}</small></td><td className="right mono">{money(p.avg_price ?? p.average_price)}</td><td><Pill tone={p.management === 'mismatch' ? 'amber' : n(p.managed_quantity) > 0 ? 'green' : 'neutral'}>{ownership(p)}</Pill></td><td className="right mono">{n(p.managed_quantity) > 0 ? <>{num(p.managed_quantity)}<small> / {money(p.protective_line ?? p.protection)}</small></> : '—'}</td><td><button className="text-button" onClick={() => inspect(p.symbol, p)}>{t('详情', 'Details')}<ChevronRight size={13} /></button></td></tr>)}</tbody></table></div> : <Empty title={t('暂无已同步持仓', 'No synchronized holdings')} />}</section>
      <p className="page-caption">{t('外部持仓仅展示；保护线只适用于本策略持仓，依赖后台持续运行。', 'External holdings are read only. Protection applies to strategy holdings and requires the background service.')}</p></>
  }

  function signals() {
    const signalsList = search(state.signals).filter(s => filter === 'all' || (filter === 'buy' || filter === 'sell' ? String(s.side || s.type).toLowerCase().includes(filter) : statusKey(s.status) === filter))
    return <><PageHeading title={t('信号', 'Signal center')} /><div className="page-toolbar"><div className="tabs">{[['all', t('全部信号', 'All signals')], ['buy', t('买入', 'Buy')], ['sell', t('卖出', 'Sell')]].map(([key, value]) => <button className={filter === key ? 'active' : ''} key={key} onClick={() => setFilter(key)}>{value}</button>)}</div><span className="muted">{t('进入实盘执行流程的信号', 'Signals entering live execution')}</span></div>{signalsList.length ? <div className="signal-list">{signalsList.map((s, i) => { const buy = String(s.side || s.type).toLowerCase().includes('buy'); return <section className="panel signal-card" key={s.id || i}><div className={`signal-direction ${buy ? 'buy' : 'sell'}`}>{buy ? <ArrowUpRight size={22} /> : <ArrowDownLeft size={22} />}</div><div className="signal-main"><div className="signal-title"><h3>{s.symbol}</h3><Pill tone={buy ? 'green' : 'amber'}>{buy ? t('策略买入', 'Strategy entry') : t('策略退出', 'Strategy exit')}</Pill>{signalStatus(s.status)}<time>{dateTime(s.timestamp || s.created_at, true)}</time></div><p>{s.reason || s.message || s.rule || t('点击查看完整信号依据', 'Inspect the full signal rationale')}</p><div className="signal-metadata"><span>{t('参考价格', 'Reference')} <strong className="mono">{money(s.price ?? s.reference_price ?? s.evidence?.signal_price)}</strong></span><span>{t('风控', 'Risk')} <strong>{s.risk_result || s.risk_status || t('查看详情', 'Inspect details')}</strong></span><span>{t('有效期', 'Expires')} <strong className="mono">{dateTime(s.expires_at)}</strong></span><span className="id-short">#{String(s.id).slice(0, 12)}</span></div></div><div className="signal-actions"><button className="icon-button" title={t('执行链路', 'Execution trace')} onClick={() => inspect(t('信号与关联链路', 'Signal & execution trace'), { ...s, linked_orders: state.orders.filter(x => x.intent_id === s.id || x.id === s.broker_order_id) })}><ArrowUpRight size={18} /></button></div></section> })}</div> : <section className="panel"><Empty title={t('暂无匹配信号', 'No matching signals')} description={t('信号进入实盘执行流程后显示。', 'Signals appear when they enter live execution.')} /></section>}</>
  }

  function orders() {
    const list = filter === 'fills' ? search(state.fills) : search(state.orders).filter(o => filter === 'all' || (filter === 'working' ? !filled(o) : ['submission_unknown', 'unknown', 'rejected', 'pending_approval'].includes(statusKey(o.status))))
    return <><PageHeading title={t('订单', 'Order center')} actions={button(t('刷新账户', 'Refresh account'), () => void verifyLive(), RefreshCw, 'secondary', 'live-verify')} />{accountNotice()}<div className="compact-stats"><Metric label={t('未完成订单', 'Working orders')} value={String(openOrders.length)} /><Metric label={t('结果待确认', 'Unknown submissions')} value={String(state.orders.filter(o => ['unknown', 'submission_unknown'].includes(statusKey(o.status))).length)} /><Metric label={t('成交笔数', 'Verified fills')} value={String(state.fills.length)} /><Metric label={t('占用资金', 'Reserved capital')} value={money(account.reserved_cash)} /></div><section className="panel"><div className="panel-heading"><div className="tabs">{[['all', t('全部订单', 'All orders')], ['working', t('未完成', 'Working')], ['attention', t('需关注', 'Needs attention')], ['fills', t('成交记录', 'Fills')]].map(([key, value]) => <button key={key} className={filter === key ? 'active' : ''} onClick={() => setFilter(key)}>{value}</button>)}</div><Pill>Robinhood</Pill></div>{list.length ? <div className="table-scroll"><table className="roomy-table"><thead><tr><th>{t('证券 / 委托编号', 'Security / order ID')}</th><th>{t('方向', 'Side')}</th><th className="right">{filter === 'fills' ? t('成交数量', 'Fill quantity') : t('已成 / 委托', 'Filled / quantity')}</th><th className="right">{filter === 'fills' ? t('成交价', 'Fill price') : t('限价', 'Limit price')}</th><th>{t('状态', 'Status')}</th><th>{t('时间', 'Time')}</th><th>{t('操作', 'Action')}</th></tr></thead><tbody>{list.map((o, i) => <tr key={o.id || i}><td><div className="order-symbol"><strong>{o.symbol}</strong><span className="mono">{String(o.id || o.order_id).slice(0, 15)}</span></div></td><td><Pill tone={statusKey(o.side) === 'buy' ? 'green' : 'amber'}>{statusKey(o.side) === 'buy' ? t('买入', 'Buy') : t('卖出', 'Sell')}</Pill></td><td className="right mono">{filter === 'fills' ? num(o.quantity ?? o.qty) : <>{num(o.filled_quantity ?? o.filled_qty ?? 0)}<small> / {num(o.quantity ?? o.qty)}</small></>}</td><td className="right mono">{money(filter === 'fills' ? o.price ?? o.fill_price : o.limit_price ?? o.price)}</td><td>{filter === 'fills' ? <Pill tone="green" dot>{t('已核验', 'Verified')}</Pill> : signalStatus(o.status)}</td><td className="mono muted">{dateTime(o.timestamp || o.created_at || o.submitted_at || o.filled_at, true)}</td><td><div className="table-actions"><button className="text-button" onClick={() => inspect(t('订单执行详情', 'Order execution detail'), { ...o, intent: state.intents.find(x => x.id === o.intent_id), fills: filter === 'fills' ? undefined : state.fills.filter(f => f.order_id === o.id || f.broker_order_id === o.id) })}>{t('详情', 'Details')}</button></div></td></tr>)}</tbody></table></div> : <Empty title={query || filter !== 'all' ? t('没有匹配记录', 'No matching records') : t('暂无本系统实盘订单', 'No system live orders yet')} />}</section><p className="page-caption">{t('手动撤单请在 Robinhood 操作，平台同步最终结果。', 'Cancel manually in Robinhood; the platform reconciles the final result.')}</p></>
  }

  function strategy() {
    return <><PageHeading title={t('策略与回测', 'Strategy & backtests')} actions={button(t('刷新策略状态', 'Refresh strategy status'), () => void refreshStrategies(), RefreshCw)} /><ActiveStrategy {...strategyProps} liveArmed={liveStatusError || !liveState ? null : liveState.armed === true} onSettings={() => navigate('settings')} /><ReversalResearch language={language} onInspect={inspectInfo} /></>
  }

  function audit() {
    const logs = search(state.audit).filter(a => filter === 'all' || String(a.category || '').toLowerCase().includes(filter) || (filter === 'error' && ['error', 'warning', 'critical'].includes(a.level)))
    const runs = search(state.runs)
    return <><PageHeading title={t('日志', 'Audit trail')} /><section className="panel">
      <div className="panel-heading"><div className="tabs">{[['all', t('全部事件', 'All events')], ['error', t('异常', 'Exceptions')], ['runs', t('巡检记录', 'Scan runs')]].map(([key, label]) => <button key={key} className={filter === key ? 'active' : ''} onClick={() => setFilter(key)}>{label}</button>)}</div><span className="muted mono">{filter === 'runs' ? runs.length : logs.length} {t('条记录', 'records')}</span></div>
      {filter === 'runs' ? (runs.length ? <div className="table-scroll"><table><thead><tr><th>{t('状态', 'Status')}</th><th>{t('时间', 'Time')}</th><th>{t('提交订单', 'Orders sent')}</th><th>{t('撤单请求', 'Cancellation requests')}</th><th /></tr></thead><tbody>{runs.map((r, i) => <tr key={r.checked_at || i} onClick={() => inspect(t('实盘巡检', 'Live scan'), r)}><td>{signalStatus(r.status)}</td><td className="mono muted">{dateTime(r.checked_at || r.completed_at, true)}</td><td>{r.orders_sent ?? '—'}</td><td>{r.cancel_requests ?? '—'}</td><td><ChevronRight size={14} /></td></tr>)}</tbody></table></div> : <Empty title={query ? t('没有匹配的巡检记录', 'No matching scan runs') : t('等待首次巡检', 'Waiting for the first scan')} />)
        : logs.length ? <div className="audit-list">{logs.map((a, i) => <button className="audit-row" key={a.id || i} onClick={() => inspect(t('审计详情 · ', 'Audit detail · ') + (a.event || a.event_type || a.action || ''), a)}><span className={`audit-dot ${a.level === 'error' ? 'red' : a.level === 'warning' ? 'amber' : ''}`} /><time className="mono">{dateTime(a.timestamp || a.created_at, true)}</time><span className="audit-category"><Pill>{categoryName(a.category || 'business')}</Pill></span><span className="audit-message"><strong>{eventName(a)}</strong><span>{a.message || a.reason || a.summary || (a.symbol ? `${a.symbol} · ${a.result || ''}` : t('查看详情', 'View details'))}</span></span>{a.symbol && <span className="mono audit-symbol">{a.symbol}</span>}<span className="id-short">{String(a.run_id || a.intent_id || a.id || '').slice(0, 12)}</span><ChevronRight size={14} /></button>)}</div> : <Empty title={t('没有匹配的审计事件', 'No matching audit events')} description={t('可按股票或记录编号搜索。', 'Search by symbol or record ID.')} />}
      <div className="panel-bottom"><span><LockKeyhole size={13} />{t('凭证脱敏 · 追加保存 · 关联追踪', 'Redacted credentials · append-only · linked traces')}</span><span>{t('审计记录保留原始 UTC 时间', 'Audit records retain original UTC timestamps')}</span></div>
    </section></>
  }

  function liveTradingPanel() {
    const armed = liveState?.armed === true
    const quoteCheck = liveState?.last_quote_check
    const authorized = brokerConnection?.connection?.authorized === true
    const canPrepare = connected && !busy && !armed && !liveStatusError && !strategyStatusError && Boolean(activeStrategy) && authorized
    return <section className="panel settings-panel live-trading-panel" id="live-trading-controls">
      <div className="panel-heading"><h2><ShieldCheck size={17} />{t('实盘自动交易', 'Live automated trading')}</h2><Pill tone={armed ? 'green' : 'neutral'} dot>{liveLabel}</Pill></div>
      <div className="live-trading-body">
        <div className="strategy-selected-inline"><div className="live-strategy-name"><span>{t('交易策略', 'Trading strategy')}</span><strong>{activeStrategyName}</strong></div><button className="text-button" onClick={() => navigate('strategy')}>{t('查看规则', 'View rules')}<ChevronRight size={14} /></button></div>
        <div className="live-account-row"><div><strong>Robinhood</strong><span className="mono">{liveState?.account_masked || t('真实交易账户', 'Live brokerage account')}</span></div>{brokerStatusError ? <button className="text-button" onClick={() => void refreshBrokerConnection()}>{t('重试连接', 'Retry connection')}</button> : authorized ? <Pill tone="green"><Check size={12} />{t('已连接', 'Connected')}</Pill> : <button className="button secondary" disabled={Boolean(busy)} onClick={() => void connectBroker()}><Link2 size={14} />{t('连接账户', 'Connect account')}</button>}</div>
        {liveStatusError && <p className="live-service-error">{t('服务状态待确认，请刷新后再开启。', 'Service status is unconfirmed. Refresh before activation.')} <button className="text-button" onClick={() => void refreshLive()}>{t('刷新', 'Refresh')}</button></p>}
        <form onSubmit={event => { event.preventDefault(); if (canPrepare) void beginLiveActivation() }}>
          <ManagedPolicy language={language} onInspect={inspectInfo}
            policy={strategyState?.strategies?.find((row: Item) => row.id === activeStrategy?.id)?.execution_policy}
            entryFeasibility={liveState?.entry_feasibility}
            config={liveState?.config?.strategy_id === activeStrategy?.id ? liveState?.config : undefined} />
          <div className="live-start-row"><p>{armed ? t('停止后保留现有订单与持仓。', 'Stopping preserves existing orders and positions.') : t('平台按 V2 策略自动确定股票范围、单笔额度和仓位限制，并同步账户供你确认。', 'The platform uses V2 to determine its universe, order and position limits, then synchronizes your account for confirmation.')}</p>{armed ? <button type="button" className="button danger" disabled={busy === 'live-disarm'} onClick={() => void disarmLive()}><Square size={15} />{t('停止实盘', 'Stop live trading')}</button> : <button className="button primary" type="submit" disabled={!canPrepare}>{busy === 'live-activation-check' ? <Loader2 size={15} className="spin" /> : <Play size={15} />}{t('开启实盘', 'Enable live trading')}</button>}</div>
        </form>
        <div className="live-tools-row"><button type="button" className="text-button" onClick={() => inspect(t('运行详情', 'Execution details'), { [t('最近实盘巡检', 'Latest live scan')]: liveState?.last_run, [t('盘口交叉校验', 'Cross-feed quote check')]: quoteCheck || t('等待交易信号', 'Awaiting a signal') })}>{t('运行详情', 'Execution details')}</button><button type="button" className="text-button" onClick={() => navigate('audit')}>{t('查看日志', 'View audit trail')}</button></div>

      </div>
    </section>
  }

  function liveActivationDialog() {
    if (!liveArmConfirm) return null
    const blockers = liveActivationBlockers()
    const checking = liveArmConfirm.checking === true
    const close = () => { if (busy !== 'live-arm') setLiveArmConfirm(null) }
    return <div className="modal-overlay" onClick={close}><section className="confirm-modal live-activation-modal" role="dialog" aria-modal="true" aria-labelledby="live-arm-title" onClick={event => event.stopPropagation()}>
      <div className="confirm-icon"><LockKeyhole size={25} /></div><h2 id="live-arm-title">{t('确认开启实盘', 'Confirm live trading')}</h2>
      <p>{t('检查通过并确认后，策略可在以下范围内使用真实资金自动买卖。现有外部持仓不会自动纳入管理。', 'After checks pass and you confirm, the strategy can automatically trade real funds within this scope. Existing external positions will not be adopted automatically.')}</p>
      <div className="live-arm-scope">
        <div><span>{t('选用策略', 'Selected strategy')}</span><strong>{strategyState?.strategies?.find((row: Item) => row.id === liveArmConfirm.config?.strategy_id)?.name || activeStrategyName}</strong></div>
        <div><span>{t('真实账户', 'Live account')}</span><strong className="mono">{liveArmConfirm.account_masked || '—'}</strong></div>
      </div>
      <ManagedPolicy language={language} onInspect={inspectInfo} policy={liveArmConfirm.policy} config={liveArmConfirm.config} entryFeasibility={liveState?.entry_feasibility} compact />
      {liveArmConfirm.error && <p className="live-service-error">{liveArmConfirm.error}</p>}
      <div className="live-arm-checks" aria-live="polite"><div><strong>{checking ? t('正在检查', 'Checking') : blockers.length ? t('待完成', 'To complete') : t('开启检查通过', 'Activation checks passed')}</strong><button className="text-button" disabled={Boolean(busy)} onClick={() => void beginLiveActivation()}><RefreshCw size={13} />{t('重新检查', 'Check again')}</button></div>
        {checking ? <p><Loader2 size={14} className="spin" />{t('正在刷新账户和开启条件，不会下单。', 'Refreshing the account and activation checks. No order will be sent.')}</p> : blockers.length ? <ul className="live-readiness-list">{blockers.map(reason => <li key={reason}><Clock3 size={14} /><span>{liveNote(reason)}</span></li>)}</ul> : <p className="positive"><Check size={14} />{t('请核对账户和范围，再确认开启。', 'Review the account and scope, then confirm activation.')}</p>}
      </div>
      <div className="confirm-actions"><button className="button secondary" disabled={busy === 'live-arm'} onClick={close}>{t('取消', 'Cancel')}</button><button className="button danger" disabled={Boolean(busy) || blockers.length > 0} onClick={() => void armLive()}>{busy === 'live-arm' ? <Loader2 size={15} className="spin" /> : <LockKeyhole size={15} />}{t('确认使用真实资金并开启', 'Confirm and enable')}</button></div>
    </section></div>
  }

  function settingsPage() {
    return <><PageHeading title={t('设置', 'Settings')} /><div className="live-settings-layout">{liveTradingPanel()}<section className="panel display-preferences"><div className="panel-heading"><h2><Globe2 size={14} />{t('显示偏好', 'Display preferences')}</h2></div><div className="settings-details"><div className="setting-select"><span>{t('语言', 'Language')}</span><div className="segmented" role="group" aria-label={t('语言', 'Language')}><button className={language === 'zh' ? 'active' : ''} aria-pressed={language === 'zh'} onClick={() => setLanguage('zh')}>简体中文</button><button className={language === 'en' ? 'active' : ''} aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>English</button></div></div><div className="setting-select"><span>{t('时间显示', 'Timezone')}</span><div className="segmented" role="group" aria-label={t('时间显示', 'Timezone')}><button className={timezone === 'America/New_York' ? 'active' : ''} aria-pressed={timezone === 'America/New_York'} onClick={() => setTimezone('America/New_York')}>{t('美东时间 ET', 'Eastern time ET')}</button>{localZone !== 'America/New_York' && <button className={timezone === localZone ? 'active' : ''} aria-pressed={timezone === localZone} onClick={() => setTimezone(localZone)}>{t('本地时间', 'Local time')}</button>}</div></div></div></section></div></>
  }

  return <div className="app-shell">
    {mobileMenu && <button className="sidebar-scrim" aria-label="Close menu" onClick={() => setMobileMenu(false)} />}
    <aside className={`sidebar ${mobileMenu ? 'open' : ''}`}>
      <a className="brand" href="#overview" onClick={event => { event.preventDefault(); navigate('overview') }}>northstar<span className="brand-period">.</span></a>
      <nav aria-label={t('主导航', 'Main navigation')}>{nav.slice(0, 5).map(item => <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><item.icon size={18} strokeWidth={1.7} /><span>{t(item.zh, item.en)}</span>{item.id === 'signals' && state.signals.filter(signal => statusKey(signal.status) === 'new').length > 0 && <span className="nav-count">{state.signals.filter(signal => statusKey(signal.status) === 'new').length}</span>}</button>)}</nav>
      <details className="nav-more" open={['strategy', 'audit'].includes(page) || undefined}><summary><MoreHorizontal size={18} /><span>{t('更多', 'More')}</span><ChevronDown size={13} /></summary><nav>{nav.slice(5).map(item => <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><item.icon size={18} strokeWidth={1.7} /><span>{t(item.zh, item.en)}</span></button>)}</nav></details>
      <div className="sidebar-bottom"><button className={`nav-item ${page === 'settings' ? 'active' : ''}`} aria-current={page === 'settings' ? 'page' : undefined} onClick={() => navigate('settings')}><Settings2 size={18} strokeWidth={1.7} /><span>{t('设置', 'Settings')}</span></button></div>
    </aside>
    <div className="workspace"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu-button" onClick={() => setMobileMenu(!mobileMenu)} aria-label="Open menu"><Menu size={19} /></button><strong>{headerTitle}</strong></div><div className="topbar-right"><div className="global-search"><Search size={15} /><input ref={searchRef} value={query} onChange={e => setQuery(e.target.value)} placeholder={t('搜索…', 'Search…')} aria-label={t('搜索当前页面', 'Search current page')} /><kbd>⌘ K</kbd>{query && <button className="clear-search" onClick={() => setQuery('')} aria-label="Clear search"><X size={12} /></button>}{query && ['overview', 'settings', 'strategy'].includes(page) && <div className="global-search-results"><div className="search-results-label">{t('搜索结果', 'Search results')}</div>{[...search(state.candidates).slice(0, 3).map(item => ({ item, category: t('候选股票', 'Candidate'), title: item.symbol })), ...search(state.orders).slice(0, 3).map(item => ({ item, category: t('订单', 'Order'), title: `${item.symbol} · ${item.status}` })), ...search(state.audit).slice(0, 3).map(item => ({ item, category: t('审计', 'Audit'), title: item.event || item.action }))].map((result, i) => <button key={i} onClick={() => { inspect(result.title, result.item); setQuery('') }}><Search size={13} /><span>{result.title}<small>{result.category}</small></span><ChevronRight size={12} /></button>)}{!search(state.candidates).length && !search(state.orders).length && !search(state.audit).length && <p>{t('没有匹配结果', 'No matching results')}</p>}</div>}</div><button className={`notification-button icon-button ${alerts.length ? 'unread' : ''}`} onClick={() => setNotifications(!notifications)} title={t('通知中心', 'Notifications')}><Bell size={18} /></button></div></header>
      <div className="market-bar"><div><span className={`status-dot ${market.is_open ? 'green-dot' : 'gray-dot'}`} /><strong>{market.is_open ? t('美股开市', 'Market open') : market.phase ? t('美股休市', 'Market closed') : t('日历待确认', 'Calendar pending')}</strong></div><div><span>{nowDate}</span><button className="timezone-button" onClick={() => setTimezone(timezone === 'America/New_York' ? localZone : 'America/New_York')} title={t('切换美东 / 本地时间', 'Switch Eastern / local time')}><span className="mono">{currentTime}</span><span>{timezone === 'America/New_York' ? 'ET' : 'LOCAL'}</span></button><button className="text-button" onClick={() => navigate('settings')}><Pill tone={liveState?.armed ? 'green' : 'neutral'}>{liveLabel}</Pill></button></div></div>
      <main className="main-content">
        {error && <div className="connection-banner"><AlertTriangle size={17} /><span><strong>{t('后端服务暂不可用', 'Backend service unavailable')}</strong> {t('请检查后台服务。', 'Check the background services.')} <small>{error}</small></span><button className="text-button" onClick={() => void refresh()}><RefreshCw size={14} />{t('重试', 'Retry')}</button></div>}
        {loading ? <div className="initial-loading"><Loader2 size={27} className="spin" /><span>{t('正在连接交易空间…', 'Connecting to your workspace…')}</span></div> : page === 'overview' ? overview() : page === 'screener' ? screener() : page === 'portfolio' ? portfolio() : page === 'signals' ? signals() : page === 'orders' ? orders() : page === 'strategy' ? strategy() : page === 'audit' ? audit() : settingsPage()}
        
      </main>
    </div>
    {authenticationRequired && <div className="modal-overlay auth-overlay"><form className="confirm-modal" onSubmit={async e => { e.preventDefault(); const result = await act('session', '/session', { token: sessionToken }, 'POST', t('已建立安全会话。', 'Secure session established.')); if (result) { setSessionToken(''); await refresh() } }}><div className="confirm-icon safe"><LockKeyhole size={25} /></div><h2>{t('解锁交易空间', 'Unlock your workspace')}</h2><p>{t('此部署已启用访问保护。请输入访问口令。', 'This deployment requires access authentication. Enter the configured access token to establish an HttpOnly session. The token is not retained in browser storage.')}</p><label className="auth-token-label">{t('访问口令', 'Access token')}<input type="password" autoComplete="current-password" value={sessionToken} onChange={e => setSessionToken(e.target.value)} required /></label><div className="confirm-actions"><button type="submit" className="button primary" disabled={!sessionToken || Boolean(busy)}>{busy === 'session' ? <Loader2 size={15} className="spin" /> : <LockKeyhole size={15} />}{t('建立安全会话', 'Start secure session')}</button></div></form></div>}
    {liveActivationDialog()}
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role="status">{toast.error ? <AlertTriangle size={18} /> : <Check size={18} />}<span>{toast.message}</span><button className="icon-button" onClick={() => setToast(null)} aria-label="Dismiss"><X size={15} /></button></div>}
    {notifications && <div className="notification-popover"><div className="panel-heading"><h2>{t('需要关注', 'Needs attention')}<span className="count">{alerts.length}</span></h2><button className="icon-button" onClick={() => setNotifications(false)} aria-label="Close"><X size={16} /></button></div>{alerts.length ? alerts.slice(0, 5).map((a, i) => <button className="notification-item" key={a.id || i} onClick={() => { inspect(t('异常事件', 'Exception'), a); setNotifications(false) }}><AlertTriangle size={16} /><span><strong>{a.message || a.event || a.action}</strong><small>{dateTime(a.timestamp || a.created_at, true)}</small></span><ChevronRight size={13} /></button>) : <Empty title={t('当前没有异常通知', 'No exception notifications')} />}</div>}
    {detail && <div className="modal-overlay" onClick={() => setDetail(null)}><section className="detail-drawer" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={detail.title}><div className="drawer-heading"><div><h2>{detail.title}</h2></div><button className="icon-button" onClick={() => setDetail(null)} aria-label="Close"><X size={20} /></button></div><div className="drawer-content">{!detail.informational && <div className="detail-note"><Link2 size={15} />{t('事件详情 · 小数显示两位 · 时间戳以 UTC 保存', 'Event details · decimals shown to two places · timestamps stored in UTC')}</div>}{Object.entries(detail.item).filter(([, value]) => value !== undefined).map(([key, value]) => <div key={key} className={`detail-field ${detail.informational || typeof value === 'object' ? 'wide' : ''}`}><label>{key.replaceAll('_', ' ')}</label>{detail.informational && Array.isArray(value) && value.every(item => typeof item === 'string') ? <ul className="detail-text-list">{value.map((item, index) => <li key={index}>{item}</li>)}</ul> : typeof value === 'object' && value !== null ? <pre>{formatDetailJson(value, key)}</pre> : <span className={key.endsWith('_id') || key === 'id' ? 'mono' : ''}>{formatDetailValue(value, key)}</span>}</div>)}</div><div className="drawer-footer">{!detail.informational && <button className="button secondary" onClick={() => { setDetail(null); navigate('audit') }}><FileText size={15} />{t('前往完整日志', 'Open audit trail')}</button>}<button className="button primary" onClick={() => setDetail(null)}>{t('完成', 'Done')}<Check size={15} /></button></div></section></div>}
  </div>
}

function PageHeading({ title, actions }: { title: string; actions?: ReactNode }) {
  return <div className="page-heading inner-heading"><h1>{title}</h1>{actions && <div className="heading-actions">{actions}</div>}</div>
}

function Metric({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return <div className="metric"><span>{label}</span><strong className={positive === undefined ? '' : positive ? 'positive' : 'negative'}>{value}</strong></div>
}
