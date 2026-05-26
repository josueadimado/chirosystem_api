"""Provider dashboard: reschedule and book-next using the same slot rules as public online booking."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from apps.clinic.booking_availability import provider_interval_blocked_online
from apps.clinic.clinic_time import clinic_localdate, slot_start_is_in_past
from apps.clinic.booking_provider_eligibility import provider_can_offer_service_online
from apps.clinic.models import Appointment, Patient, Provider, Service
from apps.clinic.online_booking_hours import (
    CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES,
    PUBLIC_BOOKING_HOURS_BLURB,
    effective_desk_booking_window_minutes,
    interval_outside_effective_public_window,
    public_booking_treatment_duration_minutes,
)
from apps.clinic.public_booking_service import public_online_booking_calendar_span_minutes

DESK_BOOKING_HOURS_BLURB = (
    "Desk booking uses the same open hours as online booking, but visits may extend past "
    "public closing (through 9:00 PM) when scheduling extra time for a patient."
)


def user_may_manage_appointment(user, appointment: Appointment) -> bool:
    role = getattr(user, "role", None)
    if role in ("owner_admin", "staff"):
        return True
    if role == "doctor":
        prov = Provider.objects.filter(user=user).first()
        return bool(prov and prov.id == appointment.provider_id)
    return False


def user_may_book_as_provider(user, provider: Provider) -> bool:
    """Doctors may only create bookings on their own calendar; owner/staff may pick any provider."""
    role = getattr(user, "role", None)
    if role in ("owner_admin", "staff"):
        return True
    if role == "doctor":
        prov = Provider.objects.filter(user=user).first()
        return bool(prov and prov.id == provider.id)
    return False


def parse_appointment_date(date_str: str):
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_start_time_value(raw: str):
    """
    Accept HH:MM, HH:MM:SS, or a public-booking slot label like '9:30 AM'.
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", s):
        parts = s.split(":")
        h = int(parts[0])
        m = int(parts[1])
        sec = int(parts[2]) if len(parts) > 2 else 0
        return datetime(2000, 1, 1, h % 24, min(m, 59), min(sec, 59)).time()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", s, re.I)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    ap = m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return datetime(2000, 1, 1, h, mi, 0).time()


def validate_slot_for_online_booking_rules(
    *,
    provider: Provider,
    service: Service,
    appt_date,
    start_time,
    exclude_appointment_id: int | None,
) -> tuple[str | None, bool]:
    """
    Returns (error_message, is_conflict).
    error_message is None when the slot is valid.
    """
    today = clinic_localdate()
    if appt_date < today:
        return "Pick today or a future date.", False

    if slot_start_is_in_past(appt_date, start_time):
        return "Pick a time later today that has not passed yet.", False

    start_dt = datetime.combine(appt_date, start_time)
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timedelta(minutes=span)
    st_t = start_dt.time()
    en_t = end_dt.time()
    treat_end_dt = start_dt + timedelta(minutes=public_booking_treatment_duration_minutes(service))
    treatment_end_t = treat_end_dt.time()

    if interval_outside_effective_public_window(appt_date, st_t, en_t, service):
        return PUBLIC_BOOKING_HOURS_BLURB, False

    if provider_interval_blocked_online(
        provider.pk, appt_date, st_t, en_t, block_overlap_end=treatment_end_t
    ):
        return "That time is not open for online booking with this provider. Please pick another slot.", False

    overlapping = Appointment.objects.filter(
        provider=provider,
        appointment_date=appt_date,
        start_time__lt=en_t,
        end_time__gt=st_t,
    ).exclude(
        status__in=[
            Appointment.Status.CANCELLED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.COMPLETED,
        ]
    )
    if exclude_appointment_id:
        overlapping = overlapping.exclude(pk=exclude_appointment_id)

    if overlapping.exists():
        return "That time slot is no longer available. Please choose another time.", True

    return None, False


def validate_slot_for_desk_booking_rules(
    *,
    provider: Provider,
    service: Service,
    appt_date,
    start_time,
    exclude_appointment_id: int | None,
) -> tuple[str | None, bool]:
    """
    Front desk / provider dashboard: same conflict and block rules as public booking,
    but allows the calendar block to extend past public online closing (through desk close).
    """
    today = clinic_localdate()
    if appt_date < today:
        return "Pick today or a future date.", False

    if slot_start_is_in_past(appt_date, start_time):
        return "Pick a time later today that has not passed yet.", False

    start_dt = datetime.combine(appt_date, start_time)
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timedelta(minutes=span)
    st_t = start_dt.time()
    en_t = end_dt.time()
    treat_end_dt = start_dt + timedelta(minutes=public_booking_treatment_duration_minutes(service))
    treatment_end_t = treat_end_dt.time()

    desk_win = effective_desk_booking_window_minutes(appt_date, service)
    if desk_win is None:
        return DESK_BOOKING_HOURS_BLURB, False
    desk_open, desk_close = desk_win
    st_min = start_time.hour * 60 + start_time.minute
    if st_min < desk_open or st_min + span > desk_close:
        return DESK_BOOKING_HOURS_BLURB, False

    if provider_interval_blocked_online(
        provider.pk, appt_date, st_t, en_t, block_overlap_end=treatment_end_t
    ):
        return "That time is not open for booking with this provider. Please pick another slot.", False

    overlapping = Appointment.objects.filter(
        provider=provider,
        appointment_date=appt_date,
        start_time__lt=en_t,
        end_time__gt=st_t,
    ).exclude(
        status__in=[
            Appointment.Status.CANCELLED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.COMPLETED,
        ]
    )
    if exclude_appointment_id:
        overlapping = overlapping.exclude(pk=exclude_appointment_id)

    if overlapping.exists():
        return "That time slot is no longer available. Please choose another time.", True

    return None, False


def validate_appointment_duration_span_for_desk(
    *,
    provider: Provider,
    service: Service | None,
    appt_date,
    start_time,
    end_time,
    exclude_appointment_id: int | None = None,
) -> tuple[str | None, bool]:
    """
    Front desk: extend or shorten a visit on the calendar (start/end span).
    Uses desk closing hours, provider blocks, and double-booking rules.
    """
    start_m = start_time.hour * 60 + start_time.minute
    end_m = end_time.hour * 60 + end_time.minute
    if end_m <= start_m:
        return "End time must be after the start time.", False

    duration = end_m - start_m
    step = CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES
    if duration < step:
        return f"A visit must be at least {step} minutes on the calendar.", False
    if duration > 240:
        return "A visit cannot be longer than 4 hours on the calendar.", False

    if service:
        desk_win = effective_desk_booking_window_minutes(appt_date, service)
        if desk_win is None:
            return DESK_BOOKING_HOURS_BLURB, False
        if end_m > desk_win[1]:
            return "End time is past staff booking hours for this day.", False

    if provider_interval_blocked_online(
        provider.pk, appt_date, start_time, end_time, block_overlap_end=end_time
    ):
        return "That time overlaps a blocked period on the provider schedule.", False

    overlapping = Appointment.objects.filter(
        provider=provider,
        appointment_date=appt_date,
        start_time__lt=end_time,
        end_time__gt=start_time,
    ).exclude(
        status__in=[
            Appointment.Status.CANCELLED,
            Appointment.Status.NO_SHOW,
            Appointment.Status.COMPLETED,
        ]
    )
    if exclude_appointment_id:
        overlapping = overlapping.exclude(pk=exclude_appointment_id)

    if overlapping.exists():
        return "That time slot is no longer available. Please choose another time.", True

    return None, False


def compute_end_time_for_slot(appt_date, start_time, service: Service):
    start_dt = datetime.combine(appt_date, start_time)
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timedelta(minutes=span)
    return end_dt.time()
