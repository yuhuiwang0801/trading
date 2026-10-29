from __future__ import annotations
from decimal import Decimal
from sqlalchemy import String, Integer, Boolean, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, Money, stamp

class SchemaVersion(Base):
    __tablename__ = 'schema_versions'
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String, default=stamp)

class Account(Base):
    __tablename__ = 'accounts'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, default='paper')
    cash: Mapped[Decimal] = mapped_column(Money, default=Decimal('10000'))
    settled_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal('10000'))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal('0'))
    initial_equity: Mapped[Decimal] = mapped_column(Money, default=Decimal('10000'))
    currency: Mapped[str] = mapped_column(String, default='USD')

class Settings(Base):
    __tablename__ = 'settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String, default='paper')
    paused_entries: Mapped[bool] = mapped_column(Boolean, default=False)
    stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciliation_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    failures: Mapped[int] = mapped_column(Integer, default=0)

class Candidate(Base):
    __tablename__ = 'candidates'
    __table_args__ = (UniqueConstraint('session_date','symbol', name='uq_candidate_session_symbol'),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_date: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String)
    security_id: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=stamp)

class Signal(Base):
    __tablename__ = 'signals'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default='NEW')
    reason: Mapped[str] = mapped_column(String)
    risk_result: Mapped[str] = mapped_column(String, default='PENDING')
    run_id: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    expires_at: Mapped[str] = mapped_column(String)
    evidence: Mapped[dict] = mapped_column(JSON)

class OrderIntent(Base):
    __tablename__ = 'order_intents'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, unique=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey('signals.id'))
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[Decimal] = mapped_column(Money)
    limit_price: Mapped[Decimal] = mapped_column(Money)
    reserved_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal('0'))
    reserved_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal('0'))
    status: Mapped[str] = mapped_column(String, default='PENDING_SUBMIT')
    mode: Mapped[str] = mapped_column(String)
    strategy: Mapped[str] = mapped_column(String, default='trend_breakout')
    version: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    data: Mapped[dict] = mapped_column(JSON, default=dict)

class BrokerOrder(Base):
    __tablename__ = 'broker_orders'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey('order_intents.id'), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    broker_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    client_order_id: Mapped[str] = mapped_column(String, unique=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[Decimal] = mapped_column(Money)
    filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal('0'))
    limit_price: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String)
    owner: Mapped[str] = mapped_column(String, default='system')
    mode: Mapped[str] = mapped_column(String)
    submitted_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    cancel_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_match_at: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(String, default='')

class Fill(Base):
    __tablename__ = 'fills'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    broker_fill_id: Mapped[str] = mapped_column(String, unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey('broker_orders.id'))
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money)
    timestamp: Mapped[str] = mapped_column(String)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal('0'))

class Position(Base):
    __tablename__ = 'positions'
    __table_args__ = (UniqueConstraint('account_id','symbol', name='uq_account_position'),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    symbol: Mapped[str] = mapped_column(String)
    security_id: Mapped[str] = mapped_column(String, default='')
    quantity: Mapped[Decimal] = mapped_column(Money)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    last_price: Mapped[Decimal] = mapped_column(Money)
    entry_atr: Mapped[Decimal] = mapped_column(Money)
    ma20: Mapped[Decimal] = mapped_column(Money)
    highest_close: Mapped[Decimal] = mapped_column(Money)
    protection: Mapped[Decimal] = mapped_column(Money)
    managed: Mapped[bool] = mapped_column(Boolean, default=True)
    strategy: Mapped[str] = mapped_column(String, default='trend_breakout')
    opened_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    exit_status: Mapped[str] = mapped_column(String, default='MONITORING')
    last_bar_at: Mapped[str | None] = mapped_column(String, nullable=True)

class Settlement(Base):
    __tablename__ = 'settlements'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    fill_id: Mapped[str] = mapped_column(ForeignKey('fills.id'), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    amount: Mapped[Decimal] = mapped_column(Money)
    settle_date: Mapped[str] = mapped_column(String)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[str] = mapped_column(String, default=stamp, index=True)
    category: Mapped[str] = mapped_column(String)
    event: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String, default='system')
    account_id: Mapped[str] = mapped_column(String, default='paper-usd')
    symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    intent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    coverage: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)
    data_source: Mapped[str] = mapped_column(String)
    quality: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String, default='')

class Lease(Base):
    __tablename__ = 'leases'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)

class SessionEquity(Base):
    __tablename__ = 'session_equity'
    session_date: Mapped[str] = mapped_column(String, primary_key=True)
    opening_equity: Mapped[Decimal] = mapped_column(Money)


# Corrections are appended as separate events; application code cannot rewrite history.
from sqlalchemy import event

@event.listens_for(Audit, 'before_update')
@event.listens_for(Audit, 'before_delete')
def audit_is_append_only(mapper, connection, target):
    raise ValueError('Audit events are append-only; append a correction instead')
