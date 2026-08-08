import json

import pandas as pd

from pipeline import export

BASES = {'159263': 1.0, '161130': 4.0, '161125': 3.0, '518850': 8.0}
BENCH_BASES = {'000300': 4500.0, 'SPX': 6800.0}


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
    monkeypatch.setattr(export, 'fetch_all', lambda: (closes, bench))

    out = tmp_path / 'data.json'
    export.export(str(out))
    data = json.loads(out.read_text(encoding='utf-8'))

    assert data['meta']['total_return_pct'] is not None
    assert data['meta']['csi300_ytd_pct'] is not None
    assert data['meta']['sp500_ytd_pct'] is not None
    assert data['meta']['current_value'] > 100000
    assert len(data['holdings']) == 4
    assert len(data['rebalances']) == 2
    assert len(data['series']['dates']) == len(data['series']['portfolio'])
    assert len(data['series']['dates']) == len(data['series']['csi300'])
    assert len(data['series']['dates']) == len(data['series']['sp500'])
    assert data['series']['portfolio'][0] == 0.0


def test_export_survives_us_holiday_on_last_cn_trading_day(tmp_path, monkeypatch):
    """回归: 组合最后一个A股交易日恰好是美股假日(基准序列没有这一天),
    ytd_return()/主图归一化不应该因为精确日期查找而崩溃(export.py:58 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    last = etf_dates[-1]
    default_bench_dates = pd.DatetimeIndex([pd.Timestamp('2025-12-31')]).append(etf_dates)
    bench_dates_missing_last = default_bench_dates[default_bench_dates != last]
    closes, bench = _make_fake_fetch_all(
        etf_dates,
        bench_dates_by_code={
            '000300': bench_dates_missing_last,
            'SPX': bench_dates_missing_last,
        },
    )
    monkeypatch.setattr(export, 'fetch_all', lambda: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out))  # 曾经会在这里抛 KeyError

    assert payload['meta']['sp500_ytd_pct'] is not None
    assert payload['meta']['csi300_ytd_pct'] is not None
    assert payload['series']['sp500'][-1] is not None
    assert payload['series']['csi300'][-1] is not None


def test_export_holdings_survive_asset_data_gap(tmp_path, monkeypatch):
    """回归: 某只资产的原始行情序列比其它资产少最后一天(模拟数据源退化导致的缺口),
    持仓明细应使用 run_portfolio 已对齐(ffill)的序列, 而不是原始序列(export.py:138 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    closes, bench = _make_fake_fetch_all(etf_dates)
    # 161130 的原始序列缺失最后一天, 但其它资产仍有该日数据 -> common index 仍包含最后一天
    closes['161130'] = closes['161130'].iloc[:-1]
    monkeypatch.setattr(export, 'fetch_all', lambda: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out))  # 曾经会在这里抛 KeyError

    codes = {h['code'] for h in payload['holdings']}
    assert codes == {'159263', '161130', '161125', '518850'}


def test_export_benchmark_return_not_nan_when_history_starts_late(tmp_path, monkeypatch):
    """回归: 基准历史比组合起始日晚开始时, reindex+ffill 无法回补最早的缺口,
    加一次 bfill 后归一化基准点不应变成 NaN, 导致整条收益率曲线消失(export.py:112 曾经的 bug)。"""
    etf_dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    bench_dates_late = pd.DatetimeIndex(etf_dates[1:])  # 第一个交易日没有基准数据
    closes, bench = _make_fake_fetch_all(
        etf_dates,
        bench_dates_by_code={'000300': bench_dates_late, 'SPX': bench_dates_late},
    )
    monkeypatch.setattr(export, 'fetch_all', lambda: (closes, bench))

    out = tmp_path / 'data.json'
    payload = export.export(str(out))

    assert all(v is not None for v in payload['series']['sp500'])
    assert all(v is not None for v in payload['series']['csi300'])
