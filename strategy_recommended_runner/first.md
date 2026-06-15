 执行步骤

 1. 删除 request_state.csv

 rm a_stock_data/meta/request_state.csv

 这让 update_a_stock_pytdx.py 认为所有股票都需要重新拉取（load_request_state() 返回空字典，skip 逻辑不触发）。parquet 目录已经是空的，无需额外清理。

 2. 重新拉取全量数据

 python3 strategy_recommended_runner/scripts/update_a_stock_pytdx.py

 脚本会对全部 ~5200 只股票从 2016-01-01 开始拉取，多线程并发，预计耗时较长（参考历史约 15~30 分钟）。

 3. 验证 parquet 文件存在

 ls a_stock_data/parquet/ | wc -l

 应看到几千个 .parquet 文件。

4. 重新运行完整日常流程

 ./strategy_recommended_runner/run_daily_rebound_top3.sh

 此时 Step 1 会因 request_state.csv 已是今天而全部 skip（速度极快），Step 2 和 Step 3 正常执行。