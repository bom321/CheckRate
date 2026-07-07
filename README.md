# CheckRate — Dashboard ติดตามอัตราดอกเบี้ยเงินฝาก

ระบบติดตามประกาศอัตราดอกเบี้ยเงินฝากของธนาคาร: ดาวน์โหลด PDF ประกาศ → อ่านค่าอัตราดอกเบี้ย →
บันทึกประวัติเป็น CSV → แจ้งเตือนทางอีเมลเมื่อมีการเปลี่ยนแปลง พร้อม **เว็บ Dashboard** สำหรับดูภาพรวม
กราฟแนวโน้ม จัดการค่า config และสั่งรันตรวจสอบด้วยตนเอง ออกแบบให้แพ็กเป็น Docker รันบน **Synology NAS** ได้

รองรับ **หลายธนาคารพร้อมกัน (parallel)** และเพิ่มธนาคาร/รูปแบบ PDF ใหม่ได้ผ่านระบบ parser แบบ plugin

---

## คุณสมบัติหลัก

- **Monitor หลายธนาคารแบบขนาน** — ดาวน์โหลด + อ่านค่าทุกธนาคารที่เปิดใช้งานพร้อมกัน ธนาคารหนึ่งพังไม่ล้มทั้งระบบ
- **Parser แบบ plugin** — โค้ดอ่านค่าของแต่ละธนาคารแยกเป็นไฟล์ (`app/monitor/banks/<code>.py`) เพิ่มธนาคารใหม่ที่มี PDF คนละรูปแบบได้โดยไม่แตะโค้ดส่วนกลาง
- **แจ้งเตือนอีเมลผ่าน SMTP + App Password** (ไม่พึ่ง Gmail API/OAuth) รองรับผู้รับหลายคน แก้ผ่านหน้าเว็บได้
- **เว็บ Dashboard (FastAPI):**
  - **ภาพรวม** — 1 ตารางต่อ 1 ธนาคาร เทียบอัตราปัจจุบัน vs ครั้งก่อน + ไฮไลต์แถวที่เปลี่ยนแปลง
  - **รายละเอียดต่อธนาคาร** — กราฟแนวโน้ม (Chart.js) + ตารางประวัติ + ลิงก์เปิด PDF
  - **จัดการอัตรา** — เพิ่ม/ลบ/แก้ rate target (กำหนด key + ชื่อย่อเอง), เปิด-ปิดธนาคาร, แก้ลิงก์ดาวน์โหลดเอกสาร, ตั้งผู้รับอีเมล
  - **Log & รัน** — ดู log, สั่ง "รันตรวจสอบทันที", ปุ่ม "ทดสอบส่งอีเมล"
- **ทำงานแบบ offline ได้** — Chart.js และฟอนต์ไทย (Noto Sans Thai) ฝังในโปรเจกต์ ไม่พึ่ง CDN
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
│   │       ├── __init__.py      # registry: parser id → module
│   │       └── scb.py           # ตัวอ่านของ SCB
│   └── web/                     # เว็บ Dashboard (FastAPI)
│       ├── main.py              # routes + API
│       ├── data_access.py       # ชั้นอ่าน config/CSV/log/result
│       ├── templates/           # Jinja2
│       └── static/              # CSS/JS/ฟอนต์/Chart.js (ฝังในเครื่อง)
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
| `HOST_DATA_DIR` | path บน NAS ที่ map เข้า `/data` ในคอนเทนเนอร์ |
| `TZ` | timezone (ค่าเริ่มต้น `Asia/Bangkok`) |

> **App Password ที่มีช่องว่าง** ต้องใส่เครื่องหมายคำพูดครอบใน `.env` เช่น `SMTP_PASSWORD="abcd efgh ijkl mnop"`

---

## วิธีรันบนเครื่อง (local dev — macOS/Linux)

```bash
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
python -m app.monitor.rate_monitor              # ทุกธนาคารที่เปิดใช้งาน (parallel)
python -m app.monitor.rate_monitor --only SCB   # เฉพาะบางธนาคาร
python -m app.monitor.rate_monitor --test-email # ทดสอบส่งอีเมล
```

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

1. สร้างไฟล์ `app/monitor/banks/<code>.py` มีฟังก์ชัน `extract_rates(pdf_bytes, bank)`
   (และ effective-date ถ้ารูปแบบวันที่ต่างจากเดิม)
2. ลงทะเบียน parser id ใน `app/monitor/banks/__init__.py`
3. เพิ่มรายการธนาคารใน `banks_config.json` (ผ่านหน้า **จัดการอัตรา** บนเว็บ หรือแก้ไฟล์ตรง ๆ)

ไม่ต้องแก้ `rate_monitor.py` หรือ `common.py` — flow ส่วนกลางเป็น generic

---

## Tech stack

Python 3.13 · FastAPI · Uvicorn · Jinja2 · pdfplumber · Chart.js · supercronic · Docker
