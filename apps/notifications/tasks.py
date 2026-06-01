"""Celery tasks: SMS, email, Google Calendar sync for bookings."""

from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.clinic.utils import format_time_12h, format_usd_plain

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool((getattr(settings, "EMAIL_HOST", None) or "").strip())


def _clinic_tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit"))


def _send_reminder_email(*, to_email: str, subject: str, body: str) -> bool:
    from django.conf import settings as django_settings
    from django.core.mail import send_mail

    if not _smtp_configured():
        return False
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=django_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Reminder email failed to=%s", to_email)
        return False


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

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_sms

    if not patient_wants_booking_sms(patient):
        return "patient_pref_no_sms"

    to = (patient.phone or "").strip()
    if not to:
        return "no_phone"

    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    est_pay = format_usd_plain(appt.booked_service.price) if appt.booked_service else ""
    body = booking_confirmation_body(
        first_name=patient.first_name.strip() or "there",
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
        estimated_payment=est_pay,
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

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_email

    if not patient_wants_booking_email(patient):
        return "patient_pref_no_email"

    email = (patient.email or "").strip()
    if not email:
        return "no_email"

    first_name = patient.first_name.strip() or "there"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    est_pay = format_usd_plain(appt.booked_service.price) if appt.booked_service else ""
    est_block = ""
    if est_pay:
        est_block = (
            f"  Expected amount at time of visit: {est_pay}\n"
            f"    (Estimate for this booked service; add-on services may change your final balance.)\n\n"
        )

    subject = f"Booking Confirmed — {service_name} on {date_disp}"
    body = (
        f"Hi {first_name},\n\n"
        f"Your appointment at Relief Chiropractic has been confirmed!\n\n"
        f"  Service: {service_name}\n"
        f"  Date: {date_disp}\n"
        f"  Time: {time_disp}\n"
        f"{est_block}"
        f"If you need to reschedule or cancel, please call us or visit our website.\n\n"
        f"We'll send appointment reminders using the contact preferences saved on your chart.\n\n"
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
    Celery Beat (default daily 9:00 in CLINIC_TIMEZONE): day-before **SMS and email**
    for appointments happening *tomorrow*. SMS only if the patient opted in (`sms_consent`).
    Email requires SMTP configured + patient email. Each channel tracked separately on the appointment.
    """
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import appointment_reminder_body, send_sms, twilio_configured

    tz = _clinic_tz()
    today_local = timezone.now().astimezone(tz).date()
    tomorrow = today_local + timedelta(days=1)

    candidates = (
        Appointment.objects.filter(
            appointment_date=tomorrow,
            status=Appointment.Status.BOOKED,
        )
        .select_related("patient", "provider", "booked_service")
        .order_by("start_time")
    )

    from apps.clinic.patient_communication_prefs import (
        patient_wants_reminder_email,
        patient_wants_reminder_sms,
    )

    sms_sent = 0
    email_sent = 0
    twilio_on = twilio_configured()
    smtp_on = _smtp_configured()

    for appt in candidates:
        patient = appt.patient
        service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
        date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
        time_disp = format_time_12h(appt.start_time)
        est_pay = format_usd_plain(appt.booked_service.price) if appt.booked_service else ""
        provider_display = str(appt.provider)
        first = patient.first_name.strip() or "there"

        if twilio_on and patient_wants_reminder_sms(patient) and appt.day_before_reminder_sms_at is None:
            to_phone = (patient.phone or "").strip()
            if to_phone:
                body = appointment_reminder_body(
                    first_name=first,
                    service_name=service_name,
                    appt_date_display=date_disp,
                    appt_time_display=time_disp,
                    provider_display=provider_display,
                    estimated_payment=est_pay,
                )
                sid = send_sms(to_e164=to_phone, body=body)
                if sid:
                    updated = Appointment.objects.filter(
                        pk=appt.pk, day_before_reminder_sms_at__isnull=True
                    ).update(day_before_reminder_sms_at=timezone.now())
                    if updated:
                        sms_sent += 1

        if smtp_on and patient_wants_reminder_email(patient) and appt.day_before_reminder_email_at is None:
            em = (patient.email or "").strip()
            if em:
                subject = f"Reminder — {service_name} tomorrow at {time_disp}"
                date_long = appt.appointment_date.strftime("%A, %B %d, %Y")
                pay_block = (
                    f"\n  Estimated amount at visit: {est_pay}\n"
                    if est_pay
                    else ""
                )
                msg = (
                    f"Hi {first},\n\n"
                    f"This is a reminder from Relief Chiropractic about your visit tomorrow.\n\n"
                    f"  Service: {service_name}\n"
                    f"  Date: {date_long}\n"
                    f"  Time: {time_disp}\n"
                    f"  Provider: {provider_display}\n"
                    f"{pay_block}\n"
                    f"If you need to reschedule or cancel, please call us or use our website.\n\n"
                    f"— Relief Chiropractic"
                )
                if _send_reminder_email(to_email=em, subject=subject, body=msg):
                    updated = Appointment.objects.filter(
                        pk=appt.pk, day_before_reminder_email_at__isnull=True
                    ).update(day_before_reminder_email_at=timezone.now())
                    if updated:
                        email_sent += 1

    logger.info(
        "Day-before reminders for %s: sms_sent=%s email_sent=%s twilio=%s smtp=%s",
        tomorrow,
        sms_sent,
        email_sent,
        twilio_on,
        smtp_on,
    )
    return {
        "for_date": str(tomorrow),
        "sms_sent": sms_sent,
        "email_sent": email_sent,
        "twilio": twilio_on,
        "smtp": smtp_on,
    }


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
    # Staff SMS: use internal service name so the schedule matches the EMR (e.g. "Miscellaneous").
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


@shared_task
def send_patient_cancel_confirmation_sms_task(appointment_id: int, require_sms_consent: bool = True) -> str:
    """Patient-facing SMS after cancel. Public flow requires sms_consent; staff portal does not."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import patient_cancel_confirmation_sms_body, send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    if appt.status != Appointment.Status.CANCELLED:
        return "not_cancelled"

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_sms

    if not patient_wants_booking_sms(patient):
        return "patient_pref_no_sms"
    if require_sms_consent and not patient.sms_consent:
        return "no_sms_consent"

    to = (patient.phone or "").strip()
    if not to:
        return "no_phone"

    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = patient_cancel_confirmation_sms_body(
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
    )
    sid = send_sms(to_e164=to, body=body)
    logger.info("Patient cancel SMS: appt=%s to=%s sid=%s", appointment_id, to, sid)
    return sid or "send_failed"


@shared_task
def send_patient_cancel_confirmation_email_task(appointment_id: int) -> str:
    """Patient-facing email after public online cancel."""
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
    if appt.status != Appointment.Status.CANCELLED:
        return "not_cancelled"

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_email

    if not patient_wants_booking_email(patient):
        return "patient_pref_no_email"

    email = (patient.email or "").strip()
    if not email:
        return "no_email"

    first_name = patient.first_name.strip() or "there"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    provider_name = str(appt.provider)

    subject = "Appointment Cancelled — Relief Chiropractic"
    body = (
        f"Hi {first_name},\n\n"
        f"Your {service_name} appointment on {date_disp} at {time_disp} with {provider_name} has been cancelled.\n\n"
        f"Questions? Call us at +1 (269) 408-0303.\n\n"
        f"If you'd like to rebook, visit book.reliefchiropractic.net\n\n"
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
        logger.info("Patient cancel email sent: appt=%s to=%s", appointment_id, email)
        return "sent"
    except Exception:
        logger.exception("Patient cancel email failed: appt=%s to=%s", appointment_id, email)
        return "send_failed"


@shared_task
def send_patient_reschedule_confirmation_sms_task(appointment_id: int, require_sms_consent: bool = True) -> str:
    """Patient-facing SMS after reschedule. Public flow requires sms_consent; staff portal does not."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import patient_reschedule_confirmation_sms_body, send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"

    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    if appt.status != Appointment.Status.BOOKED:
        return "not_booked"

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_sms

    if not patient_wants_booking_sms(patient):
        return "patient_pref_no_sms"
    if require_sms_consent and not patient.sms_consent:
        return "no_sms_consent"

    to = (patient.phone or "").strip()
    if not to:
        return "no_phone"

    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = patient_reschedule_confirmation_sms_body(
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
    )
    sid = send_sms(to_e164=to, body=body)
    logger.info("Patient reschedule SMS: appt=%s to=%s sid=%s", appointment_id, to, sid)
    return sid or "send_failed"


@shared_task
def send_patient_reschedule_confirmation_email_task(appointment_id: int) -> str:
    """Patient-facing email after public online reschedule."""
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
    if appt.status != Appointment.Status.BOOKED:
        return "not_booked"

    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_email

    if not patient_wants_booking_email(patient):
        return "patient_pref_no_email"

    email = (patient.email or "").strip()
    if not email:
        return "no_email"

    first_name = patient.first_name.strip() or "there"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    provider_name = str(appt.provider)

    subject = "Appointment Rescheduled — Relief Chiropractic"
    body = (
        f"Hi {first_name},\n\n"
        f"Your {service_name} appointment has been moved to {date_disp} at {time_disp} with {provider_name}.\n\n"
        f"Questions? Call us at +1 (269) 408-0303.\n\n"
        f"If you need to make further changes, visit book.reliefchiropractic.net\n\n"
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
        logger.info("Patient reschedule email sent: appt=%s to=%s", appointment_id, email)
        return "sent"
    except Exception:
        logger.exception("Patient reschedule email failed: appt=%s to=%s", appointment_id, email)
        return "send_failed"


@shared_task
def send_provider_dashboard_reschedule_patient_sms_task(appointment_id: int) -> str:
    """Doctor/staff rescheduled from dashboard — transactional SMS (sms_consent + phone)."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import provider_dashboard_reschedule_patient_sms_body, send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    p = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_sms

    if not patient_wants_booking_sms(p):
        return "patient_pref_no_sms"
    to = (p.phone or "").strip()
    if not to:
        return "no_phone"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = provider_dashboard_reschedule_patient_sms_body(
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
    )
    sid = send_sms(to_e164=to, body=body)
    logger.info("Provider dashboard reschedule SMS: appt=%s to=%s sid=%s", appointment_id, to, sid)
    return sid or "send_failed"


@shared_task
def send_provider_dashboard_reschedule_patient_email_task(appointment_id: int) -> str:
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
    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_email

    if not patient_wants_booking_email(patient):
        return "patient_pref_no_email"
    email = (patient.email or "").strip()
    if not email:
        return "no_email"
    first = patient.first_name.strip() or "there"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    prov = str(appt.provider)
    subject = "Appointment Rescheduled — Relief Chiropractic"
    body = (
        f"Hi {first},\n\n"
        f"Your {service_name} appointment has been moved to {date_disp} at {time_disp} with {prov}.\n\n"
        f"Questions? Call us at +1 (269) 408-0303.\n\n"
        f"If you need to make further changes, visit book.reliefchiropractic.net\n\n"
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
        logger.info("Provider dashboard reschedule email: appt=%s to=%s", appointment_id, email)
        return "sent"
    except Exception:
        logger.exception("Provider dashboard reschedule email failed: appt=%s", appointment_id)
        return "send_failed"


@shared_task
def send_provider_dashboard_book_next_patient_sms_task(appointment_id: int) -> str:
    """Same as public booking confirmation SMS path: phone on file, no extra consent check."""
    from apps.clinic.models import Appointment
    from apps.clinic.twilio_sms import provider_dashboard_book_next_patient_sms_body, send_sms, twilio_configured

    if not twilio_configured():
        return "twilio_disabled"
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return "appointment_missing"
    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_sms

    if not patient_wants_booking_sms(patient):
        return "patient_pref_no_sms"
    to = (patient.phone or "").strip()
    if not to:
        return "no_phone"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    body = provider_dashboard_book_next_patient_sms_body(
        service_name=service_name,
        appt_date_display=date_disp,
        appt_time_display=time_disp,
        provider_display=str(appt.provider),
    )
    sid = send_sms(to_e164=to, body=body)
    logger.info("Provider dashboard book-next SMS: appt=%s to=%s sid=%s", appointment_id, to, sid)
    return sid or "send_failed"


@shared_task
def send_provider_dashboard_book_next_patient_email_task(appointment_id: int) -> str:
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
    patient = appt.patient
    from apps.clinic.patient_communication_prefs import patient_wants_booking_email

    if not patient_wants_booking_email(patient):
        return "patient_pref_no_email"
    email = (patient.email or "").strip()
    if not email:
        return "no_email"
    first = patient.first_name.strip() or "there"
    service_name = appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    prov = str(appt.provider)
    subject = "Appointment Booked — Relief Chiropractic"
    body = (
        f"Hi {first},\n\n"
        f"Your next {service_name} appointment has been booked for {date_disp} at {time_disp} with {prov}.\n\n"
        f"Questions? Call us at +1 (269) 408-0303.\n\n"
        f"If you need to make changes, visit book.reliefchiropractic.net\n\n"
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
        logger.info("Provider dashboard book-next email: appt=%s to=%s", appointment_id, email)
        return "sent"
    except Exception:
        logger.exception("Provider dashboard book-next email failed: appt=%s", appointment_id)
        return "send_failed"


@shared_task
def process_auto_no_show_appointments_task() -> dict:
    """
    Celery Beat (every 15 minutes): mark booked/checked-in visits as no-show when
    start time + grace (default 60 min) has passed, apply fee billing, notify patient.
    """
    from apps.clinic.auto_no_show import process_auto_no_show_appointments

    return process_auto_no_show_appointments()
