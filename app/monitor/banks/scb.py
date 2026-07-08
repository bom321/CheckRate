#!/usr/bin/env python3
"""
banks/scb.py — ตัวอ่านอัตราดอกเบี้ยของ ธนาคารไทยพาณิชย์ (SCB)
parser id: "scb_passbook"

อ่านตารางเมทริกซ์จากประกาศ (แถว = ผลิตภัณฑ์/ระยะเวลา/วงเงิน, คอลัมน์ = ประเภทลูกค้า 13 คอลัมน์)
โดยแต่ละ rate_target กำหนดได้เองว่าจะอ่าน:
  - section_keyword : ข้อความหัวข้อกลุ่มผลิตภัณฑ์ (เช่น "เงินฝากประจำ แบบมีสมุด", "ออมทรัพย์แบบไม่มีสมุดคู่ฝาก")
                       default = DEFAULT_SECTION_KEYWORD (เงินฝากประจำ แบบมีสมุด — พฤติกรรมเดิม)
  - row_keyword     : ข้อความหัวแถว/ผลิตภัณฑ์ (เช่น "12 เดือน") default = "{tenor_months} เดือน"
  - depositor       : ประเภทลูกค้า/คอลัมน์ — คีย์เวิร์ดไทย/อังกฤษ หรือเลขคอลัมน์ 1-13 (ดู DEPOSITOR_COLUMNS)
                       default = "บุคคลธรรมดา" (คอลัมน์ 1 — พฤติกรรมเดิม)
  - amount_m        : ใช้เลือก tier วงเงิน ถ้าแถวที่พบมีหลาย tier

เพิ่มธนาคารใหม่ = สร้างไฟล์ banks/<code>.py แบบเดียวกัน แล้วลงทะเบียนใน banks/__init__.py
(ใช้ helper ที่ generic จาก banks/_tablekit.py ร่วมกันได้)

แต่ละ bank module ต้องมี:
  PARSER_IDS : list[str]              รายชื่อ parser id ที่ไฟล์นี้รองรับ
  extract_rates(pdf_bytes, bank)      -> dict | None
  (ทางเลือก) get_effective_date(pdf_bytes) -> str | None  ถ้า format วันที่ต่างจากค่าเริ่มต้น
"""

import io, re
import pdfplumber

from ..common import log
from ._tablekit import (
    thai_skeleton, kw_in_line, line_equals_kw, row_values,
    pick_amount_tier, parse_tier_type_and_amount,
)

PARSER_IDS = ["scb_passbook"]

DEFAULT_SECTION_KEYWORD = "เงินฝากประจำ แบบมีสมุด"
DEFAULT_DEPOSITOR = "บุคคลธรรมดา"

# ตารางประเภทลูกค้าของ SCB มี 13 คอลัมน์เสมอ — บรรทัดค่าที่ถอดได้ต้องมีครบ 13 token
# ถ้าไม่ครบ แปลว่า pdfplumber ถอด "-" หลุด → index คอลัมน์จะเลื่อน (อ่านผิดคอลัมน์แบบเงียบ ๆ)
# จึงปฏิเสธบรรทัดนั้นแทนที่จะคืนค่าที่อาจผิด
EXPECTED_COLUMNS = 13

# ─────────────────────────── Depositor column map (13 คอลัมน์ตายตัวของ SCB) ───────────────────────────
DEPOSITOR_COLUMNS: dict[int, list[str]] = {
    1:  ["บุคคลธรรมดา", "personal", "individual"],
    2:  ["นิติบุคคลทั่วไป", "juristic person"],
    3:  ["ราชการ", "government agency", "government"],
    4:  ["นิติบุคคลไม่แสวงหากำไรและบริษัทประกันภัย", "non-profit juristic person"],
    5:  ["สถาบันการเงิน", "financial institution"],
    6:  ["สถานศึกษา", "educational institution", "school"],
    7:  ["กองทุน", "fund"],
    8:  ["สหกรณ์", "cooperative"],
    9:  ["ผู้มีถิ่นฐานนอกประเทศบุคคลธรรมดา", "non-resident personal"],
    10: ["ผู้มีถิ่นฐานนอกประเทศนิติบุคคล", "non-resident juristic person"],
    11: ["ผู้มีถิ่นฐานนอกประเทศสถาบันการเงิน", "non-resident financial institution"],
    12: ["นิติบุคคลพิเศษ", "special juristic person"],
    13: ["บุคคลพิเศษ", "special person"],
}

_ALIAS_TO_COLUMN: dict[str, int] = {}
for _col, _aliases in DEPOSITOR_COLUMNS.items():
    for _alias in _aliases:
        _ALIAS_TO_COLUMN[thai_skeleton(_alias)] = _col


def resolve_depositor(value) -> int | None:
    """แปลงค่า depositor (คีย์เวิร์ดไทย/อังกฤษ หรือเลข 1-13) → หมายเลขคอลัมน์ หรือ None ถ้าไม่รู้จัก"""
    if isinstance(value, int):
        return value if 1 <= value <= 13 else None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 13 else None
    return _ALIAS_TO_COLUMN.get(thai_skeleton(s))


# ─────────────────────────── Row/section scanning ───────────────────────────
_TOP_LEVEL_RE = re.compile(r"^\d+\.\s+\S")


def _find_row_line(lines: list[str], section_kw: str, row_kw: str,
                    amount_m: float | None) -> tuple[str | None, str]:
    """หาแถวข้อมูล (บรรทัดที่มีค่าตัวเลข 13 คอลัมน์) ที่ตรงกับ section + row keyword ที่กำหนด"""
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

    row_idx = None
    for i in range(start + 1, end):
        if line_equals_kw(row_kw, lines[i]):
            row_idx = i
            break
    if row_idx is None:
        for i in range(start + 1, end):
            if kw_in_line(row_kw, lines[i]):
                row_idx = i
                break
    if row_idx is None:
        return None, ""

    row_line = lines[row_idx]
    if row_values(row_line):
        return row_line, "บรรทัดเดียว (ไม่มี tier วงเงิน)"

    tiers: list[tuple[str, int, str]] = []
    for i in range(row_idx + 1, end):
        s = lines[i]
        if not kw_in_line("วงเงิน", s):
            break
        info = parse_tier_type_and_amount(s)
        if info and row_values(s):
            tiers.append((info[0], info[1], s))

    if not tiers:
        return None, ""

    if amount_m is None:
        return tiers[0][2], "ไม่ระบุวงเงิน (ใช้ tier แรกที่พบ)"

    return pick_amount_tier(tiers, amount_m)


def extract_rates(pdf_bytes: bytes, bank: dict) -> dict | None:
    """อ่านค่าอัตราดอกเบี้ยตาม rate_targets (แต่ละตัวกำหนด section/row/depositor เอง)"""
    rate_targets = bank["rate_targets"]

    lines: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for raw in text.splitlines():
                    s = raw.strip()
                    if not s:
                        continue
                    if line_equals_kw("ประเภทลูกค้า", s) or line_equals_kw("ประเภทเงินฝาก", s):
                        continue
                    lines.append(s)
    except Exception as e:
        log.error(f"scb.extract_rates: อ่าน PDF ล้มเหลว: {e}")
        return None

    result: dict = {}
    tiers_used: dict = {}

    for target in rate_targets:
        key = target["key"]
        section_kw = target.get("section_keyword") or DEFAULT_SECTION_KEYWORD
        tenor = target.get("tenor_months")
        row_kw = target.get("row_keyword") or (f"{tenor} เดือน" if tenor else None)
        if not row_kw:
            log.error(f"extract_rates [{key}]: ไม่มี row_keyword และไม่มี tenor_months ให้สร้าง default")
            return None

        depositor_value = target.get("depositor", DEFAULT_DEPOSITOR)
        col = resolve_depositor(depositor_value)
        if col is None:
            log.error(f"extract_rates [{key}]: ไม่รู้จัก depositor '{depositor_value}'")
            return None

        line, desc = _find_row_line(lines, section_kw, row_kw, target.get("amount_m"))
        if line is None:
            log.error(f"extract_rates [{key}]: ไม่พบแถวที่ตรง section='{section_kw}' row='{row_kw}'")
            return None

        vals = row_values(line)
        if len(vals) != EXPECTED_COLUMNS:
            log.error(f"extract_rates [{key}]: บรรทัดมี {len(vals)} คอลัมน์ (คาดว่า {EXPECTED_COLUMNS}) "
                      f"— ถอดข้อมูลไม่น่าเชื่อถือ ปฏิเสธเพื่อกันอ่านผิดคอลัมน์: {line}")
            return None

        raw_v = vals[col - 1]
        if raw_v == "-":
            log.error(f"extract_rates [{key}]: ไม่มีอัตราสำหรับคอลัมน์ {col} (แสดง '-') ในบรรทัด: {line}")
            return None
        try:
            rate = float(raw_v)
        except ValueError:
            log.error(f"extract_rates [{key}]: ค่าไม่ใช่ตัวเลข: {raw_v!r}")
            return None

        result[key] = rate
        tiers_used[key] = desc
        log.info(f"  {target.get('label', key)}: {rate:.2f}%  ← {desc}")

    result["tiers_used"] = tiers_used
    return result
