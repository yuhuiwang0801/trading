#!/usr/bin/env python3
"""Download an immutable read-only exploratory snapshot; never submits orders."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from trading import config  # Load credentials server-side without printing them.
from trading.reversal_data import build_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2')
    parser.add_argument('--sample-size', type=int, default=200)
    args = parser.parse_args()
    if not 20 <= args.sample_size <= 500:
        parser.error('sample-size must be between 20 and 500')
    manifest = build_snapshot(args.output, args.sample_size)
    print(json.dumps({k: v for k, v in manifest.items() if k != 'files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
