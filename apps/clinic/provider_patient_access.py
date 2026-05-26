"""Chiropractic vs massage: which patients a doctor may edit vs view read-only."""

from __future__ import annotations

from django.db.models import Exists, OuterRef

from .models import Appointment, Patient, Provider, Service

_APPT_DISCIPLINE_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
)


def provider_for_doctor_user(user) -> Provider | None:
    if not user or getattr(user, "role", None) != "doctor":
        return None
    return Provider.objects.filter(user=user).first()


def patient_has_discipline_appointments(patient_id: int, service_type: str) -> bool:
    return Appointment.objects.filter(
        patient_id=patient_id,
        booked_service__service_type=service_type,
    ).exclude(status__in=_APPT_DISCIPLINE_EXCLUDED).exists()


def patient_has_any_appointments(patient_id: int) -> bool:
    return Appointment.objects.filter(patient_id=patient_id).exists()


def clinical_access_level(provider: Provider | None, patient: Patient) -> str:
    """
    full — doctor may edit demographics and chart notes for this patient.
    read_only — may open chart/history but not change records (other discipline only).
    """
    if provider is None:
        return "full"
    st = (provider.primary_service_type or "").strip()
    if not st:
        return "full"
    if not patient_has_any_appointments(patient.pk):
        return "full"
    if patient_has_discipline_appointments(patient.pk, st):
        return "full"
    return "read_only"


def clinical_access_message(provider: Provider | None, access: str) -> str:
    if access != "read_only" or provider is None:
        return ""
    discipline = (provider.primary_service_type or "chiropractic").strip()
    if discipline == Service.ServiceType.MASSAGE:
        return (
            "This patient has no massage visits on file. You can review their record, "
            "but only massage staff or the front desk can edit their chart."
        )
    return (
        "This patient has no chiropractic visits on file. You can review their record, "
        "but only chiropractic doctors or the front desk can edit their chart."
    )


def filter_patient_queryset_for_provider_discipline(qs, provider: Provider):
    """Directory list: patients in this provider's discipline (or brand-new with no visits)."""
    st = (provider.primary_service_type or "").strip()
    if not st:
        return qs
    in_discipline = Appointment.objects.filter(
        patient_id=OuterRef("pk"),
        booked_service__service_type=st,
    ).exclude(status__in=_APPT_DISCIPLINE_EXCLUDED)
    has_any_appt = Appointment.objects.filter(patient_id=OuterRef("pk"))
    return qs.filter(Exists(in_discipline) | ~Exists(has_any_appt))


def appointment_matches_provider_discipline(appointment: Appointment, provider: Provider) -> bool:
    st = (provider.primary_service_type or "").strip()
    if not st or not appointment.booked_service_id:
        return False
    return appointment.booked_service.service_type == st
