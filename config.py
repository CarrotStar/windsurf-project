import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Project root (same folder as this file)
BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")


@dataclass
class SymbolConfig:
    """Per-symbol grid trading parameters."""
    symbol: str          # e.g. "ETH/USDT"
    lower_price: float
    upper_price: float
    grid_count: int
    investment: float


class Config:
    # Exchange
    EXCHANGE: str = os.getenv("EXCHANGE", "binance")
    API_KEY: str = os.getenv("API_KEY", "")
    API_SECRET: str = os.getenv("API_SECRET", "")
    TESTNET: bool = os.getenv("TESTNET", "true").lower() == "true"
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # Market type
    MARKET_TYPE: str = os.getenv("MARKET_TYPE", "future")  # 'spot' or 'future'
    LEVERAGE: int = int(os.getenv("LEVERAGE", "1"))          # Futures leverage (1 = no leverage)

    # Grid parameters (defaults used when per-symbol vars are not set)
    SYMBOLS: str = os.getenv("SYMBOLS", os.getenv("SYMBOL", "BTC/USDT"))  # comma-separated
    LOWER_PRICE: float = float(os.getenv("LOWER_PRICE", "25000"))
    UPPER_PRICE: float = float(os.getenv("UPPER_PRICE", "35000"))
    GRID_COUNT: int = int(os.getenv("GRID_COUNT", "10"))
    INVESTMENT: float = float(os.getenv("INVESTMENT", "1000"))

    # Telegram
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Google Sheets
    GOOGLE_SHEETS_JSON_KEY: str = os.getenv(
        "GOOGLE_SHEETS_JSON_KEY",
        os.getenv("GOOGLE_CREDENTIALS_FILE", str(BASE_DIR / "credentials" / "credentials.json")),
    )
    GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")        # Spreadsheet ID from URL (priority)
    GOOGLE_SHEET_NAME: str = os.getenv("GOOGLE_SHEET_NAME", "")     # Spreadsheet name (fallback lookup)
    GOOGLE_WORKSHEET_NAME: str = os.getenv("GOOGLE_WORKSHEET_NAME", "transactions")  # Tab for trades

    # AWS RDS PostgreSQL
    DB_HOST: str = os.getenv("DB_HOST", "")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "gridtrading")
    DB_USER: str = os.getenv("DB_USER", "")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_SSL_MODE: str = os.getenv("DB_SSL_MODE", "require")  # RDS requires SSL by default

    # Risk management
    MAX_LOSS_PCT: float = float(os.getenv("MAX_LOSS_PCT", "20"))  # Stop bot if loss > X% of investment
    MAX_TOTAL_LOSS: float = float(os.getenv("MAX_TOTAL_LOSS", "0"))  # 0 = disabled; stop ALL bots if combined loss exceeds this

    # Trading costs
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.001"))  # 0.1% default maker/taker fee (decimal)

    # Grid behaviour
    AUTO_ADJUST_GRID: bool = os.getenv("AUTO_ADJUST_GRID", "false").lower() == "true"

    # Bot settings
    CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "30"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_JSON: bool = os.getenv("LOG_JSON", "false").lower() == "true"

    @classmethod
    def get_symbol_configs(cls) -> "list[SymbolConfig]":
        """Return a SymbolConfig for each symbol in SYMBOLS.

        Per-symbol overrides use the pattern <PREFIX>_LOWER / _UPPER / _GRID_COUNT / _INVESTMENT
        where PREFIX = symbol with '/' replaced by '_' and uppercased.
        Example: ETH/USDT → ETH_USDT_LOWER=1500
        """
        symbols = [s.strip() for s in cls.SYMBOLS.split(",") if s.strip()]
        result = []
        for sym in symbols:
            prefix = sym.replace("/", "_").upper()  # ETH/USDT → ETH_USDT
            result.append(SymbolConfig(
                symbol=sym,
                lower_price=float(os.getenv(f"{prefix}_LOWER", str(cls.LOWER_PRICE))),
                upper_price=float(os.getenv(f"{prefix}_UPPER", str(cls.UPPER_PRICE))),
                grid_count=int(os.getenv(f"{prefix}_GRID_COUNT", str(cls.GRID_COUNT))),
                investment=float(os.getenv(f"{prefix}_INVESTMENT", str(cls.INVESTMENT))),
            ))
        return result

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        for sc in cls.get_symbol_configs():
            if sc.lower_price >= sc.upper_price:
                errors.append(f"{sc.symbol}: LOWER_PRICE must be less than UPPER_PRICE")
            if sc.grid_count < 2:
                errors.append(f"{sc.symbol}: GRID_COUNT must be at least 2")
            if sc.investment <= 0:
                errors.append(f"{sc.symbol}: INVESTMENT must be positive")
        if not (0 < cls.MAX_LOSS_PCT <= 100):
            errors.append("MAX_LOSS_PCT must be between 0 and 100")
        if cls.LEVERAGE < 1:
            errors.append("LEVERAGE must be >= 1")
        if cls.CHECK_INTERVAL <= 0:
            errors.append("CHECK_INTERVAL must be > 0")
        if cls.MARKET_TYPE not in ("spot", "future"):
            errors.append(f"MARKET_TYPE must be 'spot' or 'future', got '{cls.MARKET_TYPE}'")
        if cls.FEE_RATE < 0 or cls.FEE_RATE > 0.1:
            errors.append("FEE_RATE must be between 0 and 0.1 (10%)")
        if cls.MAX_TOTAL_LOSS < 0:
            errors.append("MAX_TOTAL_LOSS must be >= 0 (0 = disabled)")
        if not cls.PAPER_TRADING:
            if not cls.API_KEY or cls.API_KEY == "your_exchange_api_key":
                errors.append("API_KEY is required for live trading")
            if not cls.API_SECRET or cls.API_SECRET == "your_exchange_api_secret":
                errors.append("API_SECRET is required for live trading")
        if not cls.DB_HOST or cls.DB_HOST == "your-rds-endpoint.rds.amazonaws.com":
            errors.append("DB_HOST is required (AWS RDS endpoint)")
        if not cls.DB_USER:
            errors.append("DB_USER is required")
        if not cls.DB_PASSWORD:
            errors.append("DB_PASSWORD is required")
        return errors

    @classmethod
    def summary(cls) -> str:
        sym_lines = ""
        for sc in cls.get_symbol_configs():
            sym_lines += (
                f"  {sc.symbol:<12} range={sc.lower_price:,.2f}-{sc.upper_price:,.2f}"
                f"  grids={sc.grid_count}  invest=${sc.investment:,.2f}\n"
            )
        mkt = cls.MARKET_TYPE.upper()
        lev = f" (Leverage {cls.LEVERAGE}x)" if cls.MARKET_TYPE == "future" and cls.LEVERAGE > 1 else ""
        return (
            f"Exchange      : {cls.EXCHANGE}\n"
            f"Market        : {mkt}{lev}\n"
            f"Mode          : {'Paper Trading' if cls.PAPER_TRADING else 'Live Trading'}\n"
            f"Symbols       :\n{sym_lines}"
            f"Max Loss      : {cls.MAX_LOSS_PCT}% per symbol"
            + (f" | Total Loss Limit: ${cls.MAX_TOTAL_LOSS:,.2f}" if cls.MAX_TOTAL_LOSS > 0 else "") + "\n"
            f"Fee Rate      : {cls.FEE_RATE * 100:.3f}% | Auto-Adjust Grid: {cls.AUTO_ADJUST_GRID}\n"
            f"Database      : {cls.DB_USER}@{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}\n"
            f"Check Interval: {cls.CHECK_INTERVAL}s | JSON Logging: {cls.LOG_JSON}"
        )
