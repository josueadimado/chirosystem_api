"""
Public (web/voice) booking time windows.

Combines ClinicSettings.business_hours with fixed rules:
- Monday–Thursday: chiropractic 8:00 AM–6:00 PM, massage 9:00 AM–6:00 PM (visits must end by closing).
- Friday: same open times, close at 4:00 PM.
- Saturday & Sunday: no online booking.

The effective window is the intersection of clinic hours and these rules (narrower wins).
"""

from __future__ import annotations

import re
from datetime import date, time

from .models import ClinicSettings, Service


def _hard_policy_open_close_minutes(appt_date: date, service: Service) -> tuple[int, int] | None:
    """Fixed Mon–Fri policy in minutes from midnight; None if online booking closed that calendar day."""
    if appt_date.weekday() >= 5:
        return None
    is_friday = appt_date.weekday() == 4
    close_min = 16 * 60 if is_friday else 18 * 60
    if service.service_type == Service.ServiceType.CHIROPRACTIC:
        open_min = 8 * 60
    elif service.service_type == Service.ServiceType.MASSAGE:
        open_min = 9 * 60
    else:
        open_min = 8 * 60
    if open_min >= close_min:
        return None
    return open_min, close_min


def _clinic_minutes_for_date(appt_date: date) -> tuple[int, int] | None:
    """Business hours from ClinicSettings for that weekday. None if closed. Fallback 9–6 if not listed."""
    day_name = appt_date.strftime("%A")
    clinic = ClinicSettings.get_solo()
    bh_list = clinic.business_hours or []
    default = (9 * 60, 18 * 60)
    for entry in bh_list:
        if entry.get("day", "").lower() != day_name.lower():
            continue
        hours_str = entry.get("hours", "")
        if hours_str.lower() in ("closed", ""):
            return None
        parts = re.split(r"\s*[–—-]\s*", hours_str)
        if len(parts) != 2:
            return default
        start_min = end_min = None
        for i, part in enumerate(parts):
            t_match = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", part.strip(), re.I)
            if not t_match:
                return default
            h = int(t_match.group(1))
            m = int(t_match.group(2))
            ap = t_match.group(3).upper()
            if ap == "PM" and h != 12:
                h += 12
            if ap == "AM" and h == 12:
                h = 0
            if i == 0:
                start_min = h * 60 + m
            else:
                end_min = h * 60 + m
        if start_min is None or end_min is None or start_min >= end_min:
            return default
        return start_min, end_min
    return default


def effective_public_booking_window_minutes(appt_date: date, service: Service) -> tuple[int, int] | None:
    """
    Minutes [open, close) style: slots must satisfy start >= open and end <= close
    (same convention as availability: cursor + duration <= day_end).
    """
    policy = _hard_policy_open_close_minutes(appt_date, service)
    if policy is None:
        return None
    clinic = _clinic_minutes_for_date(appt_date)
    if clinic is None:
        return None
    c_open, c_close = clinic
    p_open, p_close = policy
    a = max(c_open, p_open)
    b = min(c_close, p_close)
    if a >= b:
        return None
    return a, b


def interval_outside_effective_public_window(appt_date: date, start: time, end: time, service: Service) -> bool:
    """True if [start, end] is not fully inside the effective public booking window."""
    w = effective_public_booking_window_minutes(appt_date, service)
    if w is None:
        return True
    w_open, w_close = w
    st = start.hour * 60 + start.minute
    et = end.hour * 60 + end.minute
    return st < w_open or et > w_close


PUBLIC_BOOKING_HOURS_BLURB = (
    "Online booking: Monday–Friday only (closed weekends). "
    "Chiropractic: 8:00 AM–6:00 PM; massage: 9:00 AM–6:00 PM; Friday we close at 4:00 PM."
)
