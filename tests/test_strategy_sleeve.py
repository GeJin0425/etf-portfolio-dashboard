import pandas as pd
import pytest

from pipeline.portfolio import run_portfolio
from pipeline.strategy_feed import parse_feed


def test_feed_uses_executed_fills_and_rejects_stale_output():
    payload = {
        'meta': {'as_of_date': '2026-10-08', 'execution_mode': 'next_open',
                 'strategy_version': 'flow_z20_on_b2_b3__next_open_candidate_a'},
        'trades': [
            {'buy_signal_date': '2026-09-28', 'buy_date': '2026-09-29',
             'buy_price': 3.0, 'buy_level': 'b1',
             'sell_signal_date': '2026-09-29', 'sell_date': '2026-09-30',
             'sell_price': 3.2, 'sell_reason': 'RSI确认'},
            {'buy_signal_date': '2026-09-30', 'buy_date': '2026-10-08',
             'buy_price': 3.1, 'buy_level': 'b2'},
        ],
        'current_status': {'position_asset': '510880'},
        'signals': [{'date': '2026-09-30', 'action': 'BUY', 'pending': False}],
        'series': {'dates': ['2026-09-30', '2026-10-08'], 'close': [3.0, 3.1]},
    }
    feed = parse_feed(payload, '2026-10-08')
    assert feed['switch_asset'] == '511260'
    assert [(f['date'], f['action']) for f in feed['fills']] == [('2026-10-08', 'BUY')]
    with pytest.raises(ValueError, match='stale'):
        parse_feed(payload, '2026-10-09')


def test_quarter_end_switch_and_next_open_round_trip_stays_in_sleeve():
    dates = pd.DatetimeIndex(['2026-09-28', '2026-09-29', '2026-09-30',
                              '2026-10-08', '2026-10-09'])
    closes = {
        '159263': pd.Series(1.0, index=dates),
        '161130': pd.Series(10.0, index=dates),
        '161125': pd.Series(10.0, index=dates),
        '518850': pd.Series(10.0, index=dates),
        '510880': pd.Series(3.0, index=dates),
        '511260': pd.Series(100.0, index=dates),
    }
    strategy = {'switch_asset': '511260', 'fills': [
        {'date': '2026-10-08', 'signal_date': '2026-09-30', 'action': 'BUY',
         'price': 3.0, 'reason': 'b1'},
        {'date': '2026-10-09', 'signal_date': '2026-10-08', 'action': 'SELL',
         'price': 3.0, 'reason': 'RSI确认'},
    ]}
    result = run_portfolio(
        closes, [('159263', .38), ('161130', .28), ('161125', .22), ('518850', .12)],
        start='2026-09-28', strategy=strategy,
        bond_opens=pd.Series(100.0, index=dates),
    )
    assert result['rebalances'][0]['date'] == '2026-09-30'
    assert result['shares']['159263'] == 0
    assert result['sleeve_asset'] == '511260'
    assert result['shares']['511260'] > 0
    assert [t['code'] for t in result['strategy_trades']] == [
        '511260', '510880', '510880', '511260',
    ]
    assert result['strategy_trades'][1]['shares'] >= 12000  # includes 38% sleeve's bond-lot cash
    assert result['cash'] >= 0
    assert result['equity'].loc['2026-09-30'] > 99000
