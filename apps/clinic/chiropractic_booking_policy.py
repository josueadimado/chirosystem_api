"""Rules for chiropractic online booking (e.g. long-inactive returning patients)."""

from __future__ import annotations

import os
from datetime import date

from django.utils import timezone

from .models import Appointment, Patient, Service

_DEFAULT_GAP_DAYS = 730  # ~2 years


def chiro_returning_gap_days() -> int:
    raw = (os.environ.get("CHIRO_RETURNING_GAP_DAYS") or "").strip()
    if raw.isdigit():
        return max(30, int(raw))
    return _DEFAULT_GAP_DAYS


def last_completed_chiropractic_visit_date(patient: Patient) -> date | None:
    """Most recent completed chiropractic appointment (by visit date)."""
    d = (
        Appointment.objects.filter(
            patient=patient,
            status=Appointment.Status.COMPLETED,
            booked_service__service_type=Service.ServiceType.CHIROPRACTIC,
        )
        .order_by("-appointment_date", "-start_time")
        .values_list("appointment_date", flat=True)
        .first()
    )
    return d


def public_new_client_intake_services():
    return Service.objects.filter(
        is_active=True,
        show_in_public_booking=True,
        service_type=Service.ServiceType.CHIROPRACTIC,
        is_new_client_intake=True,
    ).order_by("name")


def chiropractic_booking_must_use_intake(patient: Patient, service: Service) -> str | None:
    """
    If the patient must book a new-client intake visit instead of this service, return an error message.
    Otherwise return None.
    """
    if service.service_type != Service.ServiceType.CHIROPRACTIC:
        return None
    if service.is_new_client_intake:
        return None
    last = last_completed_chiropractic_visit_date(patient)
    if last is None:
        return None
    gap = chiro_returning_gap_days()
    if (timezone.localdate() - last).days <= gap:
        return None
    intake_qs = public_new_client_intake_services()
    if not intake_qs.exists():
        return None
    names = ", ".join(intake_qs.values_list("name", flat=True)[:8])
    years = round(gap / 365.25, 1)
    return (
        f"It has been more than {years:g} years since your last completed chiropractic visit with us. "
        f"Please schedule a new patient or reactivation visit first ({names}). "
        "After that visit, you can book regular follow-up appointments online."
    )


def chiropractic_intake_context_for_patient(patient: Patient | None) -> dict:
    """Fields merged into public patient-lookup JSON for booking UI."""
    if not patient:
        return {
            "chiropractic_returning_gap_requires_intake": False,
            "chiropractic_intake_services": [],
            "last_chiropractic_visit_date": None,
            "chiropractic_gap_days": chiro_returning_gap_days(),
        }
    last = last_completed_chiropractic_visit_date(patient)
    intake_qs = public_new_client_intake_services()
    intake_list = [{"id": s.id, "name": s.name} for s in intake_qs]
    gap = chiro_returning_gap_days()
    lapsed = last is not None and (timezone.localdate() - last).days > gap
    requires = bool(lapsed and intake_list)
    return {
        "chiropractic_returning_gap_requires_intake": requires,
        "chiropractic_intake_services": intake_list,
        "last_chiropractic_visit_date": str(last) if last else None,
        "chiropractic_gap_days": gap,
    }
