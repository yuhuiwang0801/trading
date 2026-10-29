"""NYSE regular sessions. Every public timestamp is timezone-aware UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

UTC = timezone.utc
NEW_YORK = ZoneInfo('America/New_York')


def as_utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return value.astimezone(UTC)


def iso(value: datetime | pd.Timestamp) -> str:
    return value.isoformat().replace('+00:00', 'Z')


@lru_cache(maxsize=8)
def calendar_for_year(year: int):
    # Calendar bounds are explicit, so the library's rolling default never drops
    # old replay sessions. XNYS encodes DST, holidays and scheduled early closes.
    return xcals.get_calendar('XNYS', start=f'{year-2}-01-01', end=f'{year+2}-12-31')


def session_bounds(session_date: str) -> tuple[datetime, datetime] | None:
    day = pd.Timestamp(session_date)
    cal = calendar_for_year(day.year)
    if not cal.is_session(day):
        return None
    return cal.session_open(day).to_pydatetime(), cal.session_close(day).to_pydatetime()


def previous_session(session_date: str) -> str:
    day = pd.Timestamp(session_date)
    cal = calendar_for_year(day.year)
    previous = cal.date_to_session(day - pd.Timedelta(days=1), direction='previous')
    return previous.date().isoformat()


def market_state(now: datetime | str | None = None) -> dict:
    now = as_utc(now)
    local = now.astimezone(NEW_YORK)
    day = pd.Timestamp(local.date())
    cal = calendar_for_year(local.year)
    bounds = session_bounds(local.date().isoformat())
    opened, closed = bounds if bounds else (None, None)
    is_open = bool(opened and opened <= now < closed)
    phase = 'closed'
    if is_open:
        phase = 'regular'
    elif bounds and local.hour >= 4 and now < opened:
        phase = 'pre_market'
    elif bounds and now >= closed and local.hour < 20:
        phase = 'post_market'
    next_day = day if bounds and now < opened else day + pd.Timedelta(days=1)
    next_session = cal.date_to_session(next_day, direction='next')
    next_open = cal.session_open(next_session)
    return {
        'phase': phase, 'is_open': is_open,
        'session_date': local.date().isoformat(),
        'open': iso(opened) if opened else None,
        'close': iso(closed) if closed else None,
        'next_open': iso(next_open), 'timezone': 'America/New_York',
        'calendar': 'XNYS', 'as_of': iso(now),
        'early_close': bool(bounds and (closed - opened) < timedelta(hours=6, minutes=30)),
    }


# Convenient name for integrations.
get_market_state = market_state
