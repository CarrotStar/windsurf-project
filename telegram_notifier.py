import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send notifications to a Telegram chat via Bot API."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(
            token and chat_id
            and token != "your_telegram_bot_token"
            and chat_id != "your_telegram_chat_id"
        )
        if not self.enabled:
            logger.warning("Telegram notifications are DISABLED (token/chat_id not configured)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled | msg: %s", text[:80])
            return False

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
