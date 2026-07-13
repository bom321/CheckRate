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
                       "last_announce": None, "tracked_since": None, "no_prev": False}

    before = [r for r in rows if (r.get("effective_date") or "")[:7] < month]
    in_month = [r for r in rows if (r.get("effective_date") or "")[:7] == month]
    baseline = before[-1] if before else None       # ไม่มีประกาศก่อนหน้าเลย → ไม่มีอะไรให้เทียบ

    products_all = []
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
        products_all.append({
            "label": t.get("alias") or t.get("label") or key,
            "depositor": t.get("depositor") or "บุคคลธรรมดา",   # ไม่ระบุใน config = อัตราของบุคคลธรรมดา
            "key": key, "previous": prev, "current": last,
            "net": net, "times": len(timeline), "timeline": timeline,
        })

    changed = [p for p in products_all if p["times"]]   # 2a/5a: สถิติทั้งหมดนับจากรายการที่เปลี่ยนจริง
    last_row = in_month[-1] if in_month else baseline   # เดือนที่ไม่มีประกาศ → อ้างฉบับสุดท้ายก่อนหน้า
    return base | {
        "announce_count": len(in_month),
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
    }


def _build_overview(month: str, options: list[str]) -> dict:
    """ประกอบ context ของหน้า overview ทั้งหน้า (การ์ดธนาคาร + ตารางกลุ่ม + KPI หัวหน้า)"""
    summaries = [_bank_month_summary(b, month) for b in da.load_banks()]
    active, other = [], []
    for s in summaries:
        (active if s["has_data"] and s["bank"].get("enabled") else other).append(s)
    checked = [da.last_checked(s["bank"]["code"]) for s in summaries if s["has_data"]]

    return {
        "month": month,
        "month_options": options,
        "banks": active,
        "other_banks": other,          # ปิดใช้งาน / ยังไม่มีข้อมูล — แสดงเป็นแถบ muted ท้ายหน้า
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

    options = _month_options(bank["code"])
    if not (month and _MONTH_RE.match(month)):
        month = options[0] if options else datetime.now().strftime("%Y-%m")
    summary = _bank_month_summary(bank, month)

    # ข้อมูลกราฟ: labels = วันที่, 1 dataset ต่อ 1 rate key — แสดงย้อนหลังไม่เกิน 12 ครั้งล่าสุด
    # (history เรียงเก่า→ใหม่อยู่แล้ว slice ท้ายสุด = 12 ครั้งล่าสุด; ธนาคารที่มีน้อยกว่าได้ครบตามเดิม)
    chart_history = history[-12:]
    labels = [thaidate.thai_date(r.get("effective_date", "")) for r in chart_history]
    datasets = []
    for t in targets:
        key = t["key"]
        series = []
        for r in chart_history:
            v = _fmt_rate(r.get(key))
            series.append(float(v) if v is not None else None)
        datasets.append({"key": key, "label": t.get("alias") or t.get("label") or key, "data": series})

    return templates.TemplateResponse(request, "bank_detail.html", {
        "active": "overview", "bank": bank, "targets": targets,
        "item": summary, "month": month, "month_options": options,
        "chart_labels": labels, "chart_datasets": datasets, "has_data": bool(history),
        "last_checked": da.last_checked(code),
        "supports_discover_year": da.supports_discover_year(bank),
        "pdf_years": pdf_years,
        "pdf_count": sum(len(g["files"]) for g in pdf_years),
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


# ─────────────────────────── Logs page + API ───────────────────────────
@app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(auth.require_admin_page)])
def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "active": "logs",
        "banks": [b["code"] for b in da.load_banks()],
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


@app.post("/api/backfill", dependencies=[Depends(auth.require_admin_api)])
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


@app.post("/api/discover-year", dependencies=[Depends(auth.require_admin_api)])
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
