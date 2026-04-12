import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import ccxt

# Transient ccxt errors that are safe to retry
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RequestTimeout,
    ccxt.RateLimitExceeded,
)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds

logger = logging.getLogger(__name__)


@dataclass
class Order:
    id: str
    order_type: str          # 'buy' | 'sell'
    price: float
    amount: float
    level_index: int
    status: str = "open"     # 'open' | 'filled' | 'cancelled'
    filled_price: Optional[float] = None
    filled_at: Optional[float] = None


class ExchangeClient:
    """
    Unified interface for both paper trading and live exchange trading.

    Paper trading mode:
      - Fetches real market prices via public API (no keys needed).
      - Simulates order fills locally when price crosses order price.

    Live trading mode:
      - Places and manages real limit orders on the exchange via ccxt.
    """

    def __init__(self, config, symbol: str = ""):
        self.config = config
        self._base_symbol: str = symbol or getattr(config, "SYMBOLS", "").split(",")[0].strip() or "BTC/USDT"
        self.is_futures: bool = getattr(config, "MARKET_TYPE", "spot").lower() == "future"
        # Futures symbols use 'BTC/USDT:USDT' format in ccxt
        self.symbol: str = self._futures_symbol(self._base_symbol) if self.is_futures else self._base_symbol
        self.paper_trading: bool = config.PAPER_TRADING
        self._exchange = self._build_exchange()
        self._paper_orders: dict[str, Order] = {}
        # Set leverage for futures
        if self.is_futures and not self.paper_trading:
            self._set_leverage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _retry_call(self, func, *args, **kwargs):
        """Execute *func* with exponential backoff on transient exchange errors."""
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                delay = _BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Exchange request failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, exc, delay,
                )
                time.sleep(delay)
            except ccxt.BaseError:
                raise  # non-transient errors bubble immediately
        raise last_exc  # type: ignore[misc]

    def get_current_price(self) -> float:
        ticker = self._retry_call(self._exchange.fetch_ticker, self.symbol)
        price = float(ticker["last"])
        logger.debug("Current price %s: %.4f", self.symbol, price)
        return price

    def place_order(
        self,
        order_type: str,
        price: float,
        amount: float,
        level_index: int,
    ) -> Optional[Order]:
        if self.paper_trading:
            return self._paper_place(order_type, price, amount, level_index)
        return self._live_place(order_type, price, amount, level_index)

    def restore_order(self, order: Order) -> None:
        """Re-register a recovered order so paper-trading cancel works correctly."""
        if self.paper_trading:
            self._paper_orders[order.id] = order

    def cancel_order(self, order_id: str) -> bool:
        if self.paper_trading:
            return self._paper_cancel(order_id)
        return self._live_cancel(order_id)

    def check_filled_orders(
        self, open_orders: dict[str, Order], current_price: float
    ) -> list[Order]:
        if self.paper_trading:
            return self._paper_check_fills(open_orders, current_price)
        return self._live_check_fills(open_orders)

    def fetch_funding_rate(self) -> dict:
        """Fetch current funding rate for this symbol.

        Returns dict with:
          - rate: float (e.g. 0.0001 = 0.01%)
          - next_time: float (unix timestamp of next settlement)
        Returns empty dict if not futures or on error.
        """
        if not self.is_futures:
            return {}
        try:
            info = self._retry_call(self._exchange.fetch_funding_rate, self.symbol)
            return {
                "rate": float(info.get("fundingRate", 0) or 0),
                "next_time": float(info.get("fundingTimestamp", 0) or info.get("timestamp", 0) or 0),
            }
        except ccxt.BaseError as exc:
            logger.warning("Failed to fetch funding rate for %s: %s", self.symbol, exc)
            return {}

    def get_market_min_cost(self) -> float:
        """Fetch the minimum order value (notional) from exchange limits."""
        if self.paper_trading:
            return 0.0
        try:
            if not self._exchange.markets:
                self._retry_call(self._exchange.load_markets)
            market = self._exchange.market(self.symbol)
            return float(market.get("limits", {}).get("cost", {}).get("min") or 0.0)
        except ccxt.BaseError as exc:
            logger.warning("Failed to fetch market limits for %s: %s", self.symbol, exc)
            return 0.0

    def format_price(self, price: float) -> float:
        """Format price according to exchange precision rules."""
        try:
            if not self._exchange.markets:
                self._exchange.load_markets()
            return float(self._exchange.price_to_precision(self.symbol, price))
        except ccxt.BaseError as exc:
            logger.debug("Failed to format price for %s: %s", self.symbol, exc)
            return round(price, 4)

    def format_amount(self, amount: float) -> float:
        """Format amount according to exchange precision rules."""
        try:
            if not self._exchange.markets:
                self._exchange.load_markets()
            return float(self._exchange.amount_to_precision(self.symbol, amount))
        except ccxt.BaseError as exc:
            logger.debug("Failed to format amount for %s: %s", self.symbol, exc)
            return round(amount, 6)

    # ------------------------------------------------------------------
    # Paper trading implementation
    # ------------------------------------------------------------------

    def _paper_place(
        self, order_type: str, price: float, amount: float, level_index: int
    ) -> Order:
        order = Order(
            id=f"paper_{uuid.uuid4().hex[:8]}",
            order_type=order_type,
            price=price,
            amount=amount,
            level_index=level_index,
        )
        self._paper_orders[order.id] = order
        logger.debug(
            "Paper order placed: %s %.6f @ %.4f (level %d)",
            order_type, amount, price, level_index,
        )
        return order

    def _paper_cancel(self, order_id: str) -> bool:
        order = self._paper_orders.get(order_id)
        if order and order.status == "open":
            order.status = "cancelled"
            return True
        return False

    def _paper_check_fills(
        self, open_orders: dict[str, Order], current_price: float
    ) -> list[Order]:
        filled: list[Order] = []
        for order in list(open_orders.values()):
            if order.status != "open":
                continue
            should_fill = (
                order.order_type == "buy" and current_price <= order.price
            ) or (
                order.order_type == "sell" and current_price >= order.price
            )
            if should_fill:
                order.status = "filled"
                order.filled_price = order.price
                order.filled_at = time.time()
                filled.append(order)
        return filled

    # ------------------------------------------------------------------
    # Live trading implementation
    # ------------------------------------------------------------------

    def _live_place(
        self, order_type: str, price: float, amount: float, level_index: int
    ) -> Optional[Order]:
        try:
            if order_type == "buy":
                result = self._retry_call(
                    self._exchange.create_limit_buy_order,
                    self.symbol, amount, price,
                )
            else:
                result = self._retry_call(
                    self._exchange.create_limit_sell_order,
                    self.symbol, amount, price,
                )
            logger.info(
                "Live order placed: %s %.6f @ %.4f | id=%s",
                order_type, amount, price, result["id"],
            )
            return Order(
                id=result["id"],
                order_type=order_type,
                price=price,
                amount=amount,
                level_index=level_index,
            )
        except ccxt.BaseError as exc:
            logger.error("Failed to place %s order @ %.4f: %s", order_type, price, exc)
            return None

    def _live_cancel(self, order_id: str) -> bool:
        if order_id.startswith("paper_"):
            logger.warning("Skipping cancellation of paper order in live mode: %s", order_id)
            return False
        try:
            self._retry_call(self._exchange.cancel_order, order_id, self.symbol)
            logger.info("Order cancelled: %s", order_id)
            return True
        except ccxt.BaseError as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def _live_check_fills(self, open_orders: dict[str, Order]) -> list[Order]:
        """Detect fills using a single fetch_open_orders() call.

        Orders that were tracked as open but are no longer present in the
        exchange response are assumed filled. We then call fetch_order()
        individually only for those orders to retrieve the fill price.
        This reduces API calls from O(N) to O(1) + O(filled).
        """
        live = {oid: o for oid, o in open_orders.items() if not oid.startswith("paper_")}
        if not live:
            return []

        try:
            exchange_open = self._retry_call(self._exchange.fetch_open_orders, self.symbol)
            exchange_open_ids = {o["id"] for o in exchange_open}
        except ccxt.BaseError as exc:
            logger.error("Failed to fetch open orders for %s: %s", self.symbol, exc)
            return []

        filled: list[Order] = []
        for order_id, order in live.items():
            if order_id in exchange_open_ids:
                continue  # still open
            try:
                result = self._retry_call(self._exchange.fetch_order, order_id, self.symbol)
                if result["status"] in ("closed", "filled"):
                    order.status = "filled"
                    order.filled_price = float(result.get("average") or result["price"])
                    order.filled_at = time.time()
                    filled.append(order)
            except ccxt.BaseError as exc:
                logger.error("Failed to confirm fill for order %s: %s", order_id, exc)
        return filled

    # ------------------------------------------------------------------
    # Exchange initialisation
    # ------------------------------------------------------------------

    @staticmethod
    def _futures_symbol(symbol: str) -> str:
        """Convert 'BTC/USDT' → 'BTC/USDT:USDT' for ccxt futures."""
        if ":" in symbol:
            return symbol  # already futures format
        quote = symbol.split("/")[-1] if "/" in symbol else "USDT"
        return f"{symbol}:{quote}"

    def _set_leverage(self) -> None:
        leverage = getattr(self.config, "LEVERAGE", 1)
        if leverage < 1:
            leverage = 1
        try:
            self._exchange.set_leverage(leverage, self.symbol)
            logger.info("Leverage set to %dx for %s", leverage, self.symbol)
        except ccxt.BaseError as exc:
            logger.warning("Failed to set leverage for %s: %s", self.symbol, exc)

    def _build_exchange(self) -> ccxt.Exchange:
        exchange_cls = getattr(ccxt, self.config.EXCHANGE.lower())
        market_type = "future" if self.is_futures else "spot"
        params: dict = {
            "options": {
                "defaultType": market_type,
                "fetchCurrencies": not self.paper_trading,  # avoid authenticated SAPI call in paper mode
            },
            "enableRateLimit": True,
        }

        # Only attach API keys for live trading with valid (non-placeholder) keys
        valid_key = (
            self.config.API_KEY
            and self.config.API_KEY not in ("", "your_exchange_api_key")
        )
        if not self.paper_trading and valid_key:
            params["apiKey"] = self.config.API_KEY
            params["secret"] = self.config.API_SECRET

        exchange: ccxt.Exchange = exchange_cls(params)

        # Restrict market loading to only the required type.
        # ccxt Binance's load_markets() calls multiple authenticated SAPI endpoints
        # (margin/allPairs, capital/config/getall) when loading all market types.
        # We only need spot OR linear futures, so skip all others.
        if self.is_futures:
            exchange.options['fetchMarkets'] = ['linear']
        else:
            exchange.options['fetchMarkets'] = ['spot']
        exchange.options['fetchCurrencies'] = False
        exchange.has['fetchCurrencies'] = False

        if not self.paper_trading and self.config.TESTNET:
            if self.config.EXCHANGE.lower() == "binance" and self.is_futures:
                if hasattr(exchange, "enable_demo_trading"):
                    exchange.enable_demo_trading(True)
                    logger.info("Binance futures demo trading mode enabled")
                else:
                    logger.warning("Binance demo trading mode not found in this ccxt version. Consider upgrading ccxt.")
            else:
                if hasattr(exchange, "set_sandbox_mode"):
                    exchange.set_sandbox_mode(True)
                    logger.info("Exchange sandbox/testnet mode enabled")
        return exchange
