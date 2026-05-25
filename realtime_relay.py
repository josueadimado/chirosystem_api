"""
FastAPI WebSocket bridge: Twilio Media Streams ↔ OpenAI Realtime API.

Twilio sends mulaw (g711_ulaw) audio; OpenAI handles STT, reasoning, TTS, and tool calls.
Booking tools use the same Django services as voice_relay.py.

Run: uvicorn realtime_relay:app --host 0.0.0.0 --port 8002
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

from apps.clinic.models import Appointment, ClinicSettings, Provider, Service, VoiceCallLog
from apps.clinic.patient_phone import patients_matching_phone
from apps.clinic.public_booking_service import (
    cancel_appointment_public,
    create_appointment_from_public_booking,
    reschedule_appointment_public,
)
from apps.clinic.serializers import PublicBookingSerializer
from apps.clinic.utils import normalize_phone
from apps.clinic.voice_ai import _booking_catalog_json, _parse_time_12h
from apps.clinic.voice_logging import (
    async_append_voice_conversation_turn,
    async_upsert_voice_call_log,
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
        "description": "List upcoming booked appointments for a phone number.",
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


def _build_system_prompt(*, from_number: str) -> str:
    clinic = ClinicSettings.get_solo()
    catalog = _booking_catalog_json()
    hours = _format_business_hours(clinic)
    returning_note = ""
    norm = normalize_phone(from_number)
    if norm:
        matches = patients_matching_phone(norm)
        if len(matches) == 1:
            p = matches[0]
            returning_note = (
                f"\nRETURNING PATIENT: {p.first_name} {p.last_name} (phone on file). "
                "Do not treat them as a brand-new patient."
            )
        elif len(matches) > 1:
            returning_note = "\nMultiple patients share this phone — confirm full name before booking."

    return f"""You are Sarah, the friendly front desk receptionist at {clinic.clinic_name} in Michigan.

You are on a phone call helping patients book, reschedule, or cancel appointments.

CLINIC INFORMATION:
Name: {clinic.clinic_name}
Phone: (269) 408-0303
Hours: {hours}
Timezone: America/Detroit

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

BOOKING FLOW:
1. Greet the caller
2. Get their first and last name
3. Ask what service they need
4. Ask for preferred date and time
5. Check availability using check_availability before confirming
6. Confirm details and book with book_appointment
7. Give confirmation and say goodbye

HANDLING ISSUES:
- If slot taken: call check_availability and offer alternatives
- If new chiropractic patient may need intake: suggest New Office Visit / intake service from catalog
- If caller wants cancel/reschedule: get_upcoming_appointments then cancel_appointment or reschedule_appointment
- If outside hours: let them know and offer next available day

IMPORTANT:
- Always check availability before confirming a booking
- Always confirm details before book_appointment
- Always say a warm goodbye after booking
"""


def _default_provider_id(catalog: dict[str, Any], service_id: int) -> int | None:
    provs = (catalog.get("providers_by_service") or {}).get(service_id) or []
    if not provs:
        return None
    return int(provs[0]["id"])


def _service_row(catalog: dict[str, Any], service_id: int) -> dict[str, Any] | None:
    for svc in catalog.get("services") or []:
        if int(svc["id"]) == int(service_id):
            return svc
    return None


def get_public_available_slots(
    *,
    service_id: int,
    appt_date: date_type,
    provider_id: int | None = None,
) -> list[str]:
    """
    Same slot logic as public booking availability (views.booking_options.availability).
    Returns human labels like '9:00 AM', '9:15 AM'.
    """
    from apps.clinic.booking_availability import provider_interval_blocked_online
    from apps.clinic.online_booking_hours import (
        CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES,
        effective_public_booking_window_minutes,
        public_booking_last_slot_start_minute,
        public_booking_treatment_duration_minutes,
    )
    from apps.clinic.public_booking_service import public_online_booking_calendar_span_minutes

    service = Service.objects.filter(
        pk=service_id, is_active=True, show_in_public_booking=True
    ).first()
    if not service:
        return []
    catalog = _booking_catalog_json()
    pid = provider_id or _default_provider_id(catalog, service_id)
    if not pid:
        return []
    provider = Provider.objects.filter(pk=pid, active=True).first()
    if not provider:
        return []

    win = effective_public_booking_window_minutes(appt_date, service)
    if not win:
        return []
    day_start, day_end = win
    required_span = public_online_booking_calendar_span_minutes(service)
    closing_compliance_span = public_booking_treatment_duration_minutes(service)
    last_slot_start = public_booking_last_slot_start_minute(appt_date, day_end)

    taken: set[int] = set()
    for s, e in (
        Appointment.objects.filter(provider=provider, appointment_date=appt_date)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.COMPLETED,
            ]
        )
        .values_list("start_time", "end_time")
    ):
        for m in range(s.hour * 60 + s.minute, e.hour * 60 + e.minute):
            taken.add(m)

    available: list[str] = []
    cursor = day_start
    while cursor <= last_slot_start:
        h, m = divmod(cursor, 60)
        slot_start_time = dt_time(hour=h, minute=m)
        end_total = cursor + required_span
        eh, em = divmod(end_total, 60)
        slot_end_time = dt_time(hour=min(eh, 23), minute=em if eh < 24 else 59)
        treat_total = cursor + closing_compliance_span
        teh, tem = divmod(treat_total, 60)
        treat_end = dt_time(hour=min(teh, 23), minute=tem if teh < 24 else 59)
        if cursor + closing_compliance_span <= day_end and not any(
            cursor <= t < cursor + required_span for t in taken
        ):
            if not provider_interval_blocked_online(
                provider.pk,
                appt_date,
                slot_start_time,
                slot_end_time,
                block_overlap_end=treat_end,
            ):
                suffix = "AM" if h < 12 else "PM"
                dh = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
                available.append(f"{dh}:{m:02d} {suffix}")
        cursor += CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES
    return available


def _get_upcoming_appointments(phone: str) -> list[dict[str, Any]]:
    norm = normalize_phone(phone)
    if not norm:
        return []
    patients = patients_matching_phone(norm)
    if not patients:
        return []
    now = timezone.now()
    rows = (
        Appointment.objects.filter(
            patient__in=patients,
            status=Appointment.Status.BOOKED,
            appointment_date__gte=now.date(),
        )
        .select_related("booked_service")
        .order_by("appointment_date", "start_time")[:5]
    )
    out = []
    for a in rows:
        svc = a.booked_service.name if a.booked_service else "appointment"
        out.append(
            {
                "appointment_id": a.id,
                "service": svc,
                "date": a.appointment_date.isoformat(),
                "time": a.start_time.strftime("%I:%M %p").lstrip("0"),
            }
        )
    return out


def _book_appointment_sync(payload: dict[str, Any]) -> dict[str, Any]:
    ser = PublicBookingSerializer(data=payload)
    if not ser.is_valid():
        logger.info("realtime book_appointment serializer errors: %s", ser.errors)
        return {"ok": False, "error": str(ser.errors)}
    vd = ser.validated_data
    appt, err = create_appointment_from_public_booking(vd)
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "appointment_id": appt.id,
        "date": appt.appointment_date.isoformat(),
        "time": appt.start_time.strftime("%I:%M %p").lstrip("0"),
        "service": appt.booked_service.name if appt.booked_service else "",
    }


def _cancel_appointment_sync(phone: str, appointment_id: int) -> dict[str, Any]:
    norm = normalize_phone(phone)
    if not norm:
        return {"ok": False, "error": "Invalid phone number."}
    appt, err = cancel_appointment_public(
        phone_normalized=norm, appointment_id=int(appointment_id)
    )
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "appointment_id": appt.id if appt else appointment_id}


def _reschedule_appointment_sync(
    phone: str, appointment_id: int, new_date: str, new_time: str
) -> dict[str, Any]:
    norm = normalize_phone(phone)
    if not norm:
        return {"ok": False, "error": "Invalid phone number."}
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


_check_availability_async = sync_to_async(get_public_available_slots, thread_sensitive=True)
_get_upcoming_async = sync_to_async(_get_upcoming_appointments, thread_sensitive=True)
_book_async = sync_to_async(_book_appointment_sync, thread_sensitive=True)
_cancel_async = sync_to_async(_cancel_appointment_sync, thread_sensitive=True)
_reschedule_async = sync_to_async(_reschedule_appointment_sync, thread_sensitive=True)
_build_prompt_async = sync_to_async(_build_system_prompt, thread_sensitive=True)


async def _run_tool(name: str, args: dict[str, Any], *, call_sid: str, from_number: str) -> str:
    logger.info("Realtime tool call [%s]: %s(%s)", call_sid[:8], name, args)
    try:
        if name == "check_availability":
            service_id = int(args["service_id"])
            appt_date = date_type.fromisoformat(str(args["date"]))
            provider_id = args.get("provider_id")
            pid = int(provider_id) if provider_id is not None else None
            slots = await _check_availability_async(
                service_id=service_id, appt_date=appt_date, provider_id=pid
            )
            return json.dumps({"available_slots": slots, "date": appt_date.isoformat()})

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
            return json.dumps(result)

        if name == "get_upcoming_appointments":
            phone = str(args.get("phone") or from_number)
            appts = await _get_upcoming_async(phone)
            return json.dumps({"appointments": appts})

        if name == "cancel_appointment":
            phone = str(args.get("phone") or from_number)
            result = await _cancel_async(phone, int(args["appointment_id"]))
            return json.dumps(result)

        if name == "reschedule_appointment":
            phone = str(args.get("phone") or from_number)
            result = await _reschedule_async(
                phone,
                int(args["appointment_id"]),
                str(args["new_date"]),
                str(args["new_time"]),
            )
            return json.dumps(result)

        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as exc:
        logger.exception("Realtime tool %s failed", name)
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
                "instructions": instructions,
                "modalities": ["text", "audio"],
                "voice": "shimmer",
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600,
                },
                "tools": REALTIME_TOOLS,
                "tool_choice": "auto",
            },
        }
        await self.openai_ws.send(json.dumps(session_update))
        logger.info("Realtime [%s] OpenAI session started", self.call_sid[:8])

    async def send_greeting(self) -> None:
        if self._greeting_sent or not self.openai_ws or not self.greeting.strip():
            return
        self._greeting_sent = True
        await self.openai_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio", "text"],
                        "instructions": (
                            "Say the following to the caller as your very first words, "
                            f"warmly and naturally: {self.greeting}"
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

    async def twilio_media_to_openai(self, payload_b64: str) -> None:
        if not self.openai_ws:
            return
        await self.openai_ws.send(
            json.dumps({"type": "input_audio_buffer.append", "audio": payload_b64})
        )

    async def forward_openai_audio_to_twilio(self, delta_b64: str) -> None:
        if not delta_b64:
            return
        await self.twilio_ws.send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": delta_b64},
            }
        )

    async def handle_openai_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype in ("response.output_audio.delta", "response.audio.delta"):
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
            logger.error("Realtime [%s] OpenAI error: %s", self.call_sid[:8], event)
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
            await bridge.handle_openai_event(event)
    except ConnectionClosed:
        logger.info("Realtime [%s] OpenAI socket closed", bridge.call_sid[:8])
    except Exception:
        logger.exception("Realtime [%s] OpenAI reader error", bridge.call_sid[:8])


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
                greeting = (custom.get("greeting") or "").strip()
                from_number = (custom.get("from_number") or start.get("from") or "").strip()
                if not call_sid:
                    call_sid = (custom.get("call_sid") or "unknown").strip()

                bridge = RealtimeBridge(
                    twilio_ws=ws,
                    call_sid=call_sid,
                    from_number=from_number,
                    greeting=greeting,
                    stream_sid=stream_sid,
                )
                await bridge.connect_openai()
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
                    await bridge.twilio_media_to_openai(payload)
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
    except Exception:
        logger.exception(
            "Realtime [%s] unexpected error",
            bridge.call_sid[:8] if bridge else "?",
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
