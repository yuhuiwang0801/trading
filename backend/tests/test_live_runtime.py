"""UI activation uses the shared ledger; legacy env values grant no permission."""
import pytest

from trading.live_execution import ARM_CONFIRMATION
from trading.live_runtime import create_live_service, live_control_identity
from trading.mcp_broker import LiveExecutionDisabled
from test_broker_connection_api import FakeOAuth
from test_robinhood_adapter import Client, intent


@pytest.mark.parametrize('legacy_flag', [None, 'false', 'true'])
def test_factory_defaults_closed_and_cross_process_stop_is_immediate(tmp_path, monkeypatch, legacy_flag):
    if legacy_flag is None:
        monkeypatch.delenv('ROBINHOOD_LIVE_ENABLED', raising=False)
    else:
        monkeypatch.setenv('ROBINHOOD_LIVE_ENABLED', legacy_flag)
    clients = [Client(), Client()]
    for client in clients:
        client.data['get_equity_orders']['orders'] = [row for row in client.data['get_equity_orders']['orders']
                                                    if row['state'] == 'filled']
    database = 'sqlite:///'+str(tmp_path/'main.db')
    api, worker = [create_live_service(database, client=client, oauth=FakeOAuth()) for client in clients]
    assert live_control_identity(api) == live_control_identity(worker)
    assert not api._may_write() and not worker._may_write()
    with pytest.raises(LiveExecutionDisabled): worker.adapter.submit(intent())
    assert clients[1].calls == []
    api.prepare({'symbols':['AAPL'],'max_order_amount':'110','max_daily_added':'110','max_positions':3})
    assert not worker._may_write()  # Preparation reads and scope never grant permission.
    assert all(name.startswith('get_') for name, _ in clients[0].calls)
    api.arm(ARM_CONFIRMATION, 1)
    assert worker._may_write()  # No worker restart or environment mutation.
    with pytest.raises(LiveExecutionDisabled): worker.adapter.submit(intent())  # No per-order guard.
    assert clients[1].calls == []
    api.disarm()
    assert not worker._may_write()
    with pytest.raises(LiveExecutionDisabled): worker.adapter.submit(intent())
    assert clients[1].calls == []
    assert not worker.snapshot()['intents']

