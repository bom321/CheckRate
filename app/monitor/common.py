#!/usr/bin/env python3
"""
common.py — ฟังก์ชันร่วมของ Deposit Rate Monitor (ไม่มี logic เฉพาะธนาคาร)

รวม: path/env resolution, logging, download PDF, การอ่านวันที่ (Thai date),
CSV read/write, settings.json, result JSON (per-bank) และการส่งอีเมลผ่าน SMTP.

ค่าทั้งหมดอ่านจาก environment variable — ไม่ hardcode path หรือรหัสผ่าน:
  DATA_DIR       โฟลเดอร์เก็บ config/CSV/PDF/log/result/settings (ค่าเริ่มต้น = โฟลเดอร์โปรเจกต์)
  SMTP_HOST      โฮสต์ SMTP (เช่น smtp.gmail.com)
  SMTP_PORT      พอร์ต (465 = SSL, 587 = STARTTLS)
  SMTP_USER      อีเมลผู้ส่ง / ผู้ล็อกอิน
  SMTP_PASSWORD  App Password 16 หลัก
  EMAIL_FROM     ที่อยู่ผู้ส่ง (ค่าเริ่มต้น = SMTP_USER)
  EMAIL_TO       ผู้รับเริ่มต้น (ถ้าไม่มี email_to ใน settings.json)
"""

import subprocess, io, re, csv, os, json, logging, logging.handlers, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pdfplumber

# ─────────────────────────── Paths (env-based) ───────────────────────────
OUTPUT_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(OUTPUT_DIR, "banks_config.json")
LOG_PATH      = os.path.join(OUTPUT_DIR, "rate_monitor.log")
SETTINGS_PATH = os.path.join(OUTPUT_DIR, "settings.json")

RATE_CHANGE_THRESHOLD = 0.5

THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}

# ─────────────────────────── Logging ───────────────────────────
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("deposit_monitor")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fh = logging.handlers.TimedRotatingFileHandler(
        LOG_PATH, when="D", interval=1, backupCount=90, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)
    import sys
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(sh)
    return logger

log = _setup_logger()

# ─────────────────────────── Config / Settings ───────────────────────────
def load_config(enabled_only: bool = True) -> list[dict]:
    """โหลด banks_config.json คืน list ของ bank (ค่าเริ่มต้น: เฉพาะ enabled=true)"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    banks = data.get("banks", [])
    if enabled_only:
        return [b for b in banks if b.get("enabled", False)]
    return banks


def load_settings() -> dict:
    """อ่าน settings.json (เช่น email_to). คืน {} ถ้าไม่มีไฟล์/อ่านไม่ได้"""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.error(f"load_settings: {e}")
        return {}


def save_settings(settings: dict) -> None:
    """เขียน settings.json แบบ atomic (temp แล้ว replace)"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def get_bank_paths(bank_code: str) -> tuple[str, str]:
    """คืน (pdf_dir, csv_path) ของแต่ละธนาคาร — โฟลเดอร์ PDF แยกตามรหัสย่อ"""
    pdf_dir  = os.path.join(OUTPUT_DIR, "pdfs", bank_code)
    csv_path = os.path.join(OUTPUT_DIR, f"{bank_code.lower()}_deposit_rate.csv")
    return pdf_dir, csv_path


def get_csv_headers(rate_targets: list[dict]) -> list[str]:
    headers = ["effective_date"]
    for t in rate_targets:
        k = t["key"]
        headers += [k, f"change_{k.split('rate_')[1]}"]
    return headers

# ─────────────────────────── PDF Download ───────────────────────────
def download_pdf(url: str, referer: str) -> bytes | None:
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "60",
             "-A", UA, "-H", f"Referer: {referer}", "-H", "Accept: application/pdf,*/*",
             url],
            capture_output=True, timeout=70,
        )
        data = r.stdout
        if data and data[:4] == b"%PDF":
            return data
        log.error(f"download_pdf: ไม่ใช่ PDF (received {len(data)} bytes, starts: {data[:20]})")
        return None
    except Exception as e:
        log.error(f"download_pdf exception: {e}")
        return None

# ─────────────────────────── Date Extraction (Thai, generic) ───────────────────────────
def get_effective_date(pdf_bytes: bytes) -> str | None:
    """ดึงวันที่มีผลจาก PDF → YYYY-MM-DD (ค.ศ.). Thai date parser แบบทั่วไป
    ธนาคารที่มี format ต่างสามารถ override ฟังก์ชันนี้ใน banks/<code>.py ได้"""
    month_pat = "|".join(THAI_MONTHS.keys())
    date_re = re.compile(rf"(\d{{1,2}})\s+({month_pat})\s+(\d{{4}})")
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = pdf.pages[0].extract_text() or ""
            m = date_re.search(text)
            if m:
                day   = int(m.group(1))
                month = THAI_MONTHS[m.group(2)]
                year  = int(m.group(3)) - 543
                return f"{year:04d}-{month:02d}-{day:02d}"
    except Exception as e:
        log.error(f"get_effective_date: {e}")
    return None

# ─────────────────────────── CSV Helpers ───────────────────────────
def get_latest_csv_row(csv_path: str) -> dict | None:
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
            return rows[-1] if rows else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.error(f"get_latest_csv_row: {e}")
        return None


def get_prev_rates(row: dict | None, rate_targets: list[dict]) -> dict | None:
    if row is None:
        return None
    try:
        return {t["key"]: float(row[t["key"]]) for t in rate_targets}
    except Exception:
        return None


def _fmt_change(change: float | None) -> str:
    if change is None:
        return ""
    return f"+{change:.2f}" if change > 0 else (f"{change:.2f}" if change < 0 else "0.00")


def _reconcile_csv_header(csv_path: str, headers: list[str]) -> None:
    """ถ้า header ปัจจุบันของไฟล์ต่างจาก headers ที่คาด (เช่น เพิ่ม/ลบ rate_target)
    ให้ rewrite ทั้งไฟล์ด้วย header ใหม่ (แถวเก่าเติมค่าว่างในคอลัมน์ที่เพิ่มมาใหม่)"""
    if not (os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0):
        return
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        current_headers = reader.fieldnames or []
        rows = list(reader)
    if current_headers == headers:
        return
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers, restval="")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in headers})
    os.replace(tmp, csv_path)
    log.info(f"CSV header reconciled: {os.path.basename(csv_path)} "
             f"({len(current_headers)} → {len(headers)} คอลัมน์)")


def append_to_csv(csv_path: str, date_iso: str, rates: dict,
                  prev_rates: dict | None, rate_targets: list[dict]) -> dict:
    headers = get_csv_headers(rate_targets)
    _reconcile_csv_header(csv_path, headers)
    changes: dict = {}
    for t in rate_targets:
        k = t["key"]
        chg_k = f"change_{k.split('rate_')[1]}"
        changes[chg_k] = round(rates[k] - prev_rates[k], 4) if prev_rates else None

    row = {"effective_date": date_iso}
    for t in rate_targets:
        k     = t["key"]
        chg_k = f"change_{k.split('rate_')[1]}"
        row[k]     = f"{rates[k]:.2f}"
        row[chg_k] = _fmt_change(changes[chg_k])

    file_exists = os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0
    with open(csv_path, "a" if file_exists else "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            w.writeheader()
        w.writerow(row)

    return changes

# ─────────────────────────── Sanity Check ───────────────────────────
def check_warnings(rates: dict, prev_rates: dict | None, rate_targets: list[dict]) -> list[str]:
    if prev_rates is None:
        return []
    warnings = []
    for t in rate_targets:
        k = t["key"]
        if prev_rates.get(k) is not None:
            change = rates[k] - prev_rates[k]
            if abs(change) > RATE_CHANGE_THRESHOLD:
                msg = f"{t['label']}: เปลี่ยนแปลง {change:+.2f}% (เกินกว่า ±{RATE_CHANGE_THRESHOLD}%)"
                warnings.append(msg)
                log.warning(msg)
    return warnings

# ─────────────────────────── Result JSON (per-bank) ───────────────────────────
def write_result(result_type: str, **kwargs):
    """เขียนผลรันล่าสุดลง {code}_result.json (แยกไฟล์ต่อธนาคาร กัน race ตอน parallel)"""
    bank_code = kwargs.get("bank", "unknown")
    data = {"type": result_type, "timestamp": datetime.now().isoformat(timespec="seconds"), **kwargs}
    path = os.path.join(OUTPUT_DIR, f"{str(bank_code).lower()}_result.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log.info(f"Result written: {os.path.basename(path)} type={result_type}")

# ─────────────────────────── Email (SMTP) ───────────────────────────
def get_recipients() -> list[str]:
    """ผู้รับอีเมล: จาก settings.json (email_to) ก่อน ไม่งั้น fallback env EMAIL_TO.
    รองรับทั้ง string (คั่นด้วย , หรือ ;) และ list"""
    to = load_settings().get("email_to")
    if not to:
        to = os.environ.get("EMAIL_TO", "")
    if isinstance(to, str):
        return [x.strip() for x in re.split(r"[;,]", to) if x.strip()]
    if isinstance(to, (list, tuple)):
        return [str(x).strip() for x in to if str(x).strip()]
    return []


def send_email(subject: str, html_body: str) -> bool:
    """ส่งอีเมล HTML ผ่าน SMTP (SSL 465 หรือ STARTTLS 587). คงลายเซ็นเดิม (subject, html) -> bool"""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465") or "465")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM") or user
    recipients = get_recipients()

    if not host or not user or not password:
        log.error("send_email failed: SMTP config ไม่ครบ (ต้องมี SMTP_HOST/SMTP_USER/SMTP_PASSWORD)")
        return False
    if not recipients:
        log.error("send_email failed: ไม่มีผู้รับ (ตั้ง email_to ใน settings.json หรือ EMAIL_TO)")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)

        log.info(f"Email sent → {', '.join(recipients)}  subject: {subject}")
        return True
    except Exception as e:
        log.error(f"send_email failed: {e}")
        return False

# ─────────────────────────── Email Builders ───────────────────────────
def _fmt_rate(val, prev_val) -> tuple[str, str, str]:
    new_s = f"{val:.2f}%" if val is not None else "-"
    old_s = f"{prev_val:.2f}%" if prev_val is not None else "-"
    chg_s = (f"{(val - prev_val):+.2f}%" if val is not None and prev_val is not None else "-")
    return new_s, old_s, chg_s


def build_new_rates_email(bank: dict, eff_date: str, prev_date: str | None,
                          rates: dict, prev_rates: dict | None, warnings: list[str],
                          pdf_fname: str) -> tuple[str, str]:
    subject = f"[{bank['code']}] อัตราดอกเบี้ยเงินฝากประจำ มีผลตั้งแต่ {eff_date}"
    rows_html = ""
    for t in bank["rate_targets"]:
        k = t["key"]
        new_s, old_s, chg_s = _fmt_rate(rates.get(k), prev_rates.get(k) if prev_rates else None)
        rows_html += (f"<tr><td>{t['label']}</td>"
                      f"<td align='right'>{new_s}</td>"
                      f"<td align='right'>{old_s}</td>"
                      f"<td align='right'>{chg_s}</td></tr>\n")

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = (f"<p>⚠️ <strong>พบการเปลี่ยนแปลงที่ผิดปกติ (เกิน {RATE_CHANGE_THRESHOLD}%)</strong><br>"
                     f"กรุณาตรวจสอบข้อมูลจาก PDF ต้นฉบับก่อนใช้งาน<ul>{items}</ul></p>")

    html = f"""
<p>{bank['name']} ({bank['code']}) ประกาศอัตราดอกเบี้ยใหม่ มีผลตั้งแต่ <strong>{eff_date}</strong><br>
(เปลี่ยนจากประกาศ {prev_date or '-'})</p>

<table border="1" cellpadding="6" cellspacing="0"
       style="border-collapse:collapse;font-family:monospace;font-size:14px">
  <tr style="background:#f0f0f0">
    <th>ประเภท</th><th>อัตราใหม่</th><th>อัตราเก่า</th><th>เปลี่ยน</th>
  </tr>
  {rows_html}
</table>
{warn_html}
<hr>
<p style="font-size:12px;color:#888">📎 PDF: {pdf_fname}<br>
📊 ประวัติ: {bank['code'].lower()}_deposit_rate.csv</p>"""
    return subject, html


def build_error_email(bank: dict, step: str, message: str, ts: str) -> tuple[str, str]:
    subject = f"[{bank['code']} ERROR] ระบบติดตามอัตราดอกเบี้ยเกิดข้อผิดพลาด {ts[:10]}"
    html = f"""
<p>❌ <strong>พบข้อผิดพลาด — {bank['name']} ({bank['code']})</strong></p>
<table cellpadding="6">
  <tr><td><strong>วันที่รัน</strong></td><td>{ts}</td></tr>
  <tr><td><strong>ขั้นตอนที่ล้มเหลว</strong></td><td>{step}</td></tr>
  <tr><td><strong>รายละเอียด</strong></td><td>{message}</td></tr>
</table>
<p style="font-size:12px;color:#888">Log: {LOG_PATH}</p>"""
    return subject, html


def build_test_email() -> tuple[str, str]:
    """อีเมลทดสอบ — ใช้ verify ค่า SMTP ผ่านปุ่มบนหน้าเว็บ / CLI --test-email"""
    ts = datetime.now().isoformat(timespec="seconds")
    host = os.environ.get("SMTP_HOST", "-")
    port = os.environ.get("SMTP_PORT", "-")
    user = os.environ.get("SMTP_USER", "-")
    recipients = ", ".join(get_recipients()) or "-"
    subject = f"[TEST] ทดสอบระบบส่งอีเมล CheckRate {ts[:10]}"
    html = f"""
<p>✅ <strong>ทดสอบส่งอีเมลสำเร็จ</strong> — ระบบ CheckRate เชื่อมต่อ SMTP ได้เรียบร้อย</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:14px">
  <tr><td><strong>เวลา</strong></td><td>{ts}</td></tr>
  <tr><td><strong>SMTP host</strong></td><td>{host}:{port}</td></tr>
  <tr><td><strong>ผู้ส่ง</strong></td><td>{user}</td></tr>
  <tr><td><strong>ผู้รับ</strong></td><td>{recipients}</td></tr>
</table>
<p style="font-size:12px;color:#888">อีเมลนี้ส่งจากปุ่ม "ทดสอบส่งอีเมล" หรือคำสั่ง <code>--test-email</code></p>"""
    return subject, html
