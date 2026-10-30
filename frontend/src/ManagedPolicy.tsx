import { formatMoney as money, formatPercent as pct } from './format'

type Item = Record<string, any>

// A confirmation must describe the same server-owned policy that will execute.
// This is an additional UI guard; the server independently verifies the hashes.
export function managedPolicyBlockers(prepared: Item | null, current: Item | null, declared?: Item) {
  const config = prepared?.config
  const policy = config?.strategy_policy || prepared?.policy
  if (config?.scope_mode !== 'strategy_managed' || !config.policy_hash || !config.policy_version ||
      !config.universe_hash || !policy || policy.policy_hash !== config.policy_hash || policy.version !== config.policy_version) {
    return ['UI_POLICY_UNCONFIRMED']
  }
  const latest = current?.config || current?.configuration
  if (latest?.scope_mode !== 'strategy_managed' || latest.policy_hash !== config.policy_hash ||
      latest.policy_version !== config.policy_version || latest.universe_hash !== config.universe_hash ||
      (declared?.policy_hash && declared.policy_hash !== config.policy_hash)) {
    return ['UI_POLICY_CHANGED']
  }
  return []
}

export default function ManagedPolicy({ language, policy, config, entryFeasibility, compact = false, onInspect }: {
  language: 'zh' | 'en'; policy?: Item; config?: Item; entryFeasibility?: Item; compact?: boolean
  onInspect?: (title: string, item: Item) => void
}) {
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  if (!policy) return <p className="live-control-note">{t('正在读取策略的股票范围和仓位规则。', 'Loading the strategy universe and allocation rules.')}</p>
  const fixed = policy.universe_mode === 'fixed_vetted_universe'
  const prepared = config?.scope_mode === 'strategy_managed' && config.policy_hash === policy.policy_hash
  const symbols = prepared && Array.isArray(config?.symbols) ? config.symbols : []
  const intraday = policy.intraday_exit
  const tradingWindow = (value: any) => typeof value === 'string'
    ? value.replace('regular close', t('正常收盘', 'regular close')).replace('America/New_York', t('美东', 'ET'))
    : '—'
  const cap = (weight: any, absolute: any) => weight == null ? '—' : absolute == null
    ? t(`账户净值的 ${pct(weight)}`, `${pct(weight)} of account equity`)
    : t(`净值的 ${pct(weight)} 与 ${money(absolute)} 取较小值`, `Lower of ${pct(weight)} of equity or ${money(absolute)}`)
  return <div className={`live-policy-summary ${compact ? 'compact' : ''}`}>
    {!compact && <div className="live-policy-title"><strong>{t('策略自动管理交易条件', 'Strategy-managed trading conditions')}</strong><span>{t('无需填写', 'No manual inputs')}</span></div>}
    <dl className="live-policy-grid">
      <div><dt>{t('股票范围', 'Stock universe')}</dt><dd>{fixed ? t(`策略股票池 · ${policy.universe_count ?? '—'} 只`, `Strategy universe · ${policy.universe_count ?? '—'} stocks`) : policy.universe_mode === 'eligible_active_us_equities' ? t('符合策略筛选条件的活跃美股', 'Active US equities passing strategy screens') : t('由策略确定', 'Defined by strategy')}</dd></div>
      <div><dt>{t('单笔买入上限', 'Buy order cap')}</dt><dd>{cap(policy.max_order_weight, policy.max_order_absolute)}</dd></div>
      <div><dt>{t('每日新增买入上限', 'Daily added buys')}</dt><dd>{cap(policy.max_daily_added_weight, policy.max_daily_added_absolute)}</dd></div>
      <div><dt>{t('持仓上限（含外部持仓）', 'Position cap (including external holdings)')}</dt><dd>{policy.max_positions ?? '—'} {t('只', 'stocks')}</dd></div>
      {policy.max_entries_per_day != null && <div><dt>{t('每日新开仓', 'New entries per day')}</dt><dd>{t(`最多 ${policy.max_entries_per_day} 只`, `Up to ${policy.max_entries_per_day}`)}</dd></div>}
      {policy.max_industry_weight != null && <div><dt>{t('行业持仓上限', 'Industry exposure cap')}</dt><dd>{pct(policy.max_industry_weight)}</dd></div>}
      {policy.max_issuer_weight != null && <div><dt>{t('同一发行人上限', 'Issuer exposure cap')}</dt><dd>{pct(policy.max_issuer_weight)}</dd></div>}
      {policy.max_exposure_weight != null && <div><dt>{t('总敞口上限', 'Total exposure cap')}</dt><dd>{pct(policy.max_exposure_weight)}</dd></div>}
      {policy.buy_window && <div><dt>{t('买入时间', 'Entry window')}</dt><dd>{tradingWindow(policy.buy_window)}</dd></div>}
      {policy.sell_window && <div><dt>{t('卖出时间', 'Exit window')}</dt><dd>{tradingWindow(policy.sell_window)}</dd></div>}
      {intraday?.enabled === true && <>
        <div><dt>{t('盘中退出阈值', 'Intraday exit thresholds')}</dt><dd>{t(`偏离修复 ${pct(intraday.rebound_fraction)} · 亏损 ${pct(intraday.absolute_stop_loss)}`, `${pct(intraday.rebound_fraction)} dislocation repair · ${pct(intraday.absolute_stop_loss)} loss`)}</dd></div>
        <div><dt>{t('盈利回撤保护', 'Gain drawdown protection')}</dt><dd>{t(`盈利 ${pct(intraday.trailing_activation_pct)} 后，峰值回撤 ${pct(intraday.trailing_drawdown_pct)}`, `After a ${pct(intraday.trailing_activation_pct)} gain, ${pct(intraday.trailing_drawdown_pct)} below the peak`)}</dd></div>
      </>}
    </dl>
    {prepared ? <div className="live-policy-amounts"><div><span>{t('最近账户快照：单笔 / 每日上限', 'Latest account snapshot: order / daily cap')}</span><strong className="mono">{money(config.max_order_amount)} / {money(config.max_daily_added)}</strong></div><div><span>{t('计入可用现金后的单笔上限', 'Buy cap after available cash constraint')}</span><strong className="mono">{money(config.current_buy_order_ceiling)}</strong></div></div> : <p className="live-control-note">{t('点击开启后，平台同步账户并自动计算对应金额。', 'Select Enable to synchronize the account and calculate the dollar limits.')}</p>}
    <p className="live-control-note">{t('金额随账户净值自动调整，实际买入还受可用现金、现有持仓和未完成订单限制；仅整股、无杠杆。', 'Dollar limits adjust with account equity. Available cash, existing holdings and pending orders further constrain purchases. Whole shares only, without leverage.')}</p>
    {prepared && entryFeasibility?.status === 'no_affordable_candidates' && <div className="live-entry-feasibility" role="status">
      <p>{t(`按最新收盘价和当前策略预算，${entryFeasibility.candidate_count} 个候选均不足以买入 1 整股；开启后等待满足资金条件的新信号。`, `At the latest closing prices and current strategy budget, none of the ${entryFeasibility.candidate_count} candidates is affordable as one whole share. After activation, the strategy will wait for signals that fit the budget.`)}</p>
      <p>{t(`单笔可用预算 ${money(entryFeasibility.max_buy_budget)}；最低候选收盘价 ${money(entryFeasibility.minimum_candidate_close)}。`, `Available budget per order: ${money(entryFeasibility.max_buy_budget)}; lowest candidate closing price: ${money(entryFeasibility.minimum_candidate_close)}.`)}</p>
      <small>{t('仅收盘价估算，实际以执行时报价为准。', 'Closing-price estimate only. Execution uses the current market quote.')}</small>
    </div>}
    {prepared && entryFeasibility?.status === 'some_affordable_candidates' && <div className="live-entry-feasibility" role="status"><p>{t(`按最新收盘价估算，${entryFeasibility.candidate_count} 个候选中有 ${entryFeasibility.estimated_affordable_count} 个可在当前预算内买入至少 1 整股。`, `At the latest closing prices, ${entryFeasibility.estimated_affordable_count} of ${entryFeasibility.candidate_count} candidates fit at least one whole share within the current budget.`)}</p><small>{t('仅收盘价估算，实际以执行时报价及其他风控检查为准。', 'Closing-price estimate only. Current quotes and other risk checks determine execution.')}</small></div>}
    {prepared && entryFeasibility?.status === 'no_candidates' && <p className="live-control-note" role="status">{t('最新信号暂无买入候选，开启后等待新信号。', 'The latest signal has no entry candidates. After activation, the strategy will wait for new signals.')}</p>}
    {symbols.length > 0 && <button type="button" className="text-button live-policy-symbols" onClick={() => onInspect?.(
      t('策略股票池', 'Strategy universe'),
      { [t('证券数量', 'Security count')]: symbols.length, [t('证券名单', 'Symbols')]: symbols.join(', ') },
    )}>{t(`查看 ${symbols.length} 只策略股票`, `View ${symbols.length} strategy securities`)}</button>}
  </div>
}
