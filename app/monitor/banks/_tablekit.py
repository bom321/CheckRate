#!/usr/bin/env python3
"""
banks/_tablekit.py — helper กลาง สำหรับ parser ที่อ่านตารางเมทริกซ์ PDF ภาษาไทย
(แถว = ผลิตภัณฑ์/ระยะเวลา/วงเงิน, คอลัมน์ = ประเภทลูกค้า) — ใช้ร่วมกันได้หลายธนาคาร

ปัญหาที่แก้: pdfplumber ถอดข้อความภาษาไทยจาก PDF บางไฟล์แล้วสระ/วรรณยุกต์เพี้ยน/สลับตำแหน่ง
(เช่น "เงินฝากประจำ" กลายเป็น "เงินฝากประจา") แต่ตัวเลข/เครื่องหมาย "-" ยังคงถูกต้องเสมอ
จึงจับคู่ label ด้วย "consonant skeleton" (เก็บเฉพาะพยัญชนะไทย + ascii alnum ตัวพิมพ์เล็ก)
แล้วดึงค่าตัวเลขด้วย regex บนบรรทัดต้นฉบับ (ไม่ผ่านการ normalize)
"""

import re

_THAI_CONSONANT_RE = re.compile(r"[ก-ฮ]|[a-z0-9]")
_VALUE_TOKEN_RE = re.compile(r"^(\d+\.\d+|-)$")


def thai_skeleton(s: str) -> str:
    """ตัดสระ/วรรณยุกต์/เว้นวรรค/สัญลักษณ์ทิ้ง เหลือเฉพาะพยัญชนะไทย + ascii alnum (lowercase)
    ใช้เปรียบเทียบ keyword กับบรรทัดที่ถอดจาก PDF แล้วอาจเพี้ยน"""
    return "".join(_THAI_CONSONANT_RE.findall(s.lower()))


def kw_in_line(keyword: str, line: str) -> bool:
    """True ถ้า skeleton ของ keyword เป็น substring ของ skeleton ของ line"""
    kw = thai_skeleton(keyword)
    if not kw:
        return False
    return kw in thai_skeleton(line)


def line_equals_kw(keyword: str, line: str) -> bool:
    """True ถ้า skeleton ของ keyword เท่ากับ skeleton ของ line ทั้งบรรทัด
    (ใช้กับ header ระยะเวลา เช่น '3 เดือน' vs '12 เดือน' ที่ kw_in_line อาจ match ผิดถ้าใช้ substring)"""
    kw = thai_skeleton(keyword)
    if not kw:
        return False
    return kw == thai_skeleton(line)


def row_values(line: str) -> list[str]:
    """คืน run ท้ายสุดของ token ที่เป็นตัวเลขทศนิยม/"-" ต่อเนื่องกันบนบรรทัด
    (กันปัญหา label ที่มีเลข/ขีดกลางปน เช่น 'เดือนที่ 1 - 12' เพราะ '1'/'12' ไม่ตรง \\d+\\.\\d+)"""
    tokens = line.strip().split()
    run: list[str] = []
    for tok in tokens:
        if _VALUE_TOKEN_RE.match(tok):
            run.append(tok)
        else:
            run = []
    return run


def pick_amount_tier(tiers: list[tuple[str, int, str]], target_m: float) -> tuple[str | None, str]:
    """เลือก tier ที่เหมาะกับ target_m (ล้านบาท) จาก list ของ (tier_type, amount_m, line)
    tier_type: 'less_than' หรือ 'at_least'. คืน (line ที่เลือก, คำอธิบาย)"""
    less_than = sorted([(am, ln) for (t, am, ln) in tiers if t == "less_than"], key=lambda x: x[0])
    at_least = sorted([(am, ln) for (t, am, ln) in tiers if t == "at_least"], key=lambda x: x[0])
    for upper_m, line in less_than:
        if target_m < upper_m:
            return (line, f"น้อยกว่า {upper_m} ล้านบาท")
    if at_least:
        lower_m, line = at_least[0]
        return (line, f"ตั้งแต่ {lower_m} ล้านบาทขึ้นไป (fallback)")
    return (None, "ไม่พบ tier ที่เหมาะสม")


def parse_tier_type_and_amount(line: str) -> tuple[str, int] | None:
    """หา tier type ('less_than'/'at_least') + จำนวนเงิน(ล้านบาท) จากบรรทัด 'วงเงิน...' """
    m = re.search(r"น\D{0,8}ยกว\D{0,8}(\d[\d,]*)\s*ล\D{0,6}นบาท", line)
    if m:
        return ("less_than", int(m.group(1).replace(",", "")))
    m = re.search(r"ตงั\D{0,6}แต\D{0,8}(\d[\d,]*)\s*ล\D{0,6}นบาทขึน.ไป", line)
    if not m:
        m = re.search(r"ตั้งแต่\s+(\d[\d,]*)\s*ล\D{0,4}นบาทขึ้นไป", line)
    if m:
        return ("at_least", int(m.group(1).replace(",", "")))
    return None


def amount_to_million(amount_s: str, unit: str) -> float:
    """แปลงจำนวนเงิน + หน่วย → ล้านบาท: 'แสน' = n×0.1, อื่น ๆ (ล้าน) = n
    (เดิมอยู่ใน bay.py — ย้ายมารวมศูนย์ให้ parser อื่นใช้ tier หน่วยแสนได้ เช่น KBANK MAKE)"""
    n = float(amount_s.replace(",", ""))
    return n * 0.1 if unit == "แสน" else n


# ─────────────────────────── Joined-window matchers (หัวข้อ 2 บรรทัด) ───────────────────────────
# pdfplumber/OCR ตัดหัวข้อผลิตภัณฑ์ที่ยาวขึ้นบรรทัดใหม่ได้ (เช่น "…(K-eSavings)" + "ผ่านบริการ MAKE by KBank")
# วลีที่คร่อมรอยต่อจะไม่ match รายบรรทัด — ใช้ตัวจับคู่เหล่านี้เป็น "pass 2" (เรียกเฉพาะเมื่อ pass 1 ราย
# บรรทัดล้มเหลวทั้งกระบวน) โดยต่อ skeleton ของสองบรรทัดติดกันแล้วเทียบ กัน false positive ด้วยการเข้าถึง
# เฉพาะตอน pass 1 ไม่พบผล
def kw_in_joined(keyword: str, line_a: str, line_b: str) -> bool:
    """True ถ้า skeleton ของ keyword เป็น substring ของ skeleton(line_a)+skeleton(line_b)"""
    kw = thai_skeleton(keyword)
    if not kw:
        return False
    return kw in (thai_skeleton(line_a) + thai_skeleton(line_b))


def joined_equals_kw(keyword: str, line_a: str, line_b: str) -> bool:
    """True ถ้า skeleton ของ keyword เท่ากับ skeleton(line_a)+skeleton(line_b) พอดี
    (คู่กับ line_equals_kw — ใช้กับ parser ที่ต้องการความเท่ากันทั้งบรรทัด เช่น bbl)"""
    kw = thai_skeleton(keyword)
    if not kw:
        return False
    return kw == (thai_skeleton(line_a) + thai_skeleton(line_b))


def find_joined_row(lines: list[str], start: int, end: int, row_kw: str) -> tuple[str | None, int | None]:
    """pass-2: หาแถวหัวข้อที่ pdfplumber ตัดเป็น 2 บรรทัดในช่วง [start, end)
    คืน (บรรทัดที่ต่อกันแล้ว, index บรรทัดถัดจากคู่ที่พบ = จุดเริ่มหา tier ลูก) หรือ (None, None)
    เทียบ joined_equals_kw ก่อน (เข้มกว่า) แล้วค่อย kw_in_joined (substring) เพื่อลด false positive"""
    for i in range(start, end - 1):
        if joined_equals_kw(row_kw, lines[i], lines[i + 1]):
            return lines[i] + " " + lines[i + 1], i + 2
    for i in range(start, end - 1):
        if kw_in_joined(row_kw, lines[i], lines[i + 1]):
            return lines[i] + " " + lines[i + 1], i + 2
    return None, None


def find_joined_section(lines: list[str], section_kw: str, search_start: int = 0) -> int | None:
    """pass-2: หาหัวข้อ section ที่ถูกตัด 2 บรรทัด — คืน index ของบรรทัด "ที่สอง" ของหัวข้อ
    (เนื้อหา section เริ่มบรรทัดถัดไป) หรือ None ผู้เรียกคำนวณ end ด้วย boundary ของตัวเองต่อ"""
    for i in range(search_start, len(lines) - 1):
        if kw_in_joined(section_kw, lines[i], lines[i + 1]):
            return i + 1
    return None
