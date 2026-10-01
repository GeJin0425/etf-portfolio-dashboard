"""Exercise the real chart label callbacks without a browser or network."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[1] / 'site/app.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node.js is required for frontend callbacks')


def _run_js(script):
    harness = """
const fs = require('fs');
const vm = require('vm');
const options = [];
const context = {
  document: { addEventListener() {}, getElementById() { return {}; }, querySelectorAll() { return []; } },
  window: { addEventListener() {} },
  echarts: { init() { return { setOption(option) { options.push(option); }, resize() {} }; },
    graphic: { LinearGradient: function() {} } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
"""
    result = subprocess.run(
        [NODE, '-e', harness + script, str(APP)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_axis_percentage_rounding_and_negative_zero():
    labels = _run_js("""
console.log(JSON.stringify(vm.runInContext(`[
  fmtAxisPercent(-3.3155999999999994),
  fmtAxisPercent(23.159999999999997, true),
  fmtAxisPercent(-0.004, true),
  fmtAxisPercent(0, true),
  fmtAxisPercent(5),
  fmtAxisPercent(null),
  fmtAxisPercent(Infinity)
]`, context)));
""")
    assert labels == ['-3.32%', '+23.16%', '0%', '0%', '5%', '--', '--']


def test_both_actual_axis_callbacks_format_labels_without_changing_data():
    output = _run_js("""
const series = { dates: ['2026-09-29', '2026-09-30'],
  portfolio: [0, 8.33], csi300: [0, -7.63], sp500: [0, 10.86],
  ndx100: [0, 19.71], drawdown: [0, -1.96] };
context.series = series;
vm.runInContext('renderMainChart(series); renderDrawdownChart(series);', context);
console.log(JSON.stringify({
  mainLabel: options[0].yAxis.axisLabel.formatter(23.159999999999997),
  drawdownLabel: options[1].yAxis.axisLabel.formatter(-3.3155999999999994),
  mainValues: options[0].series[0].data,
  drawdownValues: options[1].series[0].data,
  mainMin: options[0].yAxis.min,
  mainMax: options[0].yAxis.max,
}));
""")
    assert output['mainLabel'] == '+23.16%'
    assert output['drawdownLabel'] == '-3.32%'
    assert output['mainValues'] == [0, 8.33]
    assert output['drawdownValues'] == [0, -1.96]
    assert output['mainMin'] == pytest.approx(-10.9108)
    assert output['mainMax'] == pytest.approx(22.9908)
