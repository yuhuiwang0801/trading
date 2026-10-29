#!/usr/bin/env python3
"""Acquire SEC filing-time fundamentals for reversal V2; no broker/account calls."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import exchange_calendars as xcals
from trading.reversal_fundamentals import download_sec_fundamentals, build_fundamental_features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/reversal/fundamentals_sec_20261027_v2')
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    if not args.build_only:
        download_sec_fundamentals(args.snapshot, args.output, user_agent=os.getenv('SEC_USER_AGENT'))
    if not args.download_only:
        calendar = xcals.get_calendar('XNYS', start='2015-01-01', end='2026-11-02')
        sessions = calendar.sessions_in_range('2016-01-01', '2026-10-26').strftime('%Y-%m-%d').tolist()
        print(json.dumps(build_fundamental_features(args.snapshot, args.output, sessions), indent=2))


if __name__ == '__main__':
    main()
