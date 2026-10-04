import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from pipeline import export
from pipeline.freshness import FreshnessError

BASES = {'159263': 1.0, '161130': 4.0, '161125': 3.0, '518850': 8.0}
BENCH_BASES = {'000300': 4500.0, 'SPX': 6800.0, 'NDX': 20000.0}
NOW = datetime(2026, 7, 15, 22, tzinfo=timezone.utc)


def _series(dates, base):
    return pd.Series([base * (1 + 0.0004 * i) for i in range(len(dates))], index=dates)


def _make_fake_fetch_all(etf_dates, bench_dates_by_code=None):
    bench_dates_by_code = bench_dates_by_code or {}
    closes = {code: _series(etf_dates, base) for code, base in BASES.items()}
    default_bench_dates = pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(etf_dates)
    bench = {
        code: _series(bench_dates_by_code.get(code, default_bench_dates), base)
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
    export.validate_payload_freshness(data, now=NOW)


def test_export_replaces_value_sleeve_after_quarter_end(tmp_path, monkeypatch):
    dates = pd.bdate_range('2026-01-05', '2026-10-08')
    bench_dates = pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(dates[:-1])
    closes, bench = _make_fake_fetch_all(dates, {
        'SPX': bench_dates, 'NDX': bench_dates,
    })
    closes['159263'] = closes['159263'].loc[:'2026-09-30']
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))
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
    now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    payload = export.export(tmp_path / 'data.json', now=now)
    assert payload['meta']['as_of_date'] == '2026-10-08'
    assert payload['meta']['strategy_asset'] == '510880'
    assert {h['code'] for h in payload['holdings']} == {'510880', '161130', '161125', '518850'}
    assert [(t['code'], t['action']) for t in payload['strategy_trades']] == [
        ('511260', '卖出'), ('510880', '买入'),
    ]
    assert payload['meta']['source_dates']['159263']['expected'] == '2026-09-30'
    export.validate_payload_freshness(payload, now=now)


def test_export_survives_us_holiday_on_last_cn_trading_day(tmp_path, monkeypatch):
    """回归: 组合最后一个A股交易日恰好是美股假日(基准序列没有这一天),
    ytd_return()/主图归一化不应该因为精确日期查找而崩溃(export.py:58 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-03', freq='B')
    last = etf_dates[-1]
    default_bench_dates = pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(etf_dates)
    bench_dates_missing_last = default_bench_dates[default_bench_dates != last]
    closes, bench = _make_fake_fetch_all(
        etf_dates,
        bench_dates_by_code={
            'SPX': bench_dates_missing_last,
            'NDX': bench_dates_missing_last,
        },
    )
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out), now=datetime(2026, 7, 3, 7, 40, tzinfo=timezone.utc))

    assert payload['meta']['sp500_ytd_pct'] is not None
    assert payload['meta']['csi300_ytd_pct'] is not None
    assert payload['meta']['ndx100_ytd_pct'] is not None
    assert payload['series']['sp500'][-1] is not None
    assert payload['series']['csi300'][-1] is not None
    assert payload['series']['ndx100'][-1] is not None


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


def test_export_benchmark_return_not_nan_when_history_starts_late(tmp_path, monkeypatch):
    """回归: 基准历史比组合起始日晚开始时, reindex+ffill 无法回补最早的缺口,
    加一次 bfill 后归一化基准点不应变成 NaN, 导致整条收益率曲线消失(export.py:112 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    bench_dates_late = pd.DatetimeIndex(etf_dates[1:])  # 第一个交易日没有基准数据
    closes, bench = _make_fake_fetch_all(
        etf_dates,
        bench_dates_by_code={'000300': bench_dates_late, 'SPX': bench_dates_late, 'NDX': bench_dates_late},
    )
    monkeypatch.setattr(export, 'fetch_all', lambda **kwargs: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out), now=NOW)

    assert all(v is not None for v in payload['series']['sp500'])
    assert all(v is not None for v in payload['series']['csi300'])
    assert all(v is not None for v in payload['series']['ndx100'])


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
