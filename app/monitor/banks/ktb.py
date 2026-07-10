#!/usr/bin/env python3
"""
banks/ktb.py — ตัวอ่านอัตราดอกเบี้ยของ ธนาคารกรุงไทย (KTB)
parser id: "ktb"

โครงสร้างตารางแบบเดียวกับ scb.py (แถว = ผลิตภัณฑ์/ระยะเวลา/วงเงิน, คอลัมน์ = ประเภทลูกค้า 10 คอลัมน์
pad ช่องว่างด้วย "-" เหมือนกัน) แต่ต่างจาก SCB ตรง:
  - ไม่มี URL ล่าสุดคงที่ — resolve_latest_url สแกนหน้า rates แล้วเลือก asset id สูงสุด
  - discover_year ใช้ AJAX รายเดือนของเว็บ (ไม่ใช่เลขลำดับประกาศแบบ SCB, ไม่ใช่ probe รายวันแบบ KBANK)
  - tier วงเงินเป็นแบบ "ตั้งแต่/มากกว่า X ถึง Y ล้านบาท" (ช่วง) ไม่ใช่ "น้อยกว่า"/"ตั้งแต่ ... ขึ้นไป" แบบ SCB

Bot-protection: Incapsula (เหมือน SCB) — ต้องตั้ง fetch_mode: "impersonate" ใน banks_config.json
(ตรวจแล้ว: การดาวน์โหลดตัว PDF เองผ่านได้ด้วย curl ธรรมดา แต่หน้า rates + AJAX ประวัติโดน challenge
จึงใช้ curl_cffi ทุกจุดเพื่อความสม่ำเสมอ)

คอลัมน์ประเภทลูกค้า 10 คอลัมน์ (ยืนยันลำดับจริงด้วย x-coordinate ของ header ในตาราง PDF ไม่ใช่เดา):
  1 บุคคลธรรมดา, 2 นิติบุคคลทั่วไป, 3 นิติบุคคลที่ไม่แสวงหากำไร, 4 ราชการ, 5 รัฐวิสาหกิจ,
  6 สถาบันการเงิน, 7 กองทุนและบริษัทประกัน, 8 ผู้มีถิ่นฐานนอกประเทศ-บุคคลธรรมดา,
  9 ผู้มีถิ่นฐานนอกประเทศ-นิติบุคคล, 10 นิติบุคคลพิเศษ
"""

import io, os, random, re, time
from datetime import datetime
from urllib.parse import quote

import pdfplumber

from .. import common
from ..common import log
from ._tablekit import thai_skeleton, kw_in_line, line_equals_kw, row_values, parse_tier_type_and_amount

PARSER_IDS = ["ktb"]

SITE_BASE = "https://krungthai.com"
RATES_PAGE_URL = f"{SITE_BASE}/th/rates/viewdetail/28"
AJAX_URL = f"{SITE_BASE}/th/rates/getratefeejsonformat"
CATEGORY_ID = "28"

REQUEST_DELAY_SEC = 6.0     # เว็บ KTB มี Incapsula เหมือน SCB — หน่วงเวลาทุก request เสมอ
REQUEST_JITTER_SEC = 2.0

MAX_TABLE_PAGES = 8         # ตารางอัตราดอกเบี้ยเงินฝากอยู่หน้า 1-3 ของ PDF ~46 หน้า — จำกัดหน้าที่อ่าน
                             # กัน keyword ชนข้อความเงื่อนไข/ผลิตภัณฑ์อื่นท้ายเล่ม และเร็วขึ้นมาก

DEFAULT_DEPOSITOR = "บุคคลธรรมดา"
EXPECTED_COLUMNS = 10

# ─────────────────────────── Depositor column map (10 คอลัมน์ตายตัวของ KTB) ───────────────────────────
DEPOSITOR_COLUMNS: dict[int, list[str]] = {
    1:  ["บุคคลธรรมดา", "บุคคล", "personal", "individual"],
    2:  ["นิติบุคคลทั่วไป", "นิติบุคคล", "juristic person"],
    3:  ["นิติบุคคลที่ไม่แสวงหากำไร", "ไม่แสวงหากำไร", "มูลนิธิ", "non-profit"],
    4:  ["ราชการ", "หน่วยงานราชการ", "ส่วนราชการ", "government"],
    5:  ["รัฐวิสาหกิจ", "state enterprise"],
    6:  ["สถาบันการเงิน", "การเงิน", "financial institution"],
    7:  ["กองทุนและบริษัทประกัน", "กองทุน", "บริษัทประกัน", "ประกัน", "fund"],
    8:  ["ผู้มีถิ่นฐานนอกประเทศบุคคลธรรมดา", "นอกประเทศบุคคล", "non-resident personal"],
    9:  ["ผู้มีถิ่นฐานนอกประเทศนิติบุคคล", "นอกประเทศนิติบุคคล", "non-resident juristic person"],
    10: ["นิติบุคคลพิเศษ", "พิเศษ", "special juristic person"],
}

_ALIAS_TO_COLUMN: dict[str, int] = {}
for _col, _aliases in DEPOSITOR_COLUMNS.items():
    for _alias in _aliases:
        _ALIAS_TO_COLUMN[thai_skeleton(_alias)] = _col


def resolve_depositor(value) -> int | None:
    """แปลงค่า depositor (คีย์เวิร์ดไทย/อังกฤษ หรือเลข 1-10) → หมายเลขคอลัมน์ หรือ None ถ้าไม่รู้จัก"""
    if isinstance(value, int):
        return value if 1 <= value <= EXPECTED_COLUMNS else None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= EXPECTED_COLUMNS else None
    return _ALIAS_TO_COLUMN.get(thai_skeleton(s))


# ─────────────────────────── HTTP (curl_cffi — Incapsula bypass) ───────────────────────────
def _new_session():
    from curl_cffi import requests as cffi_requests
    return cffi_requests.Session(impersonate="chrome")


_BLOCK_PAGE_MAX_LEN = 2000  # เพจ/response ที่โดน Incapsula บล็อกจริงมีขนาดเล็กมาก (~200-500 ไบต์)


def _is_blocked(payload) -> bool:
    """ตรวจสัญญาณบล็อกของ Incapsula — ต่างจาก SCB ตรงที่หน้าเว็บ/AJAX ปกติของ KTB ก็ฝัง
    <script src="/_Incapsula_Resource..."> (anti-bot ทั่วไป, ไม่ใช่สัญญาณบล็อก) อยู่ในหน้าจริงเสมออยู่แล้ว
    (ยืนยันแล้ว: หน้า rates ปกติ ~138KB ก็มีคำนี้) จึงต้องเช็คขนาดด้วย — หน้า/response ที่โดนบล็อกจริง
    เป็น challenge page ล้วน ๆ ขนาดเล็กมาก (~200-500 ไบต์) ส่วนของจริง (ทั้งหน้าเว็บ, AJAX fragment แม้เดือน
    ที่ไม่มีประกาศ, และไฟล์ PDF) มีขนาดใหญ่กว่านี้มากเสมอ"""
    size = len(payload)
    if size > _BLOCK_PAGE_MAX_LEN:
        return False
    needle = b"_Incapsula_Resource" if isinstance(payload, bytes) else "_Incapsula_Resource"
    return needle in payload


_HREF_RE = re.compile(r'href="(/Download/rateFee/RateFeeDownload_(\d+)([^"]*?\.pdf))"')


def _extract_pdf_links(html: str) -> list[tuple[int, str]]:
    """หา href ของ PDF อัตราดอกเบี้ยเงินฝาก (กรองตารางเงินตราต่างประเทศ/เอกสารอื่นที่ปนอยู่ในหน้าเดียวกันทิ้ง)
    คืน (asset_id, absolute_url) — href มี Thai ดิบไม่ percent-encode จึงต้อง quote() ก่อนใช้ยิง request"""
    out: list[tuple[int, str]] = []
    for m in _HREF_RE.finditer(html):
        path, asset_id_s, fname = m.group(1), m.group(2), m.group(3)
        if not kw_in_line("ตารางอัตราดอกเบี้ยเงินฝาก", fname):
            continue
        if kw_in_line("ต่างประเทศ", fname):
            continue
        out.append((int(asset_id_s), SITE_BASE + quote(path, safe="/")))
    return out


def resolve_latest_url(bank: dict) -> str | None:
    """สแกนหน้า rates หาลิงก์ PDF อัตราดอกเบี้ยเงินฝาก เลือก asset id สูงสุด (=ล่าสุด)
    คืน URL เดิมทุกรอบที่รัน (ไม่มี URL คงที่แบบ SCB) — rate_monitor dedupe ด้วยวันที่มีผลจากเนื้อ PDF เอง
    (แบบเดียวกับที่ SCB ใช้ URL คงที่ทุกวัน) จึงไม่ต้องมี state/cache ที่นี่"""
    code = bank.get("code", "KTB")
    try:
        session = _new_session()
        r = session.get(RATES_PAGE_URL, timeout=45)
    except Exception as e:
        log.error(f"ktb.resolve_latest_url: โหลดหน้า rates ไม่สำเร็จ: {e}")
        return None
    if r.status_code != 200 or _is_blocked(r.text):
        log.warning(f"[{code}] resolve_latest_url: โหลดหน้า rates ไม่สำเร็จ/โดนบล็อก (HTTP {r.status_code}) "
                    f"— ลองรอบถัดไป")
        return None

    links = _extract_pdf_links(r.text)
    if not links:
        log.error(f"[{code}] resolve_latest_url: ไม่พบลิงก์ PDF อัตราดอกเบี้ยเงินฝากในหน้า rates")
        return None

    asset_id, url = max(links, key=lambda x: x[0])
    log.info(f"[{code}] resolve_latest_url: ล่าสุด asset_id={asset_id}")
    return url


# ─────────────────────────── Rate extraction ───────────────────────────
_TOP_LEVEL_RE = re.compile(r"^\d+\.\s+\S")  # หัวข้อระดับบนสุด เช่น "9. ประจ า" (ไม่ match "9.1 ...")

# tier วงเงินของ KTB เป็นแบบ "ตั้งแต่ X (บาท/ล้านบาท) ถึง Y ล้านบาท" หรือ "มากกว่า X ล้านบาท ถึง Y ล้านบาท"
# (ต่างจาก SCB ที่เป็น "น้อยกว่า X"/"ตั้งแต่ X ขึ้นไป" แบบไม่มีขอบบน) \D ใช้เผื่อ pdfplumber ถอดสระ/วรรณยุกต์เพี้ยน
_RANGE_TIER_RE = re.compile(
    r"(?:ต\D{0,4}งแต\D{0,4}|มากกว\D{0,4})\s*([\d,]+)\s*(บาท|ล\D{0,4}นบาท)?"
    r"\D{0,10}ถ\D{0,4}ง\D{0,10}([\d,]+)\s*ล\D{0,4}นบาท"
)


def _to_million(amount: str, unit: str | None) -> float:
    v = float(amount.replace(",", ""))
    return v / 1_000_000 if (unit and "บาท" in unit and "ล" not in unit) else v


def _parse_ktb_tier(line: str) -> tuple[str, float, float | None] | None:
    """หา tier จากบรรทัด 'วงเงินฝาก...' — รองรับทั้งแบบช่วง (KTB ปกติ) และ less_than/at_least
    (fallback เผื่ออนาคต KTB เปลี่ยนมาใช้รูปประโยคแบบ SCB)"""
    m = _RANGE_TIER_RE.search(line)
    if m:
        low_s, unit, high_s = m.groups()
        return ("range", _to_million(low_s, unit), float(high_s.replace(",", "")))
    info = parse_tier_type_and_amount(line)
    if info:
        return (info[0], float(info[1]), None)
    return None


def _pick_ktb_tier(tiers: list[tuple[str, float, float | None, str]],
                    target_m: float) -> tuple[str | None, str]:
    ranges = [(lo, hi, ln) for (t, lo, hi, ln) in tiers if t == "range"]
    less_than = sorted([(amt, ln) for (t, amt, _h, ln) in tiers if t == "less_than"], key=lambda x: x[0])
    at_least = sorted([(amt, ln) for (t, amt, _h, ln) in tiers if t == "at_least"], key=lambda x: x[0])

    for lo, hi, ln in ranges:
        if lo <= target_m <= hi:
            return (ln, f"{lo:g}–{hi:g} ล้านบาท")
    for upper_m, ln in less_than:
        if target_m < upper_m:
            return (ln, f"น้อยกว่า {upper_m:g} ล้านบาท")
    if at_least:
        lower_m, ln = at_least[0]
        return (ln, f"ตั้งแต่ {lower_m:g} ล้านบาทขึ้นไป (fallback)")
    if ranges:
        lo, hi, ln = max(ranges, key=lambda r: r[1])
        return (ln, f"ไม่อยู่ในช่วงที่กำหนด (fallback: {lo:g}–{hi:g} ล้านบาท)")
    return (None, "ไม่พบ tier ที่เหมาะสม")


def _find_row_line(lines: list[str], section_kw: str | None, row_kw: str,
                    amount_m: float | None) -> tuple[str | None, str]:
    """หาแถวข้อมูลที่ตรงกับ row keyword (section_kw เป็น optional — ข้อ/แถวของ KTB unique ทั้งเอกสาร
    อยู่แล้วในปัจจุบัน แต่เก็บ hook ไว้เผื่ออนาคต format เปลี่ยนแล้ว keyword ชนกันหลายจุด)"""
    if section_kw:
        start = None
        for i, s in enumerate(lines):
            if kw_in_line(section_kw, s):
                start = i
                break
        if start is None:
            return None, ""
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if _TOP_LEVEL_RE.match(lines[i]):
                end = i
                break
        if not row_kw:
            # ไม่ระบุ row_keyword — ใช้ได้เฉพาะกรณี section เป็นบรรทัดเดียวจบในตัว ไม่มี sub-row แยก
            # (KTB เป็น list แบนราบ หลายข้อไม่มีลูกแยก เช่น "2. ออมทรัพย์ 0.250 ..." — บรรทัด section
            # เองมีค่าตรง ๆ อยู่แล้ว) ถ้า section เป็นหัวข้อกว้างจริง (ไม่มีค่าตรง ๆ) ยังต้องระบุ row_keyword อยู่
            if row_values(lines[start]):
                return lines[start], "บรรทัดเดียว (section = row, ไม่มี tier วงเงิน)"
            return None, ""
        # รวมบรรทัด section เอง (start) ในการค้นหา row ด้วย — ไม่ใช่แค่ start+1 เป็นต้นไป เพราะ section
        # กับ row อาจชี้บรรทัดเดียวกัน (เช่น ตั้ง section_keyword="2. ออมทรัพย์" + row_keyword="ออมทรัพย์"
        # แบบเดิม — ถ้าเริ่มค้นที่ start+1 จะข้ามบรรทัดที่มีค่าจริงไปเลย หาไม่เจอทั้งที่ระบุครบ)
        row_search_start = start
    else:
        start, end = -1, len(lines)
        if not row_kw:
            return None, ""
        row_search_start = 0

    row_idx = None
    for i in range(row_search_start, end):
        if line_equals_kw(row_kw, lines[i]):
            row_idx = i
            break
    if row_idx is None:
        for i in range(row_search_start, end):
            if kw_in_line(row_kw, lines[i]):
                row_idx = i
                break
    if row_idx is None:
        return None, ""

    row_line = lines[row_idx]
    if row_values(row_line):
        return row_line, "บรรทัดเดียว (ไม่มี tier วงเงิน)"

    # แถวเป็นหัวข้อไม่มีค่าตรง ๆ — มองหาบรรทัดลูก "- วงเงินฝาก..." ต่อเนื่องกันหลังแถวนี้
    tiers: list[tuple[str, float, float | None, str]] = []
    for i in range(row_idx + 1, end):
        s = lines[i]
        if not kw_in_line("วงเงิน", s):
            break
        info = _parse_ktb_tier(s)
        if info and row_values(s):
            tiers.append((*info, s))

    if not tiers:
        return None, ""
    if amount_m is None:
        return tiers[0][3], "ไม่ระบุวงเงิน (ใช้ tier แรกที่พบ)"
    return _pick_ktb_tier(tiers, amount_m)


def extract_rates(pdf_bytes: bytes, bank: dict) -> dict | None:
    """อ่านค่าอัตราดอกเบี้ยตาม rate_targets (แต่ละตัวกำหนด row/depositor/amount_m เอง
    section_keyword เป็น optional ต่างจาก SCB ที่บังคับ)"""
    rate_targets = bank["rate_targets"]

    lines: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:MAX_TABLE_PAGES]:
                text = page.extract_text() or ""
                for raw in text.splitlines():
                    s = raw.strip()
                    if not s:
                        continue
                    if line_equals_kw("ประเภทลูกค้า", s) or line_equals_kw("ประเภทเงินฝาก", s):
                        continue
                    lines.append(s)
    except Exception as e:
        log.error(f"ktb.extract_rates: อ่าน PDF ล้มเหลว: {e}")
        return None

    result: dict = {}
    tiers_used: dict = {}
    failed: list[str] = []

    for target in rate_targets:
        key = target["key"]
        section_kw = target.get("section_keyword")
        tenor = target.get("tenor_months")
        row_kw = target.get("row_keyword") or (f"ประจำ {tenor} เดือน" if tenor else None)
        if not row_kw and not section_kw:
            log.error(f"extract_rates [{key}]: ไม่มี row_keyword, tenor_months, และ section_keyword "
                      f"— ต้องระบุอย่างน้อยหนึ่งอย่าง ข้าม target นี้")
            failed.append(key); continue

        depositor_value = target.get("depositor", DEFAULT_DEPOSITOR)
        col = resolve_depositor(depositor_value)
        if col is None:
            log.error(f"extract_rates [{key}]: ไม่รู้จัก depositor '{depositor_value}' — ข้าม target นี้")
            failed.append(key); continue

        line, desc = _find_row_line(lines, section_kw, row_kw, target.get("amount_m"))
        if line is None:
            row_note = f" row='{row_kw}'" if row_kw else ""
            section_note = f" section='{section_kw}'" if section_kw else ""
            log.error(f"extract_rates [{key}]: ไม่พบแถวที่ตรง{row_note}{section_note} — ข้าม target นี้")
            failed.append(key); continue

        vals = row_values(line)
        if len(vals) != EXPECTED_COLUMNS:
            log.error(f"extract_rates [{key}]: บรรทัดมี {len(vals)} คอลัมน์ (คาดว่า {EXPECTED_COLUMNS}) "
                      f"— ถอดข้อมูลไม่น่าเชื่อถือ ข้าม target นี้กันอ่านผิดคอลัมน์: {line}")
            failed.append(key); continue

        raw_v = vals[col - 1]
        if raw_v == "-":
            log.error(f"extract_rates [{key}]: ไม่มีอัตราสำหรับคอลัมน์ {col} (แสดง '-') — ข้าม target นี้: {line}")
            failed.append(key); continue
        try:
            rate = float(raw_v)
        except ValueError:
            log.error(f"extract_rates [{key}]: ค่าไม่ใช่ตัวเลข: {raw_v!r} — ข้าม target นี้")
            failed.append(key); continue

        result[key] = rate
        tiers_used[key] = desc
        log.info(f"  {target.get('label', key)}: {rate:.2f}%  ← {desc}")

    if not result:
        log.error("extract_rates: อ่านค่าไม่ได้เลยสักตัว (ทุก target ล้มเหลว)")
        return None
    if failed:
        log.warning(f"extract_rates: ข้าม {len(failed)} target ที่ตั้งค่าผิด/หาไม่เจอ: {', '.join(failed)} "
                    f"(อีก {len(result)} ตัวอ่านได้ปกติ)")

    result["tiers_used"] = tiers_used
    return result


# ─────────────────────────── Full-year discovery (manual, ละเอียด) ───────────────────────────
# หน้า https://krungthai.com/th/rates/viewdetail/28 มี dropdown เดือน/ปี ที่ยิง AJAX POST ไปที่
# getratefeejsonformat (categoryId=28, month, year) คืน HTML fragment ลิงก์ PDF ของเดือนนั้น ๆ
# ต้องมี RequestVerificationToken (ดึงจากหน้า viewdetail/28) + cookie session เดียวกับตอน GET
#
# เว็บ KTB มี Incapsula เหมือน SCB (ยืนยันแล้ว: curl ธรรมดายิง AJAX โดน challenge, curl_cffi
# impersonate="chrome" ผ่าน) จึงหน่วงเวลา+ตรวจจับบล็อกแบบเดียวกับ scb.discover_year
_TOKEN_RE = re.compile(r"RequestVerificationToken['\"]?\s*:\s*'([^']+)'")


def discover_year(bank: dict, year: int | None = None) -> list[str]:
    """สแกนประกาศทั้งปี (ค.ศ.) ทีละเดือนผ่าน AJAX ของเว็บ (~12 request/ปี ไม่ต้อง resume state)
    ดาวน์โหลด+บันทึกเฉพาะไฟล์ที่ยังไม่มีในเครื่อง (เทียบจากวันที่จริงในเนื้อหา PDF) คืนรายชื่อไฟล์ใหม่"""
    code = bank.get("code", "KTB")
    yr = year or datetime.now().year

    pdf_dir, _ = common.get_bank_paths(code)
    os.makedirs(pdf_dir, exist_ok=True)
    existing_dates = set()
    for f in os.listdir(pdf_dir):
        m = re.match(rf"{code.lower()}_deposit_(\d{{4}}-\d{{2}}-\d{{2}})\.pdf$", f)
        if m:
            existing_dates.add(m.group(1))

    session = _new_session()
    try:
        r = session.get(RATES_PAGE_URL, timeout=45)
    except Exception as e:
        log.error(f"[{code}] discover_year: โหลดหน้า rates ไม่สำเร็จ: {e}")
        return []
    if _is_blocked(r.text):
        log.warning(f"[{code}] discover_year: โดนบล็อกตั้งแต่โหลดหน้าแรก — หยุดทันที (ลองใหม่ภายหลัง)")
        return []

    m = _TOKEN_RE.search(r.text)
    if not m:
        log.error(f"[{code}] discover_year: หา RequestVerificationToken ไม่เจอ — หน้าเว็บอาจเปลี่ยน format")
        return []
    token = m.group(1)

    now = datetime.now()
    months = [mo for mo in range(1, 13) if not (yr == now.year and mo > now.month)]
    log.info(f"[{code}] discover_year: สแกนปี {yr} เดือน {months[0]}-{months[-1]} "
             f"หน่วง ~{REQUEST_DELAY_SEC}-{REQUEST_DELAY_SEC + REQUEST_JITTER_SEC:.0f}s/request")

    all_links: dict[int, str] = {}
    blocked = False
    for month in months:
        time.sleep(REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC))
        try:
            resp = session.post(
                AJAX_URL,
                headers={"RequestVerificationToken": token, "Referer": RATES_PAGE_URL,
                         "X-Requested-With": "XMLHttpRequest"},
                data={"categoryId": CATEGORY_ID, "month": str(month), "year": str(yr)},
                timeout=45,
            )
        except Exception as e:
            log.warning(f"[{code}] discover_year: เดือน {month} request ล้มเหลว: {e} — ข้าม")
            continue
        if _is_blocked(resp.text):
            log.warning(f"[{code}] discover_year: โดนบล็อกที่เดือน {month} — หยุดสแกนทันที "
                        f"(ของที่ได้แล้วเก็บไว้)")
            blocked = True
            break
        for asset_id, url in _extract_pdf_links(resp.text):
            all_links.setdefault(asset_id, url)

    saved: list[str] = []
    for asset_id in sorted(all_links):
        if blocked:
            break
        url = all_links[asset_id]
        time.sleep(REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC))
        try:
            dl = session.get(url, timeout=60, headers={"Referer": RATES_PAGE_URL})
        except Exception as e:
            log.warning(f"[{code}] discover_year: โหลด asset {asset_id} ล้มเหลว: {e} — ข้าม")
            continue
        raw = dl.content
        if _is_blocked(raw):
            log.warning(f"[{code}] discover_year: โดนบล็อกตอนโหลดไฟล์ asset {asset_id} — หยุดทันที")
            break
        if not raw or raw[:4] != b"%PDF":
            log.warning(f"[{code}] discover_year: asset {asset_id} ไม่ใช่ PDF — ข้าม")
            continue

        eff_date = common.get_effective_date(raw)
        if eff_date is None:
            log.warning(f"[{code}] discover_year: asset {asset_id} หาวันที่ในเนื้อหาไม่เจอ — ข้าม")
            continue
        if eff_date in existing_dates:
            continue

        fname = f"{code.lower()}_deposit_{eff_date}.pdf"
        with open(os.path.join(pdf_dir, fname), "wb") as f:
            f.write(raw)
        saved.append(fname)
        existing_dates.add(eff_date)
        log.info(f"[{code}] discover_year: พบและบันทึก {fname} (asset_id={asset_id})")

    log.info(f"[{code}] discover_year: เสร็จสิ้น — พบไฟล์ใหม่ {len(saved)} ไฟล์: {', '.join(saved) or '-'}")
    return saved
