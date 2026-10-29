#!/usr/bin/env python3
"""Run the frozen daily proxy protocol; never connects to a broker.

Usage: uv run --group research python scripts/run_reversal_research.py
The 2024+ partition is read only after an immutable code/data/config seal.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timezone
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from trading.reversal_backtest import ReversalBacktestConfig, run_backtest
from trading.reversal_actions import normalize_corporate_actions
from trading.reversal_data import load_exploration_panel, read_json
from trading.reversal_evaluation import benchmark_and_uncertainty, signal_diagnostics
from trading.reversal_signals import SignalConfig, prepare_signals, candidates_for

ROOT = Path(__file__).resolve().parents[1]


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def write_json(path, value):
    raw = json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.gz':
        with gzip.open(path, 'wt', encoding='utf-8') as f:
            f.write(raw)
    else:
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(raw, encoding='utf-8')
        os.replace(temp, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def percent(value):
    return '—' if value is None else f'{value:+.2%}'


def dual_source_metadata(evidence):
    """Describe observed cross-check coverage, never infer full-market approval."""
    comparisons = []
    if isinstance(evidence, dict):
        successful = {call.get('label') for call in evidence.get('calls', [])
                      if call.get('status') == 'passed' and call.get('tool') == 'get_equity_historicals'}
        for label, symbols in evidence.get('comparison', {}).items():
            if label not in successful or not isinstance(symbols, dict):
                continue
            for symbol, details in symbols.items():
                if isinstance(details, dict) and details.get('valid_overlap', 0) > 0:
                    comparisons.append({'period': label, 'symbol': symbol,
                                        'valid_overlap': details['valid_overlap']})
    return {'status': 'sampled_history_compared' if comparisons else 'unavailable',
            'sampled_symbols': sorted({row['symbol'] for row in comparisons}),
            'comparisons': comparisons,
            'scope': 'Read-only sampled historical OHLCV comparison; not full-universe, corporate-action or execution validation.'}


def public_summary(summary):
    """Allowlisted research-only fields; no full data, trades or account state."""
    allowed = ('status', 'title', 'period', 'limitations', 'generated_at')
    result = {key: summary[key] for key in allowed if key in summary}
    result['data'] = {key: summary['data'][key] for key in
                      ('source', 'feed', 'symbol_count', 'selected_sample_count',
                       'quarantined_count', 'dataset_version', 'crosscheck_description',
                       'scorable_mean', 'scorable_median')
                      if key in summary.get('data', {})}
    result['runs'] = [{'experiment': run['experiment'], 'cost_bps': run['cost_bps'],
                       'metrics': {key: run['metrics'].get(key) for key in
                                   ('total_return', 'max_drawdown', 'cagr', 'sharpe', 'ending_equity')}}
                      for run in summary['runs']]
    result['benchmark'] = {'total_return': summary['benchmark'].get('total_return')}
    result['report_available'] = True
    return result


def apply_identity_quarantine(panel, quarantine):
    excluded = set((quarantine or {}).get('excluded_security_ids', []))
    return panel.loc[~panel.security_id.isin(excluded)].copy()


def charts(output, runmap, diagnostics, main_period):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(3, 2, figsize=(15, 13), layout='constrained')
    colors = {'E01': '#087f8c', 'E00': '#b06a3b'}
    for experiment in ['E01', 'E00']:
        r = runmap[(main_period, experiment, 10, 100000)]
        eq = pd.DataFrame(r['equity'])
        dates = pd.to_datetime(eq.session)
        axes[0, 0].plot(dates, eq.nav / 100000, label=experiment, color=colors[experiment])
        axes[0, 1].plot(dates, 100 * eq.drawdown, label=experiment, color=colors[experiment])
    r = runmap[(main_period, 'E01', 10, 100000)]
    bm = pd.DataFrame(r['benchmark']['curve'])
    axes[0, 0].plot(pd.to_datetime(bm.session), bm.spy, label='SPY adjusted gross', color='#657385', alpha=.8)
    axes[0, 0].plot(pd.to_datetime(bm.session), bm.market_cash, label='SPY + cash proxy', color='#9475b5', linestyle='--')
    axes[0, 0].set_title('Frozen period: net equity (10 bps per side)')
    axes[0, 1].set_title('Drawdown (%)')
    for experiment in ['E01', 'E00']:
        costs = [0, 2, 5, 10, 20]
        returns = [100 * runmap[(main_period, experiment, cost, 100000)]['metrics']['total_return'] for cost in costs]
        axes[1, 0].plot(costs, returns, marker='o', label=experiment, color=colors[experiment])
        decay = diagnostics[main_period][experiment]['top_decile_decay_gross']
        axes[1, 1].plot([int(k) for k in decay], [100 * v for v in decay.values()], marker='o', label=experiment, color=colors[experiment])
        deciles = diagnostics[main_period][experiment]['deciles']
        axes[2, 0].plot([v['decile'] for v in deciles], [100 * v['forward_5'] for v in deciles], marker='o', label=experiment, color=colors[experiment])
        annual = r['annual'] if experiment == 'E01' else runmap[(main_period, experiment, 10, 100000)]['annual']
        offset = -.18 if experiment == 'E01' else .18
        axes[2, 1].bar([v['year'] + offset for v in annual], [100 * v['return'] for v in annual], width=.35, label=experiment, color=colors[experiment])
    axes[1, 0].set(title='All-inclusive cost sensitivity', xlabel='Cost per side (bps)', ylabel='Cumulative return (%)')
    axes[1, 1].set(title='Top-decile gross signal decay', xlabel='Trading-day holding intervals', ylabel='Open-to-open return (%)')
    axes[2, 0].set(title='5-day gross signal deciles (1 = highest score)', xlabel='Score decile', ylabel='Open-to-open return (%)')
    axes[2, 1].set(title='Calendar-year net results (continuous ledger)', ylabel='Return (%)')
    for ax in axes.flat:
        ax.grid(alpha=.18)
        ax.legend(fontsize=8)
    fig.suptitle('US equity reversal | historical daily proxy | exploratory, not execution validated', fontsize=16)
    fig.savefig(output / 'research_charts.png', dpi=160)
    fig.savefig(output / 'research_charts.svg')
    plt.close(fig)


def render_html(output, summary, periods, diagnostics, primary):
    esc = html.escape
    def table(headers, rows):
        return '<table><thead><tr>' + ''.join(f'<th>{esc(str(x))}</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{esc(str(v))}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table>'
    rows = []
    for name, result in periods.items():
        for exp in ['E01', 'E00']:
            r = next(v for v in result['runs'] if v['experiment'] == exp and v['cost_bps'] == 10)
            m = r['metrics']
            rows.append([name, result['start'] + ' → ' + result['end'], exp, percent(m['total_return']), percent(m['cagr']),
                         percent(m['max_drawdown']), f"{m['sharpe']:.2f}" if m['sharpe'] is not None else '—', m['trade_count'], percent(m['average_exposure'])])
    costs = []
    for bps in [0, 2, 5, 10, 20]:
        r = [next(r for r in summary['runs'] if r['experiment'] == exp and r['cost_bps'] == bps) for exp in ['E01', 'E00']]
        costs.append([f'{bps} bp', percent(r[0]['metrics']['total_return']), percent(r[1]['metrics']['total_return'])])
    bm = primary['benchmark']
    ci = bm.get('block_bootstrap', {}).get('20', {})
    ci_text = '无可用区间' if 'lower_95' not in ci else f"每日均值 {percent(ci['mean_daily_excess'])}，95%区间 [{percent(ci['lower_95'])}, {percent(ci['upper_95'])}]"
    annual_rows = [[a['year'], percent(a['return']), percent(a['max_drawdown']), percent(a['average_exposure']), f"${a['costs']:,.2f}"] for a in primary['annual']]
    limitations = ''.join(f'<li>{esc(s)}</li>' for s in summary['limitations'])
    svg = (output / 'research_charts.svg').read_text()
    svg = svg[svg.find('<svg'):]
    main = primary['metrics']
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>美股行业相对反转 · 首轮回测</title>
<style>body{{font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#243341;background:#f6f8fa;margin:0}}main{{max-width:1120px;margin:40px auto;padding:32px;background:white;border:1px solid #dce3e9;border-radius:14px}}h1{{font-size:29px;line-height:1.3}}h2{{font-size:20px;margin-top:34px}}.tag{{color:#8d5a15;background:#fff3d6;padding:7px 11px;border-radius:6px;display:inline-block}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#f0f5f7;padding:17px;border-radius:9px}}.card b{{display:block;font-size:25px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:right;border-bottom:1px solid #e0e6ec;padding:10px}}th:first-child,td:first-child{{text-align:left}}th{{background:#f0f4f7}}.scroll{{overflow:auto}}svg{{width:100%;height:auto}}small{{color:#647484}}a{{color:#087f8c}}li{{margin-bottom:7px}}code{{background:#f0f4f7;padding:2px 4px}}@media(max-width:700px){{main{{margin:0;padding:20px}}.cards{{grid-template-columns:repeat(2,1fr)}}}}</style>
<main><span class="tag">真实历史日线 · 探索性初筛 · 未启用实盘</span><h1>美股短期行业相对反转：第一版策略与回测</h1>
<p>按研究方案固定 E01（行业相对反转），并用 E00（原始跌幅反转）作同样本对照。参数未按收益调优。主展示是冻结评估区间 {esc(summary['period']['start'])} 至 {esc(summary['period']['end'])}，初始模拟资金 $100,000，每边成本 10 bp。</p>
<div class="cards"><div class="card">E01 累计净收益<b>{percent(main['total_return'])}</b></div><div class="card">E01 年化收益<b>{percent(main['cagr'])}</b></div><div class="card">最大回撤<b>{percent(main['max_drawdown'])}</b></div><div class="card">期末权益<b>${main['ending_equity']:,.2f}</b></div></div>
<h2>可以怎样解读</h2><p>{esc(summary['conclusion'])}</p>
<p>SPY 总收益代理 {percent(bm['total_return'])}；按前日股票仓位配置 SPY、其余现金的描述性基准 {percent(bm['market_cash_return'])}。E01 相对该仓位基准的20交易日区块重采样结果：{ci_text}。该区间不包含数据偏差，不是原方案要求的行业/规模/Beta/波动匹配 Alpha 检验。</p>
<h2>固定策略</h2><p>月末按过去60日原始成交额中位数选池，下月生效；原始股价≥$5、至少252个已观察交易日。股票5日总收益代理减去同行（剔除自身）逐日等权收益的5日复合，取负数作为 E01；每天每组至少5个其他有效同行。E00、E01使用同一可评分全集，排序后只考虑前10%且分数&gt;0的股票。</p>
<p>次日开盘价格代理入场，持有5个交易日间隔；每日最多4只、同时最多20只、单票/发行人5%、行业25%、当日买入20%。整股、只做多、不借款、不加仓、不因重复信号延期、不做同日重入。按历史T+3/T+2/T+1结算限制购买力，现金收益为零。退出失败保留敞口，期末按观察价格估值。</p>
<h2>分段结果与对照</h2><div class="scroll">{table(['区间','日期','策略','累计净收益','年化','最大回撤','夏普','已平仓交易','平均股票仓位'],rows)}</div><p><small>各区间从现金启动，年度切片使用该区间同一连续账本。冻结评估结果已查看，不能将它重复用于新参数选择再称为新样本外。</small></p>
{svg}
<h2>成本敏感性</h2>{table(['每边全包成本','E01累计收益','E00累计收益'],costs)}<p>0 bp 是毛收益代理；2/5/10/20 bp 是假设情景，不代表券商收费。不同成本会影响整股数量与可用现金，因此各行均重新运行完整账本。税费之外的固定数据、MCP与服务器费用尚未提供，项目利润另需扣除。</p>
<h2>E01年度结果</h2>{table(['年份','净收益','年内最大回撤','平均仓位','模型成本'],annual_rows)}
<h2>数据和双源使用</h2><p>Alpaca SIP 提供原始、仅拆股复权及完整复权日线。{esc(summary['data']['crosscheck_description'])} 不拼接两源成交量或平均价格，抽样核对不代表全样本或公司行动已通过验收；两源差异、插值与数据缺失保存在质量报告。</p><p>当前存续普通股候选中按2016年末流动性预选 {summary['data']['selected_sample_count']} 只，其中 {summary['data']['symbol_count']} 只有有效研究价格与分类。主评估期 E00/E01 共同可评分股票数日均 {summary['data']['scorable_mean']:.1f} 只、中位数 {summary['data']['scorable_median']:.1f} 只；行业不足5个有效同行等情况不评分。该样本并非原方案要求的无幸存者偏差历史全市场前500。SIC来自当前SEC记录，不能假称历史分类。</p>
<h2>尚不能由此证明的内容</h2><ul>{limitations}</ul>
<h2>复现与审计</h2><p>运行：<code>uv run --group research python scripts/run_reversal_research.py --snapshot data/reversal/{esc(summary['data']['dataset_version'])}</code>。完整结果、逐笔成交、拒绝原因、公司行动账本、每日净值、全部评分、覆盖统计、配置/代码/数据哈希保存在本地研究运行目录。公开页面只发布汇总结果。<code>report.json</code>为本地机器可读入口；<code>freeze.json</code>记录打开2024+数据前的封存。</p>
<p>依据：<a href="https://chatgpt.com/space/page_15b6fdc3269c8191b1c7dc5080ba6f4d">用户研究方案</a> · <a href="https://docs.alpaca.markets/us/reference/stockbars">Alpaca历史行情口径</a> · <a href="https://robinhood.com/us/en/support/articles/trading-with-your-agent/">Robinhood官方工具能力</a> · <a href="https://www.sec.gov/search-filings/edgar-application-programming-interfaces">SEC公开数据</a></p><small>生成时间 {esc(summary['generated_at'])} · 策略版本 industry_reversal_daily_proxy_v1 · 个人税前</small></main></html>'''
    (output / 'report.html').write_text(body, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', default=str(ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2'))
    parser.add_argument('--output', default=None)
    args = parser.parse_args()
    snapshot = Path(args.snapshot)
    generated = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    output = Path(args.output) if args.output else ROOT / 'data/reversal/runs' / run_id
    output.mkdir(parents=True, exist_ok=False)
    config_path = ROOT / 'config/reversal_research_v1.json'
    config = json.loads(config_path.read_text())
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    config_hash, manifest_hash = digest(config_path), digest(snapshot / 'manifest.json')
    dual_path = ROOT / 'data/reversal/dual_source_validation.json'
    dual_evidence = json.loads(dual_path.read_text()) if dual_path.exists() else None
    dual_metadata = dual_source_metadata(dual_evidence)
    if dual_evidence is not None:
        write_json(output / 'dual_source_validation.json', dual_evidence)
    quarantine_path = snapshot / 'identity_quarantine.json'
    quarantine = json.loads(quarantine_path.read_text()) if quarantine_path.exists() else None
    quarantine_hash = digest(quarantine_path) if quarantine is not None else None
    if quarantine is not None:
        write_json(output / 'identity_quarantine.json', quarantine)
    verified_files = ['daily_bars.csv.gz', 'daily_bars_holdout_LOCKED.csv.gz', 'corporate_actions.json.gz']
    for name in verified_files:
        if digest(snapshot / name) != manifest['files'][name]['sha256']:
            raise ValueError('Snapshot checksum mismatch')
    actions = read_json(snapshot / 'corporate_actions.json.gz')
    if not isinstance(actions, list):
        raise ValueError('Corporate actions must be a list of source events')
    codepaths = [Path(__file__), *sorted((ROOT / 'backend/trading').glob('reversal*.py'))]
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in codepaths}
    # Written before any strategy return is computed; fixed E01 always remains
    # primary even if E00 is better. Cost sweeps cannot promote a new signal.
    write_json(output / 'freeze.json', {'frozen_at': generated, 'config': config,
               'config_sha256': config_hash, 'code_sha256': hashes,
               'manifest_sha256': manifest_hash,
               'verified_file_sha256': {name: manifest['files'][name]['sha256'] for name in verified_files},
               'dual_source_evidence_sha256': digest(output / 'dual_source_validation.json') if dual_evidence is not None else None,
               'identity_quarantine_sha256': quarantine_hash,
               'project_holdout_opened': False, 'confirmatory': False,
               'reason': 'Fixed E00/E01 protocol; incomplete point-in-time data prevents confirmatory claims.'})
    shutil.copy2(config_path, output / 'config.json')
    raw = apply_identity_quarantine(load_exploration_panel(snapshot), quarantine)
    signal_cfg = SignalConfig(**{f.name: config[f.name] for f in fields(SignalConfig) if f.name in config})
    ledger_cfg = {f.name: config[f.name] for f in fields(ReversalBacktestConfig) if f.name in config and f.name != 'cost_bps'}
    raw, development_actions = normalize_corporate_actions(
        raw, actions, price_quantum=ledger_cfg.get('adjusted_price_quantum', 0.01))
    write_json(output / 'development_corporate_action_audit.json', development_actions)
    panel, scores, coverage = prepare_signals(raw, signal_cfg)
    periods, diagnostics, runmap, trial_registry = {}, {}, {}, []

    def run_period(name, start, end, costs):
        usable = panel[panel.session.le(end)].copy()
        prior = usable.loc[usable.session.lt(start), 'session'].max()
        usable = usable[usable.session.ge(prior if isinstance(prior, str) else start)]
        period_runs = []
        for experiment in ['E00', 'E01']:
            candidates = candidates_for(scores, experiment)
            candidates = candidates[candidates.signal_session.ge(start) & candidates.signal_session.le(end)]
            for cost in costs:
                cfg = ReversalBacktestConfig(**ledger_cfg, cost_bps=cost)
                result = run_backtest(usable, candidates, cfg, start, end)
                m = result['metrics']
                m['cagr'], m['ending_equity'] = m['annualized_return'], m['final_equity']
                if cost == 10:
                    bootstrap = config.get('bootstrap', {})
                    result['benchmark'] = benchmark_and_uncertainty(
                        result, usable, repetitions=bootstrap.get('repetitions', 2000),
                        seed=bootstrap.get('seed', 20261027), blocks=bootstrap.get('block_sessions', [10, 20, 60]))
                    if result['benchmark']['status'] != 'descriptive_proxy':
                        raise ValueError(f'Incomplete SPY benchmark in {name}; repair data before publishing performance')
                filename = f'{name}_{experiment}_{cost}bp.json.gz'
                write_json(output / filename, result)
                trial = {'period': name, 'experiment': experiment, 'cost_bps': cost, 'initial_cash': cfg.initial_cash,
                         'metrics': m, 'result_file': filename, 'result_sha256': digest(output / filename)}
                trial_registry.append(trial)
                period_runs.append(trial)
                runmap[(name, experiment, cost, cfg.initial_cash)] = result
                print(json.dumps({'period': name, 'experiment': experiment, 'cost_bps': cost,
                                  'return': m['total_return'], 'drawdown': m['max_drawdown'],
                                  'trades': m['trade_count']}), flush=True)
        diagnostics[name] = signal_diagnostics(panel[panel.session.le(end)], scores, start, end)
        periods[name] = {'start': start, 'end': end, 'runs': period_runs}
        write_json(output / 'trials.json', trial_registry)

    protocol = config['split_protocol']
    for name in ['development', 'validation']:
        run_period(name, *protocol[name], [0, 10])
    # No selection or mutation occurs between development and opening holdout.
    if hashes != {str(p.relative_to(ROOT)): digest(p) for p in codepaths}:
        raise ValueError('Research code changed after freeze; start a new audited run')
    if digest(config_path) != config_hash or digest(snapshot / 'manifest.json') != manifest_hash:
        raise ValueError('Research configuration or manifest changed after freeze')
    if quarantine_hash is not None and digest(quarantine_path) != quarantine_hash:
        raise ValueError('Identity quarantine changed after freeze')
    for filename in verified_files:
        if digest(snapshot / filename) != manifest['files'][filename]['sha256']:
            raise ValueError('Frozen research prices changed before holdout evaluation')
    write_json(output / 'holdout_opened.json', {'opened_at': datetime.now(timezone.utc).isoformat(),
               'config_sha256': config_hash, 'strategy': 'E01',
               'read_once_for_this_version': True, 'repeated_runs_are_reproduction_not_new_holdouts': True})
    reserved = pd.read_csv(snapshot / 'daily_bars_holdout_LOCKED.csv.gz',
                           dtype={'security_id': str, 'issuer_id': str, 'industry': str}, keep_default_na=False)
    reserved = apply_identity_quarantine(reserved, quarantine)
    raw = pd.concat([raw, reserved], ignore_index=True)
    # Rebuild the accounting annotations over the complete chronological panel,
    # including the prior observations needed by events at the split boundary.
    raw, complete_actions = normalize_corporate_actions(
        raw, actions, price_quantum=ledger_cfg.get('adjusted_price_quantum', 0.01))
    write_json(output / 'corporate_action_audit.json', complete_actions)
    panel, scores, coverage = prepare_signals(raw, signal_cfg)
    scores.to_csv(output / 'all_scores.csv.gz', index=False, compression='gzip')
    coverage.to_csv(output / 'daily_coverage.csv', index=False)
    name = 'frozen_evaluation'
    run_period(name, *protocol[name], config['cost_bps'])
    primary = runmap[(name, 'E01', 10, 100000)]
    candidates = candidates_for(scores, 'E01')
    start, end = protocol[name]
    candidates = candidates[candidates.signal_session.ge(start) & candidates.signal_session.le(end)]
    capacity = []
    for capital in [10000, 1000000]:
        cfg = ReversalBacktestConfig(**{**ledger_cfg, 'initial_cash': capital}, cost_bps=10)
        result = run_backtest(panel[panel.session.ge('2023-12-29') & panel.session.le(end)], candidates, cfg, start, end)
        filename = f'{name}_E01_10bp_{capital}usd.json.gz'
        write_json(output / filename, result)
        capacity.append({'initial_cash': capital, 'metrics': result['metrics'], 'result_file': filename,
                         'interpretation': 'Whole-share and cash-budget sensitivity only; market-impact/depth capacity is not measured.'})
    limits = [
        '当前存续证券筛选与当前SEC行业分类回填，存在幸存者偏差和分类前视偏差；不能认定历史Alpha。',
        '样本按2016年末流动性固定预选，非历史全市场月度前500；后续上市和退市股票覆盖不足。',
        'next-open日线成交代理尚未验证09:35–09:40窗口、5秒延迟、价差、深度、部分成交和退出排队。',
        '收益使用供应商复权总收益代理；公司行动以可用事件或复权因子重建，未知分配支付日按30日延迟，非完整退市/并购会计账本。',
        '未取得历史市值/行业/公告可用时间，行业-规模-Beta-波动匹配基准与财报过滤尚未运行。',
        '现金收益设为零，固定数据/API/服务器成本和个人税务未计；模拟资金规模诊断不是可交易容量结论。',
        '冻结评估区间已打开；后续调整参数必须登记新版本，不能继续称该区间为未查看保留期。',
        '研究模块只保存结果，不改变当前运行策略，不发送真实或模拟券商订单。',
    ]
    if primary['metrics']['stale_position_days']:
        limits.append(f"存在 {primary['metrics']['stale_position_days']} 个持仓日沿用旧估值，退出/退市回收资料不足；净值可能高估，当前结果不完整。")
    if quarantine is not None:
        limits.extend(quarantine.get('remaining_limitations', []))
        limits.append('表现计算前按证券身份验收规则隔离整个历史序列：' + '、'.join(quarantine.get('excluded_symbols', [])) + '；原始数据与隔离依据均保留。')
    main_net = primary['metrics']['total_return']
    raw_net = runmap[(name, 'E00', 10, 100000)]['metrics']['total_return']
    conclusion = ('该固定版本在本样本和10bp每边成本假设下盈利。' if main_net > 0 else
                  '该固定版本在本样本和10bp每边成本假设下亏损。' if main_net < 0 else
                  '该固定版本在本样本和10bp每边成本假设下净收益为零。')
    conclusion += ('行业调整后的净收益高于原始跌幅基线。' if main_net > raw_net else '行业调整后的净收益未超过原始跌幅基线。')
    conclusion += '由于历史股票池、行业和执行数据未通过完整验收，结论仍是证据不足，不能据此启用实盘或宣称发现非理性定价。'
    crosschecked = dual_metadata['status'] == 'sampled_history_compared'
    crosscheck_description = ('Robinhood仅对 ' + '、'.join(dual_metadata['sampled_symbols']) + ' 的指定历史区间进行独立OHLCV抽样核对。'
                              if crosschecked else '本次没有可核验的Robinhood历史交叉核对证据。')
    limits.append(crosscheck_description + '该步骤不能证明整个股票池、完整历史或成交执行都已通过双源验收。')
    primary_coverage = coverage[coverage.session.ge(primary['start']) & coverage.session.le(primary['end'])]
    summary = {'status': 'exploratory', 'title': '行业相对反转 E01 · 真实日线初筛',
               'period': {'start': primary['start'], 'end': primary['end']},
               'data': {'source': 'Alpaca; Robinhood sampled history comparison' if crosschecked else 'Alpaca', 'feed': 'SIP historical',
                        'symbol_count': int(panel.loc[panel.symbol.ne('SPY'), 'symbol'].nunique()),
                        'selected_sample_count': manifest['sample_count'], 'dataset_version': manifest['dataset_version'],
                        'quarantined_count': len((quarantine or {}).get('excluded_security_ids', [])),
                        'scorable_mean': float(primary_coverage.scorable_count.mean()),
                        'scorable_median': float(primary_coverage.scorable_count.median()),
                        'crosscheck_description': crosscheck_description, 'crosscheck_coverage': dual_metadata},
               'runs': periods[name]['runs'], 'benchmark': {k: v for k, v in primary['benchmark'].items() if k != 'curve'},
               'limitations': limits, 'generated_at': generated, 'conclusion': conclusion,
               'profitability_validated': False, 'execution_validated': False,
               'run_id': run_id, 'primary_experiment': 'E01', 'primary_cost_bps': 10}
    report = {'summary': summary, 'periods': periods, 'diagnostics': diagnostics,
              'capacity_diagnostics': capacity, 'config': config, 'manifest': manifest,
              'dual_source_validation': dual_evidence,
              'identity_quarantine': quarantine,
              'corporate_action_audit': complete_actions,
              'coverage_summary': coverage[coverage.session.ge('2017-01-01')].drop(columns='session').describe().to_dict(),
              'research_status': {'G0': 'not_passed', 'G1': 'exploratory_only', 'G2': 'not_validated',
                                  'G3': 'not_eligible', 'G4': 'not_run', 'live': 'not_enabled_by_this_research'}}
    write_json(output / 'report.json', report)
    charts(output, runmap, diagnostics, name)
    render_html(output, summary, periods, diagnostics, primary)
    latest = ROOT / 'data/reversal/latest'
    latest.mkdir(parents=True, exist_ok=True)
    for name in ['report.json', 'report.html', 'research_charts.png', 'research_charts.svg']:
        temporary = latest / (name + '.tmp')
        shutil.copy2(output / name, temporary)
        os.replace(temporary, latest / name)
    # Public copies contain summary statistics and a self-contained chart only.
    # Both paths let an already running API serve the result without a restart.
    for destination in [ROOT / 'frontend/public/reversal-research', ROOT / 'frontend/dist/reversal-research']:
        destination.mkdir(parents=True, exist_ok=True)
        temporary = destination / 'report.html.tmp'
        shutil.copy2(output / 'report.html', temporary)
        os.replace(temporary, destination / 'report.html')
        write_json(destination / 'summary.json', public_summary(summary))
    print(json.dumps({'completed': str(output), 'report': str(latest / 'report.html'), 'summary': summary}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
