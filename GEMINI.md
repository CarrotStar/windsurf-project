# Gemini CLI Project Rules: Grid Trading Bot

## 🛠 Tech Stack
- **Language**: Python 3
- **Core Trading**: `ccxt` (Crypto Exchange Trading for Spot & Futures)
- **Database**: PostgreSQL via `psycopg2-binary` (Hosted on AWS RDS)
- **Logging**: Google Sheets API (`google-api-python-client`)
- **Notifications**: Telegram Bot API (`requests`)
- **Configuration**: `python-dotenv` (via `config.py`)

## 📋 AI Agent Guidelines & "Skills"
- **API Security**: NEVER hardcode API keys, secrets, or database credentials. Always rely on `config.py` which loads from `.env`.
- **Trading Safety**: Always assume and maintain `PAPER_TRADING=true` during development and testing. Any modifications to `grid_bot.py` or `exchange_client.py` require rigorous validation to prevent unintended live trades.
- **Error Handling**: 
  - Network/Exchange: Handle `ccxt` rate limits, timeouts, and API errors gracefully.
  - Database: Implement robust reconnection logic and use parameterized queries for `psycopg2` to prevent SQL injection.
  - Third-party: Handle Google Sheets API quotas and Telegram delivery failures without crashing the core trading loop.
- **Architecture**: The project uses a synchronous loop (`CHECK_INTERVAL` in `config.py`). When adding features, ensure they do not cause significant blocking that could delay the grid evaluation cycle.
