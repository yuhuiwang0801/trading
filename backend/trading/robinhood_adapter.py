"""Schema-bound Robinhood equity adapter with an explicit, default-off write gate.

The adapter is deliberately independent of the paper ledger. Its snapshots contain
private account identifiers for internal reconciliation; API callers must redact
them. Only ``review_equity_order`` is granted the reviewed preview exception to
the read-only annotation rule. Tool guides never execute or enable anything.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import re
from uuid import UUID

from jsonschema import Draft202012Validator

from .mcp_broker import LiveExecutionDisabled
from .robinhood_verify import (
    APPROVAL_STATES, PORTFOLIO_AMOUNTS, _decimal, _fail, _identifier,
    _no_external_refs, _orders, _pages, _payload, _positions, _read, _rows,
    _timestamp, _validate,
    _VerificationFailure,
)


ORDER_STATES = {
    'new': 'SUBMITTED', 'queued': 'SUBMITTED', 'confirmed': 'SUBMITTED',
    'unconfirmed': 'SUBMITTED', 'locating': 'SUBMITTED',
    'partially_filled': 'PARTIALLY_FILLED', 'filled': 'FILLED',
    'pending_cancelled': 'CANCEL_PENDING', 'cancelled': 'CANCELLED',
    'partially_filled_rest_cancelled': 'CANCELLED',
    'rejected': 'REJECTED', 'failed': 'REJECTED', 'voided': 'REJECTED',
    'locate_failed': 'REJECTED',
}
APPROVAL_STATUS = {
    'APPROVAL_STATE_PENDING': 'AWAITING_APPROVAL',
    'APPROVAL_STATE_APPROVED': 'APPROVED',
    'APPROVAL_STATE_AUTO_APPROVED': 'APPROVED',
    'APPROVAL_STATE_DECLINED': 'DECLINED',
    'APPROVAL_STATE_EXPIRED': 'EXPIRED',
}
QUOTE_FIELDS = (
    'symbol', 'last_trade_price', 'venue_last_trade_time', 'last_non_reg_trade_price',
    'venue_last_non_reg_trade_time', 'adjusted_previous_close', 'previous_close',
    'previous_close_date', 'bid_price', 'venue_bid_time', 'ask_price', 'venue_ask_time',
    'has_traded', 'state',
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value, **kwargs):
    return format(_decimal(value, **kwargs), 'f')


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', value):
        _fail('invalid_symbol', '股票代码无效')
    return value


def normalize_order_request(order):
    """Canonical whole-share limit/day intent; UUID is required even for preview."""
    if not isinstance(order, dict):
        _fail('invalid_order', '订单参数无效')
    allowed = {'account_number', 'symbol', 'side', 'quantity', 'limit_price', 'ref_id'}
    if set(order) != allowed:
        _fail('invalid_order', '订单只支持整股、常规时段、当日限价交易')
    account = _identifier(order['account_number'])
    if len(account) <= 4 or order['side'] not in {'BUY', 'SELL'}:
        _fail('invalid_order', '账户或订单方向无效')
    quantity = _decimal(order['quantity'], nonnegative=True)
    price = _decimal(order['limit_price'], nonnegative=True)
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 1000000:
        _fail('invalid_quantity', '仅支持正整数股数')
    if price <= 0 or price > 1000000 or price % Decimal('0.01') != 0:
        _fail('invalid_limit_price', '限价必须为正数且精确到美分')
    try:
        if not isinstance(order['ref_id'], str) or str(UUID(order['ref_id'])) != order['ref_id']:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail('invalid_ref_id', '订单必须带有规范的 UUID 幂等标识')
    return {'account_number': account, 'symbol': _symbol(order['symbol']), 'side': order['side'],
            'quantity': format(quantity, 'f'), 'limit_price': format(price, 'f'), 'ref_id': order['ref_id']}


def normalize_preview_request(order):
    """Add regular-session market previews without expanding the write contract.

    The discovered MCP contract permits fractional shares (six decimals) and
    dollar amounts only for market orders. Dollar amounts stay dollar amounts;
    an estimated quantity must never replace the customer's original intent.
    """
    if not isinstance(order, dict) or order.get('type') != 'market':
        return normalize_order_request(order)
    common = {'account_number', 'symbol', 'side', 'ref_id', 'type'}
    if set(order) not in (common | {'quantity'}, common | {'dollar_amount'}):
        _fail('invalid_order', '市价预览须选择金额或股数，不能同时填写或设置限价')
    # Reuse the existing identity validation; synthetic limit terms stay local.
    identity = normalize_order_request({key: order[key] for key in common - {'type'}}
        | {'quantity': '1', 'limit_price': '1.00'})
    result = {key: identity[key] for key in common - {'type'}} | {'type': 'market'}
    if 'dollar_amount' in order:
        amount = _decimal(order['dollar_amount'], nonnegative=True)
        if amount < 1 or amount > 1000000000000 or amount % Decimal('0.01') != 0:
            _fail('invalid_dollar_amount', '金额须至少为 1 美元且精确到美分')
        result['dollar_amount'] = format(amount.quantize(Decimal('0.01')), 'f')
    else:
        quantity = _decimal(order['quantity'], nonnegative=True)
        if quantity <= 0 or quantity > 1000000 or quantity % Decimal('0.000001') != 0:
            _fail('invalid_quantity', '股数须为正数，最多 6 位小数')
        result['quantity'] = format(quantity, 'f')
    return result


def _arguments(order, *, submit=False):
    result = {key: order[key] for key in ('account_number', 'symbol', 'quantity', 'dollar_amount', 'limit_price')
              if key in order}
    result.update(side=order['side'].lower(), type=order.get('type', 'limit'),
                  time_in_force='gfd', market_hours='regular_hours')
    if submit:
        result['ref_id'] = order['ref_id']
    return result


def _normalize_orders(rows):
    _orders(rows)  # validates uniqueness, all states, amounts and fill reconciliation
    normalized, fills, seen_refs = [], [], set()
    for row in rows:
        ref = row.get('ref_id')
        if ref is not None:
            _identifier(ref)
            if ref in seen_refs:
                _fail('duplicate_order_reference', '订单幂等标识重复，无法唯一关联本地交易意图')
            seen_refs.add(ref)
        normalized.append({'id': row['id'], 'ref_id': ref, 'symbol': row['symbol'],
            'side': row['side'].upper(), 'state': ORDER_STATES[row['state']], 'raw_state': row['state'],
            'quantity': _number(row['quantity']) if row['quantity'] is not None else None,
            'filled_quantity': _number(row['cumulative_quantity']),
            'limit_price': _number(row['price']) if row['price'] is not None else None,
            'created_at': row['created_at'], 'updated_at': row['last_transaction_at'] or row['created_at'],
            'type': row['type'], 'time_in_force': row['time_in_force'], 'market_hours': row['market_hours'],
            'trigger': row['trigger'], 'agent_initiated': row['agent_initiated']})
        for fill in row['executions'] or []:
            fills.append({'id': fill['id'], 'order_id': row['id'], 'symbol': row['symbol'],
                'side': row['side'].upper(), 'quantity': _number(fill['quantity']), 'price': _number(fill['price']),
                'fees': _number(fill['fees']), 'timestamp': fill['timestamp']})
    return normalized, fills


def _normalize_approvals(rows, account_number):
    result, seen = [], set()
    for row in rows:
        approval_id = _identifier(row.get('approval_id'))
        if approval_id in seen:
            _fail('duplicate_approval', '审批记录重复')
        seen.add(approval_id)
        if row.get('account_number') != account_number:
            _fail('account_scope_mismatch', '审批记录不属于已选账户')
        if row.get('state') not in APPROVAL_STATES:
            _fail('unknown_approval_state', '审批状态尚未映射')
        for field in ('created_at', 'updated_at', 'expires_at'):
            if row.get(field):
                _timestamp(row[field])
        # No order id is supplied by this API. Approval completion is reconciled
        # against orders[].ref_id == approval_id, never assumed to be a fill.
        result.append({'approval_id': approval_id, 'account_number': account_number,
            'state': APPROVAL_STATUS[row['state']], 'raw_state': row['state'],
            'is_edited': row['is_edited'], 'asset_class': row['asset_class'], 'order_id': None,
            'proposed_order': deepcopy(row.get('proposed_order')),
            'actual_order': deepcopy(row.get('actual_order'))})
    return result


class RobinhoodTradingAdapter:
    def __init__(self, client, write_enabled=False):
        self.client = client
        self.write_enabled = write_enabled

    def _ready(self):
        if not getattr(self.client, 'initialized', False):
            self.client.discover()
        if not getattr(self.client, 'initialized', False):
            _fail('connection_not_initialized', '账户连接尚未初始化')

    def _account(self, expected=None):
        self._ready()
        rows = _rows(_read(self.client, 'get_accounts', {}), 'accounts')
        seen, eligible = set(), []
        for row in rows:
            number = _identifier(row.get('account_number'))
            if len(number) <= 4 or number in seen:
                _fail('invalid_account_list', '账户列表存在无效或重复记录')
            seen.add(number)
            if row.get('agentic_allowed') is True:
                eligible.append(row)
        if len(eligible) != 1:
            _fail('eligible_account_not_unique', '未能确认唯一可交易账户')
        account = eligible[0]
        if (account['state'] != 'active' or account['deactivated'] is not False
                or account['permanently_deactivated'] is not False):
            _fail('account_not_active', '交易账户当前不可用')
        if expected is not None and account['account_number'] != expected:
            _fail('account_scope_mismatch', '选定账户与获授权交易账户不一致')
        if (account['type'] not in {'cash', 'margin', 'limited_margin'}
                or not re.fullmatch(r'[a-z_]{1,64}', account['brokerage_account_type'])):
            _fail('unknown_account_type', '账户类型尚未支持')
        return account

    def _gate(self):
        enabled = self.write_enabled() if callable(self.write_enabled) else self.write_enabled
        if enabled is not True:
            raise LiveExecutionDisabled('Real broker writes are disabled; no order or cancellation was sent')

    def _prepare(self, name, arguments):
        # No general annotation bypass: only these three individually reviewed
        # equity operations are accepted, and writes still require _gate().
        if name not in {'review_equity_order', 'place_equity_order', 'cancel_equity_order'}:
            _fail('operation_not_allowed', '不允许调用该交易接口')
        tool = self.client.tools.get(name)
        if not isinstance(tool, dict):
            _fail('required_tool_unavailable', '交易接口尚不可用')
        annotations = tool.get('annotations', {})
        if not isinstance(annotations, dict) or annotations.get('destructiveHint') is True:
            _fail('unsafe_tool_annotation', '交易接口标记与审核结果不一致')
        _validate(tool.get('inputSchema'), arguments, 'input_schema_mismatch')
        output = tool.get('outputSchema')
        if not isinstance(output, dict):
            _fail('output_schema_unavailable', '交易接口缺少返回结构')
        _no_external_refs(output)
        try:
            Draft202012Validator.check_schema(output)
        except Exception:
            _fail('output_schema_invalid', '交易接口返回结构无效')
        return output

    def _result(self, name, arguments, schema):
        payload = _payload(self.client.call_discovered(name, arguments))
        _validate(schema, payload, 'output_schema_mismatch')
        return payload['data']

    def fresh_quote(self, symbol):
        """Fetch one uncached public quote without account reads or write access.

        This narrowly reviewed market-data operation does not expand the account
        verifier's read allowlist. The execution guard evaluates the returned
        venue timestamps and prices; receiving a response never renews them.
        """
        symbol = _symbol(symbol)
        name, arguments = 'get_equity_quotes', {'symbols': [symbol]}
        try:
            self._ready()
            tool = self.client.tools.get(name)
            if not isinstance(tool, dict):
                _fail('required_tool_unavailable', '已发现的接口缺少必要只读工具')
            annotations = tool.get('annotations', {})
            if (not isinstance(annotations, dict) or annotations.get('readOnlyHint') is not True
                    or annotations.get('destructiveHint', False) is not False):
                _fail('unsafe_tool_annotation', '工具没有明确标记为非破坏性只读接口')
            _validate(tool.get('inputSchema'), arguments, 'input_schema_mismatch')
            schema = tool.get('outputSchema')
            if not isinstance(schema, dict):
                _fail('output_schema_unavailable', '只读接口未提供输出结构，已停止核验')
            _no_external_refs(schema)
            try:
                Draft202012Validator.check_schema(schema)
            except Exception:
                _fail('output_schema_invalid', '只读接口的输出结构无效')
            payload = _payload(self.client.call_discovered(name, arguments))
            _validate(schema, payload, 'output_schema_mismatch')
            rows = _rows(payload['data'], 'results')
            if len(rows) != 1:
                _fail('quote_not_resolved', '券商盘口未返回唯一股票报价')
            quote = rows[0].get('quote')
            if not isinstance(quote, dict) or any(field not in quote for field in QUOTE_FIELDS):
                _fail('quote_unavailable', '券商盘口报价缺失')
            if quote['symbol'] != symbol:
                _fail('quote_symbol_mismatch', '券商盘口股票代码不匹配')
            # Never use settled close as a live quote or retain tool guidance.
            return deepcopy({field: quote[field] for field in QUOTE_FIELDS})
        except _VerificationFailure:
            raise
        except Exception:
            raise _VerificationFailure('quote_read_failed', '券商盘口读取失败，已停止交易检查') from None

    def read_snapshot(self, approval_ids=None):
        selected = self._account()
        number = selected['account_number']
        portfolio = _read(self.client, 'get_portfolio', {'account_number': number})
        if 'account_number' in portfolio and portfolio['account_number'] != number:
            _fail('account_scope_mismatch', '账户资金响应不匹配')
        values = {key: _number(portfolio.get(key)) for key in PORTFOLIO_AMOUNTS}
        buying = portfolio.get('buying_power')
        if not isinstance(buying, dict):
            _fail('buying_power_unavailable', '账户股票购买力未知')
        if portfolio.get('currency') != 'USD' or buying.get('display_currency') != 'USD':
            _fail('unsupported_account_currency', '仅支持美元交易账户')
        power = _number(buying.get('buying_power'))
        unleveraged = _number(buying.get('unleveraged_buying_power'))
        raw_positions = _pages(self.client, 'get_equity_positions', 'positions', number)
        _, compatible_positions = _positions(raw_positions)
        raw_orders = _pages(self.client, 'get_equity_orders', 'orders', number)
        _, _, compatible_orders = _orders(raw_orders)
        orders, fills = _normalize_orders(raw_orders)
        positions = [{'symbol': row['symbol'], 'quantity': _number(row['quantity']),
            'available_quantity': _number(row['shares_available_for_sells']),
            'average_price': _number(row['average_buy_price']) if row.get('average_buy_price') is not None else None,
            'type': row['type']}
            for row in raw_positions if _decimal(row['quantity']) != 0]
        setting = _read(self.client, 'get_trade_approval_setting', {'account_number': number})['setting']
        if setting['account_number'] != number:
            _fail('account_scope_mismatch', '审批设置不属于已选账户')
        raw_approvals = _pages(self.client, 'get_trade_approvals', 'approvals', number)
        approvals = _normalize_approvals(raw_approvals, number)
        if approval_ids:
            requested = list(dict.fromkeys(_identifier(value) for value in approval_ids))
            merged = {item['approval_id']: item for item in approvals}
            for start in range(0, len(requested), 100):
                batch = requested[start:start + 100]
                data = _read(self.client, 'get_trade_approvals', {'account_number': number, 'approval_ids': batch})
                for item in _normalize_approvals(_rows(data, 'approvals'), number):
                    if item['approval_id'] not in batch:
                        _fail('approval_scope_mismatch', '审批查询返回了未请求的记录')
                    merged[item['approval_id']] = item
            approvals = list(merged.values())
        return {'checked_at': _now(), 'account_number': number,
            'account': {'masked_number': '••••' + number[-4:], 'type': selected['type'],
                'account_type': selected['brokerage_account_type'], 'currency': 'USD',
                'cash': values['cash'], 'buying_power': power, 'unleveraged_buying_power': unleveraged,
                'total_value': values['total_value'], 'approval_required': setting['human_must_approve_trades']},
            'positions': positions, 'orders': orders, 'fills': fills, 'approvals': approvals,
            'compatible': compatible_positions and compatible_orders}

    def tradability(self, account_number, symbol):
        selected = self._account(_identifier(account_number))
        symbol = _symbol(symbol)
        data = _read(self.client, 'get_equity_tradability', {'account_number': account_number, 'symbols': [symbol]})
        rows = _rows(data, 'results')
        if len(rows) != 1 or rows[0]['symbol'] != symbol or data.get('not_found'):
            _fail('symbol_not_resolved', '股票可交易状态未完整解析')
        row = rows[0]
        types = row.get('account_type_tradabilities')
        permissions = [item['account_type_tradability'] for item in types or []
                       if item['account_type'] == selected['brokerage_account_type']]
        blockers = []
        if row.get('state') != 'active' or row['tradeable'] is not True:
            blockers.append('instrument_not_tradable')
        if len(permissions) != 1 or permissions[0] not in {'tradable', 'position_closing_only'}:
            blockers.append('account_type_not_tradable')
        halt = row.get('internal_halt_reason') or row.get('internal_halt_details')
        sessions = row.get('internal_halt_sessions')
        if (halt and not sessions) or 'regular_hours' in (sessions or []):
            blockers.append('instrument_halted')
        return {'symbol': symbol, 'buyable': not blockers and permissions[0] == 'tradable',
            'sellable': not blockers, 'tradeable': not blockers, 'blockers': blockers,
            'account_type_tradability': permissions[0] if len(permissions) == 1 else None,
            'fractional_tradability': row.get('fractional_tradability')}

    def preview(self, order):
        order = normalize_preview_request(order)
        tradability = self.tradability(order['account_number'], order['symbol'])
        arguments = _arguments(order)
        schema = self._prepare('review_equity_order', arguments)
        data = self._result('review_equity_order', arguments, schema)
        market = order.get('type') == 'market'
        notional = 'dollar_amount' in order
        try:
            size_matches = (_decimal(data.get('dollar_amount')) == _decimal(order['dollar_amount'])
                if notional else _decimal(data.get('quantity')) == _decimal(order['quantity'])
                and data.get('dollar_amount') is None)
            # A broker may also echo a computed quantity for a dollar order.
            # Validate it only as an estimate; the notional echo is mandatory.
            if notional and data.get('quantity') is not None:
                size_matches = size_matches and _decimal(data['quantity']) > 0
            price_matches = (data.get('limit_price') is None if market else
                _decimal(data.get('limit_price')) == _decimal(order['limit_price']))
        except Exception:
            size_matches = price_matches = False
        if (data['symbol'] != order['symbol'] or data['side'] != order['side'].lower()
                or data['type'] != order.get('type', 'limit') or not size_matches or not price_matches
                or data.get('stop_price') is not None):
            _fail('preview_intent_mismatch', '券商预览与原始订单参数不一致')
        errors = list(tradability['blockers'])
        fractional = notional or (market and _decimal(order['quantity']) % 1 != 0)
        if fractional and tradability['fractional_tradability'] != 'tradable':
            errors.append('fractional_not_tradable')
        if order['side'] == 'BUY' and not tradability['buyable'] and not errors:
            errors.append('position_closing_only')
        warnings = ['broker_order_alert'] if data['order_checks'] else []
        quote = data['quote_data']
        if quote is None:
            errors.append('quote_unavailable')
        else:
            if quote['symbol'] != order['symbol'] or quote['state'] != 'active' or quote['has_traded'] is not True:
                errors.append('quote_not_tradable')
            for key in ('last_trade_price', 'adjusted_previous_close', 'previous_close', 'bid_price', 'ask_price'):
                _decimal(quote[key], nonnegative=True)
            if _decimal(quote['last_trade_price']) <= 0:
                errors.append('quote_not_tradable')
            for key in ('venue_last_trade_time', 'venue_bid_time', 'venue_ask_time'):
                _timestamp(quote[key])
            if quote.get('last_non_reg_trade_price') is not None:
                _decimal(quote['last_non_reg_trade_price'], nonnegative=True)
            if quote.get('venue_last_non_reg_trade_time') is not None:
                _timestamp(quote['venue_last_non_reg_trade_time'])
            if not data.get('market_data_disclosure'):
                errors.append('market_disclosure_unavailable')
        estimated_quantity = estimated_total = None
        if not market:
            estimated_total = format(_decimal(order['quantity']) * _decimal(order['limit_price']), 'f')
        elif notional:
            estimated_total = order['dollar_amount']
        quote_price = _decimal(quote['ask_price' if order['side'] == 'BUY' else 'bid_price']) if quote else Decimal(0)
        if market and quote_price > 0:
            if notional:
                estimated_quantity = format(_decimal(order['dollar_amount']) / quote_price, 'f')
            else:
                estimated_total = format(_decimal(order['quantity']) * quote_price, 'f')
        if market and not notional and estimated_total is not None and _decimal(estimated_total) < 1 and fractional:
            errors.append('fractional_value_below_minimum')
        return {'status': 'BLOCKED' if errors or warnings else 'READY', 'order': order, 'ref_id': order['ref_id'],
            'checked_at': _now(), 'warnings': warnings, 'errors': errors, 'order_checks': deepcopy(data['order_checks']),
            'estimated_total': estimated_total,
            'estimated_total_kind': 'requested_amount' if notional else 'quote_estimate' if market else 'limit_value',
            'estimated_quantity': estimated_quantity,
            'approval_required': data.get('customer_approval_required'),
            'approval_reason': data.get('customer_approval_required_reason'),
            'quote_data': deepcopy(quote), 'market_data_disclosure': data.get('market_data_disclosure', ''),
            'tradability': tradability}

    def submit(self, order):
        self._gate()  # disabled means no network side effect, including reads
        order = normalize_order_request(order)
        self._account(order['account_number'])
        arguments = _arguments(order, submit=True)
        schema = self._prepare('place_equity_order', arguments)
        self._gate()  # check the current arm state immediately before the write
        unknown = {'status': 'UNKNOWN', 'broker_order_id': None, 'approval_id': None,
            'ref_id': order['ref_id'], 'order': None, 'fills': [], 'reason': 'reconciliation_required'}
        try:
            data = self._result('place_equity_order', arguments, schema)
            broker_order, approval = data.get('order'), data.get('approval')
            if isinstance(broker_order, dict) == isinstance(approval, dict):
                return unknown
            if approval is not None:
                item = _normalize_approvals([approval], order['account_number'])[0]
                unknown['approval_id'] = item['approval_id']
                if item['asset_class'] != 'ASSET_CLASS_EQUITY' or item['is_edited']:
                    return unknown
                proposal = item['proposed_order']
                if proposal is not None and not self._proposal_matches(proposal, order):
                    return unknown
                if item['state'] != 'AWAITING_APPROVAL':
                    return unknown
                return {**unknown, 'status': 'AWAITING_APPROVAL', 'approval': item, 'reason': None}
            rows, fills = _normalize_orders([broker_order])
            result = rows[0]
            unknown['broker_order_id'] = result['id']
            if (result['ref_id'] != order['ref_id'] or result['symbol'] != order['symbol']
                    or result['side'] != order['side'] or result['type'] != 'limit'
                    or result['quantity'] is None or _decimal(result['quantity']) != _decimal(order['quantity'])
                    or result['limit_price'] is None or _decimal(result['limit_price']) != _decimal(order['limit_price'])
                    or result['time_in_force'] != 'gfd' or result['market_hours'] != 'regular_hours'
                    or result['trigger'] != 'immediate' or result['agent_initiated'] is not True):
                return unknown
            return {**unknown, 'status': result['state'], 'order': result, 'fills': fills, 'reason': None}
        except Exception:
            # The request may already have reached the broker. No payload-derived
            # error, inferred rejection or automatic retry can erase that fact.
            return unknown

    @staticmethod
    def _proposal_matches(proposal, order):
        # Approval schemas permit sparse summaries. Missing/invalid terms must be
        # a failed relationship, not an exception that skips the caller's block.
        try:
            price = proposal.get('limit_price')
            return (proposal.get('asset_class') == 'ASSET_CLASS_EQUITY'
                and proposal.get('symbol') == order['symbol'] and proposal.get('side') == order['side'].lower()
                and proposal.get('order_type') == 'limit' and proposal.get('time_in_force') == 'gfd'
                and proposal.get('market_hours', 'regular_hours') == 'regular_hours'
                and _decimal(proposal.get('quantity')) == _decimal(order['quantity'])
                and isinstance(price, dict) and price.get('currency_code', 'USD') == 'USD'
                and _decimal(price.get('amount')) == _decimal(order['limit_price'])
                and not proposal.get('sub_orders') and not proposal.get('contingency_type'))
        except Exception:
            return False

    def cancel(self, account_number, order_id):
        self._gate()
        self._account(_identifier(account_number))
        order_id = _identifier(order_id)
        arguments = {'account_number': account_number, 'order_id': order_id}
        schema = self._prepare('cancel_equity_order', arguments)
        self._gate()
        try:
            data = self._result('cancel_equity_order', arguments, schema)
            return {'status': 'CANCEL_PENDING' if data['accepted'] else 'CANCEL_REJECTED',
                    'broker_order_id': order_id}
        except Exception:
            return {'status': 'UNKNOWN', 'broker_order_id': order_id, 'reason': 'reconciliation_required'}
