"""Clinic timezone from DB (admin Settings), with env fallback."""

from __future__ import annotations

import datetime
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo


def get_clinic_tz_name() -> str:
    """IANA timezone name: ClinicSettings DB → CLINIC_TIMEZONE env → Detroit."""
    try:
        from apps.clinic.models import ClinicSettings

        clinic = ClinicSettings.get_cached()
        if clinic.timezone:
            return clinic.timezone
    except Exception:
        pass
    return getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")


def get_clinic_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(get_clinic_tz_name())
    except Exception:
        return ZoneInfo("America/Detroit")


def now_clinic() -> datetime.datetime:
    """Current datetime in clinic timezone."""
    return timezone.now().astimezone(get_clinic_timezone())


def today_clinic() -> datetime.date:
    """Today's date in clinic timezone."""
    return now_clinic().date()


def clinic_tz_name() -> str:
    """Alias for get_clinic_tz_name (backward compatible)."""
    return get_clinic_tz_name()


def filter_past_slot_times_for_date(
    slot_times: list[datetime.time],
    appt_date: datetime.date,
    *,
    buffer_minutes: int = 30,
) -> list[datetime.time]:
    """Drop slots before now+buffer when booking for today (clinic local time)."""
    if appt_date != today_clinic():
        return slot_times
    cutoff_time = (now_clinic() + timedelta(minutes=buffer_minutes)).time()
    return [s for s in slot_times if s >= cutoff_time]


def is_past_slot_for_clinic_today(
    slot_time: datetime.time,
    appt_date: datetime.date,
    *,
    buffer_minutes: int = 30,
) -> bool:
    """True if this start time is too soon to book for today (clinic local clock)."""
    if appt_date != today_clinic():
        return False
    cutoff_time = (now_clinic() + timedelta(minutes=buffer_minutes)).time()
    return slot_time < cutoff_time


def _timezone_display_label(tz: str) -> str:
    return tz.replace("_", " ")


def get_all_timezones() -> dict[str, list[dict[str, str]]]:
    """
    All valid IANA timezone names grouped by region (first path segment).
    """
    import zoneinfo

    grouped: dict[str, list[dict[str, str]]] = {}
    for tz in sorted(zoneinfo.available_timezones()):
        parts = tz.split("/")
        region = parts[0] if len(parts) > 1 else "Other"
        grouped.setdefault(region, []).append(
            {"value": tz, "label": _timezone_display_label(tz)}
        )

    for region in grouped:
        grouped[region].sort(key=lambda x: x["label"])

    return grouped


def is_valid_iana_timezone(tz_name: str) -> bool:
    """True if tz_name is a valid IANA timezone identifier."""
    import zoneinfo

    name = (tz_name or "").strip()
    if not name:
        return False
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        return False
