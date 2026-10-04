# 4ETF 实盘组合看板

跟踪组合：2026-01-05 收盘以价值ETF易方达(159263) 38% + 纳斯达克100LOF(161130) 28% + 标普500LOF(161125) 22% + 黄金ETF华夏(518850) 12% 建仓。2026-09-30 季末收盘起，原价值ETF的 38% 改为 [510880 红利策略](https://gejin0425.github.io/510880-dividend-strategy/)仓位：持有红利ETF(510880)，卖出后配置十年国债ETF(511260)，直到下次买入。其余三项仍按季末收盘再平衡。

- 在线看板：https://gejin0425.github.io/etf-portfolio-dashboard/
- 主图：我的组合累计收益(%) vs 沪深300 vs 标普500
- 每个境内交易日的次日北京时间 05:30 抓取行情、510880 已发布的策略成交和美股基准后重新计算并发布（GitHub Actions）

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pipeline.export   # 生成 site/data.json
python -m pipeline.export --validate site/data.json   # 发布前独立复核逐标的数据日期
python -m pytest tests/ -v
python -m http.server 8000 --directory site
```

## 收益口径

- **组合收益**：ETF 前复权收盘价（含现金分红），建仓/再平衡均按收盘价成交。
- **费率**：佣金万0.5（0.005%），单笔最低0.5元，ETF免印花税；配置在 `pipeline/fetch.py`。
- **再平衡**：每季度（3/6/9/12月）最后交易日收盘，将组合调回 38/28/22/12 目标权重；自 2026-09-30 起，38% 目标分配给当时的策略持仓资产。卖出优先、买入随后，剩余现金保留在组合内。
- **策略成交**：直接读取 510880 策略看板的已成交记录，不重新计算信号。T 日收盘信号在 T+1 交易日开盘执行；轮换使用该 38% 仓位的卖出所得及整手交易留下的仓位现金。季度切换日使用收盘价，之后 510880 使用策略公布的前复权开盘成交价，511260 使用行情源的前复权开盘价；每日收盘估值。上游策略数据日期落后时停止发布。
- **基准**：沪深300 与标普500 均为收盘点位（标普500按美元计价），主图以 2026-01-05（组合建仓日）为起点归一化；KPI 卡片同时显示官方 2026 YTD（自 2025-12-31 收盘）。
- **数据源**：东方财富日K为主（前复权自动含分红），腾讯/新浪为备用；510880 策略信号和成交来自其公开看板 JSON。159263 的 2026-09-30 前复权收盘历史固定在 `data/159263-through-2026-09-30.csv`（当日腾讯日K），后续分红不会改写退出前的组合净值。

## 发布前数据日期校验

- 每个原始资产和基准序列都必须覆盖其交易所最近一个已收盘交易日，不能只凭记录条数、组合最终日期或文件生成时间判定新鲜度。主源有足够历史但已陈旧时会尝试备用源；全部源不合格则构建失败，不上传、不部署。
- 159263、161130、161125 按深交所日历，518850、510880、511260 和沪深300 按上交所日历；159263 历史固定核验至 2026-09-30。境内上市的海外 LOF 仍按境内上市交易所的收盘日期。SPX、NDX 按美国股票市场日历。
- 收盘界限为北京时间 15:00、纽约时间 16:00（官方半日市为 13:00），使用 IANA 时区处理夏令时。当前工作日 UTC 21:30 的任务对应次日北京时间 05:30，在境内和美股收盘、510880 策略发布后执行。
- 当前离线日历明确覆盖 **2025–2026 年**，含交易所假期、周末和已知特别休市，不采用普通工作日近似；2026 年国庆 10 月 1–7 日休市，9 月 30 日行情在假期内可保持合格，10 月 8 日收盘后必须有当日行情。
- **维护要求：进入未配置的交易所本地年份即停止发布**。请在新年首次任务前更新 `pipeline/freshness.py` 中两地官方休市及美国半日市列表，并加入边界测试；遇临时休市也须根据交易所公告更新。不能以延长容忍天数代替日历维护。
- `meta.source_dates` 保留每个标的的原始最新日期、预期日期和市场；`updated_at` 仅表示本次生成时间。写文件采用同目录临时文件再原子替换，校验/序列化/替换失败时保留已有产物。历史预热行情和交易策略不变，本次校验不保证历史中间每一天都完整。

官方日历来源：[上交所 2025](https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml)、[上交所 2026](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)、[深交所 2026](https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html)、[NYSE](https://www.nyse.com/trade/hours-calendars)、[Nasdaq](https://nasdaqtrader.com/Trader.aspx?id=Calendar)。2025 年 1 月 9 日美股特别休市依据 [NYSE 公告](https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx)。

## 文件结构

- `pipeline/fetch.py`：行情抓取与多源切换
- `pipeline/freshness.py`：交易所日历与逐标的原始数据日期校验
- `pipeline/portfolio.py`：建仓 + 季度再平衡模拟引擎
- `pipeline/export.py`：统计指标与 `site/data.json` 生成
- `site/`：看板前端（ECharts）

## 免责声明

本仓库为个人投资记录与研究展示，不构成投资建议。
