"""Computed and stored patient demographics for chart / directory views."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Min, OuterRef, Q, Subquery
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Appointment, Patient, Visit

_APPT_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
)
_FUTURE_APPT_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
    Appointment.Status.COMPLETED,
)


def annotate_patient_list_stats(qs):
    """Directory list stats: completed visits, last service, next booking, date established."""
    appt_base = Appointment.objects.filter(patient_id=OuterRef("pk")).exclude(status__in=_APPT_EXCLUDED)
    last_appt = appt_base.order_by("-appointment_date", "-start_time")
    last_completed_visit = (
        Visit.objects.filter(
            patient_id=OuterRef("pk"),
            status=Visit.Status.COMPLETED,
            completed_at__isnull=False,
            completed_at__lte=timezone.now(),
        )
        .order_by("-completed_at")
        .annotate(visit_day=TruncDate("completed_at"))
    )
    today = timezone.localdate()
    next_appt = (
        Appointment.objects.filter(patient_id=OuterRef("pk"), appointment_date__gte=today)
        .exclude(status__in=_FUTURE_APPT_EXCLUDED)
        .order_by("appointment_date", "start_time")
    )
    return qs.annotate(
        visit_count=Count(
            "visit",
            filter=Q(visit__status=Visit.Status.COMPLETED),
            distinct=True,
        ),
        last_visit=Subquery(last_completed_visit.values("visit_day")[:1]),
        last_service=Subquery(last_appt.values("booked_service__name")[:1]),
        next_appointment_date=Subquery(next_appt.values("appointment_date")[:1]),
        next_appointment_time=Subquery(next_appt.values("start_time")[:1]),
        date_established=Min(
            "appointment__appointment_date",
            filter=~Q(appointment__status__in=_APPT_EXCLUDED),
        ),
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


def apply_patient_directory_list_filter(qs, directory: str):
    """
    Filter + sort patient list queryset after PatientViewSet list annotations
    (visit_count, last_visit, next_appointment_date, …).
    """
    key = (directory or "").strip().lower()
    default_order = ("-last_visit", "last_name", "first_name")
    if not key:
        return qs.order_by(*default_order)

    today = timezone.localdate()

    if key == "upcoming":
        return qs.filter(next_appointment_date__isnull=False).order_by(
            "next_appointment_date",
            "next_appointment_time",
            "last_name",
            "first_name",
        )
    if key == "no_upcoming":
        return qs.filter(next_appointment_date__isnull=True).order_by(*default_order)
    if key == "never_seen":
        return qs.filter(last_visit__isnull=True).order_by("last_name", "first_name")
    if key == "seen_recent":
        since = today - timedelta(days=30)
        return qs.filter(last_visit__gte=since).order_by(*default_order)
    if key == "recall_due":
        before = today - timedelta(days=180)
        return qs.filter(last_visit__isnull=False, last_visit__lt=before).order_by(
            "last_visit", "last_name", "first_name"
        )
    if key == "new_patients":
        return qs.filter(visit_count=0).order_by("-next_appointment_date", "last_name", "first_name")

    return qs.order_by(*default_order)
