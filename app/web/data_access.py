#!/usr/bin/env python3
"""
data_access.py — ชั้นอ่านข้อมูลสำหรับเว็บ Dashboard

อ่าน config / CSV / PDF / log / result / settings จาก DATA_DIR (env เดียวกับ monitor)
ออกแบบให้ **ทนไฟล์หาย** — ธนาคารที่ยังไม่มี CSV/PDF จะไม่ทำให้เว็บพัง
ใช้ helper ร่วมจาก app.monitor.common เพื่อไม่ให้ path/logic ซ้ำซ้อน
"""

import os, csv, json, re
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
