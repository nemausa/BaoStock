# 利弗莫尔 100 关键点首次突破策略一键执行说明

这个文档只说明 `100` 元关键点的原教旨突破买法，不是旧的“接近 100 但未突破”策略。

## 每天只运行这个脚本

每天收盘后，在项目根目录执行：

```bash
./strategy_recommended_runner/run_livermore_100_breakout.sh 2026-06-26
```

把 `2026-06-26` 换成当天信号日即可。

如果不传日期，脚本默认使用系统当天日期：

```bash
./strategy_recommended_runner/run_livermore_100_breakout.sh
```

一键脚本会自动完成两步：

```text
1. 更新 A 股日线 parquet
2. 快速扫描指定信号日的 100 关键点首次突破候选
```

输出文件：

```text
strategy_recommended_runner/outputs/livermore_key_levels/key_level_100_breakout_candidate_2026-06-26.csv
```

## 快速模式和全量重建

默认就是快速模式，不会重建全量 `events.csv/signals.csv`。

如果你已经更新过行情，可以跳过行情更新，只导出候选：

```bash
./strategy_recommended_runner/run_livermore_100_breakout.sh 2026-06-26 --skip-update
```

如果你需要重新做历史统计、复盘、更新 `events.csv/signals.csv`，才运行全量重建：

```bash
./strategy_recommended_runner/run_livermore_100_breakout.sh 2026-06-26 --rebuild-events
```

全量重建会扫描全市场全历史，耗时明显更久。日常选股不需要每天执行它。

如果行情已经更新过，但你仍想重建全量事件：

```bash
./strategy_recommended_runner/run_livermore_100_breakout.sh 2026-06-26 --skip-update --rebuild-events
```

默认 Python 是 `python3`。如果要指定虚拟环境：

```bash
PYTHON_BIN=/home/nemausa/venv/a-stock/bin/python \
./strategy_recommended_runner/run_livermore_100_breakout.sh 2026-06-26
```

## 如何看结果

终端会输出候选数、排名表和结论。

```text
候选数: 0
```

表示当天无符合规则股票，不买。

```text
是否可买 = 待确认
```

表示本地还没有下一个交易日开盘价，只是预选结果。下个交易日开盘必须人工确认：

```text
开盘 >= 100：可以买排名第一
开盘 < 100：不买
```

```text
是否可买 = 否
```

表示下个交易日开盘已经低于 `100`，不买。

```text
是否可买 = 是
```

表示本地已有次日开盘数据，且开盘仍在 `100` 以上。只看排名第一。

## 策略定义

信号日必须满足：

```text
事件类型 = first_breakout_event
关键价位 = 100
趋势状态 = up
收盘确认突破 = True
是否首次突破 = True
```

含义：

```text
前面没有反复突破
当天收盘确认站上 100
趋势已经处于 up
这是第一次有效突破 100 关键点
```

## 买入规则

信号日收盘后只做预选。

真正买入必须等下一交易日开盘确认：

```text
次日开盘 >= 100：可以买候选第一名
次日开盘 < 100：不买
```

如果脚本输出 `是否可买 = 待确认`，说明本地还没有下一个交易日行情。此时只能作为预选结果，开盘前必须人工确认。

## 多只股票如何选一只

脚本会对同一天所有符合突破条件的股票打分排序。

打分规则：

```text
放量突破：+3
D0涨幅 5%-15%：+3
D0涨幅 15%-20%：+1
D0收盘 100-108：+3
D0收盘 108-115：+1
D0最低价 >= 100：+2
D0最高价 / D0收盘 <= 1.05：+2
次日开盘 >= 100：+5
次日开盘相对D0收盘在 -1% 到 +5%：+3
次日开盘高开超过 8%：-3
```

排序规则：

```text
先排除次日开盘 < 100 的股票
再按评分从高到低
再按 D0收盘价从高到低
再按 D0涨幅更接近 10% 优先
最后按股票代码升序
```

实盘只看排名第一。

## 卖出规则

买入后按下面规则卖出：

```text
跌破 100：卖出
盈利达到 15%：止盈
从持仓最高价回撤 7%：保护性卖出
最多持有 10 个交易日
```

其中 `跌破 100` 是关键点失败，优先级最高。

## 查看买入后走势

这部分不是每天必须执行，只在需要复盘走势时使用。

导出某一天信号后 20 个交易日价格：

```bash
python3 strategy_recommended_runner/scripts/export_key_level_100_breakout_next20.py \
  --start-date 2026-06-26 \
  --end-date 2026-06-26 \
  --prefix key_level_100_breakout_2026_06_26_next20
```

生成 K 线图：

```bash
python3 strategy_recommended_runner/scripts/export_key_level_100_breakout_charts.py \
  --prices-file strategy_recommended_runner/outputs/livermore_key_levels/key_level_100_breakout_2026_06_26_next20_prices.csv \
  --summary-file strategy_recommended_runner/outputs/livermore_key_levels/key_level_100_breakout_2026_06_26_next20_summary.csv \
  --out-dir strategy_recommended_runner/outputs/livermore_key_levels/charts/key_level_100_breakout_2026_06_26_next20
```

打开：

```text
strategy_recommended_runner/outputs/livermore_key_levels/charts/key_level_100_breakout_2026_06_26_next20/index.html
```

## 不要混用旧脚本

旧脚本：

```bash
python3 strategy_recommended_runner/scripts/export_key_level_100_candidate.py --date 2026-06-26
```

它对应的是：

```text
D0收盘 90-95
D0最高价 < 100
接近 100 但还没有突破
```

这个不是利弗莫尔原教旨的“首次突破 100”买法。
