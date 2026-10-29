#!/usr/bin/env python3
"""Explicitly requested sample only: reads plus a broker preview, never writes."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4
from trading.mcp_broker import MCPClient, TokenProvider
from trading.robinhood_oauth import RobinhoodOAuth
from trading.robinhood_adapter import RobinhoodTradingAdapter
from trading.robinhood_verify import READ_TOOLS, _payload, _validate, _decimal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--side', choices=['BUY', 'SELL'], required=True)
    parser.add_argument('--quantity', required=True)
    parser.add_argument('--limit', required=True, help='Explicit price or latest-ask for a BUY preview')
    parser.add_argument('--output', default='data/robinhood-preview-evidence.json')
    args = parser.parse_args()
    client = MCPClient(token_provider=TokenProvider(RobinhoodOAuth(), allow_legacy=False))
    original = client.call_discovered
    calls = []
    def guarded_call(name, arguments):
        if name not in READ_TOOLS | {'get_equity_quotes', 'review_equity_order'}:
            raise RuntimeError('Broker writes are prohibited by this verification command')
        calls.append(name)
        return original(name, arguments)
    client.call_discovered = guarded_call
    adapter = RobinhoodTradingAdapter(client, write_enabled=False)
    snapshot = adapter.read_snapshot()
    price, source_quote = args.limit, None
    if price == 'latest-ask':
        if args.side != 'BUY': raise ValueError('latest-ask requires BUY')
        schema = client.tools['get_equity_quotes']
        if schema.get('annotations', {}).get('readOnlyHint') is not True:
            raise ValueError('Quotes tool must be read-only')
        arguments = {'symbols':[args.symbol]}
        _validate(schema['inputSchema'], arguments, 'invalid_quote_request')
        payload = _payload(client.call_discovered('get_equity_quotes', arguments))
        _validate(schema['outputSchema'], payload, 'invalid_quote_response')
        rows = payload['data']['results']
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('Symbol quote unavailable')
        source_quote = rows[0]['quote']
        if not isinstance(source_quote, dict) or source_quote['symbol'] != args.symbol:
            raise ValueError('Symbol quote does not match')
        ask = _decimal(source_quote['ask_price'], nonnegative=True)
        if ask <= 0: raise ValueError('No usable ask is available; preview was not sent')
        price = format(ask, 'f')
    request = {'account_number':snapshot['account_number'], 'symbol':args.symbol, 'side':args.side,
               'quantity':args.quantity, 'limit_price':price, 'ref_id':str(uuid4())}
    preview = adapter.preview(request)
    preview['order'].pop('account_number', None)
    report = {'kind':'actual_robinhood_preview', 'account_masked':snapshot['account']['masked_number'],
              'request':preview['order'], 'preview':preview, 'source_quote':source_quote,
              'calls':calls, 'orders_sent':0, 'cancellations_sent':0, 'write_enabled':False}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)
    print(json.dumps({'status':preview['status'], 'request':preview['order'],
                      'errors':preview['errors'], 'warnings':preview['warnings'],
                      'order_checks':preview['order_checks'], 'approval_required':preview['approval_required'],
                      'orders_sent':0, 'report':str(path)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
