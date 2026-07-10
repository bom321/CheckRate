#!/usr/bin/env python3
"""
banks/kbank.py — ตัวอ่านอัตราดอกเบี้ยของ ธนาคารกสิกรไทย (KBANK)
parser id: "kbank"

ต่างจาก SCB (banks/scb.py) หลายจุด — ทดสอบยืนยันจริงแล้วก่อนเขียนไฟล์นี้:
  1. **URL ของ PDF ฝังวันที่ประกาศ** (ไม่มี URL "ล่าสุด" คงที่แบบ SCB):
     https://www.kasikornbank.com/th/rate/deposits/{DDMMYYYY}-deposit-rates-th.pdf
     ต้องหา URL จริงด้วย resolve_latest_url() (ไล่ "probe" วันที่ — วันที่ไม่มีประกาศจะได้ HTML
     ขนาดคงที่ ~112KB ไม่ใช่ %PDF)
  2. **เว็บ/PDF มี bot-protection (Akamai) บล็อก curl ธรรมดา (403)** — ต้องดาวน์โหลดด้วย
     curl_cffi (impersonate="chrome") ผ่าน common.download_pdf(..., mode="impersonate")
     (ตั้ง "fetch_mode": "curl-impersonate" ใน banks_config.json)
  3. **ตารางไม่เติม "-" ในช่องว่าง** (ต่างจาก SCB ที่เติมครบทุกคอลัมน์) — แต่ละแถวมีจำนวนค่าไม่เท่ากัน
     (คอลัมน์ที่ไม่มีอัตราจะถูกตัดออกจากแถวไปเลย ไม่ใช่แสดงเป็น "-") จึงระบุคอลัมน์ด้วยลำดับ token
     ไม่ได้แน่นอน (นับผิดถ้าคอลัมน์กลางหาย) → อ่านค่าด้วย **พิกัด x** (extract_words) แทน จับคู่กับ
     ตำแหน่ง x ของ keyword ประเภทลูกค้าในโซน header ของ PDF (ยืนยันแล้วว่าตำแหน่งคงที่ ±4px
     ตลอดทั้งเอกสารและระหว่างฉบับต่างวันที่ประกาศ)
  4. **วงเงินมีทศนิยม + มี tier แบบ "ช่วง"** (เช่น "ตั้งแต่ 10.0 ล้านบาท แต่ไม่ถึง 30.0 ล้านบาท")
     ซึ่ง SCB ไม่มี (SCB มีแค่ "น้อยกว่า"/"ตั้งแต่...ขึ้นไป") — จึงมี tier parser ของตัวเอง
     (ไม่ reuse _tablekit.parse_tier_type_and_amount/pick_amount_tier ที่ออกแบบมาสำหรับ SCB โดยเฉพาะ)

ใช้ร่วมกับ SCB ได้เฉพาะส่วน generic: thai_skeleton/kw_in_line (ทนข้อความไทยที่ pdfplumber ถอดเพี้ยน)
"""

import io, os, re, json
from datetime import datetime, timedelta

import pdfplumber

from .. import common
from ..common import log
from ._tablekit import thai_skeleton, kw_in_line

PARSER_IDS = ["kbank"]

DEFAULT_ROW_TEMPLATE = "เงินฝากประจำ {tenor} เดือน"
DEFAULT_PDF_URL_TEMPLATE = "https://www.kasikornbank.com/th/rate/deposits/{date}-deposit-rates-th.pdf"


# ─────────────────────────── Column resolution (พิกัด x บนหน้า PDF) ───────────────────────────
# แต่ละคอลัมน์ระบุด้วย "anchor keyword" คำเดี่ยวที่ไม่กำกวมในโซน header ของหน้า (ยืนยันจาก PDF จริง
# ว่าตำแหน่ง x คงที่ทั้งเอกสาร) — คอลัมน์ นิติบุคคล(1)/(2) และ นิติบุคคลพิเศษ(1)/(2) ยังไม่รองรับ
# เพราะ anchor คำว่า "นิติบุคคล"/"พิเศษ" ซ้ำกันหลายคอลัมน์ แยกด้วย keyword เดี่ยวไม่ได้ (กำกวม)
_ANCHOR_KEYWORDS: dict[int, str] = {
    1: "ธรรมดา",    # (1) บุคคลธรรมดา
    4: "ราชการ",    # (4) โรงพยาบาล/สถานศึกษา/หน่วยงานราชการ
    5: "การเงิน",   # (5) สถาบันการเงิน
    6: "กองทุน",    # (6) กองทุน
}
_DEPOSITOR_ALIASES: dict[int, list[str]] = {
    1: ["บุคคลธรรมดา", "personal", "individual"],
    4: ["ราชการ", "หน่วยงานราชการ", "government", "government agency"],
    5: ["สถาบันการเงิน", "financial institution"],
    6: ["กองทุน", "fund"],
}
_COLUMN_X_TOLERANCE = 15.0  # px — คอลัมน์ห่างกันจริง ~35-40px, ตำแหน่งเบี้ยวจริง ≤4px ระหว่างหน้า/ฉบับ


def resolve_depositor(value) -> int | None:
    """แปลง depositor (คีย์เวิร์ดไทย/อังกฤษ หรือเลขคอลัมน์) → รหัสคอลัมน์ที่รองรับ (1,4,5,6) หรือ None"""
    if isinstance(value, int):
        return value if value in _ANCHOR_KEYWORDS else None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if n in _ANCHOR_KEYWORDS else None
    sk = thai_skeleton(s)
    for col, aliases in _DEPOSITOR_ALIASES.items():
        if any(thai_skeleton(a) == sk for a in aliases):
            return col
    return None


_ROW_Y_TOLERANCE = 3.0  # px — ข้อความบรรยาย tier กับตัวเลขอัตราบนบรรทัดเดียวกันบางครั้ง top ต่างกัน
                        # เล็กน้อย (~0.2-0.7px) ซึ่งอาจคาบเส้นแบ่งของ round() ทำให้แยกคนละแถวผิดพลาด
                        # ใช้ clustering ตามระยะห่างแทน (เล็กกว่าระยะห่างบรรทัดจริง ~13px มาก จึงไม่รวมข้ามบรรทัด)


def _group_rows(words: list[dict]) -> list[list[dict]]:
    """group คำในหน้าเดียวกันเป็นแถวเดียวกันตามความใกล้ของ 'top' (ไม่ใช้ round() ตรง ๆ — ดู _ROW_Y_TOLERANCE)"""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w["top"])
    rows: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        if w["top"] - rows[-1][-1]["top"] <= _ROW_Y_TOLERANCE:
            rows[-1].append(w)
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _row_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def _find_column_anchor_x(scoped_words: list[dict], col: int) -> float | None:
    """หาตำแหน่ง x ของคอลัมน์ col จาก anchor keyword ใน scoped_words — คืน None ถ้าไม่พบหรือกำกวม
    (พบหลายตำแหน่งที่ไม่ใช่กลุ่มเดียวกัน แปลว่า keyword นี้ไม่ unique พอในโซนที่ค้นหา)
    ผู้เรียกต้องจำกัด scoped_words ให้แคบพอ (เช่น เฉพาะเหนือหัวข้อ tenor บนหน้าเดียวกัน) ไม่ใช่ทั้งเอกสาร
    เพราะคำอย่าง 'กองทุน'/'ราชการ'/'ธรรมดา' ปรากฏซ้ำในเนื้อหาส่วนอื่นของเอกสารด้วย"""
    keyword = _ANCHOR_KEYWORDS.get(col)
    if not keyword:
        return None
    kw_sk = thai_skeleton(keyword)
    xs = sorted(w["x0"] for w in scoped_words if thai_skeleton(w["text"]) == kw_sk)
    if not xs:
        return None
    clusters = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= _COLUMN_X_TOLERANCE:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    if len(clusters) != 1:
        return None
    return sum(clusters[0]) / len(clusters[0])


# ─────────────────────────── Latest URL resolver (date-probe) ───────────────────────────
# หน้า list (deposits.aspx) เป็น SPA + ติด Akamai JS challenge (proof-of-work) — scrape ไม่ได้
# จึงต้อง "ไล่วันที่" ตรง ๆ กับ PDF endpoint แทน (ยืนยันแล้วว่าดึง PDF ตรงด้วย impersonate ได้)
COLD_START_LOOKBACK_DAYS = 200  # ใช้ตอนยังไม่เคย probe เลย (bootstrap ครั้งแรก) — KBANK ประกาศห่างกันได้ถึง ~5 เดือน
MAX_PROBES_PER_RUN = 220        # กันไม่ให้ probe เกินจำเป็นในเคสที่ไม่พบอะไรเลย
RECHECK_DAYS = 4                # ทุกครั้งไล่ probe ซ้ำ N วันล่าสุด (กันประกาศย้อนหลัง/เผื่อ timezone)


def _probe_state_path(code: str) -> str:
    return os.path.join(common.OUTPUT_DIR, f"{code.lower()}_probe_state.json")


def _load_probed_through(code: str):
    """คืนวันที่ (date) ที่เคย probe ถึงล่าสุด — กันการ re-scan ช่วงเดิมทุกครั้ง (ซึ่งช้าขึ้นเรื่อย ๆ)"""
    try:
        with open(_probe_state_path(code)) as f:
            s = json.load(f).get("probed_through")
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except Exception:
        return None


def _save_probed_through(code: str, d) -> None:
    try:
        with open(_probe_state_path(code), "w") as f:
            json.dump({"probed_through": d.isoformat()}, f)
    except Exception as e:
        log.warning(f"kbank: บันทึก probe_state ไม่ได้: {e}")


def resolve_latest_url(bank: dict) -> str | None:
    """PDF ของ KBANK ฝังวันที่ในชื่อไฟล์ (ไม่มี URL ล่าสุดคงที่) → ไล่ probe วันที่หาไฟล์ล่าสุดที่มีจริง
    (วันที่ไม่มีประกาศจะได้หน้า HTML ขนาดคงที่ ไม่ใช่ %PDF)

    เพื่อไม่ให้ช้าขึ้นเรื่อย ๆ (re-scan ช่วงว่างระหว่างประกาศทุกครั้ง) จะจำ 'probed_through' ไว้ในไฟล์ state
    → แต่ละรอบ probe เฉพาะวันใหม่ (บวกซ้ำ RECHECK_DAYS วันล่าสุด). รอบแรกสุด/ครั้งเดียวเท่านั้นที่อาจ
    scan ช่วงยาว (bootstrap)"""
    code = bank["code"]
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.error("kbank.resolve_latest_url: ไม่ได้ติดตั้ง curl_cffi (pip install curl_cffi) "
                  "— ใช้ latest_pdf_url เดิมใน config แทน")
        return bank.get("latest_pdf_url") or None

    base = bank.get("pdf_url_template") or DEFAULT_PDF_URL_TEMPLATE
    referer = bank.get("referer", "")
    today = datetime.now().date()
    end = today + timedelta(days=3)  # เผื่อประกาศล่วงหน้า/timezone

    _, csv_path = common.get_bank_paths(code)
    latest_row = common.get_latest_csv_row(csv_path)
    d_csv = None
    if latest_row:
        try:
            d_csv = datetime.strptime(latest_row["effective_date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            d_csv = None
    probed = _load_probed_through(code)

    if d_csv is None and probed is None:
        # bootstrap: ยังไม่เคยรู้อะไรเลย → ถอยหลังจากวันนี้ หยุดตัวแรกที่เจอ (= ใหม่สุด)
        days = [today - timedelta(days=i) for i in range(COLD_START_LOOKBACK_DAYS)]
        stop_at_first_hit = True
    else:
        # probe เดินหน้าเฉพาะช่วงที่ยังไม่ได้ตรวจ (+ ซ้ำ RECHECK_DAYS วันล่าสุด)
        lower = (d_csv + timedelta(days=1)) if d_csv else (today - timedelta(days=COLD_START_LOOKBACK_DAYS))
        if probed:
            lower = max(lower, probed - timedelta(days=RECHECK_DAYS - 1))
        n_days = max((end - lower).days + 1, 0)
        days = [lower + timedelta(days=i) for i in range(n_days)]
        stop_at_first_hit = False   # เก็บตัวใหม่สุดในช่วง (อาจมีหลายประกาศถ้าเว้นช่วงหาย)

    found_url, found_date = None, None
    probed_reached = probed or (today - timedelta(days=1))
    for i, d in enumerate(days):
        if i >= MAX_PROBES_PER_RUN:
            break
        url = base.format(date=d.strftime("%d%m%Y"))
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=15,
                                  headers={"Referer": referer, "Accept": "application/pdf,*/*"})
        except Exception:
            continue
        if d <= today:
            probed_reached = max(probed_reached, d)
        if r.content[:4] == b"%PDF":
            found_url, found_date = url, d
            if stop_at_first_hit:
                break

    # จำว่า probe ถึงวันไหนแล้ว (ไม่เกินวันนี้) เพื่อรอบหน้าไม่ต้อง scan ซ้ำ
    _save_probed_through(code, min(probed_reached, today))

    if found_url:
        log.info(f"kbank.resolve_latest_url: พบประกาศ {found_date} -> {found_url}")
    else:
        log.info("kbank.resolve_latest_url: ไม่พบประกาศใหม่ในช่วงที่ตรวจสอบ")
    return found_url


# ─────────────────────────── Full-year discovery (manual, ละเอียด) ───────────────────────────
# ต่างจาก resolve_latest_url (เดินหน้า/ถอยหลังแบบเร็ว หยุดเมื่อรู้ "ล่าสุด") — โหมดนี้ไล่ probe
# "ทุกวัน" ในปีที่กำหนดโดยไม่หยุดกลางทาง เพื่อหาไฟล์ที่ resolve_latest_url เคยพลาด (เช่น bootstrap
# ที่หยุดตัวแรกที่เจอ ข้ามประกาศเก่ากว่านั้นในปีเดียวกันไป) ใช้เวลานาน (~เป็นนาที) เหมาะกดด้วยมือเป็นครั้งคราว
def discover_year(bank: dict, year: int | None = None) -> list[str]:
    """สแกนหาไฟล์ประกาศทุกวันในปีที่กำหนด (ค่าเริ่มต้น = ปีปัจจุบัน) ตั้งแต่ 1 ม.ค. ถึงวันนี้/สิ้นปี
    ดาวน์โหลด+บันทึกเฉพาะไฟล์ที่ยังไม่มีในเครื่อง (ไม่ทับไฟล์เดิม) คืนรายชื่อไฟล์ที่บันทึกใหม่"""
    code = bank["code"]
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.error("kbank.discover_year: ไม่ได้ติดตั้ง curl_cffi (pip install curl_cffi)")
        return []

    base = bank.get("pdf_url_template") or DEFAULT_PDF_URL_TEMPLATE
    referer = bank.get("referer", "")
    today = datetime.now().date()
    yr = year or today.year
    start = datetime(yr, 1, 1).date()
    end = today if yr == today.year else datetime(yr, 12, 31).date()

    pdf_dir, _ = common.get_bank_paths(code)
    os.makedirs(pdf_dir, exist_ok=True)
    existing = {f for f in os.listdir(pdf_dir) if f.endswith(".pdf")}

    saved: list[str] = []
    total_days = (end - start).days + 1
    log.info(f"kbank.discover_year: เริ่มสแกนปี {yr} ({total_days} วัน) — ใช้เวลานาน กดครั้งเดียวพอ")
    d = start
    checked = 0
    while d <= end:
        checked += 1
        url = base.format(date=d.strftime("%d%m%Y"))
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=15,
                                  headers={"Referer": referer, "Accept": "application/pdf,*/*"})
        except Exception:
            d += timedelta(days=1)
            continue
        if r.content[:4] == b"%PDF":
            fname = f"{code.lower()}_deposit_{d.isoformat()}.pdf"
            if fname not in existing:
                with open(os.path.join(pdf_dir, fname), "wb") as f:
                    f.write(r.content)
                saved.append(fname)
                existing.add(fname)
                log.info(f"kbank.discover_year: พบและบันทึก {fname} ({checked}/{total_days})")
        d += timedelta(days=1)

    log.info(f"kbank.discover_year: เสร็จสิ้น ({checked} วัน) — พบไฟล์ใหม่ {len(saved)} ไฟล์: "
             f"{', '.join(saved) or '-'}")
    return saved


# ─────────────────────────── Tier parsing (เฉพาะ KBANK — วงเงินทศนิยม + tier แบบช่วง) ───────────────────────────
_NUM_RE = re.compile(r"\d[\d,]*\.\d+")
_TIER_BOUNDARY_RE = re.compile(r"^\d[\d,]*\.0$")  # วงเงินของ KBANK เขียนแบบ "10.0", "500.0" เสมอ (ทศนิยมตัวเดียว)
_VALUE_TOKEN_RE = re.compile(r"^\d[\d,]*\.\d+$")


def _tier_bounds(line: str) -> list[float]:
    """ดึงตัวเลขที่เป็นขอบเขตวงเงิน (รูปแบบ X.0 เสมอ) จากบรรทัด 'วงเงิน …'
    (ค่าอัตราอ่านแยกด้วยพิกัด x ไม่ใช่จากบรรทัดนี้ จึงไม่ต้องแยก 'ค่า' ออกจากตรงนี้)"""
    bounds = []
    for n in _NUM_RE.findall(line):
        clean = n.replace(",", "")
        if _TIER_BOUNDARY_RE.match(clean):
            bounds.append(float(clean))
    return bounds


def _tier_type(line: str, bounds: list[float]) -> tuple[str, float, float | None] | None:
    """คืน (tier_type, low, high) จาก label ของบรรทัด 'วงเงิน …'
    tier_type: 'less_than' (< high) / 'between' (low <= x < high) / 'at_least' (>= low)"""
    if kw_in_line("น้อยกว่า", line):
        return ("less_than", 0.0, bounds[0])
    if kw_in_line("แต่ไม่ถึง", line):
        if len(bounds) < 2:
            return None
        return ("between", bounds[0], bounds[1])
    if kw_in_line("ขึ้นไป", line) or kw_in_line("ตั้งแต่", line):
        return ("at_least", bounds[0], None)
    return None


def _pick_tier(tiers: list[tuple[str, float, float | None, list[dict]]],
               amount_m: float) -> tuple[list[dict] | None, str]:
    for t_type, lo, hi, words in tiers:
        if t_type == "less_than" and amount_m < hi:
            return words, f"น้อยกว่า {hi:g} ล้านบาท"
        if t_type == "between" and lo <= amount_m < hi:
            return words, f"ตั้งแต่ {lo:g} ถึง {hi:g} ล้านบาท"
        if t_type == "at_least" and amount_m >= lo:
            return words, f"ตั้งแต่ {lo:g} ล้านบาทขึ้นไป"
    at_leasts = [(lo, words) for (t, lo, hi, words) in tiers if t == "at_least"]
    if at_leasts:
        lo, words = max(at_leasts, key=lambda x: x[0])
        return words, f"ตั้งแต่ {lo:g} ล้านบาทขึ้นไป (fallback)"
    return None, "ไม่พบ tier ที่เหมาะสม"


def _value_at_column(value_words: list[dict], anchor_x: float) -> str | None:
    """หา token ตัวเลขในกลุ่มคำที่ x0 ใกล้ anchor_x ที่สุด (ภายใน tolerance) — คืน None ถ้าไม่มีค่า
    ที่ตำแหน่งนี้ (คอลัมน์นั้นไม่มีอัตราให้บริการสำหรับ tier/tenor นี้ ซึ่งเกิดขึ้นได้ปกติ)"""
    candidates = [w for w in value_words if _VALUE_TOKEN_RE.match(w["text"])]
    if not candidates:
        return None
    best = min(candidates, key=lambda w: abs(w["x0"] - anchor_x))
    if abs(best["x0"] - anchor_x) > _COLUMN_X_TOLERANCE:
        return None
    return best["text"]


# ─────────────────────────── Section scanning ───────────────────────────
_TOP_LEVEL_RE = re.compile(r"^\d+\.\s+\S")


def _find_tenor_tiers(flat_rows: list[tuple[int, list[dict]]], row_kw: str):
    """หาแถวหัวข้อ '{row_kw}' (เช่น 'เงินฝากประจำ 12 เดือน') แล้วเก็บ tier ที่ตามมา
    จนกว่าจะเจอหัวข้อ 'เงินฝากประจำ' อีกตัว (tenor ถัดไป) หรือหมวดเลขลำดับใหม่ (เช่น '8. เงินฝากพื้นฐาน')

    รองรับทั้งกรณีค่าอยู่แถวเดียวกับ 'วงเงิน …' และกรณี pdfplumber ตัดแถวไปไว้ถัดไป (พบใน 24 เดือน)
    โดยดึงค่าจากแถวถัดไปที่มี token ตัวเลขในโซนคอลัมน์ (x >= 200 กันชนกับตัวเลขขอบเขตวงเงินที่ x<200)

    คืน (heading_page_idx, heading_top, tiers) — heading_page/heading_top ใช้ scope การหา column anchor
    ให้แคบแค่ 'เหนือหัวข้อ tenor นี้ บนหน้าเดียวกัน' (กัน anchor keyword ชนกับที่อื่นในเอกสาร)"""
    row_texts = [_row_text(r) for _, r in flat_rows]

    start_idx = None
    for i, txt in enumerate(row_texts):
        if kw_in_line(row_kw, txt):
            start_idx = i
            break
    if start_idx is None:
        return None, None, None

    heading_page, heading_words = flat_rows[start_idx]
    heading_top = heading_words[0]["top"] if heading_words else 0.0

    section_rows: list[list[dict]] = []
    section_texts: list[str] = []
    for i in range(start_idx + 1, len(flat_rows)):
        txt = row_texts[i]
        if kw_in_line("เงินฝากประจำ", txt) or _TOP_LEVEL_RE.match(txt.strip()):
            break
        _, words = flat_rows[i]
        section_rows.append(words)
        section_texts.append(txt)

    tiers = []
    i = 0
    while i < len(section_rows):
        txt = section_texts[i]
        if not kw_in_line("วงเงิน", txt):
            i += 1
            continue
        bounds = _tier_bounds(txt)
        if not bounds:
            i += 1
            continue
        tt = _tier_type(txt, bounds)
        if tt is None:
            i += 1
            continue

        value_words = section_rows[i]
        has_values_here = any(_VALUE_TOKEN_RE.match(w["text"]) and w["x0"] >= 200 for w in value_words)
        if not has_values_here and i + 1 < len(section_rows):
            nxt_words, nxt_txt = section_rows[i + 1], section_texts[i + 1]
            if not kw_in_line("วงเงิน", nxt_txt):
                value_words = nxt_words

        tiers.append((*tt, value_words))
        i += 1
    return heading_page, heading_top, (tiers or None)


def extract_rates(pdf_bytes: bytes, bank: dict) -> dict | None:
    """อ่านค่าอัตราดอกเบี้ยตาม rate_targets — เวอร์ชันนี้รองรับเงินฝากประจำมาตรฐาน
    (เงินฝากประจำ {N} เดือน) คอลัมน์บุคคลธรรมดา/สถาบันการเงิน/กองทุน/ราชการ; target ที่ตั้งค่าผิด/หาไม่เจอ
    จะถูกข้าม ไม่ล้มทั้งธนาคาร"""
    rate_targets = bank["rate_targets"]

    pages_words: list[list[dict]] = []
    flat_rows: list[tuple[int, list[dict]]] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for pidx, page in enumerate(pdf.pages):
                words = page.extract_words()
                pages_words.append(words)
                for row in _group_rows(words):
                    flat_rows.append((pidx, row))
    except Exception as e:
        log.error(f"kbank.extract_rates: อ่าน PDF ล้มเหลว: {e}")
        return None

    result: dict = {}
    tiers_used: dict = {}
    failed: list[str] = []

    for target in rate_targets:
        key = target["key"]
        tenor = target.get("tenor_months")
        row_kw = target.get("row_keyword") or (DEFAULT_ROW_TEMPLATE.format(tenor=tenor) if tenor else None)
        if not row_kw:
            log.error(f"extract_rates [{key}]: ไม่มี row_keyword และไม่มี tenor_months — ข้าม target นี้")
            failed.append(key); continue

        depositor_value = target.get("depositor", "บุคคลธรรมดา")
        col = resolve_depositor(depositor_value)
        if col is None:
            log.error(f"extract_rates [{key}]: depositor '{depositor_value}' ไม่รู้จัก/ยังไม่รองรับ "
                      f"(รองรับ: บุคคลธรรมดา, สถาบันการเงิน, กองทุน, ราชการ) — ข้าม target นี้")
            failed.append(key); continue

        heading_page, heading_top, tiers = _find_tenor_tiers(flat_rows, row_kw)
        if not tiers:
            log.error(f"extract_rates [{key}]: ไม่พบหัวข้อ/tier ของ '{row_kw}' — ข้าม target นี้")
            failed.append(key); continue

        # scope การหาคอลัมน์ให้แคบแค่ "เหนือหัวข้อ tenor นี้ บนหน้าเดียวกัน" — กัน anchor keyword
        # (เช่น 'กองทุน'/'ราชการ') ชนกับที่ปรากฏซ้ำในเนื้อหาส่วนอื่นของเอกสาร
        scoped_words = [w for w in pages_words[heading_page] if w["top"] < heading_top]
        anchor_x = _find_column_anchor_x(scoped_words, col)
        if anchor_x is None:
            log.error(f"extract_rates [{key}]: หาตำแหน่งคอลัมน์ไม่ได้ (header เพี้ยน/ไม่พบใกล้ '{row_kw}') "
                      f"— ข้าม target นี้")
            failed.append(key); continue

        amount_m = target.get("amount_m")
        if amount_m is None:
            value_words, desc = tiers[0][3], "ไม่ระบุวงเงิน (ใช้ tier แรกที่พบ)"
        else:
            value_words, desc = _pick_tier(tiers, amount_m)

        if value_words is None:
            log.error(f"extract_rates [{key}]: {desc} — ข้าม target นี้")
            failed.append(key); continue

        raw_v = _value_at_column(value_words, anchor_x)
        if raw_v is None:
            log.error(f"extract_rates [{key}]: ไม่มีอัตราสำหรับคอลัมน์นี้ในบรรทัด (อาจไม่มีให้บริการลูกค้าประเภทนี้) "
                      f"— ข้าม target นี้")
            failed.append(key); continue
        try:
            rate = float(raw_v.replace(",", ""))
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
