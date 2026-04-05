"""
Twilio Programmable Voice webhooks for AI-assisted phone booking.

Multi-step conversational flow:
  1. Greeting → ask name
  2. Confirm name → list services → ask choice
  3. If multiple providers → ask which one (skip if only one)
  4. Ask date and time
  5. Confirm everything → book

Steps 1-3 and 5 use instant local parsing (no API calls).
Step 4 uses a local regex parser first; OpenAI is only called as fallback
for date/time expressions the regex can't handle.

Configure your Twilio phone number "A call comes in" webhook to POST to:
  {TWILIO_VOICE_PUBLIC_BASE_URL}/api/v1/voice/twilio/incoming/

Requires: TWILIO_AUTH_TOKEN (signature validation), OPENAI_API_KEY (optional fallback).
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from decimal import Decimal
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from twilio.request_validator import RequestValidator
from zoneinfo import ZoneInfo

from .models import ClinicSettings, VoiceCallLog
from .public_booking_service import create_appointment_from_public_booking
from .serializers import PublicBookingSerializer
from .voice_ai import (
    _booking_catalog_json,
    _parse_time_12h,
    extract_name_from_speech,
    match_service_from_speech,
    openai_parse_datetime,
    parse_datetime_from_speech,
)
from .voice_logging import upsert_voice_call_log

logger = logging.getLogger(__name__)

CONV_TTL = 900  # 15-minute conversation timeout


# ─── Helpers ───────────────────────────────────────────────────────────

def _voice_absolute_url(request, named_route: str) -> str:
    path = reverse(named_route)
    base = (getattr(settings, "TWILIO_VOICE_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return f"{base}{path}"
    return request.build_absolute_uri(path)


def _twilio_signature_ok(request, route_name: str) -> bool:
    token = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    if not token:
        logger.warning("Twilio voice: TWILIO_AUTH_TOKEN missing; rejecting webhook")
        return False
    if settings.DEBUG and getattr(settings, "VOICE_SKIP_TWILIO_SIGNATURE", False):
        return True
    validator = RequestValidator(token)
    url = _voice_absolute_url(request, route_name)
    params = request.POST.dict()
    signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "") or ""
    return bool(signature and validator.validate(url, params, signature))


def _twiml_response(inner_xml: str) -> HttpResponse:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner_xml}</Response>'
    return HttpResponse(xml, content_type="text/xml; charset=utf-8")


def _say(text: str) -> str:
    return f'<Say voice="Polly.Joanna">{escape(text)}</Say>'


def _gather_speech(request, prompt: str, *, hint: str = "") -> HttpResponse:
    """
    TwiML that listens for speech while the prompt plays.
    Prompt is INSIDE <Gather> so Twilio captures speech immediately.
    """
    action = _voice_absolute_url(request, "twilio_voice_gather").replace("&", "&amp;")
    hints_attr = f' hints="{escape(hint)}"' if hint else ""
    inner = (
        f'<Gather input="speech" action="{action}" method="POST" '
        f'timeout="10" speechTimeout="4" speechModel="phone_call" '
        f'language="en-US"{hints_attr}>'
        + _say(prompt)
        + "</Gather>"
        + _say("I didn't hear a response. Please call back when you're ready. Goodbye.")
    )
    return _twiml_response(inner)


def _conv_key(call_sid: str) -> str:
    return f"voice_conv:{call_sid}"


def _get_conv(call_sid: str) -> dict:
    return cache.get(_conv_key(call_sid)) or {"step": "name", "retries": 0}


def _set_conv(call_sid: str, data: dict):
    cache.set(_conv_key(call_sid), data, CONV_TTL)


def _clear_conv(call_sid: str):
    cache.delete(_conv_key(call_sid))


def _format_service_list(catalog: dict) -> str:
    """Build a spoken list of service categories and names."""
    services = catalog.get("services") or []
    chiro = [s for s in services if s.get("service_type") == "chiropractic"]
    massage = [s for s in services if s.get("service_type") == "massage"]

    parts = []
    if chiro:
        names = ", ".join(s["name"] for s in chiro)
        parts.append(f"For chiropractic, we have: {names}")
    if massage:
        names = ", ".join(s["name"] for s in massage)
        parts.append(f"For massage, we have: {names}")
    if not parts:
        names = ", ".join(s["name"] for s in services)
        parts.append(f"We offer: {names}")
    return ". ".join(parts)


# ─── Incoming call ─────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def twilio_voice_incoming(request):
    if not _twilio_signature_ok(request, "twilio_voice_incoming"):
        return HttpResponse("Forbidden", status=403)

    call_sid = (request.POST.get("CallSid") or "").strip()
    from_num = (request.POST.get("From") or "").strip()
    upsert_voice_call_log(call_sid=call_sid, from_number=from_num, outcome=VoiceCallLog.Outcome.PROMPTED)

    _set_conv(call_sid, {"step": "name", "retries": 0, "from_num": from_num})

    clinic = ClinicSettings.get_solo()
    cn = escape(clinic.clinic_name)
    prompt = (
        f"Thank you for calling {cn}! "
        "I'd be happy to help you book an appointment. "
        "May I have your first and last name, please?"
    )
    return _gather_speech(request, prompt)


# ─── Gather handler (routes to the correct step) ──────────────────────

@csrf_exempt
@require_POST
def twilio_voice_gather(request):
    if not _twilio_signature_ok(request, "twilio_voice_gather"):
        return HttpResponse("Forbidden", status=403)

    speech = (request.POST.get("SpeechResult") or "").strip()
    call_sid = (request.POST.get("CallSid") or "").strip()
    from_num = (request.POST.get("From") or "").strip()

    conv = _get_conv(call_sid)
    step = conv.get("step", "name")

    logger.info("Voice [%s] step=%s speech=%r", call_sid[:8], step, speech[:120] if speech else "")

    if from_num:
        conv["from_num"] = from_num

    if not speech:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= 3:
            _clear_conv(call_sid)
            upsert_voice_call_log(
                call_sid=call_sid, from_number=from_num,
                outcome=VoiceCallLog.Outcome.EMPTY_SPEECH,
                detail="No speech after retries",
            )
            return _twiml_response(_say(
                "I didn't hear anything. Please call back when you're ready. Goodbye."
            ))
        _set_conv(call_sid, conv)
        step_prompts = {
            "name": "I didn't hear your name. Could you say your first and last name?",
            "service": "Which service would you like? You can say chiropractic or massage.",
            "datetime": "What date and time would you like? For example, next Monday at 3 PM.",
            "confirm": "Would you like to confirm this booking? Just say yes or no.",
        }
        return _gather_speech(request, step_prompts.get(step, "Could you please repeat that?"))

    conv["retries"] = 0
    upsert_voice_call_log(call_sid=call_sid, from_number=from_num, transcript=speech)

    handler = {
        "name": _handle_name_step,
        "service": _handle_service_step,
        "datetime": _handle_datetime_step,
        "confirm": _handle_confirm_step,
    }.get(step, _handle_name_step)

    return handler(request, call_sid, from_num, speech, conv)


# ─── Step 1: Name (instant — no AI) ───────────────────────────────────

def _handle_name_step(request, call_sid, from_num, speech, conv):
    fn, ln = extract_name_from_speech(speech)
    logger.info("Voice [%s] name parsed: first=%r last=%r", call_sid[:8], fn, ln)

    if not fn:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= 3:
            _clear_conv(call_sid)
            upsert_voice_call_log(
                call_sid=call_sid, from_number=from_num,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail="Could not get name",
            )
            return _twiml_response(_say(
                "I'm having trouble hearing your name. "
                "Please try again later or book online. Goodbye."
            ))
        _set_conv(call_sid, conv)
        return _gather_speech(
            request,
            "I didn't quite get that. Could you say your first and last name one more time?",
        )

    conv["first_name"] = fn
    conv["last_name"] = ln
    conv["step"] = "service"
    conv["retries"] = 0

    catalog = _booking_catalog_json()
    conv["_catalog"] = catalog
    _set_conv(call_sid, conv)

    name_display = f"{fn} {ln}".strip()
    service_list = _format_service_list(catalog)
    prompt = (
        f"Nice to meet you, {name_display}! "
        f"{service_list}. "
        "Which service would you like to book?"
    )
    hints = ", ".join(s["name"] for s in catalog.get("services", []))
    return _gather_speech(request, prompt, hint=hints)


# ─── Step 2: Service (instant — no AI) ────────────────────────────────

def _handle_service_step(request, call_sid, from_num, speech, conv):
    catalog = conv.get("_catalog") or _booking_catalog_json()
    services = catalog.get("services") or []

    matched = match_service_from_speech(speech, services)
    logger.info("Voice [%s] service matched: %s", call_sid[:8], matched["name"] if matched else "NONE")

    if not matched:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= 3:
            _clear_conv(call_sid)
            upsert_voice_call_log(
                call_sid=call_sid, from_number=from_num,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail=f"Could not match service from: {speech}",
            )
            return _twiml_response(_say(
                "I'm having trouble finding that service. "
                "Please book online or call the front desk. Goodbye."
            ))
        _set_conv(call_sid, conv)
        service_list = _format_service_list(catalog)
        service_names = [s["name"] for s in services]
        return _gather_speech(
            request,
            f"I didn't find that one. Here are our services again: {service_list}. Which would you like?",
            hint=", ".join(service_names),
        )

    conv["service_id"] = matched["id"]
    conv["service_name"] = matched["name"]
    conv["service_duration"] = matched["duration_minutes"]
    conv["service_price"] = matched["price"]
    conv["retries"] = 0

    pbs = catalog.get("providers_by_service") or {}
    providers = pbs.get(matched["id"]) or []
    if providers:
        conv["provider_id"] = providers[0]["id"]
        conv["provider_name"] = providers[0]["provider_name"]

    conv["step"] = "datetime"
    _set_conv(call_sid, conv)
    return _gather_speech(
        request,
        f"Great choice! When would you like to come in for your {matched['name']}? "
        "Please say the date and time, like next Tuesday at 2:30 PM.",
    )


# ─── Step 3: Date & time (local parser → OpenAI fallback) ─────────────

def _handle_datetime_step(request, call_sid, from_num, speech, conv):
    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    today = timezone.now().astimezone(ZoneInfo(tz_name)).date()

    appt_date, start_time = parse_datetime_from_speech(speech, today)

    if not appt_date or not start_time:
        ai_date, ai_time = openai_parse_datetime(speech, today.isoformat())
        if not appt_date and ai_date:
            appt_date = ai_date
        if not start_time and ai_time:
            start_time = ai_time
        logger.info("Voice [%s] after AI fallback: date=%s time=%s", call_sid[:8], appt_date, start_time)

    if not appt_date or not start_time:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= 3:
            _clear_conv(call_sid)
            upsert_voice_call_log(
                call_sid=call_sid, from_number=from_num,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail=f"Could not parse date/time from: {speech}",
            )
            return _twiml_response(_say(
                "I'm having trouble with the date and time. "
                "Please book online or call the front desk. Goodbye."
            ))
        _set_conv(call_sid, conv)
        missing = "date and time" if (not appt_date and not start_time) else ("date" if not appt_date else "time")
        return _gather_speech(
            request,
            f"I got part of that but couldn't catch the {missing}. "
            f"Could you say the full date and time again? Like Monday at 9 AM.",
        )

    conv["appointment_date"] = appt_date
    conv["start_time"] = start_time
    conv["step"] = "confirm"
    conv["retries"] = 0
    _set_conv(call_sid, conv)

    try:
        d = date_type.fromisoformat(appt_date)
        date_display = d.strftime("%A, %B %d")
    except ValueError:
        date_display = appt_date

    t = _parse_time_12h(start_time)
    time_display = t.strftime("%I:%M %p") if t else start_time

    name = f"{conv.get('first_name', '')} {conv.get('last_name', '')}".strip()
    service = conv.get("service_name", "your visit")

    prompt = (
        f"Let me confirm: {name}, {service}, "
        f"on {date_display} at {time_display}. "
        "Does that sound right? Say yes to confirm or no to start over."
    )
    return _gather_speech(request, prompt, hint="yes, no, correct, that's right, sounds good")


# ─── Step 5: Confirm (instant — no AI) ────────────────────────────────

def _handle_confirm_step(request, call_sid, from_num, speech, conv):
    lower = speech.lower().strip()
    affirmatives = {
        "yes", "yeah", "yep", "yup", "correct", "that's right", "right",
        "sure", "ok", "okay", "confirm", "please", "go ahead", "book it",
        "sounds good", "perfect", "absolutely", "do it", "that works",
        "yes please", "yea",
    }
    negatives = {"no", "nope", "nah", "wrong", "start over", "cancel", "not right", "incorrect"}

    is_yes = any(w in lower for w in affirmatives)
    is_no = any(w in lower for w in negatives)

    if is_no and not is_yes:
        conv["step"] = "name"
        conv["retries"] = 0
        for k in ["first_name", "last_name", "service_id", "service_name", "provider_id",
                   "provider_name", "appointment_date", "start_time", "_catalog",
                   "service_duration", "service_price"]:
            conv.pop(k, None)
        _set_conv(call_sid, conv)
        return _gather_speech(
            request,
            "No problem! Let's start over. What is your first and last name?",
        )

    if not is_yes:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= 3:
            _clear_conv(call_sid)
            upsert_voice_call_log(
                call_sid=call_sid, from_number=from_num,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail="Could not confirm",
            )
            return _twiml_response(_say(
                "I'm having trouble confirming. Please try booking online. Goodbye."
            ))
        _set_conv(call_sid, conv)
        return _gather_speech(
            request,
            "Just say yes to confirm the booking, or no to start over.",
            hint="yes, no",
        )

    # ── Confirmed — build payload and book ──
    phone = conv.get("from_num", from_num)
    if phone.startswith("tel:"):
        phone = phone[4:]

    try:
        appt_date = date_type.fromisoformat(conv["appointment_date"])
    except (ValueError, KeyError):
        _clear_conv(call_sid)
        return _twiml_response(_say("Something went wrong with the date. Please call back. Goodbye."))

    t = _parse_time_12h(conv.get("start_time", ""))
    if not t:
        _clear_conv(call_sid)
        return _twiml_response(_say("Something went wrong with the time. Please call back. Goodbye."))

    payload = {
        "first_name": conv.get("first_name", "")[:100],
        "last_name": conv.get("last_name", "")[:100],
        "phone": phone,
        "email": "",
        "service_id": conv.get("service_id"),
        "service_duration_minutes": int(conv.get("service_duration", 15)),
        "service_price": Decimal(str(conv.get("service_price", "0"))),
        "appointment_date": appt_date,
        "start_time": t,
    }
    if conv.get("provider_id"):
        payload["provider_id"] = int(conv["provider_id"])

    transcript = (
        f"Name: {conv.get('first_name')} {conv.get('last_name')}, "
        f"Service: {conv.get('service_name')}, "
        f"Date: {conv.get('appointment_date')}, Time: {conv.get('start_time')}"
    )

    ser = PublicBookingSerializer(data=payload)
    if not ser.is_valid():
        logger.info("Voice booking serializer errors: %s", ser.errors)
        _clear_conv(call_sid)
        upsert_voice_call_log(
            call_sid=call_sid, from_number=from_num, transcript=transcript,
            outcome=VoiceCallLog.Outcome.SERIALIZER_REJECTED,
            detail=str(ser.errors)[:2000],
        )
        return _twiml_response(
            _say(
                "I'm sorry, something didn't work out. "
                "That time slot may not be available. "
                "Please try another time or book online. Goodbye."
            )
        )

    appt, book_err = create_appointment_from_public_booking(ser.validated_data)
    if book_err:
        _clear_conv(call_sid)
        upsert_voice_call_log(
            call_sid=call_sid, from_number=from_num, transcript=transcript,
            outcome=VoiceCallLog.Outcome.SLOT_OR_RULE_ERROR,
            detail=book_err[:2000],
        )
        return _twiml_response(
            _say(f"I'm sorry, that didn't work. {book_err} "
                 "Please try a different time or book online. Goodbye.")
        )

    _clear_conv(call_sid)
    upsert_voice_call_log(
        call_sid=call_sid, from_number=from_num, transcript=transcript,
        outcome=VoiceCallLog.Outcome.BOOKED, appointment=appt,
    )
    t_disp = appt.start_time.strftime("%I:%M %p")
    d_disp = appt.appointment_date.strftime("%A, %B %d")
    return _twiml_response(
        _say(
            f"You're all set! Your {conv.get('service_name', 'appointment')} is booked for "
            f"{d_disp} at {t_disp}. "
            "You'll receive a text confirmation shortly. "
            "Thank you for choosing Relief Chiropractic. Have a great day!"
        )
    )
