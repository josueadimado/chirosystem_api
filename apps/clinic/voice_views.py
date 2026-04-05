"""
Twilio Programmable Voice – AI booking assistant.

Flow: Name → Service → Date/Time → Confirm → Book.
If a slot is taken, loops back to Date/Time instead of hanging up.
All steps use instant local parsing; OpenAI is only a date/time fallback.

Webhook: {TWILIO_VOICE_PUBLIC_BASE_URL}/api/v1/voice/twilio/incoming/
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

CONV_TTL = 900
MAX_RETRIES = 5


# ─── TwiML helpers ────────────────────────────────────────────────────

def _voice_url(request, named_route: str) -> str:
    path = reverse(named_route)
    base = (getattr(settings, "TWILIO_VOICE_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def _sig_ok(request, route_name: str) -> bool:
    token = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    if not token:
        return False
    if settings.DEBUG and getattr(settings, "VOICE_SKIP_TWILIO_SIGNATURE", False):
        return True
    validator = RequestValidator(token)
    url = _voice_url(request, route_name)
    sig = request.META.get("HTTP_X_TWILIO_SIGNATURE", "") or ""
    return bool(sig and validator.validate(url, request.POST.dict(), sig))


def _xml(inner: str) -> HttpResponse:
    return HttpResponse(
        f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
        content_type="text/xml; charset=utf-8",
    )


def _say(text: str) -> str:
    return f'<Say voice="Polly.Joanna">{escape(text)}</Say>'


def _listen(request, prompt: str, *, hint: str = "") -> HttpResponse:
    """Gather speech — prompt plays INSIDE <Gather> so mic is on immediately."""
    action = _voice_url(request, "twilio_voice_gather").replace("&", "&amp;")
    h = f' hints="{escape(hint)}"' if hint else ""
    return _xml(
        f'<Gather input="speech" action="{action}" method="POST" '
        f'timeout="8" speechTimeout="3" speechModel="phone_call" '
        f'language="en-US"{h}>'
        + _say(prompt)
        + "</Gather>"
        + _say("I didn't hear anything. Please call back anytime. Goodbye.")
    )


# ─── Conversation state ───────────────────────────────────────────────

def _key(sid: str) -> str:
    return f"voice_conv:{sid}"

def _get(sid: str) -> dict:
    return cache.get(_key(sid)) or {"step": "name", "retries": 0}

def _put(sid: str, d: dict):
    cache.set(_key(sid), d, CONV_TTL)

def _end(sid: str):
    cache.delete(_key(sid))


def _svc_list(catalog: dict) -> str:
    svcs = catalog.get("services") or []
    chiro = [s["name"] for s in svcs if s.get("service_type") == "chiropractic"]
    mass = [s["name"] for s in svcs if s.get("service_type") == "massage"]
    parts = []
    if chiro:
        parts.append("Chiropractic: " + ", ".join(chiro))
    if mass:
        parts.append("Massage: " + ", ".join(mass))
    return ". ".join(parts) if parts else ", ".join(s["name"] for s in svcs)


# ─── Incoming call ─────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def twilio_voice_incoming(request):
    if not _sig_ok(request, "twilio_voice_incoming"):
        return HttpResponse("Forbidden", status=403)

    sid = (request.POST.get("CallSid") or "").strip()
    frm = (request.POST.get("From") or "").strip()
    upsert_voice_call_log(call_sid=sid, from_number=frm, outcome=VoiceCallLog.Outcome.PROMPTED)
    _put(sid, {"step": "name", "retries": 0, "from_num": frm})

    clinic = ClinicSettings.get_solo()
    return _listen(
        request,
        f"Hi, thanks for calling {escape(clinic.clinic_name)}! "
        "I can help you book an appointment. "
        "What's your first and last name?",
    )


# ─── Main gather router ───────────────────────────────────────────────

@csrf_exempt
@require_POST
def twilio_voice_gather(request):
    if not _sig_ok(request, "twilio_voice_gather"):
        return HttpResponse("Forbidden", status=403)

    speech = (request.POST.get("SpeechResult") or "").strip()
    sid = (request.POST.get("CallSid") or "").strip()
    frm = (request.POST.get("From") or "").strip()

    conv = _get(sid)
    step = conv.get("step", "name")
    logger.info("Voice [%s] step=%s speech=%r", sid[:8], step, speech[:120] if speech else "")

    if frm:
        conv["from_num"] = frm

    if not speech:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.EMPTY_SPEECH, detail="silence")
            return _xml(_say("It seems like you're not there. Feel free to call back anytime. Goodbye!"))
        _put(sid, conv)
        nudge = {
            "name": "I'm still here! Could you tell me your first and last name?",
            "service": "Which service would you like? Chiropractic or massage?",
            "datetime": "What date and time work for you? Like next Monday at 9 AM.",
            "confirm": "Just say yes to book it, or no if you'd like to change something.",
        }
        return _listen(request, nudge.get(step, "I'm here! Go ahead."))

    conv["retries"] = 0
    upsert_voice_call_log(call_sid=sid, from_number=frm, transcript=speech)

    handlers = {
        "name": _step_name,
        "service": _step_service,
        "datetime": _step_datetime,
        "confirm": _step_confirm,
    }
    return handlers.get(step, _step_name)(request, sid, frm, speech, conv)


# ─── Step 1: Name ─────────────────────────────────────────────────────

def _step_name(request, sid, frm, speech, conv):
    fn, ln = extract_name_from_speech(speech)

    if not fn and speech.strip():
        words = speech.strip().split()
        fn = words[0].title()
        ln = " ".join(words[1:]).title() if len(words) > 1 else ""

    logger.info("Voice [%s] name: %r %r", sid[:8], fn, ln)

    if not fn:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="name")
            return _xml(_say("I'm having trouble hearing you. Please call back or book online. Goodbye!"))
        _put(sid, conv)
        return _listen(request, "Sorry, I missed that. Could you say your name again?")

    conv.update(first_name=fn, last_name=ln, step="service", retries=0)
    catalog = _booking_catalog_json()
    conv["_catalog"] = catalog
    _put(sid, conv)

    name = f"{fn} {ln}".strip()
    services = _svc_list(catalog)
    return _listen(
        request,
        f"Got it, {name}! We offer: {services}. Which one would you like?",
        hint=", ".join(s["name"] for s in catalog.get("services", [])),
    )


# ─── Step 2: Service ──────────────────────────────────────────────────

def _step_service(request, sid, frm, speech, conv):
    catalog = conv.get("_catalog") or _booking_catalog_json()
    services = catalog.get("services") or []
    matched = match_service_from_speech(speech, services)

    if not matched:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                                  detail=f"service: {speech}")
            return _xml(_say("I couldn't find that service. Please book online or call the front desk. Goodbye!"))
        _put(sid, conv)
        return _listen(
            request,
            f"I didn't catch the service. We have: {_svc_list(catalog)}. Which one?",
            hint=", ".join(s["name"] for s in services),
        )

    conv.update(
        service_id=matched["id"],
        service_name=matched["name"],
        service_duration=matched["duration_minutes"],
        service_price=matched["price"],
        retries=0,
    )
    pbs = catalog.get("providers_by_service") or {}
    providers = pbs.get(matched["id"]) or []
    if providers:
        conv["provider_id"] = providers[0]["id"]
        conv["provider_name"] = providers[0]["provider_name"]

    conv["step"] = "datetime"
    _put(sid, conv)
    return _listen(
        request,
        f"Great, {matched['name']}! What date and time work for you?",
    )


# ─── Step 3: Date & time ──────────────────────────────────────────────

def _step_datetime(request, sid, frm, speech, conv):
    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    today = timezone.now().astimezone(ZoneInfo(tz_name)).date()

    appt_date, start_time = parse_datetime_from_speech(speech, today)

    if not appt_date or not start_time:
        ai_date, ai_time = openai_parse_datetime(speech, today.isoformat())
        appt_date = appt_date or ai_date
        start_time = start_time or ai_time

    if not appt_date or not start_time:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                                  detail=f"datetime: {speech}")
            return _xml(_say("I'm having trouble with the date. You can also book on our website anytime. Goodbye!"))
        _put(sid, conv)
        if not appt_date and not start_time:
            msg = "I didn't catch the date or time. Could you say something like next Monday at 9 AM?"
        elif not appt_date:
            msg = f"I got the time but missed the date. What day would you like?"
        else:
            msg = f"I got the date but missed the time. What time works for you?"
        return _listen(request, msg)

    conv.update(appointment_date=appt_date, start_time=start_time, step="confirm", retries=0)
    _put(sid, conv)

    try:
        d = date_type.fromisoformat(appt_date)
        dd = d.strftime("%A, %B %d")
    except ValueError:
        dd = appt_date
    t = _parse_time_12h(start_time)
    td = t.strftime("%I:%M %p") if t else start_time
    name = f"{conv.get('first_name', '')} {conv.get('last_name', '')}".strip()
    svc = conv.get("service_name", "your appointment")

    return _listen(
        request,
        f"OK {name}, {svc} on {dd} at {td}. Shall I book it? Say yes or no.",
        hint="yes, no, correct, sounds good",
    )


# ─── Step 4: Confirm ──────────────────────────────────────────────────

_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "correct", "right", "sure", "ok", "okay",
    "confirm", "please", "go ahead", "book it", "sounds good", "perfect",
    "absolutely", "do it", "that works", "yes please", "yea", "that's right",
}
_NO_WORDS = {"no", "nope", "nah", "wrong", "start over", "cancel", "not right", "incorrect", "change"}


def _step_confirm(request, sid, frm, speech, conv):
    lower = speech.lower().strip()
    is_yes = any(w in lower for w in _YES_WORDS)
    is_no = any(w in lower for w in _NO_WORDS)

    # ── Patient wants to change something ──
    if is_no and not is_yes:
        conv.update(step="datetime", retries=0)
        conv.pop("appointment_date", None)
        conv.pop("start_time", None)
        _put(sid, conv)
        return _listen(
            request,
            "No problem! What other date and time would you like instead?",
        )

    # ── Unclear response ──
    if not is_yes:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="confirm")
            return _xml(_say("No worries, you can book on our website anytime. Goodbye!"))
        _put(sid, conv)
        return _listen(request, "Just say yes to confirm, or no to pick a different time.", hint="yes, no")

    # ── Confirmed — try to book ──
    return _do_book(request, sid, frm, conv)


# ─── Booking logic (with retry on slot conflict) ──────────────────────

def _do_book(request, sid, frm, conv):
    phone = conv.get("from_num", frm)
    if phone.startswith("tel:"):
        phone = phone[4:]

    try:
        appt_date = date_type.fromisoformat(conv["appointment_date"])
    except (ValueError, KeyError):
        conv.update(step="datetime", retries=0)
        _put(sid, conv)
        return _listen(request, "Something went wrong with the date. Could you say the date and time again?")

    t = _parse_time_12h(conv.get("start_time", ""))
    if not t:
        conv.update(step="datetime", retries=0)
        _put(sid, conv)
        return _listen(request, "Something went wrong with the time. Could you say the date and time again?")

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
        logger.info("Voice serializer errors: %s", ser.errors)
        upsert_voice_call_log(call_sid=sid, from_number=frm, transcript=transcript,
                              outcome=VoiceCallLog.Outcome.SERIALIZER_REJECTED,
                              detail=str(ser.errors)[:2000])
        conv.update(step="datetime", retries=0)
        conv.pop("appointment_date", None)
        conv.pop("start_time", None)
        _put(sid, conv)
        return _listen(
            request,
            "Sorry, that time doesn't seem to be available. "
            "Would you like to try a different date or time?",
        )

    appt, err = create_appointment_from_public_booking(ser.validated_data)

    if err:
        logger.info("Voice booking error: %s", err)
        upsert_voice_call_log(call_sid=sid, from_number=frm, transcript=transcript,
                              outcome=VoiceCallLog.Outcome.SLOT_OR_RULE_ERROR,
                              detail=err[:2000])
        conv.update(step="datetime", retries=0)
        conv.pop("appointment_date", None)
        conv.pop("start_time", None)
        _put(sid, conv)
        return _listen(
            request,
            "Unfortunately that time slot is already taken. "
            "What other date or time would work for you?",
        )

    # ── Success! ──
    _end(sid)
    upsert_voice_call_log(call_sid=sid, from_number=frm, transcript=transcript,
                          outcome=VoiceCallLog.Outcome.BOOKED, appointment=appt)
    td = appt.start_time.strftime("%I:%M %p")
    dd = appt.appointment_date.strftime("%A, %B %d")
    svc = conv.get("service_name", "appointment")
    return _xml(_say(
        f"You're all set! Your {svc} is booked for {dd} at {td}. "
        "You'll get a text and email confirmation shortly. "
        "Thanks for calling, have a great day!"
    ))
