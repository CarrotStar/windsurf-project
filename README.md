# Grid Trading Bot 🤖

บอท Grid Trading ภาษา Python รองรับการ log ผ่าน Google Sheets และแจ้งเตือน Telegram

---

## คุณสมบัติ
- **Grid Trading** — วางคำสั่งซื้อ/ขายแบบตะแกรง (Grid) อัตโนมัติ
- **Paper Trading** — ทดสอบกลยุทธ์โดยไม่ใช้เงินจริง (ใช้ราคาจริงจาก Exchange)
- **Live Trading** — เชื่อมต่อ Exchange จริงผ่านไลบรารี [ccxt](https://github.com/ccxt/ccxt) (รองรับกว่า 100 Exchange)
- **Google Sheets** — บันทึก Trade, Summary, และ Event log แบบ real-time
- **Telegram** — แจ้งเตือนทุกคำสั่งที่ถูก Fill, สรุปชั่วโมงละครั้ง, และแจ้ง Error

---

## โครงสร้างไฟล์

```
windsurf-project/
├── main.py                  # Entry point — รันที่นี่
├── config.py                # โหลดค่า config จาก .env
├── grid_bot.py              # Core logic ของ Grid Trading
├── exchange_client.py       # เชื่อมต่อ Exchange (paper / live)
├── telegram_notifier.py     # ส่งข้อความ Telegram
├── google_sheets_logger.py  # บันทึกข้อมูลลง Google Sheets
├── requirements.txt         # Python dependencies
├── .env.example             # ตัวอย่างไฟล์ config (copy → .env)
└── credentials.json         # (สร้างเอง) Google Service Account key
```

---

## การติดตั้ง

### 1. ติดตั้ง Python dependencies

```bash
pip install -r requirements.txt
```

### 2. สร้างไฟล์ `.env`

```bash
copy .env.example .env   # Windows
# หรือ
cp .env.example .env     # Linux / macOS
```

แก้ไขค่าใน `.env` ตามต้องการ (ดูรายละเอียดด้านล่าง)

---

## การตั้งค่า

### Exchange (ccxt)
| ตัวแปร | คำอธิบาย | ค่าเริ่มต้น |
|---|---|---|
| `EXCHANGE` | ชื่อ exchange เช่น `binance`, `okx`, `bybit` | `binance` |
| `API_KEY` | API Key จาก Exchange (ต้องการเฉพาะ Live mode) | |
| `API_SECRET` | API Secret (ต้องการเฉพาะ Live mode) | |
| `TESTNET` | ใช้ Testnet ของ Exchange | `true` |
| `PAPER_TRADING` | **`true`** = จำลองคำสั่ง ไม่ส่งไป Exchange | `true` |

### Grid Parameters
| ตัวแปร | คำอธิบาย | ตัวอย่าง |
|---|---|---|
| `SYMBOL` | คู่เงินที่ต้องการเทรด | `BTC/USDT` |
| `LOWER_PRICE` | ราคาต่ำสุดของ Grid | `25000` |
| `UPPER_PRICE` | ราคาสูงสุดของ Grid | `35000` |
| `GRID_COUNT` | จำนวนช่อง Grid | `10` |
| `INVESTMENT` | เงินลงทุนรวม (USDT) | `1000` |

**Grid Step** = `(UPPER_PRICE - LOWER_PRICE) / GRID_COUNT`  
**Order Size per Grid** = `INVESTMENT / GRID_COUNT / level_price`

### Telegram
1. สร้าง Bot ผ่าน [@BotFather](https://t.me/BotFather) แล้วนำ Token มาใส่
2. หา Chat ID โดยส่งข้อความให้ Bot แล้วเปิด URL:  
   `https://api.telegram.org/bot<TOKEN>/getUpdates`  
   หรือใช้ [@userinfobot](https://t.me/userinfobot)

| ตัวแปร | คำอธิบาย |
|---|---|
| `TELEGRAM_TOKEN` | Token จาก BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID ของคุณ |

### Google Sheets
1. ไปที่ [Google Cloud Console](https://console.cloud.google.com/)
2. สร้าง Project → Enable **Google Sheets API**
3. สร้าง **Service Account** → ดาวน์โหลด JSON key บันทึกเป็น `credentials.json`
4. สร้าง Google Sheet ใหม่ → แชร์ให้ email ของ Service Account (Editor)
5. คัดลอก Sheet ID จาก URL:  
   `https://docs.google.com/spreadsheets/d/**SHEET_ID**/edit`

| ตัวแปร | คำอธิบาย |
|---|---|
| `GOOGLE_CREDENTIALS_FILE` | Path ของ `credentials.json` |
| `GOOGLE_SHEET_ID` | ID ของ Google Sheet |

---

## วิธีใช้งาน

### Paper Trading (ทดสอบ — แนะนำสำหรับเริ่มต้น)
ตั้งค่า `.env`:
```
PAPER_TRADING=true
SYMBOL=BTC/USDT
LOWER_PRICE=60000
UPPER_PRICE=70000
GRID_COUNT=10
INVESTMENT=1000
```

รันบอท:
```bash
python main.py
```

### Live Trading
ตั้งค่า `.env`:
```
PAPER_TRADING=false
TESTNET=false
API_KEY=your_real_api_key
API_SECRET=your_real_api_secret
```

> ⚠️ **คำเตือน**: Live Trading ใช้เงินจริง ควรทดสอบด้วย Paper Trading ก่อนเสมอ

---

## Google Sheets — โครงสร้างข้อมูล

บอทจะสร้าง Sheet ย่อย 3 แท็บอัตโนมัติ:

| Sheet | คำอธิบาย |
|---|---|
| **Trades** | ประวัติ Trade ทุกรายการ (Timestamp, Type, Price, Amount, Profit) |
| **Summary** | สรุปสถานะปัจจุบัน อัปเดตทุก Cycle |
| **Events** | Log เหตุการณ์สำคัญ (Start, Stop, Error, Summary) |

---

## Telegram — การแจ้งเตือน

| เหตุการณ์ | รายละเอียด |
|---|---|
| 🤖 Bot Started | แสดงค่า config และจำนวน Order ที่วาง |
| 🔵 BUY Filled | แสดง Price, Amount, Value |
| 🟢 SELL Filled | แสดง Price, Amount, Value, Grid Profit, Total Profit |
| ⚠️ Out of Range | ราคาหลุดออกนอก Grid |
| 📊 Hourly Summary | สรุปทุกชั่วโมง |
| ❌ Error | แจ้งเมื่อเกิด Error |
| 🛑 Bot Stopped | สรุปผลเมื่อบอทหยุด |

---

## หยุดบอท

กด `Ctrl+C` — บอทจะ:
1. ยกเลิก Open Order ทั้งหมด
2. ส่งสรุปผลไปยัง Telegram
3. บันทึก Event ลง Google Sheets

---

## ข้อควรระวัง

- Grid Trading เหมาะกับตลาดที่ราคา **sideways** (ผันผวนในช่วงแคบ)
- หากราคาออกนอก Grid และไม่กลับมา อาจขาดทุนจากการถือ Position ด้านใดด้านหนึ่ง
- ควรตั้ง Grid Range ให้เหมาะสมกับสภาพตลาด
- ทดสอบด้วย Paper Trading ก่อนเสมอ
