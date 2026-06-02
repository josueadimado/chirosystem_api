"""
Clinic-level Redis cache helpers.

All keys share the prefix  clinic:{CLINIC_ID}:  so they stay isolated if
multi-tenancy is ever added.  The single-tenant ID is 1 (ClinicSettings pk).

Usage
-----
    from apps.clinic.cache_utils import (
        get_clinic_settings_cached,
        invalidate_settings_cache,
        invalidate_booking_cache,
        invalidate_providers_cache,
        CACHE_KEY_BOOKING_OPTIONS,
        TTL_BOOKING,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from apps.clinic.models import ClinicSettings

# ── Identity ─────────────────────────────────────────────────────────────────

# Single-tenant: clinic is always pk=1. If multi-tenancy is added later,
# replace this constant with a per-request lookup and parameterise the helpers.
CLINIC_ID: int = 1


def _k(name: str) -> str:
    return f"clinic:{CLINIC_ID}:{name}"


# ── Cache keys ────────────────────────────────────────────────────────────────

CACHE_KEY_SETTINGS: str = _k("settings")
CACHE_KEY_BOOKING_OPTIONS: str = _k("booking_options")
CACHE_KEY_VOICE_CATALOG: str = _k("voice_catalog")
CACHE_KEY_PROVIDERS: str = _k("providers_list")
CACHE_KEY_INTAKE_SERVICES: str = _k("intake_services")

# ── TTLs (seconds) ────────────────────────────────────────────────────────────

TTL_SETTINGS: int = 180   # 3 minutes  — clinic name, address, fees
TTL_BOOKING: int = 300    # 5 minutes  — service/provider booking options
TTL_PROVIDERS: int = 300  # 5 minutes  — authenticated provider list


# ── ClinicSettings ────────────────────────────────────────────────────────────

def get_clinic_settings_cached() -> "ClinicSettings":
    """
    Return the singleton ClinicSettings row, served from cache when warm.

    The model instance is pickled by Django's cache framework (works with both
    django.core.cache.backends.redis.RedisCache and LocMemCache).
    """
    obj = cache.get(CACHE_KEY_SETTINGS)
    if obj is None:
        from apps.clinic.models import ClinicSettings
        obj = ClinicSettings.get_solo()
        cache.set(CACHE_KEY_SETTINGS, obj, TTL_SETTINGS)
    return obj


# ── Invalidation helpers (called from signals) ────────────────────────────────

def invalidate_settings_cache() -> None:
    cache.delete(CACHE_KEY_SETTINGS)


def invalidate_booking_cache() -> None:
    """Clear all booking-related caches (services changed or providers changed)."""
    cache.delete_many([
        CACHE_KEY_BOOKING_OPTIONS,
        CACHE_KEY_VOICE_CATALOG,
        CACHE_KEY_INTAKE_SERVICES,
    ])


def invalidate_providers_cache() -> None:
    """Clear authenticated provider list cache (separate from booking options)."""
    cache.delete(CACHE_KEY_PROVIDERS)
