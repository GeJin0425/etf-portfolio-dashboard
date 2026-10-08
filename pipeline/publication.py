"""Check the public Pages artifact without changing the portfolio or repository."""

import argparse
import sys

import requests

from .export import validate_payload_freshness
from .freshness import utc_now

URL = 'https://gejin0425.github.io/etf-portfolio-dashboard/data.json'


def read_public(now=None):
    now = utc_now(now)
    response = requests.get(URL, params={'checked_at': int(now.timestamp())},
                            headers={'Cache-Control': 'no-cache'}, timeout=20)
    response.raise_for_status()
    return response.json()


def check_live(now=None):
    now = utc_now(now)
    data = read_public(now)
    validate_payload_freshness(data, now=now)
    missing = [code for code in ('000300', 'SPX', 'NDX')
               if data['meta']['source_dates'][code]['status'] != 'available']
    if missing:
        print(f'::warning::Comparison data incomplete: {", ".join(missing)}')
    print(f'Public portfolio is current through {data["meta"]["as_of_date"]}; '
          f'commit {data["meta"].get("build_commit")}')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-live', action='store_true', required=True)
    parser.parse_args()
    try:
        check_live()
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f'::error::Public dashboard is stale or unavailable: {exc}', file=sys.stderr)
        sys.exit(1)
