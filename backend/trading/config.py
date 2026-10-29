"""Server-only configuration; no credentials are serialized to API clients."""
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('TRADING_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA / "trading.db"}')
DEMO_DATABASE_URL = os.getenv('DEMO_DATABASE_URL', f'sqlite:///{DATA / "demo.db"}')
SOURCE = os.getenv('MARKET_SOURCE', 'demo')
SCAN_INTERVAL = max(5, int(os.getenv('SCAN_INTERVAL_SECONDS', '30')))
