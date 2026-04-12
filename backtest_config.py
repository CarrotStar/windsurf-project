"""Configuration for grid trading backtester."""

from dataclasses import dataclass


@dataclass
class BacktestConfig:
    """All parameters needed for a single backtest run."""

    # Market
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1m"
    start_date: str = "2025-01-01"
    end_date: str = "2025-04-01"

    # Grid parameters
    lower_price: float = 60000.0
    upper_price: float = 70000.0
    grid_count: int = 10
    investment: float = 1000.0

    # Fees
    maker_fee_pct: float = 0.1   # %
    taker_fee_pct: float = 0.1   # %

    # Risk
    max_loss_pct: float = 20.0   # stop if loss exceeds this % of investment

    # Market type
    market_type: str = "spot"    # 'spot' or 'future'
    leverage: int = 1

    # Output
    output_dir: str = "backtest_results"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def fee_rate(self) -> float:
        """Average fee rate as a decimal (e.g. 0.1% → 0.001)."""
        return ((self.maker_fee_pct + self.taker_fee_pct) / 2) / 100

    def summary(self) -> str:
        mkt = self.market_type.upper()
        lev = f" (Leverage {self.leverage}x)" if self.market_type == "future" and self.leverage > 1 else ""
        return (
            f"Symbol       : {self.symbol}\n"
            f"Exchange     : {self.exchange}\n"
            f"Timeframe    : {self.timeframe}\n"
            f"Period       : {self.start_date} → {self.end_date}\n"
            f"Market       : {mkt}{lev}\n"
            f"Grid Range   : {self.lower_price:,.2f} — {self.upper_price:,.2f}\n"
            f"Grid Count   : {self.grid_count}\n"
            f"Investment   : ${self.investment:,.2f}\n"
            f"Fee (per side): {self.maker_fee_pct}% maker / {self.taker_fee_pct}% taker\n"
            f"Max Loss     : {self.max_loss_pct}%\n"
            f"Output       : {self.output_dir}/"
        )
