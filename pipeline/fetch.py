"""行情数据抓取: 东方财富主源, 腾讯/新浪备用。

资产使用前复权(含现金分红), 基准使用不复权点位。
"""

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as clock_time, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .freshness import (FreshnessError, latest_completed_session, next_session,
                        utc_now, validate_series)
from .strategy_feed import SWITCH_DATE
from .verified_bridge import verify_no_actions

LEGACY_LAST_CLOSE = datetime(2026, 9, 30, 7, tzinfo=timezone.utc)

UA = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    )
}

ASSETS = [
    {'code': '159263', 'name': '价值ETF易方达', 'market': 'XSHE', 'em': '0.159263', 'tx': 'sz159263', 'qfq': True, 'weight': 0.38},
    {'code': '161130', 'name': '纳斯达克100LOF', 'market': 'XSHE', 'em': '0.161130', 'tx': 'sz161130', 'qfq': True, 'weight': 0.28},
    {'code': '161125', 'name': '标普500LOF', 'market': 'XSHE', 'em': '0.161125', 'tx': 'sz161125', 'qfq': True, 'weight': 0.22},
    {'code': '518850', 'name': '黄金ETF华夏', 'market': 'XSHG', 'em': '1.518850', 'tx': 'sh518850', 'qfq': True, 'weight': 0.12},
]

STRATEGY_ASSETS = [
    {'code': '510880', 'name': '红利ETF华泰柏瑞', 'market': 'XSHG', 'em': '1.510880', 'tx': 'sh510880', 'qfq': True, 'weight': 0.38},
    {'code': '511260', 'name': '十年国债ETF', 'market': 'XSHG', 'em': '1.511260', 'tx': 'sh511260', 'qfq': True, 'weight': 0.38},
]

BENCHMARKS = [
    {'code': '000300', 'name': '沪深300', 'market': 'XSHG', 'em': '1.000300', 'tx': 'sh000300', 'sina': None, 'qfq': False},
    {'code': 'SPX', 'name': '标普500', 'market': 'US', 'em': '100.SPX', 'tx': 'usINX', 'sina': '.INX', 'qfq': False},
    {'code': 'NDX', 'name': '纳斯达克100', 'market': 'US', 'em': '100.NDX100', 'tx': 'usNDX', 'sina': '.NDX', 'qfq': False},
]

START_DATE = '2026-01-05'
INITIAL_CAPITAL = 100000
FEE_RATE = 0.00005
FEE_MIN = 0.5

KLINE_COLS = ['date', 'open', 'close', 'high', 'low', 'volume',
              'amount', 'amplitude', 'pct_chg', 'change', 'turnover']
FLOAT_COLS = ['open', 'close', 'high', 'low', 'volume', 'amount',
              'amplitude', 'pct_chg', 'change', 'turnover']


def _to_close_df(rows):
    if rows and len(rows[0]) == 6:
        cols = ['date', 'open', 'close', 'high', 'low', 'volume']
    else:
        cols = KLINE_COLS
    df = pd.DataFrame(rows, columns=cols)
    df['date'] = pd.to_datetime(df['date'])
    for col in df.columns:
        if col != 'date':
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['close'])
    df = df.drop_duplicates('date').set_index('date').sort_index()
    return df[['open', 'close', 'high', 'low', 'volume']]


def fetch_em(defn, limit=1600):
    """东方财富日K线, 资产前复权 / 基准不复权"""
    url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
    params = {
        'secid': defn['em'],
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': 101,
        'fqt': 1 if defn.get('qfq') else 0,
        'lmt': limit,
        'end': '20500101',
    }
    headers = {**UA, 'Referer': 'https://quote.eastmoney.com/'}
    for attempt in range(4):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            data = resp.json().get('data')
            if data and data.get('klines'):
                rows = [line.split(',') for line in data['klines']]
                return _to_close_df(rows)
        except Exception:
            pass
        time.sleep(1.2 * (attempt + 1))
    return None


def fetch_tx(defn, limit=1600):
    """腾讯日K: CN走fqkline(按defn['qfq']决定优先读取的复权/不复权字段), 美股指数走kline"""
    code = defn['tx']
    if code.startswith('us'):
        url = (
            f'https://web.ifzq.gtimg.cn/appstock/app/kline/kline'
            f'?param={code},day,,,{limit}'
        )
        key = 'us.' + code[2:]
        row_key = 'day'
    else:
        # fqkline/get 接口的 ,qfq 后缀是硬性要求: 实测不带它只返回 {'version': ...},
        # 不返回任何K线数据, 与是否需要前复权无关, 因此始终附加。
        # 是否使用前复权价格由下面 row_key 的字段优先级决定。
        url = (
            f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
            f'?param={code},day,,,{limit},qfq'
        )
        key = code
        row_key = 'qfqday' if defn.get('qfq', True) else 'day'
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            payload = resp.json()
            node = payload['data'][key]
            # Never silently switch between adjusted ETF prices and raw prices.
            rows = node.get(row_key)
            if rows:
                return _to_close_df(rows)
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def fetch_sina_us(defn, start='2015-01-01'):
    """新浪美股指数日K备用(标普500)"""
    if not defn.get('sina'):
        return None
    symbol = defn['sina']
    url = (
        'https://stock.finance.sina.com.cn/usstock/api/jsonp.php/'
        f'var%20_data=/US_MinKService.getDailyK?symbol={symbol}'
    )
    headers = {**UA, 'Referer': 'https://finance.sina.com.cn/'}
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=25)
            text = resp.text
            lo = text.find('[')
            hi = text.rfind(']')
            if lo == -1 or hi <= lo:
                continue
            rows = json.loads(text[lo:hi + 1])
            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['d'])
            df = df.set_index('date').sort_index()
            close = df['c'].astype(float)
            close = close[close.index >= pd.Timestamp(start)]
            return close.to_frame('close')
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def fetch_quotes(defn, limit=1600, *, now=None, as_of=None):
    """多源按序尝试; 足够长但陈旧的主源也必须切换, 不得直接发布。"""
    now = utc_now(now)
    as_of = utc_now(as_of) if as_of is not None else now
    runtime_close = latest_completed_session(defn['market'], now)
    needed_close = latest_completed_session(defn['market'], as_of)
    sources = [('eastmoney', partial(fetch_em, limit=limit)),
               ('tencent', partial(fetch_tx, limit=limit))]
    if defn.get('sina'):
        sources.append(('sina', fetch_sina_us))
    minimum = defn.get('min_rows', 60)
    errors = []
    for name, source in sources:
        df = source(defn)
        if df is None or len(df) < minimum:
            errors.append(f'{name}: insufficient history ({0 if df is None else len(df)} rows; need {minimum})')
            continue
        try:
            if as_of == now and df.index[-1].date() > runtime_close:
                raise FreshnessError(f'{defn["code"]}: future/uncompleted quote date {df.index[-1].date()}')
            # Benchmark feeds may include a later, still-open session. It is
            # never used: the exact China-close cutoff below selects the bar.
            df = df.loc[:pd.Timestamp(needed_close)]
            if len(df) < minimum:
                raise FreshnessError(f'{defn["code"]}: insufficient history before {needed_close} '
                                     f'({len(df)} rows; need {minimum})')
            validate_series(df['close'], defn, as_of)
        except FreshnessError as exc:
            errors.append(f'{name}: {exc}')
            continue
        print(
            f'Quote source {defn["code"]}: {name}, '
            f'latest={df.index[-1].date()}, required={needed_close}, '
            f'adjusted={bool(defn.get("qfq"))}',
            flush=True,
        )
        return df
    raise RuntimeError(
        f'无法获取有效新鲜行情: {defn["code"]} {defn["name"]}; ' + '; '.join(errors)
    )


def fetch_close(defn, limit=1600, *, now=None):
    return fetch_quotes(defn, limit=limit, now=now)['close']


def load_pre_q4_snapshot():
    path = Path(__file__).resolve().parents[1] / 'data' / 'pre-q4-snapshot.json'
    with path.open(encoding='utf-8') as f:
        snapshot = json.load(f)
    if snapshot['switch_date'] != SWITCH_DATE or snapshot['through'] != '2026-09-29':
        raise ValueError('Q4 切换快照日期不匹配')
    if abs(snapshot['opening_fees'] - (INITIAL_CAPITAL - snapshot['equity'][0][1])) > 1e-6:
        raise ValueError('Q4 快照建仓费用与首日净值不匹配')
    if snapshot['strategy_prices_at_switch']['511260'] <= 0:
        raise ValueError('511260 Q3 固定价格锚点无效')
    return snapshot


def fetch_strategy_quotes(*, now=None):
    now = utc_now(now)
    quote = fetch_quotes({**STRATEGY_ASSETS[1], 'min_rows': 2}, now=now)
    anchor = pd.Timestamp(SWITCH_DATE)
    if anchor not in quote.index:
        raise FreshnessError('511260: missing Q3 close for price continuity')
    saved_close = load_pre_q4_snapshot()['strategy_prices_at_switch']['511260']
    factor = saved_close / float(quote.loc[anchor, 'close'])
    anchored = quote.loc[anchor:].copy()
    anchored[['open', 'close']] *= factor
    return {'511260': anchored}


def verified_raw_ratio(defn, saved_close, anchor, required_date, *, now):
    """Extend a frozen adjusted close with raw returns only after official checks.

    Tencent ``day`` is an unadjusted exchange close, never a substitute for
    ``qfqday``. A same-response raw anchor and complete daily path let us use
    its *returns* while official disclosures rule out intervening share or
    cash actions. Any uncertainty fails the entire build.
    """
    raw = fetch_tx({**defn, 'qfq': False})
    code = defn['code']
    if raw is None or raw.empty or anchor not in raw.index:
        raise FreshnessError(f'{code}: raw bridge missing frozen price anchor')
    if raw.index[-1].date() != required_date:
        raise FreshnessError(f'{code}: raw bridge latest date is not {required_date}')
    needed = [anchor]
    day = anchor.date()
    while day < required_date:
        day = next_session(defn['market'], day)
        if day <= required_date:
            needed.append(pd.Timestamp(day))
    missing = pd.DatetimeIndex(needed).difference(raw.index)
    if len(missing):
        raise FreshnessError(f'{code}: raw bridge missing {missing[0]:%Y-%m-%d}')
    prices = raw.loc[needed, 'close']
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in prices):
        raise FreshnessError(f'{code}: raw bridge has invalid close')
    raw_anchor = float(prices.loc[anchor])
    if not math.isfinite(saved_close) or saved_close <= 0 or abs(raw_anchor - saved_close) > 0.0005:
        raise FreshnessError(f'{code}: raw and frozen adjusted anchors differ')
    provenance = verify_no_actions(code, required_date, now=now)
    bridged = prices * (saved_close / raw_anchor)
    bridged.attrs['price_method'] = 'verified_no_action_raw_ratio'
    bridged.attrs['price_provenance'] = {
        'raw_provider': 'tencent',
        'raw_anchor_date': anchor.date().isoformat(),
        'raw_anchor_close': raw_anchor,
        'frozen_adjusted_anchor_close': saved_close,
        'disclosure_check': provenance,
    }
    print(f'Quote bridge {code}: Tencent raw return from {anchor.date()} '
          f'through {required_date}, official disclosures checked through '
          f'{provenance["notices_through"]}', flush=True)
    return bridged


def fetch_all(*, now=None):
    """Holdings are required; comparison indices may be absent without blocking NAV."""
    now = utc_now(now)
    china_close = latest_completed_session('XSHE', now)
    cutoff = datetime.combine(china_close, clock_time(15), ZoneInfo('Asia/Shanghai'))
    defs = ASSETS + BENCHMARKS
    after_switch = latest_completed_session('XSHE', now).isoformat() >= SWITCH_DATE
    snapshot = load_pre_q4_snapshot() if after_switch else None
    anchor = pd.Timestamp(SWITCH_DATE)

    def read(defn):
        if defn in BENCHMARKS:
            try:
                return fetch_quotes(defn, now=now, as_of=cutoff)['close']
            except RuntimeError as exc:
                print(f'Optional benchmark unavailable: {exc}', flush=True)
                return None
        if not after_switch or defn not in ASSETS:
            return fetch_close(defn, now=now)
        if defn['code'] == '159263':
            return pd.Series([snapshot['prices_at_switch'][defn['code']]], index=[anchor])
        # Historical NAV and holdings through Q3 are frozen in the snapshot;
        # Q4 only needs the switch anchor and subsequent trading sessions.
        quote_defn = {**defn, 'min_rows': 2}
        try:
            live = fetch_close(quote_defn, now=now)
        except RuntimeError:
            if defn['code'] not in ('161130', '161125'):
                raise
            return verified_raw_ratio(defn, snapshot['prices_at_switch'][defn['code']],
                                      anchor, china_close, now=now)
        # ponytail: the quote window must reach this anchor; increase lookback if it ages out.
        if anchor not in live.index:
            raise FreshnessError(f'{defn["code"]}: missing Q3 close for price continuity')
        return live.loc[anchor:] * (snapshot['prices_at_switch'][defn['code']] / live.loc[anchor])

    with ThreadPoolExecutor(max_workers=len(defs)) as pool:
        series_by_code = dict(zip(
            (d['code'] for d in defs),
            pool.map(read, defs),
        ))
    closes = {a['code']: series_by_code[a['code']] for a in ASSETS}
    bench = {b['code']: series_by_code[b['code']] for b in BENCHMARKS}
    return closes, bench
