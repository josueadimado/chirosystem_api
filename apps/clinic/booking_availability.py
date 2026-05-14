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
