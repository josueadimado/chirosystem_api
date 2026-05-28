"""Suggest catalog diagnosis codes from the patient's most recent prior visit."""

from __future__ import annotations

from django.db.models import Count, Q

from .models import Appointment, DiagnosisCode, Visit
from .visit_diagnosis import diagnosis_ids_from_visit


def _prior_appointments_q(appointment: Appointment) -> Q:
    return Q(appointment_date__lt=appointment.appointment_date) | Q(
        appointment_date=appointment.appointment_date,
        start_time__lt=appointment.start_time,
    )


def _active_catalog_ids_ordered(raw_ids: list[int]) -> list[int]:
    if not raw_ids:
        return []
    active = set(
        DiagnosisCode.objects.filter(pk__in=raw_ids, is_active=True).values_list("pk", flat=True)
    )
    return [pk for pk in raw_ids if pk in active]


def _latest_prior_visit_with_diagnoses(appointment: Appointment) -> Visit | None:
    prior_appt_ids = (
        Appointment.objects.filter(patient_id=appointment.patient_id)
        .filter(_prior_appointments_q(appointment))
        .exclude(pk=appointment.pk)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
            ],
        )
        .values_list("pk", flat=True)
    )
    return (
        Visit.objects.filter(appointment_id__in=prior_appt_ids)
        .annotate(
            diag_count=Count(
                "visit_diagnoses",
                filter=Q(visit_diagnoses__diagnosis_id__isnull=False),
            )
        )
        .filter(diag_count__gt=0)
        .select_related("appointment", "appointment__booked_service")
        .prefetch_related("visit_diagnoses")
        .order_by("-appointment__appointment_date", "-appointment__start_time")
        .first()
    )


def _prior_visit_meta(visit: Visit) -> dict[str, str]:
    appt = visit.appointment
    service_name = appt.booked_service.name if appt.booked_service else ""
    return {
        "appointment_date": appt.appointment_date.isoformat(),
        "start_time": appt.start_time.strftime("%I:%M %p").lstrip("0"),
        "service_name": service_name,
    }


def consultation_diagnosis_prefill_for_appointment(appointment: Appointment) -> dict:
    """
    Diagnosis IDs to show checked when opening consultation.

    Uses this visit's saved catalog rows when present; otherwise copies the most
    recent prior visit that had catalog diagnoses (still active in the clinic list).
    """
    try:
        visit = appointment.visit
    except Visit.DoesNotExist:
        visit = None

    if visit:
        current = _active_catalog_ids_ordered(diagnosis_ids_from_visit(visit))
        if current:
            return {
                "diagnosis_ids": current,
                "prefilled_from_prior": False,
                "prior_visit": None,
            }

    prior_visit = _latest_prior_visit_with_diagnoses(appointment)
    if not prior_visit:
        return {
            "diagnosis_ids": [],
            "prefilled_from_prior": False,
            "prior_visit": None,
        }

    raw_ids = diagnosis_ids_from_visit(prior_visit)
    active_ids = _active_catalog_ids_ordered(raw_ids)
    if not active_ids:
        return {
            "diagnosis_ids": [],
            "prefilled_from_prior": False,
            "prior_visit": None,
        }

    return {
        "diagnosis_ids": active_ids,
        "prefilled_from_prior": True,
        "prior_visit": _prior_visit_meta(prior_visit),
    }
