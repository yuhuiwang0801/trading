"""Pure V2 live checks. Frozen rules; no broker writes or research-file mutation."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

from .calendar import calendar_for_year, previous_session, session_bounds
from .db import utc
from .strategy import D

STRATEGY_ID = 'reversal_v2'
TREND_ID = 'trend_breakout'
CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config' / 'reversal_research_v2.json'


def rules():
    return json.loads(CONFIG_PATH.read_text())


def config_hash():
    return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()


def shifted_session(day, count):
    cal = calendar_for_year(int(day[:4]))
    return cal.session_offset(pd.Timestamp(day), count).date().isoformat()


def entry_window(now):
    bounds = session_bounds(str(utc(now).astimezone(ZoneInfo('America/New_York')).date()))
    return bool(bounds and bounds[0] + timedelta(minutes=5) <= utc(now) < bounds[0] + timedelta(minutes=10))


def exit_window(now):
    bounds = session_bounds(str(utc(now).astimezone(ZoneInfo('America/New_York')).date()))
    return bool(bounds and bounds[0] + timedelta(minutes=5) <= utc(now) < bounds[1])


def valid_entry_packet(packet, session):
    try:
        return bool(isinstance(packet, dict) and packet.get('strategy_id') == STRATEGY_ID
                    and packet.get('quality') in {'ready', 'valid'}
                    and packet.get('config_hash') == config_hash()
                    and packet.get('signal_session') == previous_session(session)
                    and packet.get('execution_session', packet.get('execute_session')) == session
                    and isinstance(packet.get('candidates'), list))
    except (ValueError, TypeError, KeyError):
        return False


def update_position(position, observations, session):
    """Replay missing daily closes exactly once and retain the first exit trigger.

    Relative returns use signal-close anchoring, as in frozen V2. Absolute return
    anchors the adjusted price series to verified fill prices (fees included).
    A missing relative day permanently disables partial-path rebound decisions;
    calendar timeout remains active even during missing/failing data refreshes.
    """
    pos = deepcopy(position)
    cfg = rules()['dynamic_exit']
    last = pos.get('last_daily_session') or pos['signal_session']
    end = previous_session(session)
    rows = {r['session']: r for r in observations if isinstance(r, dict)
            and isinstance(r.get('session'), str) and r['session'] <= end}
    # Vendor total-return series may be rebased by a newly effective dividend
    # or split. Recompute the series conversion from the same snapshot; the
    # actual fill price and frozen entry gap remain immutable.
    entry_observation = rows.get(pos['entry_session'], {})
    if entry_observation.get('quality') in {'valid', 'ready', 'missing_peer_residual'}:
        try:
            factor = D(entry_observation['adjusted_open']) / D(entry_observation['open'])
            if factor > 0:
                pos['entry_adjustment_factor'] = str(factor)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            pass
    complete = pos.get('relative_path_complete', True)
    cumulative = D(pos.get('cumulative_relative_log_return', '0'))
    day = shifted_session(last, 1)
    available_end = max(rows, default='')
    while day <= end:
        # A not-yet-refreshed packet is not proof of a permanently missing bar.
        # Keep the checkpoint so the next fresh packet can replay this close.
        if day > available_end:
            break
        obs = rows.get(day, {})
        valid = obs.get('quality') in {'valid', 'ready', 'missing_peer_residual'}
        try:
            residual = D(obs['residual_log_return']) if valid else None
        except (KeyError, ValueError, TypeError):
            residual = None
        if residual is None:
            complete = False
        else:
            cumulative += residual
        total_return = None
        if valid:
            try:
                factor = D(pos['entry_adjustment_factor'])
                total_return = D(obs['total_return_index']) / (D(pos['avg_price']) * factor) - 1
                pos['last_price'] = str(D(obs['close']))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                pass
        reason = None
        if not pos.get('exit_latched'):
            if valid and obs.get('fundamental_known') is True and obs.get('fundamental_exit') is True:
                reason = 'FUNDAMENTAL_RISK'
            elif total_return is not None and total_return <= -D(cfg['absolute_stop_loss']):
                reason = 'ABSOLUTE_STOP'
            elif complete and cumulative <= -D(pos['entry_gap']) * D(cfg['relative_stop_multiple']):
                reason = 'RELATIVE_STOP'
            elif complete and cumulative >= D(pos['entry_gap']) * D(cfg['rebound_fraction']):
                reason = 'REBOUND_TARGET'
            elif shifted_session(day, 1) >= pos['max_exit_session']:
                reason = 'MAX_HOLD'
            if reason:
                pos.update(exit_latched=True, exit_reason=reason, exit_signal_session=day,
                           exit_due_session=shifted_session(day, 1),
                           exit_signal_total_return=str(total_return) if total_return is not None else None,
                           exit_signal_relative_log_return=str(cumulative) if complete else None)
        pos['last_daily_session'] = day
        day = shifted_session(day, 1)
    pos.update(relative_path_complete=complete, cumulative_relative_log_return=str(cumulative))
    if session >= pos['max_exit_session'] and not pos.get('exit_latched'):
        pos.update(exit_latched=True, exit_reason='MAX_HOLD', exit_due_session=pos['max_exit_session'],
                   exit_signal_session=previous_session(pos['max_exit_session']))
    return pos
