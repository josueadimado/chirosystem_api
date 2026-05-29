"""Sort order for doctor dashboard day list (checked-in first, past visits last)."""

from __future__ import annotations

from django.utils import timezone

from .models import Appointment

ACTIVE_STATUS_ORDER = {
    Appointment.Status.CHECKED_IN: 0,
    Appointment.Status.IN_CONSULTATION: 1,
    Appointment.Status.AWAITING_PAYMENT: 2,
}

TERMINAL_STATUSES = frozenset(
    {
        Appointment.Status.COMPLETED,
        Appointment.Status.CANCELLED,
        Appointment.Status.NO_SHOW,
    }
)


def _minutes_since_midnight(t) -> int:
    return t.hour * 60 + t.minute


def _is_past_slot(appt: Appointment, appt_date, clinic_today, now_minutes: int) -> bool:
    if appt.status in TERMINAL_STATUSES:
        return True
    if appt_date < clinic_today:
        return True
    if appt_date > clinic_today:
        return False
    return _minutes_since_midnight(appt.end_time) <= now_minutes


def _sort_tier(appt: Appointment, appt_date, clinic_today, now_minutes: int) -> int:
    if appt.status in ACTIVE_STATUS_ORDER:
        return 0
    if appt.status == Appointment.Status.BOOKED and not _is_past_slot(appt, appt_date, clinic_today, now_minutes):
        return 1
    return 2


def sort_doctor_dashboard_appointments(
    appts: list[Appointment],
    *,
    appt_date,
    clinic_today=None,
) -> list[Appointment]:
    """In-place sort; returns same list for chaining."""
    clinic_today = clinic_today or timezone.localdate()
    now = timezone.localtime()
    now_minutes = now.hour * 60 + now.minute

    def sort_key(appt: Appointment):
        tier = _sort_tier(appt, appt_date, clinic_today, now_minutes)
        status_sub = ACTIVE_STATUS_ORDER.get(appt.status, 9)
        start_min = _minutes_since_midnight(appt.start_time)
        if tier == 2:
            return (tier, status_sub, -start_min)
        if tier == 1:
            in_window = (
                not _is_past_slot(appt, appt_date, clinic_today, now_minutes)
                and start_min <= now_minutes
            )
            return (tier, 0 if in_window else 1, start_min)
        return (tier, status_sub, start_min)

    appts.sort(key=sort_key)
    return appts
