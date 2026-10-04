"""生成看板所需的 site/data.json"""

import argparse
import json
import os
import tempfile
from datetime import date
from pathlib import Path
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
)
from .freshness import FreshnessError, utc_now, validate_quote_date, validate_series
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
    """官方口径 YTD: 以上年最后一个交易日收盘为基准。

    close_series 使用自己的交易日历(如美股), end_date 来自组合的A股交易日历,
    两者可能在假日错位, 因此用 asof 取 end_date 当日或之前最近一个有效收盘价,
    而不是精确匹配日期(精确匹配在日历错位时会 KeyError)。
    """
    base = close_series[close_series.index <= pd.Timestamp(base_date)]
    if base.empty:
        return None
    start = float(base.iloc[-1])
    end_val = close_series.asof(pd.Timestamp(end_date))
    if pd.isna(end_val):
        return None
    end = float(end_val)
    return round((end / start - 1) * 100, 2)


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


def validate_pre_q4_equity(equity):
    """Never republish a revised pre-Q4 portfolio curve."""
    path = Path(__file__).resolve().parents[1] / 'data' / 'pre-q4-portfolio.csv'
    saved = pd.read_csv(path, parse_dates=['date']).set_index('date')['value']
    actual = equity.loc[:saved.index[-1]]
    if not actual.index.equals(saved.index) or not np.allclose(actual, saved, rtol=0, atol=1e-6):
        raise ValueError('Q4 前组合净值与已固定的历史快照不一致')


def validate_payload_freshness(payload, *, now=None):
    """Independent pre-upload gate; valid JSON alone does not imply fresh data."""
    now = utc_now(now)
    try:
        meta = payload['meta']
        sources = meta['source_dates']
        active = meta['as_of_date'] >= SWITCH_DATE
        for defn in ASSETS + (STRATEGY_ASSETS if active else []) + BENCHMARKS:
            actual = date.fromisoformat(sources[defn['code']]['actual'])
            check_now = LEGACY_LAST_CLOSE if active and defn['code'] == '159263' else now
            expected = validate_quote_date(actual, defn, check_now)
            if (sources[defn['code']]['expected'] != expected
                    or sources[defn['code']]['market'] != defn['market']):
                raise FreshnessError(f'{defn["code"]}: inconsistent freshness metadata')
        if (meta['as_of_date'] != sources[ASSETS[1]['code']]['actual']
                or payload['series']['dates'][-1] != meta['as_of_date']):
            raise FreshnessError('Portfolio date does not match validated asset dates')
        if active and meta['strategy_as_of_date'] != meta['as_of_date']:
            raise FreshnessError('510880 strategy date does not match portfolio date')
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
    # Re-check each raw input before union+ffill, including mocked/cached callers.
    source_series = {**closes, **({'510880': strategy['closes']} if strategy else {}),
                     **{code: q['close'] for code, q in strategy_quotes.items()}, **bench}
    source_dates = {
        defn['code']: validate_series(
            source_series[defn['code']], defn,
            LEGACY_LAST_CLOSE if strategy and defn['code'] == '159263' else now)
        for defn in ASSETS + (STRATEGY_ASSETS if strategy else []) + BENCHMARKS
    }

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
    )
    equity = result['equity']
    if strategy:
        validate_pre_q4_equity(equity)
    dates = [d.strftime('%Y-%m-%d') for d in equity.index]

    # ffill 无法回补序列最前面的缺口(基准历史晚于组合起始日时会出现),
    # 再 bfill 一次保证归一化基准点(iloc[0])一定有值, 避免整条收益率曲线变 NaN
    csi = bench['000300'].reindex(equity.index).ffill().bfill()
    spx = bench['SPX'].reindex(equity.index).ffill().bfill()
    ndx = bench['NDX'].reindex(equity.index).ffill().bfill()
    csi_ret = (csi / csi.iloc[0] - 1) * 100
    spx_ret = (spx / spx.iloc[0] - 1) * 100
    ndx_ret = (ndx / ndx.iloc[0] - 1) * 100
    port_ret = (equity / INITIAL_CAPITAL - 1) * 100
    dd = (equity - equity.cummax()) / equity.cummax() * 100

    stats = compute_stats(equity, INITIAL_CAPITAL)
    stats.update({
        'csi300_return_pct': round(float(csi_ret.iloc[-1]), 2),
        'sp500_return_pct': round(float(spx_ret.iloc[-1]), 2),
        'ndx100_return_pct': round(float(ndx_ret.iloc[-1]), 2),
        'csi300_ytd_pct': ytd_return(bench['000300'], dates[-1]),
        'sp500_ytd_pct': ytd_return(bench['SPX'], dates[-1]),
        'ndx100_ytd_pct': ytd_return(bench['NDX'], dates[-1]),
        'ytd_base_date': '2025-12-31',
        'excess_csi300_pct': round(float(port_ret.iloc[-1] - csi_ret.iloc[-1]), 2),
        'excess_sp500_pct': round(float(port_ret.iloc[-1] - spx_ret.iloc[-1]), 2),
        'excess_ndx100_pct': round(float(port_ret.iloc[-1] - ndx_ret.iloc[-1]), 2),
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
        first_date = max(equity.index[0], pd.Timestamp(SWITCH_DATE)) if strategy and code == result['sleeve_asset'] else equity.index[0]
        p0 = float(series.loc[first_date])
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
        })

    beijing_now = now.astimezone(ZoneInfo('Asia/Shanghai'))
    payload = {
        'meta': {
            **stats,
            'start_date': START_DATE,
            'as_of_date': dates[-1],
            'updated_at': beijing_now.isoformat(),
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
