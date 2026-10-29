import { useCallback, useEffect, useState } from 'react'
import { ExternalLink, RefreshCw } from 'lucide-react'
import { formatNumber as decimal, formatPercent as percentage } from './format'
import './ReversalResearch.css'

type Metrics = {
  total_return?: number | null; max_drawdown?: number | null; cagr?: number | null;
  sharpe?: number | null; ending_equity?: number | null;
}
type Research = {
  status?: string; title?: string; generated_at?: string; report_available?: boolean;
  period?: { start?: string; end?: string };
  data?: { source?: string; feed?: string; symbol_count?: number; scorable_mean?: number; scorable_median?: number };
  runs?: { experiment: string; cost_bps: number; metrics: Metrics }[];
  benchmark?: { total_return?: number | null }; limitations?: string[];
}
export default function ReversalResearch({ language, onInspect }: { language: 'zh' | 'en'; onInspect: (title: string, item: Record<string, any>) => void }) {
  const [report, setReport] = useState<Research | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [reportPath, setReportPath] = useState('/api/research/reversal/report')
  const t = (zh: string, en: string) => language === 'zh' ? zh : en
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      let response = await fetch('/api/research/reversal/latest', { signal, cache: 'no-store' })
      let path = '/api/research/reversal/report'
      // Older running APIs can serve these public, account-free research
      // artifacts without restarting any trading process.
      if (response.status === 404) {
        response = await fetch('/reversal-research/summary.json', { signal, cache: 'no-store' })
        path = '/reversal-research/report.html'
      }
      if (!response.ok) throw new Error('Research unavailable')
      const data = await response.json()
      if (signal?.aborted) return
      setReport(data); setReportPath(path); setFailed(false)
    } catch {
      if (signal?.aborted) return
      setFailed(true)
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])
  const refined = report?.runs?.some(run => ['V2', 'B0', 'F1', 'X1'].includes(run.experiment)) ?? false
  const experiments = refined ? ['V2', 'B0', 'F1', 'X1'] : ['E01', 'E00']
  const labels: Record<string, string> = {
    V2: t('V2 完整改进', 'V2 Combined refinement'),
    B0: t('B0 同池原版', 'B0 Original strategy, corrected universe'),
    F1: t('F1 仅入场过滤', 'F1 Entry filters only'),
    X1: t('X1 仅退出改进', 'X1 Exit refinement only'),
    E01: t('E01 行业相对反转', 'E01 Industry-relative reversal'),
    E00: t('E00 原始跌幅反转', 'E00 Raw-return reversal'),
  }
  const rows = experiments.flatMap(experiment => {
    const run = report?.runs?.find(item => item.experiment === experiment && Number(item.cost_bps) === 10)
    return run ? [run] : []
  })
  const available = report && !['not_run', 'unavailable'].includes(report.status || '') && rows.length > 0
  const title = report?.title || (refined
    ? t('短期反转 · 基本面过滤与动态退出', 'Short-term reversal · fundamental filters and dynamic exits')
    : t('短期反转 · 真实日线初筛', 'Short-term reversal · historical daily screening'))
  return <section className="panel reversal-research" aria-label={t('短期反转研究', 'Short-term reversal research')}>
    <div className="panel-heading">
      <h2>{title}<span className="pill amber">{t('探索性研究', 'Exploratory')}</span></h2>
      <div className="reversal-actions">
        <button className="text-button" disabled={loading} onClick={() => void refresh(AbortSignal.timeout(15000))} aria-label={t('刷新研究结果', 'Refresh research')}><RefreshCw size={14} className={loading ? 'spin' : ''} />{t('刷新', 'Refresh')}</button>
        {report?.report_available && <a className="text-button" href={reportPath} target="_blank" rel="noreferrer">{t('完整报告', 'Full report')}<ExternalLink size={14} /></a>}
      </div>
    </div>
    {failed && <p className="reversal-message" role="status">{t('研究报告暂时不可读；请刷新，或确认服务已加载研究接口。', 'The research report is unavailable. Refresh or check that the service has loaded the research endpoints.')}</p>}
    {available ? <>
      <div className="reversal-context"><strong>{report.period?.start || '—'} → {report.period?.end || '—'}</strong><span>{report.data?.source || '—'}{report.data?.feed ? ` / ${report.data.feed}` : ''}</span><span>{report.data?.symbol_count ?? '—'} {t('只样本证券', 'sample securities')}{report.data?.scorable_mean != null && ` · ${t('日均可评分', 'daily scored average')} ${decimal(report.data.scorable_mean)}`}</span><span>{t('每边全包成本 10 bp', 'All-in cost: 10 bp per side')}</span></div>
      <div className="table-scroll"><table><thead><tr><th>{t('策略', 'Strategy')}</th><th>{t('成本后总收益', 'Net total return')}</th><th>{t('年化收益', 'CAGR')}</th><th>{t('最大回撤', 'Max drawdown')}</th><th>{t('夏普', 'Sharpe')}</th></tr></thead><tbody>{rows.map(run => <tr key={run.experiment}><td><strong>{labels[run.experiment]}</strong></td><td className="mono">{percentage(run.metrics.total_return)}</td><td className="mono">{percentage(run.metrics.cagr)}</td><td className="mono">{percentage(run.metrics.max_drawdown)}</td><td className="mono">{decimal(run.metrics.sharpe)}</td></tr>)}</tbody></table></div>
      <div className="reversal-context"><span>{t('同期 SPY 买入持有收益', 'SPY buy-and-hold return over the same period')}: <strong>{percentage(report.benchmark?.total_return)}</strong></span></div>
      <p className="reversal-message">{t('日线开盘价格代理回测，尚未验证真实成交或策略 Alpha；与 SPY 的仓位及风险可能不同。策略选用与实盘开启状态以上方平台状态为准。', 'Daily-open proxy backtest; actual execution and strategy alpha are unvalidated. Exposure and risk may differ from SPY. See the platform status above for strategy selection and live activation.')}</p>
      {Array.isArray(report.limitations) && report.limitations.length > 0 && <button className="text-button" onClick={() => onInspect(t('研究说明', 'Research notes'), { [t('数据范围与结论限制', 'Data coverage and limitations')]: report.limitations })}>{t('研究说明', 'Research notes')}</button>}
    </> : !failed && <p className="reversal-message" role="status">{loading ? t('正在读取研究报告…', 'Loading research report…') : t('尚未生成真实历史研究结果。', 'No historical research results have been generated yet.')}</p>}
  </section>
}
