#!/usr/bin/env python3
"""Bounded, read-only data comparison. Never changes the production source."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
import httpx
from trading import config  # Load this workspace's server-side .env without printing it.
from trading.calendar import as_utc, iso, market_state, previous_session, session_bounds, NEW_YORK
from trading.market import MarketService, daily_indicators
from trading.mcp_broker import MCPClient, TokenProvider
from trading.robinhood_oauth import RobinhoodOAuth
from trading.robinhood_verify import _payload, _validate, _decimal

ALLOW = frozenset({'get_equity_quotes', 'get_equity_historicals', 'get_equity_technical_indicators'})


def local_state():
    with httpx.Client(base_url='http://127.0.0.1:8010', timeout=20) as http:
        state = http.get('/api/state').raise_for_status().json()
        live = http.get('/api/live').raise_for_status().json()
        return {'source':state['provider']['source'], 'feed':state['provider']['feed'],
                'paper_settings_digest':hashlib.sha256(json.dumps(state['settings'],sort_keys=True).encode()).hexdigest(),
                'paper_orders':len(state['orders']), 'paper_fills':len(state['fills']),
                'live_enabled':live['enabled'], 'live_armed':live['armed'],
                'live_orders':len(live['orders']), 'live_fills':len(live['fills'])}


def main():
    now = as_utc()
    day = market_state(now)['session_date']
    prior = previous_session(day)
    opened, closed = session_bounds(prior)
    symbols = ['AAPL', 'SPY']
    start_day = (now.date() - timedelta(days=400)).isoformat()
    end = day + 'T00:00:00Z'
    report = {'checked_at':iso(now), 'market':market_state(now), 'symbols':symbols,
              'before':local_state(), 'calls':[], 'data':{}, 'comparison':{},
              'orders_sent':0, 'source_changed':False}
    client = MCPClient(token_provider=TokenProvider(RobinhoodOAuth(), allow_legacy=False))
    original_call = client.call_discovered
    def guarded(name, arguments):
        if name not in ALLOW: raise RuntimeError('Operation outside read-only research scope')
        return original_call(name, arguments)
    client.call_discovered = guarded
    client.discover()
    report['tool_count'] = len(client.tools)
    schema_path = Path('data/robinhood-market-discovered-schemas.json')
    schema_path.write_text(json.dumps({'checked_at':iso(as_utc()), 'tools':list(client.tools.values())},ensure_ascii=False,indent=2))
    schema_path.chmod(0o600)

    def call(label, name, arguments):
        started = time.perf_counter()
        entry = {'label':label, 'tool':name, 'arguments':arguments}
        try:
            tool = client.tools[name]
            if tool.get('annotations', {}).get('readOnlyHint') is not True:
                raise ValueError('Missing read-only annotation')
            _validate(tool['inputSchema'], arguments, 'input_schema_mismatch')
            # Validate the schema before a request can follow an external reference.
            from trading.robinhood_verify import _no_external_refs
            _no_external_refs(tool['outputSchema'])
            payload = _payload(client.call_discovered(name, arguments))
            _validate(tool['outputSchema'], payload, 'output_schema_mismatch')
            entry['status'] = 'passed'
            report['data'][label] = payload['data']  # Never persist or act on guide.
            return payload['data']
        except Exception as exc:
            entry.update(status='failed', error_type=type(exc).__name__, code=getattr(exc,'code',None))
            return None
        finally:
            entry['seconds'] = round(time.perf_counter()-started,3)
            report['calls'].append(entry)
            print(json.dumps(entry,ensure_ascii=False),flush=True)

    base = {'symbols':symbols, 'bounds':'regular', 'adjustment_type':'none'}
    daily = call('daily', 'get_equity_historicals', {**base, 'start_time':start_day+'T00:00:00Z', 'end_time':end, 'interval':'day'})
    for interval in ['minute','5minute']:
        call(interval, 'get_equity_historicals', {**base, 'start_time':iso(opened), 'end_time':iso(closed), 'interval':interval})
    call('quotes', 'get_equity_quotes', {'symbols':symbols})
    for indicator, period in [('sma',20),('sma',60),('atr',14)]:
        call(indicator+str(period), 'get_equity_technical_indicators', {**base,'start_time':start_day+'T00:00:00Z',
            'end_time':end,'interval':'day','type':indicator,'period':period,'output':'last:3'})

    normalized = {}
    for label in ['daily','minute','5minute']:
        summaries = {}
        for item in report['data'].get(label,{}).get('results',[]) or []:
            rows = item.get('bars') or []
            if any(not isinstance(r,dict) for r in rows):
                summaries[item['symbol']] = {'invalid_null_row':True}
                continue
            bad = 0
            for row in rows:
                o,h,l,c = [_decimal(row[k]) for k in ('open_price','high_price','low_price','close_price')]
                if l <= 0 or h < l or not l <= o <= h or not l <= c <= h or row['volume'] < 0: bad += 1
            summaries[item['symbol']] = {'count':len(rows), 'first':rows[0]['begins_at'] if rows else None,
                'last':rows[-1]['begins_at'] if rows else None, 'invalid_ohlc':bad,
                'duplicates':len(rows)-len({r['begins_at'] for r in rows}),
                'interpolated':sum(r.get('interpolated') is True for r in rows),
                'interpolated_flag_missing':sum('interpolated' not in r for r in rows),
                'sessions':sorted({r.get('session','missing') for r in rows}),
                'volume_sum':sum(r['volume'] for r in rows)}
            if label == 'daily':
                # Robinhood daily timestamps are UTC date labels, not NY session starts.
                normalized[item['symbol']] = [{'t':iso(session_bounds(r['begins_at'][:10])[0]),'o':r['open_price'],'h':r['high_price'],
                    'l':r['low_price'],'c':r['close_price'],'v':r['volume']} for r in rows if r.get('interpolated') is not True]
        report['comparison'][label] = summaries
    if daily:
        report['comparison']['robinhood_locally_computed'] = {
            s:daily_indicators(rows,day,'robinhood_unverified_volume_scope') for s,rows in normalized.items()}
        for metrics in report['comparison']['robinhood_locally_computed'].values():
            if metrics:
                metrics['volume_scope'] = 'unverified'
    for label in ['sma20','sma60','atr14']:
        report['comparison'][label] = report['data'].get(label)
    provider = MarketService.from_env().provider
    try:
        alpaca = provider.bars(symbols,'1Day',start_day,day,asof=day)
        report['comparison']['alpaca_iex_locally_computed'] = {s:daily_indicators(alpaca[s],day,'iex') for s in symbols}
        for interval, alpaca_interval in [('minute','1Min'),('5minute','5Min')]:
            rows = provider.bars(symbols,alpaca_interval,iso(opened),iso(closed-timedelta(microseconds=1)),asof=day)
            report['comparison']['alpaca_'+interval] = {s:{'count':len(rows[s]),'volume_sum':sum(r['v'] for r in rows[s])} for s in symbols}
    except Exception as exc:
        report['comparison']['alpaca_error'] = {'error_type':type(exc).__name__,
            'http_status':getattr(exc,'http_status',None), 'error_code':getattr(exc,'error_code',None)}
    finally:
        provider.close()
    report['after'] = local_state()
    report['production_state_unchanged'] = report['before'] == report['after']
    report['completed_at'] = iso(as_utc())
    path = Path('data/robinhood-market-probe-evidence.json')
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
        handle.write('\n')
    path.chmod(0o600)
    print(json.dumps({'report':str(path),'production_state_unchanged':report['production_state_unchanged'],
          'comparison':report['comparison']},ensure_ascii=False),flush=True)


if __name__ == '__main__': main()
