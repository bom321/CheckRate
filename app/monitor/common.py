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
import contextlib, contextvars, hashlib
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
# ธนาคารที่กำลังทำงานอยู่ใน thread นี้ — ตั้งด้วย bank_log_context() ตอนเริ่มงานของแต่ละธนาคาร
# ContextVar แยกกันต่อ thread อยู่แล้ว ธนาคารที่รันขนานกันจึงไม่ปนแท็กกัน
_current_bank: contextvars.ContextVar[str] = contextvars.ContextVar("current_bank", default="")


@contextlib.contextmanager
def bank_log_context(code: str):
    """ครอบงานของธนาคารหนึ่ง — log ทุกบรรทัดที่เกิดข้างในจะได้แท็ก [CODE] อัตโนมัติ"""
    token = _current_bank.set(code or "")
    try:
        yield
    finally:
        _current_bank.reset(token)


class _BankTagFilter(logging.Filter):
    """เติมแท็ก [CODE] ให้ทุกบรรทัดที่ยังไม่มี — หน้าเว็บกรอง log รายธนาคารด้วยแท็กนี้ (tail_log)

    ติดไว้ที่ logger ไม่ใช่ที่ handler เพื่อให้ทำงานครั้งเดียวต่อ record (ไม่งั้นแท็กจะซ้ำ)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        code = _current_bank.get()
        if code:
            msg = str(record.msg)
            if not msg.lstrip().startswith(f"[{code}]"):
                record.msg = f"[{code}] {msg}"
        return True


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("deposit_monitor")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.addFilter(_BankTagFilter())
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


def is_date_in_year(eff_date: str | None, year: int | None) -> bool:
    """True ถ้า eff_date (YYYY-MM-DD) อยู่ในปีที่ขอ (ค.ศ.) — ใช้กันไฟล์ "ผิดปี" หลุดเข้ามาตอน discover_year

    ผู้เรียก (scb.py/ktb.py/bbl.py) ต้อง resolve `yr = year or datetime.now().year` ให้เป็น int ที่แน่นอน
    ก่อนเรียกฟังก์ชันนี้เสมอ — ไม่ใช่ส่ง `year` (ที่อาจเป็น None) ตรง ๆ ไม่งั้นตอนรัน discover แบบไม่เลือกปี
    (year=None) จะไม่ได้กรองอะไรเลย รั่วเหมือนเดิม ถ้าเผลอเรียกด้วย year=None จริง ๆ ฟังก์ชันนี้ fail-closed
    (คืน False เสมอ = ไม่บันทึกไฟล์) ดีกว่าปล่อยผ่านทุกปีแบบเงียบ ๆ
    """
    if not eff_date or year is None:
        return False
    return eff_date[:4].isdigit() and int(eff_date[:4]) == year


def change_col(key: str) -> str:
    """ชื่อคอลัมน์ change ของ target — รองรับ key ที่ไม่ได้ขึ้นต้นด้วย 'rate_'
    (เช่น key='saving_epb' → 'change_saving_epb', key='rate_3m_1m' → 'change_3m_1m')"""
    _, sep, suffix = key.partition("rate_")
    return f"change_{suffix if sep else key}"


def get_csv_headers(rate_targets: list[dict]) -> list[str]:
    headers = ["effective_date"]
    for t in rate_targets:
        k = t["key"]
        headers += [k, change_col(k)]
    return headers

# ─────────────────────────── Parse cache (backfill) ───────────────────────────
# backfill re-parse PDF ทุกไฟล์ทุกครั้ง ซึ่งแพงมาก (BBL ต้อง OCR ~3.5 วิ/ไฟล์, SCB ~4.6 วิ/ไฟล์)
# ทั้งที่ผลลัพธ์ของไฟล์เดิม + target เดิม ย่อมเหมือนเดิมเสมอ → cache ผล extract_rates() ไว้ต่อไฟล์
#
# cache หมดอายุเองเมื่ออย่างใดอย่างหนึ่งเปลี่ยน:
#   sha256      — เนื้อไฟล์ PDF (ไฟล์ถูกแทนที่ด้วยฉบับแก้ไข)
#   targets_sig — rate_targets ของธนาคารนั้น (ผู้ใช้เพิ่ม/แก้ tier ในหน้า /config)
#   parser_ver  — เวอร์ชัน parser (bump เองใน PARSER_VERSION เมื่อแก้ logic การอ่านค่า)
def parse_cache_path(bank_code: str) -> str:
    return os.path.join(OUTPUT_DIR, "parse_cache", f"{bank_code.lower()}.json")


def targets_signature(bank: dict) -> str:
    """ลายเซ็นของ rate_targets — เปลี่ยน target แล้ว cache ของธนาคารนั้นต้องถือว่าใช้ไม่ได้"""
    raw = json.dumps(bank.get("rate_targets", []), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_parse_cache(bank_code: str) -> dict:
    """คืน {fname: {"sha256":…, "targets_sig":…, "parser_ver":…, "rates": {…}}} — ไฟล์เสีย = เริ่มใหม่"""
    try:
        with open(parse_cache_path(bank_code), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.warning(f"load_parse_cache({bank_code}): {e} — จะ parse ใหม่ทั้งหมด")
        return {}


def save_parse_cache(bank_code: str, cache: dict) -> None:
    """เขียน cache แบบ atomic — cache พังไม่ควรทำให้ backfill ล้ม แค่ช้าลง"""
    path = parse_cache_path(bank_code)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"save_parse_cache({bank_code}): {e}")

# ─────────────────────────── Manual override (admin กรอกค่าเอง) ───────────────────────────
# ค่าที่ OCR อ่านไม่ได้จริง ๆ หรืออ่านผิด (พบจริง: "1.25" → "L25", "0.30" → "0,30") — admin กรอกทับได้
# จากหน้า /bank/{code}/manual เก็บ**แยกไฟล์จาก parse_cache โดยเจตนา**: cache ต้องเป็นผล OCR ล้วน ๆ
# (ใช้ตรวจ/debug parser ได้ตรงไปตรงมา) ส่วนไฟล์นี้เป็นของคน ถ้าปนกันจะสืบไม่ได้ว่าเลขไหนมาจากไหน
# backfill_bank()/run_bank() เรียก apply_manual() ทับค่าหลังได้ rates จาก cache/parse เสร็จเสมอ
# จึงไม่ถูกล้างทิ้งตอน backfill สร้าง CSV ใหม่ทั้งไฟล์ (ต่างจากค่าที่เก็บใน CSV ตรง ๆ ซึ่งจะหายทุกรอบ)
MANUAL_RATE_MIN, MANUAL_RATE_MAX = 0.0, 10.0   # ช่วงเดียวกับ MAX_PLAUSIBLE_RATE ที่ parser ใช้เช็ค


def manual_path(bank_code: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{bank_code.lower()}_manual.json")


def load_manual(bank_code: str) -> dict:
    """คืน {date_iso: {key: {"value":…, "by":…, "at":…}}} — ไฟล์เสีย/ไม่มี = ไม่มี override"""
    try:
        with open(manual_path(bank_code), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.warning(f"load_manual({bank_code}): {e} — ถือว่าไม่มี override")
        return {}


def save_manual(bank_code: str, data: dict) -> None:
    """เขียน manual override แบบ atomic — ไม่ silently swallow exception (ต่างจาก save_parse_cache)
    เพราะค่าที่กรอกเองหายไปเงียบ ๆ ร้ายแรงกว่า cache miss มาก ผู้เรียก (API) ต้องรู้ว่าบันทึกไม่สำเร็จ"""
    path = manual_path(bank_code)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def apply_manual(bank_code: str, date_iso: str, rates: dict) -> tuple[dict, list[str]]:
    """ทับค่าใน rates ด้วยค่าที่กรอกเองของประกาศวันนี้ (ถ้ามี) — คืน (rates ใหม่, รายชื่อ key ที่ถูกทับ)
    คืน dict ใหม่เสมอ ไม่แก้ของเดิม (กัน caller ที่อาจอ้าง reference เดิมอยู่ เช่น parse cache entry)"""
    overrides = load_manual(bank_code).get(date_iso)
    if not overrides:
        return rates, []
    result = dict(rates)
    applied = []
    for key, entry in overrides.items():
        value = entry.get("value") if isinstance(entry, dict) else entry
        if value is None:
            continue
        try:
            result[key] = float(value)
            applied.append(key)
        except (TypeError, ValueError):
            log.warning(f"apply_manual({bank_code}, {date_iso}): ค่า override ของ '{key}' ไม่ใช่ตัวเลข: {value!r}")
    return result, applied

# ─────────────────────────── PDF Download ───────────────────────────
def _download_pdf_curl(url: str, referer: str) -> bytes | None:
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
        if b"_Incapsula_Resource" in data:
            log.info("download_pdf: โดน Incapsula challenge — สลับไปโหมด impersonate (ปลดบล็อกอัตโนมัติ)")
            return _download_pdf_impersonate(url, referer)
        log.error(f"download_pdf: ไม่ใช่ PDF (received {len(data)} bytes, starts: {data[:20]})")
        return None
    except Exception as e:
        log.error(f"download_pdf exception: {e}")
        return None


# Incapsula (Imperva) — หน้า challenge ที่บล็อกจะเป็น HTML สั้น ๆ (~200-500 ไบต์) ฝัง
# <script src="/_Incapsula_Resource?SWJIYLWA=..."> ยืนยันกับ krungthai.com แล้วว่าแค่ GET
# สคริปต์นั้นด้วย session เดิม (cookie visid_incap/incap_ses เดิม) เซิร์ฟเวอร์ก็ปลดบล็อก session
# ให้เลย — ไม่ต้องรัน JS จริง จากนั้นยิง request เดิมซ้ำจะได้ของจริง
_INCAPSULA_SCRIPT_RE = re.compile(r'src="(/_Incapsula_Resource[^"]+)"')


def solve_incapsula_challenge(session, blocked_text: str, base_url: str) -> bool:
    """พยายามปลดบล็อก Incapsula ให้ session ที่โดน challenge — คืน True ถ้าโหลดสคริปต์ปลดบล็อกสำเร็จ
    (ผู้เรียกต้องยิง request เดิมซ้ำเองอีกครั้ง)"""
    m = _INCAPSULA_SCRIPT_RE.search(blocked_text)
    if not m:
        return False
    try:
        r = session.get(base_url + m.group(1), timeout=30)
        log.info(f"solve_incapsula_challenge: โหลดสคริปต์ปลดบล็อกแล้ว (HTTP {r.status_code})")
        return r.status_code == 200
    except Exception as e:
        log.warning(f"solve_incapsula_challenge: โหลดสคริปต์ปลดบล็อกไม่สำเร็จ: {e}")
        return False


def _download_pdf_impersonate(url: str, referer: str) -> bytes | None:
    """ดาวน์โหลดผ่าน curl_cffi (เลียนลายนิ้วมือ TLS ของ Chrome) — ใช้กับเว็บที่มี
    bot protection แบบ Akamai/Cloudflare/Incapsula ที่บล็อก curl ธรรมดา (เช่น KBANK, KTB)
    ถ้าเจอ challenge ของ Incapsula จะปลดบล็อกแล้วลองซ้ำอีกหนึ่งครั้ง"""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.error("download_pdf (impersonate): ไม่ได้ติดตั้ง curl_cffi (pip install curl_cffi)")
        return None
    headers = {"Referer": referer, "Accept": "application/pdf,*/*"}
    try:
        session = cffi_requests.Session(impersonate="chrome")
        r = session.get(url, timeout=60, headers=headers)
        data = r.content
        if not (data and data[:4] == b"%PDF"):
            base_url = "/".join(url.split("/", 3)[:3])  # scheme://host
            if solve_incapsula_challenge(session, r.text, base_url):
                r = session.get(url, timeout=60, headers=headers)
                data = r.content
        if data and data[:4] == b"%PDF":
            return data
        log.error(f"download_pdf (impersonate): ไม่ใช่ PDF (HTTP {r.status_code}, "
                  f"{len(data)} bytes, starts: {data[:20]})")
        return None
    except Exception as e:
        log.error(f"download_pdf (impersonate) exception: {e}")
        return None


def download_pdf(url: str, referer: str, mode: str = "curl") -> bytes | None:
    """ดาวน์โหลด PDF ประกาศ mode='curl' (ค่าเริ่มต้น, SCB ฯลฯ) หรือ 'impersonate'
    (bypass bot-protection ด้วย curl_cffi — ใช้กับธนาคารที่ตั้ง fetch_mode: curl-impersonate)"""
    if mode == "impersonate":
        return _download_pdf_impersonate(url, referer)
    return _download_pdf_curl(url, referer)

# ─────────────────────────── Date Extraction (Thai, generic) ───────────────────────────
# pdfplumber บางครั้งถอดข้อความไทยแล้วสระสลับตำแหน่ง/มีช่องว่างแทรกกลางคำ (เช่น "มีนาคม" -> "มนี าคม" —
# ี กับ น สลับกันด้วยซ้ำ ไม่ใช่แค่เว้นวรรค) ทำให้ regex ชื่อเดือนแบบตรงตัวพลาดได้ จึงจับคู่ด้วย "skeleton"
# (เก็บเฉพาะพยัญชนะไทย ตัดสระ/วรรณยุกต์/เว้นวรรคทิ้ง) เทคนิคเดียวกับ banks/_tablekit.py แต่คัดลอกแยกไว้
# ที่นี่ (ไม่ import จาก banks/ เพราะ banks/__init__.py import จาก common.py อยู่แล้ว — จะเกิด circular import)
_THAI_CONSONANT_RE = re.compile(r"[ก-ฮ]|[a-z0-9]")


def _thai_skeleton(s: str) -> str:
    return "".join(_THAI_CONSONANT_RE.findall(s.lower()))


_DATE_CANDIDATE_RE = re.compile(r"(\d{1,2})\s*(.{2,15}?)\s*(\d{4})")


def get_effective_date(pdf_bytes: bytes) -> str | None:
    """ดึงวันที่มีผลจาก PDF → YYYY-MM-DD (ค.ศ.). Thai date parser แบบทั่วไป
    ธนาคารที่มี format ต่างสามารถ override ฟังก์ชันนี้ใน banks/<code>.py ได้
    จับคู่ชื่อเดือนด้วย skeleton (ดูหมายเหตุด้านบน) ทนข้อความไทยที่ pdfplumber ถอดเพี้ยน"""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        log.error(f"get_effective_date: {e}")
        return None

    for m in _DATE_CANDIDATE_RE.finditer(text):
        day_s, mid, year_s = m.groups()
        mid_sk = _thai_skeleton(mid)
        for month_name, month_num in THAI_MONTHS.items():
            if _thai_skeleton(month_name) == mid_sk:
                try:
                    return f"{int(year_s) - 543:04d}-{month_num:02d}-{int(day_s):02d}"
                except ValueError:
                    continue
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


def build_csv_row(date_iso: str, rates: dict, prev_rates: dict | None,
                  rate_targets: list[dict]) -> tuple[dict, dict]:
    """สร้างแถว CSV หนึ่งแถว + dict ของ change ต่อ target (ไม่แตะไฟล์)"""
    changes: dict = {}
    for t in rate_targets:
        k = t["key"]
        chg_k = change_col(k)
        if k in rates and prev_rates and prev_rates.get(k) is not None:
            changes[chg_k] = round(rates[k] - prev_rates[k], 4)
        else:
            changes[chg_k] = None

    row = {"effective_date": date_iso}
    for t in rate_targets:
        k     = t["key"]
        chg_k = change_col(k)
        # target ที่ถูกข้าม (ไม่มีใน rates) → เขียนช่องว่างไว้ ไม่ทำให้ทั้งแถวพัง
        if k in rates:
            row[k]     = f"{rates[k]:.2f}"
            row[chg_k] = _fmt_change(changes[chg_k])
        else:
            row[k]     = ""
            row[chg_k] = ""
    return row, changes


def write_csv_atomic(csv_path: str, rows: list[dict], rate_targets: list[dict]) -> None:
    """เขียน CSV ทั้งไฟล์แบบ atomic — ใช้ตอน backfill ที่สร้างไฟล์ใหม่ทั้งใบ

    สำคัญ: backfill เดิม truncate ไฟล์จริงทิ้งก่อนแล้วค่อย parse ทีละไฟล์ ถ้าถูกฆ่ากลางคัน
    (เช่น subprocess timeout จากหน้าเว็บ) CSV จะเหลือแค่ header — ข้อมูลประวัติหายทั้งก้อน
    """
    headers = get_csv_headers(rate_targets)
    tmp = f"{csv_path}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers, restval="")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in headers})
    os.replace(tmp, csv_path)


def append_to_csv(csv_path: str, date_iso: str, rates: dict,
                  prev_rates: dict | None, rate_targets: list[dict]) -> dict:
    headers = get_csv_headers(rate_targets)
    _reconcile_csv_header(csv_path, headers)
    row, changes = build_csv_row(date_iso, rates, prev_rates, rate_targets)

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
        if k in rates and prev_rates.get(k) is not None:
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


def send_email(subject: str, html_body: str, to: list[str] | None = None) -> bool:
    """ส่งอีเมล HTML ผ่าน SMTP (SSL 465 หรือ STARTTLS 587). ไม่ส่ง `to` = ผู้รับตาม get_recipients() เดิม"""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465") or "465")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM") or user
    recipients = to or get_recipients()

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
        # warnings รวม 2 ประเภท: เปลี่ยนแปลงเกิน threshold (check_warnings) และ target ที่อ่านค่าไม่ได้
        # (run_bank ส่วน 7) — หัวข้อจึงต้องเป็นกลาง ไม่เจาะจงแค่ "เปลี่ยนแปลงผิดปกติ" เหมือนเดิม
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = (f"<p>⚠️ <strong>ข้อควรระวัง</strong><br>"
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
