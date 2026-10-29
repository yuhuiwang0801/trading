"""Read-only commissioning report for the authenticated Robinhood MCP account.

This module never binds a broker to the trading engine and cannot submit,
preview, cancel, approve or decline an order. Tool guides are data, not code or
instructions. Only aggregate, masked results leave this verifier.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re

from jsonschema import Draft202012Validator

from .mcp_broker import MCPAuthenticationRequired


READ_TOOLS = frozenset({'get_accounts', 'get_portfolio', 'get_equity_positions',
    'get_equity_orders', 'get_trade_approval_setting', 'get_trade_approvals', 'get_equity_tradability'})
MAX_PAGES = 100
OPEN_ORDER_STATES = frozenset({'new', 'queued', 'confirmed', 'unconfirmed', 'partially_filled', 'pending_cancelled', 'locating'})
TERMINAL_ORDER_STATES = frozenset({'filled', 'cancelled', 'rejected', 'failed', 'voided', 'partially_filled_rest_cancelled', 'locate_failed'})
APPROVAL_STATES = frozenset({'APPROVAL_STATE_PENDING', 'APPROVAL_STATE_APPROVED', 'APPROVAL_STATE_DECLINED',
                           'APPROVAL_STATE_EXPIRED', 'APPROVAL_STATE_AUTO_APPROVED'})
POSITION_AMOUNTS = ('quantity', 'intraday_quantity', 'shares_available_for_sells', 'shares_held_for_sells',
    'shares_held_for_stock_grants', 'shares_held_for_options_events', 'shares_held_for_asset_transfer',
    'shares_pending_from_options_events')
PORTFOLIO_AMOUNTS = ('total_value', 'equity_value', 'options_value', 'futures_value', 'event_contracts_value',
    'crypto_value', 'cash', 'pending_deposits', 'mutual_funds_value', 'fixed_income_value')


class _VerificationFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code, message):
    raise _VerificationFailure(code, message)


def _decimal(value, *, nonnegative=False):
    if not isinstance(value, str) or not value or len(value) > 128:
        _fail('invalid_financial_value', '金额或数量缺失，无法完成核验')
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail('invalid_financial_value', '金额或数量格式无效')
    if (not number.is_finite() or abs(number.as_tuple().exponent) > 30
            or len(number.as_tuple().digits) > 64 or abs(number.adjusted()) > 30
            or (nonnegative and number < 0)):
        _fail('invalid_financial_value', '金额或数量不是有效的有限数值')
    return number


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        _fail('invalid_identifier', '返回的记录标识无效')
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        _fail('invalid_timestamp', '返回的时间戳无效')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        _fail('invalid_timestamp', '返回的时间戳无效')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail('invalid_timestamp', '返回的时间戳没有时区')
    return value


def _no_external_refs(schema):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (not isinstance(value, str) or not value.startswith('#')):
                _fail('external_schema_reference', '接口结构包含外部引用，已停止核验')
            _no_external_refs(value)
    elif isinstance(schema, list):
        for value in schema:
            _no_external_refs(value)


def _validate(schema, data, code):
    if not isinstance(schema, dict):
        _fail(code, '接口未提供可验证的数据结构')
    _no_external_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(data)
    except _VerificationFailure:
        raise
    except Exception:
        _fail(code, '返回数据与已发现的接口结构不一致')


def _payload(result):
    if not isinstance(result, dict) or result.get('isError'):
        _fail('broker_tool_failed', 'Robinhood 只读查询未成功')
    if 'structuredContent' in result:
        payload = result['structuredContent']
    else:
        content = result.get('content')
        if not isinstance(content, list):
            _fail('ambiguous_tool_output', '只读查询没有唯一可验证的结构化结果')
        texts = [item.get('text') for item in content if isinstance(item, dict) and item.get('type') == 'text']
        if len(texts) != 1 or not isinstance(texts[0], str):
            _fail('ambiguous_tool_output', '只读查询没有唯一可验证的结构化结果')
        try:
            payload = json.loads(texts[0])
        except (ValueError, UnicodeError):
            _fail('invalid_tool_json', '只读查询结果不是有效 JSON')
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict) or not isinstance(payload.get('guide'), str):
        _fail('invalid_tool_payload', '只读查询缺少约定的数据结构')
    return payload


def _read(client, name, arguments):
    if name not in READ_TOOLS:
        _fail('write_operation_blocked', '验证器只允许账户只读查询')
    tool = client.tools.get(name)
    if not isinstance(tool, dict):
        _fail('required_tool_unavailable', '已发现的接口缺少必要只读工具')
    annotations = tool.get('annotations', {})
    if (not isinstance(annotations, dict) or annotations.get('readOnlyHint') is not True
            or annotations.get('destructiveHint', False) is not False):
        _fail('unsafe_tool_annotation', '工具没有明确标记为非破坏性只读接口')
    _validate(tool.get('inputSchema'), arguments, 'input_schema_mismatch')
    if not isinstance(tool.get('outputSchema'), dict):
        _fail('output_schema_unavailable', '只读接口未提供输出结构，已停止核验')
    # Validate the output schema itself before an account query is sent.
    _no_external_refs(tool['outputSchema'])
    try:
        Draft202012Validator.check_schema(tool['outputSchema'])
    except Exception:
        _fail('output_schema_invalid', '只读接口的输出结构无效')
    payload = _payload(client.call_discovered(name, arguments))
    _validate(tool['outputSchema'], payload, 'output_schema_mismatch')
    return payload['data']  # guide is deliberately ignored and never retained.


def _rows(data, field):
    rows = data.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _fail('incomplete_collection', '返回列表含未知或空记录，无法确认完整性')
    return rows


def _pages(client, name, field, account_number):
    arguments = {'account_number': account_number}
    rows, seen = [], set()
    for _ in range(MAX_PAGES):
        data = _read(client, name, arguments)
        if 'account_number' in data and data['account_number'] != account_number:
            _fail('account_scope_mismatch', '查询响应的账户与选定账户不一致')
        page = _rows(data, field)
        for row in page:
            if 'account_number' in row and row['account_number'] != account_number:
                _fail('account_scope_mismatch', '返回记录不属于选定账户')
        rows.extend(page)
        cursor = data.get('next', '')
        if not isinstance(cursor, str):
            _fail('invalid_pagination_cursor', '分页游标无效，无法确认完整性')
        if cursor == '':
            return rows
        if cursor in seen:
            _fail('repeated_pagination_cursor', '分页游标重复，无法确认完整性')
        seen.add(cursor)
        arguments = {'account_number': account_number, 'cursor': cursor}
    _fail('pagination_limit_exceeded', '查询超过分页上限，无法确认完整性')


def _positions(rows):
    seen, count, compatible = set(), 0, True
    for row in rows:
        symbol = _identifier(row['symbol'])
        if symbol in seen:
            _fail('duplicate_position', '持仓列表存在重复记录，无法确认完整性')
        seen.add(symbol)
        values = {field: _decimal(row.get(field), nonnegative=field not in {'quantity', 'intraday_quantity'}) for field in POSITION_AMOUNTS}
        if row.get('average_buy_price') is not None:
            _decimal(row['average_buy_price'], nonnegative=True)
        quantity = values['quantity']
        if quantity == 0:
            continue
        count += 1
        if quantity < 0 or row.get('type') != 'long':
            compatible = False
    return count, compatible


def _orders(rows):
    seen_orders, seen_fills, open_count, compatible = set(), set(), 0, True
    for row in rows:
        order_id = _identifier(row['id'])
        if order_id in seen_orders:
            _fail('duplicate_order', '订单分页存在重复记录，无法确认完整性')
        seen_orders.add(order_id)
        state = row['state']
        if state not in OPEN_ORDER_STATES | TERMINAL_ORDER_STATES:
            _fail('unknown_order_state', '订单状态尚未映射，无法判断剩余风险')
        open_count += state in OPEN_ORDER_STATES
        _identifier(row['symbol'])
        if row['side'] not in {'buy', 'sell', 'sell_short'}:
            _fail('unknown_order_side', '订单方向尚未映射')
        if row['side'] == 'sell_short' and state in OPEN_ORDER_STATES:
            compatible = False
        cumulative = _decimal(row['cumulative_quantity'], nonnegative=True)
        quantity = _decimal(row['quantity'], nonnegative=True) if row.get('quantity') is not None else None
        dollar_amount = row.get('dollar_based_amount')
        # Observed upstream shape: a canceled, entirely unfilled dollar-based
        # market order can retain zero requested shares instead of null. This
        # exception does not apply to share-sized, filled or other-state orders.
        unfilled_dollar_cancellation = (quantity == 0 and cumulative == 0 and state == 'cancelled'
            and row.get('type') == 'market' and row.get('executions') == []
            and isinstance(dollar_amount, dict) and dollar_amount.get('currency_code') == 'USD'
            and _decimal(dollar_amount.get('amount'), nonnegative=True) > 0)
        if quantity is not None and ((quantity <= 0 and not unfilled_dollar_cancellation) or cumulative > quantity):
            _fail('inconsistent_fill_quantity', '累计成交数量与订单数量不一致')
        if state == 'filled' and (cumulative <= 0 or (quantity is not None and cumulative != quantity)):
            _fail('inconsistent_fill_quantity', '已成交订单的累计数量与订单状态不一致')
        if state in {'partially_filled', 'partially_filled_rest_cancelled'} and (cumulative <= 0 or (quantity is not None and cumulative >= quantity)):
            _fail('inconsistent_fill_quantity', '部分成交订单的累计数量与订单状态不一致')
        for field in ('price', 'stop_price', 'average_price'):
            if row.get(field) is not None:
                _decimal(row[field], nonnegative=True)
        _decimal(row['fees'], nonnegative=True)
        _timestamp(row['created_at'])
        if row.get('last_transaction_at'):
            _timestamp(row['last_transaction_at'])
        if row.get('dollar_based_amount') is not None:
            _decimal(row['dollar_based_amount']['amount'], nonnegative=True)
            if row['dollar_based_amount']['currency_code'] != 'USD':
                _fail('unsupported_order_currency', '订单金额币种尚未支持')
        executions = row.get('executions')
        if executions is None and cumulative == 0:
            executions = []
        if not isinstance(executions, list):
            _fail('fill_history_incomplete', '有成交数量但没有完整成交明细')
        total = Decimal('0')
        for execution in executions:
            fill_id = _identifier(execution['id'])
            if fill_id in seen_fills:
                _fail('duplicate_fill', '成交标识重复，无法确认完整性')
            seen_fills.add(fill_id)
            quantity = _decimal(execution['quantity'], nonnegative=True)
            price = _decimal(execution['price'], nonnegative=True)
            if quantity <= 0 or price <= 0:
                _fail('invalid_fill', '成交价格或数量无效')
            with localcontext() as context:
                context.prec = 128
                total += quantity
            _decimal(execution['fees'], nonnegative=True)
            _timestamp(execution['timestamp'])
        if total != cumulative:
            _fail('fill_history_incomplete', '成交明细合计与累计成交数量不一致')
    return open_count, len(seen_fills), compatible


def _new_report():
    return {'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'connection_verified': False, 'account_verified': False, 'tool_count': 0,
        'account': {key: None for key in ('masked_number', 'account_type', 'trading_type', 'state', 'currency',
            'total_value', 'cash', 'buying_power', 'unleveraged_buying_power', 'positions_count', 'orders_count',
            'open_orders_count', 'fill_count', 'approval_required', 'approval_updated_at', 'pending_approvals_count')},
        'checks': [], 'blockers': ['order_preview_not_verified', 'live_execution_not_commissioned'],
        'execution': {'mode': 'paper', 'live_enabled': False, 'orders_sent': 0},
        'capabilities': {'preview_discovered': False, 'submit_discovered': False,
                         'cancel_discovered': False, 'fill_history_via_orders': False}}


def verify_robinhood(client):
    """Verify the one eligible account through read-only, validated tool calls."""
    report = _new_report()
    stage = 'connection'

    def passed(message):
        report['checks'].append({'name': stage, 'status': 'passed', 'message': message})

    def blocker(code):
        if code not in report['blockers']:
            report['blockers'].append(code)

    try:
        if not getattr(client, 'initialized', False):
            client.discover()
        if not getattr(client, 'initialized', False) or not isinstance(getattr(client, 'tools', None), dict):
            _fail('connection_not_initialized', '尚未完成经过授权的接口发现')
        report['connection_verified'] = True
        report['tool_count'] = len(client.tools)
        report['capabilities'].update({key: name in client.tools for key, name in (
            ('preview_discovered', 'review_equity_order'), ('submit_discovered', 'place_equity_order'),
            ('cancel_discovered', 'cancel_equity_order'))})
        passed('已完成经过授权的 MCP 接口发现')

        stage = 'account_scope'
        accounts = _rows(_read(client, 'get_accounts', {}), 'accounts')
        seen_accounts, eligible = set(), []
        for account in accounts:
            number = _identifier(account.get('account_number'))
            if len(number) <= 4 or number in seen_accounts:
                _fail('invalid_account_list', '账户列表存在无效或重复记录')
            seen_accounts.add(number)
            if account.get('agentic_allowed') is True:
                eligible.append(account)
        if len(eligible) != 1:
            _fail('eligible_account_not_unique', '无法确认唯一允许该连接交易的账户')
        selected = eligible[0]
        if selected['state'] != 'active' or selected['deactivated'] is not False or selected['permanently_deactivated'] is not False:
            _fail('account_not_active', '专用账户当前不可正常交易')
        account_number = selected['account_number']
        if selected['type'] not in {'cash', 'margin', 'limited_margin'} or not re.fullmatch(r'[a-z_]{1,64}', selected['brokerage_account_type']):
            _fail('unknown_account_type', '账户类型尚未映射')
        report['account'].update({'masked_number': '••••' + account_number[-4:],
            'account_type': selected['brokerage_account_type'], 'trading_type': selected['type'], 'state': 'active'})
        report['account_verified'] = True
        passed(f'已读取 {len(accounts)} 个账户，确认唯一允许该连接交易的活跃账户')

        stage = 'portfolio'
        portfolio = _read(client, 'get_portfolio', {'account_number': account_number})
        values = {name: _decimal(portfolio.get(name)) for name in PORTFOLIO_AMOUNTS}
        currency = portfolio.get('currency')
        if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
            _fail('invalid_currency', '账户币种缺失或无效')
        report['account'].update({'total_value': format(values['total_value'], 'f'), 'cash': format(values['cash'], 'f'), 'currency': currency})
        buying_power = portfolio.get('buying_power')
        if not isinstance(buying_power, dict):
            _fail('buying_power_unavailable', 'Robinhood 未提供可验证的股票购买力')
        buying = _decimal(buying_power.get('buying_power'))
        unleveraged = _decimal(buying_power.get('unleveraged_buying_power'))
        if buying_power.get('display_currency') != currency:
            _fail('currency_mismatch', '购买力与账户余额的币种不一致')
        for field in ('intraday_buying_power', 'off_intraday_buying_power'):
            if buying_power.get(field) is not None:
                _decimal(buying_power[field])
        report['account'].update({'buying_power': format(buying, 'f'), 'unleveraged_buying_power': format(unleveraged, 'f')})
        if buying <= 0 or unleveraged <= 0:
            blocker('insufficient_buying_power')
        if currency != 'USD':
            blocker('unsupported_account_currency')
        passed('已读取券商账户价值、现金及股票购买力；查询限定专用账户，响应不含账号回显')

        stage = 'positions'
        positions = _pages(client, 'get_equity_positions', 'positions', account_number)
        count, compatible = _positions(positions)
        report['account']['positions_count'] = count
        passed('持仓分页已完整读取；零股数记录不计入持仓，响应不含账号回显，归属仅由请求参数限定')
        if not compatible:
            blocker('unsupported_short_or_boxed_positions')
            report['checks'].append({'name': 'strategy_compatibility', 'status': 'partial',
                'message': '账户含做空、对锁或尚未支持的持仓类型，不能直接交给当前策略管理'})

        stage = 'orders_and_fills'
        orders = _pages(client, 'get_equity_orders', 'orders', account_number)
        open_count, fill_count, orders_compatible = _orders(orders)
        report['account'].update({'orders_count': len(orders), 'open_orders_count': open_count, 'fill_count': fill_count})
        report['capabilities']['fill_history_via_orders'] = True
        passed('订单及成交分页、成交标识和累计数量已核验；响应不含账号回显，归属仅由请求参数限定')
        if not orders_compatible:
            blocker('unsupported_short_orders')
            compatibility_check = next((check for check in report['checks'] if check['name'] == 'strategy_compatibility'), None)
            if compatibility_check:
                compatibility_check['message'] = '账户含当前策略尚不支持的持仓或做空挂单，不能直接交给策略管理'
            else:
                report['checks'].append({'name': 'strategy_compatibility', 'status': 'partial',
                    'message': '账户有未完成的做空订单，不能直接交给当前策略管理'})

        stage = 'approval_setting'
        setting = _read(client, 'get_trade_approval_setting', {'account_number': account_number})['setting']
        if setting['account_number'] != account_number:
            _fail('account_scope_mismatch', '交易审批设置不属于选定账户')
        report['account']['approval_required'] = setting['human_must_approve_trades']
        if setting.get('updated_at'):
            report['account']['approval_updated_at'] = _timestamp(setting['updated_at'])
        passed('已读取交易审批设置并核对响应中的账户')

        stage = 'pending_approvals'
        approvals = _pages(client, 'get_trade_approvals', 'approvals', account_number)
        seen_approvals, pending = set(), 0
        for approval in approvals:
            if approval['account_number'] != account_number:
                _fail('account_scope_mismatch', '待审批记录不属于选定账户')
            approval_id = _identifier(approval['approval_id'])
            if approval_id in seen_approvals:
                _fail('duplicate_approval', '待审批分页存在重复记录，无法确认完整性')
            seen_approvals.add(approval_id)
            if approval['state'] not in APPROVAL_STATES:
                _fail('unknown_approval_state', '交易审批状态尚未映射')
            pending += approval['state'] == 'APPROVAL_STATE_PENDING'
        report['account']['pending_approvals_count'] = pending
        passed('待审批订单分页已完整读取，并逐条核对账户')
        # Read commissioning cannot claim that preview, execution or live recovery
        # has been tested, even when every account read succeeds.
        report['status'] = 'partial' if report['blockers'] else 'passed'
    except _VerificationFailure as exc:
        blocker(exc.code)
        if exc.code == 'account_scope_mismatch':
            report['account_verified'] = False
        report['checks'].append({'name': stage, 'status': 'failed', 'message': exc.message})
    except MCPAuthenticationRequired:
        report['connection_verified'] = False
        report['account_verified'] = False
        blocker('authorization_required')
        report['checks'].append({'name': stage, 'status': 'failed', 'message': 'Robinhood 授权已失效或尚未完成，需要重新连接'})
    except Exception:
        blocker('broker_read_failed')
        report['checks'].append({'name': stage, 'status': 'failed', 'message': 'Robinhood 只读查询未能完成，未执行任何交易'})
    return report
