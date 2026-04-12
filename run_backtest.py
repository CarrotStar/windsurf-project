#!/usr/bin/env python3
"""
CLI entry point for the Grid Trading Backtester.

Usage:
  # Single run
  python run_backtest.py --symbol BTC/USDT --start 2025-01-01 --end 2025-04-01 \
      --lower 60000 --upper 70000 --grids 10 --investment 1000

  # Optimization mode — sweep grid_count values
  python run_backtest.py --optimize --symbol BTC/USDT \
      --lower 60000 --upper 70000 --investment 1000
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from backtest_config import BacktestConfig
from backtester import Backtester, export_trades_csv, export_summary_txt


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Grid Trading Backtester — test your strategy on historical data",
    )

    # Market
    p.add_argument("--symbol", default="BTC/USDT", help="Trading pair (default: BTC/USDT)")
    p.add_argument("--exchange", default="binance", help="Exchange for OHLCV data (default: binance)")
    p.add_argument("--timeframe", default="5m", help="Candle interval (default: 5m)")
    p.add_argument("--start", default="2025-01-01", help="Start date ISO (default: 2025-01-01)")
    p.add_argument("--end", default="2025-04-01", help="End date ISO (default: 2025-04-01)")

    # Grid
    p.add_argument("--lower", type=float, default=60000, help="Grid lower price")
    p.add_argument("--upper", type=float, default=70000, help="Grid upper price")
    p.add_argument("--grids", type=int, default=10, help="Grid count (default: 10)")
    p.add_argument("--investment", type=float, default=1000, help="Investment in USDT (default: 1000)")

    # Fees / risk
    p.add_argument("--fee", type=float, default=0.1, help="Fee per side in %% (default: 0.1)")
    p.add_argument("--max-loss", type=float, default=20, help="Max loss %% (default: 20)")

    # Market type
    p.add_argument("--market-type", default="spot", choices=["spot", "future"], help="spot or future")
    p.add_argument("--leverage", type=int, default=1, help="Futures leverage (default: 1)")

    # Modes
    p.add_argument("--optimize", action="store_true", help="Run optimization sweep over grid_count values")

    # Output
    p.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")

    return p.parse_args()


def setup_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
    logging.basicConfig(level=log_level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout)])
    for noisy in ("ccxt", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_config(args: argparse.Namespace, grid_count: int | None = None) -> BacktestConfig:
    return BacktestConfig(
        symbol=args.symbol,
        exchange=args.exchange,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        lower_price=args.lower,
        upper_price=args.upper,
        grid_count=grid_count or args.grids,
        investment=args.investment,
        maker_fee_pct=args.fee,
        taker_fee_pct=args.fee,
        max_loss_pct=args.max_loss,
        market_type=args.market_type,
        leverage=args.leverage,
    )


def run_single(args: argparse.Namespace) -> None:
    config = build_config(args)
    bt = Backtester(config)
    result = bt.run()

    # Export results
    output_dir = Path(config.output_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sym = config.symbol.replace("/", "")

    csv_path = output_dir / f"{sym}_{ts}_trades.csv"
    txt_path = output_dir / f"{sym}_{ts}_summary.txt"

    export_trades_csv(result, str(csv_path))
    export_summary_txt(result, str(txt_path))

    print(result.summary())


def run_optimize(args: argparse.Namespace) -> None:
    grid_counts = [5, 8, 10, 15, 20, 25, 30]
    results = []

    # Fetch candles once and reuse — build a baseline to get candles
    base_config = build_config(args, grid_count=grid_counts[0])
    logger = logging.getLogger(__name__)
    logger.info("Optimization mode — testing grid_count values: %s", grid_counts)

    for gc in grid_counts:
        config = build_config(args, grid_count=gc)
        bt = Backtester(config)
        result = bt.run()
        results.append((gc, result))
        logger.info("grid_count=%d → net_profit=$%.4f (ROI=%.3f%%)", gc, result.net_profit, result.roi_pct)

    # Print comparison table
    print("\n" + "=" * 90)
    print("  OPTIMIZATION RESULTS")
    print("=" * 90)
    print(
        f"{'Grids':>6} | {'Trades':>7} | {'Gross Profit':>14} | {'Fees':>10} | "
        f"{'Net Profit':>12} | {'ROI %':>8} | {'Max DD %':>8} | {'Win Rate':>8}"
    )
    print("-" * 90)
    for gc, r in results:
        print(
            f"{gc:>6} | {r.total_trades:>7} | ${r.gross_profit:>12,.4f} | ${r.total_fees:>8,.4f} | "
            f"${r.net_profit:>10,.4f} | {r.roi_pct:>+7.3f}% | {r.max_drawdown_pct:>7.2f}% | {r.win_rate:>6.1f}%"
        )
    print("=" * 90)

    # Find best
    best_gc, best_r = max(results, key=lambda x: x[1].net_profit)
    print(f"\n  Best grid_count = {best_gc} → Net Profit ${best_r.net_profit:,.4f} (ROI {best_r.roi_pct:+.3f}%)\n")

    # Export best result
    output_dir = Path(base_config.output_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sym = base_config.symbol.replace("/", "")

    export_trades_csv(best_r, str(output_dir / f"{sym}_{ts}_optimize_best_trades.csv"))
    export_summary_txt(best_r, str(output_dir / f"{sym}_{ts}_optimize_best_summary.txt"))


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)

    # Validate
    if args.lower >= args.upper:
        print("ERROR: --lower must be less than --upper")
        sys.exit(1)
    if args.grids < 2:
        print("ERROR: --grids must be at least 2")
        sys.exit(1)
    if args.investment <= 0:
        print("ERROR: --investment must be positive")
        sys.exit(1)

    if args.optimize:
        run_optimize(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()
