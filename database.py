"""
AWS RDS PostgreSQL persistence layer for Grid Trading Bot.

Stores:
  - bot_state  : running config + cumulative stats (one row per symbol)
  - orders     : every order placed (open / filled / cancelled), tagged by symbol
  - trades     : every completed trade, tagged by symbol

Requires: psycopg2-binary
AWS RDS Free Tier: db.t3.micro | 20 GB SSD | 750 hrs/month
SSL is enforced by default (DB_SSL_MODE=require).
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS bot_state (
    symbol        TEXT             PRIMARY KEY,
    lower_price   DOUBLE PRECISION NOT NULL,
    upper_price   DOUBLE PRECISION NOT NULL,
    grid_count    INTEGER          NOT NULL,
    investment    DOUBLE PRECISION NOT NULL,
    initial_price DOUBLE PRECISION NOT NULL,
    total_profit  DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_funding_profit    DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_trades  INTEGER          NOT NULL DEFAULT 0,
    start_time    TIMESTAMP        NOT NULL,
    last_update   TIMESTAMP        NOT NULL,
    next_funding_ts_ms      DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_known_funding_rate DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id           TEXT             NOT NULL,
    symbol       TEXT             NOT NULL DEFAULT 'BTC/USDT',
    order_type   TEXT             NOT NULL,
    price        DOUBLE PRECISION NOT NULL,
    amount       DOUBLE PRECISION NOT NULL,
    level_index  INTEGER          NOT NULL,
    status       TEXT             NOT NULL DEFAULT 'open',
    filled_price DOUBLE PRECISION,
    filled_at    TIMESTAMP,
    created_at   TIMESTAMP        NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
    id           SERIAL           PRIMARY KEY,
    symbol       TEXT             NOT NULL DEFAULT 'BTC/USDT',
    timestamp    TIMESTAMP        NOT NULL,
    order_type   TEXT             NOT NULL,
    price        DOUBLE PRECISION NOT NULL,
    amount       DOUBLE PRECISION NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    profit       DOUBLE PRECISION NOT NULL,
    total_profit DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMP        NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders (symbol, status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol        ON trades (symbol);
"""

# Migration: handle old single-row schema (id=1) → new symbol-PK schema
_MIGRATE_SQL = """
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'bot_state' AND column_name = 'id'
    ) THEN
        DROP TABLE IF EXISTS bot_state;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'orders' AND c.contype = 'p' AND array_length(c.conkey, 1) >= 2
    ) THEN
        DROP TABLE IF EXISTS orders;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trades' AND column_name = 'symbol'
    ) THEN
        DROP TABLE IF EXISTS trades;
    END IF;
END $$;

-- Add new columns for funding fee tracking (added in April 2026)
DO $$ BEGIN
    ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS total_funding_profit    DOUBLE PRECISION NOT NULL DEFAULT 0;
    ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS next_funding_ts_ms      DOUBLE PRECISION NOT NULL DEFAULT 0;
    ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS last_known_funding_rate DOUBLE PRECISION NOT NULL DEFAULT 0;
END $$;
"""


class Database:
    """
    AWS RDS PostgreSQL persistence layer.

    Supports multiple symbols concurrently — every table is keyed by symbol.
    Uses a ThreadedConnectionPool (min=2, max=10) so all bot threads share safely.
    """

    def __init__(self):
        from config import Config  # local import avoids circular deps at module load
        self._cfg = Config
        self._pool: ThreadedConnectionPool = self._build_pool()
        self._migrate()
        self._init_tables()
        logger.info(
            "Connected to AWS RDS PostgreSQL: %s@%s:%d/%s",
            Config.DB_USER, Config.DB_HOST, Config.DB_PORT, Config.DB_NAME,
        )

    # ------------------------------------------------------------------
    # Connection pool helpers
    # ------------------------------------------------------------------

    def _build_pool(self) -> ThreadedConnectionPool:
        c = self._cfg
        return ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=c.DB_HOST,
            port=c.DB_PORT,
            dbname=c.DB_NAME,
            user=c.DB_USER,
            password=c.DB_PASSWORD,
            sslmode=c.DB_SSL_MODE,
            connect_timeout=10,
            application_name="grid_trading_bot",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

    @contextmanager
    def _conn(self):
        """Yield a connection from the pool; commit or rollback automatically.

        If the pool is broken (e.g. RDS restarted), rebuild it and retry once.
        """
        conn = None
        try:
            conn = self._pool.getconn()
        except Exception:
            logger.warning("Connection pool broken — rebuilding…")
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = self._build_pool()
            conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            # Connection died mid-query — rollback, discard, rebuild pool
            logger.warning("DB connection lost during query: %s — rebuilding pool", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            self._pool.putconn(conn, close=True)
            conn = None  # prevent double-putconn in finally
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = self._build_pool()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            if conn is not None:
                self._pool.putconn(conn)

    def health_check(self) -> bool:
        """Verify the database connection is alive (SELECT 1)."""
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return True
        except Exception as exc:
            logger.error("Database health check failed: %s", exc)
            return False

    def _migrate(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_MIGRATE_SQL)

    def _init_tables(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_SQL)

    # ------------------------------------------------------------------
    # Bot state  (one row per symbol)
    # ------------------------------------------------------------------

    def save_state(
        self,
        symbol: str,
        lower_price: float,
        upper_price: float,
        grid_count: int,
        investment: float,
        initial_price: float,
        total_profit: float,
        total_trades: int,
        start_time: str,
        total_funding_profit: float,
        next_funding_ts_ms: float,
        last_known_funding_rate: float,
    ) -> None:
        now = datetime.now()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_state
                        (symbol, lower_price, upper_price, grid_count, investment,
                         initial_price, total_profit, total_trades, start_time, last_update,
                         total_funding_profit, next_funding_ts_ms, last_known_funding_rate)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        lower_price             = EXCLUDED.lower_price,
                        upper_price             = EXCLUDED.upper_price,
                        grid_count              = EXCLUDED.grid_count,
                        investment              = EXCLUDED.investment,
                        initial_price           = EXCLUDED.initial_price,
                        total_profit            = EXCLUDED.total_profit,
                        total_trades            = EXCLUDED.total_trades,
                        start_time              = EXCLUDED.start_time,
                        last_update             = EXCLUDED.last_update,
                        total_funding_profit    = EXCLUDED.total_funding_profit,
                        next_funding_ts_ms      = EXCLUDED.next_funding_ts_ms,
                        last_known_funding_rate = EXCLUDED.last_known_funding_rate
                    """,
                    (
                        symbol, lower_price, upper_price, grid_count, investment,
                        initial_price, total_profit, total_trades, start_time, now,
                        total_funding_profit, next_funding_ts_ms, last_known_funding_rate,
                    ),
                )

    def load_state(self, symbol: str) -> Optional[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM bot_state WHERE symbol = %s", (symbol,))
                row = cur.fetchone()
                return dict(row) if row else None

    def update_stats(
        self,
        symbol: str,
        total_profit: float,
        total_trades: int,
        total_funding_profit: float,
        next_funding_ts_ms: float,
        last_known_funding_rate: float,
    ) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bot_state
                    SET total_profit = %s, total_trades = %s, last_update = %s,
                        total_funding_profit = %s, next_funding_ts_ms = %s, last_known_funding_rate = %s
                    WHERE symbol = %s
                    """,
                    (total_profit, total_trades, datetime.now(), total_funding_profit,
                     next_funding_ts_ms, last_known_funding_rate, symbol),
                )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def upsert_order(self, order, symbol: str) -> None:
        filled_at = (
            datetime.fromtimestamp(order.filled_at) if order.filled_at else None
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO orders
                        (id, symbol, order_type, price, amount, level_index,
                         status, filled_price, filled_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id, symbol) DO UPDATE SET
                        status       = EXCLUDED.status,
                        filled_price = EXCLUDED.filled_price,
                        filled_at    = EXCLUDED.filled_at
                    """,
                    (
                        order.id, symbol, order.order_type, order.price, order.amount,
                        order.level_index, order.status, order.filled_price, filled_at,
                    ),
                )

    def load_open_orders(self, symbol: str) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM orders WHERE symbol = %s AND status = 'open' ORDER BY price",
                    (symbol,),
                )
                return [dict(r) for r in cur.fetchall()]

    def mark_order_cancelled(self, order_id: str, symbol: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = %s AND symbol = %s",
                    (order_id, symbol),
                )

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def insert_trade(self, trade: dict, symbol: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                        (symbol, timestamp, order_type, price, amount, value, profit, total_profit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        symbol, trade["timestamp"], trade["type"], trade["price"],
                        trade["amount"], trade["value"], trade["profit"],
                        trade["total_profit"],
                    ),
                )

    def get_trade_count(self, symbol: str) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM trades WHERE symbol = %s", (symbol,))
                return cur.fetchone()[0]

    def get_total_profit(self, symbol: str) -> float:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(profit), 0) FROM trades WHERE symbol = %s",
                    (symbol,),
                )
                return float(cur.fetchone()[0])

    def get_net_filled_amount(self, symbol: str) -> float:
        """Calculate the net position size from all filled orders."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(CASE WHEN order_type = 'buy' THEN amount ELSE -amount END), 0)
                    FROM orders WHERE symbol = %s AND status = 'filled'
                    """,
                    (symbol,),
                )
                res = cur.fetchone()
                return float(res[0]) if res else 0.0

    # ------------------------------------------------------------------
    # Config check / reset  (per-symbol)
    # ------------------------------------------------------------------

    def config_matches(self, symbol: str, lower: float, upper: float, grid_count: int) -> bool:
        """Return True if saved state uses the same grid parameters."""
        state = self.load_state(symbol)
        if not state:
            return False
        return (
            abs(float(state["lower_price"]) - lower) < 0.01
            and abs(float(state["upper_price"]) - upper) < 0.01
            and int(state["grid_count"]) == grid_count
        )

    def clear_symbol(self, symbol: str) -> None:
        """Delete all rows for a specific symbol — called when starting a fresh grid."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_state WHERE symbol = %s", (symbol,))
                cur.execute("DELETE FROM orders    WHERE symbol = %s", (symbol,))
                cur.execute("DELETE FROM trades    WHERE symbol = %s", (symbol,))
        logger.info("Database cleared for %s — fresh grid starting", symbol)
