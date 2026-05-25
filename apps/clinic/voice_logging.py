"""Persist Twilio voice booking attempts for admin analytics."""

from __future__ import annotations

from django.utils import timezone

from asgiref.sync import sync_to_async

from .models import VoiceCallLog

_MAX_CONVERSATION_TURNS = 200
_MAX_TURN_TEXT = 4000


def append_voice_conversation_turn(
    *,
    call_sid: str,
    role: str,
    text: str,
    step: str = "",
    from_number: str = "",
) -> None:
    """Append one dialogue line to the call log (caller, assistant, or system)."""
    if not call_sid or call_sid == "unknown":
        return
    line = (text or "").strip()
    if not line:
        return

    obj, _ = VoiceCallLog.objects.get_or_create(
        call_sid=call_sid,
        defaults={
            "from_number": (from_number or "")[:32],
            "outcome": VoiceCallLog.Outcome.PROMPTED,
        },
    )
    if from_number and not obj.from_number:
        obj.from_number = (from_number or "")[:32]

    turns = list(obj.conversation_log or [])
    turns.append(
        {
            "role": role,
            "text": line[:_MAX_TURN_TEXT],
            "step": (step or "")[:64],
            "at": timezone.now().isoformat(),
        }
    )
    if len(turns) > _MAX_CONVERSATION_TURNS:
        turns = turns[-_MAX_CONVERSATION_TURNS:]
    obj.conversation_log = turns

    if role == "caller":
        obj.transcript = line[:8000]

    obj.save(update_fields=["conversation_log", "transcript", "from_number", "updated_at"])


append_voice_conversation_turn_async = sync_to_async(
    append_voice_conversation_turn, thread_sensitive=True
)


async def async_append_voice_conversation_turn(
    *,
    call_sid: str,
    role: str,
    text: str,
    step: str = "",
    from_number: str = "",
) -> None:
    await append_voice_conversation_turn_async(
        call_sid=call_sid,
        role=role,
        text=text,
        step=step,
        from_number=from_number,
    )


def upsert_voice_call_log(
    *,
    call_sid: str,
    from_number: str = "",
    transcript: str | None = None,
    outcome: str | None = None,
    detail: str = "",
    appointment=None,
) -> None:
    if not call_sid or call_sid == "unknown":
        return
    obj, _ = VoiceCallLog.objects.get_or_create(
        call_sid=call_sid,
        defaults={
            "from_number": (from_number or "")[:32],
            "outcome": VoiceCallLog.Outcome.PROMPTED,
        },
    )
    if from_number and not obj.from_number:
        obj.from_number = (from_number or "")[:32]
    if transcript is not None:
        line = (transcript or "").strip()
        if line:
            append_voice_conversation_turn(
                call_sid=call_sid,
                role="caller",
                text=line,
                from_number=from_number,
            )
        else:
            obj.transcript = ""
    if outcome:
        # Do not replace a terminal success / intentional hang-up with a generic TCP disconnect.
        if outcome == VoiceCallLog.Outcome.DISCONNECTED and obj.outcome in (
            VoiceCallLog.Outcome.BOOKED,
            VoiceCallLog.Outcome.EMPTY_SPEECH,
            VoiceCallLog.Outcome.ABANDONED_RETRIES,
        ):
            pass
        else:
            obj.outcome = outcome
    if detail:
        obj.detail = (detail or "")[:2000]
    if appointment is not None:
        obj.appointment = appointment
    obj.save(
        update_fields=[
            "from_number",
            "transcript",
            "outcome",
            "detail",
            "appointment",
            "updated_at",
        ]
    )


# One wrapper for the whole process (do not call sync_to_async() inside each await).
_upsert_voice_call_log_async = sync_to_async(upsert_voice_call_log, thread_sensitive=True)


async def async_upsert_voice_call_log(
    *,
    call_sid: str,
    from_number: str = "",
    transcript: str | None = None,
    outcome: str | None = None,
    detail: str = "",
    appointment=None,
) -> None:
    """Same as upsert_voice_call_log but safe to await from async (e.g. FastAPI WebSockets)."""
    if transcript is not None and (transcript or "").strip():
        await async_append_voice_conversation_turn(
            call_sid=call_sid,
            role="caller",
            text=transcript,
            from_number=from_number,
        )
    await _upsert_voice_call_log_async(
        call_sid=call_sid,
        from_number=from_number,
        transcript=None,
        outcome=outcome,
        detail=detail,
        appointment=appointment,
    )
