import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from pipeline import export, fetch
from pipeline.freshness import FreshnessError
from pipeline.freshness import latest_completed_session
from pipeline.portfolio import run_portfolio

BASES = {'159263': 1.0, '161130': 4.0, '161125': 3.0, '518850': 8.0}
BENCH_BASES = {'000300': 4500.0, 'SPX': 6800.0, 'NDX': 20000.0}
NOW = datetime(2026, 7, 15, 22, tzinfo=timezone.utc)


def _series(dates, base):
    return pd.Series([base * (1 + 0.0004 * i) for i in range(len(dates))], index=dates)


def _make_fake_fetch_all(etf_dates, bench_dates_by_code=None):
    bench_dates_by_code = bench_dates_by_code or {}
    closes = {code: _series(etf_dates, base) for code, base in BASES.items()}
    us_dates = sorted({pd.Timestamp('2025-12-31')} | {
        pd.Timestamp(latest_completed_session('US', export.china_close(day)))
        for day in etf_dates
    })
    default_us_dates = pd.DatetimeIndex(us_dates)
    bench = {
        code: _series(bench_dates_by_code.get(
            code, pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(etf_dates)
            if code == '000300' else default_us_dates), base)
        for code, base in BENCH_BASES.items()
    }
    return closes, bench


def test_export_builds_payload(tmp_path, monkeypatch):
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(etf_dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    export.export(str(out), now=NOW)
    data = json.loads(out.read_text(encoding='utf-8'))

    assert data['meta']['total_return_pct'] is not None
    assert data['meta']['csi300_ytd_pct'] is not None
    assert data['meta']['sp500_ytd_pct'] is not None
    assert data['meta']['ndx100_ytd_pct'] is not None
    assert data['meta']['current_value'] > 100000
    assert len(data['holdings']) == 4
    assert len(data['rebalances']) == 2
    assert len(data['series']['dates']) == len(data['series']['portfolio'])
    assert len(data['series']['dates']) == len(data['series']['csi300'])
    assert len(data['series']['dates']) == len(data['series']['sp500'])
    assert len(data['series']['dates']) == len(data['series']['ndx100'])
    assert data['series']['portfolio'][0] == 0.0
    assert set(data['meta']['source_dates']) == set(BASES) | set(BENCH_BASES)
    assert data['meta']['source_dates']['161130']['market'] == 'XSHE'
    assert data['meta']['source_dates']['SPX']['actual'] == '2026-07-14'
    assert data['series']['sp500'][0] == 0.0
    assert all(h['return_base_date'] == '2026-01-05' for h in data['holdings'])
    export.validate_payload_freshness(data, now=NOW)


def test_us_comparison_uses_last_close_known_at_each_china_close():
    china = pd.DatetimeIndex(['2026-01-05', '2026-01-06', '2026-10-08'])
    us = pd.Series([100, 110, 120, 999], index=pd.DatetimeIndex([
        '2026-01-02', '2026-01-05', '2026-10-07', '2026-10-08']))
    aligned = export.comparison_series(us, china, 'US')
    assert aligned.tolist() == [100, 110, 120]
    assert export.normalized_return(aligned).tolist() == pytest.approx([0, 10, 20])
    assert export.ytd_return(us, '2026-10-07', '2026-01-02') == 20


def test_optional_benchmark_absence_does_not_change_portfolio(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    full = export.export(tmp_path / 'full.json', now=NOW)
    bench['NDX'] = None
    partial = export.export(tmp_path / 'partial.json', now=NOW)
    assert partial['series']['portfolio'] == full['series']['portfolio']
    assert partial['meta']['current_value'] == full['meta']['current_value']
    assert partial['meta']['source_dates']['NDX'] == {
        'actual': None, 'expected': '2026-07-14', 'market': 'US', 'status': 'unavailable'}
    assert all(v is None for v in partial['series']['ndx100'])
    assert partial['meta']['ndx100_ytd_pct'] is None
    assert partial['meta']['excess_ndx100_pct'] is None
    export.validate_payload_freshness(partial, now=NOW)


def test_optional_benchmark_historical_gap_is_visible(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    bench['SPX'] = bench['SPX'].drop(pd.Timestamp('2026-01-05'))
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    payload = export.export(tmp_path / 'data.json', now=NOW)
    source = payload['meta']['source_dates']['SPX']
    assert source['status'] == 'partial'
    assert source['first_missing'] == '2026-01-05'
    assert source['missing_count'] >= 1
    assert payload['series']['sp500'][1] is None
    assert payload['series']['sp500'][2] is not None


def test_post_switch_missing_intermediate_held_close_is_rejected():
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-09'])
    sources = {code: pd.Series([1, 2], index=dates)
               for code in ('161130', '161125', '518850', '510880', '511260')}
    opens = pd.Series([1, 2], index=dates)
    with pytest.raises(FreshnessError, match='161130.*2026-10-08'):
        export.validate_post_switch_history(sources, opens, '2026-10-09')
    assert export.required_cn_dates('2026-09-30', '2026-10-09').strftime('%Y-%m-%d').tolist() == [
        '2026-09-30', '2026-10-08', '2026-10-09']


@pytest.mark.parametrize('invalid_open', [0, -1, float('nan'), float('inf')])
def test_post_switch_invalid_strategy_open_is_rejected(invalid_open):
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08'])
    sources = {code: pd.Series([1, 2], index=dates)
               for code in ('161130', '161125', '518850', '510880', '511260')}
    opens = pd.Series([1, invalid_open], index=dates)
    with pytest.raises(FreshnessError, match='511260: invalid strategy open on 2026-10-08'):
        export.validate_post_switch_history(sources, opens, '2026-10-08')


def test_unconfigured_next_calendar_year_fails_explicitly():
    assert export.stale_after('2026-12-30') == '2027-01-01T09:00:00+08:00'
    with pytest.raises(FreshnessError, match='calendar_unavailable: XSHE: calendar year 2027'):
        export.stale_after('2026-12-31')


def test_export_replaces_value_sleeve_after_quarter_end(tmp_path, monkeypatch):
    dates = pd.bdate_range('2026-01-05', '2026-10-08')
    bench_dates = pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(dates[:-1])
    closes, bench = _make_fake_fetch_all(dates, {
        'SPX': bench_dates, 'NDX': bench_dates,
    })
    old = run_portfolio(
        {code: series.loc[:'2026-09-29'] for code, series in closes.items()},
        [(a['code'], a['weight']) for a in fetch.ASSETS],
        start=fetch.START_DATE, initial=fetch.INITIAL_CAPITAL,
        comm=fetch.FEE_RATE, min_comm=fetch.FEE_MIN,
    )
    snapshot = {
        'equity': [[d.strftime('%Y-%m-%d'), float(v)] for d, v in old['equity'].items()],
        'shares': old['shares'], 'cash': old['cash'],
        'opening_fees': fetch.INITIAL_CAPITAL - float(old['equity'].iloc[0]),
        'fees_paid': old['fees_paid'] - (fetch.INITIAL_CAPITAL - float(old['equity'].iloc[0])),
        'rebalances': old['rebalances'],
        'start_prices': {code: float(s.loc['2026-01-05']) for code, s in closes.items()},
    }
    closes['159263'] = closes['159263'].loc[:'2026-09-30']
    closes['161130'].attrs['price_method'] = 'verified_no_action_raw_ratio'
    closes['161130'].attrs['price_provenance'] = {
        'raw_provider': 'tencent', 'raw_anchor_date': '2026-09-30',
        'disclosure_check': {'market_through': '2026-10-08'},
    }
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    monkeypatch.setattr(export, 'load_pre_q4_snapshot', lambda: snapshot)
    monkeypatch.setattr(export, 'fetch_feed', lambda as_of: {
        'as_of_date': as_of, 'version': 'flow_z20_on_b2_b3__next_open_candidate_a',
        'switch_asset': '511260', 'pending_signal': None,
        'closes': pd.Series(3.0, index=dates),
        'fills': [{'date': '2026-10-08', 'signal_date': '2026-09-30',
                   'action': 'BUY', 'price': 3.0, 'reason': 'b1'}],
    })
    prices = pd.bdate_range('2026-01-05', '2026-10-08')
    monkeypatch.setattr(export, 'fetch_strategy_quotes', lambda **kwargs: {
        '511260': pd.DataFrame({'open': 100.0, 'close': 100.0}, index=prices),
    })
    # At the 05:30/06:30 Beijing run, the U.S. Oct 8 close may already exist,
    # but the China Oct 8 portfolio close can only use the U.S. Oct 7 close.
    now = datetime(2026, 10, 8, 22, tzinfo=timezone.utc)
    payload = export.export(tmp_path / 'data.json', now=now)
    assert payload['meta']['as_of_date'] == '2026-10-08'
    assert payload['meta']['strategy_asset'] == '510880'
    assert {h['code'] for h in payload['holdings']} == {'510880', '161130', '161125', '518850'}
    assert [(t['code'], t['action']) for t in payload['strategy_trades']] == [
        ('511260', '卖出'), ('510880', '买入'),
    ]
    assert payload['meta']['source_dates']['159263']['expected'] == '2026-09-30'
    assert payload['meta']['source_dates']['SPX']['actual'] == '2026-10-07'
    assert payload['meta']['source_dates']['161130']['price_method'] == 'verified_no_action_raw_ratio'
    assert payload['meta']['source_dates']['161130']['price_provenance']['raw_provider'] == 'tencent'
    assert next(h for h in payload['holdings'] if h['code'] == '510880')['return_base_date'] == '2026-09-30'
    export.validate_payload_freshness(payload, now=now)


def test_pre_q4_portfolio_snapshot_is_copied_without_recalculation():
    snapshot = fetch.load_pre_q4_snapshot()
    dates = pd.DatetimeIndex(['2026-09-30', '2026-10-08'])
    closes = {code: pd.Series(price, index=dates) for code, price in snapshot['prices_at_switch'].items()}
    closes['510880'] = pd.Series(3.0, index=dates)
    closes['511260'] = pd.Series(100.0, index=dates)
    result = run_portfolio(
        closes,
        [(a['code'], a['weight']) for a in fetch.ASSETS],
        strategy={'switch_asset': '511260', 'fills': []},
        bond_opens=pd.Series(100.0, index=dates), snapshot=snapshot,
    )
    before = result['equity'].loc[:'2026-09-29']
    assert before.index.strftime('%Y-%m-%d').tolist() == [d for d, _ in snapshot['equity']]
    assert before.tolist() == [v for _, v in snapshot['equity']]
    assert result['rebalances'][-1]['date'] == '2026-09-30'
    assert result['shares']['159263'] == 0


def test_export_survives_us_holiday_on_last_cn_trading_day(tmp_path, monkeypatch):
    """回归: 组合最后一个A股交易日恰好是美股假日(基准序列没有这一天),
    ytd_return()/主图归一化不应该因为精确日期查找而崩溃(export.py:58 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-03', freq='B')
    closes, bench = _make_fake_fetch_all(etf_dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out), now=datetime(2026, 7, 3, 7, 40, tzinfo=timezone.utc))

    assert payload['meta']['sp500_ytd_pct'] is not None
    assert payload['meta']['csi300_ytd_pct'] is not None
    assert payload['meta']['ndx100_ytd_pct'] is not None
    assert payload['series']['sp500'][-1] is not None
    assert payload['series']['csi300'][-1] is not None
    assert payload['series']['ndx100'][-1] is not None
    assert payload['meta']['source_dates']['SPX']['actual'] == '2026-07-02'


def test_export_rejects_stale_single_asset_before_ffill(tmp_path, monkeypatch):
    """不再让 union+ffill 隐藏某一只持仓缺失的最新收盘价。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(etf_dates)
    # 161130 的原始序列缺失最后一天, 但其它资产仍有该日数据 -> common index 仍包含最后一天
    closes['161130'] = closes['161130'].iloc[:-1]
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    out.write_text('previous valid artifact', encoding='utf-8')
    with pytest.raises(FreshnessError, match='161130.*stale.*2026-07-14'):
        export.export(str(out), now=NOW)
    assert out.read_text(encoding='utf-8') == 'previous valid artifact'


def test_export_does_not_future_fill_missing_comparison_baseline(tmp_path, monkeypatch):
    """A missing entry baseline cannot be reconstructed from a later close."""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(etf_dates)
    bench['000300'] = bench['000300'].drop(pd.Timestamp('2026-01-05'))
    for code in ('SPX', 'NDX'):
        bench[code] = bench[code].drop(pd.Timestamp('2026-01-02'))
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out), now=NOW)

    assert all(v is None for v in payload['series']['sp500'])
    assert all(v is None for v in payload['series']['csi300'])
    assert all(v is None for v in payload['series']['ndx100'])
    assert payload['meta']['sp500_return_pct'] is None


@pytest.mark.parametrize('code', ['000300', 'SPX', 'NDX'])
def test_export_rejects_stale_benchmark(tmp_path, monkeypatch, code):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    bench[code] = bench[code].iloc[:-1]
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    out = tmp_path / 'data.json'
    with pytest.raises(FreshnessError, match=f'{code}.*stale'):
        export.export(out, now=NOW)
    assert not out.exists()


def test_export_rejects_future_date_without_writing(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-16', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    out = tmp_path / 'data.json'
    with pytest.raises(FreshnessError, match='future/uncompleted'):
        export.export(out, now=NOW)
    assert not out.exists()


def test_export_fetch_failure_preserves_existing_artifact(tmp_path, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError('all quote sources failed')

    monkeypatch.setattr(export, 'fetch_all', fail)
    out = tmp_path / 'data.json'
    out.write_text('previous artifact', encoding='utf-8')
    with pytest.raises(RuntimeError, match='sources failed'):
        export.export(out, now=NOW)
    assert out.read_text(encoding='utf-8') == 'previous artifact'


def test_atomic_serialization_failure_preserves_existing_artifact(tmp_path):
    out = tmp_path / 'data.json'
    out.write_text('previous artifact', encoding='utf-8')
    with pytest.raises(ValueError):
        export._write_atomic(out, {'value': float('nan')})
    assert out.read_text(encoding='utf-8') == 'previous artifact'
    assert list(tmp_path.iterdir()) == [out]


def test_atomic_replace_failure_preserves_existing_artifact(tmp_path, monkeypatch):
    out = tmp_path / 'data.json'
    out.write_text('previous artifact', encoding='utf-8')

    def fail(*args):
        raise OSError('replace failed')

    monkeypatch.setattr(export.os, 'replace', fail)
    with pytest.raises(OSError, match='replace failed'):
        export._write_atomic(out, {'value': 1})
    assert out.read_text(encoding='utf-8') == 'previous artifact'
    assert list(tmp_path.iterdir()) == [out]


def test_upload_gate_rejects_old_valid_json_with_missing_freshness_metadata():
    with pytest.raises(FreshnessError, match='freshness metadata'):
        export.validate_payload_freshness({'meta': {'as_of_date': '2026-07-15'}}, now=NOW)


def test_upload_gate_rechecks_every_date_at_publication_time(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    payload = export.export(tmp_path / 'data.json', now=NOW)
    with pytest.raises(FreshnessError, match='stale'):
        export.validate_payload_freshness(
            payload, now=datetime(2026, 7, 16, 22, tzinfo=timezone.utc))


def test_upload_gate_rejects_non_null_unavailable_benchmark_kpi(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    bench['NDX'] = None
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    payload = export.export(tmp_path / 'data.json', now=NOW)
    payload['meta']['ndx100_ytd_pct'] = 12.34
    with pytest.raises(FreshnessError, match='unavailable benchmark has values'):
        export.validate_payload_freshness(payload, now=NOW)


@pytest.mark.parametrize('field,value', [('expected', '2026-07-14'), ('market', 'US'),
                                         ('as_of_date', '2026-07-14'), ('series_date', '2026-07-14')])
def test_upload_gate_rejects_inconsistent_metadata(tmp_path, monkeypatch, field, value):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(dates)
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
    payload = export.export(tmp_path / 'data.json', now=NOW)
    if field in ('expected', 'market'):
        payload['meta']['source_dates']['161130'][field] = value
    elif field == 'series_date':
        payload['series']['dates'][-1] = value
    else:
        payload['meta'][field] = value
    with pytest.raises(FreshnessError, match='inconsistent|does not match'):
        export.validate_payload_freshness(payload, now=NOW)


def test_upload_gate_cli_returns_nonzero_for_valid_json_without_source_dates(tmp_path):
    out = tmp_path / 'data.json'
    out.write_text(json.dumps({'meta': {'as_of_date': '2026-09-30'}}), encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'pipeline.export', '--validate', str(out)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'freshness metadata' in result.stderr


def test_deploy_workflow_gates_artifact_upload_on_freshness_success():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/deploy.yml').read_text()
    generation = workflow.index('if python -m pipeline.export; then exit 0; fi')
    validation = workflow.index('run: python -m pipeline.export --validate site/data.json')
    upload = workflow.index('uses: actions/upload-pages-artifact@v3')
    deployment = workflow.index('uses: actions/deploy-pages@v4')
    assert generation < validation < upload < deployment
    assert 'continue-on-error' not in workflow
    assert 'always()' not in workflow
