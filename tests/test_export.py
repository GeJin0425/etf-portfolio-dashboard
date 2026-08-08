import json

import pandas as pd

from pipeline import export


def test_export_builds_payload(tmp_path, monkeypatch):
    dates = pd.date_range('2026-01-05', '2026-07-15', freq='B')
    bases = {'159263': 1.0, '161130': 4.0, '161125': 3.0, '518850': 8.0,
             '000300': 4500.0, 'SPX': 6800.0}
    fake = {
        code: pd.Series(
            [base * (1 + 0.0004 * i) for i in range(len(dates))],
            index=dates,
        )
        for code, base in bases.items()
    }
    monkeypatch.setattr(export, 'fetch_close', lambda defn: fake[defn['code']])

    out = tmp_path / 'data.json'
    export.export(str(out))
    data = json.loads(out.read_text(encoding='utf-8'))

    assert data['meta']['total_return_pct'] is not None
    assert data['meta']['current_value'] > 100000
    assert len(data['holdings']) == 4
    assert len(data['rebalances']) == 2
    assert len(data['series']['dates']) == len(data['series']['portfolio'])
    assert len(data['series']['dates']) == len(data['series']['csi300'])
    assert len(data['series']['dates']) == len(data['series']['sp500'])
    assert data['series']['portfolio'][0] == 0.0
