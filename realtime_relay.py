"""
FastAPI WebSocket bridge: Twilio Media Streams ↔ OpenAI Realtime API.

Twilio Media Streams send PCMU (g711_ulaw); gpt-realtime uses audio/pcmu — passthrough, no conversion.
Booking tools use the same Django services as voice_relay.py.

Run: uvicorn realtime_relay:app --host 0.0.0.0 --port 8002
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import datetime
from datetime import date as date_type, time as dt_time
from decimal import Decimal
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import websockets
from asgiref.sync import sync_to_async
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from apps.clinic.error_tracking import capture_voice_ai_error
from apps.clinic.models import Appointment, ClinicSettings, Patient, Provider, Service, VoiceCallLog
from apps.clinic.timezone_utils import (
    filter_past_slot_times_for_date,
    get_clinic_tz_name,
    now_clinic,
    today_clinic,
)
from apps.clinic.patient_phone import patients_matching_phone
from apps.clinic.public_booking_service import (
    appointment_to_self_service_payload,
    cancel_appointment_public,
    create_appointment_from_public_booking,
    normalize_caller_phone,
    public_self_service_household_context,
    public_self_service_upcoming_appointments,
    reschedule_appointment_public,
)
from apps.clinic.serializers import PublicBookingSerializer
from apps.clinic.utils import normalize_phone
from apps.clinic.voice_ai import _booking_catalog_json, _parse_time_12h
from apps.clinic.voice_pricing import (
    book_appointment_tool_message,
    find_catalog_service,
    find_reexamination_service,
    is_new_patient_voice_service,
    realtime_after_booking_prompt_block,
    service_price_duration_facts,
)
from apps.clinic.voice_logging import (
    async_append_voice_conversation_turn,
    async_upsert_voice_call_log,
)
from apps.clinic.voice_office import (
    clinic_public_info_prompt_block,
    transfer_active_call_to_office,
    voice_clinic_display_name,
    voice_greeting_for_caller,
)

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("realtime_relay")

app = FastAPI(title="ChiroFlow Realtime Voice Relay")

OPENAI_REALTIME_MODEL = "gpt-realtime"
OPENAI_REALTIME_URL = (
    "wss://api.openai.com/v1/realtime"
    f"?model={OPENAI_REALTIME_MODEL}"
)

# ─── Tool schemas (OpenAI Realtime) ───────────────────────────────────

_REALTIME_ACTIVE_APPOINTMENT_STATUSES = (
    Appointment.Status.BOOKED,
    Appointment.Status.CHECKED_IN,
    Appointment.Status.IN_CONSULTATION,
    Appointment.Status.AWAITING_PAYMENT,
    Appointment.Status.COMPLETED,
)

REALTIME_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "check_availability",
        "description": "List bookable appointment start times for a service on a given date (YYYY-MM-DD).",
        "parameters": {
            "type": "object",
            "properties": {
                "service_id": {"type": "integer", "description": "Service id from the catalog"},
                "date": {"type": "string", "description": "Appointment date YYYY-MM-DD"},
                "provider_id": {
                    "type": "integer",
                    "description": "Optional provider id; defaults to first provider for the service",
                },
            },
            "required": ["service_id", "date"],
        },
    },
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Book an appointment after confirming availability with the caller.",
        "parameters": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "phone": {"type": "string"},
                "service_id": {"type": "integer"},
                "appointment_date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "12-hour time e.g. 2:30 PM"},
                "provider_id": {"type": "integer", "description": "Optional provider id"},
            },
            "required": [
                "first_name",
                "last_name",
                "phone",
                "service_id",
                "appointment_date",
                "start_time",
            ],
        },
    },
    {
        "type": "function",
        "name": "get_upcoming_appointments",
        "description": (
            "List upcoming booked appointments for a phone number. "
            "Each row includes patient_name when several people share the number. "
            "Also returns ambiguous_phone and household_members when applicable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
            },
            "required": ["phone"],
        },
    },
    {
        "type": "function",
        "name": "cancel_appointment",
        "description": "Cancel an upcoming appointment verified by phone.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "appointment_id": {"type": "integer"},
            },
            "required": ["phone", "appointment_id"],
        },
    },
    {
        "type": "function",
        "name": "reschedule_appointment",
        "description": "Reschedule an appointment to a new date and time.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "appointment_id": {"type": "integer"},
                "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                "new_time": {"type": "string", "description": "12-hour time e.g. 10:00 AM"},
            },
            "required": ["phone", "appointment_id", "new_date", "new_time"],
        },
    },
    {
        "type": "function",
        "name": "transfer_to_front_desk",
        "description": (
            "Connect the caller to a live person at the clinic front desk. Use when they ask to "
            "speak with staff, the office, a human, receptionist, or front desk; when they ask "
            "for the clinic phone number or how to reach the office; or when you cannot help and "
            "they need a person. Never read or mention any phone number — just transfer."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


# ─── Sync DB helpers ──────────────────────────────────────────────────

def _format_business_hours(clinic: ClinicSettings) -> str:
    bh = clinic.business_hours
    if not bh:
        return "Monday-Friday 8:00 AM - 5:00 PM, Saturday-Sunday: Closed"
    try:
        if isinstance(bh, str):
            bh = json.loads(bh)
        if isinstance(bh, list):
            return "; ".join(f"{row.get('day', '')}: {row.get('hours', '')}" for row in bh if row)
        if isinstance(bh, dict):
            return "; ".join(f"{day}: {times}" for day, times in bh.items() if times)
    except Exception:
        pass
    return "Monday-Friday 8:00 AM - 5:00 PM, Saturday-Sunday: Closed"


def _services_prompt_block(catalog: dict[str, Any]) -> str:
    lines = []
    for svc in catalog.get("services") or []:
        provs = catalog.get("providers_by_service", {}).get(svc["id"]) or []
        prov_txt = ", ".join(f"{p['id']}={p['provider_name']}" for p in provs) or "default"
        lines.append(
            f"- id={svc['id']}: {svc['name']} ({svc['duration_minutes']} min, ${svc['price']}, "
            f"type={svc.get('service_type', '')}, providers: {prov_txt})"
        )
    return "\n".join(lines) if lines else "(no services loaded)"


def _returning_patient_prompt_note(patient: Patient) -> str:
    """Caller matched one patient on file — recent vs re-intake (1+ year since last visit)."""
    today = timezone.localdate()
    last_appt = (
        Appointment.objects.filter(
            patient=patient,
            status__in=_REALTIME_ACTIVE_APPOINTMENT_STATUSES,
        )
        .order_by("-appointment_date", "-start_time")
        .first()
    )
    if last_appt:
        days_since = (today - last_appt.appointment_date).days
        years_since = days_since / 365
    else:
        days_since = None
        years_since = 999

    if years_since >= 1:
        gap_detail = (
            f"their last visit was over a year ago ({int(years_since)} year(s) ago)"
            if last_appt
            else "they are on file but have no prior visit date in the system"
        )
        catalog = _booking_catalog_json()
        reexam = find_reexamination_service(catalog)
        reexam_hint = ""
        if reexam:
            _, dur, price = service_price_duration_facts(reexam)
            reexam_hint = (
                f" For chiropractic, suggest {reexam['name']} (service_id {reexam['id']}) — "
                f"{dur} minutes, {price} — not the New Office Visit price."
            )
        return (
            f"\nINTERNAL STAFF NOTE (never read aloud — do not mention to the caller until they identify themselves):\n"
            f"RETURNING PATIENT (RE-INTAKE NEEDED): "
            f"{patient.first_name} {patient.last_name} is on file but {gap_detail}. "
            f"They must be treated like a new patient for chiropractic intake.{reexam_hint} "
            f"After booking, quote the exact fee for the service you booked (see AFTER BOOKING section)."
        )

    days_label = str(days_since) if days_since is not None else "unknown"
    return (
        f"\nINTERNAL STAFF NOTE (never read aloud — do not mention to the caller until they identify themselves):\n"
        f"RETURNING PATIENT (recent visit): "
        f"{patient.first_name} {patient.last_name} is on file. "
        f"Last visit was {days_label} days ago. "
        f"Do NOT treat as new patient. "
        f"No paperwork reminder needed. "
        f"They can book regular follow-up visits."
    )


def _build_system_prompt(*, from_number: str) -> str:
    clinic = ClinicSettings.get_solo()
    catalog = _booking_catalog_json()
    hours = _format_business_hours(clinic)
    returning_note = ""
    norm = normalize_phone(from_number)
    if norm:
        matches = patients_matching_phone(norm)
        if len(matches) == 1:
            returning_note = _returning_patient_prompt_note(matches[0])
        elif len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}".strip() for p in matches)
            returning_note = (
                "\nSHARED PHONE / HOUSEHOLD: Multiple patient profiles share this number "
                f"({names}). Confirm who is calling (full name) before booking or discussing visits. "
                "When listing appointments from get_upcoming_appointments, always say each "
                "patient_name with date/time/service so callers are not confused."
            )

    now = now_clinic()
    tz_name = get_clinic_tz_name()
    time_context = f"""
CURRENT DATE AND TIME:
Today is {now.strftime("%A, %B %d, %Y")}
Current time is {now.strftime("%I:%M %p")}
Timezone: {tz_name}

CRITICAL TIME RULES:
- Never offer appointment times that are before {now.strftime("%I:%M %p")} when booking for today
- If check_availability returns suggest_tomorrow: true say:
  "We don't have any more openings today — would tomorrow or another day work for you?"
- When caller says "today" use {now.strftime("%Y-%m-%d")} as the date
- When caller says "this afternoon" or "later today" only consider times after {now.strftime("%I:%M %p")}
"""

    public_clinic = clinic_public_info_prompt_block(clinic, include_phone=False)
    exact_clinic_name = voice_clinic_display_name(clinic)

    return f"""You are Sarah, the friendly front desk receptionist at {exact_clinic_name} in Michigan.

You are on a phone call helping patients with scheduling and basic public clinic information.

PUBLIC CLINIC INFORMATION (safe to share when asked — from Admin Settings):
{public_clinic}
Hours: {hours}
Timezone: {tz_name}
{time_context}

PRIVACY AND CONFIDENTIALITY (critical):
- NEVER disclose patient information, medical details, billing balances, insurance IDs, date of birth, chart notes, or any protected health information.
- Do NOT read another patient's chart, billing, or clinical details — even if you have internal background notes.
- If the opening greeting used the caller's first name (single profile on this number), you may continue using their first name — but still do NOT mention visit history, diagnoses, or billing until needed for scheduling.
- Only discuss upcoming appointments after using get_upcoming_appointments or cancel/reschedule tools (phone must match).
- SHARED PHONE: When ambiguous_phone is true or multiple visits are returned, ask who is calling (full name) if unclear. List each visit with patient_name from the tool (e.g. "Maria's massage Tuesday at 2, Juan's adjustment Tuesday at 2"). After they identify themselves, focus on their visit(s) only.
- If someone asks about a person who is NOT on this phone number's household ("is John Smith coming in?" with no match): say you cannot share other patients' information and call transfer_to_front_desk.
- Calling on behalf of a household member on this number is OK once they confirm that person's full name matches patient_name on the visit.
- Never quote tax IDs, NPI numbers, or internal billing codes.
- Service prices from AVAILABLE SERVICES are public and OK to share.

YOUR SCOPE:
- You CAN help with: booking, rescheduling, canceling, checking availability, listing the caller's own upcoming visits (after phone verification via tools).
- You CAN answer public questions: address, directions, location, email, hours, services and listed prices.
- You CANNOT help with: billing disputes, insurance verification, clinical advice, medical records, or detailed account questions — call transfer_to_front_desk.
- For anything outside scheduling or public clinic info, call transfer_to_front_desk.

PHONE NUMBER — NEVER SAY IT ALOUD:
- NEVER read, spell, or mention the clinic phone number, digits, or "call this number" in any situation.
- If the caller asks for the phone number, how to reach the office, or a direct line: say briefly you will connect them (e.g. "I can connect you to the front desk right now."), then call transfer_to_front_desk — do NOT give digits.

TRANSFER TO A LIVE PERSON:
- If the caller wants to talk to the office, front desk, receptionist, a real person, or staff — say briefly you will connect them (e.g. "Of course — one moment, I'll connect you to the front desk."), then call transfer_to_front_desk.
- Do not argue; transfer promptly when they clearly want a human or when you cannot help.
- If transfer fails, apologize briefly and offer to try connecting again — still do NOT give a phone number.

AVAILABLE SERVICES (use service_id and provider_id in tools):
{_services_prompt_block(catalog)}
{returning_note}

YOUR PERSONALITY:
- Warm, friendly and professional
- Sound like a real person not a robot
- Keep responses SHORT for phone calls
- 1-3 sentences maximum per response
- Use natural contractions (you're, I'll)
- Never say you are an AI or automated
- Vary your responses don't repeat phrases
- CLINIC NAME: Always say "{exact_clinic_name}" exactly — never shorten it, never say "the clinic" or "our office" instead of the full name when referring to the practice.
- OPENING GREETING: Returning patients hear a short hello with their first name + thank you for calling {exact_clinic_name} — do NOT repeat that; ask how you can help. New callers heard the full scheduling + front desk intro — do NOT repeat it; listen and help.

BOOKING FLOW:
1. The opening greeting already ran (short for returning patients, full intro for new callers)
2. Returning patients: you often already know their first name — confirm last name if needed for booking tools
3. New callers: get first and last name
4. Ask what service they need
5. Ask for preferred date and time
6. Check availability using check_availability before confirming
   - Omit provider_id unless the caller asked for a specific doctor — then all eligible providers are checked
7. Confirm details and book with book_appointment
8. After booking give confirmation and say goodbye — see AFTER BOOKING (NEW OFFICE / FIRST VISIT) below for first-visit services.

   IF it's a regular returning visit (not first-visit / intake):
   "You are all set — [service] on [date] at [time]. You'll get a confirmation text shortly. See you then!"

HANDLING ISSUES:
- If caller asks for address, location, directions, or "where are you": give the Address from PUBLIC CLINIC INFORMATION above in plain language.
- If caller asks what hours you are open: give Hours above.
- If caller asks for the phone number or how to reach the office: call transfer_to_front_desk (never read digits).
- If caller asks about insurance (any phrasing like "do you accept my insurance", "do you take Blue Cross", "are you in network"):
  Say: "Yes we accept most major health insurance. Would you like to schedule an appointment?"
  Then guide to booking.
  Do NOT bring up insurance unless asked.
  Do NOT try to verify specific insurance or say you don't know.
- If caller asks "are you accepting new patients" or similar:
  Say: "Yes we are! Would you like to schedule an appointment?"
  Then guide to booking.
- If slot taken: call check_availability and offer alternatives
- If new chiropractic patient may need intake: suggest New Office Visit / intake service from catalog
- If caller wants to check, confirm, cancel, or reschedule a visit: call get_upcoming_appointments first (uses this call's phone number), read empty_hint if no rows, include patient_name when reading visits back, then cancel_appointment or reschedule_appointment with the correct appointment_id
- For reschedule: call check_availability for the same service_id and new date before reschedule_appointment when possible
- If outside hours: let them know and offer next available day
- If caller needs the office for non-scheduling help: call transfer_to_front_desk

INSURANCE TRACKING:
Only mention insurance billing AFTER booking if the caller already asked about insurance earlier in this call.
If you answered an insurance question during this call, remember that — after a first-visit booking also say:
"Your insurance will be billed and you will be reimbursed based on your health benefit plan."
If insurance was never discussed on this call, do NOT mention insurance billing after booking.

{realtime_after_booking_prompt_block(catalog)}

CHIROPRACTIC NEW PATIENT RULES:
If a caller says they have never been to the clinic before AND wants chiropractic:
- Do NOT book "Chiropractic Visit" directly
- Suggest New Office Visit first — quote its price and duration from AVAILABLE SERVICES only
- After they agree, book that service and state THAT service's fee after booking (never a different visit's price)

IMPORTANT:
- Always check availability before confirming a booking
- Always confirm details before book_appointment
- Always say a warm goodbye after booking
- Never say the clinic phone number aloud — connect callers to the front desk with transfer_to_front_desk instead
"""


def _default_provider_id(catalog: dict[str, Any], service_id: int) -> int | None:
    provs = (catalog.get("providers_by_service") or {}).get(service_id) or []
    if not provs:
        return None
    return int(provs[0]["id"])


def _service_row(catalog: dict[str, Any], service_id: int) -> dict[str, Any] | None:
    return find_catalog_service(catalog, service_id)


def _get_public_available_slot_times(
    *,
    service_id: int,
    appt_date: date_type,
    provider_id: int | None = None,
) -> list[dt_time]:
    """Bookable start times — unions all eligible providers when provider_id is omitted."""
    from apps.clinic.booking_availability import public_available_slot_times_for_service

    return public_available_slot_times_for_service(
        service_id=service_id,
        appt_date=appt_date,
        provider_id=provider_id,
    )


def get_public_available_slots(
    *,
    service_id: int,
    appt_date: date_type,
    provider_id: int | None = None,
) -> list[str]:
    """Human labels like '9:00 AM' (no past-slot filter — use _check_availability_sync for voice AI)."""
    times = _get_public_available_slot_times(
        service_id=service_id, appt_date=appt_date, provider_id=provider_id
    )
    return [
        t.strftime("%I:%M %p").lstrip("0")
        for t in times
    ]


def _check_availability_sync(
    service_id: int,
    date_str: str,
    provider_id: int | None = None,
) -> dict[str, Any]:
    """
    Availability for AI voice: clinic-local today + 30-minute buffer filters past slots.
    Checks all eligible providers when provider_id is omitted; falls back to other
    providers when a specific provider is fully booked.
    """
    from apps.clinic.booking_availability import (
        format_slot_time_label,
        public_available_slot_times_for_service,
    )

    try:
        requested_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return {"available": False, "error": "Invalid date format"}

    if not Service.objects.filter(
        pk=service_id, is_active=True, show_in_public_booking=True
    ).exists():
        return {"available": False, "error": "Unknown or inactive service_id."}

    today = today_clinic()
    slots = public_available_slot_times_for_service(
        service_id=service_id,
        appt_date=requested_date,
        provider_id=provider_id,
    )
    alternate_note = ""
    if provider_id is not None and not slots:
        fallback_slots = public_available_slot_times_for_service(
            service_id=service_id,
            appt_date=requested_date,
            provider_id=None,
        )
        if fallback_slots:
            slots = fallback_slots
            alternate_note = (
                "The requested provider is fully booked that day; "
                "these times are with other available providers."
            )

    slots = filter_past_slot_times_for_date(slots, requested_date, buffer_minutes=30)

    if requested_date == today and not slots:
        return {
            "available": False,
            "slots": [],
            "suggest_tomorrow": True,
            "message": "There are no more available slots for today.",
        }

    if not slots:
        return {
            "available": False,
            "slots": [],
            "message": "No available slots for that date.",
        }

    result: dict[str, Any] = {
        "available": True,
        "date": date_str,
        "slots": [format_slot_time_label(s) for s in slots[:8]],
        "timezone": get_clinic_tz_name(),
    }
    if alternate_note:
        result["message"] = alternate_note
        result["requested_provider_unavailable"] = True
    return result


def _caller_phone_for_tools(from_number: str, args_phone: str | None = None) -> str:
    """Prefer Twilio caller ID over model-supplied phone for cancel/reschedule security."""
    return (from_number or "").strip() or (args_phone or "").strip()


def _get_upcoming_appointments(phone: str) -> dict[str, Any]:
    rows, hint = public_self_service_upcoming_appointments(phone)
    household = public_self_service_household_context(phone)
    patient_ids = {a.patient_id for a in rows}
    return {
        **household,
        "appointments": [appointment_to_self_service_payload(a) for a in rows],
        "empty_hint": hint or "",
        "multiple_appointments": len(rows) > 1,
        "multiple_patients_in_results": len(patient_ids) > 1,
    }


def _book_appointment_sync(payload: dict[str, Any]) -> dict[str, Any]:
    ser = PublicBookingSerializer(data=payload)
    if not ser.is_valid():
        logger.info("realtime book_appointment serializer errors: %s", ser.errors)
        return {"ok": False, "success": False, "error": str(ser.errors)}
    vd = ser.validated_data
    appt, err = create_appointment_from_public_booking(vd)
    if err:
        return {"ok": False, "success": False, "error": err}
    svc = appt.booked_service
    svc_name = svc.label_for_public_booking() if svc else ""
    date_s = appt.appointment_date.isoformat()
    time_s = appt.start_time.strftime("%I:%M %p").lstrip("0")
    is_new = is_new_patient_voice_service(service=svc)
    message = book_appointment_tool_message(
        service_name=svc_name,
        date_s=date_s,
        time_s=time_s,
        service=svc,
    )
    return {
        "ok": True,
        "success": True,
        "appointment_id": appt.id,
        "date": date_s,
        "time": time_s,
        "service": svc_name,
        "is_new_patient_service": is_new,
        "message": message,
    }


def _cancel_appointment_sync(phone: str, appointment_id: int) -> dict[str, Any]:
    if not appointment_id:
        return {"ok": False, "error": "appointment_id is required."}
    norm = normalize_caller_phone(phone)
    if not norm:
        return {"ok": False, "error": "Invalid phone number on this call."}
    appt, err = cancel_appointment_public(
        phone_normalized=norm, appointment_id=int(appointment_id)
    )
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "appointment_id": appt.id if appt else appointment_id}


def _reschedule_appointment_sync(
    phone: str, appointment_id: int, new_date: str, new_time: str
) -> dict[str, Any]:
    if not appointment_id:
        return {"ok": False, "error": "appointment_id is required."}
    norm = normalize_caller_phone(phone)
    if not norm:
        return {"ok": False, "error": "Invalid phone number on this call."}
    try:
        d = date_type.fromisoformat(new_date)
    except ValueError:
        return {"ok": False, "error": "Invalid new_date; use YYYY-MM-DD."}
    t = _parse_time_12h(new_time)
    if not t:
        return {"ok": False, "error": "Invalid new_time; use e.g. 2:30 PM."}
    appt, err = reschedule_appointment_public(
        phone_normalized=norm,
        appointment_id=int(appointment_id),
        new_date=d,
        new_start=t,
        sms_consent=True,
    )
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "appointment_id": appt.id,
        "date": appt.appointment_date.isoformat(),
        "time": appt.start_time.strftime("%I:%M %p").lstrip("0"),
    }


def _check_availability_for_date(
    service_id: int,
    appt_date: date_type,
    provider_id: int | None = None,
) -> dict[str, Any]:
    return _check_availability_sync(
        service_id, appt_date.isoformat(), provider_id=provider_id
    )


_check_availability_async = sync_to_async(_check_availability_for_date, thread_sensitive=True)
_get_upcoming_async = sync_to_async(_get_upcoming_appointments, thread_sensitive=True)
_book_async = sync_to_async(_book_appointment_sync, thread_sensitive=True)
_cancel_async = sync_to_async(_cancel_appointment_sync, thread_sensitive=True)
_reschedule_async = sync_to_async(_reschedule_appointment_sync, thread_sensitive=True)
_transfer_async = sync_to_async(transfer_active_call_to_office, thread_sensitive=True)
_build_prompt_async = sync_to_async(_build_system_prompt, thread_sensitive=True)
_voice_greeting_async = sync_to_async(voice_greeting_for_caller, thread_sensitive=True)
_capture_voice_error_async = sync_to_async(capture_voice_ai_error, thread_sensitive=True)


async def _log_voice_error(**kwargs) -> None:
    try:
        await _capture_voice_error_async(**kwargs)
    except Exception:
        logger.exception("error tracker: voice capture failed")


async def _log_voice_tool_failure(
    *,
    name: str,
    result: dict[str, Any],
    call_sid: str,
    from_number: str,
) -> None:
    if result.get("ok") is not False and "error" not in result:
        return
    err = str(result.get("error") or result.get("detail") or "Tool failed")[:8000]
    await _log_voice_error(
        message=f"Realtime tool {name}: {err}",
        channel="realtime",
        operation=f"tool:{name}",
        call_sid=call_sid,
        from_number=from_number,
        level="warning",
        fingerprint_message=err[:500],
        exception_type=f"VoiceTool:{name}"[:200],
    )


async def _run_tool(name: str, args: dict[str, Any], *, call_sid: str, from_number: str) -> str:
    logger.info("Realtime tool call [%s]: %s(%s)", call_sid[:8], name, args)
    try:
        if name == "check_availability":
            service_id = int(args["service_id"])
            appt_date = date_type.fromisoformat(str(args["date"]))
            provider_id = args.get("provider_id")
            pid = int(provider_id) if provider_id is not None else None
            result = await _check_availability_async(
                service_id=service_id, appt_date=appt_date, provider_id=pid
            )
            return json.dumps(result)

        if name == "book_appointment":
            catalog = await sync_to_async(_booking_catalog_json, thread_sensitive=True)()
            svc_id = int(args["service_id"])
            svc_row = _service_row(catalog, svc_id)
            if not svc_row:
                return json.dumps({"ok": False, "error": "Unknown service_id."})
            appt_date = date_type.fromisoformat(str(args["appointment_date"]))
            start_time = _parse_time_12h(str(args["start_time"]))
            if not start_time:
                return json.dumps({"ok": False, "error": "Could not parse start_time."})
            phone = str(args.get("phone") or from_number)
            payload: dict[str, Any] = {
                "first_name": str(args["first_name"])[:100],
                "last_name": str(args["last_name"])[:100],
                "phone": phone,
                "email": "",
                "sms_consent": True,
                "service_id": svc_id,
                "service_duration_minutes": int(svc_row["duration_minutes"]),
                "service_price": Decimal(str(svc_row["price"])),
                "appointment_date": appt_date,
                "start_time": start_time,
            }
            pid = args.get("provider_id") or _default_provider_id(catalog, svc_id)
            if pid:
                payload["provider_id"] = int(pid)
            result = await _book_async(payload)
            if result.get("ok"):
                await async_upsert_voice_call_log(
                    call_sid=call_sid,
                    from_number=from_number,
                    outcome=VoiceCallLog.Outcome.BOOKED,
                    detail=f"booked:{result.get('appointment_id')}",
                )
            else:
                await _log_voice_tool_failure(
                    name="book_appointment",
                    result=result,
                    call_sid=call_sid,
                    from_number=from_number,
                )
            return json.dumps(result)

        if name == "get_upcoming_appointments":
            phone = _caller_phone_for_tools(from_number, args.get("phone"))
            result = await _get_upcoming_async(phone)
            return json.dumps(result)

        if name == "cancel_appointment":
            phone = _caller_phone_for_tools(from_number, args.get("phone"))
            result = await _cancel_async(phone, int(args["appointment_id"]))
            if isinstance(result, dict):
                await _log_voice_tool_failure(
                    name="cancel_appointment",
                    result=result,
                    call_sid=call_sid,
                    from_number=from_number,
                )
            return json.dumps(result)

        if name == "reschedule_appointment":
            phone = _caller_phone_for_tools(from_number, args.get("phone"))
            result = await _reschedule_async(
                phone,
                int(args["appointment_id"]),
                str(args["new_date"]),
                str(args["new_time"]),
            )
            if isinstance(result, dict):
                await _log_voice_tool_failure(
                    name="reschedule_appointment",
                    result=result,
                    call_sid=call_sid,
                    from_number=from_number,
                )
            return json.dumps(result)

        if name == "transfer_to_front_desk":
            result = await _transfer_async(call_sid)
            if result.get("ok"):
                await async_upsert_voice_call_log(
                    call_sid=call_sid,
                    from_number=from_number,
                    outcome=VoiceCallLog.Outcome.DISCONNECTED,
                    detail=f"transferred_to_office:{result.get('transferred_to', '')}",
                )
            else:
                await _log_voice_error(
                    message=str(result.get("error") or "Transfer to front desk failed")[:8000],
                    channel="realtime",
                    operation="transfer_to_front_desk",
                    call_sid=call_sid,
                    from_number=from_number,
                    level="warning",
                )
            return json.dumps(result)

        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as exc:
        logger.exception("Realtime tool %s failed", name)
        await _log_voice_error(
            exc=exc,
            message=f"Realtime tool {name} failed",
            channel="realtime",
            operation=f"tool:{name}",
            call_sid=call_sid,
            from_number=from_number,
        )
        return json.dumps({"ok": False, "error": str(exc)})


class RealtimeBridge:
    """Bridges one Twilio Media Stream call to one OpenAI Realtime session."""

    def __init__(
        self,
        *,
        twilio_ws: WebSocket,
        call_sid: str,
        from_number: str,
        greeting: str,
        stream_sid: str,
    ) -> None:
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.from_number = from_number
        self.greeting = greeting
        self.stream_sid = stream_sid
        self.openai_ws: websockets.WebSocketClientProtocol | None = None
        self._pending_fc: dict[str, dict[str, Any]] = {}
        self._greeting_sent = False

    async def connect_openai(self) -> None:
        api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        instructions = await _build_prompt_async(from_number=self.from_number)
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
        self.openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        )
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": settings.OPENAI_REALTIME_VOICE,
                        "speed": 1.0,
                    },
                },
                "instructions": instructions,
                "tools": REALTIME_TOOLS,
                "tool_choice": "auto",
            },
        }
        await self.openai_ws.send(json.dumps(session_update))
        logger.info("Realtime [%s] OpenAI session started", self.call_sid[:8])

    async def send_greeting(self) -> None:
        """Speak the scripted opening greeting verbatim (clinic name from Admin Settings)."""
        if self._greeting_sent or not self.openai_ws or not self.greeting.strip():
            return
        self._greeting_sent = True
        greeting_text = self.greeting.strip()
        await self.openai_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "The caller just answered the phone. "
                            "Speak the following greeting OUT LOUD right now. "
                            "Use these EXACT words — same clinic name spelling, "
                            "do not paraphrase, shorten the clinic name, or add extra sentences:\n\n"
                            f"{greeting_text}"
                        ),
                    },
                }
            )
        )
        await async_append_voice_conversation_turn(
            call_sid=self.call_sid,
            role="assistant",
            text=self.greeting,
            step="greeting",
            from_number=self.from_number,
        )

    async def receive_from_twilio(self, payload_b64: str) -> None:
        """Twilio Media Stream PCMU payload → OpenAI (passthrough)."""
        if not self.openai_ws or not payload_b64:
            return
        await self.openai_ws.send(
            json.dumps({"type": "input_audio_buffer.append", "audio": payload_b64})
        )

    async def forward_openai_audio_to_twilio(self, delta_b64: str) -> None:
        """OpenAI audio delta → Twilio media event (passthrough)."""
        if not delta_b64:
            return
        await self.twilio_ws.send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": delta_b64},
            }
        )

    async def receive_from_openai(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype in (
            "response.output_audio.delta",
            "response.audio.delta",
            "audio.delta",
        ):
            delta = event.get("delta") or ""
            await self.forward_openai_audio_to_twilio(delta)
            return

        if etype in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                await async_append_voice_conversation_turn(
                    call_sid=self.call_sid,
                    role="assistant",
                    text=transcript,
                    from_number=self.from_number,
                )
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                await async_append_voice_conversation_turn(
                    call_sid=self.call_sid,
                    role="caller",
                    text=transcript,
                    from_number=self.from_number,
                )
            return

        if etype == "response.function_call_arguments.delta":
            call_id = event.get("call_id") or ""
            delta = event.get("delta") or ""
            if call_id:
                bucket = self._pending_fc.setdefault(call_id, {"name": "", "arguments": ""})
                bucket["arguments"] += delta
            return

        if etype == "response.function_call_arguments.done":
            call_id = event.get("call_id") or ""
            name = event.get("name") or ""
            arguments = event.get("arguments") or ""
            if call_id and call_id in self._pending_fc:
                bucket = self._pending_fc.pop(call_id)
                name = name or bucket.get("name") or ""
                arguments = arguments or bucket.get("arguments") or "{}"
            try:
                args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                args = {}
            output = await _run_tool(
                name,
                args,
                call_sid=self.call_sid,
                from_number=self.from_number,
            )
            if self.openai_ws:
                await self.openai_ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": output,
                            },
                        }
                    )
                )
                await self.openai_ws.send(json.dumps({"type": "response.create"}))
            return

        if etype == "error":
            err_msg = str(event.get("error") or event)[:8000]
            logger.error("Realtime [%s] OpenAI error: %s", self.call_sid[:8], event)
            await _log_voice_error(
                message=f"OpenAI Realtime error: {err_msg}",
                channel="realtime",
                operation="openai_error_event",
                call_sid=self.call_sid,
                from_number=self.from_number,
            )
            return

    async def close_openai(self) -> None:
        if self.openai_ws:
            try:
                await self.openai_ws.close()
            except Exception:
                pass
            self.openai_ws = None


async def _openai_reader(bridge: RealtimeBridge) -> None:
    assert bridge.openai_ws is not None
    try:
        async for raw in bridge.openai_ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await bridge.receive_from_openai(event)
    except ConnectionClosed:
        logger.info("Realtime [%s] OpenAI socket closed", bridge.call_sid[:8])
    except Exception as exc:
        logger.exception("Realtime [%s] OpenAI reader error", bridge.call_sid[:8])
        await _log_voice_error(
            exc=exc,
            message="OpenAI Realtime reader failed",
            channel="realtime",
            operation="openai_reader",
            call_sid=bridge.call_sid,
            from_number=bridge.from_number,
        )


@app.websocket("/ws/realtime")
async def twilio_realtime_stream(ws: WebSocket):
    await ws.accept()
    bridge: RealtimeBridge | None = None
    reader_task: asyncio.Task | None = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event", "")

            if event == "connected":
                logger.info("Realtime Twilio connected")
                continue

            if event == "start":
                start = msg.get("start") or {}
                stream_sid = (start.get("streamSid") or "").strip()
                call_sid = (start.get("callSid") or msg.get("callSid") or "").strip()
                custom = start.get("customParameters") or {}
                from_number = (custom.get("from_number") or start.get("from") or "").strip()
                if not call_sid:
                    call_sid = (custom.get("call_sid") or "unknown").strip()

                # Rebuild greeting from live Admin Settings so clinic name is always current.
                greeting = await _voice_greeting_async(from_number)
                if not greeting.strip():
                    greeting = (custom.get("greeting") or "").strip()

                bridge = RealtimeBridge(
                    twilio_ws=ws,
                    call_sid=call_sid,
                    from_number=from_number,
                    greeting=greeting,
                    stream_sid=stream_sid,
                )
                try:
                    await bridge.connect_openai()
                except Exception as exc:
                    await _log_voice_error(
                        exc=exc,
                        message="Could not start OpenAI Realtime session",
                        channel="realtime",
                        operation="connect_openai",
                        call_sid=call_sid,
                        from_number=from_number,
                    )
                    raise
                reader_task = asyncio.create_task(_openai_reader(bridge))
                await bridge.send_greeting()
                logger.info(
                    "Realtime [%s] stream started from=%s",
                    call_sid[:8],
                    from_number[:12] if from_number else "?",
                )
                continue

            if event == "media" and bridge:
                media = msg.get("media") or {}
                payload = media.get("payload") or ""
                if payload:
                    await bridge.receive_from_twilio(payload)
                continue

            if event == "stop":
                logger.info(
                    "Realtime [%s] Twilio stop",
                    bridge.call_sid[:8] if bridge else "?",
                )
                break

    except WebSocketDisconnect:
        logger.info(
            "Realtime [%s] Twilio disconnected",
            bridge.call_sid[:8] if bridge else "?",
        )
    except Exception as exc:
        logger.exception(
            "Realtime [%s] unexpected error",
            bridge.call_sid[:8] if bridge else "?",
        )
        if bridge:
            await _log_voice_error(
                exc=exc,
                message="Realtime voice websocket failed",
                channel="realtime",
                operation="websocket",
                call_sid=bridge.call_sid,
                from_number=bridge.from_number,
            )
    finally:
        if reader_task:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
        if bridge:
            await bridge.close_openai()
            if bridge.call_sid and bridge.call_sid != "unknown":
                await async_upsert_voice_call_log(
                    call_sid=bridge.call_sid,
                    from_number=bridge.from_number,
                    outcome=VoiceCallLog.Outcome.DISCONNECTED,
                    detail="realtime_session_ended",
                )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "realtime_relay"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("REALTIME_WS_PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
