#!/usr/bin/env python3
"""
auth.py — เข้าสู่ระบบผู้ดูแล (admin) ด้วยรหัส OTP ทางอีเมล ไม่มีฐานข้อมูล

ผู้ใช้ทั่วไป (ไม่ login) ดูได้เฉพาะหน้า overview / bank detail — ปุ่มรันตรวจสอบและหน้าตั้งค่า
(/config, /logs) สงวนไว้ให้ผู้ดูแลเท่านั้น รายชื่อผู้มีสิทธิ์กำหนดผ่าน env ADMIN_EMAILS
(คั่นด้วย , หรือ ; เหมือน EMAIL_TO) — ไม่เก็บใน settings.json เพราะไฟล์นั้นเว็บเขียนเองได้และ
GET /api/config, /api/settings คืนทั้ง dict ออกไป จะทำให้รายชื่อผู้ดูแลรั่วไปกับ response

Flow: กรอกอีเมล (POST /login) → ถ้าอยู่ใน allowlist ส่งรหัส 6 หลักอายุ 5 นาทีทางอีเมล
(ทุกกรณีตอบข้อความเดียวกัน กันเดาว่าอีเมลไหนมีสิทธิ์) → กรอกรหัส (POST /login/verify) →
ได้ session cookie (เซ็นด้วย itsdangerous ผ่าน SessionMiddleware) อายุ 30 วัน
"""

import os, re, time, secrets, hashlib, threading
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..monitor import common
from . import data_access as da

# ─────────────────────────── Session secret (persist ข้าม restart) ───────────────────────────
_SESSION_SECRET_FILE = os.path.join(da.DATA_DIR, ".session_secret")


def session_secret() -> str:
    """ใช้ env SESSION_SECRET ถ้าตั้งไว้ ไม่งั้น generate เก็บไว้ใน DATA_DIR ให้ cookie รอดข้าม restart"""
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret
    try:
        with open(_SESSION_SECRET_FILE, "r", encoding="utf-8") as f:
            secret = f.read().strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        fd = os.open(_SESSION_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret)
    except OSError:
        pass
    return secret


# ─────────────────────────── Allowlist ───────────────────────────
def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {_normalize(e) for e in re.split(r"[;,]", raw) if e.strip()}


# ─────────────────────────── OTP store (in-memory — โปรเซสเดียว ไม่มี --workers) ───────────────────────────
OTP_TTL = 5 * 60           # อายุรหัส 5 นาที
OTP_MAX_ATTEMPTS = 5       # ผิดได้สูงสุด 5 ครั้งต่อรหัส ก่อนต้องขอใหม่
OTP_RESEND_COOLDOWN = 60   # ขอรหัสซ้ำได้ไม่เร็วกว่านี้ต่ออีเมล (วินาที)
OTP_MAX_PER_HOUR = 5       # ขอรหัสได้ไม่เกินกี่ครั้ง/ชั่วโมง/อีเมล
OTP_STORE_MAX = 200        # เกินนี้ค่อยไล่ล้าง entry ที่หมดอายุแล้ว (กัน store โตไม่จำกัด)

_otp_lock = threading.Lock()
_otp_store: dict[str, dict] = {}


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode("utf-8")).hexdigest()


def _cleanup_locked(now: float) -> None:
    if len(_otp_store) <= OTP_STORE_MAX:
        return
    dead = [e for e, v in _otp_store.items() if now > v["expires"]]
    for e in dead:
        del _otp_store[e]


def request_otp(email_raw: str) -> None:
    """ขอรหัส OTP — ไม่คืนค่าใด ๆ ที่บอกได้ว่าอีเมลนี้มีสิทธิ์หรือไม่ (กัน enumeration)
    ส่งอีเมลจริงเฉพาะเมื่ออีเมลอยู่ใน allowlist และไม่ติด rate limit"""
    email = _normalize(email_raw)
    if not email or email not in _admin_emails():
        return
    now = time.time()
    with _otp_lock:
        _cleanup_locked(now)
        entry = _otp_store.get(email)
        recent_sends = [t for t in entry["sent_times"] if now - t < 3600] if entry else []
        if entry and now - entry["last_sent"] < OTP_RESEND_COOLDOWN:
            return
        if len(recent_sends) >= OTP_MAX_PER_HOUR:
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        _otp_store[email] = {
            "hash": _hash_code(email, code),
            "expires": now + OTP_TTL,
            "attempts": 0,
            "last_sent": now,
            "sent_times": recent_sends + [now],
        }

    # subject ต้องไม่มีตัวรหัส — common.send_email log บรรทัด subject ไว้ (rate_monitor.log อ่านได้ทางหน้า /logs)
    subject = "[CheckRate] รหัสยืนยันเข้าสู่ระบบ"
    html = f"""
    <div style="font-family:sans-serif;font-size:15px;color:#222">
      <p>รหัสยืนยันเข้าสู่ระบบ CheckRate ของคุณคือ</p>
      <p style="font-size:28px;font-weight:700;letter-spacing:4px">{code}</p>
      <p style="color:#888">รหัสนี้หมดอายุใน 5 นาที หากไม่ได้เป็นผู้ขอ สามารถละเว้นอีเมลนี้ได้</p>
    </div>
    """
    common.send_email(subject, html, to=[email])


def verify_otp(email_raw: str, code_raw: str) -> bool:
    email, code = _normalize(email_raw), (code_raw or "").strip()
    if not email or not code:
        return False
    now = time.time()
    with _otp_lock:
        entry = _otp_store.get(email)
        if not entry:
            return False
        if now > entry["expires"] or entry["attempts"] >= OTP_MAX_ATTEMPTS:
            del _otp_store[email]
            return False
        entry["attempts"] += 1
        if entry["hash"] != _hash_code(email, code):
            return False
        del _otp_store[email]
        return True


# ─────────────────────────── Dependencies บังคับสิทธิ์ ───────────────────────────
def is_admin(request: Request) -> bool:
    email = request.session.get("admin_email")
    return bool(email) and email in _admin_emails()


class LoginRequired(Exception):
    """หน้า HTML ที่ต้อง login — exception handler ใน main.py จะ redirect ไป /login"""
    def __init__(self, next_url: str):
        self.next_url = next_url


def require_admin_page(request: Request) -> None:
    if not is_admin(request):
        raise LoginRequired(request.url.path)


def require_admin_api(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="ต้องเข้าสู่ระบบก่อนใช้งานส่วนนี้")


async def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(_safe_next(exc.next_url), safe='')}", status_code=303)


def auth_context(request: Request) -> dict:
    """ส่งเข้าทุก template ผ่าน Jinja2Templates(context_processors=...) — ใช้ซ่อน nav/ปุ่มที่ต้อง admin"""
    admin = is_admin(request)
    # นับคำขอใหม่เฉพาะตอนเป็น admin (badge บนเมนู) — ไม่งั้นทุกหน้า public จะอ่านไฟล์เปล่า ๆ
    return {"is_admin": admin, "admin_email": request.session.get("admin_email"),
            "new_request_count": da.count_new_requests() if admin else 0}


# ─────────────────────────── Routes ───────────────────────────
_templates: Jinja2Templates | None = None


def configure(templates: Jinja2Templates) -> None:
    """เรียกจาก main.py หลังสร้าง Jinja2Templates — ให้ router ในไฟล์นี้ render ผ่านอินสแตนซ์เดียวกัน"""
    global _templates
    _templates = templates


def _safe_next(raw: str | None) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    next_safe = _safe_next(next)
    if is_admin(request):
        return RedirectResponse(next_safe, status_code=303)
    return _templates.TemplateResponse(request, "login.html", {"step": "email", "next": next_safe})


@router.post("/login", response_class=HTMLResponse)
async def login_request(request: Request, email: str = Form(...), next: str = Form("/")):
    request_otp(email)
    return _templates.TemplateResponse(request, "login.html", {
        "step": "verify", "email": email.strip(), "next": _safe_next(next),
        "notice": "ถ้าอีเมลนี้มีสิทธิ์เข้าใช้งาน ระบบได้ส่งรหัส 6 หลักไปให้ทางอีเมลแล้ว (หมดอายุใน 5 นาที)",
    })


@router.post("/login/verify", response_class=HTMLResponse)
async def login_verify(request: Request, email: str = Form(...), code: str = Form(...), next: str = Form("/")):
    if verify_otp(email, code):
        request.session["admin_email"] = _normalize(email)
        return RedirectResponse(_safe_next(next), status_code=303)
    return _templates.TemplateResponse(request, "login.html", {
        "step": "verify", "email": email.strip(), "next": _safe_next(next),
        "error": "รหัสไม่ถูกต้องหรือหมดอายุ กรุณาลองใหม่",
    }, status_code=400)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
