from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime


def run_command(command: list[str], skip: bool = False) -> None:
    if skip:
        print(f"跳过: {' '.join(command)}")
        return

    print(f"运行: {' '.join(command)}")
    subprocess.run(command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日筛选、排行、保存记录和验证历史快照。")
    parser.add_argument("--skip-update", action="store_true", help="跳过 BaoStock 数据更新")
    parser.add_argument("--skip-verify-all", action="store_true", help="跳过批量验证历史快照")
    parser.add_argument("--suffix", default=None, help="保存快照后缀；默认 daily")
    parser.add_argument("--charts", type=int, default=5, help="筛选脚本生成图表数量，默认 5")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    python = sys.executable
    suffix = args.suffix or "daily"

    run_command([python, "update_a_stock_baostock.py"], skip=args.skip_update)
    run_command([python, "filter_drawdown_rebound_history.py", "--progress", "--charts", str(args.charts)])
    run_command([python, "rank_rebound_candidates.py"])
    run_command([python, "verification_records.py", "save", "--suffix", suffix])
    run_command([python, "verification_records.py", "verify-all"], skip=args.skip_verify_all)
    run_command([python, "verification_records.py", "summary", "--verify-missing"])

    print()
    print(f"每日流程完成: {datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
