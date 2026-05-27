"""Clinic wall-clock helpers — timezone from DB (admin Settings) with env fallback."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from apps.clinic.timezone_utils import get_clinic_timezone, now_clinic, today_clinic


def clinic_tz() -> ZoneInfo:
    return get_clinic_timezone()


def clinic_localdate() -> date:
    """Today's calendar date at the clinic, not UTC."""
    return today_clinic()


def clinic_now() -> datetime:
    return now_clinic()


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
