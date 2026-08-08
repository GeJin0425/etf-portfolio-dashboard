"""行情数据抓取: 东方财富主源, 腾讯/新浪备用。

资产使用前复权(含现金分红), 基准使用不复权点位。
"""

import json
import time

import pandas as pd
import requests

UA = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    )
}

ASSETS = [
    {'code': '159263', 'name': '价值ETF易方达', 'em': '0.159263', 'tx': 'sz159263', 'qfq': True, 'weight': 0.38},
    {'code': '161130', 'name': '纳斯达克100LOF', 'em': '0.161130', 'tx': 'sz161130', 'qfq': True, 'weight': 0.28},
    {'code': '161125', 'name': '标普500LOF', 'em': '0.161125', 'tx': 'sz161125', 'qfq': True, 'weight': 0.22},
    {'code': '518850', 'name': '黄金ETF华夏', 'em': '1.518850', 'tx': 'sh518850', 'qfq': True, 'weight': 0.12},
]

BENCHMARKS = [
    {'code': '000300', 'name': '沪深300', 'em': '1.000300', 'tx': 'sh000300', 'sina': None, 'qfq': False},
    {'code': 'SPX', 'name': '标普500', 'em': '100.SPX', 'tx': 'usINX', 'sina': '.INX', 'qfq': False},
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
    """腾讯日K: CN走fqkline(优先qfq), 美股指数走kline"""
    code = defn['tx']
    if code.startswith('us'):
        url = (
            f'http://web.ifzq.gtimg.cn/appstock/app/kline/kline'
            f'?param={code},day,,,{limit}'
        )
        key = 'us.' + code[2:]
    else:
        url = (
            f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
            f'?param={code},day,,,{limit},qfq'
        )
        key = code
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            payload = resp.json()
            node = payload['data'][key]
            rows = node.get('qfqday') or node.get('day')
            if rows:
                return _to_close_df(rows)
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def fetch_sina_us(defn, start='2026-01-01'):
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


def fetch_close(defn, limit=1600):
    """返回前复权/不复权收盘价 Series, 多源按序尝试"""
    em = fetch_em(defn, limit=limit)
    if em is not None and len(em) >= 60:
        return em['close']
    tx = fetch_tx(defn, limit=limit)
    if tx is not None and len(tx) >= 60:
        return tx['close']
    if defn.get('sina'):
        sina = fetch_sina_us(defn)
        if sina is not None and len(sina) >= 60:
            return sina['close']
    raise RuntimeError(f'无法获取行情: {defn["code"]} {defn["name"]}')


def fetch_all():
    closes = {a['code']: fetch_close(a) for a in ASSETS}
    bench = {b['code']: fetch_close(b) for b in BENCHMARKS}
    return closes, bench
