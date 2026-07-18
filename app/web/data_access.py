#!/usr/bin/env python3
"""
data_access.py — ชั้นอ่านข้อมูลสำหรับเว็บ Dashboard

อ่าน config / CSV / PDF / log / result / settings จาก DATA_DIR (env เดียวกับ monitor)
ออกแบบให้ **ทนไฟล์หาย** — ธนาคารที่ยังไม่มี CSV/PDF จะไม่ทำให้เว็บพัง
ใช้ helper ร่วมจาก app.monitor.common เพื่อไม่ให้ path/logic ซ้ำซ้อน
"""

import os, csv, json, re, secrets, threading
from datetime import datetime

from ..monitor import common
from ..monitor import banks as monitor_banks

DATA_DIR = common.OUTPUT_DIR
LOG_PATH = common.LOG_PATH


# ─────────────────────────── Config / banks ───────────────────────────
def load_banks() -> list[dict]:
    """คืน bank ทั้งหมด (รวมที่ disabled). ทนกรณีไฟล์หาย → []"""
    try:
        return common.load_config(enabled_only=False)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def get_bank(code: str) -> dict | None:
    for b in load_banks():
        if b["code"].upper() == code.upper():
            return b
    return None


def supports_discover_year(bank: dict) -> bool:
    """True ถ้าธนาคารนี้รองรับการสแกนหาประวัติทั้งปีแบบละเอียด (เช่น KBANK)"""
    return monitor_banks.supports_discover_year(bank)


def save_banks(banks: list[dict]) -> None:
    """เขียน banks_config.json แบบ atomic (temp → replace)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = common.CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"banks": banks}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, common.CONFIG_PATH)


# ─────────────────────────── CSV history ───────────────────────────
def _csv_path(code: str) -> str:
    _, csv_path = common.get_bank_paths(code)
    return csv_path


def bank_has_csv(code: str) -> bool:
    return os.path.isfile(_csv_path(code))


def read_history(code: str) -> list[dict]:
    """คืนทุกแถวของ CSV (list ของ dict) เรียงตาม effective_date + กันแถวซ้ำ.
    ถ้าไม่มีไฟล์ → []. ทนกรณี CSV สลับลำดับ/มีวันที่ซ้ำ (เก็บแถวหลังสุดของแต่ละวันที่)"""
    path = _csv_path(code)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    by_date: dict[str, dict] = {}
    for r in rows:
        d = (r.get("effective_date") or "").strip()
        if d:
            by_date[d] = r          # วันที่ซ้ำ → เก็บแถวหลังสุด
    return [by_date[d] for d in sorted(by_date)]


def latest_two_rows(code: str) -> tuple[dict | None, dict | None]:
    """คืน (current, previous) = 2 แถวท้ายสุด (previous = None ถ้ามีแถวเดียว)"""
    rows = read_history(code)
    if not rows:
        return None, None
    if len(rows) == 1:
        return rows[-1], None
    return rows[-1], rows[-2]


def csv_mtime(code: str) -> str | None:
    """เวลาแก้ไขไฟล์ CSV ล่าสุด (ISO) — ใช้เป็น 'ตรวจสอบล่าสุด' สำรอง"""
    path = _csv_path(code)
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except OSError:
        return None


# ─────────────────────────── PDFs ───────────────────────────
def list_pdfs(code: str) -> list[str]:
    """รายชื่อไฟล์ PDF ใน pdfs/{CODE}/ (ใหม่สุดก่อน). ข้าม .DS_Store"""
    pdf_dir, _ = common.get_bank_paths(code)
    try:
        files = [f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf")]
    except FileNotFoundError:
        return []
    return sorted(files, reverse=True)


def pdf_for_date(code: str, effective_date: str) -> str | None:
    """หาไฟล์ PDF ที่ตรงกับวันที่ (เช่นในตารางประวัติ)"""
    fname = f"{code.lower()}_deposit_{effective_date}.pdf"
    return fname if fname in list_pdfs(code) else None


def pdf_abspath(code: str, filename: str) -> str:
    pdf_dir, _ = common.get_bank_paths(code)
    return os.path.join(pdf_dir, filename)


# ─────────────────────────── Upload ประกาศเอง (admin) ───────────────────────────
def effective_date_from_pdf(bank: dict, pdf_bytes: bytes) -> str | None:
    """อ่านวันที่มีผลจากเนื้อ PDF ด้วย dispatcher เดียวกับ monitor — คืน YYYY-MM-DD หรือ None ถ้าอ่านไม่ได้"""
    try:
        return monitor_banks.effective_date(pdf_bytes, bank)
    except Exception:
        return None


def uploaded_pdf_exists(code: str, effective_date: str) -> bool:
    return pdf_for_date(code, effective_date) is not None


def save_uploaded_pdf(code: str, effective_date: str, pdf_bytes: bytes) -> str:
    """เซฟไฟล์ที่อัปโหลดลง pdfs/{CODE}/{code}_deposit_{date}.pdf แบบ atomic (เขียนทับไฟล์เดิมถ้ามี)
    — คืนชื่อไฟล์. ชื่อไฟล์ = source of truth ของประวัติ (--backfill เอาวันที่จากชื่อไฟล์)"""
    pdf_dir, _ = common.get_bank_paths(code)
    os.makedirs(pdf_dir, exist_ok=True)
    fname = f"{code.lower()}_deposit_{effective_date}.pdf"
    tmp = os.path.join(pdf_dir, fname + ".tmp")
    with open(tmp, "wb") as f:
        f.write(pdf_bytes)
    os.replace(tmp, os.path.join(pdf_dir, fname))
    return fname


_PDF_DATE_RE = re.compile(r"_deposit_(\d{4})-(\d{2})-(\d{2})\.pdf$")


def list_pdfs_by_year(code: str) -> list[dict]:
    """จัดกลุ่มไฟล์ PDF ทั้งหมดที่เก็บไว้ (จาก list_pdfs — ของจริงบนดิสก์ ไม่ใช่แถว CSV เพราะ
    แถว/ไฟล์ไม่การันตี 1:1) แยกตามปี (ใหม่→เก่า, ไฟล์ในแต่ละปีก็ใหม่→เก่า) เพื่อให้ดาวน์โหลดสอบทานได้ครบ
    ไฟล์ที่ชื่อไม่ตรง pattern วันที่มาตรฐาน (ถ้ามี) จะถูกจัดเข้ากลุ่ม 'อื่น ๆ' ท้ายสุด ไม่ทิ้งไฟล์ไหน"""
    groups: dict[str, list[dict]] = {}
    others: list[dict] = []
    for fname in list_pdfs(code):
        m = _PDF_DATE_RE.search(fname)
        try:
            size_kb = round(os.path.getsize(pdf_abspath(code, fname)) / 1024, 1)
        except OSError:
            size_kb = None
        if m:
            year = m.group(1)
            groups.setdefault(year, []).append(
                {"name": fname, "date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "size_kb": size_kb})
        else:
            others.append({"name": fname, "date": None, "size_kb": size_kb})

    result = [{"year": y, "files": groups[y]} for y in sorted(groups, reverse=True)]
    if others:
        result.append({"year": "อื่น ๆ", "files": others})
    return result


# ─────────────────────────── Result JSON (per-bank) ───────────────────────────
def load_result(code: str) -> dict | None:
    path = os.path.join(DATA_DIR, f"{code.lower()}_result.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:
        return None


def last_checked(code: str) -> str | None:
    """เวลาตรวจสอบล่าสุด: จาก result.json (timestamp) ก่อน ไม่งั้นใช้ mtime CSV"""
    res = load_result(code)
    if res and res.get("timestamp"):
        return res["timestamp"]
    return csv_mtime(code)


# ─────────────────────────── Manual override (admin กรอกค่าเอง) ───────────────────────────
MANUAL_RATE_MIN = common.MANUAL_RATE_MIN
MANUAL_RATE_MAX = common.MANUAL_RATE_MAX


def load_manual(code: str) -> dict:
    return common.load_manual(code)


def save_manual(code: str, data: dict) -> None:
    common.save_manual(code, data)


# ─────────────────────────── Settings ───────────────────────────
def load_settings() -> dict:
    return common.load_settings()


def save_settings(settings: dict) -> None:
    common.save_settings(settings)


def get_recipients() -> list[str]:
    return common.get_recipients()


# ─────────────────────────── คำขอจากผู้ใช้ทั่วไป (requests.json) ───────────────────────────
# ไฟล์นี้ฝั่งเว็บ *เขียนเอง* (ต่างจาก config/manual ที่เป็น config) — monitor ไม่เคยอ่าน จึงไม่ผิด
# หลักแยก monitor/web (ดู CLAUDE.md) เป็น state ของฝั่งเว็บล้วน public POST ตัวเดียวที่ไม่ต้อง login
# จึงต้องกันสแปมเองในชั้น endpoint (main.py) ส่วนที่นี่รับผิดชอบแค่อ่าน/เขียน atomic + คุมขนาดไฟล์
REQUESTS_PATH = os.path.join(DATA_DIR, "requests.json")
MAX_REQUESTS_STORED = 500          # เกินนี้ตัดตัวเก่าสุดทิ้งตอนบันทึก (กันไฟล์โตไม่จำกัด)
REQUEST_MESSAGE_MAX = 1000         # ความยาวข้อความสูงสุด (ตัดที่ endpoint ก่อนถึงที่นี่)
REQUEST_TYPES = {"update", "wrong", "newbank"}     # ตรงกับดีไซน์ claude design
REQUEST_STATUSES = {"new", "done", "closed"}

_requests_lock = threading.Lock()   # กัน public POST หลายคำขอเขียนชนกัน (โปรเซสเดียว ไม่มี --workers)


def load_requests() -> list[dict]:
    """คืนคำขอทั้งหมด (เก่า→ใหม่ ตามลำดับที่บันทึก). ทนไฟล์หาย/พัง → []"""
    try:
        with open(REQUESTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []
    items = data.get("requests") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def _save_requests(items: list[dict]) -> None:
    """เขียน requests.json แบบ atomic (temp → replace) เหมือน save_banks — ตัดเหลือ MAX ตัวล่าสุด"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if len(items) > MAX_REQUESTS_STORED:
        items = items[-MAX_REQUESTS_STORED:]
    tmp = REQUESTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"requests": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REQUESTS_PATH)


def add_request(rec: dict) -> dict:
    """เติม id/created_at/status แล้ว append+save ภายใต้ lock — คืน record ที่บันทึกจริง"""
    rec = dict(rec)
    rec.setdefault("id", secrets.token_hex(4))
    rec.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    rec.setdefault("status", "new")
    rec.setdefault("handled_by", None)
    rec.setdefault("handled_at", None)
    with _requests_lock:
        items = load_requests()
        items.append(rec)
        _save_requests(items)
    return rec


def set_request_status(req_id: str, status: str, admin_email: str | None) -> bool:
    """อัปเดตสถานะคำขอ (admin) — คืน True ถ้าเจอและแก้แล้ว"""
    if status not in REQUEST_STATUSES:
        return False
    with _requests_lock:
        items = load_requests()
        for r in items:
            if r.get("id") == req_id:
                r["status"] = status
                # กลับมาเป็น new = เปิดใหม่ ล้างผู้จัดการ; ปิดงาน/ทำแล้ว = บันทึกคนกด+เวลา
                if status == "new":
                    r["handled_by"], r["handled_at"] = None, None
                else:
                    r["handled_by"] = admin_email
                    r["handled_at"] = datetime.now().isoformat(timespec="seconds")
                _save_requests(items)
                return True
    return False


def count_new_requests() -> int:
    """จำนวนคำขอสถานะ new — สำหรับ badge บนเมนู admin"""
    return sum(1 for r in load_requests() if r.get("status") == "new")


# ─────────────────────────── Log tail ───────────────────────────
def _parse_log_line(line: str) -> dict:
    """แยก 'YYYY-MM-DD HH:MM:SS | LEVEL | message' → dict. ทน format แปลก"""
    parts = line.split("|", 2)
    if len(parts) == 3:
        ts, level, msg = parts[0].strip(), parts[1].strip(), parts[2].strip()
        return {"ts": ts, "level": level, "msg": msg, "raw": line}
    return {"ts": "", "level": "", "msg": line.strip(), "raw": line}


def tail_log(level: str | None = None, bank: str | None = None, lines: int = 500) -> list[dict]:
    """อ่าน log จากท้ายไฟล์ แล้ว filter ตาม level/bank"""
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    parsed = [_parse_log_line(l.rstrip("\n")) for l in all_lines if l.strip()]

    if level:
        lv = level.strip().upper()
        parsed = [p for p in parsed if p["level"].upper() == lv]
    if bank:
        tag = f"[{bank.strip().upper()}]"
        parsed = [p for p in parsed if tag in p["msg"].upper()]

    return parsed[-lines:]
