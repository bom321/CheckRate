#!/usr/bin/env python3
"""
main.py — FastAPI backend สำหรับเว็บ Dashboard ติดตามอัตราดอกเบี้ยเงินฝาก

หน้า (สาธารณะ — ไม่ต้อง login):
  /?month=YYYY-MM  Overview — สรุปรายเดือน: ประกาศกี่ครั้ง · อัตราไหนเปลี่ยน · รายการที่ประกาศซ้ำในเดือนเดียว
  /bank/{code}     รายละเอียด — กราฟแนวโน้ม (Chart.js) + ตารางประวัติ + ลิงก์ PDF

หน้า/API (เฉพาะผู้ดูแลที่ login แล้ว — ดู auth.py, ยืนยันตัวตนด้วย OTP ทางอีเมล):
  /config          จัดการ rate_targets / enabled / ลิงก์ดาวน์โหลด + ผู้รับอีเมล
  /logs            ดู log + ปุ่มรันตรวจสอบ + ทดสอบส่งอีเมล
  /api/config, /api/settings, /api/logs, /api/run, /api/run/status, /api/test-email
"""

import os, sys, re, subprocess, threading, time
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import data_access as da
from . import thaidate
from . import auth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOCK_PATH = os.path.join(da.DATA_DIR, "run.lock")

# path ของ python + module ที่ใช้ trigger monitor เป็น subprocess
PY = sys.executable
MONITOR_MODULE = "app.monitor.rate_monitor"
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))  # .../CheckRate

# เพดานเวลาของงานที่ spawn จากหน้าเว็บ — discover-year ของทั้งปีใช้เวลาหลายสิบนาทีได้
# (SCB/KTB หน่วง 6-8 วิ/request, KBANK probe รายวัน) เดิมตั้งไว้ 600 วิตายตัว จึงถูกฆ่ากลางคันบ่อย
JOB_TIMEOUT_SEC = int(os.environ.get("MONITOR_JOB_TIMEOUT", "3600") or "3600")

app = FastAPI(title="CheckRate — Deposit Rate Dashboard")
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(),
                    max_age=30 * 24 * 3600, same_site="lax")
app.add_exception_handler(auth.LoginRequired, auth.login_required_handler)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[auth.auth_context])
templates.env.filters.update(thaidate.FILTERS)   # thai_date / thai_month / thai_datetime ...
auth.configure(templates)
app.include_router(auth.router)


# ─────────────────────────── Job manager (run monitor) ───────────────────────────
_job_lock = threading.Lock()
_job: dict = {
    "running": False, "kind": None, "only": None,
    "started": None, "finished": None, "returncode": None, "output": "",
}


def _fmt_rate(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return None


def _run_monitor_thread(args: list[str], kind: str, only: str | None):
    """รัน monitor เป็น subprocess, เก็บ output ลง _job"""
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(f"{kind} {datetime.now().isoformat(timespec='seconds')}\n")
    except OSError:
        pass

    env = dict(os.environ)
    env["DATA_DIR"] = da.DATA_DIR
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [PY, "-m", MONITOR_MODULE, *args],
            cwd=PROJECT_ROOT, env=env,
            capture_output=True, text=True, timeout=JOB_TIMEOUT_SEC,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        # แยก code ให้ต่างจาก error อื่น — เดิมเป็น -1 ทั้งคู่ ทำให้แยกไม่ออกว่า "งานยาวเกิน"
        # หรือ "พังจริง" (อาการเดิม: backfill ทุกธนาคารเกิน 600 วิ → ถูกฆ่าก่อนถึง BBL)
        out, rc = (f"หมดเวลา (timeout {JOB_TIMEOUT_SEC}s) — งานถูกยกเลิกกลางคัน "
                   f"ปรับเพิ่มได้ด้วย env MONITOR_JOB_TIMEOUT"), -2
    except Exception as e:
        out, rc = f"error: {e}", -1

    with _job_lock:
        _job.update(running=False, finished=datetime.now().isoformat(timespec="seconds"),
                    returncode=rc, output=out)
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def _start_job(args: list[str], kind: str, only: str | None) -> bool:
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(running=True, kind=kind, only=only,
                    started=datetime.now().isoformat(timespec="seconds"),
                    finished=None, returncode=None, output="")
    t = threading.Thread(target=_run_monitor_thread, args=(args, kind, only), daemon=True)
    t.start()
    return True


# ─────────────────────────── Overview (สรุปรายเดือน) ───────────────────────────
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _logo_url(code: str) -> str | None:
    """โลโก้ธนาคารที่ดึงมาด้วย tools/fetch_logos.py — ไม่มีไฟล์ → None (template ใช้ monogram แทน)"""
    fname = f"{code.lower()}.png"
    if os.path.isfile(os.path.join(STATIC_DIR, "img", "logos", fname)):
        return f"/static/img/logos/{fname}"
    return None


def _month_options(code: str | None = None) -> list[str]:
    """เดือนที่มีประกาศอย่างน้อย 1 ฉบับ — ใหม่ → เก่า (ไม่ระบุ code = รวมทุกธนาคาร)"""
    codes = [code] if code else [b["code"] for b in da.load_banks()]
    months = {(r.get("effective_date") or "")[:7]
              for c in codes for r in da.read_history(c)}
    return sorted((m for m in months if _MONTH_RE.match(m)), reverse=True)


def _month_options_counted(code: str) -> list[dict]:
    """ทุกเดือนแบบต่อเนื่อง (ไม่ข้ามเดือนที่ไม่มีประกาศ) พร้อมจำนวนประกาศ — ใหม่ → เก่า

    ช่วง = เดือนของประกาศแรกสุด → เดือนล่าสุดที่มีประกาศ หรือเดือนปัจจุบัน (แล้วแต่อันไหนใหม่กว่า)
    เดือนที่ไม่มีประกาศได้ count = 0 แต่ยังต้องเลือกได้ เพราะหน้า bank ยังแสดงอัตราที่ยกมาจากเดือนก่อนได้
    """
    counts: dict[str, int] = {}
    for r in da.read_history(code):
        m = (r.get("effective_date") or "")[:7]
        if _MONTH_RE.match(m):
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return []

    now = datetime.now()
    first = min(counts)
    last = max(max(counts), now.strftime("%Y-%m"))
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])

    out = []
    while (y, m) <= (ly, lm):
        key = f"{y:04d}-{m:02d}"
        out.append({"value": key, "count": counts.get(key, 0)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return list(reversed(out))


# ประเภทลูกค้า (depositor) ใน banks_config.json สะกดไม่ตรงกันข้ามธนาคาร
# (KBANK ใช้ "หน่วยงาน ราชการ" ส่วน SCB/KTB/BBL ใช้ "ราชการ") — normalize ตอนแสดงผลเท่านั้น
# ไม่แตะ config เพราะค่านั้นเป็นข้อความอิสระที่ผู้ใช้พิมพ์เองได้จากหน้า /config
def _depositor_pill(value: str | None) -> dict:
    """คืน {slug, label} สำหรับ pill ประเภทลูกค้า — ค่าที่ไม่รู้จักได้ slug 'other' แต่ยังแสดงข้อความเดิม"""
    label = (value or "").strip() or "บุคคลธรรมดา"
    if "ราชการ" in label:
        return {"slug": "gov", "label": "ราชการ"}
    if "กองทุน" in label:
        return {"slug": "fund", "label": "กองทุน"}
    if "บุคคลธรรมดา" in label:
        return {"slug": "person", "label": "บุคคลธรรมดา"}
    if "นิติบุคคล" in label:
        return {"slug": "corp", "label": label}
    return {"slug": "other", "label": label}


def _target_label(t: dict) -> str:
    return t.get("alias") or t.get("label") or t["key"]


def _bank_month_summary(bank: dict, month: str) -> dict:
    """สรุปของธนาคารหนึ่งในเดือนหนึ่ง: ประกาศกี่ครั้ง · อัตราไหนเปลี่ยนบ้าง · เปลี่ยนกี่ครั้ง

    1 แถวใน CSV = ประกาศ 1 ฉบับ (dedupe ด้วย effective_date มาแล้วจาก monitor)
    'ครั้งก่อน' = อัตราจากประกาศฉบับสุดท้าย **ก่อน** เดือนนี้ (baseline) ไม่ใช่แถวก่อนหน้าใน CSV
    จึงคำนวณ net จากค่า rate_* เอง ไม่ใช้คอลัมน์ change_* (นั่นเทียบแถวต่อแถว)
    """
    code = bank["code"]
    rows = da.read_history(code)
    base = {"bank": bank, "logo": _logo_url(code), "has_data": bool(rows)}
    if not rows:
        return base | {"announce_count": 0, "products": [], "products_all": [],
                       "changed_items": 0, "total_times": 0, "net_sum": 0.0, "up": 0, "down": 0,
                       "last_announce": None, "tracked_since": None, "no_prev": False,
                       "unreadable": []}

    before = [r for r in rows if (r.get("effective_date") or "")[:7] < month]
    in_month = [r for r in rows if (r.get("effective_date") or "")[:7] == month]
    baseline = before[-1] if before else None       # ไม่มีประกาศก่อนหน้าเลย → ไม่มีอะไรให้เทียบ
    latest_in_month = in_month[-1] if in_month else None

    products_all = []
    unreadable = []   # target ที่ประกาศล่าสุดของเดือนนี้อ่านค่าไม่ได้ (ช่องว่างใน CSV) — ไม่ใช่ "ไม่มีประกาศ"
    for t in bank.get("rate_targets", []):
        key = t["key"]
        prev = _fmt_rate(baseline.get(key)) if baseline else None
        last, timeline = prev, []
        for r in in_month:
            v = _fmt_rate(r.get(key))
            if v is None:
                continue
            if last is not None and float(v) != float(last):
                timeline.append({"date": r.get("effective_date"), "before": last, "after": v,
                                 "delta": round(float(v) - float(last), 2)})
            last = v
        net = round(float(last) - float(prev), 2) if (last and prev) else None
        # ระวัง: loop ข้างบนข้ามค่าว่างไปเงียบ ๆ (v is None: continue) ทำให้ 'last' ถูกยกมาจากประกาศ
        # ก่อนหน้าโดยผู้ใช้ไม่รู้ตัว — เช็คตรงนี้แยกจากค่า 'last' ว่าแถวล่าสุดจริง ๆ ของเดือนว่างหรือไม่
        if latest_in_month is not None and _fmt_rate(latest_in_month.get(key)) is None:
            unreadable.append(_target_label(t))
        products_all.append({
            "label": _target_label(t),
            "depositor": t.get("depositor") or "บุคคลธรรมดา",   # ไม่ระบุใน config = อัตราของบุคคลธรรมดา
            "dep": _depositor_pill(t.get("depositor")),
            "key": key, "previous": prev, "current": last,
            "net": net, "times": len(timeline), "timeline": timeline,
        })

    changed = [p for p in products_all if p["times"]]   # 2a/5a: สถิติทั้งหมดนับจากรายการที่เปลี่ยนจริง
    last_row = in_month[-1] if in_month else baseline   # เดือนที่ไม่มีประกาศ → อ้างฉบับสุดท้ายก่อนหน้า
    return base | {
        "announce_count": len(in_month),
        # วันที่ของประกาศที่คอลัมน์ "ครั้งก่อน"/"ปัจจุบัน" อ้างถึง — หัวตารางเอาไปแสดงใต้ชื่อคอลัมน์
        # เดือนที่ไม่มีประกาศ: ค่าปัจจุบันคือค่าที่ยกมาจาก baseline ทั้งคู่จึงเป็นวันเดียวกัน
        "prev_date": baseline.get("effective_date") if baseline else None,
        "cur_date": last_row.get("effective_date") if last_row else None,
        "last_announce": last_row.get("effective_date") if last_row else None,
        "tracked_since": rows[0].get("effective_date"),
        "products": changed,          # overview — เฉพาะที่เปลี่ยน
        "products_all": products_all,  # bank detail — ทุกระยะ รวมที่คงเดิม
        "changed_items": len(changed),
        "total_times": sum(p["times"] for p in changed),
        "net_sum": round(sum(p["net"] for p in changed if p["net"] is not None), 2),
        "up": sum(1 for p in changed if p["net"] and p["net"] > 0),
        "down": sum(1 for p in changed if p["net"] and p["net"] < 0),
        "no_prev": bool(in_month and baseline is None),   # เดือนที่มีประกาศแรกสุดในประวัติ
        "unreadable": unreadable,
    }


def _build_overview(month: str, options: list[str]) -> dict:
    """ประกอบ context ของหน้า overview ทั้งหน้า (การ์ดธนาคาร + ตารางกลุ่ม + KPI หัวหน้า)"""
    summaries = [_bank_month_summary(b, month) for b in da.load_banks()]
    active = [s for s in summaries if s["has_data"] and s["bank"].get("enabled")]
    checked = [da.last_checked(s["bank"]["code"]) for s in summaries if s["has_data"]]

    return {
        "month": month,
        "month_options": options,
        "banks": active,               # other (ปิดใช้งาน/ยังไม่มีข้อมูล) ถูกกรองทิ้ง — ไม่แสดงในหน้า overview
        "kpi": {
            "announcements": sum(s["announce_count"] for s in active),
            "changed_items": sum(s["changed_items"] for s in active),
            "up": sum(s["up"] for s in active),
            "down": sum(s["down"] for s in active),
            "banks_announced": sum(1 for s in active if s["announce_count"]),
            "banks_total": len(active),
            "last_checked": max((c for c in checked if c), default=None),
        },
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, month: str | None = None):
    options = _month_options()
    if not (month and _MONTH_RE.match(month)):
        month = options[0] if options else datetime.now().strftime("%Y-%m")
    ctx = _build_overview(month, options)
    return templates.TemplateResponse(request, "overview.html", ctx | {"active": "overview"})


# ─────────────────────────── Bank detail ───────────────────────────
@app.get("/bank/{code}", response_class=HTMLResponse)
def bank_detail(request: Request, code: str, month: str | None = None):
    bank = da.get_bank(code)
    if bank is None:
        raise HTTPException(404, f"ไม่พบธนาคาร {code}")

    history = da.read_history(code)
    targets = bank.get("rate_targets", [])

    pdf_years = da.list_pdfs_by_year(code)

    options = _month_options_counted(bank["code"])
    values = [o["value"] for o in options]
    if not (month and _MONTH_RE.match(month)):
        month = values[0] if values else datetime.now().strftime("%Y-%m")
    summary = _bank_month_summary(bank, month)

    # ข้อมูลกราฟ: labels = วันที่ไทย (โชว์), dates = ISO (ให้ JS คำนวณช่วงเวลา) — ส่งประวัติทั้งหมด
    # ไม่ slice เพราะปุ่มช่วงเวลา "ทั้งหมด" ต้องมีข้อมูลครบ การกรองตามช่วงทำใน detail.js แทน
    dates = [r.get("effective_date", "") for r in history]
    labels = [thaidate.thai_date(d) for d in dates]
    datasets = []
    for t in targets:
        key = t["key"]
        series = []
        for r in history:
            v = _fmt_rate(r.get(key))
            series.append(float(v) if v is not None else None)
        datasets.append({"key": key, "label": _target_label(t),
                         "dep": _depositor_pill(t.get("depositor")), "data": series})

    return templates.TemplateResponse(request, "bank_detail.html", {
        "active": "overview", "bank": bank, "targets": targets,
        "item": summary, "month": month, "month_options": options,
        "chart_labels": labels, "chart_dates": dates, "chart_datasets": datasets, "has_data": bool(history),
        "last_checked": da.last_checked(code),
        "supports_discover_year": da.supports_discover_year(bank),
        "pdf_years": pdf_years,
        "pdf_count": sum(len(g["files"]) for g in pdf_years),
        "year_options": _year_options(code),
    })


# ─────────────────────────── PDF serving ───────────────────────────
@app.get("/pdf/{code}/{filename}")
def serve_pdf(code: str, filename: str):
    # กัน path traversal
    if "/" in filename or "\\" in filename or ".." in filename or not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "ชื่อไฟล์ไม่ถูกต้อง")
    path = da.pdf_abspath(code, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "ไม่พบไฟล์ PDF")
    # content_disposition_type="inline" สำคัญ: ค่าเริ่มต้นของ Starlette คือ "attachment" ซึ่งสั่งให้
    # เบราว์เซอร์ "ดาวน์โหลด" แม้ลิงก์จะเป็น target="_blank" (แท็บใหม่เปิดแล้วปิดทันที)
    # inline = เปิดดูใน PDF viewer ของเบราว์เซอร์ — filename ยังคงไว้เพื่อให้ชื่อไฟล์ถูกต้องตอนกดเซฟ
    return FileResponse(path, media_type="application/pdf", filename=filename,
                        content_disposition_type="inline")


# ─────────────────────────── Manual override (admin กรอกค่าเอง) ───────────────────────────
# สำหรับค่าที่ OCR/parser อ่านไม่ได้จริง ๆ (ปล่อยว่างไว้ในเดือน ๆ นั้น — ดู _bank_month_summary
# คีย์ 'unreadable') หรืออ่านผิด (เช่น '1.25' → 'L25') — เก็บแยกไฟล์จาก parse cache โดยเจตนา
# (ดูรายละเอียดใน common.py) แก้แล้วสั่ง backfill รอบเดียวผ่าน _start_job เดิม (เร็วมาก เพราะ cache hit)
_MANUAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/bank/{code}/manual", response_class=HTMLResponse, dependencies=[Depends(auth.require_admin_page)])
def bank_manual_page(request: Request, code: str, month: str | None = None):
    bank = da.get_bank(code)
    if bank is None:
        raise HTTPException(404, f"ไม่พบธนาคาร {code}")

    targets = bank.get("rate_targets", [])
    history = da.read_history(code)   # เก่า → ใหม่ (จาก data_access)
    manual = da.load_manual(code)

    # เดือนที่แก้ได้จริง = เดือนที่มีแถวใน CSV เท่านั้น (ต่างจาก bank_detail ที่โชว์เดือนว่างได้
    # เพราะยกค่าจากเดือนก่อนมา — หน้านี้ไม่มีอะไรให้แก้ถ้าเดือนนั้นไม่มีประกาศ)
    month_options = [o for o in _month_options_counted(bank["code"]) if o["count"] > 0]
    month_values = [o["value"] for o in month_options]
    if month != "all" and not (month and _MONTH_RE.match(month) and month in month_values):
        month = month_values[0] if month_values else "all"

    rows = []
    for r in reversed(history):       # ใหม่ → เก่า อ่านง่ายกว่าตอนไล่หาประกาศล่าสุด
        date = r.get("effective_date") or ""
        if month != "all" and not date.startswith(month):
            continue
        overrides = manual.get(date) or {}
        cells = {}
        for t in targets:
            key = t["key"]
            raw = (r.get(key) or "").strip()
            cells[key] = {"value": raw, "empty": not raw, "manual": key in overrides}
        rows.append({"date": date, "cells": cells,
                     "pdf_name": f"{code.lower()}_deposit_{date}.pdf"})

    return templates.TemplateResponse(request, "manual.html", {
        "active": "overview", "bank": bank, "targets": targets, "rows": rows,
        "rate_min": da.MANUAL_RATE_MIN, "rate_max": da.MANUAL_RATE_MAX,
        "month": month, "month_options": month_options, "total_rows": len(history),
    })


@app.post("/api/manual/{code}", dependencies=[Depends(auth.require_admin_api)])
async def api_manual_save(code: str, request: Request):
    bank = da.get_bank(code)
    if bank is None:
        raise HTTPException(404, f"ไม่พบธนาคาร {code}")
    try:
        body = await request.json() or {}
    except Exception:
        raise HTTPException(400, "รูปแบบข้อมูลไม่ถูกต้อง")
    if not isinstance(body, dict):
        raise HTTPException(400, "รูปแบบข้อมูลไม่ถูกต้อง")

    valid_keys = {t["key"] for t in bank.get("rate_targets", [])}
    admin_email = request.session.get("admin_email")
    now = datetime.now().isoformat(timespec="seconds")

    data = da.load_manual(code)
    changed = 0
    for date_iso, fields in body.items():
        if not _MANUAL_DATE_RE.match(str(date_iso)) or not isinstance(fields, dict):
            continue
        for key, raw_value in fields.items():
            if key not in valid_keys:
                continue
            if raw_value is None or raw_value == "":
                # ค่าว่าง = ลบ override (กลับไปใช้ผล OCR ดิบตามเดิม) — ไม่ใช่ error
                if date_iso in data and key in data[date_iso]:
                    del data[date_iso][key]
                    if not data[date_iso]:
                        del data[date_iso]
                    changed += 1
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"ค่า '{raw_value}' ของ {date_iso}/{key} ไม่ใช่ตัวเลข")
            if not (da.MANUAL_RATE_MIN <= value <= da.MANUAL_RATE_MAX):
                raise HTTPException(400, f"ค่า {value} ของ {date_iso}/{key} อยู่นอกช่วงที่รับได้ "
                                        f"({da.MANUAL_RATE_MIN:g}-{da.MANUAL_RATE_MAX:g})")
            data.setdefault(date_iso, {})[key] = {"value": value, "by": admin_email, "at": now}
            changed += 1

    da.save_manual(code, data)

    backfill_started = False
    if changed:
        # rebuild CSV ให้ค่าที่เพิ่งกรอกมีผลทันที — ใช้ job queue เดิม (เร็วมากเพราะ parse cache hit,
        # apply_manual ทับค่าตอนอ่านจาก cache อยู่แล้ว ไม่ต้อง OCR ใหม่)
        backfill_started = _start_job(["--backfill", "--only", code], kind="backfill", only=code)

    return {"ok": True, "changed": changed, "backfill_started": backfill_started}


# ─────────────────────────── Config page + API ───────────────────────────
@app.get("/config", response_class=HTMLResponse, dependencies=[Depends(auth.require_admin_page)])
def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {
        "active": "config",
    })


@app.get("/api/config", dependencies=[Depends(auth.require_admin_api)])
def api_get_config():
    banks = da.load_banks()
    # โลโก้แยกออกมาต่างหาก ไม่ยัดใส่ dict ของ bank — ไม่งั้นตอนบันทึกจะถูกเขียนกลับลง banks_config.json
    logos = {b["code"]: _logo_url(b["code"]) for b in banks if b.get("code")}
    return {"banks": banks, "settings": da.load_settings(), "logos": logos}


def _validate_banks(banks) -> str | None:
    if not isinstance(banks, list):
        return "รูปแบบข้อมูลไม่ถูกต้อง (ต้องเป็น list ของธนาคาร)"
    for b in banks:
        code = b.get("code")
        if not code:
            return "มีธนาคารที่ไม่มี code"
        keys = []
        for t in b.get("rate_targets", []):
            k = t.get("key")
            if not k:
                return f"[{code}] มี rate target ที่ไม่มี key"
            if k in keys:
                return f"[{code}] key ซ้ำ: {k}"
            keys.append(k)
            # ต้องมี row_keyword หรือ tenor_months อย่างน้อยหนึ่งอย่าง ไม่งั้นจะหาแถวไม่เจอ
            if not t.get("row_keyword") and not t.get("tenor_months"):
                return (f"[{code}] target '{k}': ต้องระบุ 'Row (ผลิตภัณฑ์/ระยะเวลา)' "
                        f"หรือ 'เดือน' อย่างน้อยหนึ่งอย่าง")
    return None


@app.post("/api/config", dependencies=[Depends(auth.require_admin_api)])
async def api_save_config(request: Request):
    payload = await request.json()
    banks = payload.get("banks")
    err = _validate_banks(banks)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        da.save_banks(banks)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}


# ─────────────────────────── Settings (email recipients) ───────────────────────────
@app.get("/api/settings", dependencies=[Depends(auth.require_admin_api)])
def api_get_settings():
    return {"settings": da.load_settings(), "recipients": da.get_recipients()}


@app.post("/api/settings", dependencies=[Depends(auth.require_admin_api)])
async def api_save_settings(request: Request):
    payload = await request.json()
    settings = da.load_settings()
    if "email_to" in payload:
        settings["email_to"] = payload["email_to"]
    try:
        da.save_settings(settings)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "recipients": da.get_recipients()}


def _year_options(code: str | None = None) -> list[int]:
    """ปีที่เลือกได้สำหรับ backfill/discover-year — ปีปัจจุบันย้อนไปถึงปีที่เก่าที่สุดที่มี PDF (อย่างน้อย 3 ปี)
    ไม่ระบุ code = รวมทุกธนาคาร (หน้า Logs), ระบุ code = เฉพาะธนาคารนั้น (หน้า bank detail)
    floor อย่างน้อย 3 ปีไว้เสมอ เพราะ discover-year ต้องเลือกปีที่ *ยังไม่มี* PDF ได้ด้วย
    discover-year ใช้เลือกปีที่จะไปดาวน์โหลด ส่วน backfill ใช้เลือกปีที่จะบังคับอ่านใหม่"""
    now = datetime.now().year
    codes = [code] if code else [b["code"] for b in da.load_banks()]
    years = {int(g["year"]) for c in codes
             for g in da.list_pdfs_by_year(c) if str(g["year"]).isdigit()}
    oldest = min(years | {now - 2})
    return list(range(now, oldest - 1, -1))


# ─────────────────────────── Logs page + API ───────────────────────────
@app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(auth.require_admin_page)])
def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "active": "logs",
        "banks": [b["code"] for b in da.load_banks()],
        "year_options": _year_options(),
        "current_year": datetime.now().year,
    })


@app.get("/api/logs", dependencies=[Depends(auth.require_admin_api)])
def api_logs(level: str | None = None, bank: str | None = None, lines: int = 500):
    return {"lines": da.tail_log(level=level, bank=bank, lines=min(max(lines, 1), 5000))}


# ─────────────────────────── Run trigger + status ───────────────────────────
@app.post("/api/run", dependencies=[Depends(auth.require_admin_api)])
async def api_run(request: Request):
    only = None
    try:
        body = await request.json()
        only = (body or {}).get("only")
    except Exception:
        only = None

    args = []
    if only:
        args = ["--only", only if isinstance(only, str) else ",".join(only)]

    if not _start_job(args, kind="run", only=only):
        return JSONResponse({"ok": False, "error": "มีงานกำลังรันอยู่แล้ว"}, status_code=409)
    return {"ok": True, "started": True}


async def _read_only_year(request: Request) -> tuple[str | list | None, int | None]:
    """อ่าน {"only": ..., "year": ...} จาก body — ค่าที่ไม่ถูกต้องถือว่าไม่ได้ระบุ (ไม่ใช่ error)"""
    try:
        body = await request.json() or {}
    except Exception:
        return None, None
    only = body.get("only")
    try:
        year = int(body.get("year"))
    except (TypeError, ValueError):
        return only, None
    return only, (year if 2000 <= year <= 2100 else None)


def _job_args(flag: str, only, year: int | None) -> list[str]:
    args = [flag]
    if only:
        args += ["--only", only if isinstance(only, str) else ",".join(only)]
    if year:
        args += ["--year", str(year)]
    return args


@app.post("/api/backfill", dependencies=[Depends(auth.require_admin_api)])
async def api_backfill(request: Request):
    """สร้าง CSV ใหม่จาก PDF ที่เก็บไว้ — ใช้เติมค่าของ rate_target ที่เพิ่งเพิ่มย้อนหลัง

    year (ไม่บังคับ) = บังคับอ่าน PDF ของปีนั้นใหม่ ข้าม parse cache — ปีอื่นยังใช้ cache
    CSV ที่ได้ยังครบทุกปีเสมอ
    """
    only, year = await _read_only_year(request)
    if not _start_job(_job_args("--backfill", only, year), kind="backfill", only=only):
        return JSONResponse({"ok": False, "error": "มีงานกำลังรันอยู่แล้ว"}, status_code=409)
    return {"ok": True, "started": True}


@app.post("/api/discover-year", dependencies=[Depends(auth.require_admin_api)])
async def api_discover_year(request: Request):
    """สแกนหาประกาศทั้งปีแบบละเอียด (เฉพาะธนาคารที่รองรับ เช่น KBANK) — ใช้นานกว่าปกติ กดด้วยมือเป็นครั้งคราว

    year (ไม่บังคับ) = ปีที่จะสแกน (ค่าเริ่มต้น = ปีปัจจุบัน)
    """
    only, year = await _read_only_year(request)
    if not _start_job(_job_args("--discover-year", only, year), kind="discover-year", only=only):
        return JSONResponse({"ok": False, "error": "มีงานกำลังรันอยู่แล้ว"}, status_code=409)
    return {"ok": True, "started": True}


@app.get("/api/run/status", dependencies=[Depends(auth.require_admin_api)])
def api_run_status():
    with _job_lock:
        return dict(_job)


@app.post("/api/test-email", dependencies=[Depends(auth.require_admin_api)])
def api_test_email():
    env = dict(os.environ)
    env["DATA_DIR"] = da.DATA_DIR
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [PY, "-m", MONITOR_MODULE, "--test-email"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=90,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
    except Exception as e:
        out, ok = f"error: {e}", False
    return {"ok": ok, "output": out, "recipients": da.get_recipients()}


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(timespec="seconds")}
