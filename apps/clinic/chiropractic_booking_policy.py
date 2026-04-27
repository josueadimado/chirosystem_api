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
    If the patient must book a new-client / new office visit instead of this service, return an error message.
    Otherwise return None.

    Applies when: (1) they have no completed chiropractic visit on file yet, or (2) last chiro was longer ago than
    the configured gap. Massage and intake visit types are unaffected.
    """
    if service.service_type != Service.ServiceType.CHIROPRACTIC:
        return None
    if service.is_new_client_intake:
        return None
    if patient.online_chiro_intake_waived:
        return None
    intake_qs = public_new_client_intake_services()
    if not intake_qs.exists():
        return None
    names = ", ".join(intake_qs.values_list("name", flat=True)[:8])

    last = last_completed_chiropractic_visit_date(patient)
    if last is None:
        return (
            f"Your first chiropractic visit at our office must be scheduled as a new patient or new office visit "
            f"({names}). Please select one of those appointment types."
        )

    gap = chiro_returning_gap_days()
    if (timezone.localdate() - last).days <= gap:
        return None
    years = round(gap / 365.25, 1)
    return (
        f"It has been more than {years:g} years since your last completed chiropractic visit with us. "
        f"You need to book a first-time-style office visit first — choose a new patient, new office, or reactivation visit ({names}) — "
        "so we can re-establish you in care. After that, regular chiropractic visits can be booked online again."
    )


def chiropractic_intake_context_for_new_phone_lookup() -> dict:
    """Patient-lookup JSON when the phone number is not in the system yet (treat as new to the practice)."""
    intake_qs = public_new_client_intake_services()
    intake_list = [{"id": s.id, "name": s.name} for s in intake_qs]
    gap = chiro_returning_gap_days()
    new_requires = bool(intake_list)
    return {
        "chiropractic_returning_gap_requires_intake": False,
        "chiropractic_first_chiro_requires_intake": False,
        "chiropractic_new_patient_requires_intake": new_requires,
        "chiropractic_intake_services": intake_list,
        "last_chiropractic_visit_date": None,
        "chiropractic_gap_days": gap,
        "online_chiro_intake_waived": False,
    }


def chiropractic_intake_context_for_patient(patient: Patient) -> dict:
    """Fields merged into public patient-lookup JSON for booking UI (existing patient)."""
    last = last_completed_chiropractic_visit_date(patient)
    intake_qs = public_new_client_intake_services()
    intake_list = [{"id": s.id, "name": s.name} for s in intake_qs]
    gap = chiro_returning_gap_days()
    if patient.online_chiro_intake_waived:
        return {
            "chiropractic_returning_gap_requires_intake": False,
            "chiropractic_first_chiro_requires_intake": False,
            "chiropractic_new_patient_requires_intake": False,
            "chiropractic_intake_services": intake_list,
            "last_chiropractic_visit_date": str(last) if last else None,
            "chiropractic_gap_days": gap,
            "online_chiro_intake_waived": True,
        }
    lapsed = last is not None and (timezone.localdate() - last).days > gap
    gap_requires = bool(lapsed and intake_list)
    first_requires = bool(last is None and intake_list)
    return {
        "chiropractic_returning_gap_requires_intake": gap_requires,
        "chiropractic_first_chiro_requires_intake": first_requires,
        "chiropractic_new_patient_requires_intake": False,
        "chiropractic_intake_services": intake_list,
        "last_chiropractic_visit_date": str(last) if last else None,
        "chiropractic_gap_days": gap,
        "online_chiro_intake_waived": False,
    }
