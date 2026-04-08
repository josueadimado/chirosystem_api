"""
Twilio Programmable Voice – AI booking assistant.

Now uses ConversationRelay (streaming STT + TTS via WebSocket) for ~2-3s latency
instead of the old Gather/Say HTTP loop (~8s).

The incoming webhook returns <Connect><ConversationRelay> TwiML that tells Twilio
to open a WebSocket to our FastAPI voice_relay server.

The old <Gather> fallback is kept but should never be called in normal operation.
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

from .models import Appointment, ClinicSettings, Patient, VoiceCallLog
from .utils import normalize_phone
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


def _normalize_conversation_relay_ws_base(raw: str) -> str:
    """
    Twilio requires <ConversationRelay url="..."> to start with wss:// (not https://).
    Accepts common mistakes: https/http origin, missing scheme, or path /ws/voice duplicated in env.
    Returns origin only (no path), or "" if invalid.
    """
    u = (raw or "").strip().rstrip("/")
    if not u:
        return ""
    low = u.lower()
    if low.endswith("/ws/voice"):
        u = u[: -len("/ws/voice")].rstrip("/")
        low = u.lower()
    if "://" not in u:
        u = f"wss://{u.lstrip('/')}"
        low = u.lower()
    elif low.startswith("https://"):
        u = "wss://" + u[8:]
        logger.info(
            "VOICE_WS_PUBLIC_URL used https://; Twilio ConversationRelay requires wss://. Normalized automatically."
        )
    elif low.startswith("http://"):
        u = "wss://" + u[7:]
        logger.warning(
            "VOICE_WS_PUBLIC_URL used http://; normalized to wss://. "
            "Twilio must reach TLS on port 443 for this host."
        )
    low = u.lower()
    if not low.startswith("wss://"):
        logger.error(
            "VOICE_WS_PUBLIC_URL must start with wss:// for ConversationRelay (got %r). "
            "Fix .env or proxy; falling back to Gather if this value is used.",
            (raw or "")[:120],
        )
        return ""
    return u.rstrip("/")


def _elevenlabs_twilio_voice(tts_voice_full: str, voice_id: str) -> str:
    """
    Build the Twilio `voice` attribute for ElevenLabs TTS.

    Twilio format (see voice-configuration): voice_id, optionally -model, optionally -speed_stability_similarity.
    Example: NYC9WEgkq1u4jiqBseQ9-turbo_v2_5-0.8_0.8_0.6
    """
    if (tts_voice_full or "").strip():
        return tts_voice_full.strip()
    base = (voice_id or "").strip() or "UgBBYS2sOqTuMpoF3BR0"
    model = (getattr(settings, "ELEVENLABS_TTS_MODEL", "") or "").strip()
    tuning = (getattr(settings, "ELEVENLABS_TTS_VOICE_TUNING", "") or "").strip()
    if model:
        base = f"{base}-{model}"
    if tuning:
        base = f"{base}-{tuning}"
    return base


# ─── Incoming call (ConversationRelay) ─────────────────────────────────

@csrf_exempt
@require_POST
def twilio_voice_incoming(request):
    if not _sig_ok(request, "twilio_voice_incoming"):
        return HttpResponse("Forbidden", status=403)

    sid = (request.POST.get("CallSid") or "").strip()
    frm = (request.POST.get("From") or "").strip()
    upsert_voice_call_log(call_sid=sid, from_number=frm, outcome=VoiceCallLog.Outcome.PROMPTED)

    ws_raw = (getattr(settings, "VOICE_WS_PUBLIC_URL", "") or "").strip()
    ws_base = _normalize_conversation_relay_ws_base(ws_raw)
    voice_id = (getattr(settings, "ELEVENLABS_VOICE_ID", "") or "").strip()
    clinic = ClinicSettings.get_solo()

    import random
    clinic_name = escape(clinic.clinic_name)

    # Detect returning patients by phone before the WebSocket connects
    patient = None
    norm_phone = normalize_phone(frm)
    if norm_phone:
        patient = Patient.objects.filter(phone=norm_phone).first()

    if patient:
        pname = escape(f"{patient.first_name} {patient.last_name}".strip())
        # Check recent appointment history for smart suggestions
        last_appt = (
            Appointment.objects.filter(patient=patient)
            .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])
            .order_by("-appointment_date")
            .first()
        )
        if last_appt and last_appt.booked_service:
            last_svc = escape(last_appt.booked_service.name)
            returning_greetings = [
                f"Hey {pname}! Welcome back to {clinic_name}. Last time you had a {last_svc} — would you like to book that again, or try something different?",
                f"Hi {pname}, great to hear from you again! I see your last visit was for a {last_svc}. Want to go with that again, or something new?",
                f"{pname}! So glad you called back. Your last appointment was a {last_svc} — should I set up the same thing, or would you like to see what else we offer?",
            ]
        else:
            returning_greetings = [
                f"Hey {pname}! Welcome back to {clinic_name}. What can I help you book today?",
                f"Hi {pname}, good to hear from you again! What service would you like to schedule?",
                f"{pname}! Great to have you back at {clinic_name}. What are you looking to book today?",
            ]
        greeting = random.choice(returning_greetings)
    else:
        new_greetings = [
            f"Hey there! Thanks for calling {clinic_name}. I'm Sarah and I'd love to help you get an appointment set up. Could I get your first and last name?",
            f"Hi! You've reached {clinic_name}, this is Sarah. I can help you book an appointment real quick. What's your name?",
            f"Thanks for calling {clinic_name}! I'm Sarah. Let's get you scheduled — can I start with your first and last name?",
            f"Hey, welcome to {clinic_name}! I'm Sarah and I'll help you book an appointment. What's your name?",
            f"Hi there! Thanks for calling {clinic_name}. I'm Sarah — I can get you booked in just a minute. What's your first and last name?",
        ]
        greeting = random.choice(new_greetings)

    if ws_base:
        relay_ws_url = f"{ws_base}/ws/voice"
        # Twilio disconnects immediately if ttsProvider/voice combo is invalid or ElevenLabs isn't enabled on the account.
        tts_provider = (getattr(settings, "CONVERSATION_RELAY_TTS_PROVIDER", "") or "ElevenLabs").strip()
        tts_voice_setting = (getattr(settings, "CONVERSATION_RELAY_TTS_VOICE", "") or "").strip()
        if tts_provider.lower() == "google":
            tts_provider = "Google"
            tts_voice = tts_voice_setting or "en-US-Journey-O"
        elif tts_provider.lower() == "amazon":
            tts_provider = "Amazon"
            tts_voice = tts_voice_setting or "Joanna-Neural"
        else:
            tts_provider = "ElevenLabs"
            tts_voice = _elevenlabs_twilio_voice(tts_voice_setting, voice_id)
        # Nested <Language> matches Twilio's documented shape; some accounts fail if only parent attributes are set.
        lang_block = (
            f'<Language code="en-US" '
            f'ttsProvider="{escape(tts_provider)}" voice="{escape(tts_voice)}" '
            f'transcriptionProvider="Deepgram" speechModel="nova-2-general"/>'
        )
        el_norm = (getattr(settings, "CONVERSATION_RELAY_ELEVENLABS_TEXT_NORMALIZATION", "") or "").strip().lower()
        relay_el_attr = ""
        if tts_provider == "ElevenLabs" and el_norm in ("on", "off", "auto"):
            relay_el_attr = f' elevenlabsTextNormalization="{escape(el_norm)}"'
        twiml = (
            f'<Connect>'
            f'<ConversationRelay '
            f'url="{escape(relay_ws_url)}" '
            f'welcomeGreeting="{escape(greeting)}" '
            f'language="en-US" '
            f'interruptible="true" '
            f'dtmfDetection="true"'
            f'{relay_el_attr}>'
            f'{lang_block}'
            f'</ConversationRelay>'
            f'</Connect>'
        )
        logger.info("Voice [%s] ConversationRelay url=%s", sid[:8], relay_ws_url)
        return _xml(twiml)

    logger.warning(
        "VOICE_WS_PUBLIC_URL missing or invalid — falling back to legacy Gather loop. "
        "Set wss:// origin (e.g. wss://api.example.com) matching your public WebSocket proxy."
    )
    return _listen(
        request,
        f"Hi, thanks for calling {escape(clinic.clinic_name)}! "
        "I can help you book an appointment. "
        "What's your first and last name?",
    )


# ─── Legacy gather fallback (kept for backward compat) ────────────────

# The gather endpoint still works if ConversationRelay is not configured.
# In normal operation with ConversationRelay, this is never called.

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


# ─── Legacy step handlers ─────────────────────────────────────────────

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
            msg = "I got the time but missed the date. What day would you like?"
        else:
            msg = "I got the date but missed the time. What time works for you?"
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

    if is_no and not is_yes:
        conv.update(step="datetime", retries=0)
        conv.pop("appointment_date", None)
        conv.pop("start_time", None)
        _put(sid, conv)
        return _listen(
            request,
            "No problem! What other date and time would you like instead?",
        )

    if not is_yes:
        conv["retries"] = conv.get("retries", 0) + 1
        if conv["retries"] >= MAX_RETRIES:
            _end(sid)
            upsert_voice_call_log(call_sid=sid, from_number=frm,
                                  outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="confirm")
            return _xml(_say("No worries, you can book on our website anytime. Goodbye!"))
        _put(sid, conv)
        return _listen(request, "Just say yes to confirm, or no to pick a different time.", hint="yes, no")

    return _do_book(request, sid, frm, conv)


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
