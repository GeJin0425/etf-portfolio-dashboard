"""生成看板所需的 site/data.json"""

import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .fetch import (
    ASSETS,
    BENCHMARKS,
    FEE_MIN,
    FEE_RATE,
    INITIAL_CAPITAL,
    START_DATE,
    fetch_close,
)
from .portfolio import run_portfolio


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
            end = pd.Timestamp(f'{year}-{month:02d}-28') + pd.offsets.MonthEnd(0)
            if end > last:
                return end.strftime('%Y-%m-%d')
    return None


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
    code_name = {a['code']: a['name'] for a in ASSETS}
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


def export(output_path):
    closes = {a['code']: fetch_close(a) for a in ASSETS}
    bench = {b['code']: fetch_close(b) for b in BENCHMARKS}

    result = run_portfolio(
        closes,
        [(a['code'], a['weight']) for a in ASSETS],
        start=START_DATE,
        initial=INITIAL_CAPITAL,
        comm=FEE_RATE,
        min_comm=FEE_MIN,
    )
    equity = result['equity']
    dates = [d.strftime('%Y-%m-%d') for d in equity.index]

    csi = bench['000300'].reindex(equity.index).ffill()
    spx = bench['SPX'].reindex(equity.index).ffill()
    csi_ret = (csi / csi.iloc[0] - 1) * 100
    spx_ret = (spx / spx.iloc[0] - 1) * 100
    port_ret = (equity / INITIAL_CAPITAL - 1) * 100
    dd = (equity - equity.cummax()) / equity.cummax() * 100

    stats = compute_stats(equity, INITIAL_CAPITAL)
    stats.update({
        'csi300_return_pct': round(float(csi_ret.iloc[-1]), 2),
        'sp500_return_pct': round(float(spx_ret.iloc[-1]), 2),
        'excess_csi300_pct': round(float(port_ret.iloc[-1] - csi_ret.iloc[-1]), 2),
        'excess_sp500_pct': round(float(port_ret.iloc[-1] - spx_ret.iloc[-1]), 2),
        'rebalance_count': len(result['rebalances']),
        'total_fees': round(float(result['fees_paid']), 2),
        'current_value': round(float(equity.iloc[-1]), 2),
        'cash': round(float(result['cash']), 2),
        'cash_pct': round(float(result['cash'] / equity.iloc[-1] * 100), 2),
    })

    holdings = []
    for a in ASSETS:
        code = a['code']
        series = closes[code]
        p0 = float(series.loc[equity.index[0]])
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

    beijing_now = datetime.now(timezone(timedelta(hours=8)))
    payload = {
        'meta': {
            **stats,
            'start_date': START_DATE,
            'as_of_date': dates[-1],
            'updated_at': beijing_now.isoformat(),
            'initial_capital': INITIAL_CAPITAL,
            'fee_rate': FEE_RATE,
            'min_fee': FEE_MIN,
            'next_rebalance_date': next_rebalance_date(dates[-1]),
        },
        'holdings': holdings,
        'series': {
            'dates': dates,
            'portfolio': _round_list(port_ret),
            'csi300': _round_list(csi_ret),
            'sp500': _round_list(spx_ret),
            'drawdown': _round_list(dd),
            'value': _round_list(equity, 0),
        },
        'rebalances': enrich_rebalances(result['rebalances']),
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
    return payload


if __name__ == '__main__':
    site_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'site')
    os.makedirs(site_dir, exist_ok=True)
    export(os.path.join(site_dir, 'data.json'))
