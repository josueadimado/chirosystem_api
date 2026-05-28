"""Shared voice-booking pricing and first-visit paperwork facts (always from Service catalog)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import Service
from .utils import format_usd_plain

NEW_PATIENT_PAPERWORK_URL = "https://www.reliefchiropractic.net/s/New-Patient-Paperwork-2025.doc"
PAPERWORK_SITE_HINT = "reliefchiropractic.net — New Patient Paperwork 2025"


def normalize_service_speech(text: str) -> str:
    """Map common caller phrases to catalog names before fuzzy matching."""
    s = (text or "").lower()
    replacements = (
        ("reexamination", "re-examination"),
        ("re examination", "re-examination"),
        ("re exam", "re-examination"),
        ("new patient", "new office"),
        ("first visit", "new office"),
        ("initial visit", "new office"),
    )
    for old, new in replacements:
        s = s.replace(old, new)
    return s


def format_voice_price(amount) -> str:
    """Dollar amount for Sarah to speak (from DB); never invent a default."""
    return format_usd_plain(amount) or "the fee on file for that visit"


def is_new_patient_voice_service(
    *,
    service: Service | None = None,
    svc_dict: dict[str, Any] | None = None,
) -> bool:
    """Intake / first-visit / re-examination — paperwork and fee reminder after booking."""
    if service is not None:
        if getattr(service, "is_new_client_intake", False):
            return True
        name = (service.name or "").lower()
        public = (service.public_booking_name or "").lower()
    elif svc_dict is not None:
        if svc_dict.get("is_new_client_intake"):
            return True
        name = (svc_dict.get("name") or "").lower()
        public = ""
    else:
        return False
    combined = f"{name} {public}"
    return any(
        token in combined
        for token in (
            "new office",
            "new patient",
            "intake",
            "re-examination",
            "reexamination",
            "reactivation",
            "first visit",
            "initial visit",
        )
    )


def find_catalog_service(catalog: dict[str, Any], service_id: int) -> dict[str, Any] | None:
    for svc in catalog.get("services") or []:
        if int(svc["id"]) == int(service_id):
            return svc
    return None


def find_reexamination_service(catalog: dict[str, Any]) -> dict[str, Any] | None:
    for svc in catalog.get("services") or []:
        n = (svc.get("name") or "").lower()
        if "re-examination" in n or "reexamination" in n or "re examination" in n:
            return svc
    return None


def intake_services_for_voice(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for svc in catalog.get("services") or []:
        if svc.get("service_type") != "chiropractic":
            continue
        if is_new_patient_voice_service(svc_dict=svc):
            out.append(svc)
    return out


def service_price_duration_facts(svc: dict[str, Any] | Service) -> tuple[str, int, str]:
    if isinstance(svc, Service):
        price = format_voice_price(svc.price)
        duration = int(svc.duration_minutes or 0)
        name = svc.label_for_public_booking()
    else:
        price = format_voice_price(svc.get("price"))
        duration = int(svc.get("duration_minutes") or 0)
        name = svc.get("name") or "visit"
    return name, duration, price


def after_booking_voice_facts(
    *,
    service: Service | None = None,
    svc_dict: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Structured facts for post-booking LLM instructions (exact service price)."""
    if service is not None:
        name, duration, price = service_price_duration_facts(service)
    elif svc_dict is not None:
        name, duration, price = service_price_duration_facts(svc_dict)
    else:
        name, duration, price = "visit", 0, "the listed fee"
    return {
        "service_name": name,
        "duration_minutes": str(duration),
        "price": price,
        "paperwork_hint": PAPERWORK_SITE_HINT,
        "paperwork_url": NEW_PATIENT_PAPERWORK_URL,
        "payment_line": f"Please note your visit fee is {price} due on the date of service.",
        "paperwork_line": (
            "Please bring completed new patient paperwork or arrive 20 to 25 minutes early "
            f"to fill it out at the clinic. You can download it at {PAPERWORK_SITE_HINT}."
        ),
    }


def realtime_after_booking_prompt_block(catalog: dict[str, Any]) -> str:
    """System-prompt section: per-service fees (no hardcoded $105)."""
    intake = intake_services_for_voice(catalog)
    fee_lines = []
    for svc in intake:
        _, dur, price = service_price_duration_facts(svc)
        fee_lines.append(f"  - {svc['name']} (id={svc['id']}): {price} for {dur} minutes")
    fee_block = "\n".join(fee_lines) if fee_lines else "  - (see AVAILABLE SERVICES list for prices)"
    reexam = find_reexamination_service(catalog)
    reexam_line = ""
    if reexam:
        _, dur, price = service_price_duration_facts(reexam)
        reexam_line = (
            f"\nRE-INTAKE (last visit over 1 year): suggest {reexam['name']} (service_id: {reexam['id']}) — "
            f"{dur} minutes, {price}. Do NOT quote New Office Visit price for a RE-EXAMINATION booking."
        )
    return f"""
═══════════════════════════════════════
AFTER BOOKING (INTAKE / FIRST VISIT / RE-EXAMINATION)
═══════════════════════════════════════
Applies after booking any intake or first-visit service listed below.

CRITICAL — USE THE BOOKED SERVICE PRICE ONLY:
{fee_block}
Never say $105 unless the booked service is New Office Visit at $105.
If you booked RE-EXAMINATION, say that service's fee ({format_voice_price(reexam['price']) if reexam else 'from catalog'}).

After booking, confirm date and time, then:
1) Payment — use the exact fee for the service you booked (from REQUIRED FACTS in book_appointment tool result).
2) Paperwork — bring completed forms or arrive 20–25 minutes early; download at {PAPERWORK_SITE_HINT}
   Direct link: {NEW_PATIENT_PAPERWORK_URL}
3) Insurance reimbursement — ONLY if the caller asked about insurance earlier on this call.

Regular returning visits (not intake): short confirmation + confirmation text only — no paperwork, no 25-min-early reminder.
{reexam_line}

CHIROPRACTIC BRAND-NEW PATIENT (never been to clinic):
- Do NOT book a regular Chiropractic Visit first
- Suggest New Office Visit using its catalog price and duration
- After booking, use that service's fee (not a different visit's fee)
"""


def book_appointment_tool_message(
    *,
    service_name: str,
    date_s: str,
    time_s: str,
    service: Service | None,
    svc_dict: dict[str, Any] | None = None,
) -> str:
    """Tool result text the realtime model must follow when confirming aloud."""
    is_new = is_new_patient_voice_service(service=service, svc_dict=svc_dict)
    if is_new:
        facts = after_booking_voice_facts(service=service, svc_dict=svc_dict)
        return (
            f"Booked {service_name} on {date_s} at {time_s}. "
            "Tell the caller (use these exact facts): "
            f"1. Appointment confirmed for {date_s} at {time_s}. "
            f"2. {facts['payment_line']} "
            f"3. {facts['paperwork_line']} "
            f"4. Download link if needed: {facts['paperwork_url']} "
            "Do NOT mention a different dollar amount than the booked service fee. "
            "Only add insurance reimbursement if they asked about insurance earlier in this call."
        )
    return (
        f"Booked {service_name} on {date_s} at {time_s}. "
        "Returning visit: confirm date/time and mention confirmation text — "
        "do NOT mention 25 minutes early, paperwork, or a first-visit fee."
    )
