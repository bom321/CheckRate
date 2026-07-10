#!/usr/bin/env python3
"""
rate_monitor.py — Deposit Rate Monitor (orchestrator หลายธนาคาร)

Flow ร่วม (ไม่มี logic เฉพาะธนาคาร — อยู่ใน banks/<code>.py):
  โหลด config → (parallel) ต่อธนาคาร: download PDF → extract → เทียบ CSV → อัปเดต CSV → ส่งอีเมล

การเรียกใช้ (CLI):
  python -m app.monitor.rate_monitor                 รันทุกธนาคารที่ enabled แบบ parallel
  python -m app.monitor.rate_monitor --only SCB,KBANK รันเฉพาะบางธนาคาร (ใช้จากหน้าเว็บ/ดีบั๊ก)
  python -m app.monitor.rate_monitor --backfill       สร้าง CSV ใหม่จาก PDF ที่เก็บไว้
  python -m app.monitor.rate_monitor --discover-year  สแกนหาประกาศทั้งปีแบบละเอียด (เฉพาะ bank ที่รองรับ
                                                       เช่น KBANK) ดาวน์โหลดไฟล์ที่ยังไม่มี แล้ว backfill ให้
                                                       ใช้เวลานาน (~นาที) เหมาะกดด้วยมือเป็นครั้งคราว ไม่ใช่รันทุกวัน
  python -m app.monitor.rate_monitor --test-email     ส่งอีเมลทดสอบเพื่อ verify SMTP

ค่าทั้งหมด (path/SMTP) อ่านจาก environment variable — ดู common.py
"""

import os, sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import common
from .common import (
    log, load_config, get_bank_paths, get_csv_headers,
    download_pdf, get_latest_csv_row, get_prev_rates, append_to_csv,
    check_warnings, write_result, send_email,
    build_new_rates_email, build_error_email, build_test_email,
)
from . import banks

# จำนวน worker สูงสุดตอนรัน parallel
MAX_WORKERS = int(os.environ.get("MONITOR_MAX_WORKERS", "5") or "5")

# ─────────────────────────── Prev PDF Check ───────────────────────────
def ensure_prev_pdf_exists(bank: dict, pdf_dir: str, prev_date: str | None):
    """ถ้าไม่มี PDF ของประกาศก่อนหน้า → download จาก prev_pdf_url แล้วบันทึกไว้"""
    code = bank["code"]
    prev_url = bank.get("prev_pdf_url", "")
    if not prev_url:
        log.info(f"[{code}] ไม่มี prev_pdf_url — ข้ามการตรวจสอบ prev PDF")
        return

    if prev_date:
        expected_fname = f"{code.lower()}_deposit_{prev_date}.pdf"
        if os.path.isfile(os.path.join(pdf_dir, expected_fname)):
            log.info(f"[{code}] Prev PDF พบแล้ว: {expected_fname}")
            return
        log.info(f"[{code}] Prev PDF ไม่พบ ({expected_fname}) — กำลัง download...")
    else:
        log.info(f"[{code}] ไม่ทราบ prev_date — กำลัง download prev PDF เพื่อตรวจสอบ...")

    pdf_bytes = download_pdf(prev_url, bank["referer"], mode=bank.get("fetch_mode", "curl"))
    if pdf_bytes is None:
        log.warning(f"[{code}] ไม่สามารถ download prev PDF ได้ — ข้ามไป")
        return

    eff_date = banks.effective_date(pdf_bytes, bank)
    if eff_date is None:
        log.warning(f"[{code}] ไม่สามารถดึงวันที่จาก prev PDF — ข้ามไป")
        return

    fname = f"{code.lower()}_deposit_{eff_date}.pdf"
    fpath = os.path.join(pdf_dir, fname)
    if os.path.isfile(fpath):
        log.info(f"[{code}] Prev PDF มีอยู่แล้ว: {fname}")
        return

    with open(fpath, "wb") as f:
        f.write(pdf_bytes)
    log.info(f"[{code}] Prev PDF saved: {fname}")

# ─────────────────────────── First-Run Init ───────────────────────────
def initialize_if_needed(bank: dict, pdf_dir: str, csv_path: str):
    os.makedirs(pdf_dir, exist_ok=True)
    if [f for f in os.listdir(pdf_dir) if f.endswith(".pdf")]:
        return

    prev_url = bank.get("prev_pdf_url", "")
    if not prev_url:
        log.warning(f"[{bank['code']}] ไม่มี prev_pdf_url — ข้าม baseline download")
        return

    log.info(f"[{bank['code']}] No local PDFs — downloading previous announcement as baseline...")
    pdf_bytes = download_pdf(prev_url, bank["referer"], mode=bank.get("fetch_mode", "curl"))
    if pdf_bytes is None:
        log.warning(f"[{bank['code']}] Could not download baseline — continuing without")
        return

    eff_date = banks.effective_date(pdf_bytes, bank)
    if eff_date is None:
        log.warning(f"[{bank['code']}] Could not parse date from baseline PDF")
        return

    fname = f"{bank['code'].lower()}_deposit_{eff_date}.pdf"
    with open(os.path.join(pdf_dir, fname), "wb") as f:
        f.write(pdf_bytes)
    log.info(f"[{bank['code']}] Baseline PDF saved: {fname}")

    rates = banks.extract_rates(pdf_bytes, bank)
    if rates is None:
        log.warning(f"[{bank['code']}] Could not extract rates from baseline PDF")
        return

    append_to_csv(csv_path, eff_date, rates, prev_rates=None, rate_targets=bank["rate_targets"])
    summary = "  ".join(f"{t['key']}={rates[t['key']]:.2f}%" for t in bank["rate_targets"])
    log.info(f"[{bank['code']}] Baseline loaded: {eff_date}  {summary}")

# ─────────────────────────── Per-Bank Workflow ───────────────────────────
def run_bank(bank: dict):
    code     = bank["code"]
    targets  = bank["rate_targets"]
    pdf_dir, csv_path = get_bank_paths(code)

    log.info(f"[{code}] ── start ──")
    os.makedirs(pdf_dir, exist_ok=True)

    initialize_if_needed(bank, pdf_dir, csv_path)

    # 1. Download — บาง bank (เช่น KBANK) URL ฝังวันที่ ไม่มี URL ล่าสุดคงที่ ต้อง resolve ก่อน
    latest_url = banks.resolve_latest_url(bank)
    if not latest_url:
        # resolver หา "ประกาศใหม่กว่าที่มี" ไม่เจอ — ถ้ามีประวัติอยู่แล้วถือเป็น "ไม่มีอัปเดต" ปกติ
        # (เช่น KBANK ที่ probe วันที่ ส่วนมากจะไม่พบไฟล์ใหม่ในแต่ละรอบที่รัน ไม่ใช่ error)
        # เป็น error จริงเฉพาะตอนยังไม่มีประวัติเลย (resolve ครั้งแรกแล้วหาอะไรไม่เจอเลย)
        existing_row = get_latest_csv_row(csv_path)
        if existing_row is not None:
            eff = existing_row.get("effective_date")
            log.info(f"[{code}] ไม่พบประกาศใหม่กว่าที่มีอยู่ ({eff})")
            write_result("no_update", bank=code, effective_date=eff)
            return
        err = "ไม่พบ URL ของประกาศล่าสุด"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="resolve_url", message=err)
        send_email(*build_error_email(bank, "resolve_url", err, ts))
        return

    log.info(f"[{code}] Downloading: {latest_url}")
    pdf_bytes = download_pdf(latest_url, bank["referer"], mode=bank.get("fetch_mode", "curl"))
    if pdf_bytes is None:
        err = "PDF download failed"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="download", message=err)
        send_email(*build_error_email(bank, "download", err, ts))
        return

    log.info(f"[{code}] Downloaded {len(pdf_bytes):,} bytes")

    # 2. Date
    eff_date = banks.effective_date(pdf_bytes, bank)
    if eff_date is None:
        err = "ไม่สามารถดึงวันที่มีผลจาก PDF ได้"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="date_extraction", message=err)
        send_email(*build_error_email(bank, "date_extraction", err, ts))
        return

    log.info(f"[{code}] Effective date: {eff_date}")

    # 3. Compare
    latest_row  = get_latest_csv_row(csv_path)
    latest_date = latest_row["effective_date"] if latest_row else None
    if latest_date == eff_date:
        log.info(f"[{code}] No update. Latest: {eff_date}")
        write_result("no_update", bank=code, effective_date=eff_date)
        return

    log.info(f"[{code}] New announcement: {eff_date}  (prev: {latest_date})")

    # 3.5 ตรวจสอบว่ามี PDF ของประกาศก่อนหน้าหรือไม่ ถ้าไม่มีให้ download
    ensure_prev_pdf_exists(bank, pdf_dir, latest_date)

    # 4. Extract
    rates = banks.extract_rates(pdf_bytes, bank)
    if rates is None:
        err = "ไม่สามารถ extract อัตราดอกเบี้ยจาก PDF ได้"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="rate_extraction", message=err, effective_date=eff_date)
        send_email(*build_error_email(bank, "rate_extraction", err, ts))
        return

    # 5. Save PDF
    pdf_fname = f"{code.lower()}_deposit_{eff_date}.pdf"
    try:
        with open(os.path.join(pdf_dir, pdf_fname), "wb") as f:
            f.write(pdf_bytes)
        log.info(f"[{code}] PDF saved: {pdf_fname}")
    except Exception as e:
        err = f"ไม่สามารถบันทึก PDF: {e}"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="save_pdf", message=err)
        send_email(*build_error_email(bank, "save_pdf", err, ts))
        return

    # 6. Update CSV
    prev_rates = get_prev_rates(latest_row, targets)
    try:
        changes = append_to_csv(csv_path, eff_date, rates, prev_rates, targets)
        log.info(f"[{code}] CSV updated: {csv_path}")
    except Exception as e:
        err = f"ไม่สามารถอัปเดต CSV: {e}"
        log.error(f"[{code}] {err}")
        ts = datetime.now().isoformat(timespec="seconds")
        write_result("error", bank=code, step="csv_update", message=err)
        send_email(*build_error_email(bank, "csv_update", err, ts))
        return

    # 7. Warnings
    warnings = check_warnings(rates, prev_rates, targets)

    # 8. Write result + send email
    write_result("new_rates", bank=code, effective_date=eff_date, prev_date=latest_date,
                 rates={t["key"]: rates.get(t["key"]) for t in targets},
                 prev_rates=prev_rates, changes=changes, warnings=warnings,
                 pdf_filename=pdf_fname)

    subject, html = build_new_rates_email(bank, eff_date, latest_date, rates, prev_rates,
                                          warnings, pdf_fname)
    send_email(subject, html)
    log.info(f"[{code}] Done.")

# ─────────────────────────── Backfill ───────────────────────────
def backfill_bank(bank: dict):
    """อ่าน PDF ทั้งหมดใน pdfs/{bank_code}/ แล้วสร้าง CSV ใหม่"""
    import re, csv as _csv
    code    = bank["code"]
    targets = bank["rate_targets"]
    pdf_dir, csv_path = get_bank_paths(code)

    if not os.path.isdir(pdf_dir):
        log.warning(f"[{code}] Backfill: ไม่พบโฟลเดอร์ {pdf_dir}")
        return

    prefix   = f"{code.lower()}_deposit_"
    pdf_files = sorted([f for f in os.listdir(pdf_dir)
                        if f.startswith(prefix) and f.endswith(".pdf")])
    log.info(f"[{code}] Backfill: พบ {len(pdf_files)} ไฟล์")

    headers = get_csv_headers(targets)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        _csv.DictWriter(f, fieldnames=headers).writeheader()

    prev_rates = None
    success = 0
    for fname in pdf_files:
        m = re.match(rf"{re.escape(prefix)}(\d{{4}}-\d{{2}}-\d{{2}})\.pdf", fname)
        if not m:
            continue
        date_iso = m.group(1)
        with open(os.path.join(pdf_dir, fname), "rb") as f:
            pdf_bytes = f.read()
        print(f"\n[{date_iso}] {fname}", end="  ", flush=True)
        rates = banks.extract_rates(pdf_bytes, bank)
        if rates is None:
            print("⚠ ไม่พบอัตรา")
            continue
        append_to_csv(csv_path, date_iso, rates, prev_rates, targets)
        prev_rates = {t["key"]: rates[t["key"]] for t in targets if t["key"] in rates}
        print("✓  " + "  ".join(f"{t['key']}={rates[t['key']]:.2f}%" for t in targets if t["key"] in rates))
        success += 1

    print(f"\n=== [{code}] Backfill: {success}/{len(pdf_files)} ไฟล์ → {csv_path} ===")

# ─────────────────────────── Main (parallel) ───────────────────────────
def _filter_only(banks_list: list[dict], only: set[str] | None) -> list[dict]:
    if not only:
        return banks_list
    only_up = {c.strip().upper() for c in only}
    return [b for b in banks_list if b["code"].upper() in only_up]


def main(only: set[str] | None = None):
    log.info("═" * 60)
    log.info("Run started.")
    banks_list = _filter_only(load_config(enabled_only=True), only)
    log.info(f"Banks to run: {[b['code'] for b in banks_list]}  (parallel, max_workers={MAX_WORKERS})")
    if not banks_list:
        log.info("ไม่มีธนาคารที่ต้องรัน")
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(run_bank, bank): bank["code"] for bank in banks_list}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                fut.result()
            except Exception as e:
                log.error(f"[{code}] Unexpected error: {e}")
    log.info("Run finished.")


def _parse_only_arg(argv: list[str]) -> set[str] | None:
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            return {c for c in argv[i + 1].split(",") if c.strip()}
        if a.startswith("--only="):
            return {c for c in a.split("=", 1)[1].split(",") if c.strip()}
    return None


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--test-email" in argv:
        ok = send_email(*build_test_email())
        sys.exit(0 if ok else 1)
    elif "--backfill" in argv:
        only = _parse_only_arg(argv)
        for bank in _filter_only(load_config(enabled_only=True), only):
            backfill_bank(bank)
    elif "--discover-year" in argv:
        only = _parse_only_arg(argv)
        for bank in _filter_only(load_config(enabled_only=True), only):
            code = bank["code"]
            saved = banks.discover_year(bank)
            if saved is None:
                log.info(f"[{code}] ไม่รองรับการค้นหาประวัติทั้งปีแบบละเอียด")
                continue
            log.info(f"[{code}] พบไฟล์ใหม่ {len(saved)} ไฟล์: {', '.join(saved) or '-'}")
            if saved:
                backfill_bank(bank)  # rebuild CSV ให้รวมไฟล์ใหม่ที่เพิ่งดาวน์โหลดมา
    else:
        main(only=_parse_only_arg(argv))
