# คู่มือ Deploy CheckRate บน Synology DS916+

ระบบนี้ประกอบด้วย 2 ส่วนที่รันในคอนเทนเนอร์เดียวกัน:
- **Monitor** (`app/monitor/rate_monitor.py`) — ดาวน์โหลด PDF ประกาศดอกเบี้ย → extract → อัปเดต CSV → ส่งอีเมล (SMTP)
- **เว็บ Dashboard** (`app/web`, FastAPI) — ดูภาพรวม/รายละเอียด/จัดการ config/log ผ่านเบราว์เซอร์

---

## ขั้นตอนที่ 1 — สร้าง Gmail App Password (ถ้าใช้ Gmail ส่งอีเมล)

1. เปิดใช้งาน **2-Step Verification** ในบัญชี Google ก่อน (จำเป็น ถ้ายังไม่เปิดจะสร้าง App Password ไม่ได้)
2. ไปที่ https://myaccount.google.com/apppasswords แล้วสร้างรหัสผ่านแอปใหม่ (ตั้งชื่อเช่น "CheckRate")
3. คัดลอกรหัส 16 หลักที่ได้ (รูปแบบ `xxxx xxxx xxxx xxxx`) — จะใช้เป็นค่า `SMTP_PASSWORD`

> ถ้าใช้ผู้ให้บริการอีเมลอื่น (เช่น Outlook/Office365) ให้ปรับ `SMTP_HOST`/`SMTP_PORT` ตามผู้ให้บริการนั้น — ไม่จำเป็นต้องเป็น Gmail

---

## ขั้นตอนที่ 2 — เตรียมข้อมูลบน NAS

1. เปิด **File Station** บน DSM แล้วสร้างโฟลเดอร์เก็บข้อมูล เช่น `/volume1/deposit-rate/`
   (ชื่อ/ตำแหน่งปรับได้ — จะตั้งค่าจริงใน `.env` ด้วย `HOST_DATA_DIR` ในขั้นตอนถัดไป)
2. คัดลอกไฟล์ข้อมูลเดิมจาก Mac (โฟลเดอร์ `data/` ในโปรเจกต์นี้ ซึ่ง seed มาจาก
   `/Users/bom321/Desktop/Learn Claude/Deposit Rate/SCB/` แล้ว) เข้าไปใน `/volume1/deposit-rate/`:
   - `banks_config.json`
   - `settings.json`
   - `scb_deposit_rate.csv` (และ CSV อื่น ๆ ถ้ามี)
   - โฟลเดอร์ `pdfs/SCB/` (และของธนาคารอื่นถ้ามี)
   - (ไม่บังคับ) log เดิม → เปลี่ยนชื่อเป็น `rate_monitor.log`
3. โครงสร้างที่ควรได้บน NAS:
   ```
   /volume1/deposit-rate/
   ├── banks_config.json
   ├── settings.json
   ├── scb_deposit_rate.csv
   └── pdfs/SCB/*.pdf
   ```

---

## ขั้นตอนที่ 3 — Copy โปรเจกต์ขึ้น NAS และตั้งค่า `.env`

1. คัดลอกโฟลเดอร์โปรเจกต์นี้ทั้งหมด (`CheckRate/`) ขึ้น NAS ไปที่ `/volume1/bom321/Work/deposit-rate/docker/checkrate/`
   (ผ่าน File Station, `scp`, หรือ Git — **ไม่ต้อง** copy โฟลเดอร์ `data/` ที่ใช้ dev บน Mac ขึ้นไปด้วยก็ได้
   เพราะข้อมูลจริงจะอยู่ที่ `HOST_DATA_DIR` ตามขั้นตอนที่ 2)
2. สร้างไฟล์ `.env` จาก `.env.example`:
   ```
   cp .env.example .env
   ```
3. แก้ `.env` ใส่ค่าจริง:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=465
   SMTP_USER=your_email@gmail.com
   SMTP_PASSWORD=xxxx xxxx xxxx xxxx      # App Password จากขั้นตอนที่ 1
   EMAIL_FROM=your_email@gmail.com
   EMAIL_TO=bom321@hotmail.com            # ผู้รับเริ่มต้น (แก้ภายหลังผ่านหน้าเว็บได้)
   HOST_DATA_DIR=/volume1/deposit-rate    # ต้องตรงกับโฟลเดอร์ที่สร้างในขั้นตอนที่ 2
   WEB_PORT=8080
   ADMIN_EMAILS=bom321@hotmail.com        # อีเมลที่มีสิทธิ์เข้า /config, /logs และกดปุ่มรันตรวจสอบ
   ```
   **ห้าม commit ไฟล์ `.env` เข้า git** (มี `.gitignore` กันไว้ให้แล้ว)

---

## ขั้นตอนที่ 4 — Build & Run

### วิธี A: ผ่าน Container Manager (GUI)
1. เปิด **Container Manager** → แท็บ **Project** → **Create**
2. เลือกโฟลเดอร์โปรเจกต์ (`/volume1/bom321/Work/deposit-rate/docker/checkrate/`) ที่มี `docker-compose.yml` อยู่
3. กด **Build** แล้ว **Run** — DSM จะอ่าน `docker-compose.yml` และ `.env` ในโฟลเดอร์เดียวกันให้อัตโนมัติ

### วิธี B: ผ่าน SSH
```bash
cd /volume1/bom321/Work/deposit-rate/docker/checkrate
docker-compose up -d --build
```

ตรวจสอบว่าคอนเทนเนอร์รันอยู่:
```bash
docker ps
docker logs -f checkrate
```

---

## ขั้นตอนที่ 5 — เข้าเว็บ

เปิดเบราว์เซอร์ไปที่ `http://<NAS-IP>:8080` (พอร์ตปรับได้ผ่าน `WEB_PORT` ใน `.env`)

- **ภาพรวม** / **รายละเอียดธนาคาร** — เปิดดูได้ทุกคนโดยไม่ต้อง login
- **จัดการอัตรา**, **Log & รัน** และปุ่มรันตรวจสอบ — ต้อง login ก่อน (ปุ่ม "เข้าสู่ระบบ" มุมขวาบน)
  กรอกอีเมลที่อยู่ใน `ADMIN_EMAILS` → รับรหัส OTP 6 หลักทางอีเมล (หมดอายุ 5 นาที) → กรอกรหัสเพื่อเข้าสู่ระบบ
  session ค้างไว้ 30 วัน, ออกจากระบบได้ที่ปุ่ม "ออกจากระบบ"

---

## ขั้นตอนที่ 6 — ตั้งเวลารันอัตโนมัติ

มี 2 ทางเลือก:

### (ก) supercronic ในคอนเทนเนอร์ — ค่าเริ่มต้นของระบบนี้
คอนเทนเนอร์รัน `supercronic` อ่านตาราง `crontab` ในโปรเจกต์อัตโนมัติเมื่อ start (ดูไฟล์ `crontab`)
ค่าเริ่มต้น: รันทุกวันเวลา 09:00 (Asia/Bangkok)

**ข้อดี:** self-contained, ไม่ต้องพึ่ง DSM, ทำงานได้แม้ย้ายไปรันเครื่องอื่น
**ข้อเสีย:** แก้เวลาต้องแก้ไฟล์ `crontab` แล้ว rebuild/restart คอนเทนเนอร์:
```bash
# แก้ไฟล์ crontab แล้ว
docker-compose up -d --build
```

### (ข) DSM Task Scheduler เรียก `docker exec`
ปิด/ลบบรรทัดใน `crontab` ไม่ให้ supercronic รันซ้ำ แล้วตั้งใน DSM แทน:

1. **Control Panel** → **Task Scheduler** → **Create** → **Scheduled Task** → **User-defined script**
2. ตั้งเวลาตามต้องการ (เช่น ทุกวัน 09:00)
3. ใส่ script:
   ```bash
   docker exec checkrate python -m app.monitor.rate_monitor
   ```

**ข้อดี:** จัดการเวลาผ่าน UI ของ DSM ได้ง่าย ไม่ต้อง rebuild image
**ข้อเสีย:** ผูกกับ DSM Task Scheduler โดยเฉพาะ, ต้องจำไว้ปิด cron ในคอนเทนเนอร์ไม่ให้ทำงานซ้อนกัน

---

## อัปเดตเวอร์ชันใหม่ — `scripts/update.sh`

เมื่อโค้ดบน GitHub มี commit ใหม่ ใช้สคริปต์นี้อัปเดตให้จบในคำสั่งเดียว (git pull → build → restart →
เช็คว่าเว็บตอบจริง) — **ใช้ได้เฉพาะกรณีที่โค้ดบน NAS เป็น git clone** ไม่ใช่ไฟล์ที่ copy ผ่าน File Station:

```bash
# ครั้งแรก (ถ้ายังไม่ได้ clone) — ติดตั้ง Git Server จาก Package Center ก่อนให้มีคำสั่ง git
cd /volume1/bom321/Work/deposit-rate/docker
git clone https://github.com/bom321/checkrate.git checkrate
cd checkrate && cp .env.example .env    # แล้วแก้ .env ตามขั้นตอนที่ 3
```

### ตั้งใน DSM Task Scheduler
**Control Panel** → **Task Scheduler** → **Create** → **Scheduled Task** → **User-defined script**
(User: `root`) แล้วใส่:
```bash
sh /volume1/bom321/Work/deposit-rate/docker/checkrate/scripts/update.sh
```
ตั้งเวลาตามต้องการ หรือปล่อยเป็น task ที่กด **Run** เอาเองเมื่อจะอัปเดต

สคริปต์ปลอดภัยเมื่อรันซ้ำ: ถ้าไม่มี commit ใหม่จะ**ข้ามการ build** (แต่ยังเช็คให้ว่าคอนเทนเนอร์ยังรันอยู่
ถ้าดับจะ start ให้) ผลลัพธ์ออกทั้ง stdout (Task Scheduler ส่งอีเมลให้ได้ถ้าเปิด "Send run details by email")
และไฟล์ `update.log` ในโฟลเดอร์โปรเจกต์

**พฤติกรรมที่ควรรู้:**
- ข้อมูลจริง (CSV/PDF/config ใน `HOST_DATA_DIR`) อยู่นอกโฟลเดอร์โปรเจกต์ — สคริปต์ไม่แตะเลย
- ถ้ามีไฟล์ที่ถูกแก้ค้างไว้บน NAS (`git status` ไม่สะอาด) หรือโค้ดแตกสายจาก `origin/main` สคริปต์จะ
  **หยุดพร้อมบอกเหตุผล ไม่ทับของเดิม** ต้องเข้าไปเก็บกวาดเองก่อน
- ถ้า build ผ่านแต่เว็บไม่ตอบใน 60 วิ จะ dump `docker logs` 30 บรรทัดล่าสุดลง log แล้ว exit ด้วย code 1
- เปลี่ยน branch ได้ด้วย env: `BRANCH=dev sh scripts/update.sh`

---

## ขั้นตอนที่ 7 — ทดสอบ

1. เข้าเว็บ → หน้า **Log & รัน** → กด **"✉️ ทดสอบส่งอีเมล"** เพื่อยืนยันว่าตั้งค่า SMTP ถูกต้อง
2. กด **"▶ รันตรวจสอบทันที (ทุกธนาคาร)"** เพื่อทดสอบ pipeline แบบเต็ม
3. ดูผลใน Log console และหน้าภาพรวม (ควรเห็นวันที่/อัตราล่าสุดอัปเดต)

---

## หมายเหตุสำคัญ

- **ไม่มีการ hardcode path หรือรหัสผ่าน** — ทุกอย่างอ่านจาก environment variable (`.env`) และ `DATA_DIR`
- Path บน Mac เดิม (`/Users/bom321/...`) **ไม่ถูกใช้ในระบบนี้เลย** — เปลี่ยนเป็น `DATA_DIR=/data` (ใน container)
  ที่ map มาจาก `HOST_DATA_DIR` บน NAS ตามที่ตั้งใน `.env`
- ผู้รับอีเมลที่แก้ผ่านหน้าเว็บจะถูกเก็บใน `settings.json` (ใน `HOST_DATA_DIR`) และ override ค่า `EMAIL_TO` ใน `.env`
- เพิ่มธนาคารใหม่ในอนาคต: เพิ่มไฟล์ตัวอ่าน PDF ที่ `app/monitor/banks/<code>.py` แล้วลงทะเบียนใน
  `app/monitor/banks/__init__.py` — ไม่ต้องแก้โค้ดส่วนอื่น
- รายชื่อผู้ดูแล (`ADMIN_EMAILS`) ตั้งใน `.env` เท่านั้น — ไม่มีทางแก้จากหน้าเว็บ (กันไม่ให้ยกระดับสิทธิ์ตัวเองผ่าน API)
  ต้องแก้ `.env` แล้ว restart container ถ้าจะเพิ่ม/ลดผู้ดูแล
- session cookie เซ็นด้วย secret ที่ generate เก็บไว้ที่ `{HOST_DATA_DIR}/.session_secret` ให้อัตโนมัติ (คงอยู่ข้าม restart)
  เว็บนี้ไม่มี HTTPS ในสแตกนี้ (ไม่มี reverse proxy) — ถ้าเปิดใช้งานนอก LAN ที่เชื่อถือได้ ควรเพิ่ม HTTPS/reverse proxy เอง
