import logging
import signal
import sys
import threading
from pathlib import Path

from config import BASE_DIR, Config, SymbolConfig
from database import Database
from exchange_client import ExchangeClient
from google_sheets_logger import GoogleSheetsLogger
from grid_bot import GridBot
from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Portfolio Risk Manager (#17)
# ------------------------------------------------------------------

class PortfolioRiskManager:
    """Track combined profit/loss across all running bots.

    If MAX_TOTAL_LOSS is set (> 0), stop ALL bots once total loss reaches
    that absolute dollar amount across all symbols.
    """

    def __init__(self, max_total_loss: float):
        self._max_total_loss = max_total_loss
        self._profits: dict[str, float] = {}
        self._bots: list[GridBot] = []
        self._lock = threading.Lock()
        self._triggered = False

    def register(self, bot: "GridBot") -> None:
        with self._lock:
            self._bots.append(bot)

    def report_profit(self, symbol: str, profit: float) -> bool:
        """Update profit for symbol. Returns True if portfolio risk limit is breached."""
        if not self._max_total_loss:
            return False
        with self._lock:
            if self._triggered:
                return True
            self._profits[symbol] = profit
            total_loss = -sum(self._profits.values())
            if total_loss >= self._max_total_loss:
                self._triggered = True
                return True
        return False

    def stop_all(self, telegram: "TelegramNotifier", sheets: "GoogleSheetsLogger") -> None:
        """Signal all bots to stop and send alerts."""
        with self._lock:
            for bot in self._bots:
                bot.running = False
        total_loss = -sum(self._profits.values())
        logger.critical(
            "Portfolio risk limit breached — total loss $%.2f >= limit $%.2f — stopping all bots",
            total_loss, self._max_total_loss,
        )
        msg = (
            f"🚨 *Portfolio Risk Limit Breached*\n"
            f"Combined loss : `${total_loss:,.2f}`\n"
            f"Limit         : `${self._max_total_loss:,.2f}`\n"
            f"Action        : Stopping ALL symbol bots."
        )
        telegram.send_message(msg)
        sheets.log_bot_event("PORTFOLIO_RISK_STOP", msg)


# ------------------------------------------------------------------
# Logging setup (#9)
# ------------------------------------------------------------------

def setup_logging() -> None:
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "grid_bot.log", encoding="utf-8"),
    ]

    if Config.LOG_JSON:
        try:
            from pythonjsonlogger import jsonlogger
            formatter = jsonlogger.JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        except ImportError:
            logger.warning("python-json-logger not installed — falling back to plain text")
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    for h in handlers:
        root.addHandler(h)

    # Silence noisy third-party loggers
    for noisy in ("ccxt", "googleapiclient", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()

    logger.info("=" * 60)
    logger.info("  Grid Trading Bot  —  Starting")
    logger.info("=" * 60)
    logger.info("\n%s", Config.summary())

    # Validate configuration
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    # Shared services (thread-safe)
    telegram = TelegramNotifier(Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID)
    sheets = GoogleSheetsLogger(
        credentials_file=Config.GOOGLE_SHEETS_JSON_KEY,
        sheet_id=Config.GOOGLE_SHEET_ID,
        sheet_name=Config.GOOGLE_SHEET_NAME,
        worksheet_name=Config.GOOGLE_WORKSHEET_NAME,
    )
    db = Database()
    portfolio_risk = PortfolioRiskManager(Config.MAX_TOTAL_LOSS)

    if telegram.enabled:
        if not telegram.test_connection():
            logger.warning("Telegram connection test failed — notifications may not work")

    # Spawn one thread per symbol
    symbol_configs = Config.get_symbol_configs()
    logger.info("Starting %d symbol bot(s): %s", len(symbol_configs), [s.symbol for s in symbol_configs])

    bots: list[GridBot] = []
    threads: list[threading.Thread] = []
    for sym in symbol_configs:
        # Each bot gets its own exchange client, but shares other services.
        # The bot instance is created here so we can control it from the main thread.
        exchange = ExchangeClient(Config, symbol=sym.symbol)
        bot = GridBot(Config, sym, exchange, telegram, sheets, db, portfolio_risk=portfolio_risk)
        portfolio_risk.register(bot)
        bots.append(bot)

        t = threading.Thread(
            target=bot.run,  # The bot's run method is the entry point for the thread
            name=f"bot-{sym.symbol.replace('/', '')}",
        )
        t.start()
        threads.append(t)
        logger.info("Thread started for %s", sym.symbol)

    # --- Graceful Shutdown Handling ---
    # Set up signal handlers for SIGINT (Ctrl+C) and SIGTERM (sent by systemd).
    def shutdown_handler(signum, frame):
        # Use signal.Signals(signum).name to get a readable signal name
        logger.info("Signal %s received — stopping all bots...", signal.Signals(signum).name)
        for bot in bots:
            bot.running = False  # Signal each bot's main loop to exit

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Wait for all bot threads to complete. They will exit when their `bot.running`
    # flag is set to False by the shutdown_handler.
    for t in threads:
        t.join()

    logger.info("All bots have stopped. Exiting.")


if __name__ == "__main__":
    main()
