"""
Which providers may appear for a given public bookable Service, and when a provider may take a booking.

Intake / new-office chiropractic visits are often omitted from each doctor's online M2M list; we fall back to
the same doctor pool as other bookable chiropractic services so scheduling still works.
"""

from __future__ import annotations

from .models import Provider, Service


def provider_can_offer_service_online(provider: Provider, service: Service) -> bool:
    """True if this provider may book this service through public online booking."""
    if provider.services.filter(pk=service.pk).exists():
        return True
    # Legacy: placeholder providers with no visit types linked (e.g. some voice flows)
    if not provider.services.exists():
        return True
    if (
        service.is_new_client_intake
        and service.service_type == Service.ServiceType.CHIROPRACTIC
        and provider.services.filter(
            service_type=Service.ServiceType.CHIROPRACTIC,
            show_in_public_booking=True,
        ).exists()
    ):
        return True
    return False


def apply_intake_chiropractic_provider_fallback(
    bookable_services: list[Service],
    providers_by_service: dict[int, list[dict]],
) -> None:
    """
    For chiropractic intake services with an empty provider M2M, fill providers_by_service using other
    bookable chiro doctors, then any provider linked to a public chiro service, then active chiropractic staff.
    Mutates providers_by_service in place.
    """
    for svc in bookable_services:
        if providers_by_service.get(svc.id):
            continue
        if svc.service_type != Service.ServiceType.CHIROPRACTIC or not svc.is_new_client_intake:
            continue
        seen: dict[int, Provider] = {}
        for other in bookable_services:
            if other.pk == svc.pk or other.service_type != Service.ServiceType.CHIROPRACTIC:
                continue
            for p in other.providers.all():
                if p.active:
                    seen[p.id] = p
        if not seen:
            qs = (
                Provider.objects.filter(active=True)
                .filter(services__service_type=Service.ServiceType.CHIROPRACTIC)
                .filter(services__is_active=True, services__show_in_public_booking=True)
                .distinct()
            )
            for p in qs:
                seen[p.id] = p
        if not seen:
            for p in Provider.objects.filter(active=True, primary_service_type="chiropractic"):
                seen[p.id] = p
        providers_by_service[svc.id] = [
            {"id": p.id, "provider_name": str(p)}
            for p in sorted(seen.values(), key=lambda x: x.id)
        ]
