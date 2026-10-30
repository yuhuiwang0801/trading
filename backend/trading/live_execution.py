"""Isolated Robinhood execution ledger. Nothing in this module uses the paper ledger.

Network writes require a capable service and a durable, explicitly armed scope.
The app's activation button controls the scope; no environment toggle grants it.
The stable reference and SUBMITTING state are committed *before* submission. An
ambiguous response is reconciled by reads; it is never automatically retried.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from .calendar import market_state, session_bounds
from .db import utc, stamp
from .risk import size_entry, validate_quote
from .mcp_broker import LiveExecutionDisabled
from .quote_guard import cross_quote_check
from .robinhood_adapter import normalize_preview_request
from .robinhood_verify import _VerificationFailure
from .strategy import DEFAULT_PARAMS, D, closed_bars, entry_signal, exit_signal, limit_price_for
from . import live_reversal as reversal
from . import live_policy


ZERO = Decimal('0')
NY = ZoneInfo('America/New_York')
ARM_CONFIRMATION = 'ENABLE LIVE AUTO TRADING'
ACTIVE = {'PREPARED', 'PREVIEWED', 'SUBMITTING', 'UNKNOWN', 'AWAITING_APPROVAL',
          'SUBMITTED', 'ACCEPTED', 'CONFIRMED', 'QUEUED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'}
TERMINAL = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED', 'PREVIEW_BLOCKED', 'ABANDONED'}
_SYMBOL = re.compile(r'^[A-Z][A-Z0-9.\-]{0,9}$')


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


def _dump(value):
    return json.dumps(_json(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _fingerprint(value):
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _public(value):
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()
                if k not in {'account_number', 'raw', 'guide', 'account_id', 'rhs_account_number'}}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def _params(values=None):
    result = {**DEFAULT_PARAMS, **(values or {})}
    if set(result) - set(DEFAULT_PARAMS):
        raise ValueError('Unknown strategy parameter')
    for key, value in result.items():
        if key == 'regular_session_only':
            if value is not True:
                raise ValueError('Live execution only supports regular sessions')
        elif isinstance(value, bool) or D(value) <= 0:
            raise ValueError('Strategy values must be positive')
        elif (key.endswith('_pct') or key == 'participation_rate') and D(value) > 1:
            raise ValueError('Strategy percentage must be <= 1')
    for key in ('max_positions', 'max_candidates', 'min_listing_sessions', 'scan_interval_seconds',
                'quote_max_age_seconds', 'bar_max_age_seconds', 'order_ttl_seconds', 'max_consecutive_failures'):
        if D(result[key]) != D(result[key]).to_integral_value():
            raise ValueError('Integer strategy value required')
        result[key] = int(result[key])
    return _json(result)


def _same_params(left, right):
    """Numeric spelling changes do not change the reviewed strategy."""
    return (set(left) == set(right) and
            all(left[k] is value if isinstance(value, bool) else D(left[k]) == D(value)
                for k, value in right.items()))


class LiveExecutionService:
    """One service may be instantiated in API and worker processes with one path.

    `recover` is a worker startup operation, not an API construction operation.
    It always disarms, so restarting a worker never resumes live submissions.
    Public methods return masked JSON-safe values. Adapter calls receive the full
    account number only inside this process.
    """

    def __init__(self, path, adapter, write_enabled=False, *, server_enabled=None):
        path = str(path)
        if path.startswith('sqlite:///'):
            path = path[len('sqlite:///'):]
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self._enabled = write_enabled if server_enabled is None else server_enabled
        self._mutex = threading.RLock()
        self._write_guard = None
        self._stop_path = Path(str(self.path) + '.stop')
        try:
            descriptor = os.open(self._stop_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(uuid.uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        with self._locked() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS live_meta (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_intents (
                    id TEXT PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL,
                    ref_id TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_fills (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_positions (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            if self._get(db, 'configuration') is None:
                self._put(db, 'configuration', {'revision': 0, 'symbols': [], 'max_order_amount': '0',
                          'max_daily_added': '0', 'max_positions': 0, 'params': _params(),
                          'configured': False, 'account_number': None, 'strategy_id': reversal.TREND_ID})
                self._put(db, 'control', {'armed': False, 'armed_at': None, 'reason': 'NOT_ENABLED'})
                self._put(db, 'blocks', [])
                self._put(db, 'schema_version', 1)
                db.commit()
        if hasattr(adapter, 'write_enabled'):
            self._adapter_enabled = adapter.write_enabled
            adapter.write_enabled = self._adapter_write_gate

    def _stop_generation(self):
        try:
            value = self._stop_path.read_text()
            return value if len(value) == 36 else None
        except OSError:
            return None

    def _stop_now(self):
        """Invalidate writes immediately, including while a scan holds its lock."""
        descriptor, temporary = tempfile.mkstemp(prefix=self._stop_path.name, dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(uuid.uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._stop_path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    def _adapter_write_gate(self):
        original = self._adapter_enabled() if callable(self._adapter_enabled) else self._adapter_enabled
        return original is True and self._may_write() and self._guard_allows()

    def _guard_allows(self):
        guard = self._write_guard
        if not guard:
            return False
        if guard.get('policy_hash'):
            try:
                if live_policy.policy_for(guard['strategy_id'])['policy_hash'] != guard['policy_hash']:
                    return False
            except (OSError, KeyError, TypeError, ValueError):
                return False
        now = guard['now'] + timedelta(seconds=time.monotonic() - guard['started'])
        if 'cross_quote' in guard:
            inputs = guard['cross_quote']
            guard['quote_check'] = cross_quote_check(**inputs, now=now)
            if guard['quote_check']['status'] != 'PASSED':
                return False
        if not market_state(now)['is_open'] or not 0 <= (now - utc(guard['mirror_at'])).total_seconds() <= 60:
            return False
        if guard.get('expires_at') and now >= utc(guard['expires_at']):
            return False
        if guard.get('quote') is not None and validate_quote(guard['quote'], now, guard['params']):
            return False
        return True

    def _may_write(self):
        if not self._flag():
            return False
        generation = self._stop_generation()
        if generation is None:
            return False
        # No flock here: stop requests must reach the adapter's final write gate
        # even while another process owns the scan/preview lock.
        try:
            with sqlite3.connect(self.path, timeout=1) as db:
                control = self._get(db, 'control', {})
                config = self._get(db, 'configuration', {})
            if config.get('scope_mode') == live_policy.MODE:
                if (not live_policy.matches(config) or control.get('policy_hash') != config.get('policy_hash')
                        or control.get('configuration_revision') != config.get('revision')):
                    return False
            return control.get('armed') is True and control.get('stop_generation') == generation
        except (sqlite3.Error, OSError, ValueError):
            return False

    @contextmanager
    def _locked(self):
        with self._mutex:
            lock_path = Path(str(self.path) + '.lock')
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                db = sqlite3.connect(self.path, timeout=30)
                os.chmod(self.path, 0o600)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA synchronous=FULL')
                try:
                    yield db
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _flag(self):
        return (self._enabled() if callable(self._enabled) else self._enabled) is True

    @staticmethod
    def _get(db, key, default=None):
        row = db.execute('SELECT payload FROM live_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db, key, value):
        db.execute('INSERT INTO live_meta(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                   (key, _dump(value)))

    @staticmethod
    def _rows(db, table):
        if table not in {'live_intents', 'live_orders', 'live_fills', 'live_positions'}:
            raise ValueError('Invalid ledger table')
        return [json.loads(row[0]) for row in db.execute(f'SELECT payload FROM {table}')]

    @staticmethod
    def _save(db, table, ident, value):
        if table not in {'live_orders', 'live_fills', 'live_positions'}:
            raise ValueError('Invalid ledger table')
        db.execute(f'INSERT INTO {table}(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (ident, _dump(value)))

    @staticmethod
    def _intent(db, value):
        db.execute('INSERT INTO live_intents(id,signal_key,ref_id,created_at,payload) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (value['id'], value['signal_key'], value['ref_id'], value['created_at'], _dump(value)))

    @staticmethod
    def _audit(db, event, detail=None, now=None):
        db.execute('INSERT INTO live_audit(timestamp,event,payload) VALUES(?,?,?)',
                   (stamp(now), event, _dump(_public(detail or {}))))

    def _block(self, db, reason, now=None):
        blocks = self._get(db, 'blocks', [])
        if reason not in blocks:
            blocks.append(reason)
            self._put(db, 'blocks', blocks)
            self._audit(db, 'EXECUTION_BLOCKED', {'reason': reason}, now)
        control = self._get(db, 'control')
        if control['armed']:
            self._put(db, 'control', {**control, 'armed': False, 'reason': reason})

    @staticmethod
    def _validated_scope(scope, previous, params=None):
        """Validate before reads or mutations so malformed setup is a no-op."""
        allowed = {'symbols', 'max_order_amount', 'max_daily_added', 'max_positions', 'strategy_id'}
        if not isinstance(scope, dict) or set(scope) - allowed:
            raise ValueError('Only symbols and live execution limits may be configured')
        merged = {**{k: previous.get(k, reversal.TREND_ID if k == 'strategy_id' else None) for k in allowed}, **scope}
        if merged['strategy_id'] not in {reversal.TREND_ID, reversal.STRATEGY_ID}:
            raise ValueError('Unknown live strategy')
        if merged['strategy_id'] == reversal.STRATEGY_ID:
            merged['strategy_config_hash'] = reversal.config_hash()
        symbols = merged['symbols']
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 30 or any(
                not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError('Configure 1–30 uppercase stock symbols')
        if len(set(symbols)) != len(symbols):
            raise ValueError('Duplicate stock symbol')
        for key in ('max_order_amount', 'max_daily_added'):
            if isinstance(merged[key], bool) or not ZERO < D(merged[key]) <= D('1000000'):
                raise ValueError('Live limits must be positive and bounded')
            merged[key] = format(D(merged[key]).normalize(), 'f')
        count = D(merged['max_positions'])
        if isinstance(merged['max_positions'], bool) or count != count.to_integral_value() or not 1 <= count <= 30:
            raise ValueError('Maximum positions must be an integer between 1 and 30')
        merged['max_positions'] = int(count)
        merged['params'] = _params(params if params is not None else previous['params'])
        return deepcopy(merged)

    def _configure_locked(self, db, values, now=None):
        previous = self._get(db, 'configuration')
        if self._get(db, 'control')['armed']:
            raise ValueError('Disarm before changing live scope')
        if any(i['status'] in ACTIVE for i in self._rows(db, 'live_intents')):
            raise ValueError('Resolve existing live instructions before changing scope')
        held = {p['symbol'] for p in self._rows(db, 'live_positions') if D(p['quantity']) > 0}
        if held and previous.get('strategy_id', reversal.TREND_ID) != values['strategy_id']:
            raise ValueError('Close managed positions before changing strategy')
        all_eligible = (values.get('scope_mode') == live_policy.MODE and
                        values.get('strategy_policy', {}).get('universe_mode') == 'eligible_active_us_equities')
        if held - set(values['symbols']) and not all_eligible:
            raise ValueError('Keep all managed positions in the permitted symbols')
        mirror = self._get(db, 'mirror')
        if not mirror or not mirror.get('compatible'):
            raise ValueError('Verify the real account before configuring live execution')
        if previous.get('account_number') and previous['account_number'] != mirror['account_number']:
            raise ValueError('The configured account has changed; explicit ledger migration is required')
        # Rechecking an unchanged setup must not revoke the user's reviewed
        # revision, including equivalent decimal representations from the UI.
        same = (previous.get('configured') and previous.get('strategy_id', reversal.TREND_ID) == values['strategy_id']
                and previous.get('strategy_config_hash') == values.get('strategy_config_hash')
                and previous['symbols'] == values['symbols']
                and previous['max_positions'] == values['max_positions']
                and previous.get('scope_mode') == values.get('scope_mode')
                and previous.get('policy_hash') == values.get('policy_hash')
                and live_policy.matches(previous)
                and (values.get('scope_mode') == live_policy.MODE or
                     all(D(previous[k]) == D(values[k]) for k in ('max_order_amount', 'max_daily_added')))
                and _same_params(previous['params'], values['params']))
        if same:
            blocks = self._get(db, 'blocks', [])
            if any(b in blocks for b in ('STRATEGY_PARAMETERS_CHANGED', 'STRATEGY_POLICY_CHANGED')):
                self._put(db, 'blocks', [b for b in blocks if b not in {'STRATEGY_PARAMETERS_CHANGED', 'STRATEGY_POLICY_CHANGED'}])
                self._audit(db, 'SCOPE_RECONFIRMED', {'configuration_revision': previous['revision']}, now)
                db.commit()
            return self._snapshot(db, now)
        config = {**values, 'revision': previous['revision'] + 1, 'configured': True,
                  'account_number': mirror['account_number']}
        self._put(db, 'configuration', config)
        self._put(db, 'blocks', [b for b in self._get(db, 'blocks', []) if b not in {'STRATEGY_PARAMETERS_CHANGED', 'STRATEGY_POLICY_CHANGED'}])
        self._put(db, 'preview', None)
        self._audit(db, 'SCOPE_CONFIGURED', _public(config), now)
        db.commit()
        return self._snapshot(db, now)

    def select_strategy(self, strategy_id):
        """Select a strategy while stopped; selection never arms or contacts a broker."""
        if strategy_id not in {reversal.TREND_ID, reversal.STRATEGY_ID}:
            raise ValueError('Unknown live strategy')
        with self._locked() as db:
            if self._get(db, 'control')['armed']:
                raise ValueError('Disarm before changing strategy')
            if any(i['status'] in ACTIVE for i in self._rows(db, 'live_intents')):
                raise ValueError('Resolve existing live instructions before changing strategy')
            if any(D(p['quantity']) > 0 for p in self._rows(db, 'live_positions')):
                raise ValueError('Close managed positions before changing strategy')
            config = self._get(db, 'configuration')
            expected = reversal.config_hash() if strategy_id == reversal.STRATEGY_ID else None
            if (config.get('strategy_id', reversal.TREND_ID) != strategy_id or
                    config.get('strategy_config_hash') != expected):
                config.update(strategy_id=strategy_id, strategy_config_hash=expected,
                              revision=config['revision'] + 1)
                # A policy for another strategy is not a reusable authorization.
                if config.get('scope_mode') == live_policy.MODE:
                    for key in ('scope_mode', 'policy_version', 'policy_hash', 'universe_hash', 'strategy_policy'):
                        config.pop(key, None)
                    config['configured'] = False
                self._put(db, 'configuration', config)
                self._put(db, 'preview', None)
                self._audit(db, 'STRATEGY_SELECTED', {'strategy_id': strategy_id,
                            'configuration_revision': config['revision'], 'strategy_config_hash': expected})
                db.commit()
            return self._snapshot(db)

    def configure(self, scope, params=None):
        with self._locked() as db:
            values = self._validated_scope(scope, self._get(db, 'configuration'), params)
            return self._configure_locked(db, values)

    def prepare(self, scope, params=None, now=None):
        """Read the account and save scope in one lock; never preview or arm."""
        now = utc(now)
        started = time.monotonic()
        with self._locked() as db:
            values = self._validated_scope(scope, self._get(db, 'configuration'), params)
            # Reject before reconciliation: prepare cannot implicitly stop an
            # armed session or change its account/configuration evidence.
            if self._get(db, 'control')['armed']:
                raise ValueError('Disarm before preparing live execution')
            self._reconcile(db, now)
            if any(reason in self._get(db, 'blocks', []) for reason in ('ACCOUNT_READ_FAILED', 'ACCOUNT_CHANGED')):
                raise ValueError('Real account check failed; live scope was not changed')
            return self._configure_locked(db, values, now + timedelta(seconds=time.monotonic() - started))

    def _refresh_pristine_baseline(self, db, now):
        """Recapture manual activity only before this ledger ever traded.

        This never rewrites order/fill ownership or adopts broker positions. Any
        automated history, active external instruction, stale read or account
        mismatch leaves the existing investigation blocks intact.
        """
        blocks = self._get(db, 'blocks', [])
        external = {'EXTERNAL_ORDER_ACTIVITY', 'EXTERNAL_POSITION_ACTIVITY'}
        if not external.intersection(blocks) or self._get(db, 'control')['armed']:
            return
        if any(self._rows(db, table) for table in ('live_intents', 'live_orders', 'live_fills', 'live_positions')):
            return
        mirror, baseline, config = self._get(db, 'mirror'), self._get(db, 'baseline'), self._get(db, 'configuration')
        if (not mirror or not baseline or not mirror.get('compatible') or
                baseline['account_number'] != mirror['account_number'] or
                config.get('account_number') not in {None, mirror['account_number']} or
                not 0 <= (now - utc(mirror['checked_at'])).total_seconds() <= 60 or
                any(str(o.get('state', '')).upper() not in TERMINAL for o in mirror['orders']) or
                any(str(a.get('state', '')).upper() not in {'APPROVED', 'DECLINED', 'REJECTED', 'EXPIRED', 'CANCELLED'}
                    for a in mirror['approvals']) or
                set(blocks) - external):
            return
        replacement = {'account_number': mirror['account_number'],
                       'positions': {p['symbol']: str(D(p['quantity'])) for p in mirror['positions']},
                       'orders': {o['id']: _fingerprint(o) for o in mirror['orders']},
                       'approvals': {a['approval_id']: _fingerprint(a) for a in mirror['approvals']},
                       'created_at': stamp(now)}
        self._put(db, 'baseline', replacement)
        self._put(db, 'blocks', [b for b in blocks if b not in external])
        self._audit(db, 'PRISTINE_BASELINE_REFRESHED', {
            'prior_positions': len(baseline['positions']), 'current_positions': len(mirror['positions']),
            'prior_orders': len(baseline['orders']), 'current_orders': len(mirror['orders']),
            'cleared': sorted(external.intersection(blocks)), 'positions_adopted': 0,
            'prior_baseline_hash': _fingerprint(baseline), 'current_baseline_hash': _fingerprint(replacement)}, now)
        db.commit()

    def prepare_strategy(self, strategy_id, params=None, now=None):
        """Read the account and derive trusted policy; never preview, arm or trade.

        ``params`` is intentionally not used: managed strategies own their
        execution parameters, while legacy prepare keeps its existing contract.
        """
        policy = live_policy.policy_for(strategy_id)  # Validate before broker reads.
        now = utc(now)
        started = time.monotonic()
        with self._locked() as db:
            if self._get(db, 'control')['armed']:
                raise ValueError('Disarm before preparing live execution')
            previous = self._get(db, 'configuration')
            same_policy = (previous.get('scope_mode') == live_policy.MODE and
                           previous.get('strategy_id') == strategy_id and
                           previous.get('policy_hash') == policy['policy_hash'] and live_policy.matches(previous))
            if any(i['status'] in ACTIVE for i in self._rows(db, 'live_intents')) and not same_policy:
                raise ValueError('Resolve existing live instructions before changing scope')
            self._reconcile(db, now)
            checked = now + timedelta(seconds=time.monotonic() - started)
            mirror = self._get(db, 'mirror')
            if (not mirror or not mirror.get('compatible') or
                    not 0 <= (checked - utc(mirror['checked_at'])).total_seconds() <= 60 or
                    any(b in self._get(db, 'blocks', []) for b in ('ACCOUNT_READ_FAILED', 'ACCOUNT_CHANGED', 'ACCOUNT_INCOMPATIBLE'))):
                raise ValueError('Real account check failed; strategy scope was not changed')
            values = live_policy.managed_configuration(strategy_id, mirror['account'])
            if values['policy_hash'] != policy['policy_hash']:
                raise ValueError('Strategy policy changed during account checks; prepare again')
            self._refresh_pristine_baseline(db, checked)
            if same_policy and any(i['status'] in ACTIVE for i in self._rows(db, 'live_intents')):
                # Restart recovery may re-confirm the same policy around an
                # existing broker instruction. No scope or revision is changed.
                return self._snapshot(db, checked)
            return self._configure_locked(db, values, checked)

    def _readiness(self, db, now=None):
        now = utc(now)
        config = self._get(db, 'configuration')
        control = self._get(db, 'control')
        mirror = self._get(db, 'mirror')
        blockers = list(self._get(db, 'blocks', []))
        if not live_policy.matches(config): blockers.append('STRATEGY_POLICY_CHANGED')
        exit_recovery = (config.get('scope_mode') == live_policy.MODE and
                         (any(D(p['quantity']) > 0 for p in self._rows(db, 'live_positions')) or
                          any(i['status'] in ACTIVE for i in self._rows(db, 'live_intents'))))
        if not self._flag(): blockers.append('SERVER_WRITE_DISABLED')
        if not config['configured']: blockers.append('LIVE_SCOPE_NOT_CONFIGURED')
        if not mirror: blockers.append('ACCOUNT_NOT_RECONCILED')
        else:
            age = (now - utc(mirror['checked_at'])).total_seconds()
            if age < 0 or age > 60: blockers.append('ACCOUNT_SNAPSHOT_STALE')
            if not mirror.get('compatible'): blockers.append('ACCOUNT_INCOMPATIBLE')
            if config.get('account_number') and config['account_number'] != mirror['account_number']:
                blockers.append('ACCOUNT_CHANGED')
            if min(D(mirror['account']['cash']), D(mirror['account']['buying_power']),
                   D(mirror['account']['unleveraged_buying_power'])) <= 0 and not exit_recovery:
                blockers.append('NO_UNLEVERAGED_BUYING_POWER')
            if config.get('scope_mode') == live_policy.MODE and D(mirror['account']['total_value']) <= 0 and not exit_recovery:
                blockers.append('STRATEGY_CAPITAL_UNAVAILABLE')
        # Manual preview is optional. Each actual strategy instruction still
        # requires a fresh broker preview and cross-source book check in _execute.
        if any(i['status'] in {'UNKNOWN', 'SUBMITTING'} for i in self._rows(db, 'live_intents')):
            blockers.append('UNKNOWN_INSTRUCTION')
        return {'ready_to_arm': not blockers, 'blockers': sorted(set(blockers)),
                'armed': control['armed'], 'server_write_enabled': self._flag(),
                'configuration_revision': config['revision'],
                'confirmation_text': ARM_CONFIRMATION, 'regular_session_only': True,
                'market_open': market_state(now)['is_open']}

    def readiness(self, now=None):
        with self._locked() as db:
            return self._readiness(db, now)

    def _snapshot(self, db, now=None):
        mirror = self._get(db, 'mirror')
        owned = self._rows(db, 'live_positions')
        intents = self._rows(db, 'live_intents')
        account = dict(mirror['account']) if mirror else None
        if account:
            # The broker's settled/unleveraged buying power remains authoritative.
            # Reserving again is conservative and covers pending approval requests.
            reserved = sum((D(i.get('reserved_cash', '0')) for i in intents if i['status'] in ACTIVE), ZERO)
            account['local_reserved_cash'] = str(reserved)
            account['execution_buying_power'] = str(max(ZERO, min(D(account['cash']), D(account['buying_power']),
                                                                     D(account['unleveraged_buying_power'])) - reserved))
        audit = [{'sequence': r[0], 'timestamp': r[1], 'event': r[2], 'detail': json.loads(r[3])}
                 for r in db.execute('SELECT sequence,timestamp,event,payload FROM live_audit ORDER BY sequence DESC LIMIT 200')]
        config = self._get(db, 'configuration')
        if live_policy.matches(config):
            config = live_policy.effective_configuration(config, mirror['account'] if mirror else {})
        if config.get('strategy_policy'):
            config = {**config, 'strategy_policy': live_policy.public_policy(config['strategy_policy'])}
        return _public({'mode': 'live' if self._get(db, 'control')['armed'] else 'paper',
                        'configuration': config, 'control': self._get(db, 'control'),
                        'readiness': self._readiness(db, now), 'account': account,
                        'checked_at': mirror.get('checked_at') if mirror else None,
                        'positions': [p for p in owned if D(p['quantity']) > 0],
                        'broker_positions': mirror.get('positions', []) if mirror else [],
                        'intents': sorted(intents, key=lambda x: x['created_at'], reverse=True),
                        'orders': self._rows(db, 'live_orders'), 'fills': self._rows(db, 'live_fills'),
                        'preview': self._get(db, 'preview'), 'last_scan': self._get(db, 'last_scan'),
                        'last_quote_check': self._get(db, 'last_quote_check'),
                        'audit': audit, 'protection_location': 'local_service',
                        'existing_positions_managed': False, 'ledger': 'isolated_live'})

    def snapshot(self):
        with self._locked() as db:
            return self._snapshot(db)

    def arm(self, confirmation, configuration_revision, now=None):
        if confirmation != ARM_CONFIRMATION:
            raise ValueError('Explicit live execution confirmation is required')
        generation = self._stop_generation()
        with self._locked() as db:
            config = self._get(db, 'configuration')
            if type(configuration_revision) is not int or configuration_revision != config['revision']:
                raise ValueError('Live scope changed; review the current configuration')
            result = self._readiness(db, now)
            if not result['ready_to_arm']:
                raise ValueError('Live execution blocked: ' + ', '.join(result['blockers']))
            if generation is None or generation != self._stop_generation():
                raise ValueError('A stop request arrived; review and confirm live execution again')
            self._put(db, 'control', {'armed': True, 'armed_at': stamp(now), 'reason': 'USER_CONFIRMED',
                                    'configuration_revision': config['revision'], 'stop_generation': generation,
                                    'policy_hash': config.get('policy_hash')})
            self._audit(db, 'LIVE_ARMED', {'configuration_revision': config['revision']}, now)
            db.commit()
            return self._snapshot(db, now)

    def disarm(self):
        self._stop_now()
        with self._locked() as db:
            control = self._get(db, 'control')
            self._put(db, 'control', {**control, 'armed': False, 'reason': 'USER_DISARMED'})
            self._audit(db, 'LIVE_DISARMED', {'broker_orders_cancelled': 0})
            db.commit()
            return self._snapshot(db)

    def preview(self, request, now=None):
        """Read-only technical preview; no order intent or broker write is created."""
        now = utc(now)
        if not isinstance(request, dict) or set(request) - {'symbol', 'side', 'type', 'quantity', 'dollar_amount', 'limit_price'}:
            raise ValueError('预览仅接受股票、方向、订单类型及对应金额或股数')
        with self._locked() as db:
            self._reconcile(db, now)
            mirror = self._get(db, 'mirror')
            if not mirror or not mirror.get('compatible'):
                raise ValueError('An authorized compatible account is required')
            symbol = request.get('symbol')
            if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
                raise ValueError('股票代码无效')
            try:
                order = normalize_preview_request({**request, 'account_number': mirror['account_number'],
                                                   'ref_id': str(uuid.uuid4())})
            except _VerificationFailure as exc:
                raise ValueError(exc.message) from None
            result = self.adapter.preview(order)
            result = {**result, 'checked_at': result.get('checked_at') or stamp(now)}
            config = self._get(db, 'configuration')
            result['configuration_revision'] = config['revision']
            # Amount/fractional market previews never qualify a limit-only strategy for arming.
            result['scope_matched'] = bool(order.get('type', 'limit') == 'limit' and config['configured'] and order['symbol'] in config['symbols'] and
                                           D(order['quantity']) * D(order['limit_price']) <= D(config['max_order_amount']))
            result['preview_only'] = True
            result['automatic_order_type'] = 'whole_share_limit'
            result['order'] = _public(order)
            self._put(db, 'preview', _public(result))
            self._audit(db, 'ORDER_PREVIEWED', {'status': result.get('status'), 'order': _public(order),
                                             'scope_matched': result['scope_matched']}, now)
            db.commit()
            return _public(result)

    @staticmethod
    def _canonical_order(request, account_number, ref_id=None):
        symbol, side = request.get('symbol'), request.get('side')
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or side not in {'BUY', 'SELL'}:
            raise ValueError('A valid stock symbol and BUY or SELL side are required')
        qty, price = D(request.get('quantity')), D(request.get('limit_price'))
        if qty <= 0 or qty != qty.to_integral_value() or qty > 1000000:
            raise ValueError('Only positive whole-share orders are supported')
        if price <= 0 or price > 1000000 or price != price.quantize(D('.01')):
            raise ValueError('Limit price must be positive whole cents')
        return {'account_number': account_number, 'symbol': symbol, 'side': side,
                'quantity': str(qty), 'limit_price': str(price), 'ref_id': ref_id or str(uuid.uuid4())}

    def recover(self, now=None):
        """Called once by the worker on startup. Never resubmit a durable intent."""
        now = utc(now)
        self._stop_now()
        with self._locked() as db:
            self._put(db, 'control', {**self._get(db, 'control'), 'armed': False, 'reason': 'WORKER_RESTARTED'})
            for intent in self._rows(db, 'live_intents'):
                if intent['status'] in {'PREPARED', 'PREVIEWED'}:
                    self._abandon_unsent(db, intent, 'RECOVERY_UNSENT_ABANDONED', now)
                elif intent['status'] == 'SUBMITTING':
                    intent['status'] = 'UNKNOWN'
                    self._intent(db, intent)
                    self._block(db, 'UNKNOWN_INSTRUCTION', now)
            self._audit(db, 'WORKER_RECOVERY', {'armed': False, 'submission_retries': 0}, now)
            db.commit()
            self._reconcile(db, now)
            return self._snapshot(db, now)

    def reconcile(self, now=None):
        with self._locked() as db:
            self._reconcile(db, utc(now))
            return self._snapshot(db, now)

    def _reconcile(self, db, now):
        intents = self._rows(db, 'live_intents')
        approval_ids = [i['approval_id'] for i in intents if i.get('approval_id')]
        try:
            mirror = self.adapter.read_snapshot(approval_ids=approval_ids or None)
            self._validate_mirror(mirror)
        except Exception:
            self._block(db, 'ACCOUNT_READ_FAILED', now)
            self._audit(db, 'ACCOUNT_READ_FAILED', {'message': 'Account snapshot could not be validated'}, now)
            db.commit()
            return
        # A previous read outage is recoverable. Structural ledger/external changes
        # are deliberately latched until an operator investigates their evidence.
        self._put(db, 'blocks', [b for b in self._get(db, 'blocks', []) if b != 'ACCOUNT_READ_FAILED'])
        mirror = {**mirror, 'checked_at': mirror.get('checked_at') or stamp(now)}
        config = self._get(db, 'configuration')
        baseline = self._get(db, 'baseline')
        if not mirror.get('compatible'):
            self._block(db, 'ACCOUNT_INCOMPATIBLE', now)
        if config.get('account_number') and config['account_number'] != mirror['account_number']:
            self._block(db, 'ACCOUNT_CHANGED', now)
            db.commit()
            return
        if baseline and baseline['account_number'] != mirror['account_number']:
            self._block(db, 'ACCOUNT_CHANGED', now)
            db.commit()
            return
        if baseline is None:
            baseline = {'account_number': mirror['account_number'],
                        'positions': {p['symbol']: str(D(p['quantity'])) for p in mirror['positions']},
                        'orders': {o['id']: _fingerprint(o) for o in mirror['orders']},
                        'approvals': {a['approval_id']: _fingerprint(a) for a in mirror['approvals']},
                        'created_at': stamp(now)}
            self._put(db, 'baseline', baseline)
            self._audit(db, 'UNMANAGED_BASELINE_CAPTURED', {'positions': len(mirror['positions']),
                                                         'orders': len(mirror['orders'])}, now)
        orders_by_id = {o['id']: o for o in mirror['orders']}
        orders_by_ref = {o.get('ref_id'): o for o in mirror['orders'] if o.get('ref_id')}
        approvals = {a['approval_id']: a for a in mirror['approvals']}
        owned_order_ids = set()
        owned_approval_ids = set()
        unresolved = False
        for intent in intents:
            approval = approvals.get(intent.get('approval_id') or intent['ref_id'])
            if approval:
                intent['approval_id'] = approval['approval_id']
                owned_approval_ids.add(approval['approval_id'])
                if approval.get('is_edited') or approval.get('asset_class') != 'ASSET_CLASS_EQUITY':
                    self._block(db, 'APPROVAL_CHANGED', now)
                proposal = approval.get('proposed_order')
                if proposal is not None and not self.adapter._proposal_matches(proposal, intent):
                    self._block(db, 'APPROVAL_CHANGED', now)
                approval_state = str(approval['state']).upper()
                if approval_state in {'DECLINED', 'REJECTED', 'EXPIRED', 'CANCELLED'} and not approval.get('order_id'):
                    intent.update(status='REJECTED', reserved_cash='0', reserved_quantity='0')
                elif approval.get('order_id'):
                    # Approved requests can receive a new order ref_id. The explicit
                    # approval -> order_id relationship is the only allowed bridge.
                    intent['broker_order_id'] = approval['order_id']
                elif approval_state in {'PENDING', 'AWAITING_APPROVAL'}:
                    intent['status'] = 'AWAITING_APPROVAL'
            order = (orders_by_id.get(intent.get('broker_order_id')) or orders_by_ref.get(intent['ref_id']) or
                     orders_by_ref.get(intent.get('approval_id')))
            if order:
                owned_order_ids.add(order['id'])
                if not self._matches(intent, order):
                    self._block(db, 'ORDER_CONTRACT_CHANGED', now)
                else:
                    intent['broker_order_id'] = order['id']
                    self._save(db, 'live_orders', order['id'], {**order, 'intent_id': intent['id'], 'owner': 'system'})
                    order_fills = [f for f in mirror['fills'] if f['order_id'] == order['id']]
                    for fill in sorted(order_fills, key=lambda f: (f['timestamp'], f['id'])):
                        self._apply_fill(db, intent, fill, now)
                    cumulative = sum((D(f['quantity']) for f in self._rows(db, 'live_fills') if f['order_id'] == order['id']), ZERO)
                    if cumulative != D(order['filled_quantity']):
                        self._block(db, 'FILL_LEDGER_MISMATCH', now)
                    else:
                        intent['filled_quantity'] = str(cumulative)
                        state = str(order['state']).upper()
                        if state in TERMINAL:
                            intent.update(status=state, reserved_cash='0', reserved_quantity='0')
                        elif state in ACTIVE:
                            intent['status'] = 'CANCEL_PENDING' if intent.get('cancel_requested_at') else state
                            remainder = max(ZERO, D(intent['quantity']) - cumulative)
                            intent['reserved_cash'] = str(remainder * D(intent['limit_price']) *
                                                          (1 + D(intent['params']['fee_bps']) / 10000)) if intent['side'] == 'BUY' else '0'
                            intent['reserved_quantity'] = str(remainder) if intent['side'] == 'SELL' else '0'
                        else:
                            self._block(db, 'UNKNOWN_ORDER_STATE', now)
            elif intent['status'] in {'SUBMITTING', 'UNKNOWN', 'SUBMITTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'}:
                intent['status'] = 'UNKNOWN'
                unresolved = True
            self._intent(db, intent)
        if unresolved:
            self._block(db, 'UNKNOWN_INSTRUCTION', now)
        else:
            # A positive broker relationship resolves UNKNOWN; absence never does.
            self._put(db, 'blocks', [b for b in self._get(db, 'blocks', []) if b != 'UNKNOWN_INSTRUCTION'])
        if not any(i['status'] == 'AWAITING_APPROVAL' and now >= utc(i['expires_at'])
                   for i in self._rows(db, 'live_intents')):
            self._put(db, 'blocks', [b for b in self._get(db, 'blocks', []) if b != 'STALE_PENDING_APPROVAL'])
        for order in mirror['orders']:
            if order['id'] in owned_order_ids:
                continue
            if baseline['orders'].get(order['id']) != _fingerprint(order):
                self._block(db, 'EXTERNAL_ORDER_ACTIVITY', now)
            if str(order['state']).upper() in ACTIVE:
                self._block(db, 'EXTERNAL_OPEN_ORDER', now)
        for approval in mirror['approvals']:
            if approval['approval_id'] not in owned_approval_ids and str(approval['state']).upper() in {'PENDING', 'AWAITING_APPROVAL'}:
                self._block(db, 'EXTERNAL_PENDING_APPROVAL', now)
        owned = {p['symbol']: D(p['quantity']) for p in self._rows(db, 'live_positions')}
        actual = {p['symbol']: D(p['quantity']) for p in mirror['positions']}
        expected = {s: D(q) + owned.get(s, ZERO) for s, q in baseline['positions'].items()}
        for symbol, quantity in owned.items():
            expected.setdefault(symbol, quantity)
        for symbol in set(actual) | set(expected):
            if actual.get(symbol, ZERO) != expected.get(symbol, ZERO):
                self._block(db, 'EXTERNAL_POSITION_ACTIVITY', now)
        self._put(db, 'mirror', mirror)
        session = str(now.astimezone(NY).date())
        day = self._get(db, 'daily_baseline')
        if not day or day['session_date'] != session:
            self._put(db, 'daily_baseline', {'session_date': session, 'equity': mirror['account']['total_value']})
        self._put(db, 'last_reconciliation', {'checked_at': stamp(now), 'full_pagination': True,
                                           'blockers': self._get(db, 'blocks', [])})
        db.commit()

    @staticmethod
    def _validate_mirror(mirror):
        if not isinstance(mirror, dict) or not mirror.get('account_number') or type(mirror.get('compatible')) is not bool:
            raise ValueError('Invalid account mirror')
        if any(not isinstance(mirror.get(k), list) for k in ('positions', 'orders', 'fills', 'approvals')):
            raise ValueError('Incomplete account collections')
        account = mirror['account']
        if account.get('currency') != 'USD' or account.get('type') != 'cash':
            raise ValueError('Only a USD cash account is supported')
        for key in ('cash', 'buying_power', 'unleveraged_buying_power', 'total_value'):
            if D(account[key]) < 0:
                raise ValueError('Negative account funds')
        for key, ident in (('orders', 'id'), ('fills', 'id'), ('positions', 'symbol'), ('approvals', 'approval_id')):
            ids = [row[ident] for row in mirror[key]]
            if len(ids) != len(set(ids)):
                raise ValueError('Duplicate account records')
        for position in mirror['positions']:
            if D(position['quantity']) < 0 or D(position['available_quantity']) < 0:
                raise ValueError('Short positions are not supported')
        if mirror.get('checked_at'):
            utc(mirror['checked_at'])

    @staticmethod
    def _matches(intent, order):
        return (order['symbol'] == intent['symbol'] and order['side'] == intent['side'] and
                order.get('quantity') is not None and D(order['quantity']) == D(intent['quantity']) and
                order.get('limit_price') is not None and D(order['limit_price']) == D(intent['limit_price']) and
                order.get('type') == 'limit' and order.get('time_in_force') == 'gfd' and
                order.get('market_hours') == 'regular_hours' and order.get('trigger') == 'immediate' and
                order.get('agent_initiated') is True)

    def _apply_fill(self, db, intent, fill, now):
        prior = db.execute('SELECT payload FROM live_fills WHERE id=?', (fill['id'],)).fetchone()
        if prior:
            saved = json.loads(prior[0])
            if any(saved.get(k) != fill.get(k) for k in ('order_id', 'symbol', 'side', 'quantity', 'price', 'fees', 'timestamp')):
                self._block(db, 'EXECUTION_CHANGED', now)
            return
        quantity, price, fees = D(fill['quantity']), D(fill['price']), D(fill['fees'])
        if quantity <= 0 or price <= 0 or fees < 0 or fill['side'] != intent['side'] or fill['symbol'] != intent['symbol']:
            self._block(db, 'INVALID_EXECUTION', now)
            return
        existing = next((p for p in self._rows(db, 'live_positions') if p['symbol'] == intent['symbol']), None)
        if existing and D(existing['quantity']) == 0 and intent['side'] == 'BUY':
            existing = None
        strategy_id = intent.get('strategy_id', reversal.TREND_ID)
        if existing and existing.get('strategy_id', reversal.TREND_ID) != strategy_id:
            self._block(db, 'POSITION_STRATEGY_MISMATCH', now)
            return
        if existing is None and intent['side'] == 'SELL':
            self._block(db, 'SELL_EXCEEDS_MANAGED_POSITION', now)
            return
        position = existing or {'symbol': intent['symbol'], 'quantity': '0', 'average_price': '0', 'avg_price': '0',
                                'opened_at': fill['timestamp'], 'highest_close': str(price),
                                'last_bar_at': None, 'last_price': str(price), 'managed': True,
                                'strategy_id': strategy_id, 'params': intent['params'],
                                'realized_pnl': '0', 'exit_latched': False}
        if strategy_id == reversal.STRATEGY_ID and intent['side'] == 'BUY':
            fill_session = str(utc(fill['timestamp']).astimezone(NY).date())
            if fill_session != intent['evidence']['execution_session']:
                self._block(db, 'REVERSAL_LATE_ENTRY_FILL', now)
        if existing is None and strategy_id == reversal.STRATEGY_ID:
            evidence = intent['evidence']
            session = str(utc(fill['timestamp']).astimezone(NY).date())
            position.update(entry_gap=evidence['entry_gap'], signal_session=evidence['signal_session'],
                            entry_session=session, issuer_id=evidence['issuer_id'], industry=evidence['industry'],
                            strategy_config_hash=evidence['strategy_config_hash'],
                            cumulative_relative_log_return='0', relative_path_complete=True,
                            last_daily_session=evidence['signal_session'],
                            max_exit_session=reversal.shifted_session(session, reversal.rules()['dynamic_exit']['max_hold_sessions']))
        elif existing is None:
            position.update(entry_atr=intent['evidence']['atr'], ma20=intent['evidence']['ma20'],
                            protection=str(price - D(intent['params']['initial_stop_atr']) * D(intent['evidence']['atr'])))
        current, avg = D(position['quantity']), D(position['avg_price'])
        if intent['side'] == 'BUY':
            new_quantity = current + quantity
            new_avg = (current * avg + quantity * price + fees) / new_quantity
            position.update(quantity=str(new_quantity), avg_price=str(new_avg), average_price=str(new_avg),
                            last_price=str(price), highest_close=str(max(D(position['highest_close']), price)))
            if strategy_id != reversal.STRATEGY_ID:
                position['protection'] = str(max(D(position['protection']), new_avg - D(intent['params']['initial_stop_atr']) * D(position['entry_atr'])))
            realized = ZERO
        else:
            if quantity > current:
                self._block(db, 'SELL_EXCEEDS_MANAGED_POSITION', now)
                return
            realized = quantity * (price - avg) - fees
            position.update(quantity=str(current - quantity), last_price=str(price),
                            realized_pnl=str(D(position['realized_pnl']) + realized))
            if current == quantity:
                position['exit_latched'] = False
        self._save(db, 'live_fills', fill['id'], {**fill, 'intent_id': intent['id'], 'realized_pnl': str(realized)})
        self._save(db, 'live_positions', position['symbol'], position)
        self._audit(db, 'FILL_APPLIED', {'fill_id': fill['id'], 'intent_id': intent['id'], 'symbol': fill['symbol'],
                                     'side': fill['side'], 'quantity': fill['quantity']}, now)

    def _effective_params(self, config):
        params = dict(config['params'])
        for key in ('max_order_amount', 'max_daily_added', 'max_positions'):
            params[key] = (D(config[key]) if config.get('scope_mode') == live_policy.MODE else
                           min(D(params[key]), D(config[key])))
        params['max_positions'] = int(params['max_positions'])
        return _json(params)

    def scan(self, frame, now=None, params=None):
        now = utc(now)
        reference_now = now
        started = time.monotonic()
        with self._locked() as db:
            control = self._get(db, 'control')
            config = self._get(db, 'configuration')
            if control['armed'] and config.get('scope_mode') == live_policy.MODE and (
                    not live_policy.matches(config) or control.get('policy_hash') != config.get('policy_hash')):
                self._block(db, 'STRATEGY_POLICY_CHANGED', now)
                db.commit()
                return self._scan_result(db, 'BLOCKED', now)
            if not control['armed'] or not self._may_write():
                if control['armed']:
                    reason = 'SERVER_WRITE_DISABLED' if not self._flag() else 'STOP_REQUESTED'
                    self._put(db, 'control', {**control, 'armed': False, 'reason': reason})
                    db.commit()
                return {'status': 'DISARMED', 'orders_sent': 0, 'cancel_requests': 0}
            if frame.get('strategy_id', reversal.TREND_ID) != config.get('strategy_id', reversal.TREND_ID):
                return self._scan_result(db, 'STRATEGY_MISMATCH', now)
            if (config.get('strategy_id') == reversal.STRATEGY_ID and
                    config.get('strategy_config_hash') != reversal.config_hash()):
                self._block(db, 'STRATEGY_PARAMETERS_CHANGED', now)
                db.commit()
                return self._scan_result(db, 'BLOCKED', now)
            if (config.get('scope_mode') != live_policy.MODE and params is not None
                    and not _same_params(_params(params), config['params'])):
                self._block(db, 'STRATEGY_PARAMETERS_CHANGED', now)
                db.commit()
                return {'status': 'BLOCKED', 'reason': 'STRATEGY_PARAMETERS_CHANGED', 'orders_sent': 0}
            self._reconcile(db, now)
            now = now + timedelta(seconds=time.monotonic() - started)
            blocks = self._get(db, 'blocks', [])
            if blocks or not self._get(db, 'control')['armed']:
                return {'status': 'BLOCKED', 'blockers': blocks, 'orders_sent': 0}
            calendar = market_state(now)
            day = calendar['session_date']
            if not calendar['is_open'] or frame.get('market_open') is not True:
                return self._scan_result(db, 'MARKET_CLOSED', now)
            if (frame.get('source') != 'alpaca' or frame.get('quality') != 'valid' or
                    frame.get('session_date') != day):
                return self._scan_result(db, 'INVALID_MARKET_DATA', now)
            mirror = self._get(db, 'mirror')
            if not 0 <= (now - utc(mirror['checked_at'])).total_seconds() <= 60:
                return self._scan_result(db, 'ACCOUNT_SNAPSHOT_STALE', now)
            config = live_policy.effective_configuration(config, mirror['account'])
            settings = self._effective_params(config)
            sent, cancellations = 0, 0
            # TTL cancellation applies solely to our known broker orders. A pending
            # approval is never treated as an order or automatically approved.
            for intent in self._rows(db, 'live_intents'):
                if intent['status'] == 'AWAITING_APPROVAL' and now >= utc(intent['expires_at']):
                    self._block(db, 'STALE_PENDING_APPROVAL', now)
                    db.commit()
                    return self._scan_result(db, 'BLOCKED', now, sent, cancellations)
                if (intent['status'] in {'SUBMITTED', 'ACCEPTED', 'CONFIRMED', 'QUEUED', 'PARTIALLY_FILLED'} and
                        intent.get('broker_order_id') and not intent.get('cancel_requested_at') and
                        now >= utc(intent['expires_at'])):
                    intent.update(status='CANCEL_PENDING', cancel_requested_at=stamp(now))
                    self._intent(db, intent)
                    self._audit(db, 'CANCEL_REQUESTED', {'intent_id': intent['id'], 'order_id': intent['broker_order_id']}, now)
                    db.commit()
                    if not self._may_write():
                        break
                    self._write_guard = {'now': reference_now, 'started': started,
                                         'mirror_at': mirror['checked_at'], 'policy_hash': config.get('policy_hash'),
                                         'strategy_id': config.get('strategy_id', reversal.TREND_ID)}
                    try:
                        response = self.adapter.cancel(config['account_number'], intent['broker_order_id'])
                        self._audit(db, 'CANCEL_RESPONSE', {'intent_id': intent['id'], 'status': response.get('status')}, now)
                    except Exception:
                        self._audit(db, 'CANCEL_RESPONSE_UNKNOWN', {'intent_id': intent['id']}, now)
                    finally:
                        self._write_guard = None
                    # Accepted cancellation does not release any reservation.
                    db.commit()
                    cancellations += 1
            if config.get('strategy_id') == reversal.STRATEGY_ID:
                return self._scan_reversal(db, config, settings, frame, now, reference_now, started, cancellations)
            positions = {p['symbol']: p for p in self._rows(db, 'live_positions') if D(p['quantity']) > 0}
            broker_positions = {p['symbol']: p for p in mirror['positions']}
            candidates = {c['symbol']: c for c in frame.get('candidates', [])}
            for symbol, position in positions.items():
                if position.get('strategy_id', reversal.TREND_ID) != reversal.TREND_ID:
                    continue
                bars = closed_bars(frame.get('bars', {}).get(symbol, []), now)
                quote = frame.get('quotes', {}).get(symbol)
                if not bars or (now - utc(bars[-1]['timestamp'])).total_seconds() > int(settings['bar_max_age_seconds']):
                    continue
                indicator = candidates.get(symbol) or frame.get('holding_indicators', {}).get(symbol)
                if not indicator or not indicator.get('ma20'):
                    continue
                position['ma20'] = str(D(indicator['ma20']))
                event = exit_signal({**position, 'params': settings}, bars, position['ma20'], now)
                position.update(highest_close=str(event['highest_close']), protection=str(event['protection']),
                                last_bar_at=stamp(bars[-1]['timestamp']), last_price=str(D(bars[-1]['close'])),
                                exit_latched=bool(position.get('exit_latched') or event['triggered']))
                self._save(db, 'live_positions', symbol, position)
                db.commit()
                if not position['exit_latched'] or validate_quote(quote, now, settings):
                    continue
                active = self._rows(db, 'live_intents')
                if any(i['symbol'] == symbol and i['status'] in ACTIVE for i in active):
                    continue
                available = D(broker_positions.get(symbol, {}).get('available_quantity', '0'))
                quantity = min(D(position['quantity']), available).to_integral_value(rounding=ROUND_DOWN)
                if quantity < 1:
                    continue
                limit = limit_price_for('SELL', quote, settings)
                # Each exit chunk obeys the same explicitly configured order cap.
                quantity = min(quantity, (D(settings['max_order_amount']) / limit).to_integral_value(rounding=ROUND_DOWN))
                if quantity < 1:
                    continue
                key = f"exit:{symbol}:{position['opened_at']}:{bars[-1]['timestamp']}"
                result = self._execute(db, config, settings, {'symbol': symbol, 'side': 'SELL', 'quantity': quantity,
                                       'limit_price': limit}, key, {'atr': position['entry_atr'], 'ma20': position['ma20']},
                                       reference_now + timedelta(seconds=time.monotonic() - started), frame)
                sent += int(result)
                if self._get(db, 'blocks', []):
                    return self._scan_result(db, 'BLOCKED', now, sent, cancellations)
            if frame.get('market_filter') is True:
                for candidate in sorted(candidates.values(), key=lambda x: int(x.get('rank', 999))):
                    symbol = candidate['symbol']
                    universe_managed = (config.get('scope_mode') == live_policy.MODE and
                                        config['strategy_policy']['universe_mode'] == 'eligible_active_us_equities')
                    if ((not universe_managed and symbol not in config['symbols']) or
                            candidate.get('tradable') is False or
                            (universe_managed and candidate.get('tradable') is not True)):
                        continue
                    # Existing manual positions are intentionally not accumulated.
                    if D(broker_positions.get(symbol, {}).get('quantity', '0')) > 0:
                        continue
                    bars = closed_bars(frame.get('bars', {}).get(symbol, []), now)
                    quote = frame.get('quotes', {}).get(symbol)
                    if len(bars) < 2 or (now - utc(bars[-1]['timestamp'])).total_seconds() > int(settings['bar_max_age_seconds']):
                        continue
                    event = entry_signal(candidate, bars, now)
                    if not event or validate_quote(quote, now, settings):
                        continue
                    limit = limit_price_for('BUY', quote, settings)
                    if abs(limit - event['price']) / event['price'] > D(settings['max_price_deviation_pct']):
                        continue
                    active = [i for i in self._rows(db, 'live_intents') if i['status'] in ACTIVE]
                    if any(i['symbol'] == symbol for i in active):
                        continue
                    reserved = sum((D(i['reserved_cash']) for i in active), ZERO)
                    account = mirror['account']
                    buying_power = min(D(account['cash']), D(account['buying_power']), D(account['unleveraged_buying_power']))
                    # Entire broker exposure and all real positions constrain risk,
                    # including those we deliberately do not manage.
                    exposure = max(ZERO, D(account['total_value']) - D(account['cash']))
                    count = len({p['symbol'] for p in mirror['positions'] if D(p['quantity']) > 0} |
                                {i['symbol'] for i in active if i['side'] == 'BUY'})
                    added = sum((D(f['quantity']) * D(f['price']) + D(f['fees']) for f in self._rows(db, 'live_fills')
                                 if f['side'] == 'BUY' and str(utc(f['timestamp']).astimezone(NY).date()) == day), ZERO)
                    pnl = D(account['total_value']) - D(self._get(db, 'daily_baseline')['equity'])
                    sizing = size_entry(account['total_value'], buying_power, exposure, reserved, count,
                                        limit, event['atr'], settings, day_added=added, daily_pnl=pnl)
                    if not sizing['allowed']:
                        self._audit(db, 'ENTRY_RISK_BLOCKED', {'symbol': symbol, 'reason': sizing['reason']}, now)
                        db.commit()
                        continue
                    result = self._execute(db, config, settings, {'symbol': symbol, 'side': 'BUY',
                                           'quantity': sizing['quantity'], 'limit_price': limit},
                                           f'entry:{symbol}:{day}', {'atr': event['atr'], 'ma20': candidate['ma20'],
                                           'signal_bar_time': event['bar_time'], 'signal_price': event['price']},
                                           reference_now + timedelta(seconds=time.monotonic() - started), frame)
                    sent += int(result)
                    if self._get(db, 'blocks', []):
                        return self._scan_result(db, 'BLOCKED', now, sent, cancellations)
            return self._scan_result(db, 'SUCCESS', now, sent, cancellations)

    def _scan_reversal(self, db, config, settings, frame, now, reference_now, started, cancellations):
        day = market_state(now)['session_date']
        packet = frame.get('reversal_packet') or {}
        mirror = self._get(db, 'mirror')
        broker_positions = {p['symbol']: p for p in mirror['positions'] if D(p['quantity']) > 0}
        positions = {p['symbol']: p for p in self._rows(db, 'live_positions') if D(p['quantity']) > 0}
        sent = 0
        # The frozen daily strategy uses only V2-owned positions. Manual account
        # holdings constrain entry risk, but are never adopted or sold here.
        for symbol, position in positions.items():
            if position.get('strategy_id', reversal.TREND_ID) != reversal.STRATEGY_ID:
                continue
            observations = (packet.get('observations', {}).get(symbol, [])
                            if isinstance(packet, dict) and packet.get('strategy_id') == reversal.STRATEGY_ID
                            and packet.get('config_hash') == config.get('strategy_config_hash')
                            and (config.get('scope_mode') != live_policy.MODE or
                                 packet.get('metadata_hash') == config.get('universe_hash')) else [])
            position = reversal.update_position(position, observations, day)
            if not position.get('exit_latched'):
                position = self._update_intraday_position(
                    db, position, packet, frame, config, settings, reference_now, started)
            self._save(db, 'live_positions', symbol, position)
            db.commit()
            # Intraday protection starts at the regular open. The original
            # completed-day signals retain their 09:35 execution convention.
            intraday_exit = str(position.get('exit_reason', '')).startswith('INTRADAY_')
            exit_now = reference_now + timedelta(seconds=time.monotonic() - started)
            allowed_exit = (market_state(exit_now)['is_open'] if intraday_exit
                            else reversal.exit_window(exit_now))
            if (not position.get('exit_latched') or day < position['exit_due_session']
                    or not allowed_exit):
                continue
            quote = frame.get('quotes', {}).get(symbol)
            if validate_quote(quote, exit_now, settings):
                continue
            intents = self._rows(db, 'live_intents')
            if any(i['symbol'] == symbol and i['status'] in ACTIVE for i in intents):
                continue
            limit = limit_price_for('SELL', quote, settings)
            # A buy allocation limit must never strand an appreciated share.
            # SELL only reduces verified system ownership and remains bounded
            # by the broker's available shares; all execution gates still apply.
            quantity = min(D(position['quantity']), D(broker_positions.get(symbol, {}).get('available_quantity', '0')),
                           D('1000000')).to_integral_value(rounding=ROUND_DOWN)
            if quantity < 1:
                continue
            # A cancelled/partial exit may retry its remaining verified shares;
            # unresolved instructions retain their reservation and block retries.
            attempt = sum(i['side'] == 'SELL' and i['symbol'] == symbol and
                          i.get('evidence', {}).get('position_opened_at') == position['opened_at'] for i in intents)
            evidence = {'strategy_id': reversal.STRATEGY_ID, 'position_opened_at': position['opened_at'],
                        'exit_reason': position['exit_reason'], 'exit_signal_session': position.get('exit_signal_session')}
            if intraday_exit:
                evidence.update(intraday_evidence=position.get('intraday_evidence', {}),
                                trigger_quote_check=position.get('intraday_quote_check', {}))
            result = self._execute(db, config, settings, {'symbol': symbol, 'side': 'SELL', 'quantity': quantity,
                                   'limit_price': limit}, f"reversal-exit:{symbol}:{position['opened_at']}:{attempt}",
                                   evidence, reference_now + timedelta(seconds=time.monotonic() - started), frame)
            sent += int(result)
            if self._get(db, 'blocks', []):
                return self._scan_result(db, 'BLOCKED', now, sent, cancellations)
        if not reversal.entry_window(now):
            return self._scan_result(db, 'REVERSAL_WAITING_ENTRY_WINDOW', now, sent, cancellations)
        if not reversal.valid_entry_packet(packet, day):
            return self._scan_result(db, 'REVERSAL_DAILY_DATA_UNAVAILABLE', now, sent, cancellations)
        if (config.get('scope_mode') == live_policy.MODE and
                packet.get('metadata_hash') != config.get('universe_hash')):
            return self._scan_result(db, 'REVERSAL_UNIVERSE_MISMATCH', now, sent, cancellations)
        cfg = reversal.rules()
        master = packet.get('security_master', {})
        account = mirror['account']
        equity = D(account['total_value'])
        cash = min(D(account['cash']), D(account['buying_power']), D(account['unleveraged_buying_power']))
        if equity <= 0:
            return self._scan_result(db, 'NO_UNLEVERAGED_BUYING_POWER', now, sent, cancellations)
        for candidate in sorted(packet['candidates'], key=lambda x: int(x.get('rank', 999))):
            symbol = candidate.get('symbol')
            if symbol not in config['symbols'] or candidate.get('tradable') is False:
                continue
            try:
                if D(candidate['entry_gap']) < D(cfg['entry_filters']['minimum_relative_log_gap']):
                    continue
                if not candidate.get('issuer_id') or not candidate.get('industry') or D(candidate['signal_price']) <= 0:
                    continue
                # A candidate must describe the same identity as the full peer
                # universe, which is computed independently of user scope.
                identity = master[symbol]
                if any(str(identity[k]) != str(candidate[k]) for k in ('issuer_id', 'industry')):
                    continue
                if config.get('scope_mode') == live_policy.MODE:
                    trusted = config['strategy_policy']['security_master'][symbol]
                    if any(str(identity.get(k)) != str(trusted[k]) or str(candidate.get(k)) != str(trusted[k])
                           for k in ('security_id', 'issuer_id', 'industry')):
                        continue
            except (KeyError, TypeError, ValueError):
                continue
            quote = frame.get('quotes', {}).get(symbol)
            if validate_quote(quote, now, settings):
                continue
            intents = self._rows(db, 'live_intents')
            active = [i for i in intents if i['status'] in ACTIVE]
            fills = self._rows(db, 'live_fills')
            if (symbol in broker_positions or symbol in positions or any(i['symbol'] == symbol for i in active)
                    or any(f['symbol'] == symbol and str(utc(f['timestamp']).astimezone(NY).date()) == day for f in fills)):
                continue
            accepted_today = [i for i in intents if i['side'] == 'BUY' and i.get('strategy_id') == reversal.STRATEGY_ID
                              and i.get('evidence', {}).get('execution_session') == day
                              and i['status'] not in {'ABANDONED', 'PREVIEW_BLOCKED', 'REJECTED', 'FAILED'}]
            if len(accepted_today) >= cfg['max_entries_per_day']:
                break
            occupied = set(broker_positions) | {i['symbol'] for i in active if i['side'] == 'BUY'}
            if len(occupied) >= min(cfg['max_positions'], config['max_positions']):
                break
            pending = [i for i in active if i['side'] == 'BUY']
            reserved = sum((D(i.get('reserved_cash', '0')) for i in pending), ZERO)
            added = sum((D(f['quantity']) * D(f['price']) + D(f['fees']) for f in fills
                         if f['side'] == 'BUY' and str(utc(f['timestamp']).astimezone(NY).date()) == day), ZERO)
            day_reserved = sum((D(i.get('reserved_cash', '0')) for i in pending
                                if str(utc(i['created_at']).astimezone(NY).date()) == day), ZERO)
            issuer_exposure = industry_exposure = ZERO
            identities_known = True
            for held_symbol, held in broker_positions.items():
                meta = master.get(held_symbol) or positions.get(held_symbol)
                if not meta or not meta.get('issuer_id') or not meta.get('industry'):
                    identities_known = False
                    break
                hquote = frame.get('quotes', {}).get(held_symbol)
                value = D(held.get('market_value', '0'))
                if hquote and not validate_quote(hquote, now, settings):
                    value = D(held['quantity']) * D(hquote['ask'])
                elif value <= 0:
                    # No reliable mark means there is no proven sector capacity.
                    identities_known = False
                    break
                if str(meta['issuer_id']) == str(candidate['issuer_id']): issuer_exposure += value
                if str(meta['industry']) == str(candidate['industry']): industry_exposure += value
            for instruction in pending:
                meta = instruction.get('evidence', {})
                if not meta.get('issuer_id') or not meta.get('industry'):
                    identities_known = False
                    break
                value = D(instruction['reserved_cash'])
                if str(meta['issuer_id']) == str(candidate['issuer_id']): issuer_exposure += value
                if str(meta['industry']) == str(candidate['industry']): industry_exposure += value
            if not identities_known:
                continue
            limit = limit_price_for('BUY', quote, settings)
            allowance = min(equity * D(cfg['max_security_weight']), D(config['max_order_amount']), cash - reserved,
                            equity * D(cfg['max_issuer_weight']) - issuer_exposure,
                            equity * D(cfg['max_industry_weight']) - industry_exposure,
                            min(equity * D(cfg['max_daily_buy_weight']), D(config['max_daily_added'])) - added - day_reserved)
            quantity = (max(ZERO, allowance) / (limit * (1 + D(settings['fee_bps']) / 10000))).to_integral_value(rounding=ROUND_DOWN)
            if quantity < 1:
                continue
            close_time = session_bounds(packet['signal_session'])[1]
            execution_settings = dict(settings)
            # Session freshness is checked above. Keep the real previous-close
            # timestamp while permitting its overnight/weekend age in quote guard.
            execution_settings['bar_max_age_seconds'] = int((session_bounds(day)[0] + timedelta(minutes=10) - close_time).total_seconds())
            evidence = {'strategy_id': reversal.STRATEGY_ID, 'strategy_config_hash': config['strategy_config_hash'],
                        'entry_gap': candidate['entry_gap'], 'score': candidate.get('score'),
                        'issuer_id': candidate['issuer_id'], 'industry': candidate['industry'],
                        'signal_session': packet['signal_session'], 'execution_session': day,
                        'signal_price': candidate['signal_price'], 'signal_bar_time': stamp(close_time)}
            result = self._execute(db, config, execution_settings, {'symbol': symbol, 'side': 'BUY',
                                   'quantity': quantity, 'limit_price': limit}, f'reversal-entry:{symbol}:{day}',
                                   evidence, reference_now + timedelta(seconds=time.monotonic() - started), frame)
            sent += int(result)
            if self._get(db, 'blocks', []):
                return self._scan_result(db, 'BLOCKED', now, sent, cancellations)
        return self._scan_result(db, 'SUCCESS', now, sent, cancellations)

    def _update_intraday_position(self, db, position, packet, frame, config, settings, reference_now, started):
        """Confirm the executable book before advancing any intraday peak/exit.

        This is a read-only quote comparison, not an order or a broker preview.
        A one-source spike cannot permanently latch an exit. _execute performs
        its own preview and fresh cross-source check before any submission.
        """
        symbol = position['symbol']
        contexts = frame.get('intraday_context')
        context = contexts.get(symbol, {}) if isinstance(contexts, dict) else {}
        if not isinstance(context, dict) or context.get('price_basis_valid') is not True:
            return {**position, 'intraday_status': 'blocked',
                    'intraday_reason': context.get('reason', 'intraday_price_basis_unavailable')
                    if isinstance(context, dict) else 'intraday_price_basis_unavailable'}
        checked = reference_now + timedelta(seconds=time.monotonic() - started)
        quote = frame.get('quotes', {}).get(symbol)
        if (not market_state(checked)['is_open'] or not self._may_write() or
                self._get(db, 'blocks', []) or validate_quote(quote, checked, settings)):
            return {**position, 'intraday_status': 'blocked', 'intraday_reason': 'intraday_quote_unavailable'}
        test_order = {'symbol': symbol, 'side': 'SELL', 'quantity': '1',
                      'limit_price': str(limit_price_for('SELL', quote, settings))}
        try:
            broker_quote = self.adapter.fresh_quote(symbol)
        except Exception:
            broker_quote = None
        checked = reference_now + timedelta(seconds=time.monotonic() - started)
        report = cross_quote_check(test_order, quote, broker_quote, checked, settings)
        if (report['status'] != 'PASSED' or not market_state(checked)['is_open'] or
                not self._may_write() or self._get(db, 'blocks', [])):
            return {**position, 'intraday_status': 'blocked', 'intraday_reason': 'intraday_quote_unconfirmed',
                    'intraday_quote_check': report}
        # The lower of the two sellable bids cannot manufacture a profitable
        # target from the more optimistic source. A loss trigger is conservative.
        bid = min(D(quote['bid']), D(broker_quote['bid_price']))
        decision_frame = frame
        if (not isinstance(packet, dict) or packet.get('strategy_id') != reversal.STRATEGY_ID
                or packet.get('quality') not in ('ready', 'valid', 'expired')
                or packet.get('config_hash') != config.get('strategy_config_hash')
                or (config.get('scope_mode') == live_policy.MODE
                    and packet.get('metadata_hash') != config.get('universe_hash'))):
            # A prior valid daily checkpoint does not authenticate this packet's
            # peer basket. Preserve independently verified cash-price protection
            # but never combine that checkpoint with another policy/universe.
            decision_frame = {**frame, 'intraday_context': {
                **contexts, symbol: {**context, 'relative_eligible': False,
                                    'reason': 'intraday_packet_identity_mismatch'}}}
        from . import intraday_reversal
        updated = intraday_reversal.update_position(
            position, packet, decision_frame, checked, confirmed_bid=bid)
        updated['intraday_quote_check'] = report
        if updated.get('exit_latched') and not position.get('exit_latched'):
            self._audit(db, 'INTRADAY_EXIT_TRIGGERED', {
                'symbol': symbol, 'reason': updated.get('exit_reason'),
                'evidence': updated.get('intraday_evidence', {}), 'quote_check': report}, checked)
        return updated

    def _scan_result(self, db, status, now, sent=0, cancellations=0):
        result = {'status': status, 'checked_at': stamp(now), 'orders_sent': sent, 'cancel_requests': cancellations,
                  'blockers': self._get(db, 'blocks', [])}
        self._put(db, 'last_scan', result)
        self._audit(db, 'SCAN_COMPLETE', result, now)
        db.commit()
        return result

    def _record_quote_check(self, db, intent, report, now):
        intent['quote_check'] = report
        self._put(db, 'last_quote_check', report)
        self._intent(db, intent)
        self._audit(db, 'EXECUTION_QUOTE_CHECK', {'intent_id': intent['id'], **report}, now)
        db.commit()

    def _abandon_unsent(self, db, intent, reason, now, *, status='ABANDONED'):
        """Release only an instruction proven never to have reached the write call.

        Archive its deduplication key atomically, retaining the evidence/ref_id.
        A later scan must create and validate a new intent. Unknown submissions
        never reach this helper and continue to own their original signal key.
        """
        original = intent['signal_key']
        if status not in {'ABANDONED', 'PREVIEW_BLOCKED'}:
            raise ValueError('Invalid unsent terminal state')
        intent.update(status=status, reserved_cash='0', reserved_quantity='0',
                      original_signal_key=original, unsent_reason=reason,
                      signal_key=f"{original}:unsent:{intent['id']}")
        db.execute('UPDATE live_intents SET signal_key=? WHERE id=?', (intent['signal_key'], intent['id']))
        self._intent(db, intent)
        self._audit(db, reason, {'intent_id': intent['id'], 'never_submitted': True}, now)
        db.commit()
        return False

    def _execute(self, db, config, params, request, signal_key, evidence, now, frame):
        execution_started = time.monotonic()
        if db.execute('SELECT 1 FROM live_intents WHERE signal_key=?', (signal_key,)).fetchone():
            return False
        order = self._canonical_order(request, config['account_number'])
        intent = {**order, 'id': str(uuid.uuid4()), 'signal_key': signal_key, 'created_at': stamp(now),
                  'expires_at': stamp(now + timedelta(seconds=int(params['order_ttl_seconds']))),
                  'status': 'PREPARED', 'filled_quantity': '0', 'broker_order_id': None, 'approval_id': None,
                  'reserved_cash': str(D(order['quantity']) * D(order['limit_price']) *
                                       (1 + D(params['fee_bps']) / 10000)) if order['side'] == 'BUY' else '0',
                  'reserved_quantity': order['quantity'] if order['side'] == 'SELL' else '0',
                  'configuration_revision': config['revision'], 'strategy_id': evidence.get('strategy_id', config.get('strategy_id', reversal.TREND_ID)),
                  'policy_hash': config.get('policy_hash'), 'policy_version': config.get('policy_version'),
                  'params': params, 'evidence': _json(evidence)}
        if intent['strategy_id'] == reversal.STRATEGY_ID and order['side'] == 'BUY':
            window_end = session_bounds(evidence['execution_session'])[0] + timedelta(minutes=10)
            intent['expires_at'] = stamp(min(utc(intent['expires_at']), window_end))
        self._intent(db, intent)
        self._audit(db, 'INTENT_PERSISTED', {'intent_id': intent['id'], 'ref_id': intent['ref_id'], 'order': _public(order)}, now)
        db.commit()
        try:
            preview = self.adapter.preview(order)
        except Exception:
            preview = {'status': 'BLOCKED'}
        if preview.get('status') != 'READY':
            execution_now = now + timedelta(seconds=time.monotonic() - execution_started)
            return self._abandon_unsent(db, intent, 'EXECUTION_PREVIEW_BLOCKED', execution_now,
                                        status='PREVIEW_BLOCKED')
        intent['status'] = 'PREVIEWED'
        self._intent(db, intent)
        self._audit(db, 'EXECUTION_PREVIEW_READY', {'intent_id': intent['id']}, now)
        db.commit()
        # A preview can take long enough to invalidate quotes, signal, and session.
        # Use elapsed monotonic time so neither wall-clock corrections nor historical
        # test timestamps can bypass the time consumed by a real preview request.
        execution_now = now + timedelta(seconds=time.monotonic() - execution_started)
        if (not self._may_write() or self._get(db, 'blocks', []) or
                not market_state(execution_now)['is_open'] or execution_now >= utc(intent['expires_at']) or
                not 0 <= (execution_now - utc(self._get(db, 'mirror')['checked_at'])).total_seconds() <= 60 or
                validate_quote(frame.get('quotes', {}).get(order['symbol']), execution_now, params)):
            return self._abandon_unsent(db, intent, 'PRE_SUBMIT_GATE_BLOCKED', execution_now)
        # The preview's quote is not an execution quote. Fetch a fresh, exact
        # symbol book after preview, then compare both sources without repricing.
        read_failed = False
        try:
            robinhood_quote = self.adapter.fresh_quote(order['symbol'])
        except Exception:
            robinhood_quote = None
            read_failed = True
        execution_now = now + timedelta(seconds=time.monotonic() - execution_started)
        cross_inputs = deepcopy({'order': order, 'alpaca_quote': frame['quotes'][order['symbol']],
                        'robinhood_quote': robinhood_quote, 'params': params,
                        'signal': {'price': evidence.get('signal_price'),
                                   'bar_time': evidence.get('signal_bar_time')}})
        report = cross_quote_check(**cross_inputs, now=execution_now)
        if read_failed:
            report['status'] = 'BLOCKED'
            report['reasons'].append('ROBINHOOD_QUOTE_UNAVAILABLE')
        self._record_quote_check(db, intent, report, execution_now)
        if report['status'] != 'PASSED':
            return self._abandon_unsent(db, intent, 'CROSS_QUOTE_BLOCKED', execution_now)
        # Reads also consume time and may overlap a stop request or market close.
        self._write_guard = {'now': now, 'started': execution_started, 'expires_at': intent['expires_at'],
                             'mirror_at': self._get(db, 'mirror')['checked_at'],
                             'policy_hash': intent.get('policy_hash'), 'strategy_id': intent['strategy_id'],
                             'quote': cross_inputs['alpaca_quote'], 'params': cross_inputs['params'],
                             'cross_quote': cross_inputs, 'quote_check': report}
        if not self._may_write() or self._get(db, 'blocks', []) or not self._guard_allows():
            self._record_quote_check(db, intent, self._write_guard['quote_check'], execution_now)
            self._write_guard = None
            return self._abandon_unsent(db, intent, 'PRE_SUBMIT_GATE_BLOCKED', execution_now)
        intent['status'] = 'SUBMITTING'
        self._intent(db, intent)
        self._audit(db, 'SUBMISSION_ATTEMPT_STARTED', {'intent_id': intent['id'], 'ref_id': intent['ref_id']}, execution_now)
        db.commit()
        unsent = False
        try:
            response = self.adapter.submit(order)
        except LiveExecutionDisabled:
            # The adapter raises this only at a local gate before place_order.
            # Network/response failures inside place_order remain UNKNOWN.
            unsent = True
            response = None
        except Exception:
            response = {'status': 'UNKNOWN'}
        finally:
            final_report = self._write_guard['quote_check']
            self._write_guard = None
        execution_now = now + timedelta(seconds=time.monotonic() - execution_started)
        self._record_quote_check(db, intent, final_report, execution_now)
        if unsent:
            return self._abandon_unsent(db, intent, 'FINAL_WRITE_GATE_BLOCKED', execution_now)
        status = response.get('status', 'UNKNOWN')
        intent['status'] = status if status in ACTIVE | TERMINAL else 'UNKNOWN'
        intent['broker_order_id'] = response.get('broker_order_id')
        intent['approval_id'] = response.get('approval_id')
        if intent['status'] == 'REJECTED':
            intent.update(reserved_cash='0', reserved_quantity='0')
        elif intent['status'] == 'UNKNOWN':
            self._block(db, 'UNKNOWN_INSTRUCTION', execution_now)
        # Even a FILLED response is only provisional until full read reconciliation
        # verifies executions and resulting positions. Keep its reservation intact.
        elif intent['status'] in TERMINAL:
            intent['status'] = 'SUBMITTED'
        self._intent(db, intent)
        self._audit(db, 'SUBMISSION_RESPONSE', {'intent_id': intent['id'], 'status': intent['status'],
                                             'broker_order_id': intent['broker_order_id'], 'approval_id': intent['approval_id']}, execution_now)
        db.commit()
        return True


LiveEngine = LiveExecutionService
