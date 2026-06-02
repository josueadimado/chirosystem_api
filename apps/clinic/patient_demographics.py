"""Computed and stored patient demographics for chart / directory views."""

from __future__ import annotations

from datetime import timedelta

from decimal import Decimal

from django.db.models import Count, Exists, F, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from .models import Appointment, Invoice, Patient, Visit
from .patient_phone import duplicate_patient_message, resolve_patient_profile_duplicate

_UNPAID_INVOICE_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.OVERDUE)

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
        no_show_count=Count(
            "appointment",
            filter=Q(appointment__status=Appointment.Status.NO_SHOW),
            distinct=True,
        ),
        visit_count=Count(
            "visit",
            filter=Q(visit__status=Visit.Status.COMPLETED),
            distinct=True,
        ),
        last_visit=Subquery(last_completed_visit.values("visit_day")[:1]),
        last_service=Subquery(last_appt.values("booked_service__name")[:1]),
        next_appointment_date=Subquery(next_appt.values("appointment_date")[:1]),
        next_appointment_time=Subquery(next_appt.values("start_time")[:1]),
        _first_appointment_date=Min(
            "appointment__appointment_date",
            filter=~Q(appointment__status__in=_APPT_EXCLUDED),
        ),
    ).annotate(
        # Alias must differ from Patient.date_established (staff override column).
        effective_date_established=Coalesce(F("date_established"), F("_first_appointment_date")),
    )


def _unpaid_invoice_total_subquery(*, kind: str | None):
    """
    Per-patient sum of unpaid invoice totals.

    Uses a subquery so totals stay correct when combined with appointment/visit
    counts on the same queryset (plain Sum() on invoice__ would multiply rows).
    """
    filters = {
        "patient_id": OuterRef("pk"),
        "status__in": _UNPAID_INVOICE_STATUSES,
    }
    if kind is not None:
        filters["kind"] = kind
    return (
        Invoice.objects.filter(**filters)
        .values("patient_id")
        .annotate(_sum=Sum("total_amount"))
        .values("_sum")[:1]
    )


def annotate_patient_unpaid_balances(qs):
    """Unpaid invoice totals by kind (issued + overdue) for admin patient directory filters."""
    zero = Value(Decimal("0.00"))
    return qs.annotate(
        balance_visit=Coalesce(
            Subquery(_unpaid_invoice_total_subquery(kind=Invoice.Kind.VISIT)),
            zero,
        ),
        balance_no_show_fee=Coalesce(
            Subquery(_unpaid_invoice_total_subquery(kind=Invoice.Kind.NO_SHOW_FEE)),
            zero,
        ),
        balance_late_cancel_fee=Coalesce(
            Subquery(_unpaid_invoice_total_subquery(kind=Invoice.Kind.LATE_CANCEL_FEE)),
            zero,
        ),
        has_overdue_invoice=Exists(
            Invoice.objects.filter(patient_id=OuterRef("pk"), status=Invoice.Status.OVERDUE),
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


def _first_appointment_date(patient: Patient):
    return (
        Appointment.objects.filter(patient=patient)
        .exclude(status__in=_APPT_EXCLUDED)
        .aggregate(d=Min("appointment_date"))["d"]
    )


def effective_date_established(patient: Patient):
    """Manual staff date when set; otherwise first non-cancelled appointment."""
    manual = patient.date_established
    if manual:
        return manual
    return _first_appointment_date(patient)


def patient_account_summary(patient: Patient) -> dict:
    """
    Unpaid balances (by invoice kind) and appointment counts for chart/history headers.
    Uses subquery balances so totals match Admin → Billing.
    """
    annotated = annotate_patient_unpaid_balances(
        annotate_patient_list_stats(Patient.objects.filter(pk=patient.pk)),
    ).first()
    bal_visit = getattr(annotated, "balance_visit", None) or Decimal("0")
    bal_no_show = getattr(annotated, "balance_no_show_fee", None) or Decimal("0")
    bal_late_cancel = getattr(annotated, "balance_late_cancel_fee", None) or Decimal("0")
    balance_total = (bal_visit + bal_no_show + bal_late_cancel).quantize(Decimal("0.01"))

    today = timezone.localdate()
    upcoming_qs = (
        Appointment.objects.filter(patient=patient, appointment_date__gte=today)
        .exclude(status__in=_FUTURE_APPT_EXCLUDED)
        .order_by("appointment_date", "start_time")
    )
    next_appt = upcoming_qs.first()
    next_date = str(next_appt.appointment_date) if next_appt else None
    next_time = next_appt.start_time.strftime("%I:%M %p") if next_appt else None

    cancelled_count = Appointment.objects.filter(
        patient=patient,
        status=Appointment.Status.CANCELLED,
    ).count()

    def money(d: Decimal) -> str:
        return str(d.quantize(Decimal("0.01")))

    return {
        "balance_total": money(balance_total),
        "balance_visit": money(bal_visit),
        "balance_no_show_fee": money(bal_no_show),
        "balance_late_cancel_fee": money(bal_late_cancel),
        "has_overdue": bool(getattr(annotated, "has_overdue_invoice", False)),
        "visit_count": int(getattr(annotated, "visit_count", 0) or 0),
        "no_show_count": int(getattr(annotated, "no_show_count", 0) or 0),
        "upcoming_count": upcoming_qs.count(),
        "cancelled_count": cancelled_count,
        "next_appointment_date": next_date,
        "next_appointment_time": next_time,
    }


def patient_demographics_summary(patient: Patient) -> dict:
    """
    Extra demographics for patient_detail responses.
    date_established = staff override or first non-cancelled appointment.
    last_seen = most recent completed visit only (never a future booking).
    """
    first_date = _first_appointment_date(patient)
    manual = patient.date_established
    effective = manual or first_date

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
        "payment_profile": (patient.payment_profile or "").strip(),
        "date_established": str(effective) if effective else None,
        "date_established_override": str(manual) if manual else None,
        "first_appointment_date": str(first_date) if first_date else None,
        "last_seen": last_seen,
    }


def apply_patient_intake_validated_data(
    patient: Patient,
    data: dict,
    *,
    allow_identity_fields: bool = False,
    allow_date_established: bool = False,
    allow_online_waived: bool = False,
    allow_sms_consent: bool = False,
    allow_communication_prefs: bool = False,
) -> str | None:
    """
    Apply PATCH intake fields to patient. Returns an error message when duplicate
    rules block the save; otherwise None (caller should patient.save()).
    """
    if allow_identity_fields:
        if "first_name" in data:
            patient.first_name = (data["first_name"] or "").strip()
        if "last_name" in data:
            patient.last_name = (data["last_name"] or "").strip()
        if "phone" in data:
            patient.phone = data["phone"]
        if "email" in data:
            patient.email = (data["email"] or "").strip()

    for field in (
        "address_line1",
        "address_line2",
        "city_state_zip",
        "emergency_contact_name",
        "emergency_contact_phone",
        "marital_status",
    ):
        if field in data:
            setattr(patient, field, data[field] or "")

    if "date_of_birth" in data:
        patient.date_of_birth = data["date_of_birth"]

    dup = resolve_patient_profile_duplicate(
        first_name=patient.first_name,
        last_name=patient.last_name,
        phone=patient.phone,
        date_of_birth=patient.date_of_birth,
        exclude_pk=patient.pk,
    )
    if dup is not None:
        return duplicate_patient_message(dup, updating=True)

    if allow_date_established and "date_established" in data:
        patient.date_established = data["date_established"]
    if allow_online_waived and "online_chiro_intake_waived" in data:
        patient.online_chiro_intake_waived = bool(data["online_chiro_intake_waived"])

    if allow_sms_consent and "sms_consent" in data:
        new_consent = bool(data["sms_consent"])
        if new_consent and not patient.sms_consent:
            patient.sms_consent_at = timezone.now()
        elif not new_consent:
            patient.sms_consent_at = None
        patient.sms_consent = new_consent

    if "payment_profile" in data:
        profile = (data.get("payment_profile") or "").strip().lower()
        if profile in ("", "insurance", "cash"):
            patient.payment_profile = profile

    if allow_communication_prefs:
        from apps.clinic.patient_communication_prefs import (
            DEFAULT_NOTIFY_BILLS,
            DEFAULT_NOTIFY_BOOKING,
            DEFAULT_NOTIFY_REMINDERS,
            normalize_notify_channel,
        )

        if "notify_booking" in data:
            patient.notify_booking = normalize_notify_channel(
                data["notify_booking"], default=DEFAULT_NOTIFY_BOOKING
            )
        if "notify_reminders" in data:
            patient.notify_reminders = normalize_notify_channel(
                data["notify_reminders"], default=DEFAULT_NOTIFY_REMINDERS
            )
        if "notify_bills" in data:
            patient.notify_bills = normalize_notify_channel(
                data["notify_bills"], default=DEFAULT_NOTIFY_BILLS
            )

    return None


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
