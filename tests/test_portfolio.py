import pandas as pd

from pipeline.portfolio import run_portfolio


def _closes(dates, base, drift):
    return pd.Series(
        [base * (1 + drift * i) for i in range(len(dates))],
        index=pd.DatetimeIndex(dates),
    )


def test_initial_allocation_and_quarterly_rebalance():
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes = {
        'A': _closes(dates, 2.0, 0.001),
        'B': _closes(dates, 5.0, -0.0002),
    }
    res = run_portfolio(
        closes,
        [('A', 0.6), ('B', 0.4)],
        start='2026-01-05',
        initial=100000,
        comm=0.00005,
        min_comm=0.5,
    )
    eq = res['equity']
    assert eq.index[0] == pd.Timestamp('2026-01-05')
    assert len(res['rebalances']) == 2  # 2026-03-31 与 2026-06-30
    assert res['cash'] >= 0
    assert res['fees_paid'] > 0
    assert eq.iloc[-1] > 10000

    # 每次再平衡后权重应回到目标附近
    for rb in res['rebalances']:
        wa = {w['code']: w['pct'] for w in rb['weights_after']}
        assert abs(wa['A'] - 60) < 1
        assert abs(wa['B'] - 40) < 1


def test_rebalance_uses_last_trading_day_of_quarter():
    # 3月31日不是交易日时, 应使用3月最后一个交易日
    dates = [d for d in pd.date_range('2026-01-05', '2026-05-31', freq='B') if d != pd.Timestamp('2026-03-31')]
    closes = {
        'A': _closes(dates, 2.0, 0.0005),
        'B': _closes(dates, 5.0, 0.0003),
    }
    res = run_portfolio(
        closes,
        [('A', 0.5), ('B', 0.5)],
        start='2026-01-05',
        initial=10000,
        comm=0.0005,
        min_comm=0.5,
    )
    assert len(res['rebalances']) == 1
    assert res['rebalances'][0]['date'] == '2026-03-30'


def test_rebalances_continue_past_hardcoded_horizon():
    """回归: _quarter_ends 曾经硬编码 end_year=2027, 组合运行到 2028 年后
    会因为找不到未来季末候选日期而静默停止再平衡。"""
    dates = pd.date_range('2026-01-05', '2028-06-30', freq='B')
    closes = {
        'A': _closes(dates, 2.0, 0.0002),
        'B': _closes(dates, 5.0, -0.0001),
    }
    res = run_portfolio(
        closes,
        [('A', 0.6), ('B', 0.4)],
        start='2026-01-05',
        initial=100000,
    )
    rb_years = {pd.Timestamp(rb['date']).year for rb in res['rebalances']}
    assert 2028 in rb_years


def test_weights_must_sum_to_one():
    dates = pd.date_range('2026-01-05', '2026-02-05', freq='B')
    closes = {'A': _closes(dates, 2.0, 0.0), 'B': _closes(dates, 5.0, 0.0)}
    try:
        run_portfolio(closes, [('A', 0.5), ('B', 0.6)], start='2026-01-05')
    except ValueError:
        return
    raise AssertionError('权重和不为1时应报错')
