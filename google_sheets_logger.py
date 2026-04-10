import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",  # needed for lookup by name
]

# Fixed tab names
SHEET_SUMMARY = "Summary"
SHEET_EVENTS = "Events"

TRADE_HEADERS = [
    "Timestamp", "Symbol", "Type", "Price", "Amount", "Value (USDT)",
    "Grid Profit", "Cumulative Profit",
]
SUMMARY_HEADERS = [
    "Symbol", "Last Update", "Current Price", "Open Orders",
    "Total Trades", "Total Profit (USDT)", "Runtime",
]
EVENT_HEADERS = ["Timestamp", "Event", "Details"]


class GoogleSheetsLogger:
    """Log grid trading activity to a Google Spreadsheet."""

    def __init__(
        self,
        credentials_file: str,
        sheet_id: str,
        sheet_name: str = "",
        worksheet_name: str = "transactions",
    ):
        self.sheet_id = sheet_id
        self.worksheet_name = worksheet_name  # tab for trades
        self.enabled = False
        self.service = None
        self._lock = threading.Lock()  # httplib2 is NOT thread-safe

        creds_path = Path(credentials_file) if credentials_file else None
        if not creds_path or not creds_path.exists():
            logger.warning("Google Sheets logging is DISABLED (credentials file not found: %s)", credentials_file)
            return

        if not sheet_id and not sheet_name:
            logger.warning("Google Sheets logging is DISABLED (GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME required)")
            return

        try:
            creds = service_account.Credentials.from_service_account_file(
                credentials_file, scopes=SCOPES
            )
            self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)

            # Resolve spreadsheet ID by name if not provided directly
            if not sheet_id and sheet_name:
                self.sheet_id = self._find_sheet_id_by_name(creds, sheet_name)
                if not self.sheet_id:
                    logger.error("Spreadsheet '%s' not found (make sure service account has access)", sheet_name)
                    return

            self._ensure_sheets()
            self.enabled = True
            logger.info("Google Sheets connected: %s (trades → '%s')", self.sheet_id, self.worksheet_name)
        except FileNotFoundError:
            logger.error("Google credentials file not found: %s", credentials_file)
        except Exception as exc:
            logger.error("Google Sheets init failed: %s", exc)

    def _find_sheet_id_by_name(self, creds, name: str) -> str:
        """Search Google Drive for a spreadsheet with the given name."""
        try:
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
            results = drive.files().list(
                q=f"name='{name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                fields="files(id, name)",
                pageSize=5,
            ).execute()
            files = results.get("files", [])
            if files:
                logger.info("Found spreadsheet '%s' → id=%s", name, files[0]["id"])
                return files[0]["id"]
        except Exception as exc:
            logger.error("Drive lookup failed: %s", exc)
        return ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_trade(self, trade: dict) -> None:
        """Append a completed trade row to the transactions worksheet."""
        if not self.enabled:
            logger.debug("Sheets disabled | trade: %s", trade)
            return
        row = [
            trade.get("timestamp", ""),
            trade.get("symbol", ""),
            trade.get("type", ""),
            trade.get("price", 0),
            trade.get("amount", 0),
            _fmt(trade.get("value", 0), 4),
            _fmt(trade.get("profit", 0), 6),
            _fmt(trade.get("total_profit", 0), 6),
        ]
        self._append_row(self.worksheet_name, row)

    def update_summary(self, summary: dict) -> None:
        """Overwrite a per-symbol row on the Summary sheet (row = symbol_index + 2)."""
        if not self.enabled:
            return
        symbol = summary.get("symbol", "")
        row_num = self._summary_row(symbol)
        row = [
            symbol,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            summary.get("current_price", 0),
            summary.get("open_orders", 0),
            summary.get("total_trades", 0),
            _fmt(summary.get("total_profit", 0), 6),
            summary.get("runtime", ""),
        ]
        self._update_range(f"{SHEET_SUMMARY}!A{row_num}:G{row_num}", [row])

    def _summary_row(self, symbol: str) -> int:
        """Return a stable row number for each symbol (row 2, 3, 4, ...)."""
        if not hasattr(self, '_symbol_rows'):
            self._symbol_rows: dict[str, int] = {}
        if symbol not in self._symbol_rows:
            self._symbol_rows[symbol] = len(self._symbol_rows) + 2  # row 1 = header
        return self._symbol_rows[symbol]

    def log_bot_event(self, event: str, details: str = "") -> None:
        """Append an event row (start, stop, error, summary, etc.)."""
        if not self.enabled:
            logger.debug("Sheets disabled | event: %s", event)
            return
        clean_details = details.replace("\n", " | ").replace("*", "")
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            event,
            clean_details,
        ]
        self._append_row(SHEET_EVENTS, row)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_sheets(self) -> None:
        spreadsheet = self.service.spreadsheets().get(
            spreadsheetId=self.sheet_id
        ).execute()
        existing = {s["properties"]["title"] for s in spreadsheet["sheets"]}

        add_requests: list[dict] = []
        for name in [self.worksheet_name, SHEET_SUMMARY, SHEET_EVENTS]:
            if name not in existing:
                add_requests.append({"addSheet": {"properties": {"title": name}}})

        if add_requests:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": add_requests},
            ).execute()

        self._init_headers()

    def _init_headers(self) -> None:
        header_map = {
            self.worksheet_name: TRADE_HEADERS,
            SHEET_SUMMARY: SUMMARY_HEADERS,
            SHEET_EVENTS: EVENT_HEADERS,
        }
        for sheet_name, headers in header_map.items():
            self._set_header_if_empty(sheet_name, headers)

    def _set_header_if_empty(self, sheet_name: str, headers: list[str]) -> None:
        try:
            rng = f"{sheet_name}!A1:{chr(64 + len(headers))}1"
            self.service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
        except HttpError as exc:
            logger.error("Failed to set header for %s: %s", sheet_name, exc)

    def _append_row(self, sheet_name: str, row: list[Any]) -> None:
        if not self.service:
            return
        try:
            with self._lock:
                self.service.spreadsheets().values().append(
                    spreadsheetId=self.sheet_id,
                    range=f"{sheet_name}!A:A",
                    valueInputOption="RAW",
                    body={"values": [row]},
                ).execute()
        except HttpError as exc:
            logger.error("Sheets append failed (%s): %s", sheet_name, exc)
        except Exception as exc:
            logger.warning("Sheets append error (%s): %s", sheet_name, exc)

    def _update_range(self, range_name: str, values: list[list[Any]]) -> None:
        if not self.service:
            return
        try:
            with self._lock:
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    body={"values": values},
                ).execute()
        except HttpError as exc:
            logger.error("Sheets update failed (%s): %s", range_name, exc)
        except Exception as exc:
            logger.warning("Sheets update error (%s): %s", range_name, exc)


def _fmt(value: float, decimals: int) -> float:
    return round(float(value), decimals)
