"""Computed and stored patient demographics for chart / directory views."""

from __future__ import annotations

from django.db.models import Min
from django.utils import timezone

from .models import Appointment, Patient, Visit

_APPT_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
)


def _patient_age_years(date_of_birth) -> int | None:
    if not date_of_birth:
        return None
    today = timezone.localdate()
    years = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return max(0, years)


def patient_demographics_summary(patient: Patient) -> dict:
    """
    Extra demographics for patient_detail responses.
    date_established = first non-cancelled appointment date.
    last_seen = most recent completed visit only (never a future booking).
    """
    appt_qs = Appointment.objects.filter(patient=patient).exclude(status__in=_APPT_EXCLUDED)
    first_date = appt_qs.aggregate(d=Min("appointment_date"))["d"]

    now = timezone.now()
    last_visit = (
        Visit.objects.filter(
            patient=patient,
            status=Visit.Status.COMPLETED,
            completed_at__isnull=False,
            completed_at__lte=now,
        )
        .order_by("-completed_at")
        .values_list("completed_at", flat=True)
        .first()
    )

    last_seen = None
    if last_visit is not None:
        last_seen = timezone.localtime(last_visit).date().isoformat()

    return {
        "marital_status": (patient.marital_status or "").strip(),
        "age": _patient_age_years(patient.date_of_birth),
        "date_established": str(first_date) if first_date else None,
        "last_seen": last_seen,
    }
