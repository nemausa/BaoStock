 ## Execution

  - 激活环境后执行：

    source ~/venv/a-stock/bin/activate
    python update_a_stock_baostock.py
    python filter_drawdown_rebound_history.py --progress --charts 5
    python rank_rebound_candidates.py
    python verification_records.py save --suffix rebound_le_7_no_st

  - 如果 BaoStock 联网更新失败，停止并报告，不用混合日期缓存继续生成“当前”排行。
  - 预期产物：
      - drawdown_rebound_history_result.xlsx
      - tomorrow_rebound_probability_ranking.xlsx
      - a_stock_data/verification_records/2026-06-05_rebound_le_7_no_st/

  ## Score Source

  旧排行榜分数不是外部模型分数，是 rank_rebound_candidates.py 的规则打分：

  - probability_score = 100 * (0.62 * history_score + 0.38 * current_score)
  - history_score 来自历史同类回撤成功率、样本数、历史成功事件平均反弹幅度。
  - current_score 来自当前距低点涨幅、反弹区间、最新日涨幅、低点是否确认、当前阶段。
  - rank_score = probability_score * (1 + upside_to_20) / (1 + risk_to_low)
  - 最终按 rank_score、probability_score 降序排序。

  ## Acceptance

  - 更新后确认筛选结果 latest_date 应为最新交易日，优先为 2026-06-05。
  - 排行文件非空，且行数应与筛选结果一致或能解释差异。
  - 终端输出 Top 20，并保存完整 Excel 排行和快照。
  - 保持默认参数：7% 趋势反转、27%-33% 回撤、当前距低点不超过 7%、历史至少 1 次反弹达到 20%、排除 ST。