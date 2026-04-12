"""
Grid Trading Backtester — Core Engine

Simulates grid trading on historical OHLCV data using the same logic as
the live GridBot in grid_bot.py, but adds fee calculation, unrealized PnL,
and max-drawdown tracking.
"""

import csv
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt

from backtest_config import BacktestConfig

logger = logging.getLogger(__name__)

# ccxt fetch_ohlcv returns at most this many candles per request
_OHLCV_LIMIT = 1000


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class BacktestOrder:
    id: str
    order_type: str       # 'buy' | 'sell'
    price: float
    amount: float
    level_index: int
    status: str = "open"  # 'open' | 'filled' | 'cancelled'
    filled_price: Optional[float] = None
    filled_at: Optional[datetime] = None
    fee: float = 0.0


@dataclass
class TradeRecord:
    timestamp: datetime
    order_type: str
    price: float
    amount: float
    value: float
    fee: float
    gross_profit: float
    net_profit: float
    cumulative_profit: float
    position: float


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[TradeRecord] = field(default_factory=list)
    total_trades: int = 0
    gross_profit: float = 0.0
    total_fees: float = 0.0
    net_profit: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    roi_pct: float = 0.0
    win_rate: float = 0.0
    avg_profit_per_trade: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    price_start: float = 0.0
    price_end: float = 0.0
    price_change_pct: float = 0.0
    candles_processed: int = 0
    stopped_by_risk: bool = False
    runtime_seconds: float = 0.0

    def summary(self) -> str:
        sep = "-" * 50
        return (
            f"\n{sep}\n"
            f"  BACKTEST RESULT — {self.config.symbol}\n"
            f"{sep}\n"
            f"Period         : {self.config.start_date} → {self.config.end_date}\n"
            f"Candles        : {self.candles_processed:,}\n"
            f"Price          : {self.price_start:,.2f} → {self.price_end:,.2f} ({self.price_change_pct:+.2f}%)\n"
            f"\n"
            f"Total Trades   : {self.total_trades}\n"
            f"Gross Profit   : ${self.gross_profit:,.6f}\n"
            f"Total Fees     : ${self.total_fees:,.6f}\n"
            f"Net Profit     : ${self.net_profit:,.6f}\n"
            f"ROI            : {self.roi_pct:+.3f}%\n"
            f"\n"
            f"Win Rate       : {self.win_rate:.1f}%\n"
            f"Avg Profit/Trade: ${self.avg_profit_per_trade:,.6f}\n"
            f"Max Drawdown   : ${self.max_drawdown:,.6f} ({self.max_drawdown_pct:.2f}%)\n"
            f"\n"
            f"Unrealized PnL : ${self.unrealized_pnl:,.6f}\n"
            f"Total PnL      : ${self.total_pnl:,.6f}\n"
            f"\n"
            f"Risk Stopped   : {'Yes' if self.stopped_by_risk else 'No'}\n"
            f"Runtime        : {self.runtime_seconds:.1f}s\n"
            f"{sep}"
        )


# ------------------------------------------------------------------
# Backtester
# ------------------------------------------------------------------

class Backtester:
    """Simulate grid trading on historical OHLCV candles."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self._open_orders: dict[str, BacktestOrder] = {}
        self._trades: list[TradeRecord] = []
        self._levels: list[float] = []
        self._net_profit: float = 0.0
        self._gross_profit: float = 0.0
        self._total_fees: float = 0.0
        self._total_trades: int = 0
        self._net_position: float = 0.0        # net coin amount held
        self._peak_profit: float = 0.0
        self._max_drawdown: float = 0.0
        self._stopped_by_risk: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        t0 = time.time()
        logger.info("Backtest started\n%s", self.config.summary())

        candles = self._fetch_candles()
        if not candles:
            logger.error("No candle data fetched — aborting")
            return self._build_result([], 0.0)

        first_close = candles[0][4]
        self._setup_grid(first_close)

        for candle in candles:
            if self._stopped_by_risk:
                break
            self._process_candle(candle)

        last_close = candles[-1][4]
        result = self._build_result(candles, time.time() - t0)
        logger.info(result.summary())
        return result

    # ------------------------------------------------------------------
    # Data fetching (paginated)
    # ------------------------------------------------------------------

    def _fetch_candles(self) -> list[list]:
        """Fetch OHLCV candles from exchange via ccxt public API (paginated)."""
        exchange_cls = getattr(ccxt, self.config.exchange.lower())
        exchange: ccxt.Exchange = exchange_cls({"enableRateLimit": True})

        symbol = self.config.symbol
        if self.config.market_type == "future":
            quote = symbol.split("/")[-1] if "/" in symbol else "USDT"
            symbol = f"{symbol}:{quote}" if ":" not in symbol else symbol

        since_ms = int(datetime.fromisoformat(self.config.start_date).replace(
            tzinfo=timezone.utc
        ).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(self.config.end_date).replace(
            tzinfo=timezone.utc
        ).timestamp() * 1000)

        all_candles: list[list] = []
        logger.info(
            "Fetching %s %s candles from %s (%s → %s)…",
            symbol, self.config.timeframe, self.config.exchange,
            self.config.start_date, self.config.end_date,
        )

        while since_ms < end_ms:
            try:
                batch = exchange.fetch_ohlcv(
                    symbol,
                    timeframe=self.config.timeframe,
                    since=since_ms,
                    limit=_OHLCV_LIMIT,
                )
            except ccxt.BaseError as exc:
                logger.error("Failed to fetch OHLCV: %s", exc)
                break

            if not batch:
                break

            for candle in batch:
                if candle[0] >= end_ms:
                    break
                all_candles.append(candle)
            else:
                # Move forward only if no early break
                since_ms = batch[-1][0] + 1
                continue
            break  # break outer loop if we hit end_ms

        logger.info("Fetched %d candles", len(all_candles))
        return all_candles

    # ------------------------------------------------------------------
    # Grid setup
    # ------------------------------------------------------------------

    def _setup_grid(self, current_price: float) -> None:
        step = self._grid_step()
        self._levels = [
            round(self.config.lower_price + i * step, 8)
            for i in range(self.config.grid_count + 1)
        ]
        logger.info("Grid levels: %s", [f"{p:.2f}" for p in self._levels])

        placed = 0
        for i, level in enumerate(self._levels[:-1]):
            amount = self._order_amount(level)
            if level < current_price:
                self._place_order("buy", level, amount, i)
                placed += 1
            elif level > current_price:
                self._place_order("sell", level, amount, i)
                placed += 1
        logger.info("Grid setup: %d orders placed (price=%.2f)", placed, current_price)

    # ------------------------------------------------------------------
    # Candle processing
    # ------------------------------------------------------------------

    def _process_candle(self, candle: list) -> None:
        """Check all open orders against a single OHLCV candle [ts, O, H, L, C, V]."""
        ts_ms, open_, high, low, close, volume = candle
        candle_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        for order in list(self._open_orders.values()):
            if order.status != "open":
                continue

            filled = False
            if order.order_type == "buy" and low <= order.price:
                filled = True
            elif order.order_type == "sell" and high >= order.price:
                filled = True

            if filled:
                order.status = "filled"
                order.filled_price = order.price
                order.filled_at = candle_dt
                order.fee = order.price * order.amount * self.config.fee_rate
                del self._open_orders[order.id]
                self._handle_fill(order, candle_dt)

                if self._stopped_by_risk:
                    return

    def _handle_fill(self, order: BacktestOrder, ts: datetime) -> None:
        step = self._grid_step()
        gross_profit = 0.0

        if order.order_type == "buy":
            self._net_position += order.amount
            sell_price = round(order.price + step, 8)
            if sell_price <= self.config.upper_price:
                sell_amount = self._order_amount(sell_price)
                self._place_order("sell", sell_price, sell_amount, order.level_index + 1)

        elif order.order_type == "sell":
            self._net_position -= order.amount
            buy_price = round(order.price - step, 8)
            gross_profit = (order.price - buy_price) * order.amount
            if buy_price >= self.config.lower_price:
                buy_amount = self._order_amount(buy_price)
                self._place_order("buy", buy_price, buy_amount, order.level_index - 1)

        net_profit = gross_profit - order.fee
        self._gross_profit += gross_profit
        self._total_fees += order.fee
        self._net_profit += net_profit
        self._total_trades += 1

        # Track drawdown
        if self._net_profit > self._peak_profit:
            self._peak_profit = self._net_profit
        drawdown = self._peak_profit - self._net_profit
        if drawdown > self._max_drawdown:
            self._max_drawdown = drawdown

        # Record trade
        self._trades.append(TradeRecord(
            timestamp=ts,
            order_type=order.order_type,
            price=order.price,
            amount=order.amount,
            value=order.price * order.amount,
            fee=order.fee,
            gross_profit=gross_profit,
            net_profit=net_profit,
            cumulative_profit=self._net_profit,
            position=self._net_position,
        ))

        # Risk check
        max_loss = self.config.investment * self.config.max_loss_pct / 100
        if self._net_profit < -max_loss:
            logger.warning(
                "STOP-LOSS triggered at %s — loss $%.4f exceeds limit $%.2f",
                ts, -self._net_profit, max_loss,
            )
            self._stopped_by_risk = True

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    def _build_result(self, candles: list[list], runtime: float) -> BacktestResult:
        price_start = candles[0][4] if candles else 0.0
        price_end = candles[-1][4] if candles else 0.0
        price_change_pct = ((price_end - price_start) / price_start * 100) if price_start else 0.0

        # Unrealized PnL: value of remaining net position at final price
        unrealized_pnl = self._net_position * price_end if candles else 0.0
        # Subtract cost basis: for simplicity, approximate cost as net_position * avg grid price
        if self._net_position > 0 and candles:
            # Simple approximation: cost = net_position * (lower + upper) / 2
            # More accurate: track average entry price
            avg_grid = (self.config.lower_price + self.config.upper_price) / 2
            unrealized_pnl = self._net_position * (price_end - avg_grid)

        total_pnl = self._net_profit + unrealized_pnl

        sell_trades = [t for t in self._trades if t.order_type == "sell"]
        winning = [t for t in sell_trades if t.net_profit > 0]
        win_rate = (len(winning) / len(sell_trades) * 100) if sell_trades else 0.0

        return BacktestResult(
            config=self.config,
            trades=self._trades,
            total_trades=self._total_trades,
            gross_profit=self._gross_profit,
            total_fees=self._total_fees,
            net_profit=self._net_profit,
            max_drawdown=self._max_drawdown,
            max_drawdown_pct=(self._max_drawdown / self.config.investment * 100) if self.config.investment else 0.0,
            roi_pct=(self._net_profit / self.config.investment * 100) if self.config.investment else 0.0,
            win_rate=win_rate,
            avg_profit_per_trade=(self._net_profit / self._total_trades) if self._total_trades else 0.0,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            price_start=price_start,
            price_end=price_end,
            price_change_pct=price_change_pct,
            candles_processed=len(candles),
            stopped_by_risk=self._stopped_by_risk,
            runtime_seconds=runtime,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _grid_step(self) -> float:
        return (self.config.upper_price - self.config.lower_price) / self.config.grid_count

    def _order_amount(self, price: float) -> float:
        amount_per_grid = self.config.investment / self.config.grid_count
        return round(amount_per_grid / price, 6)

    def _place_order(self, order_type: str, price: float, amount: float, level_index: int) -> None:
        order = BacktestOrder(
            id=f"bt_{uuid.uuid4().hex[:8]}",
            order_type=order_type,
            price=price,
            amount=amount,
            level_index=level_index,
        )
        self._open_orders[order.id] = order


# ------------------------------------------------------------------
# Export functions
# ------------------------------------------------------------------

def export_trades_csv(result: BacktestResult, filepath: str) -> None:
    """Write all trades to a CSV file."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Timestamp", "Type", "Price", "Amount", "Value",
            "Fee", "Gross Profit", "Net Profit", "Cumulative Profit", "Position",
        ])
        for t in result.trades:
            writer.writerow([
                t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                t.order_type.upper(),
                f"{t.price:.4f}",
                f"{t.amount:.6f}",
                f"{t.value:.4f}",
                f"{t.fee:.6f}",
                f"{t.gross_profit:.6f}",
                f"{t.net_profit:.6f}",
                f"{t.cumulative_profit:.6f}",
                f"{t.position:.6f}",
            ])
    logger.info("Trades exported to %s (%d rows)", path, len(result.trades))


def export_summary_txt(result: BacktestResult, filepath: str) -> None:
    """Write backtest summary to a text file."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = (
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n"
        f"=== CONFIGURATION ===\n"
        f"{result.config.summary()}\n"
        f"\n"
        f"=== RESULTS ===\n"
        f"{result.summary()}\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Summary exported to %s", path)
