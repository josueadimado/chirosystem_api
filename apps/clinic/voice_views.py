"""
Twilio Programmable Voice webhooks for AI-assisted phone booking.

Configure your Twilio phone number "A call comes in" webhook to POST to:
  {TWILIO_VOICE_PUBLIC_BASE_URL}/api/v1/voice/twilio/incoming/

Requires: TWILIO_AUTH_TOKEN (signature validation), OPENAI_API_KEY (speech → booking).
"""

from __future__ import annotations

import logging
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
from .voice_ai import _booking_catalog_json, intent_to_booking_payload, openai_parse_booking_intent
from .voice_logging import upsert_voice_call_log

logger = logging.getLogger(__name__)


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


def _gather_followup(request, prompt: str) -> HttpResponse:
    action = _voice_absolute_url(request, "twilio_voice_gather").replace("&", "&amp;")
    inner = (
        _say(prompt)
        + f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" language="en-US">'
        + _say("Go ahead.")
        + "</Gather>"
        + _say("Goodbye.")
    )
    return _twiml_response(inner)


def _retry_or_goodbye(
    request,
    call_sid: str,
    prompt: str,
    *,
    from_num: str = "",
    abandon_outcome: str = VoiceCallLog.Outcome.ABANDONED_RETRIES,
    abandon_detail: str = "",
) -> HttpResponse:
    key = f"voice_gather_retry:{call_sid}"
    n = cache.get(key, 0)
    if n >= 2:
        cache.delete(key)
        upsert_voice_call_log(
            call_sid=call_sid,
            from_number=from_num,
            outcome=abandon_outcome,
            detail=(abandon_detail or prompt)[:2000],
        )
        return _twiml_response(_say("Please call back later or book online. Goodbye."))
    cache.set(key, int(n) + 1, 600)
    return _gather_followup(request, prompt)


@csrf_exempt
@require_POST
def twilio_voice_incoming(request):
    if not _twilio_signature_ok(request, "twilio_voice_incoming"):
        return HttpResponse("Forbidden", status=403)

    call_sid = (request.POST.get("CallSid") or "").strip()
    from_num = (request.POST.get("From") or "").strip()
    upsert_voice_call_log(call_sid=call_sid, from_number=from_num, outcome=VoiceCallLog.Outcome.PROMPTED)

    clinic = ClinicSettings.get_solo()
    cn = escape(clinic.clinic_name)
    action = _voice_absolute_url(request, "twilio_voice_gather").replace("&", "&amp;")
    inner = (
        f'<Say voice="Polly.Joanna">Thank you for calling {cn}. '
        "After the tone, say your first and last name, the type of visit, the date, and time. "
        "For example: Jane Doe, sixty minute massage, March twenty second at two thirty P M.</Say>"
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" language="en-US">'
        + _say("Whenever you are ready.")
        + "</Gather>"
        + _say("I did not hear anything. Please call again. Goodbye.")
    )
    return _twiml_response(inner)


@csrf_exempt
@require_POST
def twilio_voice_gather(request):
    if not _twilio_signature_ok(request, "twilio_voice_gather"):
        return HttpResponse("Forbidden", status=403)

    speech = (request.POST.get("SpeechResult") or "").strip()
    call_sid = (request.POST.get("CallSid") or "").strip()
    from_num = (request.POST.get("From") or "").strip()

    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        upsert_voice_call_log(
            call_sid=call_sid,
            from_number=from_num,
            transcript=speech or "",
            outcome=VoiceCallLog.Outcome.NO_OPENAI,
            detail="OPENAI_API_KEY not set",
        )
        return _twiml_response(
            _say(
                "Our phone booking assistant is not turned on yet. "
                "Please use our website or call the front desk. Goodbye."
            )
        )

    if not speech:
        return _retry_or_goodbye(
            request,
            call_sid,
            "I did not catch that. Please say your first and last name, the service, date, and time.",
            from_num=from_num,
            abandon_outcome=VoiceCallLog.Outcome.EMPTY_SPEECH,
            abandon_detail="No speech after retries",
        )

    upsert_voice_call_log(call_sid=call_sid, from_number=from_num, transcript=speech)

    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    today = timezone.now().astimezone(ZoneInfo(tz_name)).date().isoformat()
    catalog = _booking_catalog_json()
    intent = openai_parse_booking_intent(transcript=speech, today_iso=today, catalog=catalog)
    if not intent:
        return _retry_or_goodbye(
            request,
            call_sid,
            "Sorry, I could not understand. Please repeat a little slower.",
            from_num=from_num,
            abandon_outcome=VoiceCallLog.Outcome.OPENAI_FAILED,
            abandon_detail="OpenAI returned no parseable JSON",
        )

    payload, err = intent_to_booking_payload(intent, caller_e164=from_num, catalog=catalog)
    err_msgs = {
        "missing_name": "Please say your first and last name clearly.",
        "missing_service": "Which visit type do you want? For example initial visit or massage.",
        "missing_provider": "Which therapist would you like? Say their name.",
        "bad_date": "What date works for you?",
        "bad_time": "What time would you like?",
    }
    if err:
        if err in err_msgs:
            return _retry_or_goodbye(
                request,
                call_sid,
                err_msgs[err],
                from_num=from_num,
                abandon_outcome=VoiceCallLog.Outcome.INTENT_INCOMPLETE,
                abandon_detail=err,
            )
        upsert_voice_call_log(
            call_sid=call_sid,
            from_number=from_num,
            transcript=speech,
            outcome=VoiceCallLog.Outcome.INTENT_INCOMPLETE,
            detail=err[:2000],
        )
        return _twiml_response(_say(f"Sorry, {err} Goodbye."))

    ser = PublicBookingSerializer(data=payload)
    if not ser.is_valid():
        logger.info("Voice booking serializer errors: %s", ser.errors)
        return _retry_or_goodbye(
            request,
            call_sid,
            "Something did not match. Please repeat your name, service, date, and time.",
            from_num=from_num,
            abandon_outcome=VoiceCallLog.Outcome.SERIALIZER_REJECTED,
            abandon_detail=str(ser.errors)[:2000],
        )

    appt, book_err = create_appointment_from_public_booking(ser.validated_data)
    if book_err:
        upsert_voice_call_log(
            call_sid=call_sid,
            from_number=from_num,
            transcript=speech,
            outcome=VoiceCallLog.Outcome.SLOT_OR_RULE_ERROR,
            detail=book_err[:2000],
        )
        return _twiml_response(_say(f"Sorry. {book_err} Goodbye."))

    cache.delete(f"voice_gather_retry:{call_sid}")
    upsert_voice_call_log(
        call_sid=call_sid,
        from_number=from_num,
        transcript=speech,
        outcome=VoiceCallLog.Outcome.BOOKED,
        appointment=appt,
    )
    t_disp = appt.start_time.strftime("%I:%M %p")
    d_disp = appt.appointment_date.strftime("%A, %B %d")
    return _twiml_response(
        _say(
            f"Your visit is booked for {d_disp} at {t_disp}. "
            "You should receive a text confirmation shortly. Goodbye."
        )
    )
