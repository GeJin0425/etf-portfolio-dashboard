"""Read executed 510880 trades from its published production strategy output."""

from datetime import date

import pandas as pd
import requests

URL = 'https://gejin0425.github.io/510880-dividend-strategy/data.json'
SWITCH_DATE = '2026-09-30'


def parse_feed(payload, as_of_date):
    meta = payload['meta']
    if meta['as_of_date'] != as_of_date:
        raise ValueError(f'510880 strategy is stale: {meta["as_of_date"]} != {as_of_date}')
    if meta['execution_mode'] != 'next_open':
        raise ValueError('510880 strategy execution mode changed')
    if not meta['strategy_version'].startswith('flow_z20_on_b2_b3__'):
        raise ValueError('510880 strategy rules changed; review before following')

    switch = date.fromisoformat(SWITCH_DATE)
    as_of = date.fromisoformat(as_of_date)
    fills = []
    for trade in payload['trades']:
        buy_date = date.fromisoformat(trade['buy_date'])
        signal_date = date.fromisoformat(trade['buy_signal_date'])
        if not signal_date < buy_date <= as_of:
            raise ValueError('510880 buy fill has invalid dates')
        fills.append({'date': trade['buy_date'], 'signal_date': trade['buy_signal_date'],
                      'action': 'BUY', 'price': float(trade['buy_price']),
                      'reason': trade.get('buy_level', '')})
        if trade.get('sell_date'):
            sell_date = date.fromisoformat(trade['sell_date'])
            signal_date = date.fromisoformat(trade['sell_signal_date'])
            if not buy_date < sell_date <= as_of or not signal_date < sell_date:
                raise ValueError('510880 sell fill has invalid dates')
            fills.append({'date': trade['sell_date'], 'signal_date': trade['sell_signal_date'],
                          'action': 'SELL', 'price': float(trade['sell_price']),
                          'reason': trade.get('sell_reason', '')})
    fills.sort(key=lambda x: x['date'])
    holding = False
    switch_asset = '511260'
    for fill in fills:
        if (fill['action'] == 'BUY') == holding or fill['price'] <= 0:
            raise ValueError('510880 strategy fills are inconsistent')
        holding = fill['action'] == 'BUY'
        if date.fromisoformat(fill['date']) <= switch:
            switch_asset = '510880' if holding else '511260'
    expected_asset = '510880' if holding else '511260'
    if payload['current_status']['position_asset'] != expected_asset:
        raise ValueError('510880 strategy position disagrees with executed fills')
    series = payload['series']
    closes = pd.Series(series['close'], index=pd.DatetimeIndex(series['dates']), dtype=float)
    if closes.empty or closes.index[-1].strftime('%Y-%m-%d') != as_of_date:
        raise ValueError('510880 strategy close series is incomplete')
    return {
        'as_of_date': as_of_date,
        'version': meta['strategy_version'],
        'switch_asset': switch_asset,
        'closes': closes,
        'fills': [f for f in fills if date.fromisoformat(f['date']) > switch],
        'pending_signal': next((s for s in reversed(payload['signals'])
                                if s['date'] == as_of_date and s['pending']), None),
    }


def fetch_feed(as_of_date):
    response = requests.get(URL, timeout=25)
    response.raise_for_status()
    return parse_feed(response.json(), as_of_date)
