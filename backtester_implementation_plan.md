# Backtester Implementation Plan

## Overview

เพิ่มระบบ Backtesting Strategy สำหรับ Grid Trading Bot เพื่อทดสอบ strategy บนข้อมูลราคาย้อนหลัง (historical OHLCV) ก่อนใช้งานจริง โดยใช้ logic เดียวกับ `GridBot` ใน `grid_bot.py`

---

## Architecture

```
backtester.py          ← Engine หลัก: โหลดข้อมูล + จำลอง grid trading
backtest_config.py     ← Config เฉพาะ backtest (date range, fees, etc.)
run_backtest.py        ← Entry point สำหรับรัน backtest
backtest_results/      ← โฟลเดอร์เก็บผลลัพธ์ (CSV, summary text)
```

---

## Implementation Steps

### Step 1: สร้าง `backtest_config.py`

สร้าง dataclass `BacktestConfig` สำหรับเก็บ parameter ทั้งหมดของ backtest:

| Parameter       | Type  | Default              | Description                             |
| --------------- | ----- | -------------------- | --------------------------------------- |
| `symbol`        | str   | `"BTC/USDT"`         | Trading pair                            |
| `exchange`      | str   | `"binance"`          | Exchange สำหรับดึงข้อมูล OHLCV          |
| `timeframe`     | str   | `"1m"`               | Candle interval (1m, 5m, 15m, 1h, etc.) |
| `start_date`    | str   | `"2025-01-01"`       | วันเริ่มต้น (ISO format)                |
| `end_date`      | str   | `"2025-04-01"`       | วันสิ้นสุด (ISO format)                 |
| `lower_price`   | float | `60000.0`            | ราคาล่างสุดของ grid                     |
| `upper_price`   | float | `70000.0`            | ราคาบนสุดของ grid                       |
| `grid_count`    | int   | `10`                 | จำนวน grid levels                       |
| `investment`    | float | `1000.0`             | เงินลงทุน (USDT)                        |
| `maker_fee_pct` | float | `0.1`                | ค่า maker fee (%)                       |
| `taker_fee_pct` | float | `0.1`                | ค่า taker fee (%)                       |
| `max_loss_pct`  | float | `20.0`               | Stop-loss: หยุดถ้าขาดทุนเกิน X%         |
| `market_type`   | str   | `"spot"`             | `"spot"` หรือ `"future"`                |
| `leverage`      | int   | `1`                  | Futures leverage                        |
| `output_dir`    | str   | `"backtest_results"` | โฟลเดอร์เก็บผลลัพธ์                     |

Properties:

- `fee_rate` → คำนวณ average fee rate เป็น decimal
- `summary()` → แสดงสรุป config เป็น string

---

### Step 2: สร้าง `backtester.py` (Core Engine)

#### Data Structures

1. **`BacktestOrder`** — จำลอง grid order
   - Fields: `id`, `order_type` (buy/sell), `price`, `amount`, `level_index`, `status`, `filled_price`, `filled_at`, `fee`

2. **`TradeRecord`** — บันทึก trade ที่เกิดขึ้น
   - Fields: `timestamp`, `order_type`, `price`, `amount`, `value`, `fee`, `gross_profit`, `net_profit`, `cumulative_profit`, `position`

3. **`BacktestResult`** — สรุปผล backtest ทั้งหมด
   - Metrics: `total_trades`, `gross_profit`, `total_fees`, `net_profit`, `max_drawdown`, `roi_pct`, `win_rate`, `unrealized_pnl`, `total_pnl`, etc.

#### Backtester Class — Main Flow

```
1. Fetch OHLCV candles จาก exchange ผ่าน ccxt (public API, ไม่ต้องใช้ API key)
2. Set up grid levels เหมือน live bot
3. วนลูปแต่ละ candle:
   - เช็คว่า open orders จะถูก fill หรือไม่ จาก high/low ของ candle
   - ถ้า BUY order fill → place SELL order ขึ้นไป 1 level
   - ถ้า SELL order fill → place BUY order ลงมา 1 level + คำนวณกำไร
   - เช็ค risk limit (max loss)
4. สรุป BacktestResult
```

#### Key Methods

| Method              | Description                                                      |
| ------------------- | ---------------------------------------------------------------- |
| `run()`             | Entry point — fetch data, setup grid, simulate, return result    |
| `_fetch_candles()`  | ดึง OHLCV จาก exchange ผ่าน ccxt (paginated)                     |
| `_setup_grid()`     | สร้าง grid levels + place initial orders (mirrors `grid_bot.py`) |
| `_process_candle()` | เช็คแต่ละ candle ว่า fill orders ไหนบ้าง                         |
| `_handle_fill()`    | จัดการเมื่อ order ถูก fill (mirrors `grid_bot.py._handle_fill`)  |
| `_build_result()`   | สร้าง `BacktestResult` จาก state ปัจจุบัน                        |

#### Export Functions

- `export_trades_csv(result, filepath)` — บันทึก trades ทั้งหมดเป็น CSV
- `export_summary_txt(result, filepath)` — บันทึกสรุปผลเป็น text file

---

### Step 3: สร้าง `run_backtest.py` (Entry Point)

CLI interface รองรับ 2 โหมด:

#### Single Run

```bash
python run_backtest.py --symbol BTC/USDT --start 2025-01-01 --end 2025-04-01 \
  --lower 60000 --upper 70000 --grids 10 --investment 1000
```

#### Optimization Mode (`--optimize`)

```bash
python run_backtest.py --optimize --symbol BTC/USDT \
  --lower 60000 --upper 70000 --investment 1000
```

ทดสอบ `grid_count` หลายค่า [5, 8, 10, 15, 20, 25, 30] แล้วเปรียบเทียบผลลัพธ์เป็นตาราง

#### CLI Arguments

| Argument        | Default      | Description               |
| --------------- | ------------ | ------------------------- |
| `--symbol`      | `BTC/USDT`   | Trading pair              |
| `--exchange`    | `binance`    | Exchange                  |
| `--timeframe`   | `5m`         | Candle interval           |
| `--start`       | `2025-01-01` | Start date                |
| `--end`         | `2025-04-01` | End date                  |
| `--lower`       | `60000`      | Grid lower price          |
| `--upper`       | `70000`      | Grid upper price          |
| `--grids`       | `10`         | Grid count                |
| `--investment`  | `1000`       | Investment (USDT)         |
| `--fee`         | `0.1`        | Fee per side (%)          |
| `--max-loss`    | `20`         | Max loss (%)              |
| `--market-type` | `spot`       | spot / future             |
| `--leverage`    | `1`          | Futures leverage          |
| `--optimize`    | false        | Run multi-parameter sweep |
| `--log-level`   | `INFO`       | Logging level             |

---

### Step 4: สร้าง `backtest_results/` directory

- สร้าง `.gitkeep` เพื่อ track folder
- เพิ่มใน `.gitignore`:
  ```
  backtest_results/*.csv
  backtest_results/*.txt
  backtest_results/*.png
  ```

---

## Metrics ที่ Backtest คำนวณ

| Metric               | Description                                    |
| -------------------- | ---------------------------------------------- |
| **Net Profit**       | กำไรสุทธิหลังหักค่า fee                        |
| **ROI %**            | `net_profit / investment * 100`                |
| **Max Drawdown**     | ค่า peak-to-trough สูงสุดของ cumulative profit |
| **Max Drawdown %**   | `max_drawdown / investment * 100`              |
| **Win Rate**         | สัดส่วน sell trades ที่ net profit > 0         |
| **Avg Profit/Trade** | `net_profit / total_trades`                    |
| **Unrealized PnL**   | มูลค่า position คงเหลือ ณ ราคาปิดสุดท้าย       |
| **Total PnL**        | `net_profit + unrealized_pnl`                  |
| **Price Change %**   | การเปลี่ยนแปลงราคาจาก start → end              |

---

## ความแตกต่างจาก Live Bot

| Feature         | Live Bot (`grid_bot.py`)          | Backtester (`backtester.py`) |
| --------------- | --------------------------------- | ---------------------------- |
| Price source    | Real-time ticker                  | Historical OHLCV candles     |
| Order fill      | Exchange confirmation / paper sim | Candle high/low range check  |
| Fees            | ❌ ไม่หัก (ตาม Plan #14)          | ✅ หักค่า maker/taker fee    |
| Unrealized PnL  | ❌ ไม่แสดง (ตาม Plan #16)         | ✅ คำนวณ                     |
| Max Drawdown    | ❌ ไม่ track                      | ✅ Track peak-to-trough      |
| Database        | ✅ AWS RDS PostgreSQL             | ❌ ไม่ใช้ (export CSV/TXT)   |
| Telegram/Sheets | ✅ Real-time notifications        | ❌ ไม่ส่ง                    |
| Risk Management | ❌ ไม่มี stop-loss                | ✅ Max loss stop             |
| API Keys        | ✅ ต้องใช้                        | ❌ ไม่ต้อง (public API only) |

---

## Dependencies

- `ccxt` — ดึง historical OHLCV data (มีอยู่แล้วใน `requirements.txt`)
- No additional packages required

---

## File Changes Summary

| Action     | File                        | Description                      |
| ---------- | --------------------------- | -------------------------------- |
| **CREATE** | `backtest_config.py`        | Backtest configuration dataclass |
| **CREATE** | `backtester.py`             | Core backtesting engine          |
| **CREATE** | `run_backtest.py`           | CLI entry point                  |
| **CREATE** | `backtest_results/.gitkeep` | Output directory placeholder     |
| **MODIFY** | `.gitignore`                | Ignore backtest output files     |

---

## Usage Examples

```bash
# Basic backtest — BTC/USDT spot, 5m candles, Jan-Apr 2025
python run_backtest.py --symbol BTC/USDT --start 2025-01-01 --end 2025-04-01 \
  --lower 60000 --upper 70000 --grids 10 --investment 1000

# ETH/USDT futures with lower fees
python run_backtest.py --symbol ETH/USDT --market-type future \
  --lower 2000 --upper 2500 --grids 15 --fee 0.05

# High-precision 1m candles (slower but more accurate)
python run_backtest.py --timeframe 1m --start 2025-03-01 --end 2025-04-01

# Parameter optimization — compare grid_count values
python run_backtest.py --optimize --symbol BTC/USDT \
  --lower 60000 --upper 70000 --investment 1000
```
