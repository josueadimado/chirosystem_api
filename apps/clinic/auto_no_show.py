"""
Automatically mark unattended visits as no-show after a grace period (default 60 minutes).

Mirrors staff no-show billing: fee from service price (or clinic fallback), card charge when possible,
then patient notice via notify_bills preferences (SMS and/or email).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.clinic.clinic_time import aware_appointment_start, clinic_now
from apps.clinic.models import Appointment, ClinicSettings, Invoice
from apps.clinic.no_show_billing import apply_no_show_fee_for_appointment, compute_no_show_fee_for_appointment
from apps.clinic.no_show_patient_notice import send_no_show_patient_notice

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset(
    {
        Appointment.Status.BOOKED,
        Appointment.Status.CHECKED_IN,
    }
)


def _grace_timedelta() -> timedelta:
    solo = ClinicSettings.get_cached()
    minutes = max(15, int(solo.auto_no_show_grace_minutes or 60))
    return timedelta(minutes=minutes)


def appointment_start_passed_grace(appt: Appointment, *, now=None) -> bool:
    """True when clinic-local now is at or past start + grace."""
    now = now or clinic_now()
    start = aware_appointment_start(appt.appointment_date, appt.start_time)
    return now >= start + _grace_timedelta()


def auto_no_show_deadline(appt: Appointment):
    """Clinic-local datetime when auto no-show would fire (start + grace)."""
    return aware_appointment_start(appt.appointment_date, appt.start_time) + _grace_timedelta()


def auto_no_show_countdown_for_appointment(appt: Appointment, *, now=None) -> dict | None:
    """
    Countdown for staff UI: minutes left before automatic no-show, or None if not applicable today.
    """
    from apps.clinic.clinic_time import clinic_localdate
    from apps.clinic.utils import format_time_12h

    solo = ClinicSettings.get_cached()
    grace_min = int(_grace_timedelta().total_seconds() // 60)
    now = now or clinic_now()

    base = {
        "enabled": bool(solo.auto_no_show_enabled),
        "grace_minutes": grace_min,
        "exempt": bool(appt.auto_no_show_exempt),
    }

    if not solo.auto_no_show_enabled:
        return {**base, "applies": False, "minutes_remaining": None, "past_deadline": False}

    if appt.auto_no_show_exempt:
        return {**base, "applies": False, "minutes_remaining": None, "past_deadline": False}

    if appt.auto_no_show_processed_at is not None:
        return None

    if appt.status not in _ACTIVE_STATUSES:
        return None

    if appt.appointment_date != clinic_localdate():
        return None

    deadline = auto_no_show_deadline(appt)
    if now >= deadline:
        return {
            **base,
            "applies": True,
            "minutes_remaining": 0,
            "past_deadline": True,
            "deadline_display": format_time_12h(deadline.time()),
        }

    remaining = max(0, int((deadline - now).total_seconds() // 60))
    return {
        **base,
        "applies": True,
        "minutes_remaining": remaining,
        "past_deadline": False,
        "deadline_display": format_time_12h(deadline.time()),
    }


def process_auto_no_show_appointments() -> dict:
    """
    Find eligible appointments and mark each as no-show once.
    Safe to run repeatedly (idempotent via auto_no_show_processed_at).
    """
    solo = ClinicSettings.get_cached()
    if not solo.auto_no_show_enabled:
        return {"enabled": False, "processed": 0, "skipped": 0, "errors": 0}

    now = clinic_now()
    grace = _grace_timedelta()
    earliest_date = (now - grace - timedelta(days=1)).date()

    candidates = (
        Appointment.objects.filter(
            status__in=_ACTIVE_STATUSES,
            auto_no_show_processed_at__isnull=True,
            appointment_date__gte=earliest_date,
        )
        .select_related("patient", "provider", "booked_service")
        .order_by("appointment_date", "start_time")
    )

    processed = 0
    skipped = 0
    errors = 0
    sms_sent = 0
    email_sent = 0

    for appt in candidates:
        if not appointment_start_passed_grace(appt, now=now):
            skipped += 1
            continue
        try:
            stats = _process_one_appointment(appt.pk)
            if stats.get("status") == "processed":
                processed += 1
                sms_sent += int(stats.get("sms_sent") or 0)
                email_sent += int(stats.get("email_sent") or 0)
            elif stats.get("status") == "skipped":
                skipped += 1
            else:
                errors += 1
        except Exception:
            logger.exception("Auto no-show failed for appointment_id=%s", appt.pk)
            errors += 1

    retry_stats = _retry_pending_no_show_notices(now=now)
    sms_sent += int(retry_stats.get("sms_sent") or 0)
    email_sent += int(retry_stats.get("email_sent") or 0)

    logger.info(
        "Auto no-show run: processed=%s skipped=%s errors=%s sms=%s email=%s grace_min=%s",
        processed,
        skipped,
        errors,
        sms_sent,
        email_sent,
        int(grace.total_seconds() // 60),
    )
    return {
        "enabled": True,
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "sms_sent": sms_sent,
        "email_sent": email_sent,
        "grace_minutes": int(grace.total_seconds() // 60),
        "notice_retries": retry_stats.get("retried", 0),
    }


def _retry_pending_no_show_notices(*, now=None) -> dict:
    """Re-send billing notices that failed after a no-show was recorded (last 7 days)."""
    now = now or clinic_now()
    since = (now - timedelta(days=7)).date()
    pending = (
        Appointment.objects.filter(
            status=Appointment.Status.NO_SHOW,
            appointment_date__gte=since,
        )
        .filter(
            Q(no_show_notice_sms_at__isnull=True) | Q(no_show_notice_email_at__isnull=True)
        )
        .select_related("patient", "provider", "booked_service")
    )
    retried = 0
    sms_sent = 0
    email_sent = 0
    for appt in pending:
        inv = (
            Invoice.objects.filter(
                appointment=appt,
                kind=Invoice.Kind.NO_SHOW_FEE,
            )
            .order_by("-created_at")
            .first()
        )
        card_charged = bool(inv and inv.paid_at)
        stats = send_no_show_patient_notice(appt, invoice=inv, card_charged=card_charged)
        if stats.get("sms_sent") or stats.get("email_sent"):
            retried += 1
            sms_sent += int(stats.get("sms_sent") or 0)
            email_sent += int(stats.get("email_sent") or 0)
    return {"retried": retried, "sms_sent": sms_sent, "email_sent": email_sent}


def _process_one_appointment(appointment_id: int) -> dict:
    card_charged = False
    invoice = None
    fee_amt = Decimal("0")

    with transaction.atomic():
        locked = (
            Appointment.objects.select_for_update()
            .select_related("patient", "provider", "booked_service")
            .get(pk=appointment_id)
        )

        if locked.auto_no_show_processed_at is not None:
            return {"status": "skipped", "reason": "already_processed"}
        if locked.status not in _ACTIVE_STATUSES:
            return {"status": "skipped", "reason": "status_changed"}
        if locked.auto_no_show_exempt:
            return {"status": "skipped", "reason": "exempt"}

        if not appointment_start_passed_grace(locked):
            return {"status": "skipped", "reason": "grace_not_elapsed"}

        fee_amt = compute_no_show_fee_for_appointment(locked)
        if fee_amt > 0:
            ctx = apply_no_show_fee_for_appointment(locked, fee_amt)
            card_charged = bool(ctx.get("already_charged"))
            invoice = (
                Invoice.objects.filter(appointment=locked)
                .order_by("-created_at")
                .first()
            )

        locked.status = Appointment.Status.NO_SHOW
        locked.checked_in_at = None
        locked.consultation_started_at = None
        locked.auto_no_show_processed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "checked_in_at",
                "consultation_started_at",
                "auto_no_show_processed_at",
                "updated_at",
            ]
        )

    try:
        from apps.notifications.tasks import sync_appointment_google_calendar_task

        sync_appointment_google_calendar_task.delay(appointment_id)
    except Exception:
        logger.exception("Could not queue calendar sync after auto no-show appt=%s", appointment_id)

    locked = Appointment.objects.select_related("patient", "provider", "booked_service").get(
        pk=appointment_id
    )
    notice = send_no_show_patient_notice(
        locked,
        invoice=invoice,
        card_charged=card_charged,
    )
    return {
        "status": "processed",
        "appointment_id": appointment_id,
        "card_charged": card_charged,
        "fee": str(fee_amt),
        **notice,
    }
