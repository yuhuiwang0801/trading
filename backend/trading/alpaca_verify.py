"""Read-only Alpaca commissioning checks. No engine or broker order dependency."""
from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .calendar import as_utc, iso, market_state, previous_session, session_bounds
from .market import AlpacaMarketData, MarketDataError, MarketService, daily_indicators


def verify(provider: AlpacaMarketData, *, now=None, symbols=('AAPL', 'SPY')) -> dict:
    fixed_now = as_utc(now) if now is not None else None
    started = fixed_now or as_utc()
    session = market_state(started)
    symbols = list(dict.fromkeys(symbols))
    report = {
        'status': 'credentials_required' if not provider.configured else 'failed',
        'checked_at': iso(started), 'feed': provider.feed, 'symbols': symbols,
        'execution': {'mode': 'paper', 'broker': 'local_paper', 'live_enabled': False, 'orders_sent': 0},
        'checks': [], 'connectivity_verified': False, 'history_verified': False,
        'selected_feed_verified': False, 'session': session,
        'readiness': {'can_generate_orders': False, 'reason': '尚未验证'},
        'blockers': [], 'warnings': [],
    }
    checks = report['checks']

    def add(name, status, message, **extra):
        item = {'name': name, 'status': status, 'message': message, **extra}
        checks.append(item)
        if status == 'failed':
            report['blockers'].append(message)
        return item

    def run(name, callback, message):
        try:
            result, details = callback()
            add(name, 'passed', message, **details)
            return result
        except MarketDataError as exc:
            # Do not serialize exception text: even an injected provider may echo a secret.
            hint = {401: '密钥无效或与 Paper 账户不匹配', 403: '接口或所选 Feed 权限不足',
                    429: '请求达到速率限制'}.get(exc.http_status, '连接或数据校验失败')
            add(name, 'failed', f'{name}: {hint}', http_status=exc.http_status,
                error_code=exc.error_code if exc.error_code and str(exc.error_code).isdigit() else None)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            add(name, 'failed', f'{name}: 返回数据缺失、结构或数值不符合要求')
        return None

    if not provider.configured:
        add('credentials', 'failed', '请在服务端 .env 配置 ALPACA_API_KEY 和 ALPACA_SECRET_KEY，然后重启服务。')
        report['readiness']['reason'] = '缺少 Alpaca 密钥；未发起网络请求。'
        return report
    add('credentials', 'passed', '两个密钥字段均已配置；认证结果以实际接口检查为准。')
    if provider.trading_url != 'https://paper-api.alpaca.markets':
        add('execution_safety', 'failed', '此次接入要求 ALPACA_TRADING_URL 使用官方 Paper 地址。')
        report['readiness']['reason'] = '请使用 Paper Trading 密钥和 Paper API 地址。'
        return report
    add('execution_safety', 'passed', '验证只发送 GET 请求；策略继续使用本地 PaperBroker。')
    if provider.feed == 'iex':
        report['warnings'].append('免费 IEX 仅覆盖单一交易场所；成交量筛选与模拟成交量均为 IEX 口径，不能视为全市场 SIP 数据。')
    report['warnings'].append('公司行动接口的数据可能延迟；接口可访问不代表完整性或实时停牌状态已获保证。')

    def account_check():
        value = provider._get(provider.trading_url, '/v2/account', {})
        if not isinstance(value, dict) or value.get('status') != 'ACTIVE':
            raise ValueError('inactive paper account')
        # Account identifiers, cash, personal details and permissions are not persisted.
        return True, {'http_status': 200}

    account = run('authentication', account_check, 'Paper 账户只读认证成功；没有向 Alpaca 发送订单。')
    if account is None:
        report['readiness']['reason'] = 'Paper 账户认证未通过。'
        return report
    report['connectivity_verified'] = True

    def asset_check():
        rows = {r['symbol']: r for r in provider.assets() if r.get('symbol')}
        if any(not rows.get(s, {}).get('id') or rows[s].get('status') != 'active' for s in symbols):
            raise ValueError('missing asset identity')
        return True, {'symbols': symbols, 'http_status': 200, 'counts': {'matched': len(symbols)}}

    run('assets', asset_check, '样本证券存在可用的稳定标识。')
    day = session['session_date']
    prior = previous_session(day)
    opened, closed = session_bounds(prior)
    start = (started.date() - timedelta(days=400)).isoformat()

    def daily_check():
        rows = provider.bars(symbols, '1Day', start, day, asof=day)
        metrics = {s: daily_indicators(rows[s], day, provider.feed) for s in symbols}
        if any(v is None for v in metrics.values()):
            raise ValueError('missing consecutive daily sessions')
        return True, {'http_status': 200, 'symbols': symbols,
                      'counts': {s: v['history_sessions'] for s, v in metrics.items()}}

    daily = run('daily_bars', daily_check, f'每只样本至少 120 个连续完整交易日日线；不含当日，最近应为 {prior}。')

    def bar_check(timeframe, begin, end):
        rows = provider.bars(symbols, timeframe, iso(begin), iso(end - timedelta(microseconds=1)), asof=day)
        minutes = 5 if timeframe == '5Min' else 1
        latest = {}
        for symbol in symbols:
            valid = []
            for row in rows[symbol]:
                stamp = as_utc(row['t'])
                values = [Decimal(str(row[k])) for k in ['o', 'h', 'l', 'c', 'v']]
                o, h, l, c, v = values
                if (not all(x.is_finite() for x in values) or l <= 0 or not l <= o <= h
                        or not l <= c <= h or v < 0 or stamp < begin or stamp + timedelta(minutes=minutes) > end):
                    raise ValueError('invalid regular-session bar')
                valid.append(stamp + timedelta(minutes=minutes))
            if not valid or len(valid) != len(set(valid)):
                raise ValueError('empty or duplicate bars')
            latest[symbol] = max(valid)
        return latest, {'http_status': 200, 'symbols': symbols, 'counts': {s: len(rows[s]) for s in symbols}}

    minute = run('intraday_bars', lambda: bar_check('1Min', opened, closed), f'已读取 {prior} 常规时段 1 分钟线并校验 OHLCV。')
    five = run('five_minute_bars', lambda: bar_check('5Min', opened, closed), f'已读取 {prior} 常规时段 5 分钟线并校验 OHLCV。')
    report['history_verified'] = bool(daily and minute and five)

    def action_check():
        rows = provider.corporate_actions(symbols, start, day)
        return True, {'http_status': 200, 'counts': {'actions': len(rows)}}

    run('corporate_actions', action_check, '公司行动接口可访问；事件仍按策略规则保守排除或阻断。')

    def quote_check():
        rows = provider.quotes(symbols)
        stamps = {}
        for symbol in symbols:
            row = rows[symbol]
            bid, ask = Decimal(str(row['bp'])), Decimal(str(row['ap']))
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                raise ValueError('invalid bid ask')
            stamps[symbol] = as_utc(row['t'])
        return stamps, {'http_status': 200, 'symbols': symbols}

    quotes = run('latest_quotes', quote_check, f'已读取 {provider.feed.upper()} 样本买卖报价；时效单独判断。')
    report['selected_feed_verified'] = bool(quotes)
    current = fixed_now or as_utc()
    report['session'] = market_state(current)
    if not report['session']['is_open']:
        add('quote_freshness', 'unverified', '当前非美股常规交易时段；历史与权限可以验证，盘中报价时效待开市后复验。')
    elif quotes:
        begin = as_utc(report['session']['open'])
        # Request only completed regular 5-minute intervals; the current bar is incomplete.
        complete_end = begin + timedelta(seconds=int((current - begin).total_seconds() // 300) * 300)
        live_bars = None
        if complete_end > begin:
            live_bars = run('current_five_minute_bars', lambda: bar_check('5Min', begin, complete_end), '当前交易日已完成的 5 分钟线可读取。')
        assessed = fixed_now or as_utc()
        ages = {s: (assessed - stamp).total_seconds() for s, stamp in quotes.items()}
        bars_fresh = live_bars and all(0 <= (assessed - stamp).total_seconds() <= 360 for stamp in live_bars.values())
        if all(0 <= age <= 30 for age in ages.values()) and bars_fresh and market_state(assessed)['is_open']:
            add('quote_freshness', 'passed', '样本报价不超过 30 秒，已完成 5 分钟线不超过 360 秒。')
        else:
            add('quote_freshness', 'unverified', '样本报价或完整 5 分钟线不满足时效要求；开市初期、低流动性或延迟均可能造成此状态。')
    else:
        add('quote_freshness', 'skipped', '报价校验失败，不能评估时效。')

    failed = any(c['status'] == 'failed' for c in checks)
    fresh = any(c['name'] == 'quote_freshness' and c['status'] == 'passed' for c in checks)
    report['status'] = 'failed' if failed else 'passed' if fresh else 'partial'
    report['readiness'] = {'can_generate_orders': bool(fresh and not failed),
        'reason': '样本行情检查通过；实际候选仍由巡检、策略与风控逐笔判断。' if fresh and not failed
                  else '存在接口或数据校验失败，请按检查结果处理。' if failed
                  else '连接与历史检查通过；常规交易时段的行情时效尚未验证。'}
    report['completed_at'] = iso(fixed_now or as_utc())
    return report


def run_verification(store=None, *, output_path: Path | None = None) -> dict:
    from . import config
    provider = None
    try:
        provider = MarketService.from_env().provider
        report = verify(provider)
    except ValueError:
        report = {'status': 'failed', 'checked_at': iso(as_utc()), 'feed': 'invalid_configuration',
                  'execution': {'mode': 'paper', 'broker': 'local_paper', 'live_enabled': False, 'orders_sent': 0},
                  'checks': [{'name': 'configuration', 'status': 'failed', 'message': 'Alpaca Feed、URL 或请求频率配置无效。'}],
                  'readiness': {'can_generate_orders': False, 'reason': '请检查服务端 Alpaca 配置。'},
                  'connectivity_verified': False, 'history_verified': False, 'selected_feed_verified': False,
                  'session': market_state(), 'blockers': ['配置无效'], 'warnings': []}
    finally:
        if provider:
            provider.close()
    if store:
        store.put('market_verification', report)
    path = output_path or config.DATA / 'alpaca-verification.json'
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    return report


def main():
    from . import config
    from .runtime import RuntimeStore
    argparse.ArgumentParser(description=__doc__).parse_args()
    report = run_verification(RuntimeStore(config.DATABASE_URL))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] in {'passed', 'partial'} else 1)


if __name__ == '__main__':
    main()
