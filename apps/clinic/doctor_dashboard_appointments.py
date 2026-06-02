"""Doctor dashboard appointment list: single day (default today) or date range."""

from __future__ import annotations

from itertools import groupby

from django.utils import timezone

from .doctor_dashboard_schedule_sort import sort_doctor_dashboard_appointments
from .models import Appointment, Invoice, Visit


def _parse_date_param(value: str | None):
    if not value:
        return None
    try:
        return timezone.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def appointments_for_doctor_dashboard(provider, request) -> list[Appointment]:
    """Load and sort appointments for GET /doctor/appointments/."""
    date_from = _parse_date_param(request.query_params.get("date_from"))
    date_to = _parse_date_param(request.query_params.get("date_to"))
    single = _parse_date_param(request.query_params.get("date"))

    base = Appointment.objects.filter(provider=provider).select_related("patient", "booked_service")

    if date_from and date_to:
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        qs = base.filter(appointment_date__gte=date_from, appointment_date__lte=date_to).order_by(
            "appointment_date", "start_time"
        )
        out: list[Appointment] = []
        for day, group in groupby(qs, key=lambda a: a.appointment_date):
            out.extend(sort_doctor_dashboard_appointments(list(group), appt_date=day))
        return out

    appt_date = single or timezone.localdate()
    qs = base.filter(appointment_date=appt_date)
    return sort_doctor_dashboard_appointments(list(qs), appt_date=appt_date)


def serialize_doctor_dashboard_appointments(appt_list: list[Appointment]) -> list[dict]:
    from .square_payment import try_reconcile_invoice_from_square

    appt_ids = [x.id for x in appt_list]
    visit_by_aid = {
        v.appointment_id: v
        for v in Visit.objects.filter(appointment_id__in=appt_ids).only(
            "id", "appointment_id", "reason_for_visit"
        )
    }
    invoice_by_aid = {
        inv.appointment_id: inv
        for inv in Invoice.objects.filter(appointment_id__in=appt_ids).only(
            "id",
            "appointment_id",
            "invoice_number",
            "total_amount",
            "status",
            "kind",
        )
    }
    from apps.clinic.appointment_display import appointment_ui_status

    data: list[dict] = []
    for a in appt_list:
        inv = invoice_by_aid.get(a.id)
        if (
            a.status == Appointment.Status.AWAITING_PAYMENT
            and inv
            and inv.status in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)
        ):
            try_reconcile_invoice_from_square(inv.id)
            a.refresh_from_db()
            inv = Invoice.objects.filter(pk=inv.id).only(
                "id", "appointment_id", "invoice_number", "total_amount", "status"
            ).first()
        v = visit_by_aid.get(a.id)
        row = {
            "id": a.id,
            "patient": f"{a.patient.first_name} {a.patient.last_name}",
            "patient_id": a.patient_id,
            "service": a.booked_service.name if a.booked_service else "",
            "booked_service_id": a.booked_service_id,
            "service_type": a.booked_service.service_type if a.booked_service else "",
            "appointment_date": str(a.appointment_date),
            "start_time": a.start_time.strftime("%I:%M %p"),
            "start_time_iso": a.start_time.isoformat(timespec="seconds"),
            "end_time_iso": a.end_time.isoformat(timespec="seconds"),
            "end_time": a.end_time.strftime("%I:%M %p"),
            "status": a.status,
            "display_status": appointment_ui_status(a, invoice_kind=inv.kind if inv else None),
            "invoice_kind": inv.kind if inv else None,
            "auto_no_show_processed_at": (
                a.auto_no_show_processed_at.isoformat() if a.auto_no_show_processed_at else None
            ),
            "clinical_handoff_notes": a.clinical_handoff_notes or "",
            "reason_for_visit": v.reason_for_visit if v else "",
            "visit_id": v.id if v else None,
            "card_last4": a.patient.card_last4 or "",
            "card_brand": a.patient.card_brand or "",
            "patient_payment_profile": (a.patient.payment_profile or "").strip(),
        }
        inv = invoice_by_aid.get(a.id)
        if (
            a.status == Appointment.Status.AWAITING_PAYMENT
            and inv
            and inv.status in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)
        ):
            row["invoice_id"] = inv.id
            row["invoice_number"] = inv.invoice_number
            row["invoice_total"] = str(inv.total_amount)
        data.append(row)
    return data
