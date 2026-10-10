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
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-07-17'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: _history('2026-07-15'))
    series = fetch.fetch_close(fetch.ASSETS[0], now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))
    assert series.index[-1] == pd.Timestamp('2026-07-15')


def test_fetch_close_uses_completed_close_when_primary_has_intraday_tail(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: _history('2026-10-09'))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs: calls.append('tencent'))
    series = fetch.fetch_close(fetch.ASSETS[0], now=datetime(2026, 10, 9, 4, tzinfo=timezone.utc))
    assert series.index[-1] == pd.Timestamp('2026-10-08')
    assert calls == []


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


def test_post_switch_adjusted_quotes_need_only_anchor_and_new_sessions(monkeypatch):
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08'])
    adjusted = pd.DataFrame({'close': [4.796, 4.814]}, index=dates)
    calls = []
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: adjusted)
    monkeypatch.setattr(fetch, 'fetch_tx', lambda *args, **kwargs:
                        calls.append('tencent') or None)
    now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    result = fetch.fetch_quotes({**fetch.ASSETS[1], 'min_rows': 2}, now=now)
    assert result['close'].tolist() == [4.796, 4.814]
    assert calls == []
    with pytest.raises(RuntimeError, match='insufficient history'):
        fetch.fetch_quotes(fetch.ASSETS[1], now=now)


def test_verified_raw_ratio_uses_same_response_anchor_and_official_check(monkeypatch):
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08'])
    raw = pd.DataFrame({'close': [4.796, 4.814]}, index=dates)
    calls = []
    def raw_source(defn):
        calls.append(('raw', defn['qfq']))
        return raw
    def checked(code, through_date, *, now):
        calls.append(('official', code, through_date))
        return {'notices_through': '2026-10-09', 'sources': ['official']}
    monkeypatch.setattr(fetch, 'fetch_tx', raw_source)
    monkeypatch.setattr(fetch, 'verify_no_actions', checked)
    result = fetch.verified_raw_ratio(fetch.ASSETS[1], 4.796, dates[0],
                                      dates[-1].date(), now=datetime(2026, 10, 8, 22, tzinfo=timezone.utc))
    assert result.tolist() == pytest.approx([4.796, 4.814])
    assert calls == [('raw', False), ('official', '161130', dates[-1].date())]
    assert result.attrs['price_method'] == 'verified_no_action_raw_ratio'
    assert result.attrs['price_provenance']['raw_anchor_close'] == 4.796


def test_verified_raw_ratio_ignores_only_today_pending_bar(monkeypatch):
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08', '2026-10-09'])
    raw = pd.DataFrame({'close': [4.796, 4.814, 4.9]}, index=dates)
    calls = []
    monkeypatch.setattr(fetch, 'fetch_tx', lambda defn: raw)
    monkeypatch.setattr(fetch, 'verify_no_actions', lambda code, through_date, *, now:
                        calls.append((code, through_date)) or {'notices_through': '2026-10-09'})
    result = fetch.verified_raw_ratio(fetch.ASSETS[1], 4.796, dates[0],
                                      dates[1].date(), now=datetime(2026, 10, 9, 4, tzinfo=timezone.utc))
    assert result.index.equals(dates[:2])
    assert result.tolist() == pytest.approx([4.796, 4.814])
    assert calls == [('161130', dates[1].date())]


@pytest.mark.parametrize('raw,reason', [
    (pd.DataFrame({'close': [4.814]}, index=pd.DatetimeIndex(['2026-10-08'])), 'anchor'),
    (pd.DataFrame({'close': [4.795, 4.814]}, index=pd.DatetimeIndex(['2026-09-30', '2026-10-08'])), 'anchors differ'),
    (pd.DataFrame({'close': [4.796, 4.814]}, index=pd.DatetimeIndex(['2026-09-30', '2026-10-09'])), 'latest date'),
    (pd.DataFrame({'close': [4.796, 0]}, index=pd.DatetimeIndex(['2026-09-30', '2026-10-08'])), 'invalid close'),
])
def test_verified_raw_ratio_rejects_bad_quote_before_disclosures(monkeypatch, raw, reason):
    monkeypatch.setattr(fetch, 'fetch_tx', lambda defn: raw)
    monkeypatch.setattr(fetch, 'verify_no_actions', lambda *args, **kwargs:
                        pytest.fail('must not check disclosures for invalid raw quotes'))
    with pytest.raises(fetch.FreshnessError, match=reason):
        fetch.verified_raw_ratio(fetch.ASSETS[1], 4.796, pd.Timestamp('2026-09-30'),
                                 datetime(2026, 10, 8).date(),
                                 now=datetime(2026, 10, 8, 22, tzinfo=timezone.utc))


def test_verified_raw_ratio_requires_every_open_session(monkeypatch):
    raw = pd.DataFrame({'close': [4.796, 4.82]},
                       index=pd.DatetimeIndex(['2026-09-30', '2026-10-09']))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda defn: raw)
    monkeypatch.setattr(fetch, 'verify_no_actions', lambda *a, **k:
                        pytest.fail('incomplete raw history must not be certified'))
    with pytest.raises(fetch.FreshnessError, match='missing 2026-10-08'):
        fetch.verified_raw_ratio(fetch.ASSETS[1], 4.796, pd.Timestamp('2026-09-30'),
                                 datetime(2026, 10, 9).date(),
                                 now=datetime(2026, 10, 9, 22, tzinfo=timezone.utc))


def test_verified_raw_ratio_fails_closed_on_official_uncertainty(monkeypatch):
    raw = pd.DataFrame({'close': [4.796, 4.814]},
                       index=pd.DatetimeIndex(['2026-09-30', '2026-10-08']))
    monkeypatch.setattr(fetch, 'fetch_tx', lambda defn: raw)
    def uncertain(*args, **kwargs):
        raise fetch.FreshnessError('new corporate action notice')
    monkeypatch.setattr(fetch, 'verify_no_actions', uncertain)
    with pytest.raises(fetch.FreshnessError, match='new corporate action notice'):
        fetch.verified_raw_ratio(fetch.ASSETS[1], 4.796, pd.Timestamp('2026-09-30'),
                                 datetime(2026, 10, 8).date(),
                                 now=datetime(2026, 10, 8, 22, tzinfo=timezone.utc))


def test_benchmark_fetch_trims_later_us_close_to_china_cutoff(monkeypatch):
    raw = _history('2026-10-08')
    monkeypatch.setattr(fetch, 'fetch_em', lambda *args, **kwargs: raw)
    cutoff = datetime(2026, 10, 8, 7, tzinfo=timezone.utc)
    result = fetch.fetch_quotes(fetch.BENCHMARKS[1],
                                now=datetime(2026, 10, 8, 22, tzinfo=timezone.utc),
                                as_of=cutoff)
    assert result.index[-1] == pd.Timestamp('2026-10-07')


def test_unavailable_benchmark_does_not_block_held_quotes(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_close', lambda *args, **kwargs:
                        _history('2026-07-15')['close'])
    monkeypatch.setattr(fetch, 'fetch_quotes', lambda *args, **kwargs:
                        (_ for _ in ()).throw(RuntimeError('no benchmark source')))
    closes, bench = fetch.fetch_all(now=datetime(2026, 7, 15, 22, tzinfo=timezone.utc))
    assert closes['161130'].index[-1] == pd.Timestamp('2026-07-15')
    assert all(value is None for value in bench.values())


def test_fetch_all_uses_one_clock_for_all_symbols(monkeypatch):
    seen = []
    cutoffs = []
    now = datetime(2026, 7, 15, 22, tzinfo=timezone.utc)

    def fake_close(defn, *, now):
        seen.append(now)
        return _history('2026-09-30')['close']

    def fake_quotes(defn, *, now, as_of):
        cutoffs.append(as_of)
        return {'close': _history('2026-09-30')['close']}

    monkeypatch.setattr(fetch, 'fetch_close', fake_close)
    monkeypatch.setattr(fetch, 'fetch_quotes', fake_quotes)
    closes, bench = fetch.fetch_all(now=now)
    assert len(seen) == 4 and all(stamp == now for stamp in seen)
    assert len(cutoffs) == 3 and all(stamp.isoformat().startswith('2026-07-15T15:00:00+08:00') for stamp in cutoffs)
    assert set(closes) == {a['code'] for a in fetch.ASSETS}
    assert set(bench) == {b['code'] for b in fetch.BENCHMARKS}


def test_after_switch_legacy_price_is_limited_to_quarter_end(monkeypatch):
    live = _history('2026-10-08')['close']
    live.loc['2026-10-08'] = 1.1
    fetched = []

    def fake_close(defn, *, now):
        fetched.append(defn['code'])
        return live

    monkeypatch.setattr(fetch, 'fetch_close', fake_close)
    monkeypatch.setattr(fetch, 'fetch_quotes', lambda defn, *, now, as_of: {'close': live})
    closes, _ = fetch.fetch_all(now=datetime(2026, 10, 8, 8, tzinfo=timezone.utc))
    assert closes['159263'].index[-1] == pd.Timestamp('2026-09-30')
    assert closes['161130'].index[-1] == pd.Timestamp('2026-10-08')
    saved = fetch.load_pre_q4_snapshot()['prices_at_switch']
    assert closes['161130'].loc['2026-09-30'] == saved['161130']
    assert closes['161130'].loc['2026-10-08'] == pytest.approx(saved['161130'] * 1.1)
    assert '159263' not in fetched


def test_frozen_legacy_history_ends_at_exit_close():
    snapshot = fetch.load_pre_q4_snapshot()
    assert snapshot['through'] == '2026-09-29'
    assert snapshot['switch_date'] == '2026-09-30'
    assert set(snapshot['prices_at_switch']) == {a['code'] for a in fetch.ASSETS}
    assert snapshot['prices_at_switch']['159263'] == 1.133


def test_bond_adjusted_open_and_close_keep_fixed_switch_anchor(monkeypatch):
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08'])

    def source(scale):
        return pd.DataFrame({'open': [134.8, 135.0], 'close': [134.804, 135.1],
                             'volume': [100, 200]}, index=dates) * scale

    monkeypatch.setattr(fetch, 'fetch_quotes', lambda *args, **kwargs: source(1))
    first = fetch.fetch_strategy_quotes(now=datetime(2026, 10, 8, 8, tzinfo=timezone.utc))['511260']
    monkeypatch.setattr(fetch, 'fetch_quotes', lambda *args, **kwargs: source(0.5))
    second = fetch.fetch_strategy_quotes(now=datetime(2026, 10, 8, 8, tzinfo=timezone.utc))['511260']
    assert first.loc['2026-09-30', 'close'] == pytest.approx(134.804)
    assert first[['open', 'close']].equals(second[['open', 'close']])
    assert second.loc['2026-10-08', 'volume'] == 100  # volume is not a price


def test_bond_quote_without_switch_anchor_fails(monkeypatch):
    monkeypatch.setattr(fetch, 'fetch_quotes', lambda *args, **kwargs:
                        pd.DataFrame({'open': [135.0], 'close': [135.1]},
                                     index=pd.DatetimeIndex(['2026-10-08'])))
    with pytest.raises(fetch.FreshnessError, match='missing Q3 close'):
        fetch.fetch_strategy_quotes(now=datetime(2026, 10, 8, 8, tzinfo=timezone.utc))


def test_adjusted_etf_never_falls_back_to_unadjusted_tencent_rows(monkeypatch):
    class FakeResp:
        def json(self):
            return {'data': {'sh511260': {'day': [['2026-09-30', '1', '1', '1', '1', '10']]}}}

    monkeypatch.setattr(fetch.requests, 'get', lambda *args, **kwargs: FakeResp())
    monkeypatch.setattr(fetch.time, 'sleep', lambda *args: None)
    assert fetch.fetch_tx({'tx': 'sh511260', 'qfq': True}) is None
