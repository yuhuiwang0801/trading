"""Durable storage. Monetary values are serialized exactly, including on SQLite."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator, Text

class Base(DeclarativeBase):
    pass

class Money(TypeDecorator):
    impl = Text
    cache_ok = True
    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(str(value)))
    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)

def utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def stamp(value=None):
    return utc(value).isoformat(timespec='microseconds')

def database(url=None):
    url = url or os.getenv('DATABASE_URL', 'sqlite:///./data/trading.db')
    if url.startswith('sqlite:///') and ':memory:' not in url:
        path = url.removeprefix('sqlite:///')
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    kwargs = {'connect_args': {'check_same_thread': False, 'timeout': 20}} if url.startswith('sqlite') else {}
    if ':memory:' in url:
        from sqlalchemy.pool import StaticPool
        kwargs['poolclass'] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def configure_sqlite(dbapi_connection, _):
            # Explicit transactions avoid sqlite3 legacy SELECT autocommit races.
            dbapi_connection.isolation_level = None
            dbapi_connection.execute('PRAGMA foreign_keys=ON')
            dbapi_connection.execute('PRAGMA journal_mode=WAL')
            dbapi_connection.execute('PRAGMA busy_timeout=20000')
        @event.listens_for(engine, 'begin')
        def begin_sqlite(connection):
            # Serializes short local transactions, including reservation and cancel updates.
            connection.exec_driver_sql('BEGIN IMMEDIATE')
    return engine, sessionmaker(engine, expire_on_commit=False)
