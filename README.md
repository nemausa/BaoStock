# BaoStock A 股形态筛选说明

这个目录用于下载 A 股日线数据，并筛选“历史上出现过高点回撤约 30% 后反弹，当前再次回撤到位且仍在底部附近或刚反弹”的股票。

本项目只做技术形态筛选和复盘记录，不构成买卖建议。

## 运行环境

每次运行脚本前，先进入虚拟环境：

```bash
source ~/venv/a-stock/bin/activate
```

常用依赖包括：

- `pandas`
- `baostock`
- `tqdm`
- Excel 读写相关依赖，例如 `openpyxl`

## 目录和文件

主要脚本：

| 文件 | 作用 |
|---|---|
| `update_a_stock_baostock.py` | 从 BaoStock 更新 A 股日线数据，保存到 parquet |
| `filter_drawdown_rebound_history.py` | 核心筛选脚本，找当前底部/刚反弹股票 |
| `rank_rebound_candidates.py` | 根据筛选结果生成概率排行 |
| `verification_records.py` | 保存当前结果快照，并在以后验证收益 |
| `daily_run.py` | 每日流水线：更新、筛选、排行、保存、验证 |
| `export_stock_excel.py` | 导出单只股票日线 Excel |
| `filter_price.py` | 简单按当前价格区间筛选的旧脚本 |

主要输出：

| 文件或目录 | 作用 |
|---|---|
| `a_stock_data/parquet/` | 每只股票的日线数据 |
| `a_stock_data/meta/stock_list.csv` | 股票列表和名称 |
| `drawdown_rebound_history_result.xlsx` | 当前形态筛选结果 |
| `tomorrow_rebound_probability_ranking.xlsx` | 基于当前筛选结果生成的概率排行 |
| `a_stock_data/drawdown_rebound_charts/` | 前几只候选股票的 SVG 图表 |
| `a_stock_data/verification_records/` | 每次保存的候选股票快照和后续验证结果 |

## 数据更新

更新 A 股日线数据：

```bash
python update_a_stock_baostock.py
```

脚本会把数据写入：

```text
a_stock_data/parquet/
```

当前数据只代表本地已更新到的日期。筛选和验证前，应先确认数据已经更新。

## 核心筛选逻辑

运行：

```bash
python filter_drawdown_rebound_history.py --progress
```

默认规则：

- 排除名称中包含 `ST` 的股票。
- 用 `7%` 判断趋势切换：
  - 从低点上涨超过 `7%`，确认进入上升趋势。
  - 从高点下跌超过 `7%`，确认进入下降趋势。
  - 中间小于 `7%` 的震荡不改变趋势。
- 历史验证：
  - 历史上至少有 `1` 次同类事件。
  - 高点到低点回撤在 `27%-33%`。
  - 低点后最高反弹达到 `20%` 以上。
- 当前事件：
  - 当前高点到当前低点回撤在 `27%-33%`。
  - 当前收盘价距离当前低点的反弹不超过 `7%`。
  - 当前不要求已经涨到 `20%`，目的是找底部附近或刚刚反弹。

如果临时需要包含 ST 股票：

```bash
python filter_drawdown_rebound_history.py --include-st --progress
```

## 筛选结果字段

`drawdown_rebound_history_result.xlsx` 包含两个 sheet：

- `筛选结果`
- `说明`

常用字段：

| 字段 | 含义 |
|---|---|
| `code` | 股票代码 |
| `name` | 股票名称 |
| `latest_date` | 最新交易日 |
| `latest_close` | 最新收盘价 |
| `historical_valid_event_count` | 历史有效事件次数 |
| `current_stage` | 当前状态 |
| `current_is_bottom_confirmed` | 当前低点是否已被 7% 反弹确认 |
| `current_top_date` | 当前波段高点日期 |
| `current_top_price` | 当前波段高点价格 |
| `current_low_date` | 当前低点日期 |
| `current_low_price` | 当前低点价格 |
| `current_drawdown_pct` | 当前高点到低点的回撤百分比 |
| `current_low_to_latest_pct` | 最新收盘价相对当前低点的涨幅 |
| `current_low_to_high_pct` | 当前低点后最高价相对低点的涨幅 |
| `previous_events` | 历史有效事件摘要 |

`current_stage` 说明：

| 状态 | 含义 |
|---|---|
| `bottom_area` | 最新收盘距离当前低点不超过 `5%`，仍在底部附近 |
| `just_rebound` | 最新收盘距离当前低点超过 `5%`，但没有超过 `7%` |
| `confirmed_rebound` | 当前低点后最高涨幅已达到 `20%`，当前默认 7% 限制下通常不会出现 |

## 概率排行

生成概率排行：

```bash
python rank_rebound_candidates.py
```

输出文件：

```text
tomorrow_rebound_probability_ranking.xlsx
```

这个文件是基于当前筛选结果做的排序，主要参考：

- 历史同类形态成功率
- 历史平均反弹幅度
- 当前价格距离低点是否足够近
- 最新交易日涨幅是否温和
- 当前低点是否已经确认

字段说明：

| 字段 | 含义 |
|---|---|
| `rank` | 排名 |
| `rank_score` | 综合排序分 |
| `probability_score` | 概率倾向分 |
| `history_success_rate_pct` | 历史同类事件中涨到 20% 的比例 |
| `current_low_to_latest_pct` | 当前收盘距低点涨幅 |
| `upside_to_20pct_target_pct` | 距离低点反弹 20% 目标还差多少 |
| `risk_back_to_low_pct` | 回到当前低点的潜在回撤 |
| `reason` | 排名原因摘要 |

注意：排行榜只适合作为候选池，不代表明天一定上涨。

## 每日操作流程

推荐每天收盘后或次日开盘前，先激活虚拟环境，再按下面顺序手动运行：

```bash
source ~/venv/a-stock/bin/activate
python update_a_stock_baostock.py
python filter_drawdown_rebound_history.py --progress --charts 5
python rank_rebound_candidates.py
python verification_records.py save --suffix rebound_le_7_no_st
```

这个顺序不要调换：

1. `update_a_stock_baostock.py` 更新本地日线数据。
2. `filter_drawdown_rebound_history.py` 基于最新日线生成 `drawdown_rebound_history_result.xlsx`。
3. `rank_rebound_candidates.py` 基于筛选结果生成 `tomorrow_rebound_probability_ranking.xlsx`。
4. `verification_records.py save` 保存当天筛选结果和排行快照。

如果 BaoStock 联网更新失败，应停止本次流程，不要继续用旧缓存生成“当前”排行。

也可以直接运行一键完整流程：

```bash
source ~/venv/a-stock/bin/activate
python daily_run.py
```

`daily_run.py` 会按上面的依赖顺序执行，并额外批量验证以前保存的所有快照、生成总验证汇总。

如果当天已经更新过数据，可以跳过数据更新：

```bash
python daily_run.py --skip-update
```

如果只想保存当天结果，不想批量验证历史：

```bash
python daily_run.py --skip-verify-all
```

## 保存当前记录

为了以后验证结果，需要保存当前记录：

```bash
python verification_records.py save --suffix rebound_le_7_no_st
```

当前已保存的记录在：

```text
a_stock_data/verification_records/2026-06-02_rebound_le_7_no_st/
```

记录目录包含：

| 文件 | 含义 |
|---|---|
| `ranking_snapshot.csv` | 当时的概率排行 |
| `screening_snapshot.csv` | 当时的筛选结果 |
| `snapshot.xlsx` | Excel 版快照 |
| `manifest.json` | 记录日期、保存时间、行数 |
| `verification_*.xlsx` | 后续验证结果 |

## 以后验证结果

以后先更新数据：

```bash
python update_a_stock_baostock.py
```

再验证指定快照：

```bash
python verification_records.py verify --snapshot a_stock_data/verification_records/2026-06-02_rebound_le_7_no_st
```

如果不指定 `--snapshot`，默认验证最新快照：

```bash
python verification_records.py verify
```

批量验证所有快照：

```bash
python verification_records.py verify-all
```

生成所有快照的验证汇总：

```bash
python verification_records.py summary --verify-missing
```

验证结果会输出到对应快照目录，字段包括：

| 字段 | 含义 |
|---|---|
| `verify_date` | 验证时最新交易日 |
| `days_after_record` | 距离记录日经过的自然日 |
| `verify_close` | 验证日收盘价 |
| `close_return_pct` | 记录日收盘到验证日收盘的收益率 |
| `max_high_after_record` | 记录日之后最高价 |
| `max_gain_pct` | 记录日之后最高涨幅 |
| `days_to_max_high` | 记录后多少天出现最高反弹 |
| `min_low_after_record` | 记录日之后最低价 |
| `max_drawdown_pct` | 记录日之后最大回撤 |
| `hit_3pct` | 记录后最高涨幅是否达到 3% |
| `hit_5pct` | 记录后最高涨幅是否达到 5% |
| `hit_10pct` | 记录后最高涨幅是否达到 10% |
| `hit_20pct` | 记录后最高涨幅是否达到 20% |

汇总文件默认输出：

```text
a_stock_data/verification_records/verification_summary.xlsx
```

汇总表会按每个快照统计：

- 股票数量
- 平均收盘收益
- 平均最高涨幅
- 平均最大回撤
- 收盘正收益数量
- 最高涨幅达到 3%、5%、10%、20% 的数量和比例

## 常用命令汇总

```bash
source ~/venv/a-stock/bin/activate
python update_a_stock_baostock.py
python filter_drawdown_rebound_history.py --progress --charts 5
python rank_rebound_candidates.py
python verification_records.py save --suffix rebound_le_7_no_st
python verification_records.py verify
python verification_records.py verify-all
python verification_records.py summary --verify-missing
python daily_run.py --skip-update
```

导出单只股票日线：

```bash
python export_stock_excel.py 600797
```

查看脚本参数：

```bash
python filter_drawdown_rebound_history.py --help
python rank_rebound_candidates.py --help
python verification_records.py --help
python daily_run.py --help
```
