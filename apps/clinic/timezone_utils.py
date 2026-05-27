"""Clinic timezone from DB (admin Settings), with env fallback."""

from __future__ import annotations

import datetime

from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo


def get_clinic_timezone() -> ZoneInfo:
    """
    Get clinic timezone from database first, fall back to settings,
    then fall back to America/Detroit.
    """
    try:
        from apps.clinic.models import ClinicSettings

        clinic = ClinicSettings.get_solo()
        tz_name = clinic.timezone or getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    except Exception:
        tz_name = getattr(settings, "CLINIC_TIMEZONE", "America/Detroit")
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return ZoneInfo("America/Detroit")


def now_clinic() -> datetime.datetime:
    """Current datetime in clinic timezone."""
    return timezone.now().astimezone(get_clinic_timezone())


def today_clinic() -> datetime.date:
    """Today's date in clinic timezone."""
    return now_clinic().date()


def clinic_tz_name() -> str:
    """Clinic timezone name as string."""
    return str(get_clinic_timezone())


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
