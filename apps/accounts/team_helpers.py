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


def ensure_provider_for_doctor(
    user: User,
    *,
    primary_service_type: str | None = None,
) -> Provider:
    """
    Create or refresh Provider for a doctor and attach bookable services for that category
    (chiropractic vs massage online visit types).
    """
    pst = primary_service_type or Service.ServiceType.CHIROPRACTIC
    title = "Chiropractor" if pst == Service.ServiceType.CHIROPRACTIC else "Massage therapist"
    phone = (user.phone or "").strip()[:20]
    defaults = {
        "title": title,
        "specialty": "",
        "active": True,
        "primary_service_type": pst,
    }
    if phone:
        defaults["notification_phone"] = phone
    provider, _ = Provider.objects.update_or_create(user=user, defaults=defaults)
    services = Service.objects.filter(is_active=True, service_type=pst)
    provider.services.set(services)
    return provider


def set_provider_inactive(user: User) -> None:
    Provider.objects.filter(user=user).update(active=False)
