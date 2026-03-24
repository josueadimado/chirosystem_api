"""Provider + role flags for clinic team (doctor / owner / staff) API."""

from django.db import transaction

from apps.clinic.models import Provider, Service

from .models import User


def apply_role_flags(user: User, role: str) -> None:
    """Set Django admin flags from clinic role (owner admins can use /admin)."""
    if role == User.Roles.OWNER_ADMIN:
        user.is_staff = True
        user.is_superuser = True
    else:
        user.is_staff = False
        user.is_superuser = False


def ensure_provider_for_doctor(user: User) -> Provider:
    """Create or refresh Provider for a doctor and attach active bookable services."""
    provider, _ = Provider.objects.update_or_create(
        user=user,
        defaults={
            "title": "Chiropractor",
            "specialty": "",
            "active": True,
        },
    )
    services = Service.objects.filter(is_active=True)
    provider.services.set(services)
    return provider


def set_provider_inactive(user: User) -> None:
    Provider.objects.filter(user=user).update(active=False)
