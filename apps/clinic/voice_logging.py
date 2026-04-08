"""Persist Twilio voice booking attempts for admin analytics."""

from __future__ import annotations

from asgiref.sync import sync_to_async

from .models import VoiceCallLog


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
        obj.transcript = (transcript or "")[:8000]
    if outcome:
        obj.outcome = outcome
    if detail:
        obj.detail = (detail or "")[:2000]
    if appointment is not None:
        obj.appointment = appointment
    obj.save()


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
    await _upsert_voice_call_log_async(
        call_sid=call_sid,
        from_number=from_number,
        transcript=transcript,
        outcome=outcome,
        detail=detail,
        appointment=appointment,
    )
