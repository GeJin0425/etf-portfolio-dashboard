import pandas as pd
import pytest
from datetime import datetime, timezone

from pipeline import fetch


def test_to_close_df():
    rows = [
        ['2026-01-05', '1.0', '1.1', '1.2', '0.9', '100', '200', '1', '2', '3', '4'],
        ['2026-01-06', '1.1', '1.2', '1.3', '1.0', '200', '300', '2', '3', '4', '5'],
    ]
    df = fetch._to_close_df(rows)
    assert list(df['close']) == [1.1, 1.2]
    assert df.index[0] == pd.Timestamp('2026-01-05')


def test_fetch_close_falls_back_to_tencent(monkeypatch):
    calls = []

    def fake_em(defn, limit=1600):
        calls.append('em')
        return None

    def fake_tx(defn, limit=1600):
        calls.append('tx')
        idx = pd.date_range('2026-01-05', periods=70, freq='B')
        return pd.DataFrame({'close': [1.0 + i * 0.001 for i in range(70)]}, index=idx)

    monkeypatch.setattr(fetch, 'fetch_em', fake_em)
    monkeypatch.setattr(fetch, 'fetch_tx', fake_tx)
    s = fetch.fetch_close(
        {'code': 'X', 'name': '测试', 'market': 'XSHE', 'tx': 'szX', 'qfq': True},
        now=datetime(2026, 4, 10, 8, tzinfo=timezone.utc))
    assert len(s) == 70
    assert calls == ['em', 'tx']


def test_fetch_tx_respects_qfq_flag(monkeypatch):
    """回归: fetch_tx 曾经无视 defn['qfq'], 对不复权的基准(如沪深300)也优先读取
    前复权字段(qfqday)而不是原始字段(day)。

    注意: 腾讯 fqkline/get 接口的 ,qfq URL 参数是接口本身的硬性要求(实测不带它
    只返回 {'version': ...}, 不返回任何K线数据, 与是否需要前复权无关), 所以 URL
    上必须始终带 ,qfq; 真正决定使用哪种价格的是响应里优先读取 day 还是 qfqday 字段。"""
    captured = {}

    class FakeResp:
        def json(self):
            return {'data': {'sh000300': {
                'day': [['2026-01-05', '1', '1', '1', '1', '10']],
                'qfqday': [['2026-01-05', '2', '2', '2', '2', '10']],
            }}}

    def fake_get(url, headers, timeout):
        captured['url'] = url
        return FakeResp()

    monkeypatch.setattr(fetch.requests, 'get', fake_get)
    df = fetch.fetch_tx({'tx': 'sh000300', 'qfq': False})
    assert df is not None
    assert ',qfq' in captured['url']  # 接口硬性要求, 必须带
    assert float(df['close'].iloc[0]) == 1.0  # qfq=False 应优先用 day(不复权), 而不是 qfqday


def test_sina_us_parser(monkeypatch):
    class FakeResp:
        text = 'var _data=([{"d":"2026-01-02","o":"1","h":"2","l":"0.5","c":"1.5","v":"1","a":"2"}]);'

    def fake_get(url, headers, timeout):
        return FakeResp()

    monkeypatch.setattr(fetch.requests, 'get', fake_get)
    df = fetch.fetch_sina_us({'sina': '.INX'})
    assert df is not None
    assert float(df['close'].iloc[-1]) == 1.5


def _history(last):
    dates = pd.bdate_range(end=last, periods=70)
    return pd.DataFrame({'close': [1.0] * len(dates)}, index=dates)


def test_fetch_close_falls_back_when_primary_has_long_but_stale_history(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-07-14'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: _history('2026-07-15'))
    series = fetch.fetch_close(fetch.ASSETS[0], now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))
    assert series.index[-1] == pd.Timestamp('2026-07-15')


def test_fetch_close_falls_back_from_future_dated_primary(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-07-16'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: _history('2026-07-15'))
    series = fetch.fetch_close(fetch.ASSETS[0], now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))
    assert series.index[-1] == pd.Timestamp('2026-07-15')


def test_fetch_close_falls_back_to_fresh_sina_us(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-07-14'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: _history('2026-07-14'))
    monkeypatch.setattr(fetch, 'fetch_sina_us', lambda *args: _history('2026-07-15'))
    series = fetch.fetch_close(fetch.BENCHMARKS[1], now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))
    assert series.index[-1] == pd.Timestamp('2026-07-15')


def test_fetch_close_reports_all_stale_sources(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-07-14'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: _history('2026-07-14'))
    with pytest.raises(RuntimeError, match='eastmoney:.*stale.*tencent:.*stale'):
        fetch.fetch_close(fetch.ASSETS[0], now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))


def test_fetch_all_uses_one_clock_for_all_symbols(monkeypatch):
    seen = []
    now = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)

    def fake_close(defn, *, now):
        seen.append(now)
        return _history('2026-09-30')['close']

    monkeypatch.setattr(fetch, 'fetch_close', fake_close)
    closes, bench = fetch.fetch_all(now=now)
    assert len(seen) == 7 and all(stamp == now for stamp in seen)
    assert set(closes) == {a['code'] for a in fetch.ASSETS}
    assert set(bench) == {b['code'] for b in fetch.BENCHMARKS}
