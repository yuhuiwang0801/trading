#!/usr/bin/env python3
"""Fixed-hypothesis V2 historical retest. Read-only inputs, no broker access."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import fields
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from trading.reversal_actions import normalize_corporate_actions
from trading.reversal_backtest import ReversalBacktestConfig, run_backtest
from trading.reversal_data import read_json
from trading.reversal_evaluation import benchmark_and_uncertainty
from trading.reversal_refinement import prepare_refinement, refined_candidates
from trading.reversal_signals import SignalConfig, prepare_signals
from run_reversal_research import write_json, digest, public_summary

ROOT = Path(__file__).resolve().parents[1]
LABELS = {'B0': '原版 E01 · 修正股票池', 'X1': '仅改退出规则', 'F1': '仅加入场过滤', 'V2': '完整改进 V2'}
COLORS = {'B0': '#727b90', 'X1': '#b46c32', 'F1': '#6576c7', 'V2': '#087f8c'}


def paired_comparison(first, baseline, repetitions=2000, blocks=(10, 20, 60), seed=20261027):
    a = pd.DataFrame(first['equity']).set_index('session')
    b = pd.DataFrame(baseline['equity']).set_index('session')
    if not a.index.equals(b.index):
        raise ValueError('Paired results require identical sessions')
    values = (a.daily_return - b.daily_return).to_numpy()
    result = {'mean_daily_difference': float(values.mean()), 'block_intervals': {},
              'interpretation': 'Paired daily return difference, not a risk-matched alpha estimate; all history previously viewed.'}
    for block in blocks:
        n = len(values)
        if n < block * 2:
            continue
        rng = np.random.default_rng(seed)
        indices = (rng.integers(0, n, size=(repetitions, int(np.ceil(n / block)), 1)) + np.arange(block)) % n
        means = values[indices.reshape(repetitions, -1)[:, :n]].mean(axis=1)
        lo, hi = np.quantile(means, [.025, .975])
        result['block_intervals'][str(block)] = {'lower_95': float(lo), 'upper_95': float(hi), 'repetitions': repetitions}
    return result


def trade_statistics(run):
    fills = pd.DataFrame(run['fills'])
    trades = pd.DataFrame(run['trades'])
    equity = pd.DataFrame(run['equity'])
    buys = fills[fills.side.eq('buy')] if not fills.empty else pd.DataFrame()
    sells = fills[fills.side.eq('sell')] if not fills.empty else pd.DataFrame()
    return {'buy_count': len(buys), 'sell_count': len(sells),
            'buys_per_session': len(buys) / len(equity), 'sells_per_session': len(sells) / len(equity),
            'mean_holding_sessions': float(trades.holding_intervals.mean()) if not trades.empty else None,
            'median_holding_sessions': float(trades.holding_intervals.median()) if not trades.empty else None,
            'mean_position_count': float(equity.position_count.mean()),
            'unique_bought_symbols': int(buys.symbol.nunique()) if not buys.empty else 0,
            'exit_reasons': dict(Counter(trades.exit_reason)) if 'exit_reason' in trades else {'FIXED_HOLD': len(trades)},
            'turnover_one_way_notional_over_average_nav': float((equity.buy_notional + equity.sell_notional).sum() / equity.nav.mean())}


def charts(output, runmap):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(3, 2, figsize=(15, 13), layout='constrained')
    for label in ['B0', 'X1', 'F1', 'V2']:
        run = runmap[('recent_retest', label, 10)]
        e = pd.DataFrame(run['equity'])
        dates = pd.to_datetime(e.session)
        axes[0, 0].plot(dates, e.nav / 100000, label=label, color=COLORS[label], lw=1.5)
        axes[0, 1].plot(dates, e.drawdown * 100, label=label, color=COLORS[label], lw=1)
        axes[1, 0].plot(dates, e.exposure.rolling(20, min_periods=1).mean() * 100, label=label, color=COLORS[label], lw=1.1)
    primary = runmap[('recent_retest', 'V2', 10)]
    spy = pd.DataFrame(primary['benchmark']['curve'])
    axes[0, 0].plot(pd.to_datetime(spy.session), spy.spy, label='SPY gross', color='#9ea7a6', ls=':', lw=1.3)
    reasons = primary['trade_statistics']['exit_reasons']
    axes[1, 1].barh(list(reasons), list(reasons.values()), color='#087f8c')
    trades = pd.DataFrame(primary['trades'])
    if not trades.empty:
        counts = trades.holding_intervals.value_counts().sort_index()
        axes[2, 0].bar(counts.index, counts.values, color='#087f8c')
    for label in ['B0', 'V2']:
        costs = [5, 10, 20]
        axes[2, 1].plot(costs, [runmap[('recent_retest', label, cost)]['metrics']['total_return'] * 100 for cost in costs],
                        marker='o', color=COLORS[label], label=label)
    titles = ['Equity / initial capital (10 bp per side)', 'Drawdown (%)', 'Stock exposure: 20-session average (%)',
              'V2 exit reasons (closed trades)', 'V2 holding intervals (sessions)', 'Cost sensitivity: total return (%)']
    for ax, title in zip(axes.flat, titles):
        ax.set_title(title, loc='left', fontweight='bold')
        ax.grid(axis='y', alpha=.15)
    for ax in [axes[0, 0], axes[0, 1], axes[1, 0], axes[2, 1]]:
        ax.legend(frameon=False, fontsize=9)
    axes[2, 1].set_xlabel('All-inclusive cost, bp per side')
    for ax in [axes[0, 0], axes[0, 1], axes[1, 0]]:
        ax.tick_params(axis='x', labelrotation=25)
    fig.suptitle('Industry reversal V2 | Historical exploratory retest | 2024-2026', fontsize=16, fontweight='bold')
    fig.savefig(output / 'research_charts.png', dpi=160)
    fig.savefig(output / 'research_charts.svg')
    plt.close(fig)


def render_report(output, report):
    esc = html.escape
    p = lambda value: '—' if value is None else f'{value:+.2%}'
    n = lambda value: '—' if value is None else f'{value:,.2f}'
    summary = report['summary']
    rows = []
    for trial in report['periods']['recent_retest']['runs']:
        if trial['cost_bps'] != 10:
            continue
        m, st = trial['metrics'], trial['trade_statistics']
        rows.append(f'<tr><th>{esc(LABELS[trial["experiment"]])}</th><td>{p(m["total_return"])}</td><td>{p(m["cagr"])}</td><td>{p(m["max_drawdown"])}</td><td>{n(m["sharpe"])}</td><td>{p(m["average_exposure"])}</td><td>{n(st["mean_holding_sessions"])}</td><td>{st["sell_count"]:,}</td></tr>')
    period_rows = []
    for period, details in report['periods'].items():
        by = {r['experiment']: r for r in details['runs'] if r['cost_bps'] == 10}
        period_rows.append(f'<tr><th>{details["start"]} → {details["end"]}</th>' + ''.join(
            f'<td>{p(by[x]["metrics"]["total_return"])}<small>回撤 {p(by[x]["metrics"]["max_drawdown"])}</small></td>' for x in ['B0', 'X1', 'F1', 'V2']) + '</tr>')
    stats = report['primary_trade_statistics']
    exclusion = ''.join(f'<li>{esc(reason)}：{count:,} 个候选日</li>' for reason, count in report['screening']['recent_retest']['base_candidate_gate_counts'].items())
    reasons = ''.join(f'<tr><th>{esc(reason)}</th><td>{count:,}</td></tr>' for reason, count in stats['exit_reasons'].items())
    limitations = ''.join(f'<li>{esc(x)}</li>' for x in summary['limitations'])
    image = base64.b64encode((output / 'research_charts.png').read_bytes()).decode()
    ci = report['comparisons']['recent_retest']['V2_minus_B0']['block_intervals']['20']
    text = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>反转策略 V2 · 基本面筛选与反弹退出</title>
<style>body{{font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f3f5f6;color:#1f2f3b;margin:0}}main{{max-width:1240px;margin:auto;padding:40px 28px}}h1{{font-size:34px;line-height:1.3;margin:12px 0}}h2{{font-size:23px;margin-top:0}}.eyebrow{{color:#087f8c;font-weight:700;letter-spacing:.12em}}.lead{{font-size:19px;color:#405567;max-width:1000px}}section{{background:#fff;padding:28px;border:1px solid #e1e6e8;border-radius:14px;margin:22px 0}}.note{{background:#fff7e6;border-left:4px solid #c78d2e;padding:16px 20px;border-radius:8px}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;white-space:nowrap}}th,td{{padding:12px;text-align:right;border-bottom:1px solid #e8ecef}}th:first-child{{text-align:left}}thead{{background:#f2f6f6}}small{{display:block;color:#667988;font-size:12px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}img{{width:100%;height:auto}}a{{color:#087f8c}}li{{margin:6px 0}}code{{background:#edf1f3;padding:2px 5px;border-radius:3px}}@media(max-width:760px){{main{{padding:24px 14px}}section{{padding:18px}}.grid{{grid-template-columns:1fr}}h1{{font-size:27px}}}}</style></head><body><main>
<div class="eyebrow">RESEARCH V2 · 已查看历史的探索性复测</div><h1>先检查基本面风险，再交易短期价格修复</h1>
<p class="lead">{esc(summary['conclusion'])}</p><p class="note">本次没有重新获得独立测试集。全部参数在这次收益计算前固定，四组使用相同股票池、资金与成本；不根据结果选择“最好”的版本。研究策略尚未接入自动交易。</p>
<section><h2>具体规则</h2><div class="grid"><div><strong>入场：缩小可疑超跌范围</strong><ol><li>沿用原 E01 前10%候选，不补充排名靠后的股票。</li><li>过去5日股票自身下跌，且相对同行的对数收益落后至少2%。</li><li>当时已披露可比收入同比不低于−5%，净利润为正、同比不低于−20%。不完整或过时资料跳过。</li><li>财报披露后冷静5个交易日；破产、违约、重大减值、财务重述等指定8-K风险披露后回避20个交易日。</li></ol></div>
<div><strong>退出：以实际修复程度为主</strong><ol><li>固定信号日的相对偏离；累计后续个股相对同行的对数收益，修复80%时触发。</li><li>出现已披露基本面风险、持仓总回报跌至−5%，或相对偏离再恶化一个入场幅度时触发退出。</li><li>最长持有10个交易日间隔，避免无限等待。</li><li>所有动态条件在收盘判断，下一交易日开盘价代理卖出；不是盘中触价成交，5%也不是最大亏损保证。</li></ol></div></div>
<p>最多20只、单股/发行人5%、行业25%、每天新买最多4只；只做多、整股、按历史结算周期使用可用现金。财报仅按披露日之后的下一交易日收盘才纳入信号。</p></section>
<section><h2>2024-01-02 → 2026-10-26 · 同池对照</h2><p>初始资金 $100,000，买卖每边全包成本10bp（0.1%），现金利息为零。回撤显示损失幅度；期末持仓按市值估值。</p><div class="scroll"><table><thead><tr><th>版本</th><th>总收益</th><th>年化收益</th><th>最大回撤</th><th>夏普</th><th>平均股票仓位</th><th>平均持有/日</th><th>已平仓笔数</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>B0为修正股票池后的原版；F1包括财报、事件、绝对下跌和最低偏离入场过滤；X1为整套动态退出。不能将F1与B0的差异全部归因于基本面识别。</p>
<p>V2减B0的日收益差，20日区块bootstrap 95%区间：{ci['lower_95']*10000:+.2f} 至 {ci['upper_95']*10000:+.2f} bp/日；这不是风险匹配后的Alpha检验。</p></section>
<section><h2>不同阶段是否一致</h2><div class="scroll"><table><thead><tr><th>阶段（均已查看）</th><th>B0 原版</th><th>X1 退出</th><th>F1 入场</th><th>V2 完整</th></tr></thead><tbody>{''.join(period_rows)}</tbody></table></div><p>各阶段单独从 $100,000 现金开始，不能将本表简单相加当成连续策略收益。</p></section>
<section><h2>收益、风险与交易行为</h2><img src="data:image/png;base64,{image}" alt="四组净值、回撤、股票仓位，V2退出原因和持仓时长，以及成本敏感性"><p>V2日均新买 {stats['buys_per_session']:.2f} 只、卖出 {stats['sells_per_session']:.2f} 只；实际交易过 {stats['unique_bought_symbols']} 只股票。低仓位可能降低回撤，也会减少上涨行情中的收益。</p></section>
<section><h2>筛选和退出审计</h2><div class="grid"><div><strong>原版候选被筛掉的原因</strong><ul>{exclusion}</ul><small>按顺序分配首个失败原因；同一股票不同日期分别计数。</small></div><div><strong>V2 已平仓退出原因</strong><table>{reasons}</table></div></div><p>“无检测到风险”不等于“非理性下跌”：本轮没有完整历史新闻、分析师预期、盈利指引与所有诉讼/监管事件的可用时间数据库。8-K也可能晚于消息首次公开。</p></section>
<section><h2>数据、复现与限制</h2><p>基础样本 {summary['data']['symbol_count']} 只，Alpaca SIP历史日线；Robinhood沿用原有抽样核对，不代表全池验收。SEC原始JSON、披露事实和accession编号、每日筛选、逐笔成交、退出触发、现金账本和代码/数据哈希均保留。</p><ul>{limitations}</ul>
<p>运行存档：<code>{esc(str(output))}</code></p><p>官方口径：<a href="https://www.sec.gov/search-filings/edgar-application-programming-interfaces">SEC 历史财报接口</a> · <a href="https://www.sec.gov/files/form8-k.pdf">SEC Form 8-K 事件定义</a> · <a href="https://docs.alpaca.markets/us/reference/stockbars">Alpaca 日线</a></p><small>生成时间 {esc(summary['generated_at'])} · {esc(summary['run_id'])} · 个人税前 · 所有结果为探索性</small></section></main></body></html>'''
    (output / 'report.html').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', default=str(ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2'))
    parser.add_argument('--fundamentals', default=str(ROOT / 'data/reversal/fundamentals_sec_20261027_v2'))
    parser.add_argument('--output')
    args = parser.parse_args()
    snapshot, fundamental_dir = Path(args.snapshot), Path(args.fundamentals)
    config_path = ROOT / 'config/reversal_research_v2.json'
    identity_path = ROOT / 'data/reversal/v2_identity_review.json'
    feature_path = fundamental_dir / 'daily_features.csv.gz'
    config = read_json(config_path)
    identity = read_json(identity_path)
    manifest = read_json(snapshot / 'manifest.json')
    fundamental_manifest = read_json(fundamental_dir / 'manifest.json')
    for name, expected in fundamental_manifest['files'].items():
        if digest(fundamental_dir / name) != expected:
            raise ValueError('Fundamental input checksum mismatch: ' + name)
    if digest(ROOT / 'backend/trading/reversal_fundamentals.py') != fundamental_manifest['code_sha256']:
        raise ValueError('Fundamental builder changed after data construction')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_v2'
    output = Path(args.output) if args.output else ROOT / 'data/reversal/runs' / run_id
    output.mkdir(parents=True, exist_ok=False)
    generated = datetime.now(timezone.utc).isoformat()
    filenames = ['daily_bars.csv.gz', 'daily_bars_holdout_LOCKED.csv.gz', 'corporate_actions.json.gz']
    for name in filenames:
        if digest(snapshot / name) != manifest['files'][name]['sha256']:
            raise ValueError('Historical snapshot checksum mismatch: ' + name)
    sources = [config_path, identity_path, feature_path, snapshot / 'manifest.json',
               *sorted(fundamental_dir.glob('*.json'))]
    code = [Path(__file__), ROOT / 'scripts/run_reversal_research.py',
            *sorted((ROOT / 'backend/trading').glob('reversal*.py'))]
    hashes = {str(path.relative_to(ROOT)): digest(path) for path in set(sources + code)}
    write_json(output / 'freeze.json', {'frozen_at': generated, 'config': config, 'hashes': hashes,
               'price_file_hashes': {name: manifest['files'][name]['sha256'] for name in filenames},
               'confirmatory': False, 'all_intervals_previously_viewed': True,
               'no_parameter_search': True, 'primary_preselected': 'V2'})
    shutil.copy2(config_path, output / 'config.json')
    shutil.copy2(identity_path, output / 'identity_review.json')
    excluded = set(identity['excluded_security_ids'])
    raw = pd.concat([pd.read_csv(snapshot / name, dtype={'security_id': str, 'issuer_id': str, 'industry': str},
                                keep_default_na=False) for name in filenames[:2]], ignore_index=True)
    raw = raw[~raw.security_id.isin(excluded)].copy()
    raw, actions = normalize_corporate_actions(raw, read_json(snapshot / filenames[2]))
    write_json(output / 'corporate_action_audit.json', actions)
    signal_config = SignalConfig(**{field.name: config[field.name] for field in fields(SignalConfig) if field.name in config})
    panel, scores, coverage = prepare_signals(raw, signal_config)
    fundamentals = pd.read_csv(feature_path, dtype={'security_id': str, 'issuer_id': str})
    scores, exit_features = prepare_refinement(panel, scores, fundamentals, config['entry_filters'], config['min_other_peers'])
    scores.to_csv(output / 'all_scores.csv.gz', index=False, compression='gzip')
    exit_features.to_csv(output / 'exit_features.csv.gz', index=False, compression='gzip')
    coverage.to_csv(output / 'daily_coverage.csv', index=False)
    print(json.dumps({'stage': 'features_ready', 'securities': int(panel.symbol.nunique()-1), 'score_rows': len(scores),
                      'filtered_candidates': int(scores.candidate_v2.sum())}), flush=True)
    ledger_config = {field.name: config[field.name] for field in fields(ReversalBacktestConfig)
                     if field.name in config and field.name != 'cost_bps'}
    periods, runmap, trials, comparisons, screening = {}, {}, [], {}, {}
    for period, (start, end) in config['periods'].items():
        usable = panel[panel.session.le(end)].copy()
        prior = usable.loc[usable.session.lt(start), 'session'].max()
        usable = usable[usable.session.ge(prior if isinstance(prior, str) else start)]
        fs = exit_features[exit_features.session.ge(start) & exit_features.session.le(end)]
        period_scores = scores[scores.session.ge(start) & scores.session.le(end)]
        base = period_scores[period_scores.base_candidate]
        screening[period] = {'base_candidates': len(base), 'passed_candidates': int(base.candidate_v2.sum()),
                            'base_candidate_gate_counts': base.entry_rejection_reason.value_counts().to_dict(),
                            'base_candidate_risk_classes': base.risk_class.fillna('unknown').value_counts().to_dict(),
                            'scorable_financial_known_fraction': float(period_scores.loc[period_scores.scorable, 'financial_known'].fillna(False).mean())}
        period_runs = []
        for experiment in ['B0', 'X1', 'F1', 'V2']:
            costs = [10] + (config['sensitivity_cost_bps'] if period == 'recent_retest' and experiment in {'B0', 'V2'} else [])
            for cost in costs:
                dynamic = experiment in {'X1', 'V2'}
                candidate = refined_candidates(period_scores, filtered=experiment in {'F1', 'V2'})
                cfg = ReversalBacktestConfig(**ledger_config, cost_bps=cost,
                                            **(config['dynamic_exit'] if dynamic else {}))
                result = run_backtest(usable, candidate, cfg, start, end, exit_features=fs if dynamic else None)
                result['experiment'] = experiment
                metrics = result['metrics']
                metrics['cagr'], metrics['ending_equity'] = metrics['annualized_return'], metrics['final_equity']
                result['trade_statistics'] = trade_statistics(result)
                if cost == 10:
                    bootstrap = config['bootstrap']
                    result['benchmark'] = benchmark_and_uncertainty(result, usable, repetitions=bootstrap['repetitions'],
                                                                    blocks=bootstrap['block_sessions'], seed=bootstrap['seed'])
                    if result['benchmark']['status'] != 'descriptive_proxy':
                        raise ValueError('Incomplete benchmark')
                filename = f'{period}_{experiment}_{cost}bp.json.gz'
                write_json(output / filename, result)
                trial = {'period': period, 'experiment': experiment, 'cost_bps': cost, 'metrics': metrics,
                         'trade_statistics': result['trade_statistics'], 'result_file': filename,
                         'result_sha256': digest(output / filename)}
                trials.append(trial)
                period_runs.append(trial)
                runmap[(period, experiment, cost)] = result
                write_json(output / 'trials.json', trials)
                print(json.dumps({'period': period, 'experiment': experiment, 'cost_bps': cost,
                                  'return': metrics['total_return'], 'drawdown': metrics['max_drawdown'],
                                  'closed_trades': metrics['trade_count']}), flush=True)
        periods[period] = {'start': start, 'end': end, 'runs': period_runs}
        comparisons[period] = {f'{experiment}_minus_B0': paired_comparison(runmap[(period, experiment, 10)], runmap[(period, 'B0', 10)])
                               for experiment in ['X1', 'F1', 'V2']}
    # Freeze checks cover every code and derived-data dependency after all runs.
    if hashes != {name: digest(ROOT / name) for name in hashes}:
        raise ValueError('Frozen code, config or data changed during the run; start a new run')
    primary, baseline = runmap[('recent_retest', 'V2', 10)], runmap[('recent_retest', 'B0', 10)]
    pm, bm = primary['metrics'], baseline['metrics']
    change = (pm['total_return'] - bm['total_return']) * 100
    conclusion = f'完整V2在近期历史复测中总收益{pm["total_return"]:+.2%}，修正股票池后的原版{bm["total_return"]:+.2%}，差{change:+.2f}个百分点。'
    conclusion += f'最大回撤分别为{pm["max_drawdown"]:.2%}与{bm["max_drawdown"]:.2%}。'
    conclusion += '这是固定改进假设的结果，不代表已经识别出非理性定价或验证未来收益。'
    limits = [
        '所有区间在V1中已查看；V2为预固定规则的探索性复测，不是新的独立样本外验证。',
        'SEC披露时点筛选只剔除已披露且已覆盖的风险。8-K可能晚于事件公开；历史新闻、盈利预期/指引与全部监管事件未覆盖，剩余下跌不能认定非理性。',
        'Companyfacts为今日SEC聚合数据，按每条事实的历史filed/accession限制可用日期；未持有当年的每日供应商快照，不能排除后续抽取/源数据修订。',
        '财务同比限同概念同币种同期长度；季度优先，年度备用，季度期末超过200日/年度超过460日或资料不完整跳过。缺前身CIK映射的早期财务会变成unknown。',
        '当前存续证券预选与当前SIC行业回填仍有幸存者及分类前视偏差；190只固定基础样本，不是完整历史全市场前500。',
        '在原隔离名单外新增TSM、AZN、APC、FOXA身份/ADS排除，所有对照同池重算。因此B0不应直接等同于上次43.33%的旧池结果。',
        '反弹目标使用信号日固定相对偏离，累计之后实际相对收益；不是滚动5日窗口自然淡出。开盘已越过目标仍按首个入场日收盘评估，是当前日线近似的限制。',
        '收盘触发、次开盘代理执行；未验证盘中反弹即卖、09:35窗口、报价深度/滑点。5%收盘风控不保证成交亏损上限。',
        '整股、历史T+3/T+2/T+1可用现金、零现金利息；每边全包成本是假设，未计个人税和固定数据成本；期末未平仓未扣未来退出成本。',
        '公司行动仍含复权因子推断及未知支付日延迟代理；缺失同行路径时禁止用不完整累计收益触发修复退出。',
        'Robinhood仅沿用AAPL/SPY指定历史抽样核对，未验证整个股票池或财务数据；没有混合不同来源成交量。',
        '研究模块不调用券商写接口，不更改自动交易策略。'
    ]
    for label, result in [('V2', primary), ('B0', baseline)]:
        if result['metrics']['stale_position_days'] or result['metrics']['overdue_position_days']:
            limits.append(f'{label}存在缺价或延迟退出，须查看逐笔审计。')
    recent_coverage = coverage[coverage.session.ge('2024-01-01')]
    summary = {'status': 'exploratory', 'title': '反转 V2 · 基本面筛选与反弹退出',
               'period': {'start': primary['start'], 'end': primary['end']},
               'data': {'source': 'Alpaca SIP + SEC; Robinhood sampled cross-check', 'feed': 'SIP historical daily',
                        'symbol_count': int(panel.loc[panel.symbol.ne('SPY'), 'symbol'].nunique()),
                        'selected_sample_count': manifest['sample_count'], 'quarantined_count': len(excluded),
                        'scorable_mean': float(recent_coverage.scorable_count.mean()), 'scorable_median': float(recent_coverage.scorable_count.median()),
                        'dataset_version': manifest['dataset_version']},
               'runs': periods['recent_retest']['runs'], 'benchmark': {k: v for k, v in primary['benchmark'].items() if k != 'curve'},
               'limitations': limits, 'generated_at': generated, 'run_id': run_id, 'conclusion': conclusion,
               'primary_experiment': 'V2', 'primary_cost_bps': 10, 'profitability_validated': False, 'execution_validated': False}
    report = {'summary': summary, 'periods': periods, 'comparisons': comparisons, 'screening': screening,
              'primary_trade_statistics': primary['trade_statistics'], 'config': config,
              'identity_review': identity, 'fundamental_input_sha256': digest(feature_path),
              'data_manifest': manifest, 'fundamental_manifest': fundamental_manifest,
              'freeze': read_json(output / 'freeze.json')}
    write_json(output / 'report.json', report)
    charts(output, runmap)
    render_report(output, report)
    # Original run directories remain intact; latest points to the new report.
    latest = ROOT / 'data/reversal/latest'
    latest.mkdir(parents=True, exist_ok=True)
    for name in ['report.json', 'report.html', 'research_charts.png', 'research_charts.svg']:
        temp = latest / (name + '.tmp')
        shutil.copy2(output / name, temp)
        os.replace(temp, latest / name)
    for destination in [ROOT / 'frontend/public/reversal-research', ROOT / 'frontend/dist/reversal-research']:
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output / 'report.html', destination / 'report.html')
        write_json(destination / 'summary.json', public_summary(summary))
    print(json.dumps({'completed': str(output), 'conclusion': conclusion}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
