"""Clinic wall-clock helpers — always use CLINIC_TIMEZONE, not the developer's location."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def clinic_tz() -> ZoneInfo:
    tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit") or "America/Detroit"
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return ZoneInfo("America/Detroit")


def clinic_localdate() -> date:
    """Today's calendar date at the clinic (e.g. Michigan), not UTC."""
    return timezone.now().astimezone(clinic_tz()).date()


def clinic_now() -> datetime:
    return timezone.now().astimezone(clinic_tz())


def aware_appointment_start(appt_date: date, start_time: time) -> datetime:
    """Naive date + time from scheduling UI = clinic local wall clock."""
    return datetime.combine(appt_date, start_time, tzinfo=clinic_tz())


def slot_start_is_in_past(appt_date: date, start_time: time) -> bool:
    """True when the slot start is already at or before clinic-local now."""
    if appt_date > clinic_localdate():
        return False
    if appt_date < clinic_localdate():
        return True
    return aware_appointment_start(appt_date, start_time) <= clinic_now()
