"""
Cache-invalidation signals for clinic lookup data.

Whenever a ClinicSettings, Service, or Provider row is saved or deleted,
the corresponding Redis cache keys are cleared so the next request fetches
fresh data from the database.

These signals are connected inside ClinicConfig.ready() (apps.py).
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.clinic.cache_utils import (
    invalidate_booking_cache,
    invalidate_providers_cache,
    invalidate_settings_cache,
)

logger = logging.getLogger(__name__)


# ── ClinicSettings ────────────────────────────────────────────────────────────

@receiver(post_save, sender="clinic.ClinicSettings")
def _on_clinic_settings_saved(sender, instance, **kwargs):
    """Clear the settings cache whenever an admin saves the clinic profile."""
    invalidate_settings_cache()
    logger.debug("Cache invalidated: clinic settings (pk=%s)", instance.pk)


# ── Service ───────────────────────────────────────────────────────────────────

@receiver(post_save, sender="clinic.Service")
def _on_service_saved(sender, instance, **kwargs):
    """Clear booking-options and intake-services caches on any service change."""
    invalidate_booking_cache()
    logger.debug("Cache invalidated: booking options (service pk=%s saved)", instance.pk)


@receiver(post_delete, sender="clinic.Service")
def _on_service_deleted(sender, instance, **kwargs):
    invalidate_booking_cache()
    logger.debug("Cache invalidated: booking options (service pk=%s deleted)", instance.pk)


# ── Provider ──────────────────────────────────────────────────────────────────

@receiver(post_save, sender="clinic.Provider")
def _on_provider_saved(sender, instance, **kwargs):
    """Clear both the public booking options and the authenticated provider list."""
    invalidate_booking_cache()
    invalidate_providers_cache()
    logger.debug("Cache invalidated: booking options + provider list (provider pk=%s saved)", instance.pk)


@receiver(post_delete, sender="clinic.Provider")
def _on_provider_deleted(sender, instance, **kwargs):
    invalidate_booking_cache()
    invalidate_providers_cache()
    logger.debug("Cache invalidated: booking options + provider list (provider pk=%s deleted)", instance.pk)
