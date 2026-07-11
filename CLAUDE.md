# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

โปรเจกต์นี้เป็นภาษาไทย — โค้ดคอมเมนต์ ล็อก อีเมล และ UI ทั้งหมดเป็นภาษาไทย เขียนของใหม่ให้เข้าชุดกัน

## คำสั่งที่ใช้บ่อย

```bash
# เตรียมสภาพแวดล้อม (ครั้งแรก)
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
brew install tesseract tesseract-lang     # จำเป็นสำหรับ BBL เท่านั้น (PDF เป็นภาพสแกน)

# ต้องโหลด env ก่อนรันทุกครั้ง — ถ้าไม่ทำ ปุ่มที่พึ่ง SMTP บนเว็บจะเงียบ ๆ ไม่ทำงาน
# (subprocess ที่เว็บ spawn สืบทอด env มาจาก uvicorn)
export DATA_DIR="$PWD/data"
set -a; . ./.env; set +a

python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8080   # เว็บ dashboard

python -m app.monitor.rate_monitor                     # ตรวจทุกธนาคารที่ enabled (parallel)
python -m app.monitor.rate_monitor --only BBL,KTB      # เฉพาะบางธนาคาร
python -m app.monitor.rate_monitor --backfill          # สร้าง CSV ใหม่จาก PDF ที่เก็บไว้แล้ว
python -m app.monitor.rate_monitor --discover-year     # ดาวน์โหลดประกาศย้อนหลังของปีปัจจุบัน แล้ว backfill ให้
python -m app.monitor.rate_monitor --test-email

docker-compose up -d --build                           # คอนเทนเนอร์เดียว: uvicorn + supercronic (09:00)
```

**ไม่มี test suite** — ยืนยันงานด้วยการรันจริงเสมอ: `--backfill` (re-parse PDF ทุกไฟล์ที่มี แล้วพิมพ์ค่าที่อ่านได้
ออกมาให้ตรวจตา) แล้วเปิดเว็บดูผล ผู้ใช้ยืนยันงานผ่านเว็บจริงเป็นหลัก

**ห้าม commit/push เองโดยไม่ได้รับคำสั่งชัดเจน** (ระบุไว้ใน `handover.md`)

## สถาปัตยกรรม

สองส่วนแยกขาดกัน เชื่อมกันผ่าน **ไฟล์ใน `DATA_DIR` เท่านั้น — ไม่มีฐานข้อมูล**:

- `app/monitor/` — เครื่องยนต์ scraping (ไม่ import อะไรจากฝั่งเว็บเลย) รันได้จาก CLI และจาก cron
- `app/web/` — FastAPI dashboard เป็น **ชั้นอ่านอย่างเดียว** ของไฟล์เหล่านั้น + ปุ่มที่ spawn ตัว monitor
  เป็น subprocess (`_run_monitor_thread` ใน `main.py`, กันรันซ้อนด้วย `run.lock`)

ไฟล์ใน `DATA_DIR` (env; local `./data`, Docker `/data`): `banks_config.json`, `settings.json`,
`<code>_deposit_rate.csv`, `pdfs/<CODE>/<code>_deposit_YYYY-MM-DD.pdf`, `<code>_result.json`, log

**Flow ของ `run_bank()` (`rate_monitor.py`)** — ส่วนกลางเป็น generic ไม่มี logic เฉพาะธนาคาร:
`banks.resolve_latest_url()` → `common.download_pdf()` → `banks.effective_date()` → **dedupe เทียบกับแถว
ล่าสุดใน CSV** → ถ้าเป็นวันใหม่: `banks.extract_rates()` → เซฟ PDF → `append_to_csv` (คำนวณคอลัมน์ `change_*`
ให้เอง) → `check_warnings` (±0.5%) → `write_result()` → ส่งอีเมล

## การเพิ่มธนาคาร (สัญญาที่ต้องทำตาม)

1. สร้าง `app/monitor/banks/<code>.py` ต้องมี `PARSER_IDS: list[str]` และ
   `extract_rates(pdf_bytes, bank) -> dict | None` (คืน `{key: float, ..., "tiers_used": {...}}`)
2. เพิ่มชื่อ module ใน `_MODULES` ที่ `banks/__init__.py` — **จุดเดียวที่ต้องแตะโค้ดส่วนกลาง**
3. เพิ่ม/แก้ entry ใน `data/banks_config.json` โดย `parser` ต้องตรงกับ `PARSER_IDS`

**Hook ทางเลือก** (ไม่มีก็ได้ ระบบ fallback ให้เอง — dispatch อยู่ใน `banks/__init__.py`):
`get_effective_date(pdf_bytes)` · `resolve_latest_url(bank)` · `discover_year(bank, year=None)`
(ถ้าไม่มี `discover_year` ปุ่ม/คำสั่ง discover-year จะข้ามธนาคารนั้นเงียบ ๆ)

ฝั่งเว็บ (routes, templates, กราฟ, ปุ่ม discover-year) **generic ต่อธนาคารทั้งหมด — เพิ่มธนาคารใหม่ไม่ต้องแก้
อะไรใน `app/web/` เลย**

### กับดักที่เสียเวลาถ้าไม่รู้

- `"fetch_mode": "impersonate"` — ต้องสะกดตรงตัวนี้เท่านั้น `download_pdf()` เช็ค `mode == "impersonate"`
  แบบ exact string ค่าอื่น (เช่น `"curl-impersonate"` ที่ KBANK ตั้งไว้) ตกไปใช้ curl ธรรมดาเงียบ ๆ
- `--backfill` เอาวันที่มาจาก **ชื่อไฟล์** PDF ไม่ได้เรียก `effective_date` ซ้ำ — ชื่อไฟล์จึงเป็น source of truth
  ของประวัติ
- `--discover-year` ทำงานเฉพาะธนาคารที่ `enabled: true`
- `latest_pdf_url` / `prev_pdf_url` ปล่อยเป็น `""` ได้ถ้ามี `resolve_latest_url` (ระบบข้าม init/prev อย่างสะอาด)
- แต่ละ parser มีวิธีหาประวัติย้อนหลังคนละแบบ: SCB = ไล่เลขลำดับประกาศ, KBANK = probe รายวัน,
  KTB = AJAX รายเดือน + token, BBL = ลิงก์ทั้งหมดอยู่ในหน้าเดียว

### รูปแบบ PDF ของแต่ละธนาคาร

`scb.py` / `ktb.py` อ่าน `extract_text().splitlines()` แล้วจับ label ด้วย **consonant skeleton**
(`_tablekit.thai_skeleton` — ตัดสระ/วรรณยุกต์ทิ้ง เพราะ pdfplumber ถอดข้อความไทยแล้วสระเพี้ยน/สลับที่)
`kbank.py` ใช้พิกัด x ของคำ (`extract_words`) เพราะ KBANK ไม่ pad คอลัมน์ว่างด้วย `"-"`

`bbl.py` ต่างจากทุกตัว: **PDF ของ BBL เป็นภาพสแกนล้วน ไม่มี text layer** → render หน้า 1 เป็น PNG (300 DPI)
แล้วเรียก `tesseract ... -l tha+eng --psm 6 tsv` ผ่าน subprocess ได้คำพร้อมพิกัด/ค่าความมั่นใจ จากนั้นจับ
คอลัมน์จากการ cluster พิกัด x ของค่าตัวเลข และ anchor แถวด้วยหัวข้อ "ประจำ" + "ระยะเวลาการฝาก N เดือน"
(**ห้าม anchor ด้วยเลขข้อ** — เลขข้อเลื่อนตามปี) รายละเอียดกับดักของ OCR (ค่า `0.30`→`0,30`, เลขข้อ
`9.8`→`9.B`, เดือน `ธันวาคม`→`ชันวาคม`, หน่วย `เดือน`→`Wau`) เขียนไว้ครบใน docstring ของไฟล์ — อ่านก่อนแก้
