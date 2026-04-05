"""Celery tasks: SMS, email, Google Calendar sync for bookings."""

from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.clinic.utils import format_time_12h

logger = logging.getLogger(__name__)


@shared_task
def send_booking_confirmation_sms_task(appointment_id: int) -> str:
    """Sent right after online booking commits (async)."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import booking_confirmation_body, send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"

    to = (appt.patient.phone or "").strip()
    if not to:
        return "no_phone"

    service_name = appt.booked_service.name if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = booking_confirmation_body(
        first_name=appt.patient.first_name.strip() or "there",
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
    )
    sid = send_sms(to_e164=to, body=body)
    logger.info("Booking SMS result: appt=%s to=%s sid=%s", appointment_id, to, sid)
    return sid or "send_failed"


@shared_task
def send_booking_confirmation_email_task(appointment_id: int) -> str:
    """Send a booking confirmation email right after booking commits."""
    from django.conf import settings as django_settings
    from django.core.mail import send_mail

    from apps.clinic.models import Appointment

    if not (getattr(django_settings, "EMAIL_HOST", "") or "").strip():
        return "email_not_configured"

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"

    email = (appt.patient.email or "").strip()
    if not email:
        return "no_email"

    first_name = appt.patient.first_name.strip() or "there"
    service_name = appt.booked_service.name if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)

    subject = f"Booking Confirmed — {service_name} on {date_disp}"
    body = (
        f"Hi {first_name},\n\n"
        f"Your appointment at Relief Chiropractic has been confirmed!\n\n"
        f"  Service: {service_name}\n"
        f"  Date: {date_disp}\n"
        f"  Time: {time_disp}\n\n"
        f"We'll send you a reminder the day before your visit.\n\n"
        f"If you need to reschedule or cancel, please call us or visit our website.\n\n"
        f"Thank you for choosing Relief Chiropractic!\n"
        f"— Relief Chiropractic Team"
    )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=django_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        logger.info("Booking email sent: appt=%s to=%s", appointment_id, email)
        return "sent"
    except Exception:
        logger.exception("Booking email failed: appt=%s to=%s", appointment_id, email)
        return "send_failed"


@shared_task
def send_daily_appointment_reminders() -> dict:
    """
    Run once per day (Celery Beat). Sends SMS for appointments happening *tomorrow*
    in CLINIC_TIMEZONE, for booked/confirmed visits that have not been reminded yet.
    """
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import appointment_reminder_body, send_sms, twilio_configured

    if not twilio_configured():
        logger.info("Twilio not configured; skip daily reminders")
        return {"sent": 0, "skipped": "twilio_disabled"}

    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    tz = ZoneInfo(tz_name)
    today_local = timezone.now().astimezone(tz).date()
    tomorrow = today_local + timedelta(days=1)

    candidates = (
        Appointment.objects.filter(
            appointment_date=tomorrow,
            sms_reminder_sent_at__isnull=True,
            status__in=[Appointment.Status.BOOKED, Appointment.Status.CONFIRMED],
        )
        .select_related("patient", "provider", "booked_service")
        .order_by("start_time")
    )

    sent = 0
    for appt in candidates:
        to = (appt.patient.phone or "").strip()
        if not to:
            continue
        service_name = appt.booked_service.name if appt.booked_service else "appointment"
        date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
        time_disp = format_time_12h(appt.start_time)
        body = appointment_reminder_body(
            first_name=appt.patient.first_name.strip() or "there",
            service_name=service_name,
            appt_date_display=date_disp,
            appt_time_display=time_disp,
            provider_display=str(appt.provider),
        )
        sid = send_sms(to_e164=to, body=body)
        if sid:
            Appointment.objects.filter(pk=appt.pk, sms_reminder_sent_at__isnull=True).update(
                sms_reminder_sent_at=timezone.now()
            )
            sent += 1

    logger.info("Daily SMS reminders: sent=%s for date=%s", sent, tomorrow)
    return {"sent": sent, "for_date": str(tomorrow)}


@shared_task
def sync_appointment_google_calendar_task(appointment_id: int) -> str:
    """Create/update/delete event on the provider's connected Google Calendar."""
    from apps.clinic.google_calendar_sync import sync_appointment_to_google
    from apps.clinic.models import Appointment

    appt = (
        Appointment.objects.select_related("provider", "patient", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    return str(sync_appointment_to_google(appt))


def _send_provider_alert(*, provider, body: str) -> str:
    """Send SMS to provider.notification_phone if Twilio is on and number is set."""
    from apps.clinic.twilio_sms import send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"
    to = (getattr(provider, "notification_phone", None) or "").strip()
    if not to:
        return "no_notification_phone"
    sid = send_sms(to_e164=to, body=body)
    return sid or "send_failed"


@shared_task
def notify_provider_patient_checked_in_task(appointment_id: int) -> str:
    """SMS the appointment’s provider when the patient checks in at the kiosk."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import provider_checkin_body

    appt = (
        Appointment.objects.select_related("patient", "provider")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip()
    time_disp = format_time_12h(appt.start_time)
    body = provider_checkin_body(patient_name=patient_name or "Patient", time_display=time_disp)
    return _send_provider_alert(provider=appt.provider, body=body)


@shared_task
def notify_provider_new_booking_task(appointment_id: int) -> str:
    """SMS provider when a new appointment is created (public book or admin)."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import provider_new_booking_body

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip()
    service_name = appt.booked_service.name if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = provider_new_booking_body(
        patient_name=patient_name or "Patient",
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
    )
    return _send_provider_alert(provider=appt.provider, body=body)


@shared_task
def notify_provider_schedule_change_task(
    appointment_id: int,
    change_lines: list[str],
    old_provider_id: int | None = None,
    old_date_iso: str | None = None,
    old_time_iso: str | None = None,
) -> dict:
    """
    SMS provider(s) after staff updates an appointment.
    If the doctor changed, the previous provider gets a short “reassigned” text.
    """
    from datetime import date as date_type
    from datetime import time as time_type

    from apps.clinic.models import Appointment, Provider
    from apps.clinic.twilio_sms import provider_reassigned_away_body, provider_schedule_change_body

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return {"error": "appointment_missing"}

    out: dict = {"current_provider": None, "old_provider": None}
    patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip() or "Patient"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)

    if (
        old_provider_id
        and old_provider_id != appt.provider_id
        and old_date_iso
        and old_time_iso
    ):
        prev = Provider.objects.filter(pk=old_provider_id).first()
        if prev:
            try:
                od = date_type.fromisoformat(old_date_iso)
                ot = time_type.fromisoformat(old_time_iso)
            except ValueError:
                od, ot = appt.appointment_date, appt.start_time
            away_body = provider_reassigned_away_body(
                patient_name=patient_name,
                appt_date_display=od.strftime("%a %b %d, %Y"),
                appt_time_display=format_time_12h(ot),
            )
            out["old_provider"] = _send_provider_alert(provider=prev, body=away_body)

    if change_lines:
        changes_text = " ".join(change_lines)
        body = provider_schedule_change_body(
            patient_name=patient_name,
            appt_date_display=date_disp,
            appt_time_display=time_disp,
            changes_text=changes_text,
        )
        out["current_provider"] = _send_provider_alert(provider=appt.provider, body=body)

    return out
