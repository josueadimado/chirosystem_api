"""Twilio SMS for booking confirmations and reminders (optional — requires env keys)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _twilio_creds():
    try:
        from django.conf import settings

        sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
        token = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
        from_num = (getattr(settings, "TWILIO_PHONE_NUMBER", None) or "").strip()
    except Exception:
        sid = token = from_num = ""
    return sid, token, from_num


def twilio_configured() -> bool:
    sid, token, from_num = _twilio_creds()
    return bool(sid and token and from_num)


def send_sms(*, to_e164: str, body: str) -> str | None:
    """
    Send an SMS via Twilio. Returns Message SID on success, None on skip/failure.
    Never raises to callers — logs errors (booking flow must not break on SMS failure).
    """
    if not twilio_configured():
        logger.debug("Twilio not configured; skip SMS to %s", to_e164)
        return None
    sid, token, from_num = _twilio_creds()
    try:
        from twilio.rest import Client

        client = Client(sid, token)
        msg = client.messages.create(to=to_e164, from_=from_num, body=body)
        return msg.sid
    except Exception:
        logger.exception("Twilio SMS failed to=%s", to_e164)
        return None


def sms_footer() -> str:
    return " Reply STOP to opt out."


def booking_confirmation_body(
    *,
    first_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
    provider_display: str,
) -> str:
    return (
        f"Relief Chiropractic: Hi {first_name}, your {service_name} visit is booked for "
        f"{appt_date_display} at {appt_time_display} with {provider_display}. "
        f"We'll send a reminder the day before."
        f"{sms_footer()}"
    )


def appointment_reminder_body(
    *,
    first_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
    provider_display: str,
) -> str:
    return (
        f"Reminder from Relief Chiropractic: {first_name}, tomorrow ({appt_date_display}) "
        f"you have {service_name} at {appt_time_display} with {provider_display}."
        f"{sms_footer()}"
    )


def provider_checkin_body(*, patient_name: str, time_display: str) -> str:
    return (
        f"Relief Chiropractic: {patient_name} checked in at the kiosk. Scheduled {time_display} today."
        f"{sms_footer()}"
    )


def provider_new_booking_body(
    *,
    patient_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
) -> str:
    return (
        f"Relief Chiropractic: New booking — {patient_name}, {service_name} on {appt_date_display} at {appt_time_display}."
        f"{sms_footer()}"
    )


def provider_schedule_change_body(
    *,
    patient_name: str,
    appt_date_display: str,
    appt_time_display: str,
    changes_text: str,
) -> str:
    return (
        f"Relief Chiropractic: Schedule update — {patient_name} on {appt_date_display} at {appt_time_display}. {changes_text}"
        f"{sms_footer()}"
    )


def provider_reassigned_away_body(
    *,
    patient_name: str,
    appt_date_display: str,
    appt_time_display: str,
) -> str:
    return (
        f"Relief Chiropractic: {patient_name} was moved to another provider (was {appt_date_display} {appt_time_display})."
        f"{sms_footer()}"
    )
