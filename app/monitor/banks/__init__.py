#!/usr/bin/env python3
"""
banks/__init__.py — registry ของตัวอ่านอัตราดอกเบี้ยแยกตามธนาคาร

การเพิ่มธนาคารใหม่ (อาจมี PDF format ต่างจากเดิม):
  1. สร้างไฟล์ banks/<code>.py — กำหนด PARSER_IDS และ extract_rates(pdf_bytes, bank)
     (ทางเลือก: get_effective_date(pdf_bytes) ถ้ารูปแบบวันที่ต่างจากค่าเริ่มต้น)
  2. เพิ่มชื่อ module ลงใน _MODULES ด้านล่าง
ระบบส่วนกลาง (rate_monitor.py / common.py) ไม่มี logic เฉพาะธนาคาร
"""

import hashlib
import importlib
import os

from ..common import log, get_effective_date as _default_effective_date

# รายชื่อ module ของแต่ละธนาคาร (เพิ่มไฟล์ใหม่ที่นี่)
_MODULES = ["scb", "kbank", "ktb", "bbl"]

# parser_id -> module object
_REGISTRY: dict = {}

for _name in _MODULES:
    try:
        _mod = importlib.import_module(f"{__name__}.{_name}")
        for _pid in getattr(_mod, "PARSER_IDS", []):
            _REGISTRY[_pid] = _mod
    except Exception as e:  # pragma: no cover - ป้องกัน module เดียวพังทั้งระบบ
        log.error(f"banks: โหลด module '{_name}' ไม่สำเร็จ: {e}")


def available_parsers() -> list[str]:
    return sorted(_REGISTRY.keys())


# _tablekit ใช้ร่วมกันหลาย parser — แก้ไฟล์นั้นก็ถือว่า parser เปลี่ยนตามด้วย
_SHARED_SOURCES = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tablekit.py")]


def parser_signature(bank: dict) -> str:
    """ลายเซ็นของโค้ด parser ที่ธนาคารนี้ใช้ — เอาไว้ทำให้ parse cache ของ backfill หมดอายุเอง
    เมื่อแก้ logic การอ่านค่า (ไม่ต้องจำ bump เลขเวอร์ชันเอง) อ่านไฟล์ไม่ได้ → คืน 'unknown'
    ซึ่งจะทำให้ cache miss เสมอ (ปลอดภัยกว่าใช้ค่าที่อาจเก่า)"""
    mod = _REGISTRY.get(bank.get("parser", ""))
    src = getattr(mod, "__file__", None)
    if not src:
        return "unknown"
    h = hashlib.sha256()
    for path in [src, *_SHARED_SOURCES]:
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            return "unknown"
    return h.hexdigest()[:16]


def extract_rates(pdf_bytes: bytes, bank: dict) -> dict | None:
    """dispatch ตาม bank['parser'] → ตัวอ่านของธนาคารนั้น"""
    parser = bank.get("parser", "")
    mod = _REGISTRY.get(parser)
    if mod is None:
        log.error(f"extract_rates: ยังไม่รองรับ parser '{parser}' "
                  f"(มี: {', '.join(available_parsers()) or 'ไม่มี'})")
        return None
    return mod.extract_rates(pdf_bytes, bank)


def effective_date(pdf_bytes: bytes, bank: dict) -> str | None:
    """ใช้ get_effective_date ของ bank module ถ้ามี ไม่งั้นใช้ตัว default (Thai date)"""
    mod = _REGISTRY.get(bank.get("parser", ""))
    if mod is not None and hasattr(mod, "get_effective_date"):
        return mod.get_effective_date(pdf_bytes)
    return _default_effective_date(pdf_bytes)


def resolve_latest_url(bank: dict) -> str | None:
    """หา URL ของประกาศล่าสุด — ใช้ resolve_latest_url ของ bank module ถ้ามี
    (เช่น KBANK ที่ URL ฝังวันที่ ไม่มี URL คงที่แบบ SCB) ไม่งั้นใช้ bank['latest_pdf_url'] ตรง ๆ"""
    mod = _REGISTRY.get(bank.get("parser", ""))
    if mod is not None and hasattr(mod, "resolve_latest_url"):
        return mod.resolve_latest_url(bank)
    return bank.get("latest_pdf_url") or None


def supports_discover_year(bank: dict) -> bool:
    """True ถ้า bank module รองรับการสแกนหาประวัติทั้งปีแบบละเอียด (discover_year)"""
    mod = _REGISTRY.get(bank.get("parser", ""))
    return mod is not None and hasattr(mod, "discover_year")


def discover_year(bank: dict, year: int | None = None) -> list[str] | None:
    """สแกนหาประกาศทั้งปีแบบละเอียด (manual, ไม่ใช้ทุกวัน) — คืน None ถ้า bank module ไม่รองรับ"""
    mod = _REGISTRY.get(bank.get("parser", ""))
    if mod is not None and hasattr(mod, "discover_year"):
        return mod.discover_year(bank, year)
    return None
