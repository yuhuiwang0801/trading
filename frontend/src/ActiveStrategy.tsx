import { ChevronRight, Clock3, RefreshCw, ShieldCheck } from 'lucide-react'
import { formatMoney as money, formatPercent as pct } from './format'
import './ActiveStrategy.css'

type Item = Record<string, any>
type Shared = { language: 'zh' | 'en'; state: Item | null; error: boolean; busy: boolean; onRefresh: () => void }
const message = (value: any) => typeof value === 'string' ? value : value?.message || value?.reason || value?.code || '—'
const statusLabel = (status: string, zh: boolean) => ({
  ready: zh ? '数据就绪' : 'Data ready', blocked: zh ? '执行条件未满足' : 'Execution blocked',
  expired: zh ? '新买窗口已结束' : 'Entry window ended',
  stale: zh ? '数据待更新' : 'Data stale', running: zh ? '正在更新' : 'Updating',
  building: zh ? '正在更新' : 'Updating', refreshing: zh ? '正在更新' : 'Updating',
  queued: zh ? '更新已排队' : 'Update queued', failed: zh ? '更新失败' : 'Update failed',
  unavailable: zh ? '数据不可用' : 'Data unavailable', not_ready: zh ? '数据尚未就绪' : 'Data not ready',
  not_generated: zh ? '等待首次生成' : 'Awaiting first update', unknown: zh ? '状态待确认' : 'Status unconfirmed',
}[status] || status || (zh ? '等待首次生成' : 'Awaiting first update'))

export function StrategyRuntime({ language, state, error, busy, onRefresh }: Shared) {
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  const runtime = state?.runtime || {}
  const blockers = Array.isArray(runtime.blockers) ? runtime.blockers : []
  const jobStatus = state?.refresh_job?.status
  const updating = ['queued', 'running'].includes(jobStatus)
  const missingPacket = blockers.some((blocker: any) => message(blocker) === 'missing_or_invalid_signal_packet')
  const blockerLabel = (blocker: any) => {
    const code = message(blocker)
    if (code === 'intraday_reference_upgrade_required') return t('正在升级盘中同行参考数据，请等待更新完成。', 'Intraday peer references need an update. Wait for the data refresh to complete.')
    const labels: Record<string, [string, string]> = {
      missing_or_invalid_signal_packet: ['尚未生成有效的每日数据与信号', 'No valid daily data and signals have been generated'],
      strategy_configuration_changed: ['策略参数已变化，需要重新生成信号', 'Strategy parameters changed; rebuild daily signals'],
      universe_metadata_changed: ['股票池资料已变化，需要重新生成信号', 'Universe metadata changed; rebuild daily signals'],
      strategy_metadata_unavailable: ['策略或股票池资料暂不可用', 'Strategy or universe metadata is unavailable'],
      stale_signal_session: ['信号不是最新的完整交易日，需要更新', 'Signals are not for the latest complete session'],
      data_session_mismatch: ['行情与信号日期不一致', 'Market data and signal sessions differ'],
      execution_session_mismatch: ['信号与计划执行日期不一致', 'Signal and execution sessions differ'],
      future_packet_timestamp: ['数据时间异常，暂不可执行', 'Data timestamp is invalid; execution is blocked'],
      execution_window_expired: ['今日新买窗口已结束；已有持仓的退出独立检查', 'Today’s entry window ended; holding exits are checked separately'],
      prices_not_current_completed_session: ['缺少最新完整交易日的价格', 'Prices for the latest complete session are missing'],
      missing_latest_security_bars: ['部分股票缺少最新价格，等待完整数据', 'Some securities lack latest-session prices'],
      fundamentals_not_current_session: ['基本面资料尚未更新至信号日', 'Fundamental data is not current through the signal session'],
      missing_latest_fundamental_rows: ['部分股票缺少最新基本面资料', 'Some securities lack current fundamental data'],
      build_finished_after_signal_or_execution_deadline: ['本次更新超过执行期限，等待下一交易日的数据', 'The update missed its execution deadline; awaiting the next signal session'],
      build_crossed_signal_session_boundary: ['生成期间信号日期已切换，请重新更新', 'The signal session changed during the build; update again'],
      configuration_changed_during_build: ['更新期间配置变化，需要重新生成', 'Configuration changed during the update; rebuild required'],
      reversal_daily_builder_already_running: ['已有数据更新正在进行', 'A data update is already running'],
      mixed_or_incorrect_market_feed: ['行情来源检查未通过', 'Market-data feed validation failed'],
      empty_sip_history: ['尚未取得有效 SIP 历史行情', 'No valid SIP history was received'],
    }
    if (labels[code]) return t(...labels[code])
    if (code.startsWith('sec_refresh_failed:')) return t('部分公司公告获取失败，本次信号暂不可执行', 'A company filing update failed; these signals cannot execute')
    if (code.startsWith('incomplete_latest_sip_session:')) return t('部分股票最新 SIP 日线缺失，等待更新', 'Some latest SIP daily bars are missing; update required')
    if (code.startsWith('security_identity_or_tradability_changed:')) return t('证券身份或可交易状态变化，需要重新核对', 'A security identity or tradability status changed; review required')
    return code
  }
  return <div className="strategy-runtime" aria-live="polite">
    <div className="strategy-runtime-heading"><strong><Clock3 size={15} />{t('每日信号状态', 'Daily signal status')}</strong><span className={`pill ${error || blockers.length ? 'amber' : runtime.status === 'ready' ? 'green' : 'neutral'}`}>{error ? t('状态待确认', 'Status unconfirmed') : statusLabel(updating ? jobStatus : runtime.status, language === 'zh')}</span>{updating && <span className="muted">{t('当前数据：', 'Current data: ')}{statusLabel(runtime.status, language === 'zh')}</span>}<button className="text-button" disabled={busy || error || updating || ['queued', 'running', 'building', 'refreshing'].includes(runtime.status)} onClick={onRefresh}><RefreshCw size={14} />{t('更新数据与信号', 'Update data & signals')}</button></div>
    <dl className="strategy-runtime-dates"><div><dt>{t('行情截至', 'Data through')}</dt><dd>{missingPacket ? '—' : runtime.data_session || '—'}</dd></div><div><dt>{t('信号交易日', 'Signal session')}</dt><dd>{missingPacket ? '—' : runtime.signal_session || '—'}</dd></div><div><dt>{t('计划执行日', 'Execution session')}</dt><dd>{runtime.execute_session || '—'}</dd></div></dl>
    {blockers.length > 0 && <ul className="strategy-blockers">{blockers.map((blocker: any, i: number) => <li key={i}>{blockerLabel(blocker)}</li>)}</ul>}
    <p className="strategy-note">{t('更新仅获取行情、公告并计算候选，不提交订单。日期与资料检查通过后，仍需实盘开启及账户风控通过才能执行。', 'Updating fetches market data and filings and computes candidates. It does not submit orders. Execution additionally requires current data, live activation and account risk checks.')}</p>
  </div>
}

export default function ActiveStrategy(props: Shared & { liveArmed: boolean | null; onSettings: () => void }) {
  const { language, state, error, liveArmed, onSettings } = props
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  const active = state?.active_strategy
  const selected = (state?.strategies || []).find((row: Item) => row.id === 'reversal_v2')
  const config = selected?.parameters || {}
  const entry = config.entry_filters || {}
  const exit = config.dynamic_exit || {}
  const intraday = selected?.execution_policy?.intraday_exit
  const intradayEnabled = intraday?.enabled === true
  const reversal = active?.id === 'reversal_v2' && Boolean(selected)
  const unconfirmed = error || Boolean(state && !reversal)
  return <section className="panel active-strategy" aria-label={t('当前平台策略', 'Active platform strategy')}>
    <div className="panel-heading"><div><h2>{t('当前平台策略', 'Active platform strategy')}<span className={`pill ${unconfirmed ? 'amber' : reversal ? 'blue' : 'neutral'}`}>{unconfirmed ? t('选用状态待确认', 'Selection unconfirmed') : reversal ? t('已选用', 'Selected') : t('正在读取', 'Loading')}</span></h2><p>{selected?.name || t('V2 基本面过滤与反弹退出', 'V2 fundamental filters and rebound exits')}</p></div><button className="text-button" onClick={onSettings}><span className={`pill ${liveArmed === true ? 'green' : 'neutral'}`}>{liveArmed == null ? t('实盘状态待确认', 'Live status unconfirmed') : liveArmed ? t('实盘已开启', 'Live enabled') : t('实盘未开启', 'Live off')}</span><ChevronRight size={14} /></button></div>
    {error && <p className="strategy-note" role="status">{t('策略服务暂不可用，当前选用状态需要重新确认。', 'The strategy service is unavailable. Confirm the active selection after reconnecting.')}</p>}
    {!error && state && !reversal && <p className="strategy-note" role="status">{t('当前策略尚未确认为 V2，请刷新策略状态。', 'V2 selection is unconfirmed. Refresh the strategy status.')}</p>}
    {reversal && <>
      <div className="strategy-v2-rules"><div><span>01</span><h3>{t('相对同行超跌', 'Dislocation against peers')}</h3><p>{t(`过去 ${config.formation_sessions ?? 5} 个交易日自身下跌，相对同行的对数收益缺口至少 ${pct(entry.minimum_relative_log_gap ?? .02)}，按偏离选前 ${pct(config.candidate_fraction ?? .1)}。`, `Negative ${config.formation_sessions ?? 5}-session return and at least ${pct(entry.minimum_relative_log_gap ?? .02)} relative log-return dislocation; select the top ${pct(config.candidate_fraction ?? .1)}.`)}</p></div><div><span>02</span><h3>{t('过滤已知基本面风险', 'Screen known fundamental risks')}</h3><p>{t(`收入同比不低于 ${pct(entry.minimum_revenue_yoy ?? -.05)}，净利润为正且同比不低于 ${pct(entry.minimum_net_income_yoy ?? -.2)}。财报冷静期 ${entry.earnings_cooldown_sessions ?? 5} 日，风险公告 ${entry.risk_event_cooldown_sessions ?? 20} 日；资料未知时跳过新买入。`, `Revenue growth ≥ ${pct(entry.minimum_revenue_yoy ?? -.05)}; positive net income and growth ≥ ${pct(entry.minimum_net_income_yoy ?? -.2)}. Cooldowns: ${entry.earnings_cooldown_sessions ?? 5} sessions for earnings and ${entry.risk_event_cooldown_sessions ?? 20} for risk events. Skip entries when data is unknown.`)}</p></div><div><span>03</span><h3>{intradayEnabled ? t('盘中修复与风险退出', 'Intraday rebound and risk exits') : t('修复目标与风险退出', 'Rebound and risk exits')}</h3><p>{intradayEnabled
        ? t(`盘中偏离修复 ${pct(intraday.rebound_fraction)}、亏损 ${pct(intraday.absolute_stop_loss)} 或相对走势进一步恶化时触发退出；盈利达到 ${pct(intraday.trailing_activation_pct)} 后，从已验证峰值回撤 ${pct(intraday.trailing_drawdown_pct)} 也触发退出。基本面风险与最长 ${exit.max_hold_sessions ?? 10} 个交易日兜底仍保留。`, `Trigger an intraday exit at ${pct(intraday.rebound_fraction)} dislocation repair, ${pct(intraday.absolute_stop_loss)} loss or further relative deterioration. After a ${pct(intraday.trailing_activation_pct)} gain, a ${pct(intraday.trailing_drawdown_pct)} drawdown from the verified peak also triggers an exit. Fundamental-risk and ${exit.max_hold_sessions ?? 10}-session maximum-hold exits remain in place.`)
        : t(`按收盘数据判断偏离修复 ${pct(exit.rebound_fraction ?? .8)}、亏损 ${pct(exit.absolute_stop_loss ?? .05)}、相对走势进一步恶化或基本面风险。最长 ${exit.max_hold_sessions ?? 10} 个交易日兜底。`, `Evaluate ${pct(exit.rebound_fraction ?? .8)} dislocation repair, ${pct(exit.absolute_stop_loss ?? .05)} loss, relative deterioration and fundamental risk using completed daily data. Maximum hold: ${exit.max_hold_sessions ?? 10} sessions.`)}</p></div></div>
      <div className="strategy-risk-strip"><ShieldCheck size={15} /><span>{t(`每日最多新增 ${config.max_entries_per_day ?? 4} 只 · 最多 ${config.max_positions ?? 20} 只持仓 · 单股目标 ${pct(config.max_security_weight ?? .05)} · 行业上限 ${pct(config.max_industry_weight ?? .25)}`, `Up to ${config.max_entries_per_day ?? 4} new positions/day · ${config.max_positions ?? 20} holdings · ${pct(config.max_security_weight ?? .05)} per security · ${pct(config.max_industry_weight ?? .25)} per industry`)}</span></div>
      <p className="strategy-note">{intradayEnabled
        ? t('每日收盘后选股，下一交易日美东 09:35–09:40 新买。盘中保护从 09:30 至正常收盘每约 30 秒检查；原日线退出从 09:35 起处理。卖出仍需有效双源报价和限价执行，触发不保证成交；电脑睡眠时本地保护暂停。', 'Screen after close; enter next session at 09:35–09:40 ET. Intraday protection checks about every 30 seconds from 09:30 to the regular close; daily exits start at 09:35. Exits require valid quotes from both sources and use limit orders, so a trigger does not guarantee a fill. Local protection pauses when the computer sleeps.')
        : t('每日收盘判断；下一交易日美东 09:35–09:40 新买，日线退出从 09:35 起处理。账户额度及实时报价检查仍可能限制交易。', 'Evaluate after close; enter next session at 09:35–09:40 ET and process daily exits from 09:35. Account limits and current-quote checks may restrict execution.')}</p>
      <p className="strategy-note">{t('历史回测仍为原 V2 日线研究，不能用于验证新版盘中退出收益。实盘买入还要求参考价与昨收偏离不超过 0.5%，实际交易与回测可能不同。', 'Historical results remain the original V2 daily-data study and do not validate returns from the new intraday exits. Live entry prices must also stay within 0.5% of the prior close, so actual trades may differ from the backtest.')}</p>
      <StrategyRuntime {...props} />
    </>}
  </section>
}

export function ReversalCandidates(props: Shared & { query: string; onInspect: (candidate: Item) => void }) {
  const { language, state, query, onInspect } = props
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  const rows = (state?.runtime?.candidates || []).filter((row: Item) => !query || JSON.stringify(row).toLowerCase().includes(query.toLowerCase()))
  return <section className="panel active-strategy"><div className="panel-heading"><div><h2>{t('V2 行业反转候选', 'V2 industry reversal candidates')}<span className="count">{rows.length}</span></h2><p>{t('相对同行偏离排序 · 基本面资料按披露时间检查', 'Ranked by peer dislocation · fundamentals checked by disclosure time')}</p></div></div><StrategyRuntime {...props} />{rows.length ? <div className="table-scroll"><table className="roomy-table"><thead><tr><th>#</th><th>{t('股票', 'Security')}</th><th className="right">{t('收盘价', 'Close')}</th><th className="right">{t('相对偏离分数', 'Relative score')}</th><th>{t('基本面状态', 'Fundamental status')}</th><th /></tr></thead><tbody>{rows.map((row: Item, i: number) => <tr key={row.security_id || row.symbol || i} onClick={() => onInspect(row)}><td className="muted mono">{row.rank || i + 1}</td><td><strong>{row.symbol}</strong></td><td className="right mono">{money(row.close ?? row.signal_price)}</td><td className="right mono">{pct(row.score ?? row.relative_gap)}</td><td>{row.fundamental_status === 'eligible' || row.fundamental_status === 'passed' ? t('已通过', 'Passed') : row.fundamental_status || t('查看依据', 'Inspect evidence')}</td><td><ChevronRight size={14} /></td></tr>)}</tbody></table></div> : <div className="empty-state"><strong>{query ? t('没有匹配候选', 'No matching candidates') : t('暂无可展示的 V2 候选', 'No V2 candidates available')}</strong><p>{t('候选为空或数据尚未就绪时，等待下一次有效信号。', 'When the shortlist is empty or data is unavailable, wait for the next valid signal.')}</p></div>}</section>
}
