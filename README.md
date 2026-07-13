# CheckRate — Dashboard ติดตามอัตราดอกเบี้ยเงินฝาก

ระบบติดตามประกาศอัตราดอกเบี้ยเงินฝากของธนาคาร: ดาวน์โหลด PDF ประกาศ → อ่านค่าอัตราดอกเบี้ย →
บันทึกประวัติเป็น CSV → แจ้งเตือนทางอีเมลเมื่อมีการเปลี่ยนแปลง พร้อม **เว็บ Dashboard** สำหรับดูภาพรวม
กราฟแนวโน้ม จัดการค่า config และสั่งรันตรวจสอบด้วยตนเอง ออกแบบให้แพ็กเป็น Docker รันบน **Synology NAS** ได้

รองรับ **หลายธนาคารพร้อมกัน (parallel)** และเพิ่มธนาคาร/รูปแบบ PDF ใหม่ได้ผ่านระบบ parser แบบ plugin
ปัจจุบันมี parser พร้อมใช้งาน 4 ตัว: **SCB** (`scb_passbook`), **KBANK** (`kbank`), **KTB** (`ktb`)
และ **BBL** (`bbl`)

---

## คุณสมบัติหลัก

- **Monitor หลายธนาคารแบบขนาน** — ดาวน์โหลด + อ่านค่าทุกธนาคารที่เปิดใช้งานพร้อมกัน ธนาคารหนึ่งพังไม่ล้มทั้งระบบ
- **Parser แบบ plugin** — โค้ดอ่านค่าของแต่ละธนาคารแยกเป็นไฟล์ (`app/monitor/banks/<code>.py`) เพิ่มธนาคารใหม่ที่มี PDF คนละรูปแบบได้โดยไม่แตะโค้ดส่วนกลาง
- **ค้นหาประวัติย้อนหลัง** — `discover_year` สแกนหาไฟล์ประกาศเก่าทั้งปี (ธนาคารที่รองรับเท่านั้น) และ `--backfill` สร้าง CSV ใหม่จาก PDF ที่ดาวน์โหลดเก็บไว้แล้ว
- **แจ้งเตือนอีเมลผ่าน SMTP + App Password** (ไม่พึ่ง Gmail API/OAuth) รองรับผู้รับหลายคน แก้ผ่านหน้าเว็บได้
- **Login ผู้ดูแลด้วย OTP ทางอีเมล** (`app/web/auth.py`) — ไม่มีฐานข้อมูล/รหัสผ่าน รายชื่อผู้มีสิทธิ์กำหนดผ่าน env
  `ADMIN_EMAILS` เท่านั้น (แก้จากเว็บไม่ได้ กันยกระดับสิทธิ์ผ่าน API) session cookie เซ็นด้วย secret ที่ persist ข้าม restart
- **เว็บ Dashboard (FastAPI):**
  - **ภาพรวม** (`/?month=YYYY-MM`) และ **รายละเอียดต่อธนาคาร** — เปิดดูได้สาธารณะ ไม่ต้อง login
    - ภาพรวม: สรุปรายเดือนต่อธนาคาร (ประกาศไปกี่ครั้ง, อัตราไหนเปลี่ยน, ขึ้น/ลงสุทธิเท่าไร) พร้อม KPI รวมทุกธนาคารด้านบนและโลโก้ธนาคาร (ถ้ามีไฟล์)
    - รายละเอียด: กราฟแนวโน้ม (วาดเป็น SVG เอง ไม่พึ่งไลบรารี, วันที่แสดงเป็น พ.ศ.) + สรุปรายเดือนแบบเดียวกับภาพรวม + ลิงก์เปิด PDF ย้อนหลังจัดกลุ่มตามปี
  - **จัดการอัตรา** และ **Log & รัน** — สงวนไว้เฉพาะผู้ดูแลที่ login แล้วเท่านั้น
    - จัดการอัตรา: เพิ่ม/ลบ/แก้ rate target (กำหนด key + ชื่อย่อเอง), เปิด-ปิดธนาคาร, แก้ลิงก์ดาวน์โหลดเอกสาร, ตั้งผู้รับอีเมล
    - Log & รัน: ดู log (แยกแท็ก `[CODE]` ต่อธนาคาร แม้รันขนาน), สั่ง "รันตรวจสอบทันที", ปุ่ม "ทดสอบส่งอีเมล", ปุ่มค้นหาประวัติทั้งปี (ทุกธนาคารที่รองรับ)
  - Responsive — มีแถบเมนูล่างสำหรับมือถือ, topbar สำหรับจอใหญ่
- **ทำงานแบบ offline ได้** — กราฟวาดด้วย SVG ล้วน (ไม่พึ่งไลบรารีกราฟ) และฟอนต์ IBM Plex (Sans Thai + Mono) ฝังในโปรเจกต์ ไม่พึ่ง CDN
  โลโก้ธนาคาร (`app/web/static/img/logos/`) ก็เป็นไฟล์ในเครื่องเช่นกัน — ธนาคารที่ไม่มีไฟล์ เว็บ fallback
  ไปแสดงตัวอักษรย่อ (monogram) ให้เอง
- **พร้อม Docker** — `Dockerfile` + `docker-compose.yml` + ตั้งเวลาด้วย supercronic ในคอนเทนเนอร์

---

## โครงสร้างโปรเจกต์

```
CheckRate/
├── app/
│   ├── monitor/                 # ส่วนตรวจสอบอัตรา (ไม่พึ่งเว็บ)
│   │   ├── rate_monitor.py      # orchestrator: รันทุกธนาคารแบบ parallel + CLI
│   │   ├── common.py            # ฟังก์ชันร่วม: ดาวน์โหลด PDF, CSV, อีเมล, settings
│   │   └── banks/               # 1 ไฟล์ = 1 ธนาคาร (โค้ดอ่านค่าแยกกัน)
│   │       ├── __init__.py      # registry: parser id → module + dispatch hook ทางเลือก
│   │       ├── _tablekit.py     # helper อ่านตาราง/ข้อความไทยที่ใช้ร่วมกัน
│   │       ├── scb.py           # ตัวอ่านของ SCB (parser id: scb_passbook)
│   │       ├── kbank.py         # ตัวอ่านของ KBANK (parser id: kbank)
│   │       ├── ktb.py           # ตัวอ่านของ KTB (parser id: ktb)
│   │       └── bbl.py           # ตัวอ่านของ BBL (parser id: bbl) — PDF เป็นภาพสแกน ต้อง OCR
│   └── web/                     # เว็บ Dashboard (FastAPI)
│       ├── main.py              # routes + API
│       ├── auth.py              # login ผู้ดูแลด้วย OTP ทางอีเมล + session cookie
│       ├── data_access.py       # ชั้นอ่าน config/CSV/log/result
│       ├── thaidate.py          # Jinja filter แปลงวันที่ ISO → รูปแบบไทย (พ.ศ.)
│       ├── templates/           # Jinja2 (รวม login.html)
│       └── static/              # CSS/JS/ฟอนต์ IBM Plex/โลโก้ธนาคาร (ฝังในเครื่อง)
├── tools/
│   └── fetch_logos.py           # dev tool รันมือครั้งเดียว — ดึงโลโก้ธนาคารจาก logo.dev
├── data/                        # DATA_DIR (gitignored) — CSV/PDF/log/config/settings
├── Dockerfile
├── docker-compose.yml
├── crontab                      # ตารางเวลา supercronic (ค่าเริ่มต้น 09:00 Asia/Bangkok)
├── entrypoint.sh
├── requirements.txt
├── .env.example                # ตัวอย่างค่า env (คัดลอกเป็น .env)
└── DEPLOY.md                    # คู่มือ deploy บน Synology NAS (ไทย)
```

ข้อมูลทั้งหมด (CSV, PDF, log, config, settings) เก็บใน **`DATA_DIR`** — แยกออกจากโค้ด
เพื่อให้ persist นอกคอนเทนเนอร์และปรับตำแหน่งได้

---

## Environment variables

ตั้งค่าผ่านไฟล์ `.env` (คัดลอกจาก `.env.example`) — **ห้าม commit `.env` เข้า git**

| ตัวแปร | ความหมาย |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` | เซิร์ฟเวอร์ SMTP (Gmail: `smtp.gmail.com` / `465`) |
| `SMTP_USER` / `SMTP_PASSWORD` | บัญชี + **App Password** สำหรับส่งอีเมล |
| `EMAIL_FROM` | อีเมลผู้ส่ง (มักเป็นตัวเดียวกับ `SMTP_USER`) |
| `EMAIL_TO` | ผู้รับเริ่มต้น (คั่นหลายคนด้วย `,`) — แก้ผ่านหน้าเว็บได้ (เก็บใน `settings.json`) |
| `DATA_DIR` | ตำแหน่งเก็บข้อมูล (local: `./data`, Docker: `/data`) |
| `WEB_HOST` / `WEB_PORT` | host/port ของเว็บ (ค่าเริ่มต้น `0.0.0.0` / `8080`) |
| `ADMIN_EMAILS` | อีเมลที่มีสิทธิ์เข้า `/config`, `/logs` และกดปุ่มรันตรวจสอบ (คั่นหลายคนด้วย `,`) — ไม่มีทางแก้จากเว็บ |
| `SESSION_SECRET` | secret สำหรับเซ็น session cookie — เว้นว่างได้ ระบบ generate เก็บที่ `{DATA_DIR}/.session_secret` ให้เอง (ตั้งเองถ้ารันหลาย container ที่ต้องแชร์ session) |
| `HOST_DATA_DIR` | path บน NAS ที่ map เข้า `/data` ในคอนเทนเนอร์ |
| `TZ` | timezone (ค่าเริ่มต้น `Asia/Bangkok`) |

> **App Password ที่มีช่องว่าง** ต้องใส่เครื่องหมายคำพูดครอบใน `.env` เช่น `SMTP_PASSWORD="abcd efgh ijkl mnop"`

---

## วิธีรันบนเครื่อง (local dev — macOS/Linux)

```bash
# 0. ติดตั้ง tesseract + ภาษาไทย (จำเป็นสำหรับ BBL เท่านั้น — PDF ประกาศเป็นภาพสแกน ต้อง OCR)
brew install tesseract tesseract-lang        # macOS
# sudo apt-get install tesseract-ocr tesseract-ocr-tha   # Debian/Ubuntu
# (Docker: ติดตั้งให้แล้วใน Dockerfile)

# 1. เตรียม virtualenv + ติดตั้ง dependency
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. เตรียมค่า config
cp .env.example .env
# แก้ .env ใส่ค่า SMTP จริง (ดูตาราง env ด้านบน)

# 3. โหลด env + ตั้ง DATA_DIR
export DATA_DIR="$PWD/data"
set -a; . ./.env; set +a         # โหลด SMTP_* เข้า environment (สำคัญ!)

# 4a. รันเว็บ Dashboard
python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8080
#    เปิด http://localhost:8080

# 4b. หรือรันตรวจสอบอัตราด้วยมือ
python -m app.monitor.rate_monitor                    # ทุกธนาคารที่เปิดใช้งาน (parallel)
python -m app.monitor.rate_monitor --only SCB,KBANK   # เฉพาะบางธนาคาร (คั่นด้วย ,)
python -m app.monitor.rate_monitor --backfill         # สร้าง CSV ใหม่จาก PDF ที่เก็บไว้
python -m app.monitor.rate_monitor --discover-year    # สแกนหาประกาศทั้งปี (เฉพาะ bank ที่รองรับ)
python -m app.monitor.rate_monitor --test-email       # ทดสอบส่งอีเมล
```

> `--discover-year` ยิง request จำนวนมากไปยังเว็บธนาคาร ใช้เฉพาะตอนต้องการเติมประวัติย้อนหลัง
> ไม่ควรตั้งให้รันอัตโนมัติ (SCB มี rate-limit — ตัว parser หน่วงเวลาและหยุดเองเมื่อตรวจพบว่าโดนบล็อก)

> ตอนรันเว็บด้วยมือ ต้อง `set -a; . ./.env; set +a` **ก่อน** สั่ง uvicorn เสมอ ไม่งั้นปุ่มที่พึ่ง SMTP (ทดสอบส่งอีเมล) จะไม่ทำงาน เพราะ subprocess สืบทอด env จากตัว uvicorn

---

## รันด้วย Docker (สำหรับ Synology NAS)

```bash
cp .env.example .env      # แก้ค่าจริง โดยเฉพาะ SMTP_* และ HOST_DATA_DIR
docker-compose up -d --build
```

คอนเทนเนอร์เดียวรันทั้ง **เว็บ (uvicorn)** และ **ตัวตั้งเวลา (supercronic)** — เข้าเว็บที่ `http://<host>:8080`
ค่าเวลารันอัตโนมัติปรับได้ที่ไฟล์ `crontab` (ค่าเริ่มต้น 09:00 Asia/Bangkok)

📖 ขั้นตอนแบบละเอียด (สร้าง App Password, เตรียมข้อมูลบน NAS, Container Manager, ตั้งเวลา) ดูที่ **[DEPLOY.md](DEPLOY.md)**

---

## เพิ่มธนาคารใหม่

1. สร้างไฟล์ `app/monitor/banks/<code>.py` กำหนด `PARSER_IDS` และฟังก์ชัน `extract_rates(pdf_bytes, bank)`
2. เพิ่มชื่อ module ลงใน `_MODULES` ที่ `app/monitor/banks/__init__.py`
3. เพิ่มรายการธนาคารใน `banks_config.json` (ผ่านหน้า **จัดการอัตรา** บนเว็บ หรือแก้ไฟล์ตรง ๆ)
   โดยตั้ง `parser` ให้ตรงกับ `PARSER_IDS` ของ module

**Hook ทางเลือก** — ถ้า module ไม่มีฟังก์ชันเหล่านี้ ระบบจะข้ามหรือใช้ค่าเริ่มต้นให้เอง ไม่ error:

| ฟังก์ชัน | ใช้เมื่อ | ถ้าไม่มี |
|---|---|---|
| `get_effective_date(pdf_bytes)` | รูปแบบวันที่ในเอกสารต่างจากค่าเริ่มต้น | ใช้ `common.get_effective_date` |
| `resolve_latest_url(bank)` | URL ประกาศล่าสุดไม่คงที่ (เช่น ฝังวันที่ไว้ใน path) | ใช้ `bank["latest_pdf_url"]` ตรง ๆ |
| `discover_year(bank, year)` | รองรับการสแกนหาประกาศย้อนหลังทั้งปี | ปุ่ม/คำสั่ง discover-year จะข้ามธนาคารนี้ |

ตัวช่วยอ่านตารางและข้อความไทยที่ใช้ร่วมกันได้อยู่ใน `banks/_tablekit.py` (`thai_skeleton`, `kw_in_line`,
`row_values`, `pick_amount_tier`, ฯลฯ) — `thai_skeleton` มีไว้แก้ปัญหา pdfplumber สลับตำแหน่งสระ/แทรกช่องว่าง
กลางคำไทย

ไม่ต้องแก้ `rate_monitor.py` หรือ `common.py` — flow ส่วนกลางเป็น generic

**ถ้า PDF เป็นภาพสแกน (ไม่มี text layer)** ดู `banks/bbl.py` เป็นตัวอย่าง — render หน้า 1 เป็นภาพแล้ว OCR
ด้วย tesseract (tha+eng) จับคอลัมน์จากพิกัด x ของค่าที่อ่านได้ และตรวจความมั่นใจ (conf) ของ OCR ก่อนเชื่อค่า
รองรับทั้งแถวเงินฝากประจำ (ชี้ด้วย `tenor_months`) และแถวชื่อผลิตภัณฑ์อื่น เช่น สะสมทรัพย์ (ชี้ด้วย
`row_keyword`/`section_keyword`) และรองรับ **tier วงเงิน (`amount_m`) กับทุกแถวเสมอ** แม้ประกาศฉบับนั้น
จะไม่ได้แบ่ง tier ก็ตาม — เผื่อธนาคารเปลี่ยนมาแบ่ง tier ในอนาคตโดยไม่ต้องแก้ parser

**โลโก้ธนาคารบนเว็บ** — ไม่บังคับ ไม่มีก็ fallback เป็นตัวอักษรย่อ ถ้าต้องการโลโก้จริง รันครั้งเดียว:
```bash
export LOGODEV_TOKEN=pk_xxxx           # publishable token จาก logo.dev
python tools/fetch_logos.py            # ทุกธนาคาร (ต้องเพิ่มโดเมนใน DOMAINS ก่อนถ้าเป็นธนาคารใหม่)
```

---

## Tech stack

Python 3.13 · FastAPI · Uvicorn · Jinja2 · itsdangerous (session cookie) · pdfplumber · curl_cffi ·
tesseract (OCR) · supercronic · Docker

> `curl_cffi` ใช้ impersonate TLS fingerprint ของ Chrome เพื่อดาวน์โหลด PDF ของธนาคารที่มี bot-protection
> (KBANK, KTB, BBL) — มี manylinux wheel พร้อมใช้ ไม่ต้องแก้ `Dockerfile`
>
> `tesseract` (+ ภาษาไทย) ใช้เฉพาะ **BBL** ที่ประกาศเป็นภาพสแกนล้วน — ธนาคารอื่นอ่านข้อความจาก PDF
> ได้ตรง ๆ ผ่าน pdfplumber ถ้าไม่ติดตั้ง tesseract ระบบยังทำงานปกติ แต่ BBL จะอ่านอัตราไม่ได้และแจ้ง error
