"""Fail-closed daily-close freshness checks, using published exchange calendars.

Only the latest raw quote date is checked; historical warm-up data is untouched.
Maintain HOLIDAYS/EARLY_CLOSES from the linked exchange notices before each year.
Never substitute civil make-up workdays or a Monday-Friday-only calendar.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# SSE and SZSE share these full-day closures. Weekends are always closed.
# 2025: https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml
# 2026: https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
# SZSE: https://www.szse.cn/www/English/services/trading/calendar/index.html
CN_CLOSURES = {
    2025: [('01-01', '01-01'), ('01-28', '02-04'), ('04-04', '04-06'),
           ('05-01', '05-05'), ('05-31', '06-02'), ('10-01', '10-08')],
    2026: [('01-01', '01-03'), ('02-15', '02-23'), ('04-04', '04-06'),
           ('05-01', '05-05'), ('06-19', '06-21'), ('09-25', '09-27'),
           ('10-01', '10-07')],
}

# U.S. equity/index sessions (NYSE and Nasdaq), not the U.S. calendar for
# mainland-listed overseas LOFs, whose listing venues are SSE/SZSE.
# https://www.nyse.com/trade/hours-calendars
# https://nasdaqtrader.com/Trader.aspx?id=Calendar
# 2025 Jan 9 extraordinary closure: https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2024-632
US_CLOSURES = {
    2025: ['01-01', '01-09', '01-20', '02-17', '04-18', '05-26',
           '06-19', '07-04', '09-01', '11-27', '12-25'],
    2026: ['01-01', '01-19', '02-16', '04-03', '05-25', '06-19',
           '07-03', '09-07', '11-26', '12-25'],
}
US_EARLY_CLOSES = {
    2025: ['07-03', '11-28', '12-24'],
    2026: ['11-27', '12-24'],
}


class FreshnessError(ValueError):
    """A source is missing, stale, future-dated, or outside calendar coverage."""


def _calendar(market, year):
    if market in ('XSHG', 'XSHE'):
        if year not in CN_CLOSURES:
            raise FreshnessError(f'{market}: calendar year {year} is not configured')
        holidays = set()
        for first, last in CN_CLOSURES[year]:
            day = date.fromisoformat(f'{year}-{first}')
            end = date.fromisoformat(f'{year}-{last}')
            while day <= end:
                holidays.add(day)
                day += timedelta(days=1)
        return ZoneInfo('Asia/Shanghai'), holidays, set(), time(15)
    if market == 'US':
        if year not in US_CLOSURES:
            raise FreshnessError(f'{market}: calendar year {year} is not configured')
        holidays = {date.fromisoformat(f'{year}-{day}') for day in US_CLOSURES[year]}
        early = {date.fromisoformat(f'{year}-{day}') for day in US_EARLY_CLOSES[year]}
        return ZoneInfo('America/New_York'), holidays, early, time(16)
    raise FreshnessError(f'Unknown quote market: {market!r}')


def utc_now(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise FreshnessError('Freshness validation requires a timezone-aware timestamp')
    return now.astimezone(timezone.utc)


def latest_completed_session(market, now=None):
    """Latest exchange date whose official close has passed, including DST."""
    now = utc_now(now)
    zones = {'XSHG': 'Asia/Shanghai', 'XSHE': 'Asia/Shanghai', 'US': 'America/New_York'}
    if market not in zones:
        raise FreshnessError(f'Unknown quote market: {market!r}')
    zone = ZoneInfo(zones[market])
    day = now.astimezone(zone).date()
    while True:
        zone, holidays, early, regular_close = _calendar(market, day.year)
        close = time(13) if day in early else regular_close
        if (day.weekday() < 5 and day not in holidays
                and datetime.combine(day, close, tzinfo=zone) <= now):
            return day
        day -= timedelta(days=1)


def next_session(market, after_date):
    """First configured exchange session strictly after a quoted date."""
    day = after_date + timedelta(days=1)
    while True:
        _, holidays, _, _ = _calendar(market, day.year)
        if day.weekday() < 5 and day not in holidays:
            return day
        day += timedelta(days=1)


def validate_quote_date(actual, defn, now=None):
    expected = latest_completed_session(defn['market'], now)
    if actual != expected:
        state = 'stale' if actual < expected else 'future/uncompleted/non-session'
        raise FreshnessError(
            f'{defn["code"]} ({defn["market"]}): {state} quote date {actual}; '
            f'expected latest completed session {expected}'
        )
    return expected.isoformat()


def validate_series(series, defn, now=None):
    """Validate a raw source before reindex/ffill can hide its stale tail."""
    if series.empty or not isinstance(series.index, pd.DatetimeIndex):
        raise FreshnessError(f'{defn["code"]}: missing dated close prices')
    if (series.index.tz is not None or series.index.hasnans
            or not series.index.equals(series.index.normalize())
            or not series.index.is_monotonic_increasing or not series.index.is_unique):
        raise FreshnessError(f'{defn["code"]}: invalid daily quote date index')
    values = pd.to_numeric(series, errors='coerce').to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise FreshnessError(f'{defn["code"]}: invalid close prices')
    actual = series.index[-1].date()
    expected = validate_quote_date(actual, defn, now)
    return {'actual': actual.isoformat(), 'expected': expected, 'market': defn['market']}
