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


def setup_logging() -> None:
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "grid_bot.log", encoding="utf-8"),
    ]

    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Silence noisy third-party loggers
    for noisy in ("ccxt", "googleapiclient", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

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
        bot = GridBot(Config, sym, exchange, telegram, sheets, db)
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
