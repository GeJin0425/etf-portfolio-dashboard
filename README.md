# 4ETF 实盘组合看板

跟踪我的真实持仓组合：价值ETF易方达(159263) 38% + 纳斯达克100LOF(161130) 28% + 标普500LOF(161125) 22% + 黄金ETF华夏(518850) 12%，从 2026-01-05（当年首个交易日）收盘建仓，每季度最后一个交易日的收盘价再平衡回目标权重。

- 在线看板：https://gejin0425.github.io/etf-portfolio-dashboard/
- 主图：我的组合累计收益(%) vs 沪深300 vs 标普500
- 每日北京时间 15:40 自动抓取行情、重新计算并发布（GitHub Actions）

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pipeline.export   # 生成 site/data.json
python -m http.server 8000 --directory site
```

## 收益口径

- **组合收益**：ETF 前复权收盘价（含现金分红），建仓/再平衡均按收盘价成交。
- **费率**：佣金万0.5（0.005%），单笔最低0.5元，ETF免印花税；配置在 `pipeline/fetch.py`。
- **再平衡**：每季度（3/6/9/12月）最后交易日收盘，将组合调回 38/28/22/12 目标权重；卖出优先、买入随后，剩余现金保留在组合内。
- **基准**：沪深300 与标普500 均为收盘点位（标普500按美元计价），以 2026-01-05 为起点归一化为累计收益%。
- **数据源**：东方财富日K为主（前复权自动含分红），腾讯/新浪为备用。

## 文件结构

- `pipeline/fetch.py`：行情抓取与多源切换
- `pipeline/portfolio.py`：建仓 + 季度再平衡模拟引擎
- `pipeline/export.py`：统计指标与 `site/data.json` 生成
- `site/`：看板前端（ECharts）

## 免责声明

本仓库为个人投资记录与研究展示，不构成投资建议。
