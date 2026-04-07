"""
FastAPI WebSocket server for Twilio ConversationRelay.

Twilio handles STT (Deepgram) and TTS (ElevenLabs) natively.
We only exchange text over WebSocket — no audio processing needed.

Supports multi-service booking: if a caller says "I want chiropractic and
massage", both are detected, confirmed, and booked back-to-back.

Flow per call:
  1. Twilio sends "setup" with callSid, from, etc.
  2. Twilio sends "prompt" with transcribed caller speech.
  3. We send back "text" with our response text.
  4. Twilio converts our text to speech (ElevenLabs) and plays it.

Run: uvicorn voice_relay:app --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type, datetime, timedelta
from decimal import Decimal

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from apps.clinic.models import ClinicSettings, VoiceCallLog
from apps.clinic.public_booking_service import create_appointment_from_public_booking
from apps.clinic.serializers import PublicBookingSerializer
from apps.clinic.voice_ai import (
    _booking_catalog_json,
    _parse_time_12h,
    extract_name_from_speech,
    match_services_from_speech,
    openai_parse_datetime,
    parse_datetime_from_speech,
)
from apps.clinic.voice_logging import upsert_voice_call_log

from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("voice_relay")

app = FastAPI(title="ChiroFlow Voice Relay")

MAX_RETRIES = 5


# ─── Helpers ──────────────────────────────────────────────────────────

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


def _svc_names(services: list[dict]) -> str:
    """Human-readable list: 'X and Y' or 'X, Y, and Z'."""
    names = [s["name"] for s in services]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


async def _send_text(ws: WebSocket, text: str, *, last: bool = True):
    """Send a text response to Twilio ConversationRelay."""
    await ws.send_json({
        "type": "text",
        "token": text,
        "last": last,
    })


async def _end_session(ws: WebSocket, text: str = ""):
    """Say a final message and end the ConversationRelay session."""
    if text:
        await _send_text(ws, text, last=True)
    await ws.send_json({"type": "end"})


# ─── Service entry: holds details for one service to book ─────────────

class ServiceEntry:
    """One service the caller wants to book."""

    def __init__(self, svc: dict, provider_id: int | None = None, provider_name: str = ""):
        self.service_id: int = svc["id"]
        self.service_name: str = svc["name"]
        self.service_duration: int = svc["duration_minutes"]
        self.service_price: str = svc["price"]
        self.provider_id: int | None = provider_id
        self.provider_name: str = provider_name
        self.appointment_date: str = ""
        self.start_time: str = ""
        self.booked_appt = None


# ─── Conversation state (per-connection, no Redis needed) ─────────────

class ConversationState:
    """Tracks conversation progress for a single call."""

    def __init__(self, call_sid: str, from_number: str):
        self.call_sid = call_sid
        self.from_number = from_number
        self.step = "name"
        self.retries = 0
        self.first_name = ""
        self.last_name = ""
        self.catalog: dict | None = None

        self.services: list[ServiceEntry] = []
        self.current_svc_idx: int = 0

        # When a category has multiple services, hold them here so we
        # can ask the caller to pick a specific one.
        self.pending_categories: list[str] = []

    @property
    def current_service(self) -> ServiceEntry | None:
        if 0 <= self.current_svc_idx < len(self.services):
            return self.services[self.current_svc_idx]
        return None

    @property
    def is_multi(self) -> bool:
        return len(self.services) > 1

    @property
    def has_more_services(self) -> bool:
        return self.current_svc_idx < len(self.services) - 1


# ─── Step handlers ────────────────────────────────────────────────────

async def handle_name(ws: WebSocket, state: ConversationState, speech: str):
    fn, ln = extract_name_from_speech(speech)

    if not fn and speech.strip():
        words = speech.strip().split()
        fn = words[0].title()
        ln = " ".join(words[1:]).title() if len(words) > 1 else ""

    logger.info("Voice [%s] name: %r %r", state.call_sid[:8], fn, ln)

    if not fn:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="name",
            )
            await _end_session(ws, "I'm having trouble hearing you. Please call back or book online. Goodbye!")
            return
        await _send_text(ws, "Sorry, I missed that. Could you say your name again?")
        return

    state.first_name = fn
    state.last_name = ln
    state.step = "service"
    state.retries = 0
    state.catalog = _booking_catalog_json()

    name = f"{fn} {ln}".strip()
    services = _svc_list(state.catalog)
    await _send_text(
        ws,
        f"Got it, {name}! We offer: {services}. "
        "Which one would you like? You can also book more than one.",
    )


async def handle_service(ws: WebSocket, state: ConversationState, speech: str):
    catalog = state.catalog or _booking_catalog_json()
    all_services = catalog.get("services") or []
    pbs = catalog.get("providers_by_service") or {}
    matched = match_services_from_speech(speech, all_services)

    if not matched:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail=f"service: {speech}",
            )
            await _end_session(ws, "I couldn't find that service. Please book online or call the front desk. Goodbye!")
            return
        await _send_text(ws, f"I didn't catch the service. We have: {_svc_list(catalog)}. Which one?")
        return

    state.retries = 0

    # For each matched service, check if the match was a generic category pick
    # when the category actually has multiple specific services. If so, ask the
    # caller to choose.
    resolved: list[dict] = []
    pending: list[str] = []

    for svc in matched:
        stype = svc.get("service_type", "")
        same_type = [s for s in all_services if s.get("service_type") == stype]
        if len(same_type) > 1 and _was_category_match(speech, svc, same_type):
            if stype not in pending:
                pending.append(stype)
        else:
            resolved.append(svc)

    state.services = []
    for svc in resolved:
        providers = pbs.get(svc["id"]) or []
        pid = providers[0]["id"] if providers else None
        pname = providers[0]["provider_name"] if providers else ""
        state.services.append(ServiceEntry(svc, pid, pname))

    if pending:
        state.pending_categories = pending
        state.step = "pick_service"
        state.retries = 0
        cat = pending[0]
        cat_services = [s for s in all_services if s.get("service_type") == cat]
        names = ", ".join(s["name"] for s in cat_services)
        label = "chiropractic" if cat == "chiropractic" else "massage"
        await _send_text(
            ws,
            f"For {label}, we have: {names}. Which one would you like?",
        )
        return

    state.current_svc_idx = 0
    await _finish_service_selection(ws, state)


def _was_category_match(speech: str, matched_svc: dict, same_type_svcs: list[dict]) -> bool:
    """Return True if the speech matched a generic category keyword rather
    than a specific service name.  E.g. 'chiropractic' with multiple chiro
    services should prompt for specifics; 'adjustment' should not."""
    import re
    s = speech.lower().strip()
    s = re.sub(r"^(i('d| would) like|i want|can i get|let's do)\s+", "", s).strip()
    s = re.sub(r"\b(a|an|the|both|and|also|please|thanks)\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    name_low = matched_svc["name"].lower()
    if name_low in s or s in name_low:
        return False
    for svc in same_type_svcs:
        if svc["name"].lower() in s:
            return False
    return True


async def _finish_service_selection(ws: WebSocket, state: ConversationState):
    """After all services are resolved, proceed to confirm or datetime."""
    if state.is_multi:
        state.step = "confirm_services"
        names = _svc_names([{"name": s.service_name} for s in state.services])
        await _send_text(ws, f"You'd like to book both {names}, correct?")
    else:
        svc = state.services[0]
        state.step = "datetime"
        await _send_text(ws, f"Great, {svc.service_name}! What date and time work for you?")


async def handle_pick_service(ws: WebSocket, state: ConversationState, speech: str):
    """Caller picks a specific service from a category with multiple options."""
    catalog = state.catalog or _booking_catalog_json()
    all_services = catalog.get("services") or []
    pbs = catalog.get("providers_by_service") or {}

    if not state.pending_categories:
        state.step = "service"
        await _send_text(ws, "Which service would you like?")
        return

    current_cat = state.pending_categories[0]
    cat_services = [s for s in all_services if s.get("service_type") == current_cat]

    from apps.clinic.voice_ai import match_service_from_speech
    picked = match_service_from_speech(speech, cat_services)

    if not picked:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail=f"pick_service ({current_cat}): {speech}",
            )
            await _end_session(ws, "I'm having trouble with that. Please book online or call the front desk. Goodbye!")
            return
        names = ", ".join(s["name"] for s in cat_services)
        await _send_text(ws, f"I didn't catch which one. We have: {names}. Which would you like?")
        return

    state.retries = 0
    providers = pbs.get(picked["id"]) or []
    pid = providers[0]["id"] if providers else None
    pname = providers[0]["provider_name"] if providers else ""
    state.services.append(ServiceEntry(picked, pid, pname))

    state.pending_categories.pop(0)

    if state.pending_categories:
        next_cat = state.pending_categories[0]
        next_svcs = [s for s in all_services if s.get("service_type") == next_cat]
        names = ", ".join(s["name"] for s in next_svcs)
        label = "chiropractic" if next_cat == "chiropractic" else "massage"
        await _send_text(
            ws,
            f"Got it! And for {label}, we have: {names}. Which one?",
        )
        return

    state.current_svc_idx = 0
    await _finish_service_selection(ws, state)


_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "correct", "right", "sure", "ok", "okay",
    "confirm", "please", "go ahead", "book it", "sounds good", "perfect",
    "absolutely", "do it", "that works", "yes please", "yea", "that's right",
    "that's correct",
}
_NO_WORDS = {
    "no", "nope", "nah", "wrong", "start over", "cancel",
    "not right", "incorrect", "change",
}


async def handle_confirm_services(ws: WebSocket, state: ConversationState, speech: str):
    """Confirm the caller wants multiple services before asking for date/time."""
    lower = speech.lower().strip()
    is_yes = any(w in lower for w in _YES_WORDS)
    is_no = any(w in lower for w in _NO_WORDS)

    if is_no and not is_yes:
        state.step = "service"
        state.retries = 0
        state.services = []
        await _send_text(ws, "No problem! Which service would you like instead?")
        return

    if not is_yes:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="confirm_services",
            )
            await _end_session(ws, "No worries, you can book on our website anytime. Goodbye!")
            return
        names = _svc_names([{"name": s.service_name} for s in state.services])
        await _send_text(ws, f"Just say yes if you'd like both {names}, or no to pick something else.")
        return

    state.step = "datetime"
    state.retries = 0
    svc = state.current_service
    if state.is_multi:
        await _send_text(
            ws,
            f"Let's start with the {svc.service_name}. What date and time work for you?",
        )
    else:
        await _send_text(ws, f"Great! What date and time work for you?")


async def handle_datetime(ws: WebSocket, state: ConversationState, speech: str):
    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    today = timezone.now().astimezone(ZoneInfo(tz_name)).date()

    appt_date, start_time = parse_datetime_from_speech(speech, today)

    if not appt_date or not start_time:
        ai_date, ai_time = openai_parse_datetime(speech, today.isoformat())
        appt_date = appt_date or ai_date
        start_time = start_time or ai_time

    if not appt_date or not start_time:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES,
                detail=f"datetime: {speech}",
            )
            await _end_session(ws, "I'm having trouble with the date. You can also book on our website anytime. Goodbye!")
            return
        if not appt_date and not start_time:
            msg = "I didn't catch the date or time. Could you say something like next Monday at 9 AM?"
        elif not appt_date:
            msg = "I got the time but missed the date. What day would you like?"
        else:
            msg = "I got the date but missed the time. What time works for you?"
        await _send_text(ws, msg)
        return

    svc = state.current_service
    svc.appointment_date = appt_date
    svc.start_time = start_time
    state.step = "confirm"
    state.retries = 0

    try:
        d = date_type.fromisoformat(appt_date)
        dd = d.strftime("%A, %B %d")
    except ValueError:
        dd = appt_date
    t = _parse_time_12h(start_time)
    td = t.strftime("%I:%M %p") if t else start_time
    name = f"{state.first_name} {state.last_name}".strip()

    if state.is_multi and state.has_more_services:
        next_svc = state.services[state.current_svc_idx + 1]
        end_t = _add_minutes(t, svc.service_duration) if t else None
        end_td = end_t.strftime("%I:%M %p") if end_t else "right after"
        await _send_text(
            ws,
            f"OK {name}, {svc.service_name} on {dd} at {td}, "
            f"and then {next_svc.service_name} right after at {end_td}. "
            "Shall I book both? Say yes or no.",
        )
    else:
        await _send_text(
            ws,
            f"OK {name}, {svc.service_name} on {dd} at {td}. "
            "Shall I book it? Say yes or no.",
        )


def _add_minutes(t, minutes: int):
    """Add minutes to a time object, returns a new time."""
    if not t:
        return None
    dt = datetime.combine(date_type.today(), t) + timedelta(minutes=minutes)
    return dt.time()


async def handle_confirm(ws: WebSocket, state: ConversationState, speech: str):
    lower = speech.lower().strip()
    is_yes = any(w in lower for w in _YES_WORDS)
    is_no = any(w in lower for w in _NO_WORDS)

    if is_no and not is_yes:
        svc = state.current_service
        svc.appointment_date = ""
        svc.start_time = ""
        state.step = "datetime"
        state.retries = 0
        await _send_text(ws, "No problem! What other date and time would you like instead?")
        return

    if not is_yes:
        state.retries += 1
        if state.retries >= MAX_RETRIES:
            upsert_voice_call_log(
                call_sid=state.call_sid, from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.ABANDONED_RETRIES, detail="confirm",
            )
            await _end_session(ws, "No worries, you can book on our website anytime. Goodbye!")
            return
        await _send_text(ws, "Just say yes to confirm, or no to pick a different time.")
        return

    await do_book_all(ws, state)


# ─── Booking logic ────────────────────────────────────────────────────

async def do_book_all(ws: WebSocket, state: ConversationState):
    """Book the current service, then auto-schedule and book remaining ones."""
    phone = state.from_number
    if phone.startswith("tel:"):
        phone = phone[4:]

    booked: list[tuple[str, str, str]] = []
    last_end_time = None
    last_date = None

    for idx in range(state.current_svc_idx, len(state.services)):
        svc = state.services[idx]
        state.current_svc_idx = idx

        if idx > 0 and not svc.appointment_date and last_date and last_end_time:
            svc.appointment_date = last_date
            svc.start_time = last_end_time.strftime("%I:%M %p")

        result = await _book_single(ws, state, svc, phone)
        if result is None:
            return

        appt, td, dd = result
        svc.booked_appt = appt
        booked.append((svc.service_name, dd, td))

        last_date = svc.appointment_date
        t = _parse_time_12h(svc.start_time)
        if t:
            last_end_time = _add_minutes(t, svc.service_duration)

    await _build_final_message(ws, state, booked)


async def _book_single(
    ws: WebSocket, state: ConversationState, svc: ServiceEntry, phone: str
) -> tuple | None:
    """Book one service. Returns (appt, time_display, date_display) or None on error."""
    try:
        appt_date = date_type.fromisoformat(svc.appointment_date)
    except (ValueError, KeyError):
        state.step = "datetime"
        state.retries = 0
        await _send_text(ws, "Something went wrong with the date. Could you say the date and time again?")
        return None

    t = _parse_time_12h(svc.start_time)
    if not t:
        state.step = "datetime"
        state.retries = 0
        await _send_text(ws, "Something went wrong with the time. Could you say the date and time again?")
        return None

    payload = {
        "first_name": state.first_name[:100],
        "last_name": state.last_name[:100],
        "phone": phone,
        "email": "",
        "service_id": svc.service_id,
        "service_duration_minutes": int(svc.service_duration),
        "service_price": Decimal(str(svc.service_price)),
        "appointment_date": appt_date,
        "start_time": t,
    }
    if svc.provider_id:
        payload["provider_id"] = int(svc.provider_id)

    transcript = (
        f"Name: {state.first_name} {state.last_name}, "
        f"Service: {svc.service_name}, "
        f"Date: {svc.appointment_date}, Time: {svc.start_time}"
    )

    ser = PublicBookingSerializer(data=payload)
    if not ser.is_valid():
        logger.info("Voice serializer errors: %s", ser.errors)
        upsert_voice_call_log(
            call_sid=state.call_sid, from_number=state.from_number,
            transcript=transcript,
            outcome=VoiceCallLog.Outcome.SERIALIZER_REJECTED,
            detail=str(ser.errors)[:2000],
        )
        svc.appointment_date = ""
        svc.start_time = ""
        state.step = "datetime"
        state.retries = 0
        await _send_text(
            ws,
            f"Sorry, the time for {svc.service_name} doesn't seem to be available. "
            "Would you like to try a different date or time?",
        )
        return None

    appt, err = create_appointment_from_public_booking(ser.validated_data)

    if err:
        logger.info("Voice booking error: %s", err)
        upsert_voice_call_log(
            call_sid=state.call_sid, from_number=state.from_number,
            transcript=transcript,
            outcome=VoiceCallLog.Outcome.SLOT_OR_RULE_ERROR,
            detail=err[:2000],
        )
        svc.appointment_date = ""
        svc.start_time = ""
        state.step = "datetime"
        state.retries = 0
        await _send_text(
            ws,
            f"Unfortunately the time slot for {svc.service_name} is already taken. "
            "What other date or time would work for you?",
        )
        return None

    upsert_voice_call_log(
        call_sid=state.call_sid, from_number=state.from_number,
        transcript=transcript,
        outcome=VoiceCallLog.Outcome.BOOKED, appointment=appt,
    )
    td = appt.start_time.strftime("%I:%M %p")
    dd = appt.appointment_date.strftime("%A, %B %d")
    return appt, td, dd


async def _build_final_message(ws: WebSocket, state: ConversationState, booked: list[tuple[str, str, str]]):
    """Send the final confirmation message and end the session."""
    if len(booked) == 1:
        svc_name, dd, td = booked[0]
        msg = (
            f"You're all set! Your {svc_name} is booked for {dd} at {td}. "
            "You'll get a text and email confirmation shortly. "
            "Thanks for calling, have a great day!"
        )
    else:
        parts = [f"{name} at {td}" for name, dd, td in booked]
        date_display = booked[0][1]
        listing = " and ".join(parts) if len(parts) == 2 else ", ".join(parts[:-1]) + f", and {parts[-1]}"
        msg = (
            f"You're all set! Both appointments are booked for {date_display}: "
            f"{listing}. "
            "You'll get a text and email confirmation for each. "
            "Thanks for calling, have a great day!"
        )
    await _end_session(ws, msg)


# ─── Step dispatcher ──────────────────────────────────────────────────

STEP_HANDLERS = {
    "name": handle_name,
    "service": handle_service,
    "pick_service": handle_pick_service,
    "confirm_services": handle_confirm_services,
    "datetime": handle_datetime,
    "confirm": handle_confirm,
}

NUDGE_MESSAGES = {
    "name": "I'm still here! Could you tell me your first and last name?",
    "service": "Which service would you like? Chiropractic or massage? You can book more than one.",
    "pick_service": "Which specific service would you like?",
    "confirm_services": "Just say yes to confirm both services, or no to pick something else.",
    "datetime": "What date and time work for you? Like next Monday at 9 AM.",
    "confirm": "Just say yes to book it, or no if you'd like to change something.",
}


# ─── WebSocket endpoint ──────────────────────────────────────────────

@app.websocket("/ws/voice")
async def voice_websocket(ws: WebSocket):
    await ws.accept()
    state: ConversationState | None = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            if msg_type == "setup":
                call_sid = msg.get("callSid", "")
                from_number = msg.get("from", "")
                state = ConversationState(call_sid, from_number)

                upsert_voice_call_log(
                    call_sid=call_sid,
                    from_number=from_number,
                    outcome=VoiceCallLog.Outcome.PROMPTED,
                )
                logger.info("Voice WS [%s] setup from %s", call_sid[:8], from_number)

            elif msg_type == "prompt":
                speech = msg.get("voicePrompt", "").strip()

                if not state:
                    logger.warning("Received prompt before setup, ignoring")
                    continue

                logger.info(
                    "Voice WS [%s] step=%s speech=%r",
                    state.call_sid[:8], state.step, speech[:120] if speech else "",
                )

                upsert_voice_call_log(
                    call_sid=state.call_sid,
                    from_number=state.from_number,
                    transcript=speech,
                )

                if not speech:
                    state.retries += 1
                    if state.retries >= MAX_RETRIES:
                        upsert_voice_call_log(
                            call_sid=state.call_sid,
                            from_number=state.from_number,
                            outcome=VoiceCallLog.Outcome.EMPTY_SPEECH,
                            detail="silence",
                        )
                        await _end_session(
                            ws,
                            "It seems like you're not there. "
                            "Feel free to call back anytime. Goodbye!",
                        )
                        break
                    await _send_text(
                        ws,
                        NUDGE_MESSAGES.get(state.step, "I'm here! Go ahead."),
                    )
                    continue

                state.retries = 0
                handler = STEP_HANDLERS.get(state.step, handle_name)
                await handler(ws, state, speech)

            elif msg_type == "interrupt":
                logger.info(
                    "Voice WS [%s] caller interrupted",
                    state.call_sid[:8] if state else "?",
                )

            elif msg_type == "dtmf":
                logger.info(
                    "Voice WS [%s] DTMF: %s",
                    state.call_sid[:8] if state else "?",
                    msg.get("digit", ""),
                )

            elif msg_type == "error":
                logger.error(
                    "Voice WS ConversationRelay error: %s",
                    msg.get("description", "unknown"),
                )

    except WebSocketDisconnect:
        if state:
            logger.info("Voice WS [%s] disconnected", state.call_sid[:8])
    except Exception:
        logger.exception("Voice WS unexpected error")
        if state:
            upsert_voice_call_log(
                call_sid=state.call_sid,
                from_number=state.from_number,
                outcome=VoiceCallLog.Outcome.OPENAI_FAILED,
                detail="WebSocket error",
            )


# ─── Health check ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("VOICE_WS_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
