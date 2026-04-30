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
        msg_svc = (getattr(settings, "TWILIO_MESSAGING_SERVICE_SID", None) or "").strip()
    except Exception:
        sid = token = from_num = msg_svc = ""
    return sid, token, from_num, msg_svc


def twilio_configured() -> bool:
    sid, token, from_num, msg_svc = _twilio_creds()
    return bool(sid and token and (from_num or msg_svc))


def send_sms_detailed(*, to_e164: str, body: str) -> tuple[str | None, str | None]:
    """
    Send an SMS via Twilio. Returns (message_sid, error_message).
    On success, error_message is None. On failure or skip, message_sid is None.
    """
    if not twilio_configured():
        logger.debug("Twilio not configured; skip SMS to %s", to_e164)
        return None, (
            "Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "and either TWILIO_PHONE_NUMBER or TWILIO_MESSAGING_SERVICE_SID."
        )
    sid, token, from_num, msg_svc = _twilio_creds()
    try:
        from twilio.rest import Client

        client = Client(sid, token)
        params = {"to": to_e164, "body": body}
        if msg_svc:
            params["messaging_service_sid"] = msg_svc
        else:
            params["from_"] = from_num
        msg = client.messages.create(**params)
        logger.info("SMS sent: sid=%s to=%s status=%s", msg.sid, to_e164, msg.status)
        return msg.sid, None
    except Exception as e:
        logger.exception("Twilio SMS failed to=%s", to_e164)
        return None, str(e)


def send_sms(*, to_e164: str, body: str) -> str | None:
    """
    Send an SMS via Twilio. Uses Messaging Service SID if configured
    (required for 10DLC compliance), otherwise falls back to raw phone number.
    Returns Message SID on success, None on skip/failure.
    """
    sid, _err = send_sms_detailed(to_e164=to_e164, body=body)
    return sid


def sms_footer() -> str:
    return " Reply STOP to opt out."


def booking_confirmation_body(
    *,
    first_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
    provider_display: str,
    estimated_payment: str = "",
) -> str:
    pay = f" Est. {estimated_payment} due at visit." if estimated_payment else ""
    return (
        f"Relief Chiropractic: Hi {first_name}, your {service_name} is confirmed for "
        f"{appt_date_display} at {appt_time_display}.{pay} "
        f"We'll text and email reminders the day before and shortly before your visit.{sms_footer()}"
    )


def appointment_reminder_body(
    *,
    first_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
    provider_display: str,
    estimated_payment: str = "",
) -> str:
    pay = f" Est. {estimated_payment} due at visit." if estimated_payment else ""
    return (
        f"Relief Chiropractic: Hi {first_name}, reminder — your {service_name} is "
        f"tomorrow ({appt_date_display}) at {appt_time_display}.{pay}{sms_footer()}"
    )


def same_day_reminder_body(
    *,
    first_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
    provider_display: str,
    estimated_payment: str = "",
) -> str:
    pay = f" Est. {estimated_payment} due at visit." if estimated_payment else ""
    return (
        f"Relief Chiropractic: Hi {first_name}, your {service_name} is today at {appt_time_display} "
        f"({appt_date_display}).{pay} See you soon.{sms_footer()}"
    )


def provider_checkin_body(*, patient_name: str, time_display: str) -> str:
    return (
        f"Relief Chiropractic: {patient_name} completed check-in (scheduled {time_display}).{sms_footer()}"
    )


def provider_new_booking_body(
    *,
    patient_name: str,
    service_name: str,
    appt_date_display: str,
    appt_time_display: str,
) -> str:
    return (
        f"Relief Chiropractic: New booking — {patient_name}, {service_name} "
        f"on {appt_date_display} at {appt_time_display}.{sms_footer()}"
    )


def provider_schedule_change_body(
    *,
    patient_name: str,
    appt_date_display: str,
    appt_time_display: str,
    changes_text: str,
) -> str:
    return (
        f"Relief Chiropractic: Update — {patient_name} on {appt_date_display} "
        f"at {appt_time_display}. {changes_text}{sms_footer()}"
    )


def provider_reassigned_away_body(
    *,
    patient_name: str,
    appt_date_display: str,
    appt_time_display: str,
) -> str:
    return (
        f"Relief Chiropractic: {patient_name} was moved to another provider "
        f"(was {appt_date_display} {appt_time_display}).{sms_footer()}"
    )
