"""Persist Twilio voice booking attempts for admin analytics."""

from __future__ import annotations

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
