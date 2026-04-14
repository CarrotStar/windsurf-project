import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import Config, SymbolConfig
from database import Database
from exchange_client import ExchangeClient, Order
from google_sheets_logger import GoogleSheetsLogger
from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class GridState:
    levels: list[float] = field(default_factory=list)
    open_orders: dict[str, Order] = field(default_factory=dict)
    total_profit: float = 0.0
    total_trades: int = 0
    start_time: datetime = field(default_factory=datetime.now)
    initial_price: float = 0.0
    recovered: bool = False  # True when state was loaded from DB
    net_position_amount: float = 0.0
    avg_entry_price: float = 0.0   # weighted average cost for net_position (#16)
    # Funding rate tracking (futures only)
    total_funding_profit: float = 0.0
    next_funding_ts_ms: float = 0.0
    last_known_funding_rate: float = 0.0


_DB_WRITE_INTERVAL = 300     # write stats to DB at most every 5 minutes
_SHEETS_UPDATE_INTERVAL = 600  # update Sheets summary at most every 10 minutes


class GridBot:
    """
    Grid Trading Bot

    Strategy:
      - Divide the price range [LOWER_PRICE, UPPER_PRICE] into GRID_COUNT equal intervals.
      - Place BUY limit orders at levels below the current price.
      - Place SELL limit orders at levels above the current price.
      - When a BUY fills → place a SELL one level higher (captures spread as profit).
      - When a SELL fills → place a BUY one level lower (re-enters the grid).

    Resilience:
      - All orders and cumulative stats are persisted to AWS RDS PostgreSQL after every change.
      - On startup the bot attempts to recover the previous session automatically
        if the grid config (symbol / range / grid count) is unchanged.

    Risk Management:
      - If cumulative loss exceeds MAX_LOSS_PCT% of the investment the bot stops
        itself and sends a Telegram alert.
    """

    def __init__(
        self,
        config: Config,
        sym: SymbolConfig,
        exchange: ExchangeClient,
        telegram: TelegramNotifier,
        sheets: GoogleSheetsLogger,
        db: Database,
        portfolio_risk=None,
    ):
        self.config = config    # global settings (MAX_LOSS_PCT, CHECK_INTERVAL, etc.)
        self.sym = sym          # per-symbol settings (symbol, lower/upper price, etc.)
        self.exchange = exchange
        self.telegram = telegram
        self.sheets = sheets
        self.db = db
        self._portfolio_risk = portfolio_risk
        self.state = GridState()
        self.running = False
        self._last_summary_at: float = 0.0
        self._summary_interval: int = 3600  # hourly
        self._out_of_range_notified: bool = False
        self._last_db_write_at: float = 0.0
        self._last_sheets_update_at: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.running = True

        # Try to recover previous session first
        recovered = self._try_recover()

        if not recovered:
            logger.info("[%s] Fetching current price to initialise fresh grid…", self.sym.symbol)
            current_price = self.exchange.get_current_price()
            if not (self.sym.lower_price < current_price < self.sym.upper_price):
                logger.warning(
                    "[%s] Current price %.4f is OUTSIDE grid range [%.4f, %.4f]. "
                    "Orders will be placed only on the side that is within range.",
                    self.sym.symbol, current_price, self.sym.lower_price, self.sym.upper_price,
                )
            self._setup_grid(current_price)

        self._last_summary_at = time.time()

        try:
            while self.running:
                self._tick()
                time.sleep(self.config.CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received — stopping bot…")
        finally:
            self._stop()

    def _stop(self) -> None:
        self.running = False
        logger.info("Cancelling %d open order(s)…", len(self.state.open_orders))
        for order_id in list(self.state.open_orders):
            self.exchange.cancel_order(order_id)
            self.db.mark_order_cancelled(order_id, self.sym.symbol)

        runtime = _runtime(self.state.start_time)
        msg = (
            f"🛑 *Bot Stopped*\n"
            f"Total Trades : {self.state.total_trades}\n"
            f"Total Profit : ${self.state.total_profit:,.6f}\n"
            f"Runtime      : {runtime}"
        )
        self.telegram.send_message(msg)
        self.sheets.log_bot_event("BOT_STOPPED", msg)
        logger.info(
            "Bot stopped. Total profit: %.6f | Trades: %d",
            self.state.total_profit, self.state.total_trades,
        )

    # ------------------------------------------------------------------
    # State recovery
    # ------------------------------------------------------------------

    def _try_recover(self) -> bool:
        """Load previous state from DB if grid config matches. Returns True on success."""
        if not self.db.config_matches(
            self.sym.symbol,
            self.sym.lower_price,
            self.sym.upper_price,
            self.sym.grid_count,
        ):
            logger.info("[%s] No matching saved state — starting fresh grid", self.sym.symbol)
            self.db.clear_symbol(self.sym.symbol)
            return False

        saved = self.db.load_state(self.sym.symbol)
        open_rows = self.db.load_open_orders(self.sym.symbol)

        if not saved or not open_rows:
            logger.info("[%s] Saved state incomplete — starting fresh grid", self.sym.symbol)
            self.db.clear_symbol(self.sym.symbol)
            return False

        # Check for mode mismatch (paper vs live) before restoring
        for row in open_rows:
            is_paper = str(row["id"]).startswith("paper_")
            if self.config.PAPER_TRADING != is_paper:
                logger.warning("[%s] Trading mode switched (paper vs live) since last run. Starting fresh grid.", self.sym.symbol)
                self.db.clear_symbol(self.sym.symbol)
                return False

        # Restore state
        self.state.total_profit = float(saved["total_profit"])
        self.state.total_trades = int(saved["total_trades"])
        self.state.initial_price = float(saved["initial_price"])
        st = saved["start_time"]
        self.state.start_time = st if isinstance(st, datetime) else datetime.fromisoformat(str(st))
        self.state.levels = self._calculate_levels()
        self.state.recovered = True

        # Recover futures-specific state
        if self.exchange.is_futures:
            self.state.total_funding_profit = float(saved.get("total_funding_profit", 0.0))
            self.state.next_funding_ts_ms = float(saved.get("next_funding_ts_ms", 0.0))
            self.state.last_known_funding_rate = float(saved.get("last_known_funding_rate", 0.0))
            self.state.net_position_amount = self.db.get_net_filled_amount(self.sym.symbol)
            logger.info("[%s] Recovered net position: %.6f", self.sym.symbol, self.state.net_position_amount)

        for row in open_rows:
            order = Order(
                id=row["id"],
                order_type=row["order_type"],
                price=float(row["price"]),
                amount=float(row["amount"]),
                level_index=int(row["level_index"]),
                status="open",
            )
            self.state.open_orders[order.id] = order
            self.exchange.restore_order(order)

        logger.info(
            "[%s] State recovered — %d open orders | profit=%.6f | trades=%d",
            self.sym.symbol, len(self.state.open_orders), self.state.total_profit, self.state.total_trades,
        )
        msg = (
            f"🔄 *Bot Recovered from Previous Session*\n"
            f"Symbol       : `{self.sym.symbol}`\n"
            f"Open Orders  : `{len(self.state.open_orders)}`\n"
            f"Total Trades : `{self.state.total_trades}`\n"
            f"Total Profit : `${self.state.total_profit:,.6f}`\n"
            f"Mode         : {'📝 Paper Trading' if self.config.PAPER_TRADING else '💰 Live Trading'}"
        )
        self.telegram.send_message(msg)
        self.sheets.log_bot_event(f"BOT_RECOVERED [{self.sym.symbol}]", msg)
        return True

    # ------------------------------------------------------------------
    # Main tick (called every CHECK_INTERVAL seconds)
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            current_price = self.exchange.get_current_price()
            logger.info(
                "[%s] Price: %.4f | Open orders: %d | Profit: %.6f",
                self.sym.symbol, current_price, len(self.state.open_orders), self.state.total_profit,
            )

            # Check and process filled orders
            filled_orders = self.exchange.check_filled_orders(
                self.state.open_orders, current_price
            )
            for order in filled_orders:
                self.state.open_orders.pop(order.id, None)
                self._handle_fill(order)

            # Handle futures funding fees
            self._check_funding(current_price)

            # Risk management check
            if self._risk_limit_breached():
                return

            # Warn if price has left the grid range
            self._check_range(current_price)

            # Periodic hourly summary
            if time.time() - self._last_summary_at >= self._summary_interval:
                self._send_summary(current_price)
                self._last_summary_at = time.time()

            # Portfolio risk check (#17)
            if self._portfolio_risk and self._portfolio_risk.report_profit(
                self.sym.symbol, self.state.total_profit
            ):
                self._portfolio_risk.stop_all(self.telegram, self.sheets)
                return

            # Self-heal: if no open orders remain, re-fill the grid
            if self.running and not self.state.open_orders:
                logger.warning(
                    "[%s] No open orders detected — re-filling grid at price %.4f",
                    self.sym.symbol, current_price,
                )
                self._refill_grid(current_price)

            now = time.time()

            # Persist stats to DB — always after fills, otherwise throttled (#DB throttle)
            if filled_orders or (now - self._last_db_write_at >= _DB_WRITE_INTERVAL):
                self.db.update_stats(
                    symbol=self.sym.symbol,
                    total_profit=self.state.total_profit,
                    total_trades=self.state.total_trades,
                    total_funding_profit=self.state.total_funding_profit,
                    next_funding_ts_ms=self.state.next_funding_ts_ms,
                    last_known_funding_rate=self.state.last_known_funding_rate,
                )
                self._last_db_write_at = now

            # Update Google Sheets summary — throttled to once 10 minutes
            if now - self._last_sheets_update_at >= _SHEETS_UPDATE_INTERVAL:
                self.sheets.update_summary({
                    "symbol": self.sym.symbol,
                    "current_price": current_price,
                    "open_orders": len(self.state.open_orders),
                    "total_trades": self.state.total_trades,
                    "total_profit": self.state.total_profit,
                    "runtime": _runtime(self.state.start_time),
                })
                self._last_sheets_update_at = now

        except Exception as exc:
            logger.error("Error during tick: %s", exc, exc_info=True)
            self.telegram.send_message(f"❌ *Bot Error*\n`{exc}`")
            self.sheets.log_bot_event(f"ERROR [{self.sym.symbol}]", str(exc))

    # ------------------------------------------------------------------
    # Grid setup
    # ------------------------------------------------------------------

    def _setup_grid(self, current_price: float) -> None:
        # Check if investment per grid meets the exchange minimum notional
        amount_per_grid = self.sym.investment / self.sym.grid_count
        min_cost = self.exchange.get_market_min_cost()
        min_amount = self.exchange.get_market_min_amount()

        if min_cost and amount_per_grid < min_cost:
            err_msg = (
                f"❌ *Grid Setup Failed*\n"
                f"Symbol: `{self.sym.symbol}`\n"
                f"Your investment per grid (`${amount_per_grid:,.2f}`) is less than "
                f"the exchange minimum required notional (`${min_cost:,.2f}`).\n"
                f"👉 *Fix*: Increase `INVESTMENT` or decrease `GRID_COUNT`."
            )
            logger.error("[%s] Grid setup failed: amount per grid %.2f < min cost %.2f", self.sym.symbol, amount_per_grid, min_cost)
            self.telegram.send_message(err_msg)
            self.sheets.log_bot_event(f"SETUP_FAILED [{self.sym.symbol}]", err_msg)
            self.running = False
            return

        if min_amount:
            sample_price = (self.sym.lower_price + self.sym.upper_price) / 2
            raw_amount = amount_per_grid / sample_price
            fmt_amount = self.exchange.format_amount(raw_amount)
            if fmt_amount < min_amount:
                min_investment = min_amount * sample_price * self.sym.grid_count
                err_msg = (
                    f"❌ *Grid Setup Failed*\n"
                    f"Symbol: `{self.sym.symbol}`\n"
                    f"Amount per grid (`{fmt_amount:.6f}`) is below the exchange minimum "
                    f"quantity (`{min_amount}`).\n"
                    f"👉 *Fix*: Increase `INVESTMENT` to at least `${min_investment:,.2f}` "
                    f"or decrease `GRID_COUNT`."
                )
                logger.error(
                    "[%s] Grid setup failed: formatted amount %.6f < min amount %.6f "
                    "(need investment >= %.2f for %d grids at price ~%.2f)",
                    self.sym.symbol, fmt_amount, min_amount, min_investment,
                    self.sym.grid_count, sample_price,
                )
                self.telegram.send_message(err_msg)
                self.sheets.log_bot_event(f"SETUP_FAILED [{self.sym.symbol}]", err_msg)
                self.running = False
                return

        self.state.levels = self._calculate_levels()
        self.state.initial_price = current_price
        logger.info("[%s] Grid levels: %s", self.sym.symbol, [f"{p:.2f}" for p in self.state.levels])

        placed = 0
        for i, level in enumerate(self.state.levels[:-1]):
            amount = self._order_amount(level)
            if level < current_price:
                order = self.exchange.place_order("buy", level, amount, i)
            elif level > current_price:
                order = self.exchange.place_order("sell", level, amount, i)
            else:
                continue
            if order:
                self.state.open_orders[order.id] = order
                self.db.upsert_order(order, self.sym.symbol)
                placed += 1

        # Persist initial bot state
        self.db.save_state(
            symbol=self.sym.symbol,
            lower_price=self.sym.lower_price,
            upper_price=self.sym.upper_price,
            grid_count=self.sym.grid_count,
            investment=self.sym.investment,
            initial_price=current_price,
            total_profit=0.0,
            total_trades=0,
            start_time=self.state.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            total_funding_profit=0.0,
            next_funding_ts_ms=0.0,
            last_known_funding_rate=0.0,
        )

        step = self._grid_step()
        loss_limit = self.sym.investment * self.config.MAX_LOSS_PCT / 100
        msg = (
            f"🤖 *Grid Bot Started*\n"
            f"Symbol       : `{self.sym.symbol}`\n"
            f"Range        : `{self.sym.lower_price:,.2f}` — `{self.sym.upper_price:,.2f}`\n"
            f"Grid Step    : `{step:,.4f}` ({self.sym.grid_count} grids)\n"
            f"Investment   : `${self.sym.investment:,.2f}`\n"
            f"Stop-Loss    : `${loss_limit:,.2f}` ({self.config.MAX_LOSS_PCT}% of investment)\n"
            f"Current Price: `{current_price:,.4f}`\n"
            f"Orders Placed: `{placed}`\n"
            f"Mode         : {'📝 Paper Trading' if self.config.PAPER_TRADING else '💰 Live Trading'}"
        )
        self.telegram.send_message(msg)
        self.sheets.log_bot_event(f"BOT_STARTED [{self.sym.symbol}]", msg)
        logger.info("[%s] Grid setup complete — %d orders placed", self.sym.symbol, placed)

    # ------------------------------------------------------------------
    # Order fill handler
    # ------------------------------------------------------------------

    def _handle_fill(self, order: Order) -> None:
        step = self._grid_step()
        profit = 0.0

        fee_rate = getattr(self.config, "FEE_RATE", 0.001)

        if order.order_type == "buy":
            old_pos = self.state.net_position_amount
            self.state.net_position_amount += order.amount
            # Update weighted average entry price (#16)
            fill_px = order.filled_price or order.price
            if self.state.net_position_amount > 0:
                self.state.avg_entry_price = (
                    (old_pos * self.state.avg_entry_price + order.amount * fill_px)
                    / self.state.net_position_amount
                )
            sell_price = self.exchange.format_price(order.price + step)
            if sell_price <= self.sym.upper_price:
                sell_amount = self._order_amount(sell_price)
                new_order = self.exchange.place_order(
                    "sell", sell_price, sell_amount, order.level_index + 1
                )
                if new_order:
                    self.state.open_orders[new_order.id] = new_order
                    self.db.upsert_order(new_order, self.sym.symbol)

        elif order.order_type == "sell":
            self.state.net_position_amount -= order.amount
            if self.state.net_position_amount <= 0:
                self.state.avg_entry_price = 0.0
            buy_price = self.exchange.format_price(order.price - step)
            # Gross profit minus both-side fees (#14)
            gross = (order.price - buy_price) * order.amount
            fee = (buy_price + order.price) * order.amount * fee_rate
            profit = gross - fee
            self.state.total_profit += profit

            if buy_price >= self.sym.lower_price:
                buy_amount = self._order_amount(buy_price)
                new_order = self.exchange.place_order(
                    "buy", buy_price, buy_amount, order.level_index - 1
                )
                if new_order:
                    self.state.open_orders[new_order.id] = new_order
                    self.db.upsert_order(new_order, self.sym.symbol)

        # Mark the filled order in DB
        self.db.upsert_order(order, self.sym.symbol)

        self.state.total_trades += 1
        trade = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": self.sym.symbol,
            "type": order.order_type.upper(),
            "price": order.filled_price,
            "amount": order.amount,
            "value": (order.filled_price or order.price) * order.amount,
            "profit": profit,
            "total_profit": self.state.total_profit,
        }
        self.db.insert_trade(trade, self.sym.symbol)
        self.sheets.log_trade(trade)

        emoji = "🟢" if order.order_type == "sell" else "🔵"
        profit_line = f"\nGrid Profit  : `+${profit:,.6f}`" if profit > 0 else ""
        fee_line = ""
        if order.order_type == "sell" and profit > 0:
            fee_paid = (self.exchange.format_price(order.price - step) + order.price) * order.amount * fee_rate
            fee_line = f" *(net of ${fee_paid:,.6f} fee)*"
        msg = (
            f"{emoji} *Order Filled* — {order.order_type.upper()} `{self.sym.symbol}`\n"
            f"Price        : `{order.filled_price:,.4f}`\n"
            f"Amount       : `{order.amount:.6f}`\n"
            f"Value        : `${(order.filled_price or 0) * order.amount:,.2f}`"
            f"{profit_line}{fee_line}\n"
            f"Total Profit : `${self.state.total_profit:,.6f}`"
        )
        self.telegram.send_message(msg)
        logger.info(
            "[%s] Fill: %s @ %.4f | amount=%.6f | profit=%.6f | total=%.6f | pos=%.4f",
            self.sym.symbol, order.order_type, order.filled_price or 0, order.amount,
            profit, self.state.total_profit, self.state.net_position_amount,
        )

    # ------------------------------------------------------------------
    # Risk management
    # ------------------------------------------------------------------

    def _risk_limit_breached(self) -> bool:
        """Stop the bot if cumulative loss exceeds MAX_LOSS_PCT% of investment."""
        max_loss = self.sym.investment * self.config.MAX_LOSS_PCT / 100
        if self.state.total_profit < -max_loss:
            logger.critical(
                "STOP-LOSS triggered! Loss %.6f exceeds limit %.6f",
                -self.state.total_profit, max_loss,
            )
            msg = (
                f"🚨 *STOP-LOSS Triggered — {self.sym.symbol} Bot Stopping*\n"
                f"Cumulative Loss : `${-self.state.total_profit:,.6f}`\n"
                f"Loss Limit      : `${max_loss:,.2f}` ({self.config.MAX_LOSS_PCT}% of ${self.sym.investment:,.2f})\n"
                f"Action          : Cancelling all orders and stopping."
            )
            self.telegram.send_message(msg)
            self.sheets.log_bot_event("STOP_LOSS_TRIGGERED", msg)
            self.running = False
            return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calculate_levels(self) -> list[float]:
        step = self._grid_step()
        return [
            self.exchange.format_price(self.sym.lower_price + i * step)
            for i in range(self.sym.grid_count + 1)
        ]

    def _grid_step(self) -> float:
        return (self.sym.upper_price - self.sym.lower_price) / self.sym.grid_count

    def _order_amount(self, price: float) -> float:
        amount_per_grid = self.sym.investment / self.sym.grid_count
        return self.exchange.format_amount(amount_per_grid / price)

    def _refill_grid(self, current_price: float) -> None:
        """Re-place grid orders when open_orders is empty (self-healing)."""
        placed = 0
        for i, level in enumerate(self.state.levels[:-1]):
            amount = self._order_amount(level)
            if level < current_price:
                order = self.exchange.place_order("buy", level, amount, i)
            elif level > current_price:
                order = self.exchange.place_order("sell", level, amount, i)
            else:
                continue
            if order:
                self.state.open_orders[order.id] = order
                self.db.upsert_order(order, self.sym.symbol)
                placed += 1

        if placed:
            logger.info("[%s] Grid re-filled — %d orders placed", self.sym.symbol, placed)
            self.telegram.send_message(
                f"🔧 *Grid Re-filled* — `{self.sym.symbol}`\n"
                f"Orders Placed: `{placed}`\n"
                f"Current Price: `{current_price:,.4f}`"
            )
            self.sheets.log_bot_event(f"GRID_REFILLED [{self.sym.symbol}]", f"Re-filled {placed} orders at {current_price:.4f}")
        else:
            logger.error("[%s] Grid re-fill failed — 0 orders placed", self.sym.symbol)

    def _check_range(self, current_price: float) -> None:
        in_range = self.sym.lower_price <= current_price <= self.sym.upper_price
        if not in_range and not self._out_of_range_notified:
            direction = "Below" if current_price < self.sym.lower_price else "Above"
            bound = self.sym.lower_price if current_price < self.sym.lower_price else self.sym.upper_price
            logger.warning("Price %.4f is %s grid bound %.4f", current_price, direction.lower(), bound)
            if getattr(self.config, "AUTO_ADJUST_GRID", False):
                self._adjust_grid(current_price)
            else:
                self.telegram.send_message(
                    f"⚠️ *Price {direction} Grid Range*\n"
                    f"Current : `{current_price:,.4f}`\n"
                    f"{'Lower' if direction == 'Below' else 'Upper'}   : `{bound:,.4f}`\n"
                    f"Consider adjusting the grid or stopping the bot."
                )
                self._out_of_range_notified = True
        elif in_range and self._out_of_range_notified:
            self._out_of_range_notified = False  # Reset when price returns

    def _adjust_grid(self, current_price: float) -> None:
        """Re-centre the grid around current_price keeping the same range width (#13)."""
        half_range = (self.sym.upper_price - self.sym.lower_price) / 2
        new_lower = self.exchange.format_price(current_price - half_range)
        new_upper = self.exchange.format_price(current_price + half_range)
        logger.info(
            "[%s] Auto-adjusting grid [%.2f, %.2f] → [%.2f, %.2f]",
            self.sym.symbol, self.sym.lower_price, self.sym.upper_price, new_lower, new_upper,
        )
        for order_id in list(self.state.open_orders):
            self.exchange.cancel_order(order_id)
            self.db.mark_order_cancelled(order_id, self.sym.symbol)
        self.state.open_orders.clear()
        self.sym.lower_price = new_lower
        self.sym.upper_price = new_upper
        self._out_of_range_notified = False
        self._setup_grid(current_price)
        msg = (
            f"🔄 *Grid Auto-Adjusted* — `{self.sym.symbol}`\n"
            f"New Range: `{new_lower:,.2f}` — `{new_upper:,.2f}`"
        )
        self.telegram.send_message(msg)
        self.sheets.log_bot_event(f"GRID_ADJUSTED [{self.sym.symbol}]", msg)

    def _send_summary(self, current_price: float) -> None:
        runtime = _runtime(self.state.start_time)
        roi = (self.state.total_profit / self.sym.investment) * 100 if self.sym.investment else 0
        # Unrealized PnL (#16)
        unrealized_pnl = 0.0
        if self.state.net_position_amount > 0 and self.state.avg_entry_price > 0:
            unrealized_pnl = self.state.net_position_amount * (current_price - self.state.avg_entry_price)
        total_pnl = self.state.total_profit + unrealized_pnl
        funding_line = ""
        if self.exchange.is_futures:
            funding_line = f"Funding P/L  : `${self.state.total_funding_profit:,.6f}`\n"
        msg = (
            f"📊 *Hourly Summary*\n"
            f"Symbol       : `{self.sym.symbol}`\n"
            f"Current Price: `{current_price:,.4f}`\n"
            f"Open Orders  : `{len(self.state.open_orders)}`\n"
            f"Total Trades : `{self.state.total_trades}`\n"
            f"{funding_line}"
            f"Realized P/L : `${self.state.total_profit:,.6f}` ({roi:+.3f}%)\n"
            f"Unrealized PnL: `${unrealized_pnl:,.6f}` (pos={self.state.net_position_amount:.4f})\n"
            f"Total PnL    : `${total_pnl:,.6f}`\n"
            f"Runtime      : `{runtime}`"
        )
        self.telegram.send_message(msg)
        self.sheets.log_bot_event("HOURLY_SUMMARY", msg)
        logger.info("Hourly summary sent")

    def _check_funding(self, current_price: float) -> None:
        """If futures, check for and apply funding fees."""
        if not self.exchange.is_futures:
            return

        # If we have a pending funding event and its time has passed, apply the fee
        current_ts_ms = time.time() * 1000
        if 0 < self.state.next_funding_ts_ms < current_ts_ms:
            position_value = self.state.net_position_amount * current_price
            
            # Funding profit = - Position Value * Funding Rate
            funding_profit = -1 * position_value * self.state.last_known_funding_rate

            self.state.total_funding_profit += funding_profit
            self.state.total_profit += funding_profit
            
            logger.info(
                "[%s] Funding event processed. Position: %.4f, Rate: %.6f, Profit: %.6f",
                self.sym.symbol, self.state.net_position_amount, self.state.last_known_funding_rate, funding_profit
            )
            self.telegram.send_message(
                f"💸 *Funding Fee* — `{self.sym.symbol}`\n"
                f"Position     : `{self.state.net_position_amount:.4f}`\n"
                f"Funding Rate : `{self.state.last_known_funding_rate * 100:.4f}%`\n"
                f"Profit/Loss  : `${funding_profit:,.6f}`\n"
                f"Total Profit : `${self.state.total_profit:,.6f}`"
            )
            self.state.next_funding_ts_ms = 0  # Mark as processed

        # Always try to get the latest funding info for the *next* event
        try:
            funding_info = self.exchange.fetch_funding_rate()
            if funding_info and funding_info.get("next_time"):
                if funding_info["next_time"] > current_ts_ms:
                    self.state.next_funding_ts_ms = funding_info["next_time"]
                    self.state.last_known_funding_rate = funding_info["rate"]
        except Exception as exc:
            logger.warning("[%s] Could not update funding info: %s", self.sym.symbol, exc)


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _runtime(start: datetime) -> str:
    delta = datetime.now() - start
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"
