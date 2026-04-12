import logging
import queue
import threading
import time

import requests

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 1.05   # seconds between sends (Telegram cap: ~30 msg/min per bot)
_QUEUE_MAX = 200       # drop oldest when queue is full


class TelegramNotifier:
    """Send notifications to a Telegram chat via Bot API.

    Messages are queued and dispatched by a background daemon thread so that
    the caller is never blocked waiting for HTTP. The thread enforces a
    minimum 1-second gap between sends, and automatically retries after the
    retry_after delay on HTTP 429 responses.
    """

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(
            token and chat_id
            and token != "your_telegram_bot_token"
            and chat_id != "your_telegram_chat_id"
        )
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        if not self.enabled:
            logger.warning("Telegram notifications are DISABLED (token/chat_id not configured)")
        else:
            t = threading.Thread(
                target=self._sender_loop, daemon=True, name="telegram-sender"
            )
            t.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Queue a message for delivery. Non-blocking — returns True if queued."""
        if not self.enabled:
            logger.debug("Telegram disabled | msg: %s", text[:80])
            return False
        try:
            self._queue.put_nowait((text, parse_mode))
            return True
        except queue.Full:
            logger.warning("Telegram queue full — message dropped: %s", text[:60])
            return False

    def test_connection(self) -> bool:
        """Verify the bot token is valid and log the bot username."""
        if not self.enabled:
            return False
        try:
            response = requests.get(f"{self.base_url}/getMe", timeout=10)
            response.raise_for_status()
            bot = response.json().get("result", {})
            logger.info("Telegram connected: @%s (id=%s)", bot.get("username"), bot.get("id"))
            return True
        except Exception as exc:
            logger.error("Telegram connection test failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Background sender
    # ------------------------------------------------------------------

    def _sender_loop(self) -> None:
        while True:
            text, parse_mode = self._queue.get()
            self._do_send(text, parse_mode)
            time.sleep(_MIN_INTERVAL)

    def _do_send(self, text: str, parse_mode: str) -> bool:
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if response.status_code == 429:
                retry_after = int(
                    response.json().get("parameters", {}).get("retry_after", 30)
                )
                logger.warning("Telegram rate-limited — retrying after %ds", retry_after)
                time.sleep(retry_after)
                return self._do_send(text, parse_mode)
            response.raise_for_status()
            logger.debug("Telegram message sent")
            return True
        except requests.exceptions.Timeout:
            logger.error("Telegram send timeout")
        except requests.exceptions.HTTPError as exc:
            logger.error("Telegram HTTP error: %s | body: %s", exc, exc.response.text)
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
        return False
