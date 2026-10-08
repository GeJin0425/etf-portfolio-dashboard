"""生成看板所需的 site/data.json"""

import argparse
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .fetch import (
    ASSETS,
    BENCHMARKS,
    STRATEGY_ASSETS,
    FEE_MIN,
    FEE_RATE,
    INITIAL_CAPITAL,
    LEGACY_LAST_CLOSE,
    START_DATE,
    fetch_all,
    fetch_strategy_quotes,
    load_pre_q4_snapshot,
)
from .freshness import (FreshnessError, latest_completed_session, next_session,
                        utc_now, validate_quote_date, validate_series)
from .portfolio import run_portfolio
from .strategy_feed import SWITCH_DATE, fetch_feed


def compute_stats(equity, initial):
    final = float(equity.iloc[-1])
    total_return = (final / initial - 1) * 100
    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25
    annualized = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    dd = (equity - equity.cummax()) / equity.cummax() * 100
    ret = equity.pct_change().dropna()
    vol = ret.std() * np.sqrt(252) * 100 if len(ret) > 1 else 0.0
    sharpe = ((ret.mean() * 252 - 0.02) / (ret.std() * np.sqrt(252))
              if len(ret) > 1 and ret.std() > 0 else 0.0)
    return {
        'total_return_pct': round(float(total_return), 2),
        'annualized_pct': round(float(annualized), 2),
        'max_drawdown_pct': round(float(dd.min()), 2),
        'sharpe': round(float(sharpe), 2),
        'volatility_pct': round(float(vol), 2),
    }


def next_rebalance_date(last_date):
    last = pd.Timestamp(last_date)
    for year in range(last.year, last.year + 2):
        for month in (3, 6, 9, 12):
            # 用最后一个工作日近似实际交易日(无法预知未来的交易所假期);
            # 真正触发的再平衡日以 portfolio.run_portfolio 的历史交易日为准
            end = pd.Timestamp(f'{year}-{month:02d}-01') + pd.offsets.BMonthEnd(0)
            if end > last:
                return end.strftime('%Y-%m-%d')
    return None


def ytd_return(close_series, end_date, base_date='2025-12-31'):
    """YTD to the exact benchmark session known at the China close."""
    if pd.Timestamp(base_date) not in close_series.index:
        return None
    end_stamp = pd.Timestamp(end_date)
    if end_stamp not in close_series.index:
        return None
    start = float(close_series.loc[pd.Timestamp(base_date)])
    end = float(close_series.loc[end_stamp])
    return round((end / start - 1) * 100, 2)


def china_close(day):
    return datetime.combine(pd.Timestamp(day).date(), time(15), ZoneInfo('Asia/Shanghai'))


def comparison_series(source, portfolio_dates, market):
    """Align each China 15:00 close to its last completed market session."""
    if source is None:
        return pd.Series(np.nan, index=portfolio_dates)
    required = [pd.Timestamp(latest_completed_session(market, china_close(day)))
                for day in portfolio_dates]
    # Exact lookup: missing historical sessions remain gaps, never future-filled.
    return pd.Series([source.get(day, np.nan) for day in required], index=portfolio_dates,
                     dtype=float)


def normalized_return(aligned):
    if pd.isna(aligned.iloc[0]):
        return pd.Series(np.nan, index=aligned.index)
    return (aligned / aligned.iloc[0] - 1) * 100


def at_end(series):
    return None if pd.isna(series.iloc[-1]) else round(float(series.iloc[-1]), 2)


def required_cn_dates(first, last):
    day = date.fromisoformat(first)
    end = date.fromisoformat(last)
    out = [pd.Timestamp(day)]
    while day < end:
        day = next_session('XSHE', day)
        if day <= end:
            out.append(pd.Timestamp(day))
    return pd.DatetimeIndex(out)


def validate_post_switch_history(source_series, bond_opens, as_of_date):
    """No held close or strategy open can be hidden by portfolio forward fill."""
    needed = required_cn_dates(SWITCH_DATE, as_of_date)
    for code in ('161130', '161125', '518850', '510880', '511260'):
        missing = needed.difference(source_series[code].index)
        if len(missing):
            raise FreshnessError(f'{code}: missing held close on {missing[0]:%Y-%m-%d}')
    missing_opens = needed.difference(bond_opens.index)
    if len(missing_opens):
        raise FreshnessError(f'511260: missing strategy open on {missing_opens[0]:%Y-%m-%d}')
    opens = pd.to_numeric(bond_opens.loc[needed], errors='coerce').to_numpy(dtype=float)
    invalid = ~np.isfinite(opens) | (opens <= 0)
    if invalid.any():
        raise FreshnessError(f'511260: invalid strategy open on {needed[invalid][0]:%Y-%m-%d}')


def stale_after(as_of_date):
    try:
        next_day = next_session('XSHE', date.fromisoformat(as_of_date))
    except FreshnessError as exc:
        raise FreshnessError(f'calendar_unavailable: {exc}') from exc
    return datetime.combine(next_day + timedelta(days=1), time(9),
                            ZoneInfo('Asia/Shanghai')).isoformat()


def _round_list(series, ndigits=2):
    out = []
    for v in series:
        if pd.isna(v):
            out.append(None)
            continue
        x = round(float(v), ndigits)
        out.append(0.0 if x == 0 else x)
    return out


def enrich_rebalances(rebalances):
    code_name = {a['code']: a['name'] for a in ASSETS + STRATEGY_ASSETS}
    out = []
    for rb in rebalances:
        trades = []
        for t in rb['trades']:
            t = dict(t)
            t['name'] = code_name[t['code']]
            trades.append(t)
        out.append({
            **rb,
            'trades': trades,
            'weights_before': [
                {**w, 'name': code_name[w['code']]} for w in rb['weights_before']
            ],
            'weights_after': [
                {**w, 'name': code_name[w['code']]} for w in rb['weights_after']
            ],
        })
    return out


def validate_payload_freshness(payload, *, now=None):
    """Independent pre-upload gate; valid JSON alone does not imply fresh data."""
    now = utc_now(now)
    try:
        meta = payload['meta']
        sources = meta['source_dates']
        active = meta['as_of_date'] >= SWITCH_DATE
        for defn in ASSETS + (STRATEGY_ASSETS if active else []):
            actual = date.fromisoformat(sources[defn['code']]['actual'])
            check_now = LEGACY_LAST_CLOSE if active and defn['code'] == '159263' else now
            expected = validate_quote_date(actual, defn, check_now)
            if (sources[defn['code']]['expected'] != expected
                    or sources[defn['code']]['market'] != defn['market']):
                raise FreshnessError(f'{defn["code"]}: inconsistent freshness metadata')
        cutoff = china_close(meta['as_of_date'])
        for defn in BENCHMARKS:
            code = defn['code']
            source = sources[code]
            fields = {'000300': ('csi300', 'csi300_return_pct', 'csi300_ytd_pct', 'excess_csi300_pct'),
                      'SPX': ('sp500', 'sp500_return_pct', 'sp500_ytd_pct', 'excess_sp500_pct'),
                      'NDX': ('ndx100', 'ndx100_return_pct', 'ndx100_ytd_pct', 'excess_ndx100_pct')}[code]
            expected = latest_completed_session(defn['market'], cutoff).isoformat()
            if source['expected'] != expected or source['market'] != defn['market']:
                raise FreshnessError(f'{code}: inconsistent benchmark date metadata')
            if source['status'] in ('available', 'partial'):
                actual = date.fromisoformat(source['actual'])
                validate_quote_date(actual, defn, cutoff)
                if source['status'] == 'available' and (
                        source['missing_count'] or source['first_missing'] is not None
                        or source['ytd_base_missing'] or any(
                            value is None for value in payload['series'][fields[0]])):
                    raise FreshnessError(f'{code}: incomplete benchmark marked available')
            elif source['status'] == 'unavailable':
                if source['actual'] is not None or any(
                        value is not None for value in payload['series'][fields[0]]) or any(
                            meta[field] is not None for field in fields[1:]):
                    raise FreshnessError(f'{code}: unavailable benchmark has values')
            else:
                raise FreshnessError(f'{code}: invalid benchmark status')
        if (meta['as_of_date'] != sources[ASSETS[1]['code']]['actual']
                or payload['series']['dates'][-1] != meta['as_of_date']):
            raise FreshnessError('Portfolio date does not match validated asset dates')
        if active and meta['strategy_as_of_date'] != meta['as_of_date']:
            raise FreshnessError('510880 strategy date does not match portfolio date')
        if meta['stale_after'] != stale_after(meta['as_of_date']):
            raise FreshnessError('Inconsistent market-session freshness deadline')
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, FreshnessError):
            raise
        raise FreshnessError(f'Missing/invalid freshness metadata: {exc}') from exc


def _write_atomic(output_path, payload):
    """Never truncate the previous artifact on serialization or replace failure."""
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    directory = os.path.dirname(os.path.abspath(output_path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=directory,
                prefix='.data-', suffix='.tmp', delete=False) as f:
            temporary = f.name
            f.write(content)
        os.replace(temporary, output_path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def export(output_path, *, now=None):
    now = utc_now(now)
    closes, bench = fetch_all(now=now)
    strategy = None
    strategy_quotes = {}
    if closes['161130'].index[-1] >= pd.Timestamp(SWITCH_DATE):
        as_of_date = closes['161130'].index[-1].strftime('%Y-%m-%d')
        strategy = fetch_feed(as_of_date)
        strategy_quotes = fetch_strategy_quotes(now=now)
    snapshot = load_pre_q4_snapshot() if strategy else None
    # Re-check each raw input before union+ffill, including mocked/cached callers.
    source_series = {**closes, **({'510880': strategy['closes']} if strategy else {}),
                     **{code: q['close'] for code, q in strategy_quotes.items()}}
    source_dates = {
        defn['code']: validate_series(
            source_series[defn['code']], defn,
            LEGACY_LAST_CLOSE if strategy and defn['code'] == '159263' else now)
        for defn in ASSETS + (STRATEGY_ASSETS if strategy else [])
    }
    for code, series in closes.items():
        if series.attrs.get('price_method'):
            source_dates[code]['price_method'] = series.attrs['price_method']
            source_dates[code]['price_provenance'] = series.attrs['price_provenance']
    if strategy:
        validate_post_switch_history(
            source_series, strategy_quotes['511260']['open'], as_of_date)

    result = run_portfolio(
        {**closes, **({'510880': strategy['closes']} if strategy else {}),
         **{code: quote['close'] for code, quote in strategy_quotes.items()}},
        [(a['code'], a['weight']) for a in ASSETS],
        start=START_DATE,
        initial=INITIAL_CAPITAL,
        comm=FEE_RATE,
        min_comm=FEE_MIN,
        strategy=strategy,
        bond_opens=strategy_quotes['511260']['open'] if strategy else None,
        snapshot=snapshot,
    )
    equity = result['equity']
    dates = [d.strftime('%Y-%m-%d') for d in equity.index]
    comparison_returns = {}
    comparison_ytd = {}
    cutoff = china_close(equity.index[-1])
    for defn in BENCHMARKS:
        code = defn['code']
        raw = bench.get(code)
        expected = latest_completed_session(defn['market'], cutoff).isoformat()
        aligned = comparison_series(raw, equity.index, defn['market'])
        comparison_returns[code] = normalized_return(aligned)
        comparison_ytd[code] = (ytd_return(raw, expected) if raw is not None else None)
        if raw is None:
            source_dates[code] = {'actual': None, 'expected': expected,
                                  'market': defn['market'], 'status': 'unavailable'}
        else:
            checked = validate_series(raw, defn, cutoff)
            missing = aligned[aligned.isna()]
            first_missing = (latest_completed_session(
                defn['market'], china_close(missing.index[0])).isoformat()
                if len(missing) else None)
            source_dates[code] = {**checked,
                                  'status': 'partial' if len(missing) or comparison_ytd[code] is None else 'available',
                                  'missing_count': len(missing),
                                  'first_missing': first_missing,
                                  'ytd_base_missing': comparison_ytd[code] is None}
    csi_ret, spx_ret, ndx_ret = (comparison_returns[code]
                                 for code in ('000300', 'SPX', 'NDX'))
    port_ret = (equity / INITIAL_CAPITAL - 1) * 100
    dd = (equity - equity.cummax()) / equity.cummax() * 100

    stats = compute_stats(equity, INITIAL_CAPITAL)
    stats.update({
        'csi300_return_pct': at_end(csi_ret),
        'sp500_return_pct': at_end(spx_ret),
        'ndx100_return_pct': at_end(ndx_ret),
        'csi300_ytd_pct': comparison_ytd['000300'],
        'sp500_ytd_pct': comparison_ytd['SPX'],
        'ndx100_ytd_pct': comparison_ytd['NDX'],
        'ytd_base_date': '2025-12-31',
        'excess_csi300_pct': (round(float(port_ret.iloc[-1] - csi_ret.iloc[-1]), 2)
                              if at_end(csi_ret) is not None else None),
        'excess_sp500_pct': (round(float(port_ret.iloc[-1] - spx_ret.iloc[-1]), 2)
                             if at_end(spx_ret) is not None else None),
        'excess_ndx100_pct': (round(float(port_ret.iloc[-1] - ndx_ret.iloc[-1]), 2)
                              if at_end(ndx_ret) is not None else None),
        'rebalance_count': len(result['rebalances']),
        'total_fees': round(float(result['fees_paid']), 2),
        'current_value': round(float(equity.iloc[-1]), 2),
        'cash': round(float(result['cash']), 2),
        'cash_pct': round(float(result['cash'] / equity.iloc[-1] * 100), 2),
    })

    holdings = []
    display_assets = [a for a in ASSETS if a['code'] != '159263'] if strategy else ASSETS
    if strategy:
        display_assets = [next(a for a in STRATEGY_ASSETS if a['code'] == result['sleeve_asset'])] + display_assets
    for a in display_assets:
        code = a['code']
        series = result['aligned'][code]
        base_date = (START_DATE if not strategy or code in snapshot['start_prices']
                     else SWITCH_DATE)
        p0 = (float(snapshot['start_prices'][code]) if strategy and code in snapshot['start_prices']
              else float(series.loc[pd.Timestamp(base_date)]))
        p1 = float(series.loc[equity.index[-1]])
        value = result['shares'][code] * p1
        holdings.append({
            'code': code,
            'name': a['name'],
            'weight_target_pct': round(a['weight'] * 100, 1),
            'weight_current_pct': round(value / float(equity.iloc[-1]) * 100, 1),
            'shares': result['shares'][code],
            'price': round(p1, 3),
            'value': round(value, 2),
            'return_pct': round((p1 / p0 - 1) * 100, 2),
            'return_base_date': base_date,
        })

    beijing_now = now.astimezone(ZoneInfo('Asia/Shanghai'))
    stale_deadline = stale_after(dates[-1])
    payload = {
        'meta': {
            **stats,
            'start_date': START_DATE,
            'as_of_date': dates[-1],
            'updated_at': beijing_now.isoformat(),
            'stale_after': stale_deadline,
            'build_commit': os.environ.get('GITHUB_SHA'),
            'source_dates': source_dates,
            'initial_capital': INITIAL_CAPITAL,
            'fee_rate': FEE_RATE,
            'min_fee': FEE_MIN,
            'next_rebalance_date': next_rebalance_date(dates[-1]),
            'strategy_as_of_date': strategy['as_of_date'] if strategy else None,
            'strategy_version': strategy['version'] if strategy else None,
            'strategy_asset': result['sleeve_asset'],
            'strategy_pending_signal': strategy['pending_signal'] if strategy else None,
        },
        'holdings': holdings,
        'series': {
            'dates': dates,
            'portfolio': _round_list(port_ret),
            'csi300': _round_list(csi_ret),
            'sp500': _round_list(spx_ret),
            'ndx100': _round_list(ndx_ret),
            'drawdown': _round_list(dd),
            'value': _round_list(equity, 0),
        },
        'rebalances': enrich_rebalances(result['rebalances']),
        'strategy_trades': [
            {**t, 'name': next(a['name'] for a in STRATEGY_ASSETS if a['code'] == t['code'])}
            for t in result['strategy_trades']
        ],
    }

    validate_payload_freshness(payload, now=now)
    _write_atomic(output_path, payload)
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate', metavar='PATH', help='仅校验已有产物的逐标的新鲜度')
    args = parser.parse_args()
    if args.validate:
        with open(args.validate, encoding='utf-8') as f:
            validate_payload_freshness(json.load(f))
        print('data.json quote dates match the latest completed exchange sessions')
    else:
        site_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'site')
        os.makedirs(site_dir, exist_ok=True)
        export(os.path.join(site_dir, 'data.json'))
