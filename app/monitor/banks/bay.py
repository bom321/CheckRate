#!/usr/bin/env python3
"""
banks/bay.py — ตัวอ่านอัตราดอกเบี้ยของ ธนาคารกรุงศรีอยุธยา (Bay/Krungsri)
parser id: "bay"

PDF ของ Bay เป็น text-layer จริง (ไม่ใช่ภาพสแกนแบบ BBL) แต่มีปัญหาสระ/วรรณยุกต์เพี้ยนแบบเดียวกับ
SCB/KTB (ใช้ _tablekit.thai_skeleton แก้) บวกกับปัญหาเฉพาะตัวอีกอย่าง: บางกลุ่มอักษร (โดยเฉพาะ "ตั้ง"
และ "ขึ้น") ถูกถอดเป็น literal string "(cid:NNN)" แทรกกลางคำ (เช่น "ตั้งแต่" -> "ตงั(cid:202)แต ่") ตัวเลข
ใน "(cid:NNN)" ทำให้ regex/skeleton ที่ข้าม gap ด้วยเลขหลักเดียวกันพังได้ (เจอเลขปลอมแทนวันที่/จำนวนเงิน
จริง) จึงต้องตัด "(cid:\\d+)" ทิ้งจากทุกบรรทัดก่อนประมวลผลเสมอ (ดู _strip_cid) — แก้ที่ต้นทางครั้งเดียว
ทำให้ thai_skeleton/kw_in_line ใช้งานได้ปกติต่อจากนั้น (ยืนยันด้วยการทดสอบกับ PDF จริงแล้ว)

โครงสร้างตาราง: แถว = ผลิตภัณฑ์/ระยะเวลา/วงเงิน, 11 คอลัมน์ประเภทลูกค้า (ต่างจาก SCB ที่มี 13 คอลัมน์)
วงเงินของ Bay เป็นแบบ "ช่วง" เสมอ (น้อยกว่า/ระหว่าง/ตั้งแต่...ขึ้นไป) ปนหน่วย "แสน" กับ "ล้าน" — ต่างจาก
_tablekit.pick_amount_tier ที่รองรับแค่ less_than/at_least หน่วย "ล้านบาท" เท่านั้น จึงเขียน tier parser
ของไฟล์นี้เอง (_classify_tier/_pick_tier) เหมือนที่ kbank.py เขียนเองเพราะเหตุผลเดียวกัน (วงเงินของ KBANK
ก็เป็นช่วง "between" เช่นกัน) — reuse เฉพาะ thai_skeleton/kw_in_line/line_equals_kw/row_values จาก
_tablekit

หัวตารางของ Bay พิมพ์วนหลายบรรทัด (สูงสุด 6 บรรทัดต่อคอลัมน์) หัวข้อของคนละคอลัมน์จึงอยู่บรรทัดเดียวกัน
ปนกัน — อ่านจาก extract_text() เรียงบรรทัดตรง ๆ **จะเข้าใจผิดว่าหัวข้อที่อยู่บรรทัดเดียวกันเป็นคอลัมน์เดียวกัน**
วิธีที่ถูกคือ cluster ตำแหน่ง x ของคำในหัวตารางเทียบกับ marker "(1)".."(11)" (ยืนยันแล้วกับ PDF จริงทั้ง 6 ฉบับ
ก.พ.-มิ.ย. 2569 — โครงสร้างตรงกันทุกฉบับ):

    (1) บุคคลธรรมดา                (2) นิติบุคคลทั่วไป          (3) ราชการ/รัฐวิสาหกิจ/ประกันสังคม
    (4) นิติบุคคลที่ไม่แสวงหากำไร   (5) สถาบันการเงิน/**กองทุน**/บริษัทประกันภัย/บริษัทประกันชีวิต/
                                       กองทุนบำเหน็จบำนาญข้าราชการ
    (6) สหกรณ์                     (7)-(8) บุคคลพิเศษ 1-2      (9)-(11) ผู้ไม่มีถิ่นที่อยู่ในประเทศไทย

**คอลัมน์ 5 คือคอลัมน์ "กองทุน" ของ Bay** — Bay ยุบกองทุนรวมไว้กลุ่มเดียวกับสถาบันการเงิน/บริษัทประกัน
(ต่างจาก SCB/KTB/KBANK/BBL ที่แยกคอลัมน์ให้) แต่เป็นคอลัมน์เดียวที่ครอบคลุม "กองทุน" จริง จึง map
depositor "กองทุน" → คอลัมน์ 5
"""

import io, os, random, re, time
from datetime import datetime

import pdfplumber

from .. import common
from ..common import log, THAI_MONTHS
from ._tablekit import (thai_skeleton, kw_in_line, line_equals_kw, row_values,
                        amount_to_million, find_joined_row, find_joined_section)

PARSER_IDS = ["bay"]

DEFAULT_DEPOSITOR = "บุคคลธรรมดา"
DEFAULT_SECTION_KEYWORD = "บัญชีเงินฝากประจำ"

EXPECTED_COLUMNS = 11

# ─────────────────────────── Depositor column map (11 คอลัมน์ตายตัวของ Bay) ───────────────────────────
DEPOSITOR_COLUMNS: dict[int, list[str]] = {
    1:  ["บุคคลธรรมดา", "personal", "individual"],
    2:  ["นิติบุคคลทั่วไป", "juristic person"],
    3:  ["ราชการ", "รัฐวิสาหกิจ", "ประกันสังคม", "government", "state enterprise"],
    4:  ["นิติบุคคลที่ไม่แสวงหากำไร", "non-profit juristic person"],
    # Bay ยุบ "กองทุน" ไว้กลุ่มเดียวกับสถาบันการเงิน/บริษัทประกัน — คอลัมน์นี้คือคอลัมน์กองทุนของ Bay
    5:  ["สถาบันการเงิน", "กองทุน", "บริษัทประกันภัย", "บริษัทประกันชีวิต", "กองทุนบำเหน็จบำนาญข้าราชการ",
        "financial institution", "fund"],
    6:  ["สหกรณ์", "cooperative"],
    7:  ["บุคคลพิเศษ1", "special person 1"],
    8:  ["บุคคลพิเศษ2", "special person 2"],
    9:  ["ผู้ไม่มีถิ่นที่อยู่บุคคลธรรมดา", "non-resident personal"],
    10: ["ผู้ไม่มีถิ่นที่อยู่นิติบุคคล", "non-resident juristic person"],
    11: ["ผู้ไม่มีถิ่นที่อยู่สถาบันการเงิน", "non-resident financial institution"],
}

_ALIAS_TO_COLUMN: dict[str, int] = {}
for _col, _aliases in DEPOSITOR_COLUMNS.items():
    for _alias in _aliases:
        _ALIAS_TO_COLUMN[thai_skeleton(_alias)] = _col


def resolve_depositor(value) -> int | None:
    """แปลงค่า depositor (คีย์เวิร์ดไทย/อังกฤษ หรือเลข 1-11) → หมายเลขคอลัมน์ หรือ None ถ้าไม่รู้จัก"""
    if isinstance(value, int):
        return value if 1 <= value <= EXPECTED_COLUMNS else None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= EXPECTED_COLUMNS else None
    return _ALIAS_TO_COLUMN.get(thai_skeleton(s))


# ─────────────────────────── cid-noise cleanup ───────────────────────────
_CID_RE = re.compile(r"\(cid:\d+\)")


def _strip_cid(s: str) -> str:
    return _CID_RE.sub("", s)


# ─────────────────────────── Tier (วงเงิน) parsing ───────────────────────────
_AMOUNT_UNIT_RE = re.compile(r"(\d[\d,]*)\s*(แสน|ล.{0,3}?น)บาท")

# _amount_to_million ย้ายไปเป็น _tablekit.amount_to_million (parser อื่นก็ใช้ได้) — คง alias กันแก้ทุกจุด
_amount_to_million = amount_to_million


def _classify_tier(line: str) -> tuple[str, float, float | None, str] | None:
    """จัดประเภทบรรทัด tier (รับบรรทัดที่ตัด cid noise แล้ว) → (kind, lower_m, upper_m|None, line)
    kind: 'less_than' / 'between' / 'at_least' — คืน None ถ้าไม่ใช่บรรทัด tier ที่รู้จัก"""
    amounts = _AMOUNT_UNIT_RE.findall(line)
    if not amounts:
        return None
    if kw_in_line("น้อยกว่า", line):
        upper = _amount_to_million(*amounts[0])
        return ("less_than", 0.0, upper, line)
    if kw_in_line("แต่ไม่ถึง", line) and len(amounts) >= 2:
        lower = _amount_to_million(*amounts[0])
        upper = _amount_to_million(*amounts[1])
        return ("between", lower, upper, line)
    if kw_in_line("ขึ้นไป", line):
        lower = _amount_to_million(*amounts[0])
        return ("at_least", lower, None, line)
    return None


def _pick_tier(tiers: list[tuple[str, float, float | None, str]], target_m: float) -> tuple[str | None, str]:
    """เลือก tier ที่ตรงกับ target_m (ล้านบาท) — เหมือน _tablekit.pick_amount_tier แต่รองรับ 'between' ด้วย
    (วงเงินของ Bay เป็นช่วงเสมอ ไม่ใช่แค่ less_than/at_least แบบ SCB)"""
    less_thans = sorted([(hi, ln) for k, lo, hi, ln in tiers if k == "less_than"], key=lambda x: x[0])
    for hi, ln in less_thans:
        if target_m < hi:
            return (ln, f"น้อยกว่า {hi:g} ล้านบาท")

    betweens = sorted([(lo, hi, ln) for k, lo, hi, ln in tiers if k == "between"], key=lambda x: x[0])
    for lo, hi, ln in betweens:
        if lo <= target_m < hi:
            return (ln, f"{lo:g}-{hi:g} ล้านบาท")

    at_leasts = sorted([(lo, ln) for k, lo, hi, ln in tiers if k == "at_least"], key=lambda x: x[0])
    if at_leasts:
        lo, ln = at_leasts[0]
        return (ln, f"ตั้งแต่ {lo:g} ล้านบาทขึ้นไป (fallback)")

    return (None, "ไม่พบ tier ที่เหมาะสม")


# ─────────────────────────── Row/section scanning ───────────────────────────
# หมวดหมู่หลักของ Bay ขึ้นต้นด้วยพยัญชนะไทย + "." (เช่น "ก. บัญชีเงินฝากกระแสรายวัน", "ง. บัญชีเงินฝากประจำ")
# ผลิตภัณฑ์ย่อยในแต่ละหมวดขึ้นต้นด้วยเลข + "." (เช่น "1. เงินฝากออมทรัพย์") — ต้องแยกสองระดับนี้ให้ถูก
# เพราะ "1. เงินฝากประจำ ประเภทเงินฝากตามจำนวนวัน" (tenor เป็นวัน) มาก่อน "2. เงินฝากประจำ" (tenor เป็น
# เดือน — ที่ระบบต้องการ) ในหมวด "ง." เดียวกัน และแถวที่ต้องการมักเป็นเลขข้อ "1." เอง (เช่น
# "1. เงินฝากออมทรัพย์") ถ้าใช้เลขข้อเป็นขอบเขตหมวดจะตัดจบเร็วเกินไปจนหาแถวไม่เจอ — จึงใช้พยัญชนะไทยนำ
# เป็นขอบเขตหมวด ไม่ใช่เลขข้อ (บรรทัด "ระยะเวลาการฝาก N เดือน" ไม่ชนกับ "N - M วัน" ของหมวดวันอยู่แล้ว
# เพราะคนละหน่วย จึงไม่ต้องแยกขอบเขตระดับเลขข้ออีกชั้น)
_SECTION_BOUNDARY_RE = re.compile(r"^[ก-ฮ]\.\s+\S")
_TIER_MARKER = "ยอดเงินฝาก"


def _section_range(lines: list[str], section_kw: str) -> tuple[int | None, int]:
    """คืน (start, end) ของ section (ขอบเขตหมวดใช้พยัญชนะไทยนำ ไม่ใช่เลขข้อ — ดูคอมเมนต์ด้านบน)"""
    start = None
    for i, s in enumerate(lines):
        if kw_in_line(section_kw, s):
            start = i
            break
    if start is None:
        return None, len(lines)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _SECTION_BOUNDARY_RE.match(lines[i]):
            end = i
            break
    return start, end


def _scan_tiers_and_pick(lines: list[str], tier_start: int, end: int,
                          row_line: str, amount_m: float | None) -> tuple[str | None, str]:
    """รับ row_line (บรรทัดเดียวหรือ join 2 บรรทัด) + ช่วงหา tier ลูก → (line, desc)"""
    if row_values(row_line):
        return row_line, "บรรทัดเดียว (ไม่มี tier วงเงิน)"

    tiers: list[tuple[str, float, float | None, str]] = []
    for i in range(tier_start, end):
        s = lines[i]
        if not kw_in_line(_TIER_MARKER, s):
            break
        info = _classify_tier(s)
        if info and row_values(s):
            tiers.append(info)

    if not tiers:
        return None, ""
    if amount_m is None:
        return tiers[0][3], "ไม่ระบุวงเงิน (ใช้ tier แรกที่พบ)"
    return _pick_tier(tiers, amount_m)


def _find_row_line(lines: list[str], section_kw: str, row_kw: str,
                    amount_m: float | None) -> tuple[str | None, str]:
    """หาแถวข้อมูล (บรรทัดที่มีค่าตัวเลข 11 คอลัมน์) ที่ตรงกับ section + row keyword ที่กำหนด

    two-pass: pass 1 รายบรรทัด (พฤติกรรมเดิม); pass 2 (เมื่อ pass 1 ล้มเหลวทั้งกระบวน) จับหัวข้อ/
    section ที่ pdfplumber ตัดเป็น 2 บรรทัด — เข้า pass 2 เฉพาะตอน pass 1 ไม่ได้ผล กัน false positive"""
    # ── pass 1: รายบรรทัด ──
    start, end = _section_range(lines, section_kw)
    if start is not None:
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
        if row_idx is not None:
            line, desc = _scan_tiers_and_pick(lines, row_idx + 1, end, lines[row_idx], amount_m)
            if line is not None:
                return line, desc

    # ── pass 2: หัวข้อ/section ถูกตัด 2 บรรทัด ──
    if start is not None:
        sec_start, sec_end = start + 1, end
    else:
        js = find_joined_section(lines, section_kw)
        if js is None:
            return None, ""
        sec_start = js + 1
        sec_end = len(lines)
        for i in range(sec_start, len(lines)):
            if _SECTION_BOUNDARY_RE.match(lines[i]):
                sec_end = i
                break

    row_line, tier_start = find_joined_row(lines, sec_start, sec_end, row_kw)
    if row_line is None:
        return None, ""
    return _scan_tiers_and_pick(lines, tier_start, sec_end, row_line, amount_m)


def extract_rates(pdf_bytes: bytes, bank: dict) -> dict | None:
    """อ่านค่าอัตราดอกเบี้ยตาม rate_targets (แต่ละตัวกำหนด section/row/depositor เอง)"""
    rate_targets = bank["rate_targets"]

    lines: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = _strip_cid(page.extract_text() or "")
                for raw in text.splitlines():
                    s = raw.strip()
                    if not s:
                        continue
                    if line_equals_kw("ประเภทลูกค้า", s) or line_equals_kw("ประเภทเงินฝาก", s):
                        continue
                    lines.append(s)
    except Exception as e:
        log.error(f"bay.extract_rates: อ่าน PDF ล้มเหลว: {e}")
        return None

    result: dict = {}
    tiers_used: dict = {}
    failed: list[str] = []
    # target ที่ตั้งค่าผิด/หาไม่เจอ จะถูก "ข้าม" ไม่ทำให้ทั้งธนาคารล้ม (target อื่นยังอ่านต่อได้)

    for target in rate_targets:
        key = target["key"]
        section_kw = target.get("section_keyword") or DEFAULT_SECTION_KEYWORD
        tenor = target.get("tenor_months")
        row_kw = target.get("row_keyword") or (f"ระยะเวลาการฝาก {tenor} เดือน" if tenor else None)
        if not row_kw:
            log.error(f"extract_rates [{key}]: ไม่มี row_keyword และไม่มี tenor_months — ข้าม target นี้")
            failed.append(key); continue

        depositor_value = target.get("depositor", DEFAULT_DEPOSITOR)
        col = resolve_depositor(depositor_value)
        if col is None:
            log.error(f"extract_rates [{key}]: ไม่รู้จัก depositor '{depositor_value}' — ข้าม target นี้")
            failed.append(key); continue

        line, desc = _find_row_line(lines, section_kw, row_kw, target.get("amount_m"))
        if line is None:
            log.error(f"extract_rates [{key}]: ไม่พบแถวที่ตรง section='{section_kw}' row='{row_kw}' — ข้าม target นี้")
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


# ─────────────────────────── Effective date (override) ───────────────────────────
# ใช้ regex/logic เดียวกับ common.get_effective_date เป๊ะ ต่างแค่ตัด "(cid:NNN)" ทิ้งก่อน — ฟอนต์ของ Bay
# ถอด "ตั้งแต่วันที่" เป็น literal "(cid:NNN)" แทรกตัวเลขปลอมเข้าไปกลางข้อความ ทำให้ตัวเดิม (ที่ไม่ได้ตัด
# cid ออก) จับผิดไปเจอเลขจาก "(cid:201)" แทนวันที่จริง (ยืนยันจากการทดสอบจริงกับ PDF: ตัวเดิมคืน None เสมอ)
_DATE_CANDIDATE_RE = re.compile(r"(\d{1,2})\s*(.{2,15}?)\s*(\d{4})")


def get_effective_date(pdf_bytes: bytes) -> str | None:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = _strip_cid(pdf.pages[0].extract_text() or "")
    except Exception as e:
        log.error(f"bay.get_effective_date: {e}")
        return None

    for m in _DATE_CANDIDATE_RE.finditer(text):
        day_s, mid, year_s = m.groups()
        mid_sk = thai_skeleton(mid)
        for month_name, month_num in THAI_MONTHS.items():
            if thai_skeleton(month_name) == mid_sk:
                try:
                    return f"{int(year_s) - 543:04d}-{month_num:02d}-{int(day_s):02d}"
                except ValueError:
                    continue
    return None


# ─────────────────────────── URL discovery ───────────────────────────
# หน้า https://www.krungsri.com/th/rates/deposit ฝังลิงก์ประกาศไว้ในหน้าเดียวกันเป็น
# getmedia/<GUID>/<ชื่อไฟล์> — เว็บนี้อยู่หลัง Incapsula เหมือน KTB/SCB แต่เป็นแบบ **สุ่ม/เป็นครั้งคราว**
# (ทดสอบจริง: ยิงติดกันหลายครั้งบางครั้งผ่านตรง ๆ บางครั้งโดนบล็อก) และปลดบล็อกได้ใน **session เดิม**
# แบบเดียวกับ KTB (ต่างจาก SCB ที่ต้องเปิด session ใหม่) — ยืนยันจากการทดสอบจริงแล้ว หน้า challenge
# สั้นมาก (~950-1000 ไบต์) เทียบกับหน้าจริง (~540KB) จึงใช้ความยาว response เป็นสัญญาณบล็อกแทนการเช็ค
# แค่ "_Incapsula_Resource" in text อย่างเดียว (หน้าจริงก็มี script อ้างอิง "_Incapsula_Resource"
# ปนอยู่ตามปกติเช่นกัน เช็คแค่ substring จะ false-positive)
#
# **Bay ไม่ได้แยกประวัติเป็นปี ๆ เหมือนธนาคารอื่น** — เป็นลิสต์เดียวยาว ๆ แบ่งหน้าด้วย ?page=N ฝั่ง
# เซิร์ฟเวอร์ (10 รายการ/หน้า) สำรวจจริง ก.ค. 2569: รวม 110 ประกาศใน 11 หน้า ครอบคลุมปี 2016-2026,
# ?page=12 คืนหน้าปกติแต่ไม่มีรายการเลย (จบสะอาด) จำนวนรวมอ่านได้จาก JS ในหน้าเอง (_TOTAL_RE/_PAGESIZE_RE)
# และ **ลิสต์เรียงวันที่ใหม่→เก่าเป๊ะทั้ง 110 รายการ** จึงหยุดไล่หน้าได้ทันทีที่เลยปีที่ขอ (stop_before_year)
#
# หน้า archive แยก (/th/rates/past/deposit) มีอีก 54 ประกาศ แต่เป็นปี 2549-2551 (2006-2008) เท่านั้น
# ตั้งชื่อคนละแบบ (Deposit-Rates-Previous-NN-พ.ศ. — ไม่มีวันที่ในชื่อ) และยังไม่ได้ทดสอบว่า parser อ่าน
# layout ยุคนั้นได้ — จงใจไม่รองรับ
DEPOSIT_PAGE_URL = "https://www.krungsri.com/th/rates/deposit"
SITE_BASE = "https://www.krungsri.com"
_BLOCKED_PAGE_MAX_LEN = 5000  # หน้า challenge จริง ~950-1000 ไบต์ หน้าประกาศจริง ~540KB เผื่อ margin กว้าง ๆ

# ชื่อไฟล์ประกาศของ Bay **ไม่ได้มีรูปแบบเดียว** (สำรวจครบทั้ง 110 ไฟล์แล้ว) — จับด้วย regex ตายตัวแบบเดิม
# (deposit-rates-DDMMYYYY-th ตัวเล็ก) ตกไป 21 ไฟล์เงียบ ๆ ตัวอย่างที่เจอจริง:
#     deposit-rates-20062026-th · Deposit-Rates-06122561-th (ตัวใหญ่) · Deposit-Rates-01042561 (ไม่มี -th)
#     deposit-rates02012563-th (ไม่มีขีดหน้าวันที่) · deposit-th08022020 (คนละแบบเลย)
# จึงจับ getmedia link แบบกว้าง ตัด asset รูปทิ้งด้วยนามสกุล แล้วหา DDMMYYYY ในชื่อเอา — กฎนี้ได้
# 110/110 พอดี ไม่มีตัวปลอมหลุด (ตรงกับจำนวนที่หน้าเว็บประกาศเอง)
_ANY_MEDIA_RE = re.compile(r"getmedia/([a-f0-9-]{36})/([^'\"?\s>]*)")
_ASSET_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|ico|gif|svg|aspx)", re.I)
_NAME_DATE_RE = re.compile(r"(\d{2})(\d{2})(\d{4})")
_TOTAL_RE = re.compile(r"_convertDatasource\((\d+)\)")
_PAGESIZE_RE = re.compile(r"pageSize:\s*(\d+)")

_MAX_PAGES_CAP = 30          # กันหลุดถ้าอ่านจำนวนหน้าจาก JS ไม่ได้แล้วต้อง probe เอง
REQUEST_DELAY_SEC = 2.0      # หน่วงระหว่างหน้า — ทดสอบ ~25 ครั้งที่ 2 วิ ไม่โดน Incapsula เลย
REQUEST_JITTER_SEC = 1.0

# เพดานย้อนหลังของ discover_year — หน้าเว็บมีถึงปี 2016 จริง แต่จงใจจำกัดไว้กันโหลดทีเดียว 110 ไฟล์
# อยากได้เก่ากว่านี้ปรับเลขนี้ได้เลย (parser ทดสอบแล้วว่าอ่านไฟล์ปี 2016 ได้ครบทุก target)
MAX_HISTORY_YEARS = 5


def _new_session():
    from curl_cffi import requests as cffi_requests
    return cffi_requests.Session(impersonate="chrome")


def _is_blocked(text: str) -> bool:
    return len(text) < _BLOCKED_PAGE_MAX_LEN and "_Incapsula_Resource" in text


def _url_date_from_name(name: str) -> str | None:
    """แปลงชื่อไฟล์ → วันที่ ISO (ค.ศ.) หรือ None ถ้าไม่มี DDMMYYYY ในชื่อ

    **ปีในชื่อไฟล์เป็น พ.ศ. ก็ได้ ค.ศ. ก็ได้ และปนกันแม้ในยุคเดียวกัน** (คอมเมนต์เดิมที่ว่า "เป็น ค.ศ.
    อยู่แล้ว" ผิด — จริงแค่กับไฟล์ใหม่) จาก 110 ไฟล์เป็น พ.ศ. ถึง 61 ไฟล์ เช่น deposit-rates-04102565-th
    (= 2022-10-04) อยู่หน้าเดียวกับ deposit-rates-22112022-th (= 2022-11-22) — ยืนยันแล้วด้วยการเทียบกับ
    วันที่ในเนื้อหา PDF จริงทุกตัว ถ้าไม่แปลง พ.ศ. ตัวกรองปีจะข้ามไฟล์ พ.ศ. ทั้ง 61 ตัวไปเงียบ ๆ
    (วันที่นี้ใช้กรองคร่าว ๆ ก่อนดาวน์โหลดเท่านั้น — วันที่ที่เชื่อได้จริงต้องอ่านจากเนื้อหา PDF)"""
    m = _NAME_DATE_RE.search(name)
    if not m:
        return None
    dd, mm, yyyy = (int(g) for g in m.groups())
    ce_year = yyyy - 543 if yyyy > 2400 else yyyy
    try:
        datetime(ce_year, mm, dd)  # กันวันที่มั่ว (เลขในชื่อไฟล์ที่ไม่ใช่วันที่จริง)
    except ValueError:
        return None
    return f"{ce_year:04d}-{mm:02d}-{dd:02d}"


def _links_in_html(html: str) -> list[tuple[str, str]]:
    """คืน [(url, url_date), ...] ของลิงก์ประกาศในหน้าเดียว (เรียงตามลำดับที่ปรากฏ = ใหม่→เก่า)"""
    out, seen = [], set()
    for guid, name in _ANY_MEDIA_RE.findall(html):
        if guid in seen or _ASSET_EXT_RE.search(name):
            continue
        url_date = _url_date_from_name(name)
        if url_date is None:
            continue
        seen.add(guid)
        # ประกอบ URL จาก "ชื่อจริงที่เจอ" เสมอ ห้ามประกอบชื่อขึ้นมาเอง — ชื่อไม่ได้รูปแบบเดียว
        out.append((f"{SITE_BASE}/getmedia/{guid}/{name}", url_date))
    return out


def _fetch_page(session, page: int | None) -> str | None:
    """ดึง HTML หน้า deposit rates 1 หน้า (page=None = หน้าแรกแบบไม่ใส่ query) — None ถ้าโดนบล็อก/ล้มเหลว"""
    url = DEPOSIT_PAGE_URL if page is None else f"{DEPOSIT_PAGE_URL}?page={page}"
    try:
        html = session.get(url, timeout=30).text
        if _is_blocked(html):
            log.info(f"bay: โดน Incapsula challenge ที่ {url} — ลองปลดบล็อกใน session เดิม")
            if common.solve_incapsula_challenge(session, html, SITE_BASE):
                html = session.get(url, timeout=30).text
        if _is_blocked(html):
            log.warning(f"bay: ปลดบล็อก Incapsula ไม่สำเร็จที่ {url} — ลองใหม่ภายหลัง")
            return None
        return html
    except Exception as e:
        log.error(f"bay._fetch_page({page}): {e}")
        return None


def _fetch_deposit_page_links(max_pages: int = 1,
                              stop_before_year: int | None = None) -> list[tuple[str, str]]:
    """คืน [(url, url_date), ...] ของลิงก์ประกาศจากหน้า deposit rates

    max_pages=1 (ค่าเริ่มต้น) = อ่านแค่หน้าแรก — **ทางเดินประจำวัน (resolve_latest_url) ต้องยิงแค่
    request เดียวเหมือนเดิม ห้ามให้ช้าลงเป็น 11 เท่า** ส่วน discover_year ส่งค่ามากกว่านั้นมาเอง
    stop_before_year = หยุดไล่หน้าเมื่อทั้งหน้าเก่ากว่าปีนี้แล้ว (ลิสต์เรียงใหม่→เก่า จึงหยุดได้เลย)

    ใช้ session เดียวตลอด (Bay ปลดบล็อกได้ใน session เดิม) · โดนบล็อกกลางคัน = คืนเท่าที่ได้มา ไม่ทิ้งทั้งก้อน
    """
    session = _new_session()
    html = _fetch_page(session, None)
    if html is None:
        return []

    out = _links_in_html(html)
    if max_pages <= 1:
        return out

    # จำนวนหน้าจาก JS ในหน้าเอง: _convertDatasource(110) + pageSize: 10 → 11 หน้า
    total_m, size_m = _TOTAL_RE.search(html), _PAGESIZE_RE.search(html)
    if total_m and size_m and int(size_m.group(1)) > 0:
        total, size = int(total_m.group(1)), int(size_m.group(1))
        total_pages = -(-total // size)  # ceil
        log.info(f"bay: หน้า deposit rates มี {total} ประกาศ / {size} ต่อหน้า → {total_pages} หน้า")
    else:
        # fallback: ไล่ไปเรื่อย ๆ จนกว่าหน้าจะไม่มีรายการใหม่ (?page=เกิน คืนหน้าว่าง จบสะอาด)
        total_pages = _MAX_PAGES_CAP
        log.warning("bay: อ่านจำนวนหน้าจาก JS ไม่ได้ (หน้าเว็บอาจเปลี่ยน format) — ใช้วิธีไล่จนหมดแทน")

    seen = {url for url, _ in out}
    for page in range(2, min(total_pages, max_pages, _MAX_PAGES_CAP) + 1):
        time.sleep(REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC))
        html = _fetch_page(session, page)
        if html is None:
            log.warning(f"bay: หยุดที่หน้า {page} — คืนลิงก์ {len(out)} รายการที่ได้มาแล้ว")
            break
        page_links = [(u, d) for u, d in _links_in_html(html) if u not in seen]
        if not page_links:
            break  # หมดลิสต์แล้ว (หรือหน้าซ้ำ) — จบ
        seen.update(u for u, _ in page_links)
        out += page_links
        # ลิสต์เรียงใหม่→เก่าเป๊ะ (ยืนยันกับทั้ง 110 รายการแล้ว) ทั้งหน้าเก่ากว่าปีที่ขอ = ที่เหลือเก่ากว่าหมด
        if stop_before_year is not None and all(int(d[:4]) < stop_before_year for _, d in page_links):
            log.info(f"bay: หน้า {page} เก่ากว่าปี {stop_before_year} ทั้งหน้าแล้ว — หยุดไล่หน้าต่อ")
            break
    return out


def resolve_latest_url(bank: dict) -> str | None:
    links = _fetch_deposit_page_links()  # หน้าแรกพอ — ลิสต์เรียงใหม่→เก่า ประกาศล่าสุดอยู่หน้าแรกเสมอ
    if not links:
        log.error("bay.resolve_latest_url: หาลิงก์ประกาศจากหน้า deposit rates ไม่เจอ")
        return None
    url, eff_date = max(links, key=lambda x: x[1])
    log.info(f"bay.resolve_latest_url: ล่าสุด {eff_date} → {url}")
    return url


def discover_year(bank: dict, year: int | None = None) -> list[str]:
    """ดาวน์โหลดประกาศทุกฉบับของปีที่ระบุ (ค.ศ.) ที่ยังไม่มีในเครื่อง จากลิงก์บนหน้า deposit rates
    (ไล่ทุกหน้าของ pagination — ดูหมายเหตุด้านบน) กรองซ้ำด้วยวันที่จริงจากเนื้อหา PDF
    เสมอก่อนเซฟไฟล์ (ไม่เชื่อวันที่จากชื่อไฟล์ล้วน ๆ — ตามกับดักที่ระบุใน CLAUDE.md)"""
    code = bank["code"]
    yr = year or datetime.now().year   # ไม่ระบุปี = ปีปัจจุบัน (เหมือนธนาคารอื่น)
    referer = bank.get("referer", "")

    oldest_allowed = datetime.now().year - MAX_HISTORY_YEARS
    if yr < oldest_allowed:
        log.warning(f"bay.discover_year: ปี {yr} เก่ากว่าเพดาน {MAX_HISTORY_YEARS} ปี "
                    f"(ย้อนได้ถึงปี {oldest_allowed}) — ข้าม ปรับ MAX_HISTORY_YEARS ใน bay.py ถ้าต้องการเก่ากว่านี้")
        return []

    pdf_dir, _ = common.get_bank_paths(code)
    os.makedirs(pdf_dir, exist_ok=True)
    existing_dates = set()
    for f in os.listdir(pdf_dir):
        m = re.match(rf"{code.lower()}_deposit_(\d{{4}}-\d{{2}}-\d{{2}})\.pdf$", f)
        if m:
            existing_dates.add(m.group(1))

    links = _fetch_deposit_page_links(max_pages=_MAX_PAGES_CAP, stop_before_year=yr)
    log.info(f"bay.discover_year: พบลิงก์ประกาศทั้งหมด {len(links)} รายการ — กรองเฉพาะปี {yr}")
    saved: list[str] = []
    for url, url_eff_date in links:
        if not common.is_date_in_year(url_eff_date, yr):
            continue  # กรองคร่าว ๆ ด้วยวันที่จากชื่อไฟล์ก่อน ลดจำนวนดาวน์โหลดที่ไม่จำเป็น
        if url_eff_date in existing_dates:
            continue
        raw = common.download_pdf(url, referer, mode="curl")
        if raw is None:
            log.warning(f"bay.discover_year: ดาวน์โหลด {url} ไม่สำเร็จ — ข้าม")
            continue

        eff_date = get_effective_date(raw)  # วันที่จริงจากเนื้อหา — เชื่ออันนี้ ไม่ใช่ url_eff_date
        if eff_date is None:
            log.warning(f"bay.discover_year: {url} ดาวน์โหลดได้แต่หาวันที่ในเนื้อหาไม่เจอ — ข้าม")
            continue
        if not common.is_date_in_year(eff_date, yr):
            log.info(f"bay.discover_year: {url} วันที่มีผล {eff_date} ไม่ใช่ปี {yr} — ข้าม")
            continue
        if eff_date in existing_dates:
            continue

        fname = f"{code.lower()}_deposit_{eff_date}.pdf"
        with open(os.path.join(pdf_dir, fname), "wb") as f:
            f.write(raw)
        saved.append(fname)
        existing_dates.add(eff_date)
        log.info(f"bay.discover_year: พบและบันทึก {fname}")

    log.info(f"bay.discover_year: เสร็จสิ้น — พบไฟล์ใหม่ {len(saved)} ไฟล์: {', '.join(saved) or '-'}")
    return saved
