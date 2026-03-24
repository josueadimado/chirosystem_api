"""Helpers for public online booking: which slots are blocked for a provider."""

from __future__ import annotations

from datetime import time


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def provider_interval_blocked_online(
    provider_id: int,
    block_date,
    visit_start: time,
    visit_end: time,
) -> bool:
    """
    True if [visit_start, visit_end) overlaps any ProviderUnavailability on that date.
    Used by public availability + book; staff can still create appointments via admin API if needed.
    """
    # Local import avoids circular imports at Django startup.
    from .models import ProviderUnavailability

    blocks = ProviderUnavailability.objects.filter(provider_id=provider_id, block_date=block_date)
    sm = _time_to_minutes(visit_start)
    em = _time_to_minutes(visit_end)
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
