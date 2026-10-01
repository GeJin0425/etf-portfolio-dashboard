from datetime import date, datetime

import pandas as pd
import pytest

from pipeline.fetch import ASSETS, BENCHMARKS
from pipeline.freshness import FreshnessError, latest_completed_session, validate_series


@pytest.mark.parametrize('market,now,expected', [
    ('XSHG', '2026-10-01T01:00:00+00:00', '2026-09-30'),
    ('XSHE', '2026-10-07T07:40:00+00:00', '2026-09-30'),
    ('XSHG', '2026-10-08T06:59:59+00:00', '2026-09-30'),
    ('XSHG', '2026-10-08T07:00:00+00:00', '2026-10-08'),
    ('XSHE', '2026-10-10T08:00:00+00:00', '2026-10-09'),
    ('XSHG', '2026-02-23T08:00:00+00:00', '2026-02-13'),
    ('XSHE', '2026-02-24T07:00:00+00:00', '2026-02-24'),
    ('XSHG', '2026-01-01T00:00:00+00:00', '2025-12-31'),
    ('US', '2026-01-01T00:00:00+00:00', '2025-12-31'),
    ('US', '2026-10-01T01:00:00+00:00', '2026-09-30'),
    ('US', '2026-09-30T19:59:59+00:00', '2026-09-29'),
    ('US', '2026-09-30T20:00:00+00:00', '2026-09-30'),
    ('US', '2026-01-05T20:59:59+00:00', '2026-01-02'),
    ('US', '2026-01-05T21:00:00+00:00', '2026-01-05'),
    ('US', '2026-03-09T19:59:59+00:00', '2026-03-06'),
    ('US', '2026-03-09T20:00:00+00:00', '2026-03-09'),
    ('US', '2026-07-02T17:00:00+00:00', '2026-07-01'),
    ('US', '2026-07-03T22:00:00+00:00', '2026-07-02'),
    ('US', '2026-07-04T22:00:00+00:00', '2026-07-02'),
    ('US', '2026-11-27T17:59:59+00:00', '2026-11-25'),
    ('US', '2026-11-27T18:00:00+00:00', '2026-11-27'),
    ('US', '2026-12-24T18:00:00+00:00', '2026-12-24'),
    ('US', '2025-01-09T22:00:00+00:00', '2025-01-08'),
    ('US', '2025-07-03T17:00:00+00:00', '2025-07-03'),
])
def test_official_session_boundaries(market, now, expected):
    assert latest_completed_session(market, datetime.fromisoformat(now)) == date.fromisoformat(expected)


@pytest.mark.parametrize('market', ['XSHG', 'XSHE', 'US'])
def test_unsupported_calendar_year_fails_closed(market):
    with pytest.raises(FreshnessError, match='2027 is not configured'):
        latest_completed_session(market, datetime.fromisoformat('2027-01-08T22:00:00+00:00'))


def test_clock_requires_timezone_and_market_requires_explicit_mapping():
    with pytest.raises(FreshnessError, match='timezone-aware'):
        latest_completed_session('XSHG', datetime(2026, 10, 1))
    with pytest.raises(FreshnessError, match='Unknown quote market'):
        latest_completed_session('UNKNOWN')


def test_mainland_overseas_lofs_use_their_listing_exchange():
    assert {a['code']: a['market'] for a in ASSETS} == {
        '159263': 'XSHE', '161130': 'XSHE', '161125': 'XSHE', '518850': 'XSHG',
    }
    assert {b['code']: b['market'] for b in BENCHMARKS} == {
        '000300': 'XSHG', 'SPX': 'US', 'NDX': 'US',
    }


def test_old_mainland_date_is_fresh_through_national_day_holiday():
    series = pd.Series([1.0], index=pd.DatetimeIndex(['2026-09-30']))
    now = datetime.fromisoformat('2026-10-07T22:00:00+00:00')
    metadata = validate_series(series, ASSETS[1], now)
    assert metadata == {'actual': '2026-09-30', 'expected': '2026-09-30', 'market': 'XSHE'}
    with pytest.raises(FreshnessError, match='stale'):
        validate_series(series, ASSETS[1], datetime.fromisoformat('2026-10-08T07:40:00+00:00'))


@pytest.mark.parametrize('dates', [
    ['2026-09-30', '2026-09-29'], ['2026-09-30', '2026-09-30'],
    ['2026-09-30T12:00:00'], ['2026-09-30', None],
])
def test_invalid_daily_quote_indexes_are_rejected(dates):
    series = pd.Series([1.0] * len(dates), index=pd.DatetimeIndex(dates))
    with pytest.raises(FreshnessError, match='invalid daily quote date index'):
        validate_series(series, ASSETS[0], datetime.fromisoformat('2026-10-01T01:00:00+00:00'))


@pytest.mark.parametrize('price', [float('nan'), float('inf'), 0, -1])
def test_invalid_prices_are_rejected(price):
    series = pd.Series([price], index=pd.DatetimeIndex(['2026-09-30']))
    with pytest.raises(FreshnessError, match='invalid close prices'):
        validate_series(series, ASSETS[0], datetime.fromisoformat('2026-10-01T01:00:00+00:00'))
