#!/usr/bin/env python3
"""
main.py — FastAPI backend สำหรับเว็บ Dashboard ติดตามอัตราดอกเบี้ยเงินฝาก

หน้า:
  /                Overview — 1 ตารางต่อ 1 ธนาคาร (เทียบอัตราปัจจุบัน vs ก่อนหน้า, ไฮไลต์แถวที่เปลี่ยน)
  /bank/{code}     รายละเอียด — กราฟแนวโน้ม (Chart.js) + ตารางประวัติ + ลิงก์ PDF
  /config          จัดการ rate_targets / enabled / ลิงก์ดาวน์โหลด + ผู้รับอีเมล
  /logs            ดู log + ปุ่มรันตรวจสอบ + ทดสอบส่งอีเมล
API: /api/config, /api/settings, /api/logs, /api/run, /api/run/status, /api/test-email
"""

import os, sys, re, subprocess, threading, time
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import data_access as da

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOCK_PATH = os.path.join(da.DATA_DIR, "run.lock")

# path ของ python + module ที่ใช้ trigger monitor เป็น subprocess
PY = sys.executable
MONITOR_MODULE = "app.monitor.rate_monitor"
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))  # .../CheckRate

app = FastAPI(title="CheckRate — Deposit Rate Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


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
            capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out, rc = "หมดเวลา (timeout 600s)", -1
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


# ─────────────────────────── Overview ───────────────────────────
def _build_overview() -> list[dict]:
    """ประกอบข้อมูลตารางต่อธนาคาร (เฉพาะที่มี CSV จริง)"""
    result = []
    for bank in da.load_banks():
        code = bank["code"]
        if not da.bank_has_csv(code):
            result.append({"bank": bank, "has_data": False})
            continue
        cur, prev = da.latest_two_rows(code)
        rows = []
        for t in bank.get("rate_targets", []):
            key = t["key"]
            cur_v = _fmt_rate(cur.get(key)) if cur else None
            prev_v = _fmt_rate(prev.get(key)) if prev else None
            change = None
            if cur_v is not None and prev_v is not None:
                change = round(float(cur_v) - float(prev_v), 2)
            rows.append({
                "label": t.get("alias") or t.get("label") or key,
                "key": key,
                "current": cur_v, "previous": prev_v, "change": change,
                "changed": bool(change is not None and abs(change) > 0),
            })
        result.append({
            "bank": bank, "has_data": True, "rows": rows,
            "effective_date": cur.get("effective_date") if cur else None,
            "prev_date": prev.get("effective_date") if prev else None,
            "last_checked": da.last_checked(code),
        })
    return result


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    return templates.TemplateResponse(request, "overview.html", {
        "banks": _build_overview(), "active": "overview",
    })


# ─────────────────────────── Bank detail ───────────────────────────
@app.get("/bank/{code}", response_class=HTMLResponse)
def bank_detail(request: Request, code: str):
    bank = da.get_bank(code)
    if bank is None:
        raise HTTPException(404, f"ไม่พบธนาคาร {code}")

    history = da.read_history(code)
    targets = bank.get("rate_targets", [])

    # ข้อมูลกราฟ: labels = วันที่, 1 dataset ต่อ 1 rate key
    labels = [r.get("effective_date", "") for r in history]
    datasets = []
    for t in targets:
        key = t["key"]
        series = []
        for r in history:
            v = _fmt_rate(r.get(key))
            series.append(float(v) if v is not None else None)
        datasets.append({"key": key, "label": t.get("alias") or t.get("label") or key, "data": series})

    # ตารางประวัติแบบสลับแกน: วันที่ = คอลัมน์ (ใหม่สุดก่อน), ประเภทอัตรา = แถว
    # แสดงย้อนหลังไม่เกิน 5 ครั้งล่าสุดก็พอ
    recent = list(reversed(history))[:5]     # history เรียงเก่า→ใหม่, reversed = ใหม่→เก่า
    date_cols = [{"effective_date": r.get("effective_date", ""),
                  "pdf": da.pdf_for_date(code, r.get("effective_date", ""))}
                 for r in recent]
    target_rows = []
    for t in targets:
        key = t["key"]
        cells = [{"rate": _fmt_rate(r.get(key)),
                  "change": r.get(da.common.change_col(key), "")} for r in recent]
        target_rows.append({"label": t.get("alias") or t.get("label") or key, "cells": cells})

    return templates.TemplateResponse(request, "bank_detail.html", {
        "active": "overview", "bank": bank, "targets": targets,
        "chart_labels": labels, "chart_datasets": datasets,
        "date_cols": date_cols, "target_rows": target_rows, "has_data": bool(history),
        "last_checked": da.last_checked(code),
        "supports_discover_year": da.supports_discover_year(bank),
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
    return FileResponse(path, media_type="application/pdf", filename=filename)


# ─────────────────────────── Config page + API ───────────────────────────
@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {
        "active": "config",
    })


@app.get("/api/config")
def api_get_config():
    return {"banks": da.load_banks(), "settings": da.load_settings()}


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


@app.post("/api/config")
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
@app.get("/api/settings")
def api_get_settings():
    return {"settings": da.load_settings(), "recipients": da.get_recipients()}


@app.post("/api/settings")
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


# ─────────────────────────── Logs page + API ───────────────────────────
@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "active": "logs",
        "banks": [b["code"] for b in da.load_banks()],
    })


@app.get("/api/logs")
def api_logs(level: str | None = None, bank: str | None = None, lines: int = 500):
    return {"lines": da.tail_log(level=level, bank=bank, lines=min(max(lines, 1), 5000))}


# ─────────────────────────── Run trigger + status ───────────────────────────
@app.post("/api/run")
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


@app.post("/api/backfill")
async def api_backfill(request: Request):
    """สร้าง CSV ใหม่จาก PDF ที่เก็บไว้ — ใช้เติมค่าของ rate_target ที่เพิ่งเพิ่มย้อนหลัง"""
    only = None
    try:
        body = await request.json()
        only = (body or {}).get("only")
    except Exception:
        only = None

    args = ["--backfill"]
    if only:
        args += ["--only", only if isinstance(only, str) else ",".join(only)]

    if not _start_job(args, kind="backfill", only=only):
        return JSONResponse({"ok": False, "error": "มีงานกำลังรันอยู่แล้ว"}, status_code=409)
    return {"ok": True, "started": True}


@app.post("/api/discover-year")
async def api_discover_year(request: Request):
    """สแกนหาประกาศทั้งปีแบบละเอียด (เฉพาะธนาคารที่รองรับ เช่น KBANK) — ใช้นานกว่าปกติ กดด้วยมือเป็นครั้งคราว"""
    only = None
    try:
        body = await request.json()
        only = (body or {}).get("only")
    except Exception:
        only = None

    args = ["--discover-year"]
    if only:
        args += ["--only", only if isinstance(only, str) else ",".join(only)]

    if not _start_job(args, kind="discover-year", only=only):
        return JSONResponse({"ok": False, "error": "มีงานกำลังรันอยู่แล้ว"}, status_code=409)
    return {"ok": True, "started": True}


@app.get("/api/run/status")
def api_run_status():
    with _job_lock:
        return dict(_job)


@app.post("/api/test-email")
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
