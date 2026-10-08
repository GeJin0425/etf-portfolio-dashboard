const COLORS = {
  green: '#3fb950',
  blue: '#58a6ff',
  gold: '#d29922',
  purple: '#a371f7',
  red: '#f85149',
  orange: '#f0883e',
};

const DARK_AXIS = {
  axisLine: { lineStyle: { color: '#21262d' } },
  axisLabel: { color: '#8b949e' },
  splitLine: { lineStyle: { color: '#161b22' } },
};

function baseGrid() {
  return { left: 58, right: 24, top: 36, bottom: 44 };
}

async function main() {
  const dataUrl = ['gejin0425.github.io', 'localhost', '127.0.0.1'].includes(window.location.hostname)
    ? './data.json'
    : 'https://gejin0425.github.io/etf-portfolio-dashboard/data.json';
  document.getElementById('data-source-link').href = dataUrl;
  let data;
  try {
    const res = await fetch(dataUrl, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    showDataError(`数据加载失败: ${err.message}`);
    const dot = document.getElementById('status-dot');
    dot.classList.remove('partial');
    dot.classList.add('stale');
    dot.title = '数据加载失败';
    document.getElementById('updated-at').textContent = '数据加载失败';
    document.getElementById('portfolio-subtitle').textContent = '持仓配置无法确认';
    return;
  }

  checkStaleness(data.meta);
  renderTopbar(data);
  renderKpis(data.meta);
  renderBenchmarkNote(data.meta);
  renderMainChart(data.series);
  renderHoldingsChart(data.holdings, data.meta.current_value);
  renderHoldingsTable(data.holdings);
  renderRebalanceTable(data.rebalances);
  renderStrategy(data);
  renderNextRebalance(data.meta.next_rebalance_date);
  renderDrawdownChart(data.series);
}

function showDataError(message) {
  const el = document.getElementById('data-error');
  el.textContent = message;
  el.hidden = false;
}

function checkStaleness(meta) {
  const deadline = new Date(meta.stale_after || `${meta.as_of_date}T00:00:00+08:00`);
  const missing = Object.entries(meta.source_dates || {})
    .filter(([, source]) => source.status === 'unavailable' || source.status === 'partial')
    .map(([code]) => code);
  const dot = document.getElementById('status-dot');
  dot.classList.remove('partial');
  if (Date.now() > deadline.getTime()) {
    dot.classList.add('stale');
    dot.title = `组合数据滞后，最近数据日期 ${meta.as_of_date}`;
    showDataError(`组合数据滞后：最近数据日期 ${meta.as_of_date}；计划更新期限 ${meta.stale_after || '未知'}`);
  } else if (missing.length) {
    dot.classList.add('partial');
    dot.title = `组合数据已更新；参考基准 ${missing.join('、')} 数据不完整`;
  } else {
    dot.title = `组合数据日期 ${meta.as_of_date}`;
  }
}

function fmtSigned(value, suffix = '%') {
  if (value === null || value === undefined || isNaN(value)) return '--';
  const sign = value > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(2)}${suffix}`;
}

function fmtAxisPercent(value, signed = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  // ECharts can pass padded axis bounds with floating-point tails. Round only
  // the label, preserve the plotted values, and avoid a misleading negative zero.
  const rounded = Number(Number(value).toFixed(2));
  return `${signed && rounded > 0 ? '+' : ''}${rounded}%`;
}

function fmtMoney(value) {
  return `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;
}

function renderTopbar(data) {
  const meta = data.meta;
  if (meta.strategy_asset) {
    document.getElementById('portfolio-subtitle').textContent =
      `${meta.strategy_asset === '510880' ? '红利ETF' : '十年国债ETF'} 38 · 纳指 28 · 标普 22 · 黄金 12  |  季度末再平衡`;
  }
  document.getElementById('updated-at').textContent =
    `数据日期 ${meta.as_of_date} · 更新于 ${meta.updated_at.slice(0, 16).replace('T', ' ')}`;
  document.getElementById('holdings-tag').textContent =
    `净值 ${fmtMoney(meta.current_value)} · 现金 ${meta.cash_pct.toFixed(1)}%`;
}

function renderKpis(meta) {
  const csiYtd = meta.csi300_ytd_pct == null ? '' : ` · YTD ${fmtSigned(meta.csi300_ytd_pct)}`;
  const spxYtd = meta.sp500_ytd_pct == null ? '' : ` · YTD ${fmtSigned(meta.sp500_ytd_pct)}`;
  const ndxYtd = meta.ndx100_ytd_pct == null ? '' : ` · YTD ${fmtSigned(meta.ndx100_ytd_pct)}`;
  const cards = [
    { value: fmtSigned(meta.total_return_pct), label: '组合总收益', sub: `净值 ${fmtMoney(meta.current_value)}` },
    { value: fmtSigned(meta.annualized_pct), label: '年化收益' },
    { value: fmtSigned(meta.max_drawdown_pct), label: '最大回撤' },
    { value: meta.sharpe.toFixed(2), label: '夏普比率', plain: true },
    { value: fmtSigned(meta.excess_csi300_pct), label: '收益差 vs 沪深300', sub: `沪深300 ${fmtSigned(meta.csi300_return_pct)}(建仓)${csiYtd} · 基准 ${benchmarkSource(meta, '000300')}` },
    { value: fmtSigned(meta.excess_sp500_pct), label: '收益差 vs 标普500', sub: `标普500 ${fmtSigned(meta.sp500_return_pct)}(建仓)${spxYtd} · 基准 ${benchmarkSource(meta, 'SPX')}` },
    { value: fmtSigned(meta.excess_ndx100_pct), label: '收益差 vs 纳斯达克100', sub: `纳斯达克100 ${fmtSigned(meta.ndx100_return_pct)}(建仓)${ndxYtd} · 基准 ${benchmarkSource(meta, 'NDX')}` },
  ];
  document.getElementById('kpi-row').innerHTML = cards.map(c => `
    <div class="kpi-card">
      <div class="label">${c.label}</div>
      <div class="value ${c.plain || c.value === '--' ? 'plain' : (parseFloat(c.value) >= 0 ? 'pos' : 'neg')}">${c.value}</div>
      ${c.sub ? `<div class="sub">${c.sub}</div>` : ''}
    </div>
  `).join('');
}

function benchmarkSource(meta, code) {
  const item = meta.source_dates[code];
  if (item.status === 'unavailable') return `缺失（需要 ${item.expected}）`;
  if (item.status === 'partial') return `${item.actual}（历史缺 ${item.missing_count} 个对齐点${item.ytd_base_missing ? '，YTD基点缺失' : ''}）`;
  return item.actual;
}

function renderBenchmarkNote(meta) {
  const note = document.getElementById('benchmark-note');
  const csi = meta.csi300_ytd_pct == null ? '--' : fmtSigned(meta.csi300_ytd_pct);
  const spx = meta.sp500_ytd_pct == null ? '--' : fmtSigned(meta.sp500_ytd_pct);
  const ndx = meta.ndx100_ytd_pct == null ? '--' : fmtSigned(meta.ndx100_ytd_pct);
  note.textContent =
    `主图以建仓时各市场最近已收盘交易日为起点；每日按北京时间15:00可得数据对齐。` +
    `最新基准日期：沪深300 ${benchmarkSource(meta, '000300')} · 标普500 ${benchmarkSource(meta, 'SPX')} · 纳斯达克100 ${benchmarkSource(meta, 'NDX')}。` +
    `价格指数YTD（自${meta.ytd_base_date}）：沪深300 ${csi} · 标普500 ${spx} · 纳斯达克100 ${ndx}。` +
    `美指为美元价格指数，仅供参考；与人民币ETF组合的收益差未调整汇率、股息和跟踪误差。`;
}

function renderMainChart(series) {
  const chart = echarts.init(document.getElementById('chart-main'));
  const allDates = series.dates;

  function isoYearsAgo(dateStr, years) {
    const [y, m, d] = dateStr.split('-').map(Number);
    return new Date(Date.UTC(y - years, m - 1, d)).toISOString().slice(0, 10);
  }

  function startIndex(range) {
    const lastDate = allDates[allDates.length - 1] ?? '';
    if (!lastDate) return 0;
    let startDate;
    if (range === 'ytd') startDate = `${lastDate.slice(0, 4)}-01-01`;
    else if (range === '1y') startDate = isoYearsAgo(lastDate, 1);
    else if (range === '3y') startDate = isoYearsAgo(lastDate, 3);
    else if (range === '5y') startDate = isoYearsAgo(lastDate, 5);
    else return 0;
    const idx = allDates.findIndex(d => d >= startDate);
    return idx === -1 ? 0 : idx;
  }

  function numericValues(...arrs) {
    return arrs.flat().filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  }

  function buildOption(start) {
    const dates = allDates.slice(start);
    const s = (arr) => arr.slice(start);
    const allValues = numericValues(s(series.portfolio), s(series.csi300), s(series.sp500), s(series.ndx100));
    const minVal = Math.min(...allValues);
    const maxVal = Math.max(...allValues);
    const pad = Math.max((maxVal - minVal) * 0.12, 1);

    return {
      backgroundColor: 'transparent',
      grid: baseGrid(),
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#11151d',
        borderColor: '#21262d',
        textStyle: { color: '#dbe2ea', fontSize: 12 },
        formatter(params) {
          const lines = params.map(p =>
            `${p.marker}${p.seriesName} &nbsp;<b>${fmtSigned(p.value)}</b>`
          );
          return `<div style="font-family:var(--mono)">${params[0].axisValue}<br>${lines.join('<br>')}</div>`;
        },
      },
      legend: {
        data: ['我的组合', '沪深300', '标普500', '纳斯达克100'],
        textStyle: { color: '#8b949e' },
        top: 0,
        itemWidth: 18,
        itemHeight: 3,
      },
      xAxis: { type: 'category', data: dates, ...DARK_AXIS },
      yAxis: {
        type: 'value',
        min: minVal - pad,
        max: maxVal + pad,
        axisLabel: { color: '#8b949e', formatter: (v) => fmtAxisPercent(v, true) },
        splitLine: { lineStyle: { color: '#161b22' } },
      },
      dataZoom: [
        { type: 'slider', backgroundColor: '#11151d', fillerColor: 'rgba(88,166,255,0.12)', borderColor: '#21262d' },
      ],
      series: [
        {
          name: '我的组合',
          type: 'line',
          data: s(series.portfolio),
          showSymbol: false,
          smooth: 0.15,
          lineStyle: { width: 3, color: COLORS.green },
          itemStyle: { color: COLORS.green },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(63,185,80,0.28)' },
              { offset: 1, color: 'rgba(63,185,80,0)' },
            ]),
          },
          z: 3,
        },
        {
          name: '沪深300',
          type: 'line',
          data: s(series.csi300),
          showSymbol: false,
          smooth: 0.15,
          lineStyle: { width: 1.6, color: COLORS.blue, type: 'dashed' },
          itemStyle: { color: COLORS.blue },
          z: 2,
        },
        {
          name: '标普500',
          type: 'line',
          data: s(series.sp500),
          showSymbol: false,
          smooth: 0.15,
          lineStyle: { width: 1.6, color: COLORS.gold, type: 'dotted' },
          itemStyle: { color: COLORS.gold },
          z: 2,
        },
        {
          name: '纳斯达克100',
          type: 'line',
          data: s(series.ndx100),
          showSymbol: false,
          smooth: 0.15,
          lineStyle: { width: 1.6, color: COLORS.purple, type: 'dashed' },
          itemStyle: { color: COLORS.purple },
          z: 2,
        },
      ],
    };
  }

  const buttons = document.querySelectorAll('#chart-ranges .range-btn');
  function applyRange(range) {
    chart.setOption(buildOption(startIndex(range)), { notMerge: true });
    buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.range === range));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => applyRange(btn.dataset.range)));
  applyRange('all');
  window.addEventListener('resize', () => chart.resize());
}

function renderHoldingsChart(holdings, currentValue) {
  const chart = echarts.init(document.getElementById('chart-holdings'));
  const palette = [COLORS.blue, COLORS.purple, COLORS.gold, COLORS.orange];
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#11151d',
      borderColor: '#21262d',
      textStyle: { color: '#dbe2ea' },
      formatter: (p) => `${p.marker}${p.name}<br/>权重 <b>${p.value}%</b>`,
    },
    series: [{
      type: 'pie',
      radius: ['58%', '80%'],
      center: ['50%', '52%'],
      padAngle: 2,
      itemStyle: { borderRadius: 6, borderColor: '#0d1117', borderWidth: 2 },
      label: {
        color: '#8b949e',
        fontSize: 11,
        // 用切片自身的 value(即 weight_current_pct)而不是 ECharts 的 {d}(占图内数据总和的比例,
        // 因为现金不计入这个饼图, 总和不到100%, {d}% 会和悬浮提示/表格里的数字对不上)
        formatter: (p) => `${p.name}\n${p.value}%`,
      },
      labelLine: { lineStyle: { color: '#30363d' } },
      data: holdings.map((h, i) => ({
        name: h.code,
        value: h.weight_current_pct,
        itemStyle: { color: palette[i % palette.length] },
      })),
    }],
    graphic: [
      {
        type: 'text',
        left: 'center',
        top: '44%',
        style: {
          text: fmtMoney(currentValue),
          fill: '#dbe2ea',
          font: '700 18px SFMono-Regular, Menlo, monospace',
          textAlign: 'center',
        },
      },
      {
        type: 'text',
        left: 'center',
        top: '56%',
        style: {
          text: '当前总资产',
          fill: '#8b949e',
          font: '11px sans-serif',
          textAlign: 'center',
        },
      },
    ],
  });
  window.addEventListener('resize', () => chart.resize());
}

function renderHoldingsTable(holdings) {
  const tbody = document.getElementById('holdings-table-body');
  tbody.innerHTML = holdings.map(h => {
    const targetBar = `<span class="weight-track"><span class="weight-fill" style="width:${h.weight_target_pct}%;background:${COLORS.blue}"></span></span>`;
    const currentBar = `<span class="weight-track"><span class="weight-fill" style="width:${Math.min(h.weight_current_pct, 100)}%;background:${COLORS.green}"></span></span>`;
    const cls = h.return_pct >= 0 ? 'pos' : 'neg';
    return `
      <tr>
        <td>
          <div class="asset-name">
            <span>${h.name}</span>
            <span class="code">${h.code}</span>
          </div>
        </td>
        <td>${h.weight_target_pct}% ${targetBar}</td>
        <td>${h.weight_current_pct}% ${currentBar}</td>
        <td>${h.shares.toLocaleString('zh-CN')}</td>
        <td>${h.price.toFixed(3)}</td>
        <td>${fmtMoney(h.value)}</td>
        <td class="${cls}">${fmtSigned(h.return_pct)}<small class="return-base">自 ${h.return_base_date}</small></td>
      </tr>
    `;
  }).join('');
}

function renderRebalanceTable(rebalances) {
  const tbody = document.getElementById('rebalance-table-body');
  const rows = [...rebalances].sort((a, b) => b.date.localeCompare(a.date));
  tbody.innerHTML = rows.map(r => {
    const actions = r.trades.map(t => {
      const cls = t.action.startsWith('卖') ? 'action-sell' : 'action-buy';
      return `<span class="${cls}">${t.action} ${t.code} ×${t.shares.toLocaleString('zh-CN')}</span>`;
    }).join('&nbsp;&nbsp;') || '—';
    return `
      <tr>
        <td>${r.date}</td>
        <td>${fmtMoney(r.value_before)}</td>
        <td>${fmtMoney(r.value_after)}</td>
        <td>¥${r.fee.toFixed(2)}</td>
        <td class="action-cell">${actions}</td>
      </tr>
    `;
  }).join('');
}

function renderStrategy(data) {
  if (!data.meta.strategy_asset) return;
  document.getElementById('strategy-panel').hidden = false;
  const asset = data.meta.strategy_asset === '510880' ? '510880 红利ETF' : '511260 十年国债ETF';
  const pending = data.meta.strategy_pending_signal;
  document.getElementById('strategy-status').textContent =
    `当前持有 ${asset}；价值100于2026-09-30收盘退出。` +
    (pending ? ` ${pending.date}收盘出现${pending.action === 'BUY' ? '买入' : '卖出'}信号，下一交易日开盘执行。` : ' 等待下一次策略信号。');
  const rows = [...data.strategy_trades].sort((a, b) => b.date.localeCompare(a.date));
  document.getElementById('strategy-trades-body').innerHTML = rows.map(t => `
    <tr><td>${t.date}</td><td>${t.signal_date || '季末切换'}</td>
    <td class="${t.action === '卖出' ? 'action-sell' : 'action-buy'}">${t.action}</td>
    <td>${t.code}</td><td>${t.shares.toLocaleString('zh-CN')}</td>
    <td>¥${t.price.toFixed(3)}</td><td>¥${t.fee.toFixed(2)}</td></tr>
  `).join('') || '<tr><td colspan="7">季末切换后的策略交易尚未发生</td></tr>';
}

function renderNextRebalance(date) {
  document.getElementById('next-rebalance').textContent =
    `下次再平衡: ${date}（以当日实际最后交易日收盘为准） · 共 ${document.querySelectorAll('#rebalance-table-body tr').length} 次再平衡`;
}

function renderDrawdownChart(series) {
  const chart = echarts.init(document.getElementById('chart-drawdown'));
  chart.setOption({
    backgroundColor: 'transparent',
    grid: baseGrid(),
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#11151d',
      borderColor: '#21262d',
      textStyle: { color: '#dbe2ea' },
      formatter: (params) => `${params[0].axisValue}<br/>回撤 <b>${params[0].value.toFixed(2)}%</b>`,
    },
    xAxis: { type: 'category', data: series.dates, ...DARK_AXIS },
    yAxis: {
      type: 'value',
      max: 0,
      axisLabel: { color: '#8b949e', formatter: (v) => fmtAxisPercent(v) },
      splitLine: { lineStyle: { color: '#161b22' } },
    },
    dataZoom: [
      { type: 'slider', backgroundColor: '#11151d', fillerColor: 'rgba(88,166,255,0.12)', borderColor: '#21262d' },
    ],
    series: [{
      type: 'line',
      data: series.drawdown,
      showSymbol: false,
      lineStyle: { width: 1.5, color: COLORS.red },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(248,81,73,0)' },
          { offset: 1, color: 'rgba(248,81,73,0.35)' },
        ]),
      },
    }],
  });
  window.addEventListener('resize', () => chart.resize());
}

document.addEventListener('DOMContentLoaded', main);
