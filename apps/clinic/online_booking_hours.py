"""
Public (web/voice) booking time windows.

Combines ClinicSettings.business_hours with fixed rules:
- Monday–Thursday: chiropractic 8:00 AM–6:00 PM, massage 9:00 AM–6:00 PM (the *visit* must end by closing;
  massage turnover buffer after the visit does not cut off the last bookable start time).
- Friday: both services 7:00 AM–4:00 PM (visit end by 4:00 PM). Online window opens at policy time even if
  legacy clinic JSON still lists a later Friday start — update Settings → business hours to match signage.
- Saturday & Sunday: no online booking.

The effective window is the intersection of clinic hours and these rules for close; Friday open follows policy.
"""

from __future__ import annotations

import re
from datetime import date, time

from .models import ClinicSettings, Service

# Chiropractic: one shared public schedule grid — offered start times every N minutes (e.g. 8:00, 8:15, 8:30).
# Each visit still occupies its full duration_minutes on the calendar (45 min = three 15-min cells).
CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES = 15


def _hard_policy_open_close_minutes(appt_date: date, service: Service) -> tuple[int, int] | None:
    """Fixed Mon–Fri policy in minutes from midnight; None if online booking closed that calendar day."""
    if appt_date.weekday() >= 5:
        return None
    is_friday = appt_date.weekday() == 4
    close_min = 16 * 60 if is_friday else 18 * 60
    if is_friday:
        # Both chiropractic and massage: first bookable slot 7:00 AM; last visit must end by 4:00 PM.
        open_min = 7 * 60
    elif service.service_type == Service.ServiceType.CHIROPRACTIC:
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
    Minutes from midnight [open, close): last *patient visit* may end at ``close`` (exclusive of minute ``close``
    is not required — we use ``start + duration_minutes <= close`` for slot generation and validation).

    Friday uses policy open time (7:00) even if ``business_hours`` still lists a later start, so online booking
    matches the clinic’s stated Friday schedule; close is still intersected with clinic hours.
    """
    policy = _hard_policy_open_close_minutes(appt_date, service)
    if policy is None:
        return None
    clinic = _clinic_minutes_for_date(appt_date)
    if clinic is None:
        return None
    c_open, c_close = clinic
    p_open, p_close = policy
    if appt_date.weekday() == 4:
        # Friday: policy defines first bookable minute (7:00 both lines); do not let stale clinic JSON delay it.
        a = p_open
    else:
        a = max(c_open, p_open)
    b = min(c_close, p_close)
    if a >= b:
        return None
    return a, b


def public_booking_treatment_duration_minutes(service: Service) -> int:
    """Patient-facing visit length (no post-massage buffer) — used for closing-time compliance."""
    try:
        n = int(service.duration_minutes)
    except (TypeError, ValueError):
        n = 30
    return max(5, n)


def interval_outside_effective_public_window(appt_date: date, start: time, end: time, service: Service) -> bool:
    """True if the visit start is outside the window or the *treatment* (not buffer) ends after closing."""
    _ = end  # closing rule uses duration_minutes only; calendar span may include massage tail
    w = effective_public_booking_window_minutes(appt_date, service)
    if w is None:
        return True
    w_open, w_close = w
    st = start.hour * 60 + start.minute
    treatment_end = st + public_booking_treatment_duration_minutes(service)
    return st < w_open or treatment_end > w_close


PUBLIC_BOOKING_HOURS_BLURB = (
    "Online booking: Monday–Friday only (closed weekends). "
    "Chiropractic: 8:00 AM–6:00 PM Mon–Thu; massage: 9:00 AM–6:00 PM Mon–Thu; "
    "Friday both lines 7:00 AM–4:00 PM. Last bookable time is when your visit can finish by closing."
)
