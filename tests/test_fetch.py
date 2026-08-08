import pandas as pd

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
    s = fetch.fetch_close({'code': 'X', 'name': '测试', 'tx': 'szX', 'qfq': True})
    assert len(s) == 70
    assert calls == ['em', 'tx']


def test_sina_us_parser(monkeypatch):
    class FakeResp:
        text = 'var _data=([{"d":"2026-01-02","o":"1","h":"2","l":"0.5","c":"1.5","v":"1","a":"2"}]);'

    def fake_get(url, headers, timeout):
        return FakeResp()

    monkeypatch.setattr(fetch.requests, 'get', fake_get)
    df = fetch.fetch_sina_us({'sina': '.INX'})
    assert df is not None
    assert float(df['close'].iloc[-1]) == 1.5
