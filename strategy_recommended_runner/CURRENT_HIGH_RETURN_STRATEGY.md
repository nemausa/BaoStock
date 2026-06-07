# Current 高收益策略执行手册

这份文档只说明 `current` 原策略，也就是 2024 年约 `+103.63%`、2025 年约 `+99.12%` 的那套高收益回测口径。

## 1. 策略定位

`current` 是偏进攻的策略。它在 2024、2025 这种强反弹年份收益高，但 2021、2022 这种弱市会出现较大回撤。

已经验证的年度收益率：

| 年份 | 收益率 |
|---|---:|
| 2021 | -27.03% |
| 2022 | -2.22% |
| 2023 | +17.94% |
| 2024 | +103.63% |
| 2025 | +99.12% |

如果目标是复现 2024/2025 的高收益，执行时必须使用 `current` 模式，不要使用 `stable-a` 或 `stable-b`。

## 2. 固定参数

资金和交易规则：

- 初始本金：`50000`
- 手续费：暂不扣除
- 买入单位：`100` 股整数手
- 仓位：用可用现金尽量买满
- 同时持仓：最多 `1` 只股票
- 每天最多买入：`1` 只股票
- 买入时间：信号日后的下一个交易日开盘
- 同一股票卖出后冷却：`10` 个交易日

卖出规则：

- 止盈：买入价上涨 `8%`
- 止损：买入价下跌 `3%`
- 最多持有：`15` 个交易日
- 如果同一天同时碰到止损和止盈，脚本按止损优先处理

选股规则：

```text
selection-mode = all
latest_turn >= 8
current_drawdown_pct <= 29
max_daily_candidates = 2
max_positions = 1
max_daily_buys = 1
```

不要额外加这些稳健过滤：

```text
不要加 --min-current-drawdown 28
不要加 --min-rank-score 80
不要加 --min-rank-score 85
```

## 3. 每天晚上如何找股票

每天收盘后，在项目根目录执行：

```bash
./strategy_recommended_runner/run_daily_current.sh
```

这个脚本等价于：

```bash
/home/nemausa/venv/a-stock/bin/python daily_run.py --skip-verify-all --suffix daily
```

它会依次执行：

1. `update_a_stock_baostock.py`：更新行情。
2. `filter_drawdown_rebound_history.py`：筛选回撤模型股票。
3. `rank_rebound_candidates.py`：生成概率排行。
4. `verification_records.py save`：保存当天快照。
5. `verification_records.py summary`：汇总验证记录。

执行完成后，打开：

```text
tomorrow_rebound_probability_ranking.xlsx
```

看里面的 `概率排行` sheet。

## 4. 当天候选股票怎么筛

在 `概率排行` 里按下面条件筛选：

```text
latest_turn >= 8
current_drawdown_pct <= 29
```

筛选完成后：

1. 按 `rank` 从小到大排序。
2. 只看前 `2` 个候选。
3. 如果你当前已经持仓，就不要买新的。
4. 如果没有持仓，第二天开盘优先买第 1 个候选。
5. 如果第 1 个买不了一手，或者你决定跳过，再看第 2 个候选。

关键点：这不是严格 Top1。它是从完整排行中过滤后取前 2 个候选，但资金上最多只买 1 只。

## 5. 第二天怎么买

买入时间：

```text
信号日后的下一个交易日开盘买入
```

买入金额：

```text
用可用现金尽量买满，必须是 100 股整数手
```

例子：

```text
现金 50000，开盘价 18.79
一手成本 1879
最多买 2600 股
```

如果现金不足买 1 手，则跳过。

## 6. 买入后怎么卖

假设买入价是 `P`：

```text
止盈价 = P * 1.08
止损价 = P * 0.97
```

卖出规则：

1. 如果盘中最低价触及止损价，按止损价卖出。
2. 否则如果盘中最高价触及止盈价，按止盈价卖出。
3. 如果持有满 15 个交易日还没触发止盈止损，按第 15 个交易日收盘价卖出。

卖出后，同一只股票冷却 `10` 个交易日，不重复买。

## 7. 如何回测复核

回测 2024：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2024-01-01 2024-12-31 current
```

回测 2025：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31 current
```

`current` 可以省略，下面命令等价：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2025-01-01 2025-12-31
```

输出在：

```text
strategy_recommended_runner/outputs/
```

重点看 Excel 的这些 sheet：

- `资金汇总`：最终资产、收益率、胜率、最大回撤。
- `资金交易时间线`：发现日期、买入日期、买入价、卖出日期、卖出原因。
- `资金交易明细`：完整交易字段。
- `候选信号`：当期符合条件的候选股票。
- `资金跳过信号`：因满仓、买不起一手、冷却期等跳过的信号。

## 8. 如果没有历史 daily_replay

先生成历史每日回放：

```bash
./strategy_recommended_runner/backfill_daily_replay.sh 2024-01-01 2024-12-31
```

然后再回测：

```bash
./strategy_recommended_runner/run_recommended_strategy.sh 2024-01-01 2024-12-31 current
```

## 9. 风险说明

这套 `current` 策略的收益来自强反弹年份的高弹性，但它没有大盘弱市过滤。

已经验证：

- 2024、2025 收益很高。
- 2021 明显亏损。
- 2022 基本接近打平但仍小亏。

所以执行时要清楚：它适合追求总收益，不是最低回撤策略。如果更看重稳健，才使用 `stable-a` 或 `stable-b`。
