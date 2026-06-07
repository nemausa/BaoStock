# 跨周期稳健收益策略执行说明

这个目录用于单独执行当前固定策略。脚本副本放在 `scripts/`，输出文件放在 `outputs/`。

## 当前最终执行策略：f20 current

后续实盘按 `f20 current` 执行。

- `f20` 表示 `full_rebound_min = 0.20`
- 每日脚本默认就是 `full_rebound_min = 0.20`
- 每日脚本默认同时使用 `current_rebound_max = 0.05`
- 历史回放快照后缀使用 `rebound_top3`
- 策略模式使用 `current`
- 不使用 `stable-a` / `stable-b`
- 不额外添加 `rank_score >= 80` 或 `rank_score >= 85`

每天收盘后，在项目根目录执行：

```bash
cd /home/nemausa/stock/BaoStock
./strategy_recommended_runner/run_daily_current.sh
```

如果当天行情已经更新过，可以跳过更新：

```bash
./strategy_recommended_runner/run_daily_current.sh --skip-update
```

执行完成后打开：

```text
tomorrow_rebound_probability_ranking.xlsx
```

在 `概率排行` sheet 中筛选：

```text
latest_turn >= 8
current_drawdown_pct <= 29
```

筛选后按 `rank` 从小到大看：

1. 最多只看前 `2` 个候选。
2. 当前没有持仓时，第二天开盘优先买第 `1` 个候选。
3. 如果第 `1` 个买不了一手，或者主动跳过，再看第 `2` 个候选。
4. 当前已经持仓，就不买新的。

买入后按下面规则卖出：

```text
止盈价 = 买入价 * 1.08
止损价 = 买入价 * 0.97
最多持有 = 15 个交易日
同一股票卖出后冷却 = 10 个交易日
```

次日开盘价格判断：

```text
默认不额外过滤高开。
如果人工判断次日开盘明显追高，可以主动放弃。
不要用信号日盘中最低价当作可买入价格，回测和实盘口径都按次日开盘价。
```

已经新增两个可选回测过滤参数：

```text
MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT=2   # 次日开盘相对信号日收盘高开超过 2% 不买
MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT=5    # 次日开盘相对当前低点超过 5% 不买
```

最新对比结果保存于：

```text
strategy_recommended_runner/outputs/f20_open_entry_filter_comparison.md
strategy_recommended_runner/outputs/f20_open_entry_filter_comparison.xlsx
strategy_recommended_runner/outputs/f20_close_drawdown_202601_202604.md
strategy_recommended_runner/outputs/f20_close_drawdown_202601_202604.xlsx
```

结论：当前样本里，开盘过滤没有提高总收益。`次日开盘<=信号收盘+2%` 会明显降低 2025 和连续区间收益；`次日开盘<=当前低点+5%` 能略微降低连续最大回撤，但也降低总收益。因此默认策略先保持“次日开盘买入”，开盘过滤只作为人工风控参考。

`close_drawdown_pct` 表示按最新收盘价计算的当前回撤：

```text
close_drawdown_pct = (current_top_price - latest_close) / current_top_price * 100
```

2026-01 到 2026-04 对比结论：`25%-33%` 收益略高但回撤改善很小；`27%-33%` 收益降低，但最大回撤从约 `-21.23%` 降到约 `-15.01%`；`28%` 以上样本太少且亏损。当前不默认加入收盘回撤过滤，除非你优先降低回撤。

如果要回测 `f20 current`，显式指定 `rebound_top3` 后缀：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 current rebound_top3
```

如果目标日期没有 `f20` 的 daily replay 数据，先补数据：

```bash
./strategy_recommended_runner/backfill_daily_replay.sh 2026-01-01 2026-04-30 rebound_top3 0.20
```

如果要执行 2024/2025 高收益的 `current` 原策略，先看：

```text
strategy_recommended_runner/CURRENT_HIGH_RETURN_STRATEGY.md
```

注意：这个目录不复制行情数据。执行时仍读取项目根目录下的：

- `a_stock_data/parquet/`：股票日线行情
- `a_stock_data/verification_records/daily_replay/`：每日模型回放结果

## 策略参数

- 初始本金：`50000`
- 仓位管理：可用现金买满，必须按 `100` 股整数手买入
- 手续费：暂不扣除
- 买入方式：信号日后的下一个交易日开盘买入
- 当前低点到最新收盘涨幅上限：`5%`
- 同时持仓：最多 `1` 只
- 每天最多买入：`1` 只
- 同一股票卖出后冷却：`10` 个交易日
- 止盈：`8%`
- 止损：`3%`
- 最多持有：`15` 个交易日

## 策略模式

`run_recommended_strategy.sh` 支持三种模式：

| 模式 | 额外过滤 | 用途 |
|---|---|---|
| `current` | 无 | 当前基准策略，默认模式 |
| `stable-a` | `current_drawdown_pct >= 28`, `rank_score >= 80` | 稳健版 A，过滤低分和浅回撤信号 |
| `stable-b` | `current_drawdown_pct >= 28`, `rank_score >= 85` | 稳健版 B，更严格，优先减少低质量交易 |

共同基础过滤：

- `latest_turn >= 8`
- `current_drawdown_pct <= 29`

## 选股规则

这套策略不是严格只买每日 Top1。

执行逻辑是：

1. 先读取 `daily_replay` 中每天保存的完整排行。
2. 从完整排行中过滤：
   - `latest_turn >= 8`
   - `current_drawdown_pct <= 29`
   - 稳健模式会额外过滤 `current_drawdown_pct` 下限和 `rank_score` 下限。
3. 每个信号日最多保留过滤后的前 `2` 个候选。
4. 资金回测时最多只持有 `1` 只股票，所以有持仓时后续候选会被跳过。
5. 实际买入价使用信号日后一个交易日的开盘价。

因此，最终买入股票的原始排行可能不是第 1 名，这是正常的。

## 环境

默认使用：

```bash
/home/nemausa/venv/a-stock/bin/python
```

如果环境缺少依赖，可以执行：

```bash
/home/nemausa/venv/a-stock/bin/python -m pip install -r strategy_recommended_runner/requirements.txt
```

## 每日选股

每天收盘后执行：

```bash
./strategy_recommended_runner/run_daily_current.sh
```

执行完成后打开：

```text
tomorrow_rebound_probability_ranking.xlsx
```

在 `概率排行` sheet 里筛选：

```text
latest_turn >= 8
current_drawdown_pct <= 29
```

然后按 `rank` 从小到大看，最多看前 2 个候选。第二天开盘只买 1 只，持仓期间不再买新的。

## 直接回测

回测 2025 年：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 current rebound_top3
```

回测时加入“高开超过信号收盘 2% 不买”：

```bash
MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT=2 \
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 current rebound_top3
```

回测时加入“次日开盘距当前低点超过 5% 不买”：

```bash
MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT=5 \
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 current rebound_top3
```

回测时加入“按收盘价回撤在 27%-33%”：

```bash
MIN_CLOSE_DRAWDOWN=27 MAX_CLOSE_DRAWDOWN=33 \
./strategy_recommended_runner/run_recommended_strategy.sh 2026-01-01 2026-04-30 current rebound_top3
```

重新生成开盘过滤对比：

```bash
/home/nemausa/venv/a-stock/bin/python strategy_recommended_runner/scripts/compare_f20_open_filters.py
```

重新生成收盘回撤过滤对比：

```bash
/home/nemausa/venv/a-stock/bin/python strategy_recommended_runner/scripts/compare_f20_close_drawdown.py
```

回测 2026 年 1 月到 4 月：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2026-01-01 2026-04-30 current rebound_top3
```

回测稳健版 A：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 stable-a
```

回测稳健版 B：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 stable-b
```

也可以指定输出文件：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 strategy_recommended_runner/outputs/my_2025.xlsx stable-b
```

## 生成每日回放数据

如果某个时间段还没有 `daily_replay` 数据，先执行：

```bash
./strategy_recommended_runner/backfill_daily_replay.sh 2026-01-01 2026-04-30 rebound_top3 0.20
```

这个命令会生成到：

```text
a_stock_data/verification_records/daily_replay/
```

已有 `ranking_snapshot.csv` 的日期会自动跳过。

## 输出怎么看

回测结果是 Excel 文件，默认输出到 `strategy_recommended_runner/outputs/`。

重点看这些 sheet：

- `资金汇总`：最终资产、总收益、收益率、胜率、最大回撤。
- `资金交易时间线`：发现日期、买入日期、买入价、股数、卖出日期、卖出原因、收益。
- `资金交易明细`：完整交易字段。
- `资金每日资产`：每日账户资产和回撤。
- `候选信号`：模型筛出的候选股票。
- `资金跳过信号`：因为持仓、买不起一手、数据不足等原因跳过的信号。

## 主命令等价参数

`run_recommended_strategy.sh` 内部等价于执行：

```bash
/home/nemausa/venv/a-stock/bin/python strategy_recommended_runner/scripts/verification_records.py simulate-portfolio \
  --start-date START_DATE \
  --end-date END_DATE \
  --selection-mode all \
  --min-latest-turn 8 \
  --max-current-drawdown 29 \
  --max-daily-candidates 2 \
  --max-positions 1 \
  --max-daily-buys 1 \
  --take-profit 0.08 \
  --stop-loss 0.03 \
  --max-hold-days 15 \
  --reentry-cooldown-days 10 \
  --initial-capital 50000 \
  --lot-size 100 \
  --position-sizing all-cash \
  --report-md ""
```

如果设置了过滤环境变量，脚本会额外加入对应参数：

```bash
--min-close-drawdown 27
--max-close-drawdown 33
--max-next-open-to-signal-close-pct 2
--max-next-open-to-current-low-pct 5
```

稳健版 A 会额外加入：

```bash
--min-current-drawdown 28 --min-rank-score 80
```

稳健版 B 会额外加入：

```bash
--min-current-drawdown 28 --min-rank-score 85
```

## 脚本说明

- `scripts/verification_records.py`：主入口，负责读取每日回放并做资金回测。
- `scripts/backtest_rebound_top3.py`：生成每日回放时使用。
- `scripts/filter_drawdown_rebound_history.py`：回撤模型和事件计算依赖。
- `scripts/rank_rebound_candidates.py`：排行评分依赖。
- `scripts/update_a_stock_baostock.py`：更新本地行情数据的脚本副本。
- `scripts/backfill_2021_2024_and_backtest.py`：之前批量补历史和年度回测的辅助脚本，不是当前推荐策略的主入口。

## 使用前检查

执行前建议确认：

```bash
ls a_stock_data/parquet | head
ls a_stock_data/verification_records/daily_replay | head
```

如果第二个目录没有目标日期的数据，先运行 `backfill_daily_replay.sh`。
