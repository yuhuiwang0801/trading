"""Shared API/worker construction for the isolated Robinhood execution ledger."""
import hashlib
import os
from pathlib import Path
from sqlalchemy.engine import make_url
from . import config
from .mcp_broker import MCPClient, TokenProvider
from .robinhood_oauth import RobinhoodOAuth


LIVE_CONTROL_VERSION = 6


def live_control_identity(service):
    """Identify the shared ledger without exposing a local filesystem path."""
    return hashlib.sha256(str(service.path.resolve()).encode()).hexdigest()[:24]


def live_ledger_path(database_url):
    configured = os.getenv('ROBINHOOD_LIVE_LEDGER')
    if configured and database_url == config.DATABASE_URL:
        return str(Path(configured).expanduser().resolve())
    url = make_url(database_url)
    if url.drivername.startswith('sqlite') and url.database and url.database != ':memory:':
        source = Path(url.database).resolve()
        return str(source.with_name(source.stem + '.robinhood.sqlite3'))
    digest = hashlib.sha256(database_url.encode()).hexdigest()[:16]
    return str(config.DATA / ('robinhood-' + digest + '.sqlite3'))


def create_live_service(database_url, *, client=None, oauth=None):
    from .robinhood_adapter import RobinhoodTradingAdapter
    from .live_execution import LiveExecutionService
    oauth = oauth or RobinhoodOAuth()
    client = client or MCPClient(token_provider=TokenProvider(oauth, allow_legacy=False))
    # UI confirmation grants the durable arm permission. This capability does
    # not itself grant broker writes: the service wraps the adapter with armed,
    # stop-generation and per-instruction checks. Startup revokes the arm state.
    # The legacy ROBINHOOD_LIVE_ENABLED environment variable is no longer read.
    adapter = RobinhoodTradingAdapter(client, write_enabled=True)
    return LiveExecutionService(live_ledger_path(database_url), adapter, write_enabled=True)
