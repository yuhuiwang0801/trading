#!/usr/bin/env python3
"""Refresh immutable V2 daily signals via read-only SIP/SEC requests."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from trading.reversal_runtime import build_runtime_packet, runtime_packet_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--status-only', action='store_true')
    args = parser.parse_args()
    packet = (runtime_packet_status(data_dir=args.data_dir) if args.status_only else
              build_runtime_packet(data_dir=args.data_dir))
    print(json.dumps({k: packet.get(k) for k in ['status', 'quality', 'signal_session', 'execution_session',
          'created_at', 'packet_id', 'blocked_reasons', 'candidates', 'execution_eligible']}, indent=2))
    return 0 if packet.get('status') == 'ready' else 2


if __name__ == '__main__':
    raise SystemExit(main())
