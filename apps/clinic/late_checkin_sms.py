"""
SMS patients who have not checked in shortly after their appointment start time.

Default: 10 minutes after scheduled start, status still ``booked``, today only.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.clinic.clinic_time import aware_appointment_start, clinic_localdate, clinic_now
from apps.clinic.models import Appointment
from apps.clinic.utils import format_time_12h

logger = logging.getLogger(__name__)


def _late_checkin_enabled() -> bool:
    return bool(getattr(settings, "LATE_CHECKIN_SMS_ENABLED", True))


def _minutes_after_start() -> int:
    return max(1, int(getattr(settings, "LATE_CHECKIN_SMS_MINUTES_AFTER_START", 10)))


def _send_after_timedelta() -> timedelta:
    return timedelta(minutes=_minutes_after_start())


def appointment_past_late_checkin_threshold(appt: Appointment, *, now=None) -> bool:
    """True when clinic-local now is at or past start + configured delay."""
    now = now or clinic_now()
    start = aware_appointment_start(appt.appointment_date, appt.start_time)
    return now >= start + _send_after_timedelta()


def process_late_checkin_sms() -> dict:
    """
    Find today's booked appointments past the late threshold and send one SMS each.
    Safe to run repeatedly (idempotent via late_checkin_sms_at).
    """
    if not _late_checkin_enabled():
        return {"enabled": False, "sms_sent": 0, "skipped": 0, "errors": 0}

    from apps.clinic.patient_communication_prefs import patient_wants_reminder_sms
    from apps.clinic.twilio_sms import late_checkin_sms_body, send_sms, twilio_configured

    if not twilio_configured():
        return {"enabled": True, "twilio": False, "sms_sent": 0, "skipped": 0, "errors": 0}

    today = clinic_localdate()
    now = clinic_now()
    delay = _send_after_timedelta()
    minutes = _minutes_after_start()

    candidates = (
        Appointment.objects.filter(
            appointment_date=today,
            status=Appointment.Status.BOOKED,
            late_checkin_sms_at__isnull=True,
        )
        .select_related("patient", "provider", "booked_service")
        .order_by("start_time")
    )

    sms_sent = 0
    skipped = 0
    errors = 0

    for appt in candidates:
        if not appointment_past_late_checkin_threshold(appt, now=now):
            skipped += 1
            continue

        patient = appt.patient
        if not patient_wants_reminder_sms(patient):
            skipped += 1
            continue

        to_phone = (patient.phone or "").strip()
        if not to_phone:
            skipped += 1
            continue

        first = patient.first_name.strip() or "there"
        provider_display = str(appt.provider)
        time_disp = format_time_12h(appt.start_time)
        service_name = (
            appt.booked_service.label_for_public_booking() if appt.booked_service else "appointment"
        )

        body = late_checkin_sms_body(
            first_name=first,
            provider_display=provider_display,
            appt_time_display=time_disp,
            service_name=service_name,
        )

        try:
            sid = send_sms(to_e164=to_phone, body=body)
            if not sid:
                errors += 1
                continue
            updated = Appointment.objects.filter(
                pk=appt.pk,
                status=Appointment.Status.BOOKED,
                late_checkin_sms_at__isnull=True,
            ).update(late_checkin_sms_at=timezone.now())
            if updated:
                sms_sent += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Late check-in SMS failed for appointment_id=%s", appt.pk)
            errors += 1

    logger.info(
        "Late check-in SMS run: date=%s delay_min=%s sms_sent=%s skipped=%s errors=%s",
        today,
        minutes,
        sms_sent,
        skipped,
        errors,
    )
    return {
        "enabled": True,
        "twilio": True,
        "for_date": str(today),
        "minutes_after_start": minutes,
        "sms_sent": sms_sent,
        "skipped": skipped,
        "errors": errors,
    }
