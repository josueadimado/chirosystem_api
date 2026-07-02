"""Helpers for public online booking: which slots are blocked for a provider."""

from __future__ import annotations

from datetime import date, time

from .booking_provider_eligibility import provider_can_offer_service_online


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def provider_interval_blocked_online(
    provider_id: int,
    block_date,
    visit_start: time,
    visit_end: time,
    *,
    block_overlap_end: time | None = None,
) -> bool:
    """
    True if [visit_start, overlap_end) overlaps any ProviderUnavailability on that date.

    ``visit_end`` is the end of the slot on the calendar (for massage, includes post-visit buffer).
    For overlap with *booking blocks*, use ``block_overlap_end`` = treatment end only, so a block
    that starts at posted closing (e.g. 6:00 PM) does not reject a valid massage that *finishes*
    at closing while the calendar still holds turnover minutes after.
    """
    # Local import avoids circular imports at Django startup.
    from .models import ProviderUnavailability

    blocks = ProviderUnavailability.objects.filter(provider_id=provider_id, block_date=block_date)
    sm = _time_to_minutes(visit_start)
    em = _time_to_minutes(block_overlap_end if block_overlap_end is not None else visit_end)
    for b in blocks:
        if b.all_day:
            return True
        if b.start_time is None or b.end_time is None:
            continue
        bs = _time_to_minutes(b.start_time)
        be = _time_to_minutes(b.end_time)
        if sm < be and em > bs:
            return True
    return False


def provider_ids_for_public_booking(service, *, provider_id: int | None = None) -> list[int]:
    """
    Active provider ids eligible for public online booking of this service.
    When provider_id is set, returns only that id if eligible; otherwise all eligible providers.
    """
    from .models import Provider, Service

    if provider_id is not None:
        provider = Provider.objects.filter(pk=provider_id, active=True).first()
        if provider and provider_can_offer_service_online(provider, service):
            return [int(provider_id)]
        return []

    ids: list[int] = []
    for provider in Provider.objects.filter(active=True).order_by("id"):
        if provider.services.filter(pk=service.pk).exists() and provider_can_offer_service_online(
            provider, service
        ):
            ids.append(provider.id)
    if ids:
        return ids

    if (
        service.is_new_client_intake
        and service.service_type == Service.ServiceType.CHIROPRACTIC
    ):
        fallback = (
            Provider.objects.filter(active=True)
            .filter(
                services__service_type=Service.ServiceType.CHIROPRACTIC,
                services__is_active=True,
                services__show_in_public_booking=True,
            )
            .distinct()
            .order_by("id")
        )
        return [p.id for p in fallback if provider_can_offer_service_online(p, service)]

    return []


def _provider_taken_minutes(provider_id: int, appt_date: date) -> set[int]:
    from .models import Appointment

    taken: set[int] = set()
    for s, e in (
        Appointment.objects.filter(provider_id=provider_id, appointment_date=appt_date)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.COMPLETED,
            ]
        )
        .values_list("start_time", "end_time")
    ):
        for m in range(s.hour * 60 + s.minute, e.hour * 60 + e.minute):
            taken.add(m)
    return taken


def public_available_slot_times_for_provider(
    *,
    provider,
    service,
    appt_date: date,
) -> list[time]:
    """
    Bookable start times for one provider/service/day — same rules as
    GET /booking-options/availability/ (public online booking).
    """
    from datetime import time as time_cls

    from .online_booking_hours import (
        CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES,
        effective_public_booking_window_minutes,
        public_booking_last_slot_start_minute,
        public_booking_treatment_duration_minutes,
    )
    from .public_booking_service import public_online_booking_calendar_span_minutes

    win = effective_public_booking_window_minutes(appt_date, service)
    if not win:
        return []
    day_start, day_end = win
    required_span = public_online_booking_calendar_span_minutes(service)
    closing_compliance_span = public_booking_treatment_duration_minutes(service)
    last_slot_start = public_booking_last_slot_start_minute(appt_date, day_end)
    taken = _provider_taken_minutes(provider.pk, appt_date)

    available: list[time] = []
    cursor = day_start
    while cursor <= last_slot_start:
        h, m = divmod(cursor, 60)
        slot_start_time = time_cls(hour=h, minute=m)
        end_total = cursor + required_span
        eh, em = divmod(end_total, 60)
        slot_end_time = time_cls(hour=min(eh, 23), minute=em if eh < 24 else 59)
        treat_total = cursor + closing_compliance_span
        teh, tem = divmod(treat_total, 60)
        treat_end = time_cls(hour=min(teh, 23), minute=tem if teh < 24 else 59)
        if cursor + closing_compliance_span <= day_end and not any(
            cursor <= t < cursor + required_span for t in taken
        ):
            if not provider_interval_blocked_online(
                provider.pk,
                appt_date,
                slot_start_time,
                slot_end_time,
                block_overlap_end=treat_end,
            ):
                available.append(slot_start_time)
        cursor += CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES
    return available


def public_available_slot_times_for_service(
    *,
    service_id: int,
    appt_date: date,
    provider_id: int | None = None,
) -> list[time]:
    """Union of bookable starts across eligible providers (deduped, sorted)."""
    from .models import Provider, Service

    service = Service.objects.filter(
        pk=service_id, is_active=True, show_in_public_booking=True
    ).first()
    if not service:
        return []

    provider_ids = provider_ids_for_public_booking(service, provider_id=provider_id)
    if not provider_ids:
        return []

    seen: set[time] = set()
    merged: list[time] = []
    for pid in provider_ids:
        provider = Provider.objects.filter(pk=pid, active=True).first()
        if not provider:
            continue
        for slot in public_available_slot_times_for_provider(
            provider=provider, service=service, appt_date=appt_date
        ):
            if slot not in seen:
                seen.add(slot)
                merged.append(slot)
    merged.sort()
    return merged


def format_slot_time_label(slot_time: time) -> str:
    return slot_time.strftime("%I:%M %p").lstrip("0")
