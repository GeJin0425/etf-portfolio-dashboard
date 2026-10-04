"""行情数据抓取: 东方财富主源, 腾讯/新浪备用。

资产使用前复权(含现金分红), 基准使用不复权点位。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import pandas as pd
import requests

from .freshness import FreshnessError, latest_completed_session, utc_now, validate_series
from .strategy_feed import SWITCH_DATE

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
            f'http://web.ifzq.gtimg.cn/appstock/app/kline/kline'
            f'?param={code},day,,,{limit}'
        )
        key = 'us.' + code[2:]
        row_key = 'day'
    else:
        # fqkline/get 接口的 ,qfq 后缀是硬性要求: 实测不带它只返回 {'version': ...},
        # 不返回任何K线数据, 与是否需要前复权无关, 因此始终附加。
        # 是否使用前复权价格由下面 row_key 的字段优先级决定。
        url = (
            f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
            f'?param={code},day,,,{limit},qfq'
        )
        key = code
        row_key = 'qfqday' if defn.get('qfq', True) else 'day'
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            payload = resp.json()
            node = payload['data'][key]
            rows = node.get(row_key) or node.get('day') or node.get('qfqday')
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


def fetch_quotes(defn, limit=1600, *, now=None):
    """多源按序尝试; 足够长但陈旧的主源也必须切换, 不得直接发布。"""
    now = utc_now(now)
    sources = [('eastmoney', partial(fetch_em, limit=limit)),
               ('tencent', partial(fetch_tx, limit=limit))]
    if defn.get('sina'):
        sources.append(('sina', fetch_sina_us))
    errors = []
    for name, source in sources:
        df = source(defn)
        if df is None or len(df) < 60:
            errors.append(f'{name}: insufficient history')
            continue
        try:
            validate_series(df['close'], defn, now)
        except FreshnessError as exc:
            errors.append(f'{name}: {exc}')
            continue
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
    return snapshot


def fetch_strategy_quotes(*, now=None):
    now = utc_now(now)
    return {'511260': fetch_quotes(STRATEGY_ASSETS[1], now=now)}


def fetch_all(*, now=None):
    """并发拉取全部资产与基准, 共享同一个新鲜度校验时点。"""
    now = utc_now(now)
    defs = ASSETS + BENCHMARKS
    after_switch = latest_completed_session('XSHE', now).isoformat() >= SWITCH_DATE
    snapshot = load_pre_q4_snapshot() if after_switch else None
    anchor = pd.Timestamp(SWITCH_DATE)

    def read(defn):
        if not after_switch or defn not in ASSETS:
            return fetch_close(defn, now=now)
        if defn['code'] == '159263':
            return pd.Series([snapshot['prices_at_switch'][defn['code']]], index=[anchor])
        live = fetch_close(defn, now=now)
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
