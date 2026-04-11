# 📋 แผนการแก้ไขโค้ด (Implementation Plan) — P0 Critical

เอกสารนี้อธิบายขั้นตอนการแก้ไขปัญหาเร่งด่วน (P0) ตามที่วิเคราะห์ไว้ใน `Plan.md` เพื่อให้บอททำงานได้อย่างมีเสถียรภาพบน Production

---

## 1. Database Connection Resilience

**ไฟล์ที่เกี่ยวข้อง**: `database.py`

**ปัญหา**: ปัจจุบันหากการเชื่อมต่อกับฐานข้อมูล (AWS RDS) ขาดไปชั่วคราว บอทจะหยุดทำงานทันทีและไม่พยายามเชื่อมต่อใหม่

**แนวทางการแก้ไข**: เราจะเพิ่ม Logic ให้บอทพยายามเชื่อมต่อฐานข้อมูลใหม่โดยอัตโนมัติ (Auto-reconnect) เมื่อการเชื่อมต่อเดิมล้มเหลว

**ขั้นตอน**:

1.  เปิดไฟล์ `database.py`
2.  หาเมธอดที่เป็น Context Manager สำหรับจัดการ connection (ใน `Plan.md` ตั้งชื่อว่า `_conn`)
3.  เพิ่ม `try...except` เพื่อดักจับข้อผิดพลาดขณะดึง connection จาก pool (`getconn()`)
4.  หากเกิดข้อผิดพลาด ให้สั่งสร้าง connection pool ขึ้นมาใหม่ (`_build_pool()`) แล้วลองดึง connection อีกครั้ง

**โค้ดตัวอย่าง (นำไปปรับใช้ในคลาส `Database`)**:

```python
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)

# สมมติว่านี่คือส่วนหนึ่งของคลาส Database ของคุณ
# class Database:
#     def __init__(self):
#         self._pool = self._build_pool()
#
#     def _build_pool(self):
#         # ... โค้ดสร้าง connection pool ...
#         pass

@contextmanager
def _conn(self):
    """
    Context manager ที่จัดการ connection และมี auto-reconnect logic
    """
    try:
        # พยายามดึง connection จาก pool ที่มีอยู่
        conn = self._pool.getconn()
    except Exception:
        # หากล้มเหลว (pool อาจจะพัง) ให้ log และสร้าง pool ใหม่
        logger.warning("Connection pool is broken, attempting to rebuild...")
        self._pool = self._build_pool()
        conn = self._pool.getconn() # ลองดึง connection อีกครั้ง
    
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # คืน connection กลับสู่ pool ไม่ว่าจะสำเร็จหรือล้มเหลว
        self._pool.putconn(conn)

```

---

## 2. Exchange API Rate Limit & Retry

**ไฟล์ที่เกี่ยวข้อง**: `exchange_client.py`

**ปัญหา**: บอทไม่มีกลไกรับมือเมื่อถูก Exchange จำกัดการเรียกใช้งาน API (Rate Limit) หรือเมื่อ API ของ Exchange มีปัญหาชั่วคราว (HTTP 5xx) ซึ่งอาจทำให้ออเดอร์ไม่ถูกส่งหรือสถานะไม่อัปเดต

**แนวทางการแก้ไข**: เปิดใช้งานฟีเจอร์ `rateLimit` ที่มีมากับไลบรารี `ccxt` ซึ่งเป็นวิธีที่ง่ายและได้ผลดีที่สุด

**ขั้นตอน**:

1.  เปิดไฟล์ `exchange_client.py`
2.  หาเมธอดที่ใช้สร้าง object ของ exchange (ใน `Plan.md` ตั้งชื่อว่า `_build_exchange`)
3.  ในส่วนของ `params` ที่จะส่งให้ `ccxt` ให้เพิ่ม `enableRateLimit: True` เข้าไป

**โค้ดตัวอย่าง (นำไปปรับใช้ในคลาส `ExchangeClient`)**:

```python
# สมมติว่านี่คือเมธอด _build_exchange ในคลาส ExchangeClient
def _build_exchange(self):
    params = {
        "options": {"defaultType": self.market_type},
        "enableRateLimit": True,  # ← เพิ่มบรรทัดนี้
        # "rateLimit": 100,       # (Optional) ปรับแก้ค่า default ของ ccxt (หน่วย ms)
    }
    # ... โค้ดส่วนที่เหลือ ...
```
การเพิ่ม `enableRateLimit: True` จะทำให้ `ccxt` จัดการเรื่องการหน่วงเวลา (delay) ระหว่างการเรียก API ให้โดยอัตโนมัติ เพื่อไม่ให้เกินโควต้าที่ Exchange กำหนด

---

## 3. SIGTERM Handling สำหรับ Production

**ไฟล์ที่เกี่ยวข้อง**: `main.py`

**ปัญหา**: เมื่อรันบอทผ่าน `systemd` บนเซิร์ฟเวอร์ คำสั่งหยุด `systemctl stop gridbot` จะส่งสัญญาณ `SIGTERM` แต่โค้ดปัจจุบันดักจับแค่ `KeyboardInterrupt` (Ctrl+C) ทำให้บอทถูก "ฆ่า" ทันทีโดยไม่ยกเลิกออเดอร์ที่ค้างอยู่

**แนวทางการแก้ไข**: เพิ่มโค้ดเพื่อดักจับสัญญาณ `SIGTERM` และสั่งให้บอทหยุดทำงานอย่างนุ่มนวล (Graceful Shutdown) เช่นเดียวกับตอนกด Ctrl+C

---

## 4. Typo ใน gridbot.service

**ไฟล์ที่เกี่ยวข้อง**: `gridbot.service`

**ปัญหา**: ไฟล์ `gridbot.service` มีการพิมพ์ผิด โดยมีตัวอักษรไทย `ๅ` ต่อท้ายบรรทัดสุดท้าย ทำให้ `systemd` ไม่เข้าใจไฟล์ config และไม่สามารถสั่ง `systemctl enable gridbot` ได้

**แนวทางการแก้ไข**: ลบตัวอักษร `ๅ` ที่ไม่ต้องการออก